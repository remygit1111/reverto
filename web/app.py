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
from collections import OrderedDict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import yaml

from config.config_loader import load_bot_config
from config.models import BotConfig
from core import credentials, deal_store
from core.database import init_db as _init_db
from notifications.telegram import TelegramNotifier

import bcrypt
import ccxt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
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
    has_trading_windows: bool  = False
    started_at:          Optional[str] = None
    updated_at:          Optional[str] = None
    fees_paid_btc:       float = 0.0
    indicators:          dict  = Field(default_factory=dict)

# ── API key auth ──────────────────────────────────────────────────────────────
# Read from REVERTO_API_KEY or auto-generate one. Generated keys NEVER
# get logged — that would leak the credential into portal.log and any
# log shipper. Instead they're written to logs/.api_key_ephemeral with
# mode 0600 and removed at clean exit via atexit. The operator can
# `cat logs/.api_key_ephemeral` to retrieve it once.
_EPHEMERAL_API_KEY_FILE = (
    Path(__file__).parent.parent / "logs" / ".api_key_ephemeral"
)
_API_KEY = os.environ.get("REVERTO_API_KEY")
if not _API_KEY:
    _API_KEY = secrets.token_hex(32)
    try:
        _EPHEMERAL_API_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _EPHEMERAL_API_KEY_FILE.write_text(_API_KEY + "\n", encoding="utf-8")
        os.chmod(_EPHEMERAL_API_KEY_FILE, 0o600)
        import atexit
        atexit.register(
            lambda p=_EPHEMERAL_API_KEY_FILE: p.exists() and p.unlink()
        )
        logger.warning(
            "REVERTO_API_KEY not set — generated ephemeral key, written to %s "
            "(mode 0600). For production set REVERTO_API_KEY=... in your "
            "environment so the key survives restarts.",
            _EPHEMERAL_API_KEY_FILE,
        )
    except OSError as e:
        # Last-resort fallback: if we genuinely can't write the file
        # the operator still needs the key, so log it. Should never
        # hit in practice — logs/ is writable on every supported host.
        logger.error(
            "REVERTO_API_KEY not set and could not write %s (%s). "
            "Ephemeral key (will be lost on restart): %s",
            _EPHEMERAL_API_KEY_FILE, e, _API_KEY,
        )


# ── Session auth ──────────────────────────────────────────────────────────────
# Username/password authentication for the portal UI. The session cookie is
# signed (not encrypted) with REVERTO_SECRET_KEY — falls back to an ephemeral
# key with a WARNING so casual local usage still works. Credentials themselves
# live in logs/.auth.json as an encrypted blob, reusing the Fernet master key
# from core.credentials.

_SECRET_KEY = os.environ.get("REVERTO_SECRET_KEY")
if not _SECRET_KEY:
    _SECRET_KEY = secrets.token_hex(32)
    logger.warning(
        "REVERTO_SECRET_KEY not set — generated ephemeral signing key. "
        "EVERY EXISTING SESSION COOKIE WILL BE INVALIDATED ON THE NEXT "
        "PORTAL RESTART. For production add to ~/.bashrc: "
        "export REVERTO_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    )

_SESSION_COOKIE = "reverto_session"
_SESSION_TTL = 86400  # 24h
_SESSION_SALT = "reverto.session.v1"
_session_serializer = URLSafeTimedSerializer(_SECRET_KEY, salt=_SESSION_SALT)

# Secure-cookie flag default. Cookies are marked secure (HTTPS-only) by
# default; this protects production deployments behind a TLS reverse
# proxy. Localhost / plain-http development opts out by exporting
# REVERTO_INSECURE_COOKIES=1 — without that flag set, a browser on
# http:// will silently drop the cookie and the SPA will look broken.
_INSECURE_COOKIES = os.environ.get("REVERTO_INSECURE_COOKIES") == "1"
_COOKIE_SECURE = not _INSECURE_COOKIES
if _COOKIE_SECURE:
    logger.info(
        "Session cookies: secure=True (TLS required). "
        "For local development add `export REVERTO_INSECURE_COOKIES=1` to ~/.bashrc."
    )
else:
    logger.warning(
        "Session cookies: secure=False (REVERTO_INSECURE_COOKIES=1 set "
        "— only safe on localhost / private LAN)."
    )

_AUTH_FILE = Path(__file__).parent.parent / "logs" / ".auth.json"
_INITIAL_PW_FILE = Path(__file__).parent.parent / "logs" / ".initial_password"


def _bootstrap_auth_if_missing() -> None:
    """Create logs/.auth.json on first run with a random admin password.

    The plaintext password is written ONCE to logs/.initial_password with
    mode 0600 so the operator can retrieve it from disk. It is never
    logged — logging it would leak it into portal.log and any log tail
    that ships to stdout / remote collectors. The file is deleted
    automatically on the first successful password change.

    `session_epoch` starts at 0 and is bumped on every logout and
    password change so old session cookies are immediately invalid.
    """
    if _AUTH_FILE.exists():
        return
    pw = secrets.token_urlsafe(12)
    pw_hash = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    credentials.save_encrypted(_AUTH_FILE, {
        "username": "admin",
        "password_hash": pw_hash,
        "session_epoch": 0,
    })
    try:
        _INITIAL_PW_FILE.parent.mkdir(parents=True, exist_ok=True)
        _INITIAL_PW_FILE.write_text(pw + "\n", encoding="utf-8")
        os.chmod(_INITIAL_PW_FILE, 0o600)
    except OSError as e:
        # Fall back to logging the password only if we can't write the
        # file at all — losing the credential would be worse than the
        # log leak, and this branch should never hit in practice.
        logger.error(
            "First run — could not write %s (%s). Password: %s",
            _INITIAL_PW_FILE, e, pw,
        )
        return
    logger.warning(
        "First run — default credentials created for user 'admin'. "
        "Initial password written to %s (mode 0600). "
        "Log in, change the password, and delete that file.",
        _INITIAL_PW_FILE,
    )


def _load_auth() -> Optional[dict]:
    return credentials.load_encrypted(_AUTH_FILE)


def _save_auth(data: dict) -> None:
    credentials.save_encrypted(_AUTH_FILE, data)


def _current_session_epoch() -> int:
    """Read the current session epoch from .auth.json. Defaults to 0.

    Bumping this integer instantly invalidates every previously-issued
    session cookie because each cookie embeds the epoch it was minted
    under and _verify_session_cookie compares the two.
    """
    auth = _load_auth() or {}
    try:
        return int(auth.get("session_epoch", 0))
    except (TypeError, ValueError):
        return 0


def _bump_session_epoch() -> int:
    """Increment the session epoch and persist. Used on logout and on
    password change to nuke every outstanding cookie at once."""
    auth = _load_auth() or {}
    current = 0
    try:
        current = int(auth.get("session_epoch", 0))
    except (TypeError, ValueError):
        current = 0
    auth["session_epoch"] = current + 1
    _save_auth(auth)
    return current + 1


def _create_session_cookie(username: str) -> str:
    return _session_serializer.dumps({
        "u": username,
        "iat": int(time.time()),
        "ep": _current_session_epoch(),
    })


def _verify_session_cookie(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        data = _session_serializer.loads(token, max_age=_SESSION_TTL)
    except (BadSignature, SignatureExpired):
        return None
    except Exception as e:  # noqa: BLE001 — defensive catch-all
        # itsdangerous can raise on malformed base64 / non-JSON payloads
        # too. Treat anything weird as an invalid session — never let a
        # broken cookie escape as a 500 from the auth gate.
        logger.debug("session cookie parse failed: %s", e)
        return None
    if not isinstance(data, dict) or not data.get("u"):
        return None
    # Epoch check — any cookie minted under an older epoch is rejected
    # immediately, so logout / password-change invalidate every browser
    # that's holding a copy. Cookies minted before the epoch field
    # existed (legacy) get treated as epoch 0.
    try:
        cookie_epoch = int(data.get("ep", 0))
    except (TypeError, ValueError):
        return None
    if cookie_epoch != _current_session_epoch():
        return None
    return data


def _require_session(request: Request) -> dict:
    """FastAPI dependency — reject if the caller has no valid session cookie."""
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    return payload


def _request_actor(request: Request) -> str:
    """Return a short audit-log identifier for the caller.

    Resolution order:
      * session cookie  → `session:<username>`
      * X-API-Key header → `apikey:<8-char sha256 hint>`
      * neither         → `-` (the AuthMiddleware would normally have
                          rejected the request before we get here, so
                          this branch only fires for the public /auth
                          paths that opt out of the gate).
    Used as a `Depends(_request_actor)` on mutating endpoints in place
    of the old `Depends(_request_actor)` so the audit trail still
    captures who took the action even though the API-key dependency
    itself is gone.
    """
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if payload and payload.get("u"):
        return f"session:{payload['u']}"
    # Header-only — query-string fallback removed, see AuthMiddleware.
    provided = request.headers.get("X-API-Key")
    if provided and secrets.compare_digest(provided, _API_KEY):
        hint = hashlib.sha256(provided.encode("utf-8")).hexdigest()[:8]
        return f"apikey:{hint}"
    return "-"

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
            "has_trading_windows": False,
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
            "script-src 'self' https://unpkg.com; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"]        = "no-referrer"
        return response


_PUBLIC_PATHS = {
    "/",
    "/favicon.ico",
    "/health",
    "/auth/status",
    "/auth/login",
    "/auth/logout",
}


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate every HTTP request on a valid session cookie, except for the
    small set of public paths (landing page, static assets, auth endpoints).

    API requests without a cookie get a JSON 401 so the SPA fetch helpers
    can handle them. Non-API requests get a redirect to /, where the SPA
    will render the login view.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        if _verify_session_cookie(request.cookies.get(_SESSION_COOKIE)):
            return await call_next(request)

        # API key callers still pass through, but ONLY via the X-API-Key
        # header. The legacy `?api_key=` query-string fallback was
        # dropped — query strings end up in proxy / nginx / cloud
        # access logs and the browser's history, which leaked the
        # long-lived API key to every collector that tailed those
        # files. Scripts and CI tools must send the header.
        provided = request.headers.get("X-API-Key")
        if provided and secrets.compare_digest(provided, _API_KEY):
            return await call_next(request)

        accept = request.headers.get("accept", "")
        if (
            path.startswith("/api/")
            or path.startswith("/ws")
            or "application/json" in accept
        ):
            return JSONResponse(
                {"detail": "Authentication required"}, status_code=401
            )
        return RedirectResponse(url="/", status_code=303)


# Rate limiter — beperkt brute force en DoS op control endpoints. Sleutel
# per remote IP; in een setup achter een reverse proxy moet je X-Forwarded-For
# parsing toevoegen via een eigen key_func.
limiter = Limiter(key_func=get_remote_address)


async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Too many requests"})


_bootstrap_auth_if_missing()

# Initialise the SQLite ledger on portal boot. Idempotent — safe to call
# on every restart; creates logs/reverto.db + schema on first run.
try:
    _init_db()
except Exception as _e:  # pragma: no cover - defensive
    logger.warning("init_db failed on portal startup: %s", _e)

app = FastAPI(title="Reverto Portal", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
# Order: SecurityHeaders added first (runs last on the response), Auth added
# second (runs first on the request so 401s never leak past the gate).
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Auth endpoints ────────────────────────────────────────────────────────────


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


@app.post("/auth/login")
@limiter.limit("5/minute")
async def auth_login(body: LoginBody, request: Request):
    auth = _load_auth() or {}
    stored_hash = auth.get("password_hash", "")
    stored_user = auth.get("username", "")
    ok = False
    if stored_user and stored_hash and body.username == stored_user:
        try:
            ok = bcrypt.checkpw(body.password.encode("utf-8"), stored_hash.encode("utf-8"))
        except ValueError:
            ok = False
    if not ok:
        # Damp brute force without blocking the event loop.
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _create_session_cookie(stored_user)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=_COOKIE_SECURE,
        path="/",
    )
    _audit("auth_login", stored_user, "-")
    return resp


@app.post("/auth/logout")
async def auth_logout():
    # Server-side invalidation: bump the session epoch so every other
    # browser holding a copy of the cookie is rejected on the next
    # request, not just the one calling logout. itsdangerous tokens
    # are stateless, so without this the cookie would stay valid until
    # its TTL expired even though the user clicked "log out".
    try:
        _bump_session_epoch()
    except Exception as e:
        logger.warning("logout: failed to bump session epoch (%s)", e)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


@app.get("/auth/status")
async def auth_status(request: Request):
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if payload:
        return {"authenticated": True, "username": payload.get("u")}
    return {"authenticated": False, "username": None}


@app.post("/api/auth/change-password")
@limiter.limit("10/minute")
async def auth_change_password(
    body: ChangePasswordBody,
    request: Request,
    session: dict = Depends(_require_session),
):
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    auth = _load_auth() or {}
    stored_hash = auth.get("password_hash", "")
    try:
        ok = bool(stored_hash) and bcrypt.checkpw(
            body.current_password.encode("utf-8"), stored_hash.encode("utf-8")
        )
    except ValueError:
        ok = False
    if not ok:
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    new_hash = bcrypt.hashpw(
        body.new_password.encode("utf-8"), bcrypt.gensalt(rounds=12)
    ).decode("utf-8")
    auth["password_hash"] = new_hash
    # Bump the session epoch so any other browser still authenticated
    # under the old password is signed out on its next request. The
    # current request's caller will need to log in again too — that's
    # by design and matches what every other dashboard does on a
    # password change.
    try:
        current_epoch = int(auth.get("session_epoch", 0))
    except (TypeError, ValueError):
        current_epoch = 0
    auth["session_epoch"] = current_epoch + 1
    _save_auth(auth)
    # First change after bootstrap: drop the plaintext crib file so the
    # initial password no longer sits on disk. Best-effort: a missing
    # file is fine (user may have deleted it manually).
    if _INITIAL_PW_FILE.exists():
        try:
            _INITIAL_PW_FILE.unlink()
        except OSError:
            pass
    _audit("auth_change_password", session.get("u", "-"), "-")
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    f = STATIC_DIR / "index.html"
    return HTMLResponse(f.read_text(encoding="utf-8") if f.exists() else "<h1>Not found</h1>")


def _compute_summary(bots: list[dict]) -> dict:
    """Single source of truth voor de dashboard summary-blok.

    Wordt gebruikt door zowel GET /api/bots als de /ws/state watcher,
    zodat het HTTP fallback-pad en de WS push exact dezelfde vorm
    hebben. closed_deals is geaggregeerd over alle bots zodat toekomstige
    WS-consumers het kunnen tonen zonder extra round-trip.
    """
    total_pnl  = sum(b.get("total_pnl_btc", 0) for b in bots)
    active     = sum(1 for b in bots if b.get("running"))
    open_cnt   = sum(b.get("open_deals_count", 0) for b in bots)
    closed_cnt = sum(b.get("closed_deals_count", 0) for b in bots)
    return {
        "total_pnl_btc": round(total_pnl, 8),
        "active_bots":   active,
        "total_bots":    len(bots),
        "open_deals":    open_cnt,
        "closed_deals":  closed_cnt,
    }


@app.get("/api/bots")
async def get_bots():
    bots = [b.read_state() for b in await registry.all()]

    all_open = []
    for b in bots:
        for d in b.get("open_deals", []):
            d["bot_name"] = b.get("bot_name", b.get("slug"))
            d["bot_slug"] = b.get("slug")
            d["exchange"]  = b.get("exchange")
            all_open.append(d)

    summary = _compute_summary(bots)
    # Backwards-compat: existing /api/bots callers expected exactly
    # the 4 keys below. closed_deals is extra in the new helper but
    # additive keys are safe (SPA reads by name).
    return {
        "bots": bots,
        "summary": {
            "total_pnl_btc": summary["total_pnl_btc"],
            "active_bots":   summary["active_bots"],
            "total_bots":    summary["total_bots"],
            "open_deals":    summary["open_deals"],
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
async def api_start(slug: str, request: Request, actor: str = Depends(_request_actor)):
    _audit("bot_start", slug, actor)
    return await start_bot(slug)

@app.post("/api/bots/{slug}/stop")
@limiter.limit("20/minute")
async def api_stop(slug: str, request: Request, actor: str = Depends(_request_actor)):
    _audit("bot_stop", slug, actor)
    return await stop_bot(slug)

@app.post("/api/bots/{slug}/restart")
@limiter.limit("20/minute")
async def api_restart(slug: str, request: Request, actor: str = Depends(_request_actor)):
    _audit("bot_restart", slug, actor)
    return await restart_bot(slug)

@app.post("/api/bots/{slug}/deal/start")
@limiter.limit("5/minute")
async def api_deal_start(slug: str, request: Request, actor: str = Depends(_request_actor)):
    """Manual deal trigger — writes a sentinel file that the running
    paper engine consumes on its next tick to force-open a deal."""
    bot = await registry.get(slug)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Unknown bot: {slug}")
    trigger = LOG_DIR / f"{slug}.manual_trigger"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        trigger.write_text("", encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write trigger: {e}")
    _audit("bot_manual_deal", slug, actor)
    return {"ok": True}


@app.post("/api/portal/restart")
@limiter.limit("5/minute")
async def api_portal_restart(request: Request, actor: str = Depends(_request_actor)):
    """Restart the portal process.

    Uses os.execv in a background thread so the HTTP response can reach
    the browser before the process is replaced.
    """
    _audit("portal_restart", "-", actor)
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


# ── Chart OHLCV endpoint ──────────────────────────────────────────────────────
# In-memory cache for chart fetches. Keyed on (pair_normalized, timeframe,
# limit) → (expires_at, payload). 60s TTL keeps Bitget's REST endpoint
# reasonably idle even when several tabs poll the same chart simultaneously.
# Bounded LRU: 60s TTL plus a 256-entry hard cap. Without the cap the
# key space (pair × timeframe × limit) was effectively unbounded, so a
# misbehaving or hostile client could grow the cache indefinitely just
# by walking the limit parameter. OrderedDict.move_to_end gives us
# O(1) recency tracking; the eldest entry is evicted on miss when the
# cap is reached. Expired entries are dropped on access.
_chart_cache: "OrderedDict[tuple, tuple[float, list]]" = OrderedDict()
# TTL per timeframe — the longer the bar, the slower the data moves,
# so we can hold each cached payload longer without hiding a real
# market move from the user. 15m bars go stale fast (operators want
# to see the latest wick); 1d bars are essentially static.
_CHART_CACHE_TTL = {
    "15m": 30.0,
    "30m": 60.0,
    "1h":  120.0,
    "2h":  180.0,
    "4h":  300.0,
    "12h": 450.0,
    "1d":  600.0,
    "3d":  900.0,
    "1w":  1800.0,
}
_CHART_CACHE_TTL_DEFAULT = 60.0
_CHART_CACHE_MAX = 256
_CHART_TIMEFRAMES = (
    "15m", "30m", "1h", "2h", "4h", "12h", "1d", "3d", "1w",
)
_chart_lock = asyncio.Lock()


def _normalize_chart_pair(raw: str) -> str:
    """Accept BTCUSD or BTC/USD and return BTC/USD form expected by
    PublicExchange.SYMBOL_MAPS."""
    s = raw.strip().upper()
    if "/" in s:
        return s
    if s.endswith("USDT"):
        return f"{s[:-4]}/USDT"
    if s.endswith("USD"):
        return f"{s[:-3]}/USD"
    return s


@app.get("/api/chart/{pair}/{timeframe}")
async def api_chart(pair: str, timeframe: str, limit: int = 200):
    """Public OHLCV endpoint backing the dashboard's live candlestick chart.

    Wraps PublicExchange("bitget").get_ohlcv() with input validation, a
    60-second per-key in-memory cache, and a stable JSON shape that
    Lightweight Charts can consume directly (UTCTimestamp seconds).
    """
    if timeframe not in _CHART_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"timeframe must be one of {', '.join(_CHART_TIMEFRAMES)}",
        )
    if limit < 10 or limit > 500:
        raise HTTPException(
            status_code=400, detail="limit must be between 10 and 500"
        )

    normalized = _normalize_chart_pair(pair)
    key = (normalized, timeframe, limit)
    now = time.time()

    cached = _chart_cache.get(key)
    if cached:
        if cached[0] > now:
            # Fresh hit — bump recency and return.
            _chart_cache.move_to_end(key)
            return cached[1]
        # Expired — drop and fall through to refetch.
        _chart_cache.pop(key, None)

    try:
        from exchanges.public_exchange import PublicExchange
        async with _chart_lock:
            client = PublicExchange("bitget")
            raw = await asyncio.to_thread(
                client.get_ohlcv, normalized, timeframe, limit
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Exchange error: {e}")

    payload = [
        {
            "time":   int(c[0] // 1000),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]) if len(c) > 5 and c[5] is not None else 0.0,
        }
        for c in raw
    ]
    ttl = _CHART_CACHE_TTL.get(timeframe, _CHART_CACHE_TTL_DEFAULT)
    _chart_cache[key] = (now + ttl, payload)
    _chart_cache.move_to_end(key)
    # Bound the cache: evict the eldest entry once we cross the cap so
    # the dict can never grow unbounded under a hostile or misbehaving
    # client walking the limit parameter.
    while len(_chart_cache) > _CHART_CACHE_MAX:
        _chart_cache.popitem(last=False)
    return payload


# ── Candle range endpoint (for client-side backtester) ───────────────────────
#
# Separate cache from /api/chart: different TTL needs and different key shape
# (start/end ISO timestamps). 5-minute TTL, 64-entry cap. ccxt's fetch_ohlcv
# caps at 1000 bars per call, so we paginate transparently here, dedupe on
# ms-timestamp and return the same JSON shape as /api/chart.
_candles_cache: "OrderedDict[tuple, tuple[float, list]]" = OrderedDict()
_CANDLES_CACHE_TTL = 300.0
_CANDLES_CACHE_TTL_LARGE = 1800.0  # 30 min for limit > 5000
_CANDLES_CACHE_LARGE_THRESHOLD = 5000
_CANDLES_CACHE_MAX = 64
_CANDLES_MAX_BARS  = 300000
_CANDLES_PAGE_SLEEP_S = 0.15  # pause between paginated ccxt calls

_TF_SECONDS = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h":  60 * 60,
    "2h":  2 * 60 * 60,
    "4h":  4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d":  24 * 60 * 60,
    "3d":  3 * 24 * 60 * 60,
    "1w":  7 * 24 * 60 * 60,
}


def _parse_iso_utc(s: str) -> datetime:
    """Parse an ISO 8601 date string into an aware UTC datetime.
    Accepts trailing 'Z' (Python < 3.11 doesn't parse it natively)."""
    if not isinstance(s, str) or not s:
        raise ValueError("empty timestamp")
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _fetch_ohlcv_page_with_retry(
    client, symbol: str, timeframe: str, since_ms: int, limit: int
) -> list:
    """Single ccxt fetch_ohlcv call with exponential-backoff retry.

    Bitget occasionally returns error code 40017 ("Parameter verification
    failed startTime || endTime") on large ranges or transient hiccups —
    retry up to 3 times with 0.5s / 1.0s / 2.0s backoff before giving
    up. The caller treats a final failure as "no more data from this
    cursor" so one bad page doesn't throw the whole fetch away.
    """
    last_err = None
    for attempt in range(3):
        try:
            return await asyncio.to_thread(
                client.client.fetch_ohlcv,
                client._symbol(symbol),
                timeframe,
                since_ms,
                limit,
            )
        except Exception as e:  # noqa: BLE001 — surface every ccxt error
            last_err = e
            wait = 0.5 * (2 ** attempt)
            logger.warning(
                "fetch_ohlcv attempt %d/3 failed for %s %s since=%d: %s "
                "(retrying in %.1fs)",
                attempt + 1, symbol, timeframe, since_ms, e, wait,
            )
            await asyncio.sleep(wait)
    logger.error(
        "fetch_ohlcv exhausted retries for %s %s since=%d: %s",
        symbol, timeframe, since_ms, last_err,
    )
    return []


async def _fetch_ohlcv_range(
    client, symbol: str, timeframe: str, start_ms: int, end_ms: int
) -> list:
    """Paginated fetch_ohlcv driven purely by `since`.

    ccxt / Bitget's inverse-swap endpoint is unreliable when both
    startTime and endTime are passed on large windows (error 40017
    "Parameter verification failed"). Walking forward with `since`
    only — letting the exchange pick its own page size, then bumping
    `since` past the newest returned bar — is the most portable
    pattern and what every other ccxt-based backtester does. Stops
    as soon as the newest returned bar reaches end_ms or two
    consecutive empty pages come back.
    """
    tf_ms = _TF_SECONDS[timeframe] * 1000
    bars: dict[int, list] = {}
    since = start_ms
    empty_pages = 0
    pages_fetched = 0
    max_pages = (_CANDLES_MAX_BARS // 200) + 16
    expected_pages = max(1, ((end_ms - start_ms) // tf_ms + 199) // 200)
    logger.info(
        "Fetching ~%d pages for %s %s (max %d, range %d→%d)",
        expected_pages, symbol, timeframe, max_pages, start_ms, end_ms,
    )
    for _ in range(max_pages):
        if since >= end_ms:
            break
        if pages_fetched > 0:
            await asyncio.sleep(_CANDLES_PAGE_SLEEP_S)
        page = await _fetch_ohlcv_page_with_retry(
            client, symbol, timeframe, since, 1000,
        )
        pages_fetched += 1
        if pages_fetched % 10 == 0:
            logger.info(
                "Fetching page %d/~%d for %s %s (bars=%d)",
                pages_fetched, expected_pages, symbol, timeframe, len(bars),
            )
        if not page:
            empty_pages += 1
            if empty_pages >= 2:
                logger.info(
                    "Two empty pages in a row for %s %s — stopping with %d bars",
                    symbol, timeframe, len(bars),
                )
                break
            # Jump ahead by ~200 bars worth of ms so a persistent
            # no-data hole doesn't force us to crawl bar-by-bar.
            since += tf_ms * 200
            continue
        empty_pages = 0
        page_max_ts = since
        for row in page:
            ts = int(row[0])
            if ts > page_max_ts:
                page_max_ts = ts
            if ts < start_ms or ts > end_ms:
                continue
            bars[ts] = row
        # Advance strictly past the newest bar the exchange actually
        # returned — tracked on every row, not just the in-range ones,
        # so an all-out-of-range page still moves the cursor forward.
        if page_max_ts > since:
            since = page_max_ts + tf_ms
        else:
            since += tf_ms * 200
    logger.info(
        "Fetch complete for %s %s: %d bars over %d pages",
        symbol, timeframe, len(bars), pages_fetched,
    )
    return [bars[k] for k in sorted(bars.keys())]


@app.get("/api/candles/{pair}/{timeframe}")
@limiter.limit("20/minute")
async def api_candles(
    request: Request,
    pair: str,
    timeframe: str,
    start: str,
    end: str,
    limit: int = 5000,
):
    """Public OHLCV range endpoint backing the client-side backtester.

    Paginates ccxt under the hood (1000-bar max per request) until the
    full [start, end] range is covered, dedupes on timestamp, returns
    the same {time, open, high, low, close, volume} shape as
    /api/chart. Cached for 5 minutes per (pair, tf, start, end, limit).
    """
    if timeframe not in _CHART_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"timeframe must be one of {', '.join(_CHART_TIMEFRAMES)}",
        )
    if limit < 100:
        limit = 100
    if limit > _CANDLES_MAX_BARS:
        limit = _CANDLES_MAX_BARS

    try:
        start_dt = _parse_iso_utc(start)
        end_dt = _parse_iso_utc(end)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid timestamp: {e}")
    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="start must be before end")

    tf_s = _TF_SECONDS[timeframe]
    span_s = (end_dt - start_dt).total_seconds()
    bar_count = int(span_s / tf_s)
    # Clamp the range to at most _CANDLES_MAX_BARS candles by trimming
    # `start` forward — the backtester only needs the tail of the
    # requested window if the operator asked for too much history.
    if bar_count > limit:
        trim_bars = bar_count - limit
        start_dt = start_dt.fromtimestamp(
            start_dt.timestamp() + trim_bars * tf_s, tz=timezone.utc
        )

    normalized = _normalize_chart_pair(pair)
    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()
    key = (normalized, timeframe, start_iso, end_iso, limit)
    now = time.time()

    cached = _candles_cache.get(key)
    if cached:
        if cached[0] > now:
            _candles_cache.move_to_end(key)
            return cached[1]
        _candles_cache.pop(key, None)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    try:
        from exchanges.public_exchange import PublicExchange
        async with _chart_lock:
            client = PublicExchange("bitget")
            raw = await _fetch_ohlcv_range(
                client, normalized, timeframe, start_ms, end_ms
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Exchange error: {e}")

    payload = [
        {
            "time":   int(c[0] // 1000),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]) if len(c) > 5 and c[5] is not None else 0.0,
        }
        for c in raw
    ]
    ttl = (
        _CANDLES_CACHE_TTL_LARGE
        if limit > _CANDLES_CACHE_LARGE_THRESHOLD
        else _CANDLES_CACHE_TTL
    )
    _candles_cache[key] = (now + ttl, payload)
    _candles_cache.move_to_end(key)
    while len(_candles_cache) > _CANDLES_CACHE_MAX:
        _candles_cache.popitem(last=False)
    return payload


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
    actor: str = Depends(_request_actor),
):
    if name not in _KNOWN_EXCHANGES:
        raise HTTPException(status_code=404, detail="Unknown exchange")
    credentials.save_keys(name, body.api_key, body.api_secret)
    _audit("exchange_keys_set", name, actor)
    return {"ok": True, "exchange": name}


@app.delete("/api/exchanges/{name}/keys")
@limiter.limit("10/minute")
async def delete_exchange_keys(
    name: str,
    request: Request,
    actor: str = Depends(_request_actor),
):
    if name not in _KNOWN_EXCHANGES:
        raise HTTPException(status_code=404, detail="Unknown exchange")
    removed = credentials.delete_keys(name)
    if not removed:
        raise HTTPException(status_code=404, detail="No keys stored for exchange")
    _audit("exchange_keys_delete", name, actor)
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
    actor: str = Depends(_request_actor),
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
    _audit("bot_create", slug, actor)
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
    actor: str = Depends(_request_actor),
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
    _audit("bot_update", slug, actor)
    return {"ok": True, "slug": slug}


@app.delete("/api/bots/{slug}")
@limiter.limit("10/minute")
async def delete_bot(
    slug: str,
    request: Request,
    actor: str = Depends(_request_actor),
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
    _audit("bot_delete", slug, actor)
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
    # WebSocket auth — BaseHTTPMiddleware doesn't run on WS, so we check
    # the session cookie here. The legacy `?api_key=` query param fallback
    # was removed: query strings end up in proxy / access logs and browser
    # history, which leaked the API key. Browsers always send the session
    # cookie on a same-origin WS upgrade, so this is no regression for the
    # SPA. Reject before accept() so unauthenticated clients never see logs.
    session_ok = _verify_session_cookie(
        websocket.cookies.get(_SESSION_COOKIE)
    ) is not None
    if not session_ok:
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


class StateBroadcaster:
    """Push bot-state updates to every connected /ws/state client.

    Mirrors LogBroadcaster, but shares one flat client set (state
    updates are bot-agnostic — every client wants every update).
    """

    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: str) -> None:
        async with self._lock:
            targets = list(self._clients)
        stale: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        if stale:
            async with self._lock:
                for ws in stale:
                    self._clients.discard(ws)


state_broadcaster = StateBroadcaster()

# Module-level cache van laatst-geziene mtimes, per slug. Wordt door
# watch_state_files() gelezen/geüpdatet. Gereset op portal restart,
# wat prima is: de watcher stuurt in dat geval gewoon één extra
# broadcast bij de eerste iteratie.
_state_mtimes: dict[str, float] = {}


async def watch_state_files():
    """Poll logs/*.state.json mtimes every 2s and push changes to
    /ws/state clients. Also broadcasts the computed summary every cycle
    so the SPA's overview cards stay in sync with bot life-cycle events
    (a bot starting/stopping doesn't touch state.json, but the PID
    liveness flag flips).
    """
    while True:
        try:
            bots = await registry.all()
            for bot in bots:
                try:
                    sf = bot.state_file
                    if not sf.exists():
                        continue
                    mtime = sf.stat().st_mtime
                    if _state_mtimes.get(bot.slug) == mtime:
                        continue
                    _state_mtimes[bot.slug] = mtime
                    state = bot.read_state()
                    payload = json.dumps({
                        "type": "bot_state",
                        "slug": bot.slug,
                        "data": state,
                    })
                    await state_broadcaster.broadcast(payload)
                except Exception as e:  # noqa: BLE001
                    logger.debug("watch_state_files: bot %s failed: %s", bot.slug, e)
                    continue

            # Always broadcast summary — cheap and keeps the overview
            # cards honest even when no state.json file changed (PID
            # liveness flips as bots start/stop without touching JSON).
            try:
                snapshot = [b.read_state() for b in bots]
                summary_payload = json.dumps({
                    "type": "summary",
                    "data": _compute_summary(snapshot),
                })
                await state_broadcaster.broadcast(summary_payload)
            except Exception as e:  # noqa: BLE001
                logger.debug("watch_state_files: summary failed: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("watch_state_files iteration error: %s", e)

        await asyncio.sleep(2.0)


@app.websocket("/ws/state")
async def ws_state(websocket: WebSocket):
    # Session cookie gate — mirrors ws_logs. The legacy ?api_key=
    # query-string fallback was intentionally dropped portal-wide.
    if not _verify_session_cookie(websocket.cookies.get(_SESSION_COOKIE)):
        await websocket.close(code=4401)
        return
    await state_broadcaster.connect(websocket)
    try:
        # Initial snapshot so the SPA can render without waiting for a
        # file-change event.
        bots = await registry.all()
        snapshot: list[dict] = []
        for bot in bots:
            try:
                state = bot.read_state()
            except Exception:
                continue
            snapshot.append(state)
            try:
                await websocket.send_text(json.dumps({
                    "type": "bot_state",
                    "slug": bot.slug,
                    "data": state,
                }))
            except Exception:
                pass
        try:
            await websocket.send_text(json.dumps({
                "type": "summary",
                "data": _compute_summary(snapshot),
            }))
        except Exception:
            pass

        # Keep the socket alive — broadcasts come from watch_state_files.
        while True:
            await asyncio.sleep(30)
            await websocket.send_text("__ping__")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await state_broadcaster.disconnect(websocket)


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


# ── SQLite ledger endpoints ───────────────────────────────────────────────────
# Reads are public (historical data the operator already owns). Writes use the
# existing API key dependency + rate limiter.


@app.get("/api/db/deals")
@limiter.limit("60/minute")
async def api_db_deals(
    request: Request,
    bot_slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
):
    # SQLite calls are sync — push them onto a worker thread so a slow
    # disk or a contended write lock doesn't block the asyncio event
    # loop and starve every other request the portal is serving.
    limit = max(1, min(1000, int(limit)))

    def _query():
        deals = deal_store.get_deals(bot_slug=bot_slug, status=status, limit=limit)
        return [
            {"deal": d, "orders": deal_store.get_deal_orders(d["id"])}
            for d in deals
        ]

    return await asyncio.to_thread(_query)


@app.get("/api/db/deals/{deal_id}/orders")
@limiter.limit("60/minute")
async def api_db_deal_orders(deal_id: str, request: Request):
    return await asyncio.to_thread(deal_store.get_deal_orders, deal_id)


@app.get("/api/db/stats")
@limiter.limit("60/minute")
async def api_db_stats(request: Request, bot_slug: Optional[str] = None):
    return await asyncio.to_thread(deal_store.compute_stats, bot_slug)


class AnnotationBody(BaseModel):
    bot_slug: str
    type: str
    timeframe: str
    # Unix-second timestamps, clamped to a sane range (1970-01-01 .. ~2033).
    # Without bounds a hostile or buggy client could store a million-year
    # timestamp that would later overflow Lightweight Charts' time scale.
    x1: int = Field(ge=0, le=2_000_000_000)
    y1: Optional[float] = None
    x2: Optional[int] = Field(default=None, ge=0, le=2_000_000_000)
    y2: Optional[float] = None
    label: Optional[str] = None
    color: str = "#00d4aa"


@app.post("/api/db/annotations")
@limiter.limit("30/minute")
async def api_db_annotations_create(
    body: AnnotationBody,
    request: Request,
    actor: str = Depends(_request_actor),
):
    new_id = await asyncio.to_thread(
        deal_store.save_annotation,
        body.bot_slug,
        body.type,
        body.timeframe,
        body.x1,
        body.y1,
        body.x2,
        body.y2,
        body.label,
        body.color,
    )
    return {"id": new_id}


@app.get("/api/db/annotations")
@limiter.limit("60/minute")
async def api_db_annotations_list(
    request: Request,
    bot_slug: str,
    timeframe: Optional[str] = None,
):
    return await asyncio.to_thread(deal_store.list_annotations, bot_slug, timeframe)


@app.delete("/api/db/annotations/all")
@limiter.limit("10/minute")
async def api_db_annotations_delete_all(
    request: Request,
    bot_slug: str,
    timeframe: Optional[str] = None,
    actor: str = Depends(_request_actor),
):
    """Bulk-delete every annotation for a bot, optionally scoped to one
    timeframe. Registered BEFORE the {ann_id} catch-all so FastAPI
    routes the literal `/all` path here instead of trying to parse
    "all" as an int."""
    removed = await asyncio.to_thread(
        deal_store.delete_annotations_for, bot_slug, timeframe
    )
    _audit("annotations_clear", bot_slug, actor)
    return {"ok": True, "removed": removed}


@app.delete("/api/db/annotations/{ann_id}")
@limiter.limit("30/minute")
async def api_db_annotations_delete(
    ann_id: int,
    request: Request,
    actor: str = Depends(_request_actor),
):
    if not await asyncio.to_thread(deal_store.delete_annotation, ann_id):
        raise HTTPException(status_code=404, detail="Annotation not found")
    return {"ok": True}


@app.on_event("startup")
async def on_startup():
    logger.info("=== Portal started ===")
    asyncio.create_task(tail_logs())
    asyncio.create_task(watch_state_files())


def run_portal(host="0.0.0.0", port=8080):
    uvicorn.run(app, host=host, port=port, log_level="warning")

if __name__ == "__main__":
    run_portal()
