# web/app.py
# Reverto Web Portal — FastAPI backend
# Multi-bot: reads state from logs/{slug}.state.json per bot.
# Manages bot processes via start/stop API.
# Portal can restart itself via /api/portal/restart.

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware import Middleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

BASE_DIR   = Path(__file__).parent.parent
STATIC_DIR = Path(__file__).parent / "static"
CONFIG_DIR = BASE_DIR / "config" / "bots"
LOG_DIR    = BASE_DIR / "logs"
PID_DIR    = LOG_DIR / "pids"
PYTHON_BIN = sys.executable

# Cached ccxt client for /api/price — initialised at module load so there
# is no race condition on first request. Reused for all subsequent calls.
import ccxt as _ccxt
_price_client = _ccxt.bitget({"options": {"defaultType": "swap"}})


def _get_price_client():
    return _price_client


# ── Security middleware ───────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data:;"
        )
        return response


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
        except (json.JSONDecodeError, Exception):
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
        if not CONFIG_DIR.exists():
            return
        current_slugs = {f.stem for f in CONFIG_DIR.glob("*.yaml")}

        # Add new bots
        for f in sorted(CONFIG_DIR.glob("*.yaml")):
            slug = f.stem
            if slug not in self._bots:
                self._bots[slug] = BotInfo(
                    slug=slug,
                    config_file=str(f.relative_to(BASE_DIR))
                )

        # Remove bots whose YAML has been deleted
        stale = [slug for slug in self._bots if slug not in current_slugs]
        for slug in stale:
            del self._bots[slug]

    def all(self) -> list[BotInfo]:
        self.refresh()
        return list(self._bots.values())

    def get(self, slug: str) -> Optional[BotInfo]:
        self.refresh()
        return self._bots.get(slug)

    def valid_slugs(self) -> set[str]:
        """Return set of all known bot slugs plus 'portal'."""
        return {b.slug for b in self.all()} | {"portal"}


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

        env = os.environ.copy()
        env["PYTHONPATH"] = str(BASE_DIR)

        # Open log file and close it after Popen to avoid FD leak
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
        logger.error(f"Failed to start bot {slug}: {e}")
        return {"ok": False, "error": "Failed to start bot — check server logs"}


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
        logger.error(f"Failed to stop bot {slug}: {e}")
        return {"ok": False, "error": "Failed to stop bot — check server logs"}


def restart_bot(slug: str) -> dict:
    stop_bot(slug)
    time.sleep(1)
    return start_bot(slug)


def _do_portal_restart():
    """Restart the portal process using os.execv — replaces current process."""
    time.sleep(0.8)
    logger.info("Portal restarting via os.execv — bots continue running")
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ── FastAPI app ───────────────────────────────────────────────────────────────

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
            d["exchange"] = b.get("exchange")
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
        return JSONResponse({"error": "Unknown bot"}, status_code=404)
    return bot.read_state()


@app.post("/api/bots/{slug}/start")
async def api_start(slug: str):
    return start_bot(slug)

@app.post("/api/bots/{slug}/stop")
async def api_stop(slug: str):
    return stop_bot(slug)

@app.post("/api/bots/{slug}/restart")
async def api_restart(slug: str):
    return restart_bot(slug)


@app.post("/api/portal/restart")
async def api_portal_restart():
    """
    Restart the portal process.
    Uses os.execv in a background thread so the HTTP response can
    reach the browser before the process is replaced.
    Bots continue running — they are independent subprocesses.
    """
    logger.info("Portal restart requested via API")
    t = threading.Thread(target=_do_portal_restart, daemon=True)
    t.start()
    return {"ok": True, "message": "Portal restarting — bots continue running"}


@app.get("/api/portal/status")
async def api_portal_status():
    """Health check — browser polls this to detect when portal is back up."""
    return {"ok": True, "pid": os.getpid()}


@app.get("/api/price")
async def api_price():
    """
    Always-on BTC price endpoint.
    Runs ccxt in a thread pool so it does NOT block the event loop.
    Uses a cached ccxt client — no new connection per request.
    """
    try:
        client = _get_price_client()
        ticker = await asyncio.to_thread(client.fetch_ticker, "BTCUSD")
        price  = ticker.get("last") or ticker.get("close") or 0.0
        return {"price": price, "pair": "BTC/USD", "source": "bitget"}
    except Exception as e:
        logger.warning(f"Price fetch failed: {type(e).__name__}")
        # Fall back to first running bot's cached price
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
    # Validate slug against known bots + 'portal' — prevents path traversal
    # and unbounded memory growth via unknown slugs
    if slug not in registry.valid_slugs():
        await websocket.close(code=4004)
        return

    log_file = (LOG_DIR / "portal.log") if slug == "portal" else registry.get(slug).log_file

    await broadcaster.connect(websocket, slug)
    try:
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
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
    """Background task: tail all bot logs + portal log and broadcast new lines."""
    last: dict[str, int] = {}

    while True:
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
                    last[slug] = size  # file was rotated
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
