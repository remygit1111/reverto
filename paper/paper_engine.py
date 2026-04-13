# paper/paper_engine.py
# Simulates live trading using real market prices but virtual orders.
# Writes state to logs/{slug}.state.json after every tick so the
# web portal can read it without shared memory.

import json
import time
import logging
from datetime import datetime, UTC
from pathlib import Path
from config.models import BotConfig
from core.schedule_guard import ScheduleGuard
from exchanges.base_exchange import BaseExchange
from notifications.telegram import TelegramNotifier
from paper.paper_state import PaperState, PaperDeal, PaperOrder
from strategies.indicator_engine import IndicatorEngine
from core.liquidation_guard import LiquidationGuard

logger = logging.getLogger(__name__)


def _deal_to_dict(deal: PaperDeal, current_price: float = 0.0) -> dict:
    """Convert a PaperDeal to a JSON-serialisable dict."""
    pnl_btc, pnl_pct = deal.calculate_pnl(current_price) if current_price else (0.0, 0.0)
    return {
        "id":              deal.id,
        "symbol":          deal.symbol,
        "side":            deal.side,
        "leverage":        deal.leverage,
        "order_count":     len(deal.orders),
        "entry_price":     round(deal.orders[0].price, 2) if deal.orders else 0.0,
        "avg_entry_price": round(deal.avg_entry_price, 2),
        "total_size":      deal.total_size,
        "pnl_btc":         round(pnl_btc, 8),
        "pnl_pct":         round(pnl_pct, 4),
        "opened_at":       deal.opened_at.isoformat() if deal.opened_at else None,
        "closed_at":       deal.closed_at.isoformat() if deal.closed_at else None,
        "close_price":     deal.close_price,
        "close_reason":    deal.close_reason,
        "is_open":         deal.is_open,
        "_peak_price":     deal._peak_price,  # persist trailing stop peak
    }


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
        poll_interval: int = 10,
        state_file: str = None,
    ):
        self.config           = config
        self.exchange         = exchange
        self.notifier         = notifier
        self.state            = PaperState(initial_balance_btc)
        self.guard            = ScheduleGuard(config.schedule)
        self.indicator_engine = IndicatorEngine(config)
        self.liq_guard        = LiquidationGuard(config, notifier)
        self.poll_interval    = poll_interval
        self._current_price: float = 0.0
        self._last_snapshot: dict  = {}

        # State file for portal communication
        self._state_file = Path(state_file) if state_file else None

        # Rolling window of closing prices
        self._closes: list[float] = []
        self._closes_fetched_at: float = 0.0

        # Track schedule state for transition detection
        self._last_schedule_state: bool = None

        self.running = False
        self._started_at: datetime = None

    # ------------------------------------------------------------------
    # State file — written after every tick for portal to read
    # ------------------------------------------------------------------

    def _write_state(self, price: float, is_open: bool):
        """Write current engine state to JSON file for the web portal."""
        if not self._state_file:
            return

        summary = self.state.summary()

        # Use snapshot to avoid holding the state lock while building JSON
        open_deals_snap  = self.state.get_open_deals_snapshot()
        closed_deals_snap = list(self.state.closed_deals)

        snapshot = {
            "bot_name":            self.config.name,
            "mode":                self.config.mode.value,
            "exchange":            self.config.exchange.value,
            "pair":                self.config.pair,
            "running":             True,
            "current_price":       price,
            "schedule_open":       is_open,
            "balance_btc":         summary["balance_btc"],
            "initial_balance_btc": self.state.initial_balance_btc,
            "total_pnl_btc":       summary["total_pnl_btc"],
            "win_rate":            summary["win_rate"],
            "open_deals_count":    len(open_deals_snap),
            "closed_deals_count":  summary["closed_deals"],
            "started_at":          self._started_at.isoformat() if self._started_at else None,
            "updated_at":          datetime.now(UTC).isoformat(),
            "open_deals": [
                _deal_to_dict(d, price)
                for d in open_deals_snap.values()
            ],
            "closed_deals": [
                _deal_to_dict(d)
                for d in list(reversed(closed_deals_snap))[:50]
            ],
            "indicators": self._last_snapshot,
        }

        try:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            tmp.replace(self._state_file)  # atomic write
        except Exception as e:
            logger.debug(f"State write failed: {e}")

    def _clear_state(self):
        """
        Mark bot as stopped in the state file.
        Uses atomic tmp+replace to prevent JSON corruption on SIGKILL.
        """
        if not self._state_file:
            return
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
            else:
                data = {}
            data["running"]       = False
            data["current_price"] = 0.0
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self._state_file)  # atomic
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def start(self):
        """Start the paper engine loop."""
        logger.info(f"Starting paper engine: {self.config.name}")
        self._started_at = datetime.now()
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
        self._clear_state()
        summary = self.state.summary()
        logger.info(f"Paper engine stopped. Summary: {summary}")
        self.notifier.notify_shutdown(self.config.name)

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def _tick(self):
        """Single iteration of the main loop."""
        try:
            ticker = self.exchange.get_ticker(self.config.pair)

            # Prefer mark price — matches real exchange TP/SL behaviour
            if ticker.mark_price is not None:
                price = float(ticker.mark_price)
            else:
                price = ticker.last
                logger.debug("mark_price unavailable — falling back to ticker.last")

            self._current_price = price
            logger.debug(f"{self.config.pair} price: ${price:,.2f}")

            is_open = self.guard.is_open()

            self._check_schedule_transition(is_open)
            self._fetch_closes_if_needed()

            # Always update indicator snapshot (regardless of schedule)
            # so the dashboard shows current values during off-hours.
            if self._closes:
                self._last_snapshot = self.indicator_engine.get_indicator_snapshot(
                    self._closes
                )

            self._monitor_open_deals(price)
            self._update_liq_guard(price)

            if is_open:
                self._check_entry(price)

            # Write state for portal after every tick
            self._write_state(price, is_open)

        except Exception as e:
            logger.error(f"Tick error: {e}")
            self.notifier.notify_error(self.config.name, str(e))

    # ------------------------------------------------------------------
    # Schedule transition detection
    # ------------------------------------------------------------------

    def _check_schedule_transition(self, is_open: bool):
        """Detect and notify when trading window opens or closes."""
        if self._last_schedule_state is None:
            self._last_schedule_state = is_open
            return

        if is_open and not self._last_schedule_state:
            status = self.guard.status(is_open=is_open)
            self.notifier.notify_schedule_open(
                self.config.name,
                status.get("next_open", "—")
            )
        elif not is_open and self._last_schedule_state:
            status = self.guard.status(is_open=is_open)
            self.notifier.notify_schedule_close(
                self.config.name,
                status.get("next_open", "—"),
                len(self.state.get_open_deals_snapshot())
            )

        self._last_schedule_state = is_open

    # ------------------------------------------------------------------
    # Candle data
    # ------------------------------------------------------------------

    def _fetch_closes_if_needed(self):
        """
        Fetch OHLCV candles only when stale.
        Excludes the current incomplete candle.
        On failure, schedules a retry in ~100s instead of waiting the
        full 3600s cache window.
        """
        if time.time() - self._closes_fetched_at < 3600:
            return

        try:
            candles   = self.exchange.get_ohlcv(self.config.pair, "1h", 101)
            now_ms    = time.time() * 1000
            completed = [c for c in candles if c[0] + 3_600_000 < now_ms]

            if not completed:
                logger.warning("No completed candles — skipping cache update")
                # Retry in 100s, not 3600s
                self._closes_fetched_at = time.time() - 3500
                return

            self._closes            = [c[4] for c in completed]
            self._closes_fetched_at = time.time()
            logger.debug(f"Candles refreshed: {len(self._closes)} closes loaded")

        except Exception as e:
            logger.error(f"Failed to fetch candles: {e}")
            # Retry in 100s on failure
            self._closes_fetched_at = time.time() - 3500

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def _check_entry(self, price: float):
        """Check if conditions are met to start a new deal."""
        # Use snapshot to avoid holding the lock during the check
        if len(self.state.get_open_deals_snapshot()) > 0:
            return

        if not self._closes:
            logger.warning("No candle data — skipping entry check")
            return

        logger.info(
            f"Indicators — RSI: {self._last_snapshot.get('rsi_14','?')} | "
            f"EMA9: {self._last_snapshot.get('ema_9','?')} | "
            f"EMA21: {self._last_snapshot.get('ema_21','?')} | "
            f"MACD hist: {self._last_snapshot.get('macd_histogram','?')}"
        )

        if self.indicator_engine.check_entry_signal(self._closes):
            self._open_deal(price)

    def _open_deal(self, price: float):
        """Open a new paper deal at the current price."""
        deal_id    = self.state.new_deal_id()
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
    # Deal monitoring — uses snapshot for safe iteration
    # ------------------------------------------------------------------

    def _monitor_open_deals(self, price: float):
        """Monitor all open deals for DCA, TP and SL conditions."""
        # Use snapshot so we iterate a stable copy while the state lock
        # is only held briefly during the snapshot, not the full loop.
        snapshot = self.state.get_open_deals_snapshot()
        for deal_id, deal in snapshot.items():
            self._check_tp(deal, price)
            if deal_id not in self.state.open_deals:
                continue
            self._check_sl(deal, price)
            if deal_id not in self.state.open_deals:
                continue
            self._check_dca(deal, price)

    def _check_tp(self, deal: PaperDeal, price: float):
        """Check if take profit target has been reached."""
        avg          = deal.avg_entry_price
        target_price = avg * (1 + self.config.take_profit.target_pct / 100)

        if price >= target_price:
            if self._closes and not self.indicator_engine.check_tp_confirmation(self._closes):
                logger.info("TP price reached but confirmation not met — holding")
                return

            pnl_btc, pnl_pct = deal.calculate_pnl(price)
            self.state.close_deal(deal.id, price, "tp")
            logger.info(f"TP hit: {deal.id} at ${price:,.2f} PnL: {pnl_btc:+.6f} BTC")
            self.notifier.notify_take_profit(
                self.config.name, deal.symbol, price, pnl_btc, pnl_pct
            )

    def _check_sl(self, deal: PaperDeal, price: float):
        """
        Check if stop loss has been triggered (fixed or trailing).
        _peak_price is stored on PaperDeal so it persists across ticks
        and is included in the state JSON for restart recovery.
        """
        sl_pct = self.config.stop_loss.pct

        if self.config.stop_loss.type == "trailing":
            # Initialize peak on first tick after deal opens
            if deal._peak_price == 0.0:
                deal._peak_price = price
            deal._peak_price = max(deal._peak_price, price)
            sl_price = deal._peak_price * (1 - sl_pct / 100)
        else:
            sl_price = deal.avg_entry_price * (1 - sl_pct / 100)

        if price <= sl_price:
            pnl_btc, pnl_pct = deal.calculate_pnl(price)
            self.state.close_deal(deal.id, price, "sl")
            logger.info(
                f"SL hit ({self.config.stop_loss.type}): {deal.id} "
                f"at ${price:,.2f} PnL: {pnl_btc:+.6f} BTC"
            )
            self.notifier.notify_stop_loss(
                self.config.name, deal.symbol, price, pnl_btc, pnl_pct
            )

    def _check_dca(self, deal: PaperDeal, price: float):
        """Check if a DCA order should be placed."""
        if deal.dca_count >= self.config.dca.max_orders - 1:
            return

        last_order_price = deal.orders[-1].price
        next_dca_price   = last_order_price * (1 - self.config.dca.order_spacing_pct / 100)

        if price <= next_dca_price:
            multiplier = self.config.dca.multiplier ** deal.dca_count
            dca_size   = round(self.config.dca.base_order_size * multiplier, 8)

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
    # Liquidation guard
    # ------------------------------------------------------------------

    def _update_liq_guard(self, mark_price: float):
        """Pass current open positions to the liquidation guard."""
        positions = []
        for deal in self.state.get_open_deals_snapshot().values():
            positions.append({
                "deal_id":     deal.id,
                "symbol":      deal.symbol,
                "side":        deal.side,
                "entry_price": deal.avg_entry_price,
                "mark_price":  mark_price,
                "leverage":    deal.leverage,
            })
        self.liq_guard.update_positions(positions)
