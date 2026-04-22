"""Admin cross-user bot-overview routes.

Routes:
  GET  /api/admin/bots                           — every bot grouped by owner
  POST /api/admin/bots/{uid}/{slug}/start        — admin-initiated start
  POST /api/admin/bots/{uid}/{slug}/start-dry-run — admin-initiated dry-run
  POST /api/admin/bots/{uid}/{slug}/stop         — admin-initiated stop
  POST /api/admin/bots/{uid}/{slug}/restart      — admin-initiated restart

Every endpoint is gated on ``user.role == "admin"`` (403 otherwise).
Every lifecycle action double-logs: once to the central audit.log via
``_audit()`` AND once to the target bot's own log via
``_log_to_bot_log`` so the owner sees ``[ADMIN]`` lines when tailing
their normal bot log — matches audit v26-02's visibility pattern
(failed attempts → WARNING log line) extended for successful actions.

Kept in a separate module from admin.py because this surface will
grow (Phase 2 bulk actions, Phase 3 cross-user duplicate) and having
the admin.py file stay small keeps the ops/health probes readable.

Circular-import shape mirrors admin.py: this module is imported at the
BOTTOM of ``web/app.py`` so every name pulled from ``web.app`` is
already defined at import time.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from core import user_store
from core.user import User
from web.app import (
    _audit,
    _BOT_SLUG_RE,
    _log_to_bot_log,
    _request_user,
    limiter,
    registry,
    restart_bot,
    start_bot,
    start_bot_dry_run,
    stop_bot,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin_bots"])


def _require_admin(user: User) -> None:
    """Common guard for every route in this module.

    Mirrors the v26-02 pattern on ``/api/emergency-stop`` — reject
    non-admins with 403 before any cross-user work starts. Kept as a
    helper rather than a FastAPI Depends because the endpoints want
    the actor's ``user`` anyway for audit-log output.
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=403, detail="Admin role required",
        )


def _validate_slug(slug: str) -> None:
    if not _BOT_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Invalid slug")


# ── Read: cross-user bot overview ──────────────────────────────────────────

@router.get("/api/admin/bots")
@limiter.limit("60/minute")
async def list_all_bots(
    request: Request, user: User = Depends(_request_user),
):
    """Cross-user bot overview for the admin UI.

    Groups registry entries by owner and attaches the owner's
    username in the response so the frontend can render per-user
    headers without an N+1 ``/api/users/{id}`` round-trip. Sort is
    stable by user_id so the admin UI doesn't reorder groups on
    every refresh.

    Response shape:
      {"users": [
          {"user_id": 1, "username": "admin",
           "bots": [{slug, name, mode, running, ...}, ...]},
          ...,
      ]}
    """
    _require_admin(user)

    all_bots = await registry.all()
    by_user: dict[int, dict] = {}
    for bot in all_bots:
        if bot.user_id not in by_user:
            owner = user_store.get_user_by_id(bot.user_id)
            by_user[bot.user_id] = {
                "user_id": bot.user_id,
                "username": (
                    owner.username if owner else f"user_{bot.user_id}"
                ),
                "bots": [],
            }
        state = bot.read_state()
        by_user[bot.user_id]["bots"].append({
            "slug": bot.slug,
            "name": state.get("bot_name", bot.slug),
            "mode": state.get("mode"),
            "exchange": state.get("exchange"),
            "pair": state.get("pair"),
            "running": bot.running,
            "current_price": state.get("current_price"),
            "balance_btc": state.get("balance_btc"),
            "total_pnl_btc": state.get("total_pnl_btc"),
            "open_deals_count": state.get("open_deals_count"),
            "closed_deals_count": state.get("closed_deals_count"),
            "win_rate": state.get("win_rate"),
        })

    users_list = sorted(
        by_user.values(), key=lambda u: u["user_id"],
    )
    return {"users": users_list}


# ── Lifecycle: admin-initiated start / stop / restart ──────────────────────

@router.post("/api/admin/bots/{target_user_id}/{slug}/start")
@limiter.limit("20/minute")
async def admin_start_bot(
    target_user_id: int, slug: str,
    request: Request, user: User = Depends(_request_user),
):
    _require_admin(user)
    _validate_slug(slug)
    _audit(
        "admin_bot_start", f"user={target_user_id}/{slug}", user.username,
    )
    logger.warning(
        "[ADMIN ACTION] %s started bot user_id=%s slug=%s",
        user.username, target_user_id, slug,
    )
    _log_to_bot_log(
        target_user_id, slug,
        f"Bot started by admin user={user.username}",
    )
    return await start_bot(target_user_id, slug)


@router.post("/api/admin/bots/{target_user_id}/{slug}/start-dry-run")
@limiter.limit("20/minute")
async def admin_start_bot_dry_run(
    target_user_id: int, slug: str,
    request: Request, user: User = Depends(_request_user),
):
    _require_admin(user)
    _validate_slug(slug)
    _audit(
        "admin_bot_start_dry_run",
        f"user={target_user_id}/{slug}", user.username,
    )
    logger.warning(
        "[ADMIN ACTION] %s started (dry-run) bot user_id=%s slug=%s",
        user.username, target_user_id, slug,
    )
    _log_to_bot_log(
        target_user_id, slug,
        f"Bot dry-run started by admin user={user.username}",
    )
    return await start_bot_dry_run(target_user_id, slug)


@router.post("/api/admin/bots/{target_user_id}/{slug}/stop")
@limiter.limit("20/minute")
async def admin_stop_bot(
    target_user_id: int, slug: str,
    request: Request, user: User = Depends(_request_user),
):
    _require_admin(user)
    _validate_slug(slug)
    _audit(
        "admin_bot_stop", f"user={target_user_id}/{slug}", user.username,
    )
    logger.warning(
        "[ADMIN ACTION] %s stopped bot user_id=%s slug=%s",
        user.username, target_user_id, slug,
    )
    _log_to_bot_log(
        target_user_id, slug,
        f"Bot stopped by admin user={user.username}",
    )
    return await stop_bot(target_user_id, slug)


@router.post("/api/admin/bots/{target_user_id}/{slug}/restart")
@limiter.limit("20/minute")
async def admin_restart_bot(
    target_user_id: int, slug: str,
    request: Request, user: User = Depends(_request_user),
):
    _require_admin(user)
    _validate_slug(slug)
    _audit(
        "admin_bot_restart",
        f"user={target_user_id}/{slug}", user.username,
    )
    logger.warning(
        "[ADMIN ACTION] %s restarted bot user_id=%s slug=%s",
        user.username, target_user_id, slug,
    )
    _log_to_bot_log(
        target_user_id, slug,
        f"Bot restarted by admin user={user.username}",
    )
    return await restart_bot(target_user_id, slug)
