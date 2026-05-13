# notifications/telegram.py
# Per-user Telegram notifier (shared @RevertoAlertsBot model).
#
# Every TelegramNotifier instance is bound to a single user_id. Token
# still comes from the shared ``TELEGRAM_BOT_TOKEN`` env-var because
# one Telegram bot fans out to many chats; chat_id is looked up
# per-user from the ``telegram_configs`` table.
#
# Graceful degradation: if the user has not run the /start link flow
# yet, the constructor sets ``_enabled = False`` and every notify_*
# method becomes a silent no-op. Bots still boot, log "user not
# connected" at INFO, and keep trading — Telegram is a side channel.
#
# Safety events (ERROR, LIQ_WARN, SHUTDOWN) ALWAYS send to connected
# users regardless of ``notify_on`` — the backend enforces this in
# ``_is_enabled`` so the UI cannot accidentally mute a critical
# alert through a stale checkbox.

import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

from paper.errors import TickerError

load_dotenv()

logger = logging.getLogger(__name__)

_TELEGRAM_BASE = "https://api.telegram.org/bot"

# Event type constants — used to match against the per-user
# ``notify_on`` list. The store module references this set when it
# validates an admin-supplied preference update; keep both in sync.
EVENT_STARTUP        = "startup"
EVENT_SHUTDOWN       = "shutdown"
EVENT_STOP           = "stop"
EVENT_RESTART        = "restart"
EVENT_ENTRY          = "entry"
EVENT_DCA            = "dca_trigger"
EVENT_TP             = "tp_hit"
EVENT_SL             = "sl_hit"
EVENT_LIQ_WARN       = "liquidation_warn"
EVENT_SCHEDULE_OPEN  = "schedule_open"
EVENT_SCHEDULE_CLOSE = "schedule_close"
EVENT_ERROR          = "error"
EVENT_MANUAL_CLOSE   = "manual_close"
EVENT_MANUAL_CANCEL  = "manual_cancel"

# Safety events that always reach a connected user, bypassing
# ``notify_on``. Operators may have un-ticked these in the UI; the
# backend ignores that for the three classes of event where the
# operator absolutely needs the ping.
_ALWAYS_ON_EVENTS: frozenset = frozenset({
    EVENT_ERROR, EVENT_LIQ_WARN, EVENT_SHUTDOWN,
})


class TelegramNotifier:
    """Per-user Telegram notifier.

    Construct one per user_id. The constructor looks up the user's
    chat_id + notify_on from ``core.telegram_config_store``; if no
    config exists, every ``notify_*`` method silently no-ops
    (``_enabled = False``).

    ``notify_on=None`` (default) means "use the user's stored
    preferences". Callers can override with an explicit list — the
    portal-restart path uses this so a single notify call doesn't
    have to round-trip the store twice.

    ``chat_id_override`` is reserved for tests + the webhook's
    welcome-reply path (where we know the chat_id directly from the
    incoming Update and don't want a DB round-trip).
    """

    def __init__(
        self,
        user_id: Optional[int] = None,
        notify_on: Optional[list[str]] = None,
        *,
        chat_id_override: Optional[str] = None,
    ):
        self._token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self._token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN must be set in .env (shared "
                "@RevertoAlertsBot token).",
            )

        self._user_id = user_id

        if chat_id_override is not None:
            # Test / webhook-reply path. The notifier is bound to a
            # specific chat_id directly; no DB lookup, no notify_on
            # filter unless the caller supplied one.
            self.chat_id = str(chat_id_override)
            self._enabled = True
            self._notify_on = set(notify_on) if notify_on else None
            return

        if user_id is None:
            raise ValueError(
                "TelegramNotifier requires either user_id or "
                "chat_id_override",
            )

        # Lazy import — keeps notifications.telegram importable in
        # the rare contexts that don't have core.database set up
        # yet (e.g. unit tests that monkey-patch the store before
        # instantiating).
        from core import telegram_config_store
        config = telegram_config_store.get_config(user_id)
        if config is None:
            logger.info(
                "TelegramNotifier(user=%d): user has not run the "
                "/start flow yet — notifications disabled until "
                "they connect.", user_id,
            )
            self._enabled = False
            self.chat_id = None
            self._notify_on = set()
            return

        self._enabled = True
        self.chat_id = config["chat_id"]
        if notify_on is not None:
            self._notify_on = set(notify_on)
        else:
            self._notify_on = set(config.get("notify_on") or [])

    # ------------------------------------------------------------------
    # Gate helpers
    # ------------------------------------------------------------------

    def _is_enabled(self, event_type: str) -> bool:
        """True iff this event should hit the wire.

        Three layers:
          1. ``_enabled = False`` → never send (user not connected).
          2. Safety events bypass the per-user preference list.
          3. Otherwise the event must appear in ``notify_on``.

        ``notify_on=None`` is preserved as "send everything" so
        explicit-override callers (the portal-restart path) keep
        their unfiltered semantics.
        """
        if not self._enabled:
            return False
        if event_type in _ALWAYS_ON_EVENTS:
            return True
        if self._notify_on is None:
            return True
        return event_type in self._notify_on

    # ------------------------------------------------------------------
    # Core send method
    # ------------------------------------------------------------------

    def send(self, message: str) -> None:
        """Synchronous HTTP send. Swallows network failures so a
        Telegram outage cannot wedge an engine tick.

        No-op when the notifier is disabled. The early-return keeps
        the body of ``notify_*`` methods clean — they all guard on
        ``_is_enabled`` first.
        """
        if not self._enabled:
            return
        url = f"{_TELEGRAM_BASE}{self._token}/sendMessage"
        try:
            response = httpx.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            if response.status_code != 200:
                # Audit v26-09: log only status_code + body length,
                # never response.text — error bodies may echo the
                # URL and the URL contains the bot token.
                logger.error(
                    "Telegram error %d (body %d bytes)",
                    response.status_code, len(response.text or ""),
                )
                return
        except httpx.TimeoutException:
            logger.error("Telegram send failed: request timed out")
            return
        except httpx.RequestError:
            logger.error("Telegram send failed: network error")
            return
        except Exception as e:  # noqa: BLE001
            logger.error(f"Telegram send failed: {type(e).__name__}")
            return

        # Touch last_message_at so the admin UI can render
        # "last alert N minutes ago". Best-effort — DB unavailable
        # here is acceptable.
        if self._user_id is not None:
            try:
                from core import telegram_config_store
                telegram_config_store.touch_last_message_at(self._user_id)
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "touch_last_message_at failed for user=%d: %s",
                    self._user_id, e,
                )

    # ------------------------------------------------------------------
    # Bot lifecycle
    # ------------------------------------------------------------------

    def notify_startup(self, bot_name: str, mode: str, exchange: str):
        if not self._is_enabled(EVENT_STARTUP):
            return
        self.send(
            f"<b>{bot_name} started</b>\n"
            f"Mode     : {mode.upper()}\n"
            f"Exchange : {exchange.upper()}"
        )

    def notify_shutdown(self, bot_name: str):
        if not self._is_enabled(EVENT_SHUTDOWN):
            return
        self.send(f"<b>{bot_name} stopped</b>")

    def notify_stop(self, bot_name: str, mode: str, exchange: str):
        # New-style stop event. Falls back to the legacy "shutdown"
        # gate so existing configs still notify.
        if not (self._is_enabled(EVENT_STOP) or self._is_enabled(EVENT_SHUTDOWN)):
            return
        self.send(
            f"<b>{bot_name} stopped</b>\n"
            f"Mode     : {mode.upper()}\n"
            f"Exchange : {exchange.upper()}"
        )

    def notify_restart(self, bot_name: str):
        if not self._is_enabled(EVENT_RESTART):
            return
        self.send(
            f"🔄 <b>Reverto restarting</b>\n"
            f"Bot: {bot_name}"
        )

    # ------------------------------------------------------------------
    # Schedule events
    # ------------------------------------------------------------------

    def notify_schedule_open(self, bot_name: str, next_close: str):
        if not self._is_enabled(EVENT_SCHEDULE_OPEN):
            return
        self.send(
            f"🟢 <b>Trading window opened</b>\n"
            f"Bot       : {bot_name}\n"
            f"New deals : allowed\n"
            f"Closes at : {next_close}"
        )

    def notify_schedule_close(self, bot_name: str, next_open: str,
                               active_deals: int):
        if not self._is_enabled(EVENT_SCHEDULE_CLOSE):
            return
        self.send(
            f"🔴 <b>Trading window closed</b>\n"
            f"Bot          : {bot_name}\n"
            f"New deals    : blocked\n"
            f"Active deals : {active_deals} (still monitored)\n"
            f"Next open    : {next_open}"
        )

    # ------------------------------------------------------------------
    # Deal events
    # ------------------------------------------------------------------

    def notify_entry(self, bot_name: str, symbol: str, price: float,
                     size: float, leverage: int):
        if not self._is_enabled(EVENT_ENTRY):
            return
        lev = f"{leverage}x" if leverage > 1 else "No leverage"
        self.send(
            f"📈 <b>New deal started</b>\n"
            f"Bot      : {bot_name}\n"
            f"Symbol   : {symbol}\n"
            f"Price    : ${price:,.2f}\n"
            f"Size     : {size} contracts\n"
            f"Leverage : {lev}"
        )

    def notify_dca(self, bot_name: str, symbol: str, price: float,
                   size: float, order_number: int, avg_price: float):
        if not self._is_enabled(EVENT_DCA):
            return
        self.send(
            f"🔁 <b>DCA order placed</b>\n"
            f"Bot       : {bot_name}\n"
            f"Symbol    : {symbol}\n"
            f"Price     : ${price:,.2f}\n"
            f"Size      : {size} contracts\n"
            f"Order #   : {order_number}\n"
            f"Avg price : ${avg_price:,.2f}"
        )

    def notify_take_profit(self, bot_name: str, symbol: str, price: float,
                           pnl_btc: float, pnl_pct: float):
        if not self._is_enabled(EVENT_TP):
            return
        emoji = "🟢" if pnl_btc >= 0 else "🔴"
        self.send(
            f"{emoji} <b>Take profit hit</b>\n"
            f"Bot    : {bot_name}\n"
            f"Symbol : {symbol}\n"
            f"Price  : ${price:,.2f}\n"
            f"PnL    : {pnl_btc:+.6f} BTC ({pnl_pct:+.2f}%)"
        )

    def notify_stop_loss(self, bot_name: str, symbol: str, price: float,
                         pnl_btc: float, pnl_pct: float):
        if not self._is_enabled(EVENT_SL):
            return
        self.send(
            f"🛑 <b>Stop loss triggered</b>\n"
            f"Bot    : {bot_name}\n"
            f"Symbol : {symbol}\n"
            f"Price  : ${price:,.2f}\n"
            f"PnL    : {pnl_btc:+.6f} BTC ({pnl_pct:+.2f}%)"
        )

    # ------------------------------------------------------------------
    # Portal-triggered manual close / cancel
    # ------------------------------------------------------------------

    def notify_manual_close(self, bot_name: str, symbol: str, price: float,
                            pnl_btc: float, pnl_pct: float):
        if not self._is_enabled(EVENT_MANUAL_CLOSE):
            return
        self.send(
            f"🔧 <b>Manual close</b>\n"
            f"Bot       : {bot_name}\n"
            f"Symbol    : {symbol}\n"
            f"Price     : ${price:,.2f}\n"
            f"PnL       : {pnl_btc:+.6f} BTC ({pnl_pct:+.2f}%)\n"
            f"Triggered : portal (bot was stopped)"
        )

    def notify_manual_cancel(self, bot_name: str, symbol: str):
        if not self._is_enabled(EVENT_MANUAL_CANCEL):
            return
        self.send(
            f"🚫 <b>Manual cancel</b>\n"
            f"Bot       : {bot_name}\n"
            f"Symbol    : {symbol}\n"
            f"Triggered : portal (bot was stopped)"
        )

    # ------------------------------------------------------------------
    # Liquidation guard
    # ------------------------------------------------------------------

    def notify_liquidation_warning(self, bot_name: str, symbol: str,
                                    mark_price: float, liq_price: float,
                                    distance_pct: float):
        if not self._is_enabled(EVENT_LIQ_WARN):
            return
        self.send(
            f"⚠️ <b>LIQUIDATION WARNING</b>\n"
            f"Bot        : {bot_name}\n"
            f"Symbol     : {symbol}\n"
            f"Mark price : ${mark_price:,.2f}\n"
            f"Liq price  : ${liq_price:,.2f}\n"
            f"Distance   : {distance_pct:.2f}% ⚠️\n"
            f"<b>Consider reducing your position!</b>"
        )

    def notify_liquidation_emergency(self, bot_name: str, symbol: str,
                                      distance_pct: float):
        # Emergency always sends (when connected). Bypasses notify_on
        # via the EVENT_LIQ_WARN entry in _ALWAYS_ON_EVENTS.
        if not self._enabled:
            return
        self.send(
            f"🚨 <b>EMERGENCY — CRITICAL LIQUIDATION RISK</b>\n"
            f"Bot      : {bot_name}\n"
            f"Symbol   : {symbol}\n"
            f"Distance : {distance_pct:.2f}%\n"
            f"<b>Position is being reduced automatically!</b>"
        )

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def notify_error(self, bot_name: str, error: str):
        if not self._is_enabled(EVENT_ERROR):
            return
        self.send(
            f"❌ <b>Error</b>\n"
            f"Bot   : {bot_name}\n"
            f"Error : {error}"
        )

    def notify_error_persistent(self, bot_name: str, err: TickerError):
        """Rich error notification used when the paper/live engine has
        classified a failure as persistent.

        ERROR is in ``_ALWAYS_ON_EVENTS`` so a connected user always
        receives this regardless of their notify_on preferences —
        operational alerts override granular filtering.
        """
        if not self._is_enabled(EVENT_ERROR):
            return
        if err.is_transient:
            severity, state_label = "⚠️", "degraded"
        else:
            severity, state_label = "⛔", "blocked"

        reason = _resolve_error_reason(err)
        context = _resolve_error_context(err)
        action = _resolve_error_action(err)

        self.send(
            f"{severity} <b>Bot {state_label}</b>\n"
            f"Bot     : {bot_name}\n"
            f"Reason  : {reason}\n"
            f"Context : {context}\n"
            f"Action  : {action}"
        )


# ──────────────────────────────────────────────────────────────────────────
# Persistent-error message helpers
# ──────────────────────────────────────────────────────────────────────────

# Exchange status-page URLs surfaced in the Action line. Keys match the
# lowercase exchange name from TickerError.exchange. Unknown exchanges
# fall back to a generic "check your exchange" hint.
_STATUS_PAGES: dict[str, str] = {
    "bitget": "status.bitget.com",
    "binance": "binance.statuspage.io",
}


def _resolve_error_reason(err: TickerError) -> str:
    """Human-readable Reason line built from the error's classification.
    Mapping is by error_class rather than status_code because status is
    not always available (NetworkError has no http status)."""
    ex = err.exchange.capitalize()
    cls = err.error_class
    if cls == "RateLimitExceeded":
        return f"{ex} API returning 429 Too Many Requests"
    if cls == "AuthenticationError":
        return f"{ex} API authentication failure (401)"
    if cls in ("NetworkError", "RequestTimeout", "DDoSProtection"):
        return f"{ex} API network/timeout failure"
    if cls in ("ExchangeNotAvailable", "OnMaintenance"):
        return f"{ex} API unavailable / in maintenance"
    # Unknown / generic — surface class name + truncated message so an
    # unexpected failure still carries enough detail for a first-look
    # triage without digging into portal.log.
    head = err.message[:80].replace("\n", " ")
    return f"{cls}: {head}" if head else cls


def _resolve_error_context(err: TickerError) -> str:
    """Technical details: which API endpoint failed on which symbol, and
    how many retries were attempted before the persistent notification
    fired. Matches the spec's example format verbatim."""
    return (
        f"{err.endpoint} {err.symbol} — "
        f"{err.retry_attempt}/{err.max_retries} retries failed"
    )


def _resolve_error_action(err: TickerError) -> str:
    """What the user should do. Persistent-transient failures point at
    the exchange's status page; non-transient failures at the likely
    local cause (API-key permissions, code bug) instead."""
    ex = err.exchange.capitalize()
    if err.error_class == "AuthenticationError":
        return (
            f"Check API-key validity + permissions on {ex}. "
            "Restart bot via portal after correction."
        )
    if not err.is_transient:
        return (
            "Check portal logs for stack trace. "
            "Restart bot via portal after fix."
        )
    status_url = _STATUS_PAGES.get(err.exchange.lower())
    if status_url:
        return (
            f"Check {ex} status at {status_url}. "
            "Restart bot via portal when resolved."
        )
    return (
        f"Check {ex} API status + portal logs. "
        "Restart bot via portal when resolved."
    )
