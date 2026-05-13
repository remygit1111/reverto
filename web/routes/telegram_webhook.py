"""Telegram webhook receiver for the @RevertoAlertsBot /start flow.

Telegram doesn't sign webhook requests, so the authentication
surface is the URL secret embedded in the path: only Telegram
knows the full ``/api/telegram/webhook/<secret>`` URL because we
register it via the Bot API. Unsolicited POSTs from the open
internet hit ``/api/telegram/webhook`` (without the secret) and
land on a 404 from FastAPI — there is no handler at that path.

The webhook only handles ``/start <link_token>`` for now. Other
updates (free-form chat messages, button presses) are acknowledged
with a help reply so a curious user doesn't think the bot died.

Always returns 200 to Telegram regardless of internal state —
Telegram retries 5xx responses with exponential backoff which
would amplify any internal error into a flood.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core import telegram_config_store
from notifications.telegram import TelegramNotifier
from web.app import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram-webhook"])


_HELP_REPLY = (
    "👋 Hi! This bot only responds to setup links from the Reverto "
    "portal. Open https://app.reverto.bot/#admin/telegram to "
    "generate one."
)
_LINK_PREFIX = "link_"


# ── Request bodies ─────────────────────────────────────────────────────────


class _TgChat(BaseModel):
    id: int
    type: Optional[str] = None


class _TgFrom(BaseModel):
    id: Optional[int] = None
    first_name: Optional[str] = None


class _TgMessage(BaseModel):
    # ``from`` is a Python keyword; Pydantic v2 maps the JSON key
    # onto ``from_`` via ``Field(alias=...)`` + populate_by_name.
    model_config = ConfigDict(populate_by_name=True)

    message_id: Optional[int] = None
    chat: _TgChat
    text: Optional[str] = None
    from_: Optional[_TgFrom] = Field(default=None, alias="from")


class _TgUpdate(BaseModel):
    update_id: int
    message: Optional[_TgMessage] = None


# ── Helpers ────────────────────────────────────────────────────────────────


def _verify_secret(provided: str) -> None:
    """Constant-time match against ``TELEGRAM_WEBHOOK_SECRET``.

    Missing or empty env-var: refuse all requests with 404 (same
    response as an unknown URL would produce), so an operator that
    forgot to register a secret doesn't expose the endpoint as a
    wide-open POST.
    """
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or not secrets.compare_digest(provided, expected):
        # 404 not 401 — we deliberately don't tell the caller
        # whether the secret was the issue. An unprivileged scanner
        # gets the same response as if the route didn't exist.
        raise HTTPException(status_code=404, detail="Not Found")


def _send_reply(chat_id: int, text: str) -> None:
    """Fire-and-forget reply to the originating chat. Errors are
    swallowed — the webhook contract is "always return 200" and a
    failing reply must not break the contract.
    """
    try:
        TelegramNotifier(chat_id_override=str(chat_id)).send(text)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Webhook reply to chat=%d failed: %s", chat_id, e,
        )


# ── Route ──────────────────────────────────────────────────────────────────


@router.post("/api/telegram/webhook/{secret}")
@limiter.limit("60/minute")
async def telegram_webhook(
    secret: str,
    update: _TgUpdate,
    request: Request,
):
    """Receive a Telegram Update payload.

    The contract with Telegram is "always 200 unless the URL is
    bogus". Internal failures (bad token format, DB write error)
    log and return 200 so Telegram doesn't retry-storm.
    """
    _verify_secret(secret)

    msg = update.message
    if msg is None or msg.text is None:
        # Update kinds we don't handle (edits, callbacks, channel
        # posts). Acknowledge silently.
        return {"ok": True}

    text = msg.text.strip()
    chat_id = msg.chat.id

    if not text.startswith("/start"):
        _send_reply(chat_id, _HELP_REPLY)
        return {"ok": True}

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        _send_reply(
            chat_id,
            "👋 Welcome! To link your account, open Reverto and "
            "generate a setup link from /admin/telegram.",
        )
        return {"ok": True}

    payload = parts[1].strip()
    if not payload.startswith(_LINK_PREFIX):
        _send_reply(chat_id, _HELP_REPLY)
        return {"ok": True}

    token = payload[len(_LINK_PREFIX):]
    if not token:
        _send_reply(chat_id, _HELP_REPLY)
        return {"ok": True}

    try:
        user_id = telegram_config_store.consume_link_token(
            token, str(chat_id),
        )
    except Exception as e:  # noqa: BLE001 — DB failure
        logger.exception(
            "Webhook: consume_link_token raised for chat=%d: %s",
            chat_id, e,
        )
        # Don't leak internals to the user; give them a reasonable
        # action.
        _send_reply(
            chat_id,
            "Something went wrong on our side. Please try again "
            "in a minute, or generate a fresh link from the portal.",
        )
        return {"ok": True}

    if user_id is None:
        _send_reply(
            chat_id,
            "🔒 That link has expired or was already used. Generate "
            "a fresh one from https://app.reverto.bot/#admin/telegram.",
        )
        return {"ok": True}

    _send_reply(
        chat_id,
        "✅ <b>Connected!</b>\n\n"
        "You'll now receive Reverto alerts in this chat. Manage "
        "which events trigger a notification under "
        "/admin/telegram in the portal.",
    )
    logger.info(
        "Webhook linked Telegram chat=%d to user_id=%d",
        chat_id, user_id,
    )
    return {"ok": True}
