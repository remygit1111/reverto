# notifications/telegram.py
# Handles all Telegram notifications for Reverto.
# Sends formatted messages for all bot events.

import asyncio
import logging
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sends Telegram notifications for all Reverto events.
    All messages are sent asynchronously to avoid blocking the main bot loop.
    """

    def __init__(self, token: str = None, chat_id: str = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

        if not self.token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")

        self.bot = Bot(token=self.token)

    # ------------------------------------------------------------------
    # Core send method
    # ------------------------------------------------------------------

    async def _send(self, message: str):
        """Internal method to send a message via Telegram."""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="HTML"
            )
        except TelegramError as e:
            logger.error(f"Telegram send failed: {e}")

    def send(self, message: str):
        """Synchronous wrapper — can be called from non-async code."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            loop.run_until_complete(self._send(message))
        except RuntimeError:
            asyncio.run(self._send(message))

    # ------------------------------------------------------------------
    # Bot lifecycle
    # ------------------------------------------------------------------

    def notify_startup(self, bot_name: str, mode: str, exchange: str):
        self.send(
            f"🚀 <b>Reverto started</b>\n"
            f"Bot      : {bot_name}\n"
            f"Mode     : {mode.upper()}\n"
            f"Exchange : {exchange.upper()}"
        )

    def notify_shutdown(self, bot_name: str):
        self.send(f"🛑 <b>Reverto stopped</b>\n Bot: {bot_name}")

    # ------------------------------------------------------------------
    # Schedule events
    # ------------------------------------------------------------------

    def notify_schedule_open(self, bot_name: str, next_close: str):
        self.send(
            f"🟢 <b>Trading window opened</b>\n"
            f"Bot       : {bot_name}\n"
            f"New deals : allowed\n"
            f"Closes at : {next_close}"
        )

    def notify_schedule_close(self, bot_name: str, next_open: str, active_deals: int):
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
        self.send(
            f"❌ <b>Error</b>\n"
            f"Bot   : {bot_name}\n"
            f"Error : {error}"
        )