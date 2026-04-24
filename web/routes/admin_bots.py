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
from pydantic import BaseModel, Field

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
        user_id=user.id,
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
        user_id=user.id,
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
        user_id=user.id,
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
        user_id=user.id,
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


# ── Bulk lifecycle (Fase 2) ────────────────────────────────────────────────
# Sequential bulk stop + restart. Intentionally NOT a job-queue:
# scope is "20 bots or fewer" so a synchronous HTTP response stays
# within a sensible budget (20 × ~5s stop = ~100s worst case). The
# 20-cap is enforced in the Pydantic schema so callers never reach
# the handler with oversized payloads.
#
# Bulk start is deliberately excluded — accidentally starting
# another user's live-mode bot is a far bigger blast radius than
# stopping a paper bot one too many times, so Fase 2 stays on the
# "reverse-direction" side of the lifecycle.

# Upper bound on slug length — keeps the payload small and pairs
# with the _BOT_SLUG_RE regex on the per-bot endpoints (which
# doesn't cap length but in practice every bot slug we ship is
# well under 64 chars).
_BULK_SLUG_MAX_LEN = 64
_BULK_MAX_TARGETS = 20


class BulkBotTarget(BaseModel):
    """Single ``(user_id, slug)`` pair in a bulk request."""

    user_id: int = Field(gt=0)
    slug: str = Field(min_length=1, max_length=_BULK_SLUG_MAX_LEN)


class BulkBotRequest(BaseModel):
    """Request body for bulk lifecycle endpoints.

    ``min_length=1`` makes an empty list a 400 instead of a no-op
    200 so UIs can't silently submit nothing. ``max_length=20``
    bounds the worst-case time the handler can keep a connection
    open — 20 × ~5s shutdown ≈ 100s, still inside typical proxy
    idle-timeouts without needing streaming.
    """

    bots: list[BulkBotTarget] = Field(
        min_length=1, max_length=_BULK_MAX_TARGETS,
    )


def _validate_bulk_slugs(targets: list[BulkBotTarget]) -> None:
    """Defence-in-depth: Pydantic already caps length, but the
    per-bot endpoints also run the slug through ``_BOT_SLUG_RE``
    before touching the filesystem. Doing the same here keeps the
    bulk surface aligned so a traversal-shaped slug can't slip
    through the bulk path while being blocked on the per-bot path.
    """
    for target in targets:
        if not _BOT_SLUG_RE.match(target.slug):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid slug format: {target.slug!r}",
            )


async def _bulk_execute(
    body: BulkBotRequest,
    user: User,
    *,
    action_name: str,
    audit_action: str,
    bot_log_line: str,
    helper,
) -> dict:
    """Shared loop-body for bulk stop + bulk restart.

    ``helper`` is the async lifecycle primitive (``stop_bot`` or
    ``restart_bot``). We iterate sequentially rather than in
    parallel because the underlying helpers mutate shared state
    (registry's in-progress map, pid files) and concurrent calls
    on overlapping slugs would race — serial keeps the contract
    identical to the per-bot endpoints while still delivering the
    UI benefit of "one click, many bots".

    Failures are collected and returned alongside successes —
    partial success is a valid outcome. The caller (admin UI)
    surfaces both counts so the operator can see which subset
    still needs attention.
    """
    _validate_bulk_slugs(body.bots)
    _audit(
        audit_action, f"count={len(body.bots)}", user.username,
        user_id=user.id,
    )
    logger.warning(
        "[ADMIN BULK] %s requested bulk-%s of %d bots",
        user.username, action_name, len(body.bots),
    )

    processed: list[dict] = []
    failed: list[dict] = []

    for target in body.bots:
        try:
            result = await helper(target.user_id, target.slug)
        except Exception as e:
            logger.exception(
                "admin_bulk_%s: helper(user=%s, slug=%s) raised",
                action_name, target.user_id, target.slug,
            )
            failed.append({
                "user_id": target.user_id,
                "slug": target.slug,
                "error": str(e)[:200],
            })
            continue

        if result.get("ok"):
            processed.append({
                "user_id": target.user_id,
                "slug": target.slug,
                "result": action_name,
            })
            _log_to_bot_log(
                target.user_id, target.slug,
                bot_log_line.format(username=user.username),
            )
        else:
            failed.append({
                "user_id": target.user_id,
                "slug": target.slug,
                "error": result.get("error", "unknown"),
            })

    return {
        "ok": True,
        "processed": processed,
        "failed": failed,
        "total_requested": len(body.bots),
        "total_succeeded": len(processed),
        "total_failed": len(failed),
        "triggered_by": user.username,
    }


@router.post("/api/admin/bots/bulk/stop")
@limiter.limit("10/minute")
async def admin_bulk_stop(
    request: Request,
    body: BulkBotRequest,
    user: User = Depends(_request_user),
):
    """Sequential bulk stop, capped at 20 bots per call."""
    _require_admin(user)
    return await _bulk_execute(
        body, user,
        action_name="stop",
        audit_action="admin_bulk_stop",
        bot_log_line="Bot stopped by admin (bulk) user={username}",
        helper=stop_bot,
    )


@router.post("/api/admin/bots/bulk/restart")
@limiter.limit("10/minute")
async def admin_bulk_restart(
    request: Request,
    body: BulkBotRequest,
    user: User = Depends(_request_user),
):
    """Sequential bulk restart, capped at 20 bots per call."""
    _require_admin(user)
    return await _bulk_execute(
        body, user,
        action_name="restart",
        audit_action="admin_bulk_restart",
        bot_log_line="Bot restarted by admin (bulk) user={username}",
        helper=restart_bot,
    )
