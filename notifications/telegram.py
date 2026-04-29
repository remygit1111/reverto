# notifications/telegram.py
# Handles all Telegram notifications for Reverto.
# Uses httpx directly for thread-safe synchronous sending.
# Respects notify_on configuration — only sends events the user has enabled.

import httpx
import logging
import os
from dotenv import load_dotenv

from paper.errors import TickerError

load_dotenv()

logger = logging.getLogger(__name__)

_TELEGRAM_BASE = "https://api.telegram.org/bot"

# Event type constants — used to match against notify_on config
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
# Portal-triggered manual close / cancel (bot stopped, operator
# clicked close in the UI). Distinct from TP/SL so recipients can
# trace operator-initiated events separately from engine-driven ones.
EVENT_MANUAL_CLOSE   = "manual_close"
EVENT_MANUAL_CANCEL  = "manual_cancel"


class TelegramNotifier:
    """
    Sends Telegram notifications for all Reverto events.
    Uses httpx directly — fully thread-safe, no asyncio required.
    Token is stored privately and never embedded in stored URLs.

    Respects notify_on from the bot config — only events listed there
    will generate a Telegram message.
    """

    def __init__(self, token: str = None, chat_id: str = None,
                 notify_on: list[str] = None):
        self._token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

        if not self._token or not self.chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env"
            )

        # Default: send all events if no filter provided
        self._notify_on = set(notify_on) if notify_on else None

    def _is_enabled(self, event_type: str) -> bool:
        """Returns True if this event type should be sent."""
        if self._notify_on is None:
            return True
        return event_type in self._notify_on

    # ------------------------------------------------------------------
    # Core send method
    # ------------------------------------------------------------------

    def send(self, message: str):
        """Send a message via Telegram — thread-safe synchronous call."""
        url = f"{_TELEGRAM_BASE}{self._token}/sendMessage"
        try:
            response = httpx.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML"
                },
                timeout=10
            )
            if response.status_code != 200:
                # Audit v26-09: log only status_code + body-length,
                # not response.text. If Telegram's error format ever
                # echoes request-URL (which contains the bot token),
                # response.text would leak that into portal.log. Body
                # length is enough to distinguish "empty error" from
                # "verbose error" for debugging; content itself is
                # not needed at runtime.
                logger.error(
                    "Telegram error %d (body %d bytes)",
                    response.status_code, len(response.text or ""),
                )
        except httpx.TimeoutException:
            # Do not log the URL (contains token) — log only the error type
            logger.error("Telegram send failed: request timed out")
        except httpx.RequestError:
            logger.error("Telegram send failed: network error")
        except Exception as e:
            logger.error(f"Telegram send failed: {type(e).__name__}")

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
    #
    # Sent when the operator clicks the UI close button while the bot
    # is stopped — the portal runs DealCloseHandler directly instead
    # of writing a sentinel for the tick loop. Kept distinct from
    # notify_take_profit / notify_stop_loss so the Telegram recipient
    # can tell operator-initiated events apart from engine-driven
    # ones. Wrench / prohibit emoji match that "operator intervention"
    # framing — TP/SL's green/red targets stay reserved for automatic
    # closes that hit a price target.

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
        """Cancel doesn't realise PnL — the exchange position (if
        any) stays open and the operator manages it manually. No
        price field because the cancel is state-only bookkeeping,
        not a trade."""
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
        # Emergency always sends regardless of notify_on
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
        classified a failure as persistent — either an exhausted
        transient-retry streak or a non-transient error class that no
        retry will help.

        Severity-emoji follows the classification: non-transient failures
        (auth errors, bugs in our own code) render as ⛔ "Bot blocked"
        because they need operator intervention; exhausted-transient
        failures render as ⚠️ "Bot degraded" because the engine is still
        retrying but is structurally failing to make progress.

        Audit B-02: the non-transient label is "blocked" rather than
        "stopped" — the engine subprocess is still in its tick-loop
        when this notification fires, just unable to make progress
        until the operator intervenes. Saying "stopped" to the operator
        was misleading because process-state and progress-state are
        different things; "blocked" frames the message around what the
        operator needs to do (unblock by fixing the root cause) rather
        than implying the bot has already exited. Auto-stop on the
        engine side (transitioning ``self.running = False``) is tracked
        separately under v27-backlog B-02 — this fix is a UX-text-tweak
        only.

        Distinct from ``notify_error`` which takes a free-form string —
        that entry point stays for callers outside the engine tick path
        (insufficient-balance refusals, reconciler timeouts, etc.)."""
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
