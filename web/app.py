# web/app.py
# Reverto Web Portal — FastAPI backend
# Multi-bot: reads state from logs/{slug}.state.json per bot.
# Manages bot processes via start/stop API.
# Portal can restart itself via /api/portal/restart.

import asyncio
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import ccxt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

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


def verify_api_key(request: Request) -> None:
    """FastAPI dependency: require X-API-Key header or ?api_key= query param.

    Used on all mutating endpoints (start/stop/restart). GET endpoints stay
    public so dashboards and read-only clients still work without a key.
    """
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if not provided or not secrets.compare_digest(provided, _API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

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
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                data["running"]     = self.running
                data["slug"]        = self.slug
                data["config_file"] = self.config_file
                return data
        except Exception:
            pass

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
    def __init__(self):
        self._bots: dict[str, BotInfo] = {}
        self.refresh()

    def refresh(self):
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
        # Drop bots whose YAML was removed so the registry stays in sync.
        for stale in [s for s in self._bots if s not in current]:
            del self._bots[stale]

    def all(self) -> list[BotInfo]:
        self.refresh()
        return list(self._bots.values())

    def get(self, slug: str) -> Optional[BotInfo]:
        self.refresh()
        return self._bots.get(slug)


registry = BotRegistry()


# ── Process control ───────────────────────────────────────────────────────────

def start_bot(slug: str) -> dict:
    bot = registry.get(slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}
    if bot.running:
        return {"ok": False, "error": f"{slug} already running (PID {bot.pid})"}
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
        return {"ok": True, "message": f"{slug} started (PID {proc.pid})"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def stop_bot(slug: str) -> dict:
    bot = registry.get(slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}
    if not bot.running:
        return {"ok": False, "error": f"{slug} is not running"}
    try:
        pid = bot.pid
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        logger.info(f"Bot {slug} stopped (PID {pid})")
        return {"ok": True, "message": f"{slug} stopped (PID {pid})"}
    except ProcessLookupError:
        return {"ok": False, "error": "Process not found — already stopped?"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def restart_bot(slug: str) -> dict:
    stop_bot(slug)
    time.sleep(1)
    return start_bot(slug)


def _do_portal_restart():
    """Restart the portal process using os.execv — replaces current process."""
    time.sleep(0.8)  # Give the HTTP response time to reach the browser
    logger.info("Portal restarting via os.execv...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ── FastAPI ───────────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach standard hardening headers to every HTTP response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"]        = "no-referrer"
        return response


app = FastAPI(title="Reverto Portal", docs_url=None, redoc_url=None)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    f = STATIC_DIR / "index.html"
    return HTMLResponse(f.read_text(encoding="utf-8") if f.exists() else "<h1>Not found</h1>")


@app.get("/api/bots")
async def get_bots():
    bots      = [b.read_state() for b in registry.all()]
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
    bot = registry.get(slug)
    if not bot:
        return {"error": f"Unknown bot: {slug}"}
    return bot.read_state()


@app.post("/api/bots/{slug}/start", dependencies=[Depends(verify_api_key)])
async def api_start(slug: str):
    return start_bot(slug)

@app.post("/api/bots/{slug}/stop", dependencies=[Depends(verify_api_key)])
async def api_stop(slug: str):
    return stop_bot(slug)

@app.post("/api/bots/{slug}/restart", dependencies=[Depends(verify_api_key)])
async def api_restart(slug: str):
    return restart_bot(slug)


@app.post("/api/portal/restart", dependencies=[Depends(verify_api_key)])
async def api_portal_restart():
    """
    Restart the portal process.
    Uses os.execv in a background thread so the HTTP response
    can reach the browser before the process is replaced.
    """
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
        for bot in registry.all():
            state = bot.read_state()
            if state.get("current_price"):
                return {"price": state["current_price"], "pair": "BTC/USD", "source": "bot"}
        return {"price": 0.0, "pair": "BTC/USD", "source": "unavailable"}


# ── WebSocket log streaming ───────────────────────────────────────────────────

class LogBroadcaster:
    def __init__(self):
        self._clients: dict[str, set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, slug: str):
        await ws.accept()
        self._clients.setdefault(slug, set()).add(ws)

    def disconnect(self, ws: WebSocket, slug: str):
        if slug in self._clients:
            self._clients[slug].discard(ws)

    async def broadcast(self, slug: str, line: str):
        dead = set()
        for ws in self._clients.get(slug, set()):
            try:
                await ws.send_text(line)
            except Exception:
                dead.add(ws)
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
            broadcaster.disconnect(websocket, "portal")
        except Exception:
            broadcaster.disconnect(websocket, "portal")
        return

    # Reject unknown slugs before accepting the socket — prevents tailing
    # arbitrary file paths and keeps broadcaster keys bounded.
    if registry.get(slug) is None:
        await websocket.close(code=4004)
        return

    await broadcaster.connect(websocket, slug)
    bot = registry.get(slug)
    try:
        if bot and bot.log_file.exists():
            lines = bot.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-150:]:
                await websocket.send_text(line)
        while True:
            await asyncio.sleep(30)
            await websocket.send_text("__ping__")
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket, slug)
    except Exception:
        broadcaster.disconnect(websocket, slug)


async def tail_logs():
    last: dict[str, int] = {}
    while True:
        # Tail all bot logs + portal log
        log_files: list[tuple[str, Path]] = [
            (bot.slug, bot.log_file) for bot in registry.all()
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
    asyncio.create_task(tail_logs())


def run_portal(host="0.0.0.0", port=8080):
    uvicorn.run(app, host=host, port=port, log_level="warning")

if __name__ == "__main__":
    run_portal()
