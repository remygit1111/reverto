# web/app.py
# Reverto Web Portal — FastAPI backend
# Provides REST API endpoints and WebSocket log streaming
# for the Reverto dashboard.

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Shared state ─────────────────────────────────────────────────────────────
# The paper engine populates this at runtime.
# When running standalone (portal only), sensible defaults are shown.

class RevertoState:
    """Shared state between the engine and the web portal."""

    def __init__(self):
        self.engine_running: bool = False
        self.bot_name: str = "BTC-DCA-Paper"
        self.mode: str = "paper"
        self.exchange: str = "bitget"
        self.pair: str = "BTC/USD"
        self.current_price: float = 0.0
        self.price_updated_at: float = 0.0
        self.schedule_open: bool = False
        self.open_deals: list[dict] = []
        self.closed_deals: list[dict] = []
        self.balance_btc: float = 0.0
        self.initial_balance_btc: float = 0.0
        self.started_at: Optional[datetime] = None


state = RevertoState()

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="Reverto Portal", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"
LOG_FILE   = Path(__file__).parent.parent / "logs" / "reverto.log"


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the dashboard HTML."""
    html_file = STATIC_DIR / "index.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


@app.get("/api/status")
async def get_status():
    """Returns current bot status and performance summary."""
    uptime = None
    if state.started_at:
        delta = datetime.now() - state.started_at
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m, s   = divmod(rem, 60)
        uptime = f"{h:02d}:{m:02d}:{s:02d}"

    total_pnl = sum(d.get("pnl_btc", 0) for d in state.closed_deals)
    wins = len([d for d in state.closed_deals if d.get("pnl_btc", 0) > 0])
    win_rate = round(wins / len(state.closed_deals) * 100, 1) if state.closed_deals else 0.0

    return {
        "engine_running": state.engine_running,
        "bot_name": state.bot_name,
        "mode": state.mode,
        "exchange": state.exchange,
        "pair": state.pair,
        "current_price": state.current_price,
        "price_updated_at": state.price_updated_at,
        "schedule_open": state.schedule_open,
        "balance_btc": round(state.balance_btc, 8),
        "initial_balance_btc": round(state.initial_balance_btc, 8),
        "total_pnl_btc": round(total_pnl, 8),
        "open_deals_count": len(state.open_deals),
        "closed_deals_count": len(state.closed_deals),
        "win_rate": win_rate,
        "uptime": uptime,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/deals")
async def get_deals():
    """Returns all open and recently closed deals."""
    return {
        "open": state.open_deals,
        "closed": list(reversed(state.closed_deals))[:50],  # last 50
    }


@app.get("/api/price")
async def get_price():
    """Returns current BTC price."""
    return {
        "pair": state.pair,
        "price": state.current_price,
        "updated_at": state.price_updated_at,
    }


# ── WebSocket — live log streaming ───────────────────────────────────────────

class LogBroadcaster:
    """Tails the log file and broadcasts new lines to all connected clients."""

    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, line: str):
        dead = set()
        for ws in self.clients:
            try:
                await ws.send_text(line)
            except Exception:
                dead.add(ws)
        self.clients -= dead


broadcaster = LogBroadcaster()


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await broadcaster.connect(websocket)
    try:
        # Send last 100 lines on connect so client sees recent history
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-100:]:
                await websocket.send_text(line)

        # Keep connection alive — actual tailing is done by background task
        while True:
            await asyncio.sleep(30)
            await websocket.send_text("__ping__")

    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)
    except Exception:
        broadcaster.disconnect(websocket)


async def tail_log_file():
    """Background task: tail log file and broadcast new lines."""
    last_size = 0

    while True:
        try:
            if LOG_FILE.exists():
                current_size = LOG_FILE.stat().st_size
                if current_size > last_size:
                    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        new_lines = f.read()
                    last_size = current_size

                    for line in new_lines.splitlines():
                        if line.strip():
                            await broadcaster.broadcast(line)

                elif current_size < last_size:
                    # File was rotated
                    last_size = current_size

        except Exception:
            pass

        await asyncio.sleep(1)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(tail_log_file())


# ── Standalone runner ─────────────────────────────────────────────────────────

def run_portal(host: str = "0.0.0.0", port: int = 8080):
    """Start the web portal server."""
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_portal()
