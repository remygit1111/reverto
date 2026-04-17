# paper/paper_engine.py
# Simulates live trading using real market prices but virtual orders.
# Writes state to logs/{slug}.state.json after every tick so the
# web portal can read it without shared memory.

import json
import queue
import re
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
from core import deal_store
from core.database import init_db as _init_db

logger = logging.getLogger(__name__)

# Deal IDs produced by PaperState.new_deal_id() follow "PAPER-0001".
# Validate extracted sentinel filenames against this shape so a spoofed
# filename (e.g. "mybot.deal_close_..") cannot flow through to a dict
# lookup or downstream log. The web layer already enforces the same
# regex on inbound POSTs (web/app.py:_DEAL_ID_RE); this is defence in
# depth at the engine boundary.
_DEAL_ID_RE = re.compile(r"^[A-Z]+-\d{1,6}$")

# Ensure the SQLite schema exists before any engine instance writes to it.
# Paper bots run as subprocesses launched by the portal, so they can't rely
# on the portal's own init_db() having run in their process space.
try:
    _init_db()
except Exception as _e:  # pragma: no cover - defensive
    logger.warning("init_db failed on paper_engine import: %s", _e)

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
        "_tp_override":    deal._tp_override,
        "_sl_override":    deal._sl_override,
        "_dca_enabled":    deal._dca_enabled,
        "entry_trigger":   deal.entry_trigger,
        "exit_trigger":    deal.exit_trigger,
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
    deal._tp_override = d.get("_tp_override")
    deal._sl_override = d.get("_sl_override")
    deal._dca_enabled = d.get("_dca_enabled", True)
    et = d.get("entry_trigger")
    deal.entry_trigger = et if isinstance(et, dict) else None
    xt = d.get("exit_trigger")
    deal.exit_trigger = xt if isinstance(xt, dict) else None
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
        manual_trigger_file: str = None,
        slug: str | None = None,
    ):
        self.config           = config
        # Bot slug drives the DB ledger rows. Prefer the explicit arg from
        # main_paper.py (YAML filename stem). Fall back to deriving it from
        # the state file path, then to a sanitised config.name. When none
        # of these yield a non-empty slug we skip DB writes entirely — no
        # slug, no ledger row.
        if slug:
            self._bot_slug: str | None = slug
        elif state_file:
            stem = Path(state_file).stem
            if stem.endswith(".state"):
                stem = stem[: -len(".state")]
            self._bot_slug = stem or None
        else:
            cleaned = (config.name or "").strip().lower().replace(" ", "_")
            self._bot_slug = cleaned or None
        self.exchange         = exchange
        self.notifier         = notifier
        self.state            = PaperState(initial_balance_btc)
        self.guard            = ScheduleGuard(config.schedule)
        self.indicator_engine = IndicatorEngine(config)
        self.liq_guard        = LiquidationGuard(config, notifier)
        self.poll_interval    = poll_interval
        self._current_price: float = 0.0
        # Written to state.json as "indicators" for diagnostic inspection.
        # Not consumed by the portal UI (indicator section was removed) but
        # useful for operators inspecting the JSON file directly.
        self._last_snapshot: dict  = {}
        self._fees_paid_btc: float = 0.0

        # State file for portal communication
        self._state_file = Path(state_file) if state_file else None
        # Manual-trigger sentinel file — the web portal writes this path
        # when the operator clicks "Start Deal". The engine deletes it on
        # the next tick and opens a deal regardless of schedule / filters.
        self._manual_trigger_file = (
            Path(manual_trigger_file) if manual_trigger_file else None
        )

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

        # Wick-simulation cache: holds the most recently fetched FORMING
        # candle (high/low/close) per timeframe so TP/SL checks can fire
        # against the intra-candle range instead of only the live tick
        # price. Refreshed at most once per tick — see _refresh_wick_candle.
        self._wick_candle: dict[str, tuple[float, float, float]] = {}
        self._wick_candle_fetched_at: float = 0.0

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

    # ------------------------------------------------------------------
    # DB ledger helpers — NEVER raise; DB failures must not kill the tick.
    # ------------------------------------------------------------------

    def _db_save_deal(self, deal: PaperDeal) -> None:
        if not self._bot_slug:
            return
        try:
            deal_store.save_deal(deal, self._bot_slug, self.config.name)
        except Exception as e:
            logger.warning("deal_store.save_deal failed: %s", e)

    def _db_save_order(self, order: PaperOrder, deal_id: str, fee: float) -> None:
        if not self._bot_slug:
            return
        try:
            deal_store.save_order(order, deal_id, self._bot_slug, fee_btc=fee)
        except Exception as e:
            logger.warning("deal_store.save_order failed: %s", e)

    def _db_close_deal(
        self, deal_id: str, close_price: float, reason: str,
        pnl_btc: float, pnl_pct: float,
        exit_trigger: dict | None = None,
    ) -> None:
        if not self._bot_slug:
            return
        try:
            deal_store.close_deal(
                deal_id, close_price, reason, pnl_btc, pnl_pct,
                exit_trigger=exit_trigger,
            )
        except Exception as e:
            logger.warning("deal_store.close_deal failed: %s", e)

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
            "has_trading_windows": len(self.config.schedule.trading_windows) > 0,
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

        # JSON → SQLite migration. Existing bots predating the DB ledger
        # have history only in the state file; mirror it into SQLite on
        # first restart so the portal's /api/db endpoints see it. The
        # whole replay runs inside a single transaction via
        # deal_store.replay_deals_in_transaction so a corrupt deal in
        # the middle of the JSON rolls back the entire batch instead
        # of leaving the ledger half-migrated.
        if self._bot_slug:
            try:
                deals_to_replay = (
                    list(self.state.open_deals.values())
                    + list(self.state.closed_deals)
                )
                migrated = deal_store.replay_deals_in_transaction(
                    deals_to_replay, self._bot_slug, self.config.name,
                )
                if migrated:
                    logger.info(
                        "Migrated %d deals from %s into the DB ledger",
                        migrated, self._state_file,
                    )
            except Exception as e:
                logger.warning(
                    "JSON → DB migration failed (%s) — ledger unchanged",
                    e,
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
        logger.info(f"=== Bot started: {self.config.name} ===")
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
        logger.info(f"=== Bot stopped: {self.config.name} ===")
        self.running = False
        self.liq_guard.stop()
        self._clear_state()
        # Drop the wick-candle cache so a long-running portal process
        # that restarts engines with shifting timeframe sets never
        # accumulates stale entries. Only keep buckets that are still
        # on the engine's required-timeframe set.
        try:
            required = self.indicator_engine.required_timeframes(self.config.timeframe)
            self._wick_candle = {
                k: v for k, v in self._wick_candle.items() if k in required
            }
        except Exception:
            self._wick_candle = {}
        summary = self.state.summary()
        logger.info(f"Paper engine stopped. Summary: {summary}")
        # Legacy shutdown notification kept for back-compat. The new-style
        # notify_stop is queued by the SIGTERM handler in main_paper.py
        # BEFORE this stop() runs, so it flushes from the same drain loop
        # without being duplicated here.
        self._notify(self.notifier.notify_shutdown, self.config.name)
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
            self._refresh_wick_candle()

            # Always update indicator snapshot (regardless of schedule)
            # so the dashboard shows current values during off-hours.
            bot_tf = self.config.timeframe
            if self._closes_per_tf.get(bot_tf):
                self._last_snapshot = self.indicator_engine.get_indicator_snapshot(
                    self._closes_per_tf, bot_tf,
                )

            self._monitor_open_deals(price)
            self._update_liq_guard(price)

            # Manual trigger: the portal writes a sentinel file to force
            # an immediate deal open, bypassing schedule + indicators.
            self._check_manual_trigger(price)
            self._check_deal_sentinels(price)

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

    def _refresh_wick_candle(self):
        """Pull the current FORMING candle's high/low/close for every
        bucket the engine cares about (today: the bot's configured
        timeframe). Cached for `min(poll_interval*2, 30s)` so we never
        spam the exchange — backfilled candles change at most once per
        tick anyway. No-op when use_wick_simulation is disabled, so
        spot bots and the existing tick-only logic stay free of extra
        REST calls.
        """
        if not getattr(self.config, "use_wick_simulation", True):
            return
        now = time.time()
        # 5-second TTL by default — enough to dedupe rapid ticks but
        # short enough that a freshly broken wick lands within ~1 tick
        # of the real exchange event.
        ttl = max(5.0, min(self.poll_interval * 2.0, 30.0))
        if now - self._wick_candle_fetched_at < ttl:
            return
        tf = self.config.timeframe
        try:
            candles = self.exchange.get_ohlcv(self.config.pair, tf, 1)
        except Exception as e:
            logger.debug("wick candle fetch failed (%s) — falling back to tick", e)
            return
        if not candles:
            return
        c = candles[-1]
        try:
            self._wick_candle[tf] = (float(c[2]), float(c[3]), float(c[4]))
        except (IndexError, ValueError, TypeError):
            return
        self._wick_candle_fetched_at = now

    def _wick_high_low(self, tick_price: float) -> tuple[float, float]:
        """Return (high, low) for TP/SL evaluation. When wick simulation
        is enabled and we have a fresh forming candle, return its
        high/low — otherwise fall back to (tick_price, tick_price) so
        every existing call site keeps working with no behaviour
        change. The candle's own high/low already incorporates the
        latest tick on most exchanges, but we still take max/min with
        the live price as a defensive guard."""
        if not getattr(self.config, "use_wick_simulation", True):
            return tick_price, tick_price
        candle = self._wick_candle.get(self.config.timeframe)
        if not candle:
            return tick_price, tick_price
        high, low, _close = candle
        return max(high, tick_price), min(low, tick_price)

    # ------------------------------------------------------------------
    # Entry logic
    # ------------------------------------------------------------------

    def _check_manual_trigger(self, price: float):
        """If the manual-trigger sentinel exists, consume it and open a
        deal. By design this bypasses the schedule guard and indicator
        filters — the operator is taking explicit responsibility. We do
        still gate on the existing single-open-deal rule and on a
        liquidation pre-flight check, because opening a leveraged deal
        within the emergency-distance band would immediately cascade
        into an emergency-close on the next tick."""
        trigger = self._manual_trigger_file
        if trigger is None or not trigger.exists():
            return
        try:
            trigger.unlink()
        except OSError:
            pass
        if len(self.state.get_open_deals_snapshot()) > 0:
            logger.warning("manual trigger ignored: deal already open")
            return
        if not self._manual_trigger_liq_safe(price):
            return
        logger.info("=== Manual deal trigger fired ===")
        self._open_deal(price)

    def _manual_trigger_liq_safe(self, price: float) -> bool:
        """Return True if opening a new deal at `price` would land safely
        outside the liquidation guard's emergency band. Spot bots
        (leverage <= 1) are always safe. Returns False (and logs a
        warning) when the position would open inside the emergency
        distance — the trigger file is consumed either way so a stale
        sentinel doesn't keep retrying."""
        leverage = self.config.leverage.size
        if leverage <= 1 or not self.config.leverage.enabled:
            return True
        try:
            from core.liquidation_guard import calculate_liquidation_price
            liq_price = calculate_liquidation_price(price, leverage, "long")
        except Exception as e:
            logger.warning("manual trigger: liq calc failed (%s) — refusing", e)
            return False
        if liq_price <= 0 or price <= 0:
            return False
        distance_pct = abs(price - liq_price) / price * 100
        emergency_pct = self.config.leverage.liquidation_guard.emergency_close_pct
        if distance_pct <= emergency_pct:
            logger.warning(
                "manual trigger refused: opening at $%.2f would be %.2f%% "
                "from liquidation $%.2f (emergency band %.2f%%)",
                price, distance_pct, liq_price, emergency_pct,
            )
            self._notify(
                self.notifier.notify_error,
                self.config.name,
                f"Manual trigger refused — entry within {emergency_pct}% of liquidation",
            )
            return False
        return True

    def _check_deal_sentinels(self, price: float):
        """Consume deal_edit / deal_cancel / deal_close sentinel files.

        The portal writes these to logs/{slug}.deal_{action}_{deal_id}
        and the engine picks them up on the next tick. This is the same
        fire-and-forget pattern as the manual_trigger sentinel.
        """
        slug = self.config.name.lower().replace(" ", "_")
        log_dir = Path("logs")
        if not log_dir.exists():
            return
        for sentinel in log_dir.glob(f"{slug}.deal_*"):
            try:
                parts = sentinel.name.split(".", 1)[1].split("_", 2)
                if len(parts) < 3:
                    sentinel.unlink(missing_ok=True)
                    continue
                action = parts[1]   # edit / cancel / close
                deal_id = parts[2]  # e.g. PAPER-0001
                # Reject malformed deal IDs before touching engine state.
                # glob already constrains the filename prefix, but the
                # last segment is still free-form bytes from whoever
                # wrote the sentinel — refuse anything that doesn't look
                # like a PaperState-minted ID.
                if not _DEAL_ID_RE.match(deal_id):
                    logger.warning(
                        "Deal sentinel has invalid deal_id %r — discarding %s",
                        deal_id, sentinel.name,
                    )
                    sentinel.unlink(missing_ok=True)
                    continue
                payload = sentinel.read_text(encoding="utf-8").strip()
                sentinel.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Failed to read deal sentinel %s: %s", sentinel, e)
                continue

            deal = self.state.open_deals.get(deal_id)
            if not deal:
                logger.warning("Deal sentinel for %s but deal not open", deal_id)
                continue

            if action == "edit":
                import json as _json
                try:
                    settings = _json.loads(payload) if payload else {}
                except Exception:
                    settings = {}
                if "tp_override" in settings:
                    deal._tp_override = settings["tp_override"]
                if "sl_override" in settings:
                    deal._sl_override = settings["sl_override"]
                if "dca_enabled" in settings:
                    deal._dca_enabled = bool(settings["dca_enabled"])
                logger.info("Deal %s settings updated via portal", deal_id)

            elif action == "cancel":
                # Cancel removes the deal from tracking without realising
                # PnL — the exchange position stays open and the operator
                # is responsible for managing it manually. state.close_deal
                # computes and applies PnL internally (updating balance),
                # but we record 0.0 in the DB ledger because the gain/loss
                # has not been crystallised through an actual exit trade.
                exit_trigger = {"type": "cancelled"}
                deal.exit_trigger = exit_trigger
                self.state.close_deal(deal_id, price, "cancelled")
                self._db_close_deal(
                    deal_id, price, "cancelled", 0.0, 0.0,
                    exit_trigger=exit_trigger,
                )
                logger.info("Deal %s cancelled via portal", deal_id)
                self._notify(
                    self.notifier.notify_stop_loss,
                    self.config.name, deal.symbol, price, 0.0, 0.0,
                )

            elif action == "close":
                pnl_btc, pnl_pct = deal.calculate_pnl(price)
                exit_size = deal.total_size
                exit_trigger = {"type": "manual"}
                deal.exit_trigger = exit_trigger
                self.state.close_deal(deal_id, price, "manual")
                fee = self._calc_fee(exit_size)
                self.state.balance_btc -= fee
                self._fees_paid_btc += fee
                self._db_close_deal(
                    deal_id, price, "manual", pnl_btc, pnl_pct,
                    exit_trigger=exit_trigger,
                )
                logger.info(
                    "Deal %s manually closed at $%.2f PnL: %+.6f BTC (fee %.8f)",
                    deal_id, price, pnl_btc, fee,
                )
                self._notify(
                    self.notifier.notify_take_profit,
                    self.config.name, deal.symbol, price, pnl_btc, pnl_pct,
                )
            else:
                logger.warning("Unknown deal sentinel action: %s", action)

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

        triggered, trigger_info = self.indicator_engine.check_entry_signal(
            self._closes_per_tf, bot_tf,
            highs_per_tf=self._highs_per_tf,
            lows_per_tf=self._lows_per_tf,
        )
        if triggered:
            self._open_deal(price, entry_trigger=trigger_info)

    def _calc_fee(self, size: float) -> float:
        """Bereken de taker fee voor één order (in BTC, inverse contract)."""
        return size * self.config.dca.taker_fee

    def _open_deal(self, price: float, entry_trigger: dict | None = None):
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
            orders=[base_order],
            entry_trigger=entry_trigger if isinstance(entry_trigger, dict) else None,
        )

        self.state.open_deal(deal)
        fee = self._calc_fee(base_order.size)
        self.state.balance_btc -= fee
        self._fees_paid_btc    += fee
        self._db_save_deal(deal)
        self._db_save_order(base_order, deal.id, fee)
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
        """Check if take profit target has been reached.

        Skipped entirely when take_profit.enabled is False so the deal
        can only close through SL or manual intervention.

        With wick simulation enabled the trigger fires when the FORMING
        candle's high reaches the target, even if the live tick hasn't
        printed there yet. The fill price is still capped at the
        target (we don't simulate slippage past the line) so realised
        PnL stays consistent with the user's TP percentage.
        """
        tp_ov = deal._tp_override
        if tp_ov is not None:
            if not tp_ov.get("enabled", True):
                return
            tp_pct = tp_ov.get("target_pct", self.config.take_profit.target_pct)
        else:
            if not getattr(self.config.take_profit, 'enabled', True):
                return
            tp_pct = self.config.take_profit.target_pct

        avg          = deal.avg_entry_price
        target_price = avg * (1 + tp_pct / 100)

        wick_high, _ = self._wick_high_low(price)
        wick_hit = wick_high >= target_price
        tick_hit = price >= target_price

        if wick_hit or tick_hit:
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

            # Wick-only fills cap the simulated exit at the target_price
            # (no slippage past the line). Tick fills use the live price.
            fill_price = target_price if (wick_hit and not tick_hit) else price
            pnl_btc, pnl_pct = deal.calculate_pnl(fill_price)
            exit_size = deal.total_size
            exit_trigger = {"type": "price_tp"}
            deal.exit_trigger = exit_trigger
            self.state.close_deal(deal.id, fill_price, "tp")
            fee = self._calc_fee(exit_size)
            self.state.balance_btc -= fee
            self._fees_paid_btc    += fee
            self._db_close_deal(
                deal.id, fill_price, "tp", pnl_btc, pnl_pct,
                exit_trigger=exit_trigger,
            )
            if wick_hit and not tick_hit:
                logger.info(
                    f"TP hit (wick): {deal.id} at ${fill_price:,.2f} "
                    f"(wick high: ${wick_high:,.2f}) "
                    f"PnL: {pnl_btc:+.6f} BTC (fee {fee:.8f})"
                )
            else:
                logger.info(
                    f"TP hit: {deal.id} at ${fill_price:,.2f} "
                    f"PnL: {pnl_btc:+.6f} BTC (fee {fee:.8f})"
                )
            self._notify(
                self.notifier.notify_take_profit,
                self.config.name, deal.symbol, fill_price, pnl_btc, pnl_pct,
            )
            return

        # TP indicator groups — trigger TP even if price hasn't hit target
        tp_groups = getattr(self.config.take_profit, 'indicator_groups', [])
        if tp_groups:
            bot_tf = self.config.timeframe
            tp_hit, tp_info = self.indicator_engine.check_tp_indicator_groups(
                self._closes_per_tf, bot_tf,
                highs_per_tf=self._highs_per_tf,
                lows_per_tf=self._lows_per_tf,
            )
            if tp_hit:
                pnl_btc, pnl_pct = deal.calculate_pnl(price)
                exit_size = deal.total_size
                exit_trigger = {
                    "type": "indicator_tp",
                    "group_name": (tp_info or {}).get("group_name", ""),
                    "indicators": (tp_info or {}).get("indicators", []),
                }
                deal.exit_trigger = exit_trigger
                self.state.close_deal(deal.id, price, "tp")
                fee = self._calc_fee(exit_size)
                self.state.balance_btc -= fee
                self._fees_paid_btc += fee
                self._db_close_deal(
                    deal.id, price, "tp", pnl_btc, pnl_pct,
                    exit_trigger=exit_trigger,
                )
                logger.info(
                    f"TP hit (indicator group): {deal.id} at ${price:,.2f} "
                    f"PnL: {pnl_btc:+.6f} BTC")
                self._notify(
                    self.notifier.notify_take_profit,
                    self.config.name, deal.symbol, price, pnl_btc, pnl_pct,
                )

    def _check_sl(self, deal: PaperDeal, price: float):
        """
        Check if stop loss has been triggered (fixed, trailing, or none).
        _peak_price is stored on PaperDeal so it persists across ticks
        and is included in the state JSON for restart recovery.

        With wick simulation enabled both the trigger AND the trailing
        peak use the FORMING candle's high/low: this matches the
        backtest engine which reads candle.high/low directly. Without
        the wick the trailing peak only saw the 10-second tick price
        and would lag the real high by however much the wick exceeded
        the next sampled tick — a measurable bias that made paper and
        backtest results diverge.
        """
        sl_ov = deal._sl_override
        if sl_ov is not None:
            if not sl_ov.get("enabled", True):
                return
            sl_type = sl_ov.get("type", self.config.stop_loss.type)
            sl_pct = sl_ov.get("pct", self.config.stop_loss.pct)
        else:
            if self.config.stop_loss.type == "none":
                return
            sl_type = self.config.stop_loss.type
            sl_pct = self.config.stop_loss.pct

        wick_high, wick_low = self._wick_high_low(price)

        if sl_type == "trailing":
            # Initialize peak on first tick after deal opens.
            if deal._peak_price == 0.0:
                deal._peak_price = price
            new_peak = max(deal._peak_price, wick_high)
            if wick_high > price and new_peak > deal._peak_price:
                logger.debug(
                    "Trailing peak updated via wick: $%.2f (tick: $%.2f)",
                    wick_high, price,
                )
            deal._peak_price = new_peak
            sl_price = deal._peak_price * (1 - sl_pct / 100)
        else:
            sl_price = deal.avg_entry_price * (1 - sl_pct / 100)

        wick_hit = wick_low <= sl_price
        tick_hit = price <= sl_price

        if wick_hit or tick_hit:
            # Same fill semantics as TP: wick-only fills cap at the SL
            # line (no extra slippage), tick fills use the live price.
            fill_price = sl_price if (wick_hit and not tick_hit) else price
            pnl_btc, pnl_pct = deal.calculate_pnl(fill_price)
            exit_size = deal.total_size
            exit_trigger = {
                "type": "trailing_sl" if sl_type == "trailing" else "price_sl",
            }
            deal.exit_trigger = exit_trigger
            self.state.close_deal(deal.id, fill_price, "sl")
            fee = self._calc_fee(exit_size)
            self.state.balance_btc -= fee
            self._fees_paid_btc    += fee
            self._db_close_deal(
                deal.id, fill_price, "sl", pnl_btc, pnl_pct,
                exit_trigger=exit_trigger,
            )
            if wick_hit and not tick_hit:
                logger.info(
                    f"SL hit ({sl_type}, wick): {deal.id} "
                    f"at ${fill_price:,.2f} (wick low: ${wick_low:,.2f}) "
                    f"PnL: {pnl_btc:+.6f} BTC (fee {fee:.8f})"
                )
            else:
                logger.info(
                    f"SL hit ({sl_type}): {deal.id} "
                    f"at ${fill_price:,.2f} PnL: {pnl_btc:+.6f} BTC (fee {fee:.8f})"
                )
            self._notify(
                self.notifier.notify_stop_loss,
                self.config.name, deal.symbol, fill_price, pnl_btc, pnl_pct,
            )

    def _check_dca(self, deal: PaperDeal, price: float):
        """Check if a DCA order should be placed."""
        if not getattr(self.config.dca, 'enabled', True):
            return
        if not deal._dca_enabled:
            return
        # max_orders=0 means "base order only, never DCA".
        if self.config.dca.max_orders <= 1:
            return
        if deal.dca_count >= self.config.dca.max_orders - 1:
            return

        last_order_price = deal.orders[-1].price
        step = self.config.dca.order_spacing_pct * (
            self.config.dca.step_scale ** deal.dca_count
        )
        next_dca_price   = last_order_price * (1 - step / 100)

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
            self._db_save_order(dca_order, deal.id, fee)
            self._db_save_deal(deal)

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
