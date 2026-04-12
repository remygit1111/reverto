# paper/paper_engine.py
# Simulates live trading using real market prices but virtual orders.
# Uses the same logic as the live engine — only order execution differs.

import time
import logging
from datetime import datetime, UTC
from config.models import BotConfig
from core.schedule_guard import ScheduleGuard
from exchanges.base_exchange import BaseExchange
from notifications.telegram import TelegramNotifier
from paper.paper_state import PaperState, PaperDeal, PaperOrder
from strategies.indicator_engine import IndicatorEngine
from core.liquidation_guard import LiquidationGuard

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
        self.poll_interval = poll_interval

        # Rolling window of closing prices — cached to avoid fetching every tick.
        # Refreshed at most once per hour since we use 1h candles.
        self._closes: list[float] = []
        self._closes_fetched_at: float = 0.0  # 0.0 = never fetched (Unix epoch sentinel)

        # Track schedule state to detect open/close transitions
        self._last_schedule_state: bool = None

        self.running = False

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

            # Prefer mark price for TP/SL/DCA — matches real exchange behaviour.
            # Inverse perpetual liquidation and TP/SL are always based on mark price,
            # not last traded price. Fall back to last if mark price unavailable.
            if ticker.mark_price is not None:
                price = float(ticker.mark_price)
            else:
                price = ticker.last
                logger.debug("mark_price unavailable — falling back to ticker.last")

            logger.debug(f"{self.config.pair} price: ${price:,.2f}")

            # Cache is_open result — avoids calling it twice per tick
            is_open = self.guard.is_open()

            # Check schedule transitions
            self._check_schedule_transition(is_open)

            # Refresh candle cache if stale — runs every tick regardless of
            # deal state so indicator data stays current for TP confirmation
            self._fetch_closes_if_needed()

            # Monitor all open deals (DCA, TP, SL) — always runs
            self._monitor_open_deals(price)

            # Update liquidation guard with current positions
            self._update_liq_guard(price)

            # Only start new deals if schedule allows
            if is_open:
                self._check_entry(price)

        except Exception as e:
            logger.error(f"Tick error: {e}")
            self.notifier.notify_error(self.config.name, str(e))

    # ------------------------------------------------------------------
    # Schedule transition detection
    # ------------------------------------------------------------------

    def _check_schedule_transition(self, is_open: bool):
        """
        Detect and notify when trading window opens or closes.
        is_open: cached result from self.guard.is_open() — avoids re-evaluation.
        """
        if self._last_schedule_state is None:
            self._last_schedule_state = is_open
            return

        if is_open and not self._last_schedule_state:
            # Window just opened
            status = self.guard.status(is_open=is_open)
            self.notifier.notify_schedule_open(
                self.config.name,
                status.get("next_open", "—")
            )

        elif not is_open and self._last_schedule_state:
            # Window just closed
            status = self.guard.status(is_open=is_open)
            self.notifier.notify_schedule_close(
                self.config.name,
                status.get("next_open", "—"),
                len(self.state.open_deals)
            )

        self._last_schedule_state = is_open

    # ------------------------------------------------------------------
    # Candle data — refreshed in every tick, cached for 1 hour
    # ------------------------------------------------------------------

    def _fetch_closes_if_needed(self):
        """
        Fetch OHLCV candles only when the cached data is stale.
        1h candles change once per hour — no need to fetch every 10 seconds.
        Called from _tick() so candle data stays current even during open deals,
        ensuring TP confirmation indicators always use up-to-date prices.

        Note: the last candle in the response may be the current incomplete candle.
        We exclude it to avoid computing indicators on partial data.
        """
        if time.time() - self._closes_fetched_at < 3600:
            return  # Cache still fresh

        try:
            candles = self.exchange.get_ohlcv(self.config.pair, "1h", 101)

            # Exclude the last candle if it is the current incomplete candle.
            # A completed candle's close timestamp + 3600s should be in the past.
            now_ms = time.time() * 1000
            completed = [c for c in candles if c[0] + 3_600_000 < now_ms]

            if not completed:
                logger.warning("No completed candles available — skipping cache update")
                return

            self._closes = [c[4] for c in completed]
            self._closes_fetched_at = time.time()
            logger.debug(f"Candles refreshed: {len(self._closes)} completed closes loaded")
        except Exception as e:
            logger.error(f"Failed to fetch candles: {e}")

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def _check_entry(self, price: float):
        """
        Check if conditions are met to start a new deal.
        Evaluates all configured indicators against cached candle data.
        """
        # Only one open deal at a time for now
        if len(self.state.open_deals) > 0:
            return

        if not self._closes:
            logger.warning("No candle data available — skipping entry check")
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
            timestamp=datetime.now(UTC),
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
        """
        Monitor all open deals for DCA, TP and SL conditions.
        Checks deal_id still exists after each action to prevent
        acting on a deal that was just closed by a previous check.
        """
        for deal_id, deal in list(self.state.open_deals.items()):
            # TP check — may close the deal
            self._check_tp(deal, price)

            # Verify deal still open before SL check
            if deal_id not in self.state.open_deals:
                continue

            # SL check — may close the deal
            self._check_sl(deal, price)

            # Verify deal still open before DCA check
            if deal_id not in self.state.open_deals:
                continue

            # DCA check — never closes, only adds orders
            self._check_dca(deal, price)

    def _check_tp(self, deal: PaperDeal, price: float):
        """Check if take profit target has been reached."""
        target_pct = self.config.take_profit.target_pct
        avg = deal.avg_entry_price
        target_price = avg * (1 + target_pct / 100)

        if price >= target_price:
            # Check TP confirmation indicator if configured
            if self._closes and not self.indicator_engine.check_tp_confirmation(self._closes):
                logger.info("TP price reached but confirmation indicator not met — holding")
                return

            pnl_btc, pnl_pct = deal.calculate_pnl(price)
            self.state.close_deal(deal.id, price, "tp")
            logger.info(f"TP hit: {deal.id} at ${price:,.2f} PnL: {pnl_btc:+.6f} BTC")
            self.notifier.notify_take_profit(
                self.config.name, deal.symbol,
                price, pnl_btc, pnl_pct
            )

    def _check_sl(self, deal: PaperDeal, price: float):
        """
        Check if stop loss has been triggered.
        Supports both fixed and trailing stop loss modes as configured in YAML.

        Fixed:    sl_price = avg_entry * (1 - pct/100)
        Trailing: sl_price tracks the highest price seen since deal opened,
                  sl fires when price drops pct% below that peak.
        """
        sl_pct = self.config.stop_loss.pct

        if self.config.stop_loss.type == "trailing":
            # Track highest price seen since deal opened
            if not hasattr(deal, "_peak_price"):
                deal._peak_price = price
            deal._peak_price = max(deal._peak_price, price)
            sl_price = deal._peak_price * (1 - sl_pct / 100)
        else:
            # Fixed stop loss based on average entry price
            sl_price = deal.avg_entry_price * (1 - sl_pct / 100)

        if price <= sl_price:
            pnl_btc, pnl_pct = deal.calculate_pnl(price)
            self.state.close_deal(deal.id, price, "sl")
            sl_type = self.config.stop_loss.type
            logger.info(
                f"SL hit ({sl_type}): {deal.id} at ${price:,.2f} "
                f"PnL: {pnl_btc:+.6f} BTC"
            )
            self.notifier.notify_stop_loss(
                self.config.name, deal.symbol,
                price, pnl_btc, pnl_pct
            )

    def _check_dca(self, deal: PaperDeal, price: float):
        """
        Check if a DCA order should be placed.
        Uses the last order price as the reference point — not avg entry price —
        to prevent multiple DCA orders firing on the same price drop.

        max_orders in config means total orders including the base order.
        DCA orders allowed = max_orders - 1.
        """
        if deal.dca_count >= self.config.dca.max_orders - 1:
            return

        # Base DCA trigger on LAST order price, not average entry
        last_order_price = deal.orders[-1].price
        spacing_pct = self.config.dca.order_spacing_pct
        next_dca_price = last_order_price * (1 - spacing_pct / 100)

        if price <= next_dca_price:
            # dca_count is 0-indexed before this order is appended,
            # so multiplier uses the correct exponent for the next order
            multiplier = self.config.dca.multiplier ** deal.dca_count
            dca_size = round(self.config.dca.base_order_size * multiplier, 8)

            dca_order = PaperOrder(
                order_number=deal.dca_count + 2,
                price=price,
                size=dca_size,
                timestamp=datetime.now(UTC),
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

    # ------------------------------------------------------------------
    # Liquidation guard updates
    # ------------------------------------------------------------------

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
        self.liq_guard.update_positions(positions)
