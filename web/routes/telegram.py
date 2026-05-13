"""Per-user Telegram admin routes.

The operator manages their @RevertoAlertsBot connection through
these endpoints:

  GET    /api/telegram/config         current connection state
  POST   /api/telegram/link           mint a fresh /start link
  PATCH  /api/telegram/notify-on      update event preferences
  POST   /api/telegram/test-message   send a "connected" ping
  DELETE /api/telegram/config         disconnect (drop the row)

All endpoints are auth-gated via ``_request_user`` and rate-limited
through slowapi. The webhook route lives in
``web/routes/telegram_webhook.py`` — it accepts the inbound /start
event and is intentionally NOT mounted here so the URL-secret
gate stays unambiguously separate from the auth-gated admin
surface.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import telegram_config_store
from core.user import User
from notifications.telegram import TelegramNotifier
from web.app import _audit, _request_actor, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram"])


# ── Request bodies ─────────────────────────────────────────────────────────


class _NotifyOnBody(BaseModel):
    events: list[str] = Field(
        default_factory=list,
        description="Event-type strings the user wants to receive.",
    )


# ── Helpers ────────────────────────────────────────────────────────────────


def _mask_chat_id(chat_id: str) -> str:
    """Show only the last 4 chars; ``***1234``. The chat_id is not
    a credential per se (Telegram won't act on it without the bot
    token), but it's a per-user identifier we don't need to render
    in plaintext on every admin page load."""
    if not chat_id:
        return ""
    last = chat_id[-4:]
    return f"***{last}"


def _bot_username() -> str:
    """Resolve the @RevertoAlertsBot username. Comes from env so the
    operator can swap the bot for a sandbox during testing without
    changing code."""
    return os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("/api/telegram/config")
@limiter.limit("60/minute")
async def get_telegram_config(
    request: Request,
    user: User = Depends(_request_user),
):
    """Return the user's current Telegram setup, or a
    ``connected=false`` envelope if they have not run /start yet.

    ``chat_id_masked`` returns ``***NNNN`` rather than the raw id —
    the UI surfaces this so the operator can confirm they connected
    the right account without leaking the chat_id into screenshots
    or browser-history fragments.
    """
    config = telegram_config_store.get_config(user.id)
    if config is None:
        return {
            "connected": False,
            "chat_id_masked": None,
            "connected_at": None,
            "last_message_at": None,
            "notify_on": [],
        }
    return {
        "connected": True,
        "chat_id_masked": _mask_chat_id(config["chat_id"]),
        "connected_at": config["connected_at"],
        "last_message_at": config["last_message_at"],
        "notify_on": config["notify_on"],
    }


@router.post("/api/telegram/link")
@limiter.limit("10/minute")
async def create_telegram_link(
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Mint a one-time link token and return the t.me deeplink.

    Re-clicking "Connect" before the previous link was consumed
    drops the prior token — there is only ever one outstanding
    link per user.

    Requires ``TELEGRAM_BOT_USERNAME`` env-var to be set on the
    server (so we know which bot to embed in the t.me URL). A
    missing env-var returns 500 — this is a deploy-time
    configuration error, not an operator-facing problem.
    """
    bot_username = _bot_username()
    if not bot_username:
        logger.error(
            "TELEGRAM_BOT_USERNAME is not configured — cannot mint "
            "link for user=%d", user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Telegram bot username is not configured on server",
        )
    token = telegram_config_store.create_link_token(user.id)
    expires = telegram_config_store.get_link_token_expiry(token)
    _audit(
        "telegram_link_create", "-", actor,
        user_id=user.id, request=request,
    )
    return {
        "token": token,
        "telegram_url": (
            f"https://t.me/{bot_username}?start=link_{token}"
        ),
        "expires_at": (
            expires.isoformat()
            if isinstance(expires, datetime) else None
        ),
    }


@router.patch("/api/telegram/notify-on")
@limiter.limit("30/minute")
async def update_telegram_notify_on(
    body: _NotifyOnBody,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Replace the user's notify_on preferences with ``body.events``.

    Returns 404 if the user has not connected yet — there is no
    row to update. Returns 400 if the body contains unknown
    event-type strings.

    Safety events (ERROR / LIQ_WARN / SHUTDOWN) can be un-ticked
    in the body without the backend complaining — the backend
    enforces the override silently at notify time. The honest
    UI text in the admin page calls this out.
    """
    try:
        changed = telegram_config_store.update_notify_on(
            user.id, body.events,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not changed:
        raise HTTPException(
            status_code=404,
            detail=(
                "No Telegram config found for this user — connect "
                "first via POST /api/telegram/link."
            ),
        )
    _audit(
        "telegram_notify_on_update",
        f"count={len(body.events)}", actor,
        user_id=user.id, request=request,
    )
    config = telegram_config_store.get_config(user.id)
    return {"ok": True, "notify_on": config["notify_on"] if config else []}


@router.post("/api/telegram/test-message")
@limiter.limit("5/minute")
async def send_test_message(
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Deliver a "Reverto Alerts test" message to the user's chat.

    Useful right after the /start flow to confirm the connection
    end-to-end. Returns:
      * 200 if delivered
      * 404 if the user isn't connected yet
      * 502 if Telegram refused / network failed (we don't have a
        strong signal for "definitely failed" so this is best-
        effort; the operator can re-check via the admin page)
    """
    if not telegram_config_store.is_connected(user.id):
        raise HTTPException(
            status_code=404,
            detail="Telegram is not connected for this user.",
        )
    try:
        notifier = TelegramNotifier(user_id=user.id)
    except ValueError as e:
        # TELEGRAM_BOT_TOKEN not set on server — operator-side
        # config problem.
        logger.warning(
            "Test message: notifier instantiation failed: %s", e,
        )
        raise HTTPException(
            status_code=503,
            detail="Telegram bot token is not configured on server.",
        )

    if not notifier._enabled:  # noqa: SLF001 — defensive check
        # Race: row removed between is_connected and instantiation.
        raise HTTPException(
            status_code=404,
            detail="Telegram is not connected for this user.",
        )

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    notifier.send(
        "🔔 <b>Reverto Alerts test</b>\n"
        f"Your account is connected at {ts}.\n"
        "Manage your event preferences in /admin/telegram."
    )
    _audit(
        "telegram_test_message", "-", actor,
        user_id=user.id, request=request,
    )
    return {"ok": True}


@router.delete("/api/telegram/config")
@limiter.limit("10/minute")
async def disconnect_telegram(
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Remove the user's telegram_configs row. Idempotent — a
    second DELETE returns 404 because the prior call already
    cleared the row.
    """
    removed = telegram_config_store.disconnect(user.id)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail="Telegram was not connected for this user.",
        )
    _audit(
        "telegram_disconnect", "-", actor,
        user_id=user.id, request=request,
    )
    return {"ok": True}
