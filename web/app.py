# web/app.py
# Reverto Web Portal — FastAPI backend
# Multi-bot: reads state from logs/{slug}.state.json per bot.
# Manages bot processes via start/stop API.
# Portal can restart itself via /api/portal/restart.

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import yaml

from config.config_loader import load_bot_config
from config.models import BotConfig
from core import credentials
from notifications.telegram import TelegramNotifier

import ccxt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Maximum file size for state.json — voorkomt OOM bij corrupte/oversize files
_MAX_STATE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


class BotStateModel(BaseModel):
    """Pydantic schema voor logs/{slug}.state.json — beschermt tegen
    corrupte of geïnjecteerde JSON met onverwachte types of waarden.
    Extra velden worden genegeerd (niet gestript) zodat toekomstige
    velden niet crashen op oude portal versies."""

    model_config = ConfigDict(extra="ignore")

    bot_name:            str   = ""
    mode:                str   = ""
    exchange:            str   = ""
    pair:                str   = ""
    balance_btc:         float = Field(default=0.0, ge=-1000.0, le=1000.0)
    initial_balance_btc: float = 0.0
    total_pnl_btc:       float = 0.0
    win_rate:            float = 0.0
    open_deals_count:    int   = 0
    closed_deals_count:  int   = 0
    open_deals:          list  = Field(default_factory=list)
    closed_deals:        list  = Field(default_factory=list)
    current_price:       float = 0.0
    schedule_open:       bool  = False
    started_at:          Optional[str] = None
    updated_at:          Optional[str] = None
    fees_paid_btc:       float = 0.0
    indicators:          dict  = Field(default_factory=dict)

# ── API key auth ──────────────────────────────────────────────────────────────
# Read from REVERTO_API_KEY or auto-generate one and surface it via WARNING
# log so the operator can copy it. Auto-generated keys are ephemeral —
# restart of the portal yields a fresh key.
_API_KEY = os.environ.get("REVERTO_API_KEY")
if not _API_KEY:
    _API_KEY = secrets.token_hex(32)
    logger.warning(
        "REVERTO_API_KEY not set — generated ephemeral key for this session: %s "
        "(set REVERTO_API_KEY=... in your environment to make it persistent)",
        _API_KEY,
    )


def verify_api_key(request: Request) -> str:
    """FastAPI dependency: require X-API-Key header or ?api_key= query param.

    Used on all mutating endpoints (start/stop/restart). GET endpoints stay
    public so dashboards and read-only clients still work without a key.

    Returns een 8-char sha256 hint van de aangeleverde key, bruikbaar als
    actor-identifier in de audit log zonder de key zelf vast te leggen.
    """
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if not provided or not secrets.compare_digest(provided, _API_KEY):
        # Generieke message — onthul niet of de key ontbrak of fout was,
        # zodat een attacker geen extra info krijgt over geldige requests.
        raise HTTPException(status_code=401, detail="Unauthorized")
    return hashlib.sha256(provided.encode("utf-8")).hexdigest()[:8]

# Module-level ccxt client — reused across /api/price calls so we don't pay
# instantiation overhead on every request.
_bitget_client = ccxt.bitget({"options": {"defaultType": "swap"}})

# ccxt clients muteren interne state (rate-limit window, request id, cookie jar)
# en zijn niet thread-safe. Serialiseer alle /api/price calls met deze lock zodat
# concurrent worker threads vanuit asyncio.to_thread elkaar niet corrumperen.
_price_lock = asyncio.Lock()

BASE_DIR   = Path(__file__).parent.parent
STATIC_DIR = Path(__file__).parent / "static"
CONFIG_DIR = BASE_DIR / "config" / "bots"
LOG_DIR    = BASE_DIR / "logs"
PID_DIR    = LOG_DIR / "pids"
PYTHON_BIN = sys.executable

# ── Audit logging ─────────────────────────────────────────────────────────────
# Aparte logger "reverto.audit" → logs/audit.log met rotation. Propagate=False
# zodat audit events niet ook nog in portal.log belanden. Format:
#     2026-04-15T12:34:56+0000 | bot_start | btc_paper | a1b2c3d4
_audit_logger = logging.getLogger("reverto.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
if not _audit_logger.handlers:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _audit_handler = RotatingFileHandler(
        LOG_DIR / "audit.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _audit_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    )
    _audit_logger.addHandler(_audit_handler)


def _audit(action: str, slug: str = "-", key_hint: str = "-") -> None:
    """Schrijf één regel naar de audit log."""
    _audit_logger.info("%s | %s | %s", action, slug, key_hint)


_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def slugify(name: str) -> str:
    """Zet een vrije bot-naam om naar een veilig filename stem.

    Lowercase, spaties → underscore, alles buiten [a-z0-9_] gestript,
    meervoudige underscores worden samengetrokken. Lege resultaten
    raise ValueError zodat de caller een 400 kan returnen.
    """
    cleaned = name.strip().lower().replace(" ", "_")
    cleaned = _SLUG_RE.sub("", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        raise ValueError(f"name {name!r} produces empty slug")
    return cleaned


# ── Bot registry ──────────────────────────────────────────────────────────────

class BotInfo:
    def __init__(self, slug: str, config_file: str):
        self.slug        = slug
        self.config_file = config_file

    @property
    def pid_file(self)   -> Path: return PID_DIR / f"{self.slug}.pid"
    @property
    def log_file(self)   -> Path: return LOG_DIR  / f"{self.slug}.log"
    @property
    def state_file(self) -> Path: return LOG_DIR  / f"{self.slug}.state.json"

    @property
    def running(self) -> bool:
        if not self.pid_file.exists():
            return False
        try:
            pid = int(self.pid_file.read_text().strip())
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    @property
    def pid(self) -> Optional[int]:
        try:
            return int(self.pid_file.read_text().strip())
        except Exception:
            return None

    def read_state(self) -> dict:
        try:
            # Bounded read — lees maximaal _MAX_STATE_FILE_SIZE + 1 bytes in
            # één open()+read() zodat er geen TOCTOU gat is tussen een
            # aparte stat() en read_text(). Als er een byte extra binnenkomt
            # is de file groter dan toegestaan en vallen we terug op default.
            try:
                with open(self.state_file, "rb") as fh:
                    raw_bytes = fh.read(_MAX_STATE_FILE_SIZE + 1)
            except FileNotFoundError:
                return self._default_state()
            except MemoryError:
                logger.warning(
                    "State file %s triggered MemoryError, using defaults",
                    self.state_file,
                )
                return self._default_state()

            if len(raw_bytes) > _MAX_STATE_FILE_SIZE:
                logger.warning(
                    "State file %s exceeds %d bytes, using defaults",
                    self.state_file, _MAX_STATE_FILE_SIZE,
                )
                return self._default_state()

            raw = json.loads(raw_bytes.decode("utf-8"))
            validated = BotStateModel.model_validate(raw).model_dump()
            validated["running"]     = self.running
            validated["slug"]        = self.slug
            validated["config_file"] = self.config_file
            return validated
        except ValidationError as e:
            logger.warning("State validation failed for %s: %s", self.slug, e)
        except Exception as e:
            logger.warning("State read failed for %s: %s", self.slug, type(e).__name__)

        return self._default_state()

    def _default_state(self) -> dict:
        return {
            "slug":                self.slug,
            "config_file":         self.config_file,
            "bot_name":            self.slug,
            "mode":                "paper",
            "exchange":            "—",
            "pair":                "BTC/USD",
            "running":             self.running,
            "current_price":       0.0,
            "schedule_open":       False,
            "balance_btc":         0.0,
            "initial_balance_btc": 0.0,
            "total_pnl_btc":       0.0,
            "win_rate":            0.0,
            "open_deals_count":    0,
            "closed_deals_count":  0,
            "open_deals":          [],
            "closed_deals":        [],
            "indicators":          {},
            "started_at":          None,
            "updated_at":          None,
        }


class BotRegistry:
    # TTL voor de filesystem-glob in refresh(). Bij hoge API frequentie
    # (dashboard polls elke 5s, plus /api/price, plus tail_logs) voerde
    # iedere call een eigen glob uit — overbodig en duur op trage
    # filesystems (NFS/SMB). 5s is ruim binnen de UI-refresh cadans.
    _REFRESH_TTL = 5.0

    def __init__(self):
        self._bots: dict[str, BotInfo] = {}
        self._lock = asyncio.Lock()
        self._last_refresh: float = 0.0
        # In-progress starts per slug. Beschermt tegen dubbel-klik races
        # waarbij main_paper.py nog geen PID file heeft geschreven en
        # bot.running dus False retourneert voor beide kliks.
        self._starting: set[str] = set()
        # Initiële populatie: gebeurt vóór de event loop bestaat, dus
        # geen lock-contention mogelijk — direct synchroon vullen.
        self._refresh_locked()
        self._last_refresh = time.time()

    def _refresh_locked(self) -> None:
        """Voer de glob uit; caller moet de lock vasthouden (of init zijn)."""
        current: set[str] = set()
        if CONFIG_DIR.exists():
            for f in sorted(CONFIG_DIR.glob("*.yaml")):
                slug = f.stem
                current.add(slug)
                if slug not in self._bots:
                    self._bots[slug] = BotInfo(
                        slug=slug,
                        config_file=str(f.relative_to(BASE_DIR))
                    )
        for stale in [s for s in self._bots if s not in current]:
            del self._bots[stale]

    async def refresh(self) -> None:
        async with self._lock:
            if time.time() - self._last_refresh <= self._REFRESH_TTL:
                return
            self._refresh_locked()
            self._last_refresh = time.time()

    async def all(self) -> list[BotInfo]:
        await self.refresh()
        async with self._lock:
            return list(self._bots.values())

    async def get(self, slug: str) -> Optional[BotInfo]:
        await self.refresh()
        async with self._lock:
            return self._bots.get(slug)

    async def invalidate(self) -> None:
        """Forceer een refresh bij de volgende all()/get() call.
        Aanroepen na YAML create/delete in de bot management endpoints."""
        async with self._lock:
            self._last_refresh = 0.0

    async def begin_start(self, slug: str) -> bool:
        """Claim de start-slot voor `slug`. Retourneert True als we
        de slot kregen, False als er al een start in progress is."""
        async with self._lock:
            if slug in self._starting:
                return False
            self._starting.add(slug)
            return True

    async def end_start(self, slug: str) -> None:
        """Release de start-slot voor `slug`. Idempotent."""
        async with self._lock:
            self._starting.discard(slug)


registry = BotRegistry()


# ── Process control ───────────────────────────────────────────────────────────

async def start_bot(slug: str) -> dict:
    bot = await registry.get(slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}
    if bot.running:
        return {"ok": False, "error": f"{slug} already running (PID {bot.pid})"}

    # Claim de start-slot. Voorkomt dat een dubbel-klik (beide calls zien
    # bot.running=False omdat main_paper.py nog niet is opgestart) twee
    # subprocessen spawnt.
    if not await registry.begin_start(slug):
        return {"ok": False, "error": "Bot is already starting"}

    try:
        PID_DIR.mkdir(parents=True, exist_ok=True)

        # Use absolute path to main_paper.py and same venv Python as portal
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BASE_DIR)

        # Context manager closes the parent's FD after Popen duplicates it —
        # the child process keeps its own handle, no FD leak in the portal.
        with open(bot.log_file, "a") as log_out:
            proc = subprocess.Popen(
                [PYTHON_BIN, str(BASE_DIR / "main_paper.py"),
                 "--config", bot.config_file],
                cwd=str(BASE_DIR),
                stdout=log_out,
                stderr=log_out,
                env=env,
                start_new_session=True
            )
        logger.info(f"Bot {slug} started (PID {proc.pid})")

        # Wacht maximaal 3s tot main_paper.py zijn eigen PID file heeft
        # geschreven. Zolang we die niet zien houdt de starting-slot stand
        # zodat een volgende klik netjes een "already starting" krijgt in
        # plaats van een tweede subprocess.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if bot.pid_file.exists():
                break
            await asyncio.sleep(0.1)

        return {"ok": True, "message": f"{slug} started (PID {proc.pid})"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await registry.end_start(slug)


def _pid_alive(pid: int) -> bool:
    """Return True if `pid` is still a live process. Uses signal 0 probe."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — treat as alive.
        return True


async def stop_bot(slug: str) -> dict:
    bot = await registry.get(slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}
    if not bot.running:
        # Clean up stale PID file if the process is gone but file remains.
        if bot.pid_file.exists():
            try:
                bot.pid_file.unlink()
                logger.info(f"Bot {slug}: removed stale PID file")
            except OSError:
                pass
        return {"ok": False, "error": f"{slug} is not running"}

    pid = bot.pid
    try:
        os.kill(pid, signal.SIGTERM)
        logger.info(f"Bot {slug}: sent SIGTERM to PID {pid}")
    except ProcessLookupError:
        if bot.pid_file.exists():
            try:
                bot.pid_file.unlink()
            except OSError:
                pass
        return {"ok": False, "error": "Process not found — already stopped?"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Wait up to 10s for graceful exit (poll PID file + liveness).
    # 10s instead of 5s because PaperEngine.stop() joins the notify
    # worker with a 15s budget so notify_shutdown + notify_stop have
    # time to actually flush to Telegram (single HTTP POST per message,
    # 10s httpx timeout each). 5s wasn't enough — the bot got SIGKILL'd
    # mid-send and the stop notification never landed.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not _pid_alive(pid) or not bot.pid_file.exists():
            break
        await asyncio.sleep(0.1)

    if _pid_alive(pid):
        # Graceful shutdown timed out — escalate to SIGKILL.
        logger.warning(
            f"Bot {slug}: PID {pid} still alive after 10s — escalating to SIGKILL"
        )
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Brief wait for the kernel to reap the process.
        for _ in range(20):
            if not _pid_alive(pid):
                break
            await asyncio.sleep(0.1)

    # Remove any lingering PID file so the next start doesn't see stale state.
    if bot.pid_file.exists():
        try:
            bot.pid_file.unlink()
            logger.info(f"Bot {slug}: cleaned up PID file after stop")
        except OSError:
            pass

    logger.info(f"Bot {slug} stopped (PID {pid})")
    return {"ok": True, "message": f"{slug} stopped (PID {pid})"}


async def restart_bot(slug: str) -> dict:
    bot = await registry.get(slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}

    # Fire restart notification before tearing the subprocess down.
    # The portal owns the restart lifecycle, so the bot itself never
    # gets a chance to send this from inside its own engine loop.
    try:
        cfg = load_bot_config(bot.config_file)
        notifier = TelegramNotifier(notify_on=cfg.telegram.notify_on)
        notifier.notify_restart(cfg.name)
    except Exception as e:
        logger.warning("restart notify failed for %s: %s", slug, e)

    if bot.running:
        stop_result = await stop_bot(slug)
        if not stop_result.get("ok"):
            return stop_result

    # Poll up to 5s for PID file to disappear so start_bot() doesn't see
    # stale "already running" state from a slow-exiting previous process.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not bot.pid_file.exists() and not bot.running:
            break
        await asyncio.sleep(0.1)

    return await start_bot(slug)


def _do_portal_restart():
    """Restart the portal process using os.execv — replaces current process."""
    time.sleep(0.8)  # Give the HTTP response time to reach the browser
    logger.info("=== Portal restarting ===")
    logger.info("Portal restarting via os.execv...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ── FastAPI ───────────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach standard hardening headers to every HTTP response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"]        = "no-referrer"
        return response


# Rate limiter — beperkt brute force en DoS op control endpoints. Sleutel
# per remote IP; in een setup achter een reverse proxy moet je X-Forwarded-For
# parsing toevoegen via een eigen key_func.
limiter = Limiter(key_func=get_remote_address)


async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Too many requests"})


app = FastAPI(title="Reverto Portal", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    f = STATIC_DIR / "index.html"
    return HTMLResponse(f.read_text(encoding="utf-8") if f.exists() else "<h1>Not found</h1>")


@app.get("/api/bots")
async def get_bots():
    bots      = [b.read_state() for b in await registry.all()]
    total_pnl = sum(b.get("total_pnl_btc", 0) for b in bots)
    active    = sum(1 for b in bots if b.get("running"))
    open_cnt  = sum(b.get("open_deals_count", 0) for b in bots)

    all_open = []
    for b in bots:
        for d in b.get("open_deals", []):
            d["bot_name"] = b.get("bot_name", b.get("slug"))
            d["bot_slug"] = b.get("slug")
            d["exchange"]  = b.get("exchange")
            all_open.append(d)

    return {
        "bots": bots,
        "summary": {
            "total_pnl_btc": round(total_pnl, 8),
            "active_bots":   active,
            "total_bots":    len(bots),
            "open_deals":    open_cnt,
        },
        "all_open_deals": all_open,
    }


@app.get("/api/bots/{slug}")
async def get_bot(slug: str):
    bot = await registry.get(slug)
    if not bot:
        return {"error": f"Unknown bot: {slug}"}
    return bot.read_state()


@app.post("/api/bots/{slug}/start")
@limiter.limit("20/minute")
async def api_start(slug: str, request: Request, key_hint: str = Depends(verify_api_key)):
    _audit("bot_start", slug, key_hint)
    return await start_bot(slug)

@app.post("/api/bots/{slug}/stop")
@limiter.limit("20/minute")
async def api_stop(slug: str, request: Request, key_hint: str = Depends(verify_api_key)):
    _audit("bot_stop", slug, key_hint)
    return await stop_bot(slug)

@app.post("/api/bots/{slug}/restart")
@limiter.limit("20/minute")
async def api_restart(slug: str, request: Request, key_hint: str = Depends(verify_api_key)):
    _audit("bot_restart", slug, key_hint)
    return await restart_bot(slug)


@app.post("/api/portal/restart")
@limiter.limit("5/minute")
async def api_portal_restart(request: Request, key_hint: str = Depends(verify_api_key)):
    """Restart the portal process.

    Uses os.execv in a background thread so the HTTP response can reach
    the browser before the process is replaced.
    """
    _audit("portal_restart", "-", key_hint)
    logger.info("Portal restart requested via API")
    t = threading.Thread(target=_do_portal_restart, daemon=True)
    t.start()
    return {"ok": True, "message": "Portal restarting — reconnecting in a few seconds..."}


@app.get("/api/portal/status")
async def api_portal_status():
    """Simple health check — browser polls this to detect when portal is back."""
    return {"ok": True, "pid": os.getpid()}


@app.get("/api/price")
async def api_price():
    """
    Always-on BTC price endpoint — fetches directly from Bitget
    regardless of whether any bot is running.
    """
    try:
        # ccxt is blocking — push it to a worker thread so the event loop stays free.
        # _price_lock serialiseert concurrent calls op de gedeelde ccxt client.
        async with _price_lock:
            ticker = await asyncio.to_thread(_bitget_client.fetch_ticker, "BTCUSD")
        price = ticker.get("last") or ticker.get("close") or 0.0
        return {"price": price, "pair": "BTC/USD", "source": "bitget"}
    except Exception:
        # Fall back to first running bot's price if available
        for bot in await registry.all():
            state = bot.read_state()
            if state.get("current_price"):
                return {"price": state["current_price"], "pair": "BTC/USD", "source": "bot"}
        return {"price": 0.0, "pair": "BTC/USD", "source": "unavailable"}


# ── Exchange credentials API ──────────────────────────────────────────────────

_KNOWN_EXCHANGES = ("bitget", "kraken")


@app.get("/api/exchanges")
async def list_exchanges():
    """Welke exchanges Reverto kent en of er credentials voor opgeslagen zijn."""
    return {
        "exchanges": [
            {"name": name, "has_keys": credentials.has_keys(name)}
            for name in _KNOWN_EXCHANGES
        ]
    }


class ExchangeKeysBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)
    api_secret: str = Field(min_length=1, max_length=512)


@app.post("/api/exchanges/{name}/keys")
@limiter.limit("10/minute")
async def save_exchange_keys(
    name: str,
    body: ExchangeKeysBody,
    request: Request,
    key_hint: str = Depends(verify_api_key),
):
    if name not in _KNOWN_EXCHANGES:
        raise HTTPException(status_code=404, detail="Unknown exchange")
    credentials.save_keys(name, body.api_key, body.api_secret)
    _audit("exchange_keys_set", name, key_hint)
    return {"ok": True, "exchange": name}


@app.delete("/api/exchanges/{name}/keys")
@limiter.limit("10/minute")
async def delete_exchange_keys(
    name: str,
    request: Request,
    key_hint: str = Depends(verify_api_key),
):
    if name not in _KNOWN_EXCHANGES:
        raise HTTPException(status_code=404, detail="Unknown exchange")
    removed = credentials.delete_keys(name)
    if not removed:
        raise HTTPException(status_code=404, detail="No keys stored for exchange")
    _audit("exchange_keys_delete", name, key_hint)
    return {"ok": True, "exchange": name}


# ── Bot YAML beheer ───────────────────────────────────────────────────────────


def _bot_yaml_path(slug: str) -> Path:
    return CONFIG_DIR / f"{slug}.yaml"


def _validate_bot_payload(payload: dict) -> BotConfig:
    """Valideer een rauwe dict via BotConfig. Accepteert zowel
    {"bot": {...}} als {...} aan top-level zodat de portal flexibel
    blijft. Raised ValueError bij invalide config."""
    inner = payload.get("bot", payload)
    try:
        return BotConfig(**inner)
    except Exception as e:
        raise ValueError(str(e)) from e


@app.post("/api/bots")
@limiter.limit("20/minute")
async def create_bot(
    body: dict,
    request: Request,
    key_hint: str = Depends(verify_api_key),
):
    """Maak een nieuwe bot YAML aan. Slug komt uit de bot naam."""
    try:
        cfg = _validate_bot_payload(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    try:
        slug = slugify(cfg.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    path = _bot_yaml_path(slug)
    if path.exists():
        raise HTTPException(status_code=409, detail=f"Bot {slug} already exists")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    inner = body.get("bot", body)
    path.write_text(
        yaml.safe_dump({"bot": inner}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    await registry.invalidate()
    _audit("bot_create", slug, key_hint)
    return {"ok": True, "slug": slug}


@app.get("/api/bots/{slug}/config")
async def get_bot_config(slug: str):
    path = _bot_yaml_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bot not found")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise HTTPException(status_code=500, detail=f"YAML parse error: {e}")
    return raw


@app.put("/api/bots/{slug}/config")
@limiter.limit("10/minute")
async def update_bot_config(
    slug: str,
    body: dict,
    request: Request,
    key_hint: str = Depends(verify_api_key),
):
    path = _bot_yaml_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bot not found")
    try:
        _validate_bot_payload(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    inner = body.get("bot", body)
    path.write_text(
        yaml.safe_dump({"bot": inner}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _audit("bot_update", slug, key_hint)
    return {"ok": True, "slug": slug}


@app.delete("/api/bots/{slug}")
@limiter.limit("10/minute")
async def delete_bot(
    slug: str,
    request: Request,
    key_hint: str = Depends(verify_api_key),
):
    bot = await registry.get(slug)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.running:
        raise HTTPException(
            status_code=409, detail="Bot is running — stop it before deleting"
        )
    path = _bot_yaml_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="YAML not found")
    path.unlink()
    await registry.invalidate()
    _audit("bot_delete", slug, key_hint)
    return {"ok": True, "slug": slug}


# ── WebSocket log streaming ───────────────────────────────────────────────────

class LogBroadcaster:
    def __init__(self):
        self._clients: dict[str, set[WebSocket]] = {}
        # asyncio.Lock — essentieel zodra uvicorn meerdere workers krijgt
        # of meer dan één coroutine concurrent connect/disconnect/broadcast
        # uitvoert. Onder de huidige single-worker setup is het pad veilig
        # door de event loop, maar de lock maakt het future-proof.
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, slug: str):
        await ws.accept()
        async with self._lock:
            self._clients.setdefault(slug, set()).add(ws)

    async def disconnect(self, ws: WebSocket, slug: str):
        async with self._lock:
            if slug in self._clients:
                self._clients[slug].discard(ws)

    async def broadcast(self, slug: str, line: str):
        async with self._lock:
            targets = list(self._clients.get(slug, set()))
        dead = set()
        for ws in targets:
            try:
                await ws.send_text(line)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                if slug in self._clients:
                    self._clients[slug] -= dead


broadcaster = LogBroadcaster()


@app.websocket("/ws/logs/{slug}")
async def ws_logs(websocket: WebSocket, slug: str):
    # WebSocket auth — browsers can't set custom headers on ws, so the key
    # must arrive via query param. Reject before accept() so we never leak
    # logs to unauthenticated clients.
    provided = websocket.query_params.get("api_key")
    if not provided or not secrets.compare_digest(provided, _API_KEY):
        await websocket.close(code=4401)
        return

    # Special slug "portal" streams the portal's own log
    if slug == "portal":
        portal_log = LOG_DIR / "portal.log"
        await broadcaster.connect(websocket, "portal")
        try:
            if portal_log.exists():
                lines = portal_log.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines[-100:]:
                    await websocket.send_text(line)
            while True:
                await asyncio.sleep(30)
                await websocket.send_text("__ping__")
        except WebSocketDisconnect:
            await broadcaster.disconnect(websocket, "portal")
        except Exception:
            await broadcaster.disconnect(websocket, "portal")
        return

    # Reject unknown slugs before accepting the socket — prevents tailing
    # arbitrary file paths and keeps broadcaster keys bounded.
    bot = await registry.get(slug)
    if bot is None:
        await websocket.close(code=4004)
        return

    await broadcaster.connect(websocket, slug)
    try:
        if bot and bot.log_file.exists():
            lines = bot.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-150:]:
                await websocket.send_text(line)
        while True:
            await asyncio.sleep(30)
            await websocket.send_text("__ping__")
    except WebSocketDisconnect:
        await broadcaster.disconnect(websocket, slug)
    except Exception:
        await broadcaster.disconnect(websocket, slug)


async def tail_logs():
    last: dict[str, int] = {}
    while True:
        # Tail all bot logs + portal log
        log_files: list[tuple[str, Path]] = [
            (bot.slug, bot.log_file) for bot in await registry.all()
        ]
        log_files.append(("portal", LOG_DIR / "portal.log"))

        for slug, log_file in log_files:
            try:
                if not log_file.exists():
                    continue
                size = log_file.stat().st_size
                prev = last.get(slug, 0)
                if size > prev:
                    with open(log_file, encoding="utf-8", errors="replace") as f:
                        f.seek(prev)
                        new = f.read()
                    last[slug] = size
                    for line in new.splitlines():
                        if line.strip():
                            await broadcaster.broadcast(slug, line)
                elif size < prev:
                    last[slug] = size
            except Exception:
                pass
        await asyncio.sleep(1)


@app.on_event("startup")
async def on_startup():
    logger.info("=== Portal started ===")
    asyncio.create_task(tail_logs())


def run_portal(host="0.0.0.0", port=8080):
    uvicorn.run(app, host=host, port=port, log_level="warning")

if __name__ == "__main__":
    run_portal()
