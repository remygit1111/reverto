# paper/paper_engine.py
# Simulates live trading using real market prices but virtual orders.
# Writes state to logs/{slug}.state.json after every tick so the
# web portal can read it without shared memory.

import json
import queue
import threading
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

# Candle interval in seconds per timeframe. Drives the TTL for the
# per-timeframe close-price cache — we only need to re-fetch a
# timeframe once a new completed candle of that interval has landed.
_TF_SECONDS = {
    "15m": 15 * 60,
    "1h":  60 * 60,
    "4h":  4 * 60 * 60,
    "1d":  24 * 60 * 60,
}


def _deal_to_dict(deal: PaperDeal, current_price: float = 0.0) -> dict:
    """Convert a PaperDeal to a JSON-serialisable dict."""
    # For closed deals the realised PnL has already been stamped onto the
    # deal at close_deal() time; reuse it instead of re-deriving from a
    # current_price the caller may not have. For open deals we still need
    # an unrealised PnL based on the live tick price.
    if not deal.is_open:
        pnl_btc = deal.pnl_btc
        pnl_pct = deal.pnl_pct
    elif current_price:
        pnl_btc, pnl_pct = deal.calculate_pnl(current_price)
    else:
        pnl_btc, pnl_pct = 0.0, 0.0
    return {
        "id":              deal.id,
        "bot_name":        deal.bot_name,
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
        # Full order list — required to reconstruct the deal on restart.
        # The dashboard only reads order_count, so adding this list is
        # backwards-compatible with the frontend.
        "orders": [
            {
                "order_number": o.order_number,
                "price":        o.price,
                "size":         o.size,
                "timestamp":    o.timestamp.isoformat() if o.timestamp else None,
                "order_type":   o.order_type,
            }
            for o in deal.orders
        ],
    }


def _dict_to_deal(d: dict) -> PaperDeal:
    """Reconstruct a PaperDeal (with orders) from a state-file dict.

    Used at startup to restore deals that survived a previous run. Only
    fields that affect engine logic are restored; cosmetic fields like
    rounded prices are recomputed from the order list.
    """
    def _parse_dt(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    orders = [
        PaperOrder(
            order_number=int(o.get("order_number", 1)),
            price=float(o.get("price", 0.0)),
            size=float(o.get("size", 0.0)),
            timestamp=_parse_dt(o.get("timestamp")) or datetime.now(UTC),
            order_type=str(o.get("order_type", "base")),
        )
        for o in d.get("orders", [])
    ]

    deal = PaperDeal(
        id=str(d["id"]),
        bot_name=str(d.get("bot_name", "")),
        symbol=str(d.get("symbol", "")),
        side=str(d.get("side", "long")),
        leverage=int(d.get("leverage", 1)),
        orders=orders,
        is_open=bool(d.get("is_open", True)),
        opened_at=_parse_dt(d.get("opened_at")) or datetime.now(UTC),
        closed_at=_parse_dt(d.get("closed_at")),
        close_price=d.get("close_price"),
        close_reason=d.get("close_reason"),
        pnl_btc=float(d.get("pnl_btc", 0.0)),
        pnl_pct=float(d.get("pnl_pct", 0.0)),
    )
    deal._peak_price = float(d.get("_peak_price", 0.0))
    return deal


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
        self._fees_paid_btc: float = 0.0

        # State file for portal communication
        self._state_file = Path(state_file) if state_file else None

        # Rolling window of OHLC close/high/low per timeframe. The
        # indicator engine needs separate buckets so each indicator can
        # evaluate on its configured TF. Each TF has its own
        # last-fetched timestamp so we re-fetch each bucket at most
        # once per candle interval. Highs/lows are populated alongside
        # closes for indicators that need OHLC data (Supertrend etc.).
        self._closes_per_tf: dict[str, list[float]] = {}
        self._highs_per_tf:  dict[str, list[float]] = {}
        self._lows_per_tf:   dict[str, list[float]] = {}
        self._closes_fetched_at: dict[str, float] = {}

        # Track schedule state for transition detection
        self._last_schedule_state: bool = None

        self.running = False
        self._started_at: datetime = None

        # Notification queue + worker — verhindert dat een trage Telegram
        # call de tick-loop blokkeert (TP/SL/DCA detectie kritisch op timing).
        # De worker is een daemon thread; bij stop() krijgt hij een sentinel.
        self._notify_queue: queue.Queue = queue.Queue()
        self._notify_thread = threading.Thread(
            target=self._notify_worker, daemon=True, name="reverto-notify"
        )
        self._notify_thread.start()

        # Resume from previous run if a state file already exists. Without
        # this, every restart would lose closed-deal history and abandon
        # any open deal that was mid-flight when the bot was stopped.
        self._load_state()

    def _notify(self, fn, *args, **kwargs):
        """Plaats een notificatie-call op de queue zonder te blokkeren."""
        self._notify_queue.put((fn, args, kwargs))

    def _notify_worker(self):
        """Achtergrondthread die notificaties uit de queue dispatcht."""
        while True:
            item = self._notify_queue.get()
            if item is None:  # sentinel
                self._notify_queue.task_done()
                return
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except Exception as e:
                logger.error(f"Notification failed: {type(e).__name__}: {e}")
            finally:
                self._notify_queue.task_done()

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
        closed_deals_snap = self.state.get_closed_deals_snapshot()

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
            "fees_paid_btc":       round(self._fees_paid_btc, 10),
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

    def _load_state(self):
        """Restore engine state from a previously written state file.

        Called once from __init__. If no state file exists we keep the
        clean PaperState created above (first ever startup). If one does
        exist we rehydrate balance, fees, deal counter, closed history
        and any open deal that was in flight when the bot stopped — so
        that the engine resumes monitoring it instead of silently
        forgetting it.
        """
        if not self._state_file or not self._state_file.exists():
            return

        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "State file %s exists but could not be parsed (%s) — "
                "starting clean", self._state_file, e,
            )
            return

        # Balance + fees: persisted floats survive a restart as-is.
        if "balance_btc" in data:
            try:
                self.state.balance_btc = float(data["balance_btc"])
            except (TypeError, ValueError):
                pass
        if "initial_balance_btc" in data:
            try:
                self.state.initial_balance_btc = float(data["initial_balance_btc"])
            except (TypeError, ValueError):
                pass
        if "fees_paid_btc" in data:
            try:
                self._fees_paid_btc = float(data["fees_paid_btc"])
            except (TypeError, ValueError):
                pass

        # Closed deal history.
        closed_loaded = 0
        for raw in data.get("closed_deals", []):
            try:
                self.state.closed_deals.append(_dict_to_deal(raw))
                closed_loaded += 1
            except Exception as e:
                logger.warning("Skipping unparseable closed deal: %s", e)

        # Open deals — restored straight into open_deals so the next tick
        # picks them up via _monitor_open_deals.
        open_loaded = 0
        for raw in data.get("open_deals", []):
            try:
                deal = _dict_to_deal(raw)
                self.state.open_deals[deal.id] = deal
                open_loaded += 1
            except Exception as e:
                logger.warning("Skipping unparseable open deal: %s", e)

        # Deal counter must be ahead of any restored ID so new_deal_id()
        # never collides with a resurrected one. IDs follow PAPER-XXXX.
        max_idx = 0
        for deal in list(self.state.open_deals.values()) + self.state.closed_deals:
            try:
                idx = int(deal.id.rsplit("-", 1)[-1])
                if idx > max_idx:
                    max_idx = idx
            except (ValueError, AttributeError):
                continue
        if max_idx > self.state._deal_counter:
            self.state._deal_counter = max_idx

        logger.info(
            "Resumed state from %s — balance=%.8f BTC, open=%d, closed=%d, fees=%.8f",
            self._state_file, self.state.balance_btc,
            open_loaded, closed_loaded, self._fees_paid_btc,
        )

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

        self._notify(
            self.notifier.notify_startup,
            self.config.name,
            self.config.mode.value,
            self.config.exchange.value,
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
        # Stuur shutdown + stop notificaties en wacht tot de queue leeg is,
        # anders mist de laatste boodschap omdat de daemon thread direct
        # mee sterft. notify_shutdown blijft bestaan voor back-compat;
        # notify_stop is het nieuwe event analoog aan notify_startup.
        self._notify(self.notifier.notify_shutdown, self.config.name)
        self._notify(
            self.notifier.notify_stop,
            self.config.name,
            self.config.mode.value,
            self.config.exchange.value,
        )
        self._notify_queue.put(None)  # sentinel
        self._notify_thread.join(timeout=15)

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
            bot_tf = self.config.timeframe
            if self._closes_per_tf.get(bot_tf):
                self._last_snapshot = self.indicator_engine.get_indicator_snapshot(
                    self._closes_per_tf, bot_tf,
                )

            self._monitor_open_deals(price)
            self._update_liq_guard(price)

            if is_open:
                self._check_entry(price)

            # Write state for portal after every tick
            self._write_state(price, is_open)

        except Exception as e:
            logger.error(f"Tick error: {e}")
            self._notify(self.notifier.notify_error, self.config.name, str(e))

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
            self._notify(
                self.notifier.notify_schedule_open,
                self.config.name,
                status.get("next_open", "—"),
            )
        elif not is_open and self._last_schedule_state:
            status = self.guard.status(is_open=is_open)
            self._notify(
                self.notifier.notify_schedule_close,
                self.config.name,
                status.get("next_open", "—"),
                len(self.state.get_open_deals_snapshot()),
            )

        self._last_schedule_state = is_open

    # ------------------------------------------------------------------
    # Candle data
    # ------------------------------------------------------------------

    def _fetch_closes_if_needed(self):
        """
        Fetch OHLCV candles per required timeframe, only when stale.

        Required timeframes come from the IndicatorEngine: the bot-level
        default plus every per-indicator override. Each timeframe has
        its own TTL (matching the candle interval) so e.g. a 1d bucket
        is only re-fetched once per day while a 15m bucket is refreshed
        every 15 minutes. The current incomplete candle is excluded so
        indicator inputs match what the exchange would have shown at
        the previous closed candle.
        """
        required = self.indicator_engine.required_timeframes(self.config.timeframe)
        now = time.time()

        for tf in required:
            interval = _TF_SECONDS.get(tf)
            if interval is None:
                logger.warning("Unknown timeframe %r — skipping fetch", tf)
                continue

            last = self._closes_fetched_at.get(tf, 0.0)
            if now - last < interval:
                continue

            try:
                candles = self.exchange.get_ohlcv(self.config.pair, tf, 101)
                now_ms = time.time() * 1000
                cutoff_ms = interval * 1000
                completed = [c for c in candles if c[0] + cutoff_ms < now_ms]

                if not completed:
                    logger.warning(
                        "No completed %s candles — retry in 100s", tf
                    )
                    # Retry in 100s instead of waiting a full interval
                    self._closes_fetched_at[tf] = now - max(interval - 100, 0)
                    continue

                self._closes_per_tf[tf] = [c[4] for c in completed]
                self._highs_per_tf[tf]  = [c[2] for c in completed]
                self._lows_per_tf[tf]   = [c[3] for c in completed]
                self._closes_fetched_at[tf] = now
                logger.debug(
                    "Candles refreshed: tf=%s count=%d", tf, len(completed)
                )

            except Exception as e:
                logger.error("Failed to fetch %s candles: %s", tf, e)
                self._closes_fetched_at[tf] = now - max(interval - 100, 0)

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def _check_entry(self, price: float):
        """Check if conditions are met to start a new deal."""
        # Use snapshot to avoid holding the lock during the check
        if len(self.state.get_open_deals_snapshot()) > 0:
            return

        bot_tf = self.config.timeframe
        if not self._closes_per_tf.get(bot_tf):
            logger.warning("No candle data for bot timeframe %s — skipping entry", bot_tf)
            return

        logger.info(
            f"Indicators — RSI: {self._last_snapshot.get('rsi_14','?')} | "
            f"EMA9: {self._last_snapshot.get('ema_9','?')} | "
            f"EMA21: {self._last_snapshot.get('ema_21','?')} | "
            f"MACD hist: {self._last_snapshot.get('macd_histogram','?')}"
        )

        if self.indicator_engine.check_entry_signal(
            self._closes_per_tf, bot_tf,
            highs_per_tf=self._highs_per_tf,
            lows_per_tf=self._lows_per_tf,
        ):
            self._open_deal(price)

    def _calc_fee(self, size: float) -> float:
        """Bereken de taker fee voor één order (in BTC, inverse contract)."""
        return size * self.config.dca.taker_fee

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
        fee = self._calc_fee(base_order.size)
        self.state.balance_btc -= fee
        self._fees_paid_btc    += fee
        logger.info(f"Deal opened: {deal_id} at ${price:,.2f} (fee {fee:.8f} BTC)")

        self._notify(
            self.notifier.notify_entry,
            self.config.name,
            self.config.pair,
            price,
            base_order.size,
            self.config.leverage.size,
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
            bot_tf = self.config.timeframe
            # Optional minimum-profit gate: if configured, the indicator
            # confirmation path can only fire once the deal is at least
            # X% in profit. Prevents an indicator from closing a deal
            # that's barely above break-even.
            min_tp = self.config.take_profit.minimum_tp_pct
            if min_tp is not None:
                _, pnl_pct_now = deal.calculate_pnl(price)
                if pnl_pct_now < min_tp:
                    logger.info(
                        "TP price reached but pnl %.2f%% < minimum_tp_pct %.2f%% — holding",
                        pnl_pct_now, min_tp,
                    )
                    return
            if self._closes_per_tf.get(bot_tf) and not self.indicator_engine.check_tp_confirmation(
                self._closes_per_tf, bot_tf
            ):
                logger.info("TP price reached but confirmation not met — holding")
                return

            pnl_btc, pnl_pct = deal.calculate_pnl(price)
            exit_size = deal.total_size
            self.state.close_deal(deal.id, price, "tp")
            fee = self._calc_fee(exit_size)
            self.state.balance_btc -= fee
            self._fees_paid_btc    += fee
            logger.info(
                f"TP hit: {deal.id} at ${price:,.2f} "
                f"PnL: {pnl_btc:+.6f} BTC (fee {fee:.8f})"
            )
            self._notify(
                self.notifier.notify_take_profit,
                self.config.name, deal.symbol, price, pnl_btc, pnl_pct,
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
            exit_size = deal.total_size
            self.state.close_deal(deal.id, price, "sl")
            fee = self._calc_fee(exit_size)
            self.state.balance_btc -= fee
            self._fees_paid_btc    += fee
            logger.info(
                f"SL hit ({self.config.stop_loss.type}): {deal.id} "
                f"at ${price:,.2f} PnL: {pnl_btc:+.6f} BTC (fee {fee:.8f})"
            )
            self._notify(
                self.notifier.notify_stop_loss,
                self.config.name, deal.symbol, price, pnl_btc, pnl_pct,
            )

    def _check_dca(self, deal: PaperDeal, price: float):
        """Check if a DCA order should be placed."""
        # max_orders=0 means "base order only, never DCA".
        if self.config.dca.max_orders <= 1:
            return
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
            fee = self._calc_fee(dca_size)
            self.state.balance_btc -= fee
            self._fees_paid_btc    += fee

            logger.info(
                f"DCA #{dca_order.order_number} placed: {deal.id} "
                f"at ${price:,.2f} (fee {fee:.8f})"
            )
            self._notify(
                self.notifier.notify_dca,
                self.config.name, deal.symbol,
                price, dca_size,
                dca_order.order_number,
                deal.avg_entry_price,
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
