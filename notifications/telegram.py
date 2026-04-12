# notifications/telegram.py
# Handles all Telegram notifications for Reverto.
# Uses httpx directly for thread-safe synchronous sending.
# Respects notify_on configuration — only sends events the user has enabled.

import httpx
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TELEGRAM_BASE = "https://api.telegram.org/bot"

# Event type constants — used to match against notify_on config
EVENT_STARTUP        = "startup"
EVENT_SHUTDOWN       = "shutdown"
EVENT_ENTRY          = "entry"
EVENT_DCA            = "dca_trigger"
EVENT_TP             = "tp_hit"
EVENT_SL             = "sl_hit"
EVENT_LIQ_WARN       = "liquidation_warn"
EVENT_SCHEDULE_OPEN  = "schedule_open"
EVENT_SCHEDULE_CLOSE = "schedule_close"
EVENT_ERROR          = "error"


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
                logger.error(
                    f"Telegram error {response.status_code}: {response.text}"
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
            f"🚀 <b>Reverto started</b>\n"
            f"Bot      : {bot_name}\n"
            f"Mode     : {mode.upper()}\n"
            f"Exchange : {exchange.upper()}"
        )

    def notify_shutdown(self, bot_name: str):
        if not self._is_enabled(EVENT_SHUTDOWN):
            return
        self.send(f"🛑 <b>Reverto stopped</b>\nBot: {bot_name}")

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
