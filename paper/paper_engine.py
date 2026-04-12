# paper/paper_engine.py
# Simulates live trading using real market prices but virtual orders.
# Uses the same logic as the live engine — only order execution differs.

import time
import uuid
import logging
from datetime import datetime
from config.models import BotConfig
from core.schedule_guard import ScheduleGuard
from exchanges.base_exchange import BaseExchange
from notifications.telegram import TelegramNotifier
from paper.paper_state import PaperState, PaperDeal, PaperOrder
from strategies.indicator_engine import IndicatorEngine
from core.liquidation_guard import LiquidationGuard, calculate_liquidation_price

logger = logging.getLogger(__name__)


class PaperEngine:
    """
    Paper trading engine for Reverto.
    Fetches real prices, simulates order execution internally.
    Monitors DCA, TP and SL on every price update.
    """

    def __init__(
        self,
        config: BotConfig,
        exchange: BaseExchange,
        notifier: TelegramNotifier,
        initial_balance_btc: float = 0.1,
        poll_interval: int = 10
    ):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.state = PaperState(initial_balance_btc)
        self.guard = ScheduleGuard(config.schedule)
        self.indicator_engine = IndicatorEngine(config)
        self.liq_guard = LiquidationGuard(config, notifier)
        self._closes: list[float] = []  # Rolling window of closing prices
        self.poll_interval = poll_interval  # seconds between price checks
        self.running = False

        # Track schedule state to detect open/close transitions
        self._last_schedule_state: bool = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def start(self):
        """Start the paper engine loop."""
        logger.info(f"Starting paper engine: {self.config.name}")
        self.running = True
        self.liq_guard.start()

        self.notifier.notify_startup(
            self.config.name,
            self.config.mode.value,
            self.config.exchange.value
        )

        try:
            while self.running:
                self._tick()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Stop the paper engine and liquidation guard gracefully."""
        self.running = False
        self.liq_guard.stop()
        summary = self.state.summary()
        logger.info(f"Paper engine stopped. Summary: {summary}")
        self.notifier.notify_shutdown(self.config.name)

    # ------------------------------------------------------------------
    # Main tick — runs every poll_interval seconds
    # ------------------------------------------------------------------

    def _tick(self):
        """Single iteration of the main loop."""
        try:
            ticker = self.exchange.get_ticker(self.config.pair)
            price = ticker.last

            logger.debug(f"{self.config.pair} price: ${price:,.2f}")

            # Check schedule transitions
            self._check_schedule_transition()

            # Monitor all open deals (DCA, TP, SL) — always runs
            self._monitor_open_deals(price)
            # Update liquidation guard with current positions
            self._update_liq_guard(price)

            # Only start new deals if schedule allows
            if self.guard.is_open():
                self._check_entry(price)

        except Exception as e:
            logger.error(f"Tick error: {e}")
            self.notifier.notify_error(self.config.name, str(e))

    # ------------------------------------------------------------------
    # Schedule transition detection
    # ------------------------------------------------------------------

    def _check_schedule_transition(self):
        """Detect and notify when trading window opens or closes."""
        current = self.guard.is_open()

        if self._last_schedule_state is None:
            self._last_schedule_state = current
            return

        if current and not self._last_schedule_state:
            # Window just opened
            status = self.guard.status()
            self.notifier.notify_schedule_open(
                self.config.name,
                status.get("next_open", "—")
            )

        elif not current and self._last_schedule_state:
            # Window just closed
            status = self.guard.status()
            self.notifier.notify_schedule_close(
                self.config.name,
                status.get("next_open", "—"),
                len(self.state.open_deals)
            )

        self._last_schedule_state = current

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def _check_entry(self, price: float):
        """
        Check if conditions are met to start a new deal.
        Fetches latest candles and evaluates all configured indicators.
        """
        # Only one open deal at a time for now
        if len(self.state.open_deals) > 0:
            return

        # Fetch latest candles for indicator calculation
        try:
            candles = self.exchange.get_ohlcv(self.config.pair, "1h", 100)
            self._closes = [c[4] for c in candles]
        except Exception as e:
            logger.error(f"Failed to fetch candles: {e}")
            return

        # Log current indicator snapshot
        snapshot = self.indicator_engine.get_indicator_snapshot(self._closes)
        logger.info(
            f"Indicators — RSI: {snapshot.get('rsi_14', '?')} | "
            f"EMA9: {snapshot.get('ema_9', '?')} | "
            f"EMA21: {snapshot.get('ema_21', '?')} | "
            f"MACD hist: {snapshot.get('macd_histogram', '?')}"
        )

        # Check entry signal
        if self.indicator_engine.check_entry_signal(self._closes):
            self._open_deal(price)

    def _open_deal(self, price: float):
        """Open a new paper deal at the current price."""
        deal_id = self.state.new_deal_id()
        base_order = PaperOrder(
            order_number=1,
            price=price,
            size=self.config.dca.base_order_size,
            timestamp=datetime.utcnow(),
            order_type="base"
        )

        deal = PaperDeal(
            id=deal_id,
            bot_name=self.config.name,
            symbol=self.config.pair,
            side="long",
            leverage=self.config.leverage.size,
            orders=[base_order]
        )

        self.state.open_deal(deal)
        logger.info(f"Deal opened: {deal_id} at ${price:,.2f}")

        self.notifier.notify_entry(
            self.config.name,
            self.config.pair,
            price,
            base_order.size,
            self.config.leverage.size
        )

    # ------------------------------------------------------------------
    # Deal monitoring — always runs regardless of schedule
    # ------------------------------------------------------------------

    def _monitor_open_deals(self, price: float):
        """Monitor all open deals for DCA, TP and SL conditions."""
        for deal_id, deal in list(self.state.open_deals.items()):
            self._check_tp(deal, price)
            self._check_sl(deal, price)
            self._check_dca(deal, price)

    def _check_tp(self, deal: PaperDeal, price: float):
        """Check if take profit target has been reached."""
        target_pct = self.config.take_profit.target_pct
        avg = deal.avg_entry_price
        target_price = avg * (1 + target_pct / 100)

        if price >= target_price:
            # Check TP confirmation indicator if configured
            if self._closes and not self.indicator_engine.check_tp_confirmation(self._closes):
                logger.info(f"TP price reached but confirmation indicator not met — holding")
                return

            pnl_btc, pnl_pct = deal.calculate_pnl(price)
            self.state.close_deal(deal.id, price, "tp")
            logger.info(f"TP hit: {deal.id} at ${price:,.2f} PnL: {pnl_btc:+.6f} BTC")
            self.notifier.notify_take_profit(
                self.config.name, deal.symbol,
                price, pnl_btc, pnl_pct
            )

    def _check_sl(self, deal: PaperDeal, price: float):
        """Check if stop loss has been triggered."""
        sl_pct = self.config.stop_loss.pct
        avg = deal.avg_entry_price
        sl_price = avg * (1 - sl_pct / 100)

        if price <= sl_price:
            pnl_btc, pnl_pct = deal.calculate_pnl(price)
            closed = self.state.close_deal(deal.id, price, "sl")
            logger.info(f"SL hit: {deal.id} at ${price:,.2f} PnL: {pnl_btc:+.6f} BTC")
            self.notifier.notify_stop_loss(
                self.config.name, deal.symbol,
                price, pnl_btc, pnl_pct
            )

    def _check_dca(self, deal: PaperDeal, price: float):
        """Check if a DCA order should be placed."""
        if deal.dca_count >= self.config.dca.max_orders - 1:
            return

        avg = deal.avg_entry_price
        spacing_pct = self.config.dca.order_spacing_pct
        next_dca_price = avg * (1 - spacing_pct / 100)

        if price <= next_dca_price:
            # Calculate DCA order size with multiplier
            multiplier = self.config.dca.multiplier ** deal.dca_count
            dca_size = round(self.config.dca.base_order_size * multiplier, 8)

            dca_order = PaperOrder(
                order_number=deal.dca_count + 2,
                price=price,
                size=dca_size,
                timestamp=datetime.utcnow(),
                order_type="dca"
            )
            deal.orders.append(dca_order)

            logger.info(f"DCA #{dca_order.order_number} placed: {deal.id} at ${price:,.2f}")
            self.notifier.notify_dca(
                self.config.name, deal.symbol,
                price, dca_size,
                dca_order.order_number,
                deal.avg_entry_price
            )
    def _update_liq_guard(self, mark_price: float):
        """Pass current open positions to the liquidation guard."""
        positions = []
        for deal in self.state.open_deals.values():
            positions.append({
                "deal_id": deal.id,
                "symbol": deal.symbol,
                "side": deal.side,
                "entry_price": deal.avg_entry_price,
                "mark_price": mark_price,
                "leverage": deal.leverage,
            })
        logger.info(f"Updating liq guard with {len(positions)} positions")  # tijdelijk
        self.liq_guard.update_positions(positions)