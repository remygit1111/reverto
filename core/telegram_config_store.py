"""Per-user Telegram chat metadata + link-token tickets.

Backs the shared-bot model: every Reverto user connects their own
Telegram chat to @RevertoAlertsBot once, and from then on the
``TelegramNotifier(user_id=...)`` looks up the chat_id here when it
needs to send.

The /start link flow:
  1. Operator clicks "Connect Telegram" in admin → portal calls
     ``create_link_token(user_id)`` → emits a 32-char hex ticket.
  2. Operator opens ``t.me/RevertoAlertsBot?start=link_<token>`` →
     taps Start in Telegram.
  3. Telegram POSTs the /start event to /api/telegram/webhook/<sec>.
  4. Webhook calls ``consume_link_token(token, chat_id)`` →
     upserts the ``telegram_configs`` row, marks the ticket used.

Why one ticket per call (and not e.g. embed the user_id into the
URL): tokens are unguessable so an attacker who guesses a user-id
cannot connect their own Telegram to that user's account. Tokens
also expire (1 h) so a leaked URL has a short blast radius.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.database import get_db

logger = logging.getLogger(__name__)


def _token_correlator(token: str) -> str:
    """Non-reversible 12-char identifier for a link token, safe to log.

    Used in lookup-miss paths where we don't have a user_id to log
    instead (the token isn't in the DB, so we can't resolve who it
    belonged to). sha256-truncated so logs stay grep-able for repeat
    misses without leaking any portion of the bearer.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:12]


# Single source of truth for valid event-type strings stored in
# ``notify_on``. Imported lazily so this store module stays free
# of any notifications.* dependency at module-load time (the
# scheduler boots this module without ever importing httpx).
def _valid_event_types() -> set[str]:
    from notifications.telegram import (
        EVENT_DCA, EVENT_ENTRY, EVENT_ERROR, EVENT_LIQ_WARN,
        EVENT_MANUAL_CANCEL, EVENT_MANUAL_CLOSE, EVENT_RESTART,
        EVENT_SCHEDULE_CLOSE, EVENT_SCHEDULE_OPEN, EVENT_SHUTDOWN,
        EVENT_SL, EVENT_STARTUP, EVENT_STOP, EVENT_TP,
    )
    return {
        EVENT_STARTUP, EVENT_SHUTDOWN, EVENT_STOP, EVENT_RESTART,
        EVENT_ENTRY, EVENT_DCA, EVENT_TP, EVENT_SL,
        EVENT_LIQ_WARN, EVENT_SCHEDULE_OPEN, EVENT_SCHEDULE_CLOSE,
        EVENT_ERROR, EVENT_MANUAL_CLOSE, EVENT_MANUAL_CANCEL,
    }


# Default-on events for a freshly-connected user: every trade-flow
# event + the safety ones. Schedule-close intentionally OFF because
# bots silently mute outside hours and most operators don't want a
# nightly ping for that.
DEFAULT_NOTIFY_ON: list[str] = [
    "startup", "stop", "restart", "error", "liquidation_warn",
    "entry", "dca_trigger", "tp_hit", "sl_hit",
    "schedule_open",
]

_LINK_TOKEN_TTL = timedelta(hours=1)
_LINK_TOKEN_BYTES = 16  # 32-hex-char tokens — see secrets.token_hex


# ── Link tokens ───────────────────────────────────────────────────────────


def create_link_token(user_id: int) -> str:
    """Mint a fresh link-token for ``user_id``.

    Any unused (un-consumed and not-yet-expired) tokens belonging
    to the same user are dropped first — operators clicking
    "Connect" twice should not have multiple tickets in flight.
    The returned token is the URL-safe 32-hex-char string the
    frontend embeds in the t.me deeplink.
    """
    token = secrets.token_hex(_LINK_TOKEN_BYTES)
    now = datetime.now(timezone.utc)
    expires = now + _LINK_TOKEN_TTL
    conn = get_db()
    with conn:
        conn.execute(
            "DELETE FROM telegram_link_tokens "
            "WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO telegram_link_tokens "
            "(token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(), expires.isoformat()),
        )
    return token


def get_link_token_expiry(token: str) -> Optional[datetime]:
    """Look up ``expires_at`` for a token — used by the admin route
    so it can echo the expiry back to the UI without minting again.
    Returns ``None`` when the token doesn't exist.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT expires_at FROM telegram_link_tokens WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    try:
        ts = row["expires_at"]
        return datetime.fromisoformat(ts)
    except (ValueError, KeyError):
        return None


def consume_link_token(token: str, chat_id: str) -> Optional[int]:
    """Single-use consume + telegram_configs upsert.

    Returns the ``user_id`` on success (token valid, not yet used,
    not expired) or ``None`` otherwise. On success the
    ``telegram_configs`` row is written with the DEFAULT_NOTIFY_ON
    preference set if the user had no prior config — re-linking
    keeps whatever ``notify_on`` they had before.

    Idempotency: a token that was already consumed returns None
    (the webhook tells the operator to generate a fresh link).
    """
    now = datetime.now(timezone.utc)
    conn = get_db()
    row = conn.execute(
        "SELECT user_id, expires_at, used_at "
        "FROM telegram_link_tokens WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        logger.info(
            "Link token lookup miss for token-correlator=%s",
            _token_correlator(token),
        )
        return None
    if row["used_at"] is not None:
        logger.info(
            "Link token already used: user=%d", int(row["user_id"]),
        )
        return None
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        logger.warning(
            "Unparseable expires_at on link token for user=%d",
            int(row["user_id"]),
        )
        return None
    if expires_at <= now:
        logger.info(
            "Link token expired at %s for user=%d",
            expires_at.isoformat(), int(row["user_id"]),
        )
        return None
    user_id = int(row["user_id"])
    with conn:
        conn.execute(
            "UPDATE telegram_link_tokens SET used_at = ? WHERE token = ?",
            (now.isoformat(), token),
        )
        # Upsert: preserve a prior user's notify_on if they had one.
        existing = conn.execute(
            "SELECT notify_on FROM telegram_configs WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        notify_on_json = (
            existing["notify_on"] if existing is not None
            else json.dumps(DEFAULT_NOTIFY_ON)
        )
        conn.execute(
            "INSERT INTO telegram_configs "
            "(user_id, chat_id, notify_on, connected_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  chat_id = excluded.chat_id, "
            "  notify_on = excluded.notify_on, "
            "  connected_at = excluded.connected_at",
            (user_id, chat_id, notify_on_json, now.isoformat()),
        )
    logger.info(
        "Telegram link consumed: user=%d chat=%s",
        user_id, chat_id,
    )
    return user_id


def cleanup_expired_tokens() -> int:
    """Drop every token whose ``expires_at`` has passed (whether or
    not it was consumed). Returns the row-count deleted. Idempotent.

    Called by the hourly scheduler tick — keeping consumed tokens
    around after expiry buys nothing (a replay attempt fails the
    used_at check anyway) and the row count grows linearly with
    operator clicks otherwise.
    """
    now = datetime.now(timezone.utc)
    conn = get_db()
    with conn:
        cur = conn.execute(
            "DELETE FROM telegram_link_tokens "
            "WHERE datetime(expires_at) < datetime(?)",
            (now.isoformat(),),
        )
    return int(cur.rowcount or 0)


# ── telegram_configs CRUD ─────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    try:
        notify_on = json.loads(row["notify_on"])
        if not isinstance(notify_on, list):
            notify_on = []
    except (TypeError, ValueError):
        notify_on = []
    return {
        "user_id": int(row["user_id"]),
        "chat_id": str(row["chat_id"]),
        "notify_on": [str(e) for e in notify_on],
        "connected_at": row["connected_at"],
        "last_message_at": row["last_message_at"],
    }


def get_config(user_id: int) -> Optional[dict]:
    """Return the user's connected-chat metadata or None.

    A None return is the "user has not run the /start flow yet"
    signal — callers (``TelegramNotifier``, admin route) treat it
    as a graceful no-op and surface the "Connect Telegram" CTA in
    the UI respectively.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM telegram_configs WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def is_connected(user_id: int) -> bool:
    """Cheap presence check used by the first-time-wizard banner —
    returns True iff the user has a chat_id stored."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM telegram_configs WHERE user_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None


def update_notify_on(user_id: int, events: list[str]) -> bool:
    """Replace the user's preference list with ``events``.

    Validates each event-type against the canonical set in
    ``notifications.telegram``. Returns True on success, False
    when the user has no config row (nothing to update). Unknown
    event-types raise ValueError so the route surfaces a clean 400.
    """
    valid = _valid_event_types()
    bad = [e for e in events if e not in valid]
    if bad:
        raise ValueError(f"Unknown event types: {sorted(bad)}")
    deduped = list(dict.fromkeys(events))  # preserve order, drop dups
    conn = get_db()
    with conn:
        cur = conn.execute(
            "UPDATE telegram_configs SET notify_on = ? WHERE user_id = ?",
            (json.dumps(deduped), user_id),
        )
    return (cur.rowcount or 0) > 0


def touch_last_message_at(user_id: int) -> None:
    """Stamp the most recent successful send. Best-effort —
    failures are swallowed because the user-facing send already
    succeeded; this is just metadata."""
    try:
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE telegram_configs SET last_message_at = ? "
                "WHERE user_id = ?",
                (datetime.now(timezone.utc).isoformat(), user_id),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "touch_last_message_at failed for user=%d: %s", user_id, e,
        )


def disconnect(user_id: int) -> bool:
    """Remove the user's row. Returns True if a row was deleted."""
    conn = get_db()
    with conn:
        cur = conn.execute(
            "DELETE FROM telegram_configs WHERE user_id = ?",
            (user_id,),
        )
    return (cur.rowcount or 0) > 0


def all_connected_user_ids() -> list[int]:
    """Every user_id with a stored chat. Used by the
    public-exchange CircuitBreaker permanent-open callback so an
    operational alert fans out across every tenant whose bots
    share the affected exchange — there is no single "this user"
    in scope at that layer."""
    conn = get_db()
    rows = conn.execute(
        "SELECT user_id FROM telegram_configs ORDER BY user_id ASC",
    ).fetchall()
    return [int(r["user_id"]) for r in rows]
