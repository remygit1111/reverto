"""Admin / ops / health routes extracted from web/app.py.

Routes:
  GET  /healthz            — liveness probe (no auth, no rate-limit)
  GET  /readyz             — readiness probe w/ 3s DB timeout
  GET  /metrics            — Prometheus scrape (no auth)
  POST /api/emergency-stop — SIGTERM every running bot

Circular-import shape: this module is imported at the BOTTOM of
``web/app.py``. Module-level names we pull from ``web.app`` (limiter,
_request_actor, registry, stop_bot, _audit) are all defined before
that bottom import, so the ``from web.app import ...`` lines below
resolve against a fully-populated web.app module even though that
module is technically mid-initialisation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from web.app import (
    _audit,
    _do_portal_restart,
    _request_actor,
    limiter,
    registry,
    stop_bot,
)
# _check_db_sync_blocking is looked up dynamically inside readyz so the
# test_metrics.py `patch.object(webapp, "_check_db_sync_blocking", ...)`
# pattern continues to redirect the call site without us re-binding
# at import time.

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


# ── Health / readiness / metrics ────────────────────────────────────────────

@router.get("/healthz")
async def healthz(request: Request):
    """Liveness probe — no auth, no rate limit. Returns 200 as long as
    the portal process is actually answering. Use as Kubernetes
    livenessProbe so orchestrators don't restart healthy portals."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }


@router.get("/readyz")
async def readyz(request: Request):
    """Readiness probe — no auth, no rate limit. Verifies the SQLite
    ledger is reachable with a hard 3s timeout so a locked SQLite can't
    wedge the probe response."""
    # Late lookup so monkeypatch / unittest.mock.patch on
    # web.app._check_db_sync_blocking takes effect per-call.
    from web import app as _webapp
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_webapp._check_db_sync_blocking),
            timeout=3.0,
        )
        return {"status": "ready"}
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "error": "DB ping timed out (>3s)"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "error": str(e)[:200]},
        )


@router.get("/metrics")
async def metrics_endpoint(request: Request):
    """Prometheus scrape endpoint — no auth, no rate limit.

    Scraping is network-gated by design (firewall / ingress ACL).
    Prometheus service accounts don't carry user sessions, so sharing
    the API key with the monitoring stack would be worse than leaving
    /metrics reachable at the L7 while restricting at L3/L4.
    """
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ── Emergency stop + portal restart ─────────────────────────────────────────

@router.post("/api/emergency-stop")
@limiter.limit("5/minute")
async def api_emergency_stop(
    request: Request, actor: str = Depends(_request_actor),
):
    """Stop every running bot immediately (audit-logged + rate-limited).

    Uses the existing ``stop_bot(slug)`` plumbing so SIGTERM + notify
    drain still happens per bot — we're not pulling the rug out from
    under any in-flight order logic, just sending everyone the same
    graceful-stop signal.
    """
    _audit("emergency_stop", "-", actor)
    logger.error("EMERGENCY STOP requested by %s", actor)

    stopped: list[str] = []
    failed: list[dict] = []
    for bot in await registry.all():
        if not bot.running:
            continue
        try:
            result = await stop_bot(bot.slug)
            if result.get("ok"):
                stopped.append(bot.slug)
            else:
                failed.append({"slug": bot.slug, "error": result.get("error")})
        except Exception as e:
            failed.append({"slug": bot.slug, "error": str(e)[:200]})

    return {
        "ok": True,
        "stopped_bots": stopped,
        "failed": failed,
        "triggered_by": actor,
    }


@router.post("/api/portal/restart")
@limiter.limit("5/minute")
async def api_portal_restart(
    request: Request, actor: str = Depends(_request_actor),
):
    """Restart the portal process via os.execv in a background thread
    so the HTTP response reaches the browser before the replace."""
    _audit("portal_restart", "-", actor)
    logger.info("Portal restart requested via API")
    t = threading.Thread(target=_do_portal_restart, daemon=True)
    t.start()
    return {"ok": True, "message": "Portal restarting — reconnecting in a few seconds..."}


@router.get("/api/portal/status")
@limiter.limit("60/minute")
async def api_portal_status(request: Request):
    """Simple health check — browser polls this to detect portal-back."""
    return {"ok": True, "pid": os.getpid()}
