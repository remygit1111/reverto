# paper/paper_engine.py
# Simulates live trading using real market prices but virtual orders.
# Writes state to logs/{slug}.state.json after every tick so the
# web portal can read it without shared memory.

import queue
import sqlite3
import threading
import time
import logging
from datetime import datetime, UTC
from pathlib import Path
from config.models import BotConfig
from core.schedule_guard import ScheduleGuard
from exchanges.base_exchange import BaseExchange
from notifications.telegram import TelegramNotifier
from paper.errors import classify_exception, format_log_line
from paper.paper_state import PaperState, PaperDeal, PaperOrder
from paper.state_io import StateIO, deal_to_dict, dict_to_deal
from strategies.indicator_engine import IndicatorEngine
from core.liquidation_guard import LiquidationGuard
from core.drawdown_guard import DrawdownGuard, DrawdownGuardConfig
from core import deal_store, paths
from core.database import init_db as _init_db
from core.ids import DEAL_ID_RE

# Backwards-compatible aliases for the deal-serialisation helpers. Tests
# and other external callers import these from paper.paper_engine; they
# were extracted to paper.state_io as part of the v22 StateIO refactor
# but must keep the same import path.
_deal_to_dict = deal_to_dict
_dict_to_deal = dict_to_deal

logger = logging.getLogger(__name__)

# Deal IDs produced by PaperState.new_deal_id() follow YYYYMMDDHHMM-RRRR
# (see core/ids.py). Validate extracted sentinel filenames against this
# shape so a spoofed filename (e.g. "mybot.deal_close_..") cannot flow
# through to a dict lookup or downstream log. The web layer enforces the
# same regex on inbound POSTs (web/app.py re-exports core.ids.DEAL_ID_RE
# as _DEAL_ID_RE); this alias keeps back-compat for anyone importing the
# private attribute from this module.
_DEAL_ID_RE = DEAL_ID_RE

# ═══ Tick-loop tuning constants ═════════════════════════════════════════════
# Previously scattered magic numbers; hoisted here so operators and
# readers can tweak them in one place.

# At most one DCA order per deal per tick. Without this, a flash crash
# that drives price through multiple DCA-spacing levels in a single
# 10-second window would fire every level sequentially — turning a
# 1.2x multiplier with 10 max orders into a 1.2^9 = 5.16x base-size
# order that was never the operator's intent. Capping at 1/tick lets
# the operator see the first DCA in the state file and react before
# the next one fires on the following tick.
MAX_DCA_PER_TICK = 1

# Max seconds to wait for the notify queue to drain on stop(). The
# worker is a daemon thread, so anything still queued after this
# budget is silently dropped — operators trading off "last notification
# delivered" vs. "engine actually exits within portal.stop's timeout".
NOTIFY_DRAIN_TIMEOUT_S = 15

# Wick-simulation TTL bounds. Clamp the derived (poll_interval * 2)
# TTL so a misconfigured poll_interval can't starve or over-refresh
# the cache.
WICK_TTL_MIN_S = 5.0
WICK_TTL_MAX_S = 30.0

# Number of closed deals embedded into state.json for the UI to render.
# The DB ledger keeps the full history; this cap is about JSON bloat.
CLOSED_DEALS_UI_CAP = 50

# Rate-limit tick-error tracebacks so a broken exchange call can't
# spam the logs. First N errors log full stack; after that only the
# truncated exception string.
TICK_ERROR_TRACEBACK_LIMIT = 5

# Consecutive tick-failure count at which a transient error is reclassified
# as a persistent failure — triggers the one-shot degraded Telegram
# notification. Distinct from TICK_ERROR_TRACEBACK_LIMIT (which is a
# cumulative log-verbosity cap): the persistent threshold tracks the
# current streak and resets on any successful tick, so a short flake
# does not burn it while a sustained outage does.
TICK_ERROR_PERSISTENT_THRESHOLD = 5

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


def _collect_active_indicator_types(config: BotConfig) -> set[str]:
    """Return the set of uppercased indicator types referenced by the
    bot config — entry groups, TP groups, and the legacy flat
    entry.indicators list. Used to filter the per-tick indicator log
    line so a RSI-only bot doesn't see EMA/MACD noise."""
    active: set[str] = set()

    def _add(indicators):
        for ind in indicators or []:
            itype = getattr(ind, "type", None)
            if isinstance(itype, str) and itype.strip():
                active.add(itype.strip().upper())

    entry = getattr(config, "entry", None)
    if entry is not None:
        _add(getattr(entry, "indicators", None))
        for group in getattr(entry, "indicator_groups", None) or []:
            _add(getattr(group, "indicators", None))

    tp = getattr(config, "take_profit", None)
    if tp is not None:
        for group in getattr(tp, "indicator_groups", None) or []:
            _add(getattr(group, "indicators", None))

    return active


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
        user_id: int = 1,
    ):
        # Multi-tenant foundation (Phase 1): the engine owns a user_id
        # that it passes into every deal_store call so rows land with
        # the right tenant FK. Default 1 (admin) keeps all existing
        # callers working without edits; main_paper.py / main_live.py
        # pass it explicitly when the runner knows the bot's owner.
        self.user_id = int(user_id)
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
        # Set of uppercased indicator types that the bot is actually
        # configured to use — drives which values land in the per-tick
        # "Indicators —" log line. Computed once at construction (config
        # is immutable for the bot's lifetime) so the hot path just does
        # a set lookup.
        self._active_indicator_types: set[str] = _collect_active_indicator_types(config)
        self.liq_guard        = LiquidationGuard(config, notifier)
        # Drawdown guard defaults to a disabled config, so bots without
        # the drawdown_guard YAML key behave identically to pre-Phase-1.
        dd_cfg = getattr(config, "drawdown_guard", None) or DrawdownGuardConfig()
        self.drawdown_guard   = DrawdownGuard(dd_cfg)
        self._paused_by_drawdown: bool = False
        self.poll_interval    = poll_interval
        self._current_price: float = 0.0
        # Written to state.json as "indicators" for diagnostic inspection.
        # Not consumed by the portal UI (indicator section was removed) but
        # useful for operators inspecting the JSON file directly.
        self._last_snapshot: dict  = {}
        self._fees_paid_btc: float = 0.0

        # State file for portal communication
        self._state_file = Path(state_file) if state_file else None
        # StateIO owns the atomic read/write primitives + orphan .tmp
        # cleanup. PaperEngine still builds the snapshot dict and
        # restores its own members from load() output.
        self._state_io = StateIO(self._state_file, self._bot_slug or "")
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
        # Counter used to rate-limit tick-error tracebacks. Reset is
        # intentionally never done — we want cumulative-per-run so
        # spam doesn't re-appear after a single transient recovery.
        self._tick_error_count: int = 0
        # Consecutive-error streak — surfaced in the structured log line
        # (retry=N/M) and gates the persistent Telegram notification.
        # Resets on any successful tick so a bot that recovers does not
        # carry the streak forward. _persistent_notify_sent is edge-
        # triggered: one streak produces at most one persistent message.
        self._consecutive_tick_errors: int = 0
        self._persistent_notify_sent: bool = False

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

    def _deduct_balance(self, amount: float, reason: str) -> bool:
        """Safe balance debit with pre-flight insufficient-funds check.

        Returns True if the deduction succeeded, False if balance was too
        low. Every engine path that used to do ``balance_btc -= fee``
        directly now routes through here — on paper the net effect is the
        same (no exchange ever rejects), but the check logs clearly when
        a bot would have tripped an InsufficientFunds in live mode. For
        Phase 3 live this becomes a hard gate that prevents accidental
        over-spending.
        """
        if self.state.balance_btc < amount:
            logger.error(
                "InsufficientFunds: need %.8f BTC for %s, have %.8f",
                amount, reason, self.state.balance_btc,
            )
            self._notify(
                self.notifier.notify_error, self.config.name,
                f"Insufficient balance: {reason}",
            )
            return False
        self.state.balance_btc -= amount
        return True

    def _db_save_deal(self, deal: PaperDeal) -> None:
        """Upsert an already-tracked deal. Used by DCA / state migration.

        Treats cross-owner collision (sqlite3.IntegrityError) as an
        application bug — logs WARNING and gives up for this tick
        rather than silently regenerating the id underneath an already
        in-memory deal. For the new-deal path use
        ``_db_create_deal_with_retry`` which owns the id regeneration.
        """
        if not self._bot_slug:
            return
        try:
            deal_store.save_deal(
                deal, self._bot_slug, self.config.name, user_id=self.user_id,
            )
        except Exception as e:
            logger.warning("deal_store.save_deal failed: %s", e)

    def _db_create_deal_with_retry(
        self, deal: PaperDeal, max_attempts: int = 3,
    ) -> bool:
        """INSERT a brand-new deal row with collision retry.

        The new globally-unique id generator (core/ids.py) makes
        same-minute collisions a 1-in-10_000 event; a 3-attempt retry
        takes the compound probability to 1e-12 which is effectively
        impossible. On exhaustion we log ERROR and refuse the deal —
        caller must skip the in-memory state mutation too so the
        engine doesn't track a deal the DB doesn't know about.

        Returns True on success, False on slug-less engine or after
        ``max_attempts`` consecutive IntegrityErrors.
        """
        if not self._bot_slug:
            return False
        for attempt in range(1, max_attempts + 1):
            try:
                deal_store.create_deal(
                    deal, self._bot_slug, self.config.name,
                    user_id=self.user_id,
                )
                return True
            except sqlite3.IntegrityError:
                old_id = deal.id
                deal.id = self.state.new_deal_id()
                logger.warning(
                    "Deal id collision on %s (attempt %d/%d) — "
                    "retrying as %s",
                    old_id, attempt, max_attempts, deal.id,
                )
            except Exception as e:
                logger.warning("deal_store.create_deal failed: %s", e)
                return False
        logger.error(
            "Deal id collision not resolved after %d attempts — "
            "refusing to open deal (last id attempted: %s)",
            max_attempts, deal.id,
        )
        return False

    def _db_save_order(self, order: PaperOrder, deal_id: str, fee: float) -> None:
        if not self._bot_slug:
            return
        try:
            deal_store.save_order(
                order, deal_id, self._bot_slug,
                user_id=self.user_id, fee_btc=fee,
            )
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
                user_id=self.user_id,
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
        """Build the state snapshot and delegate the atomic write to StateIO."""
        if not self._state_file:
            return

        summary = self.state.summary()
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
                deal_to_dict(d, price)
                for d in open_deals_snap.values()
            ],
            "closed_deals": [
                deal_to_dict(d)
                for d in list(reversed(closed_deals_snap))[:CLOSED_DEALS_UI_CAP]
            ],
            "indicators": self._last_snapshot,
            # Persist drawdown guard so a restart doesn't reset the peak
            # and silently disable the kill-switch for the next leg down.
            "drawdown_guard":      self.drawdown_guard.to_dict(),
            "paused_by_drawdown":  self._paused_by_drawdown,
            # Clock-skew pause lives on LiveEngine only — attribute may
            # be absent on pure PaperEngine. getattr keeps paper runs
            # from introducing a spurious state field.
            "paused_by_clock_skew": getattr(self, "_paused_by_clock_skew", False),
        }

        self._state_io.write(snapshot)

    def _load_state(self):
        """Restore engine state from a previously written state file.

        Called once from __init__. If no state file exists we keep the
        clean PaperState created above (first ever startup). If one does
        exist we rehydrate balance, fees, closed history and any open
        deal that was in flight when the bot stopped — so the engine
        resumes monitoring it instead of silently forgetting it.

        Orphan .tmp cleanup and raw JSON parsing live in StateIO.load();
        this method owns only the "what to do with the restored dict"
        half of the contract.
        """
        data = self._state_io.load()
        if data is None:
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

        # Restore drawdown guard peak + triggered state. Critical for live —
        # without this every bot-restart would silently reset the drawdown
        # baseline to the current balance, disabling the kill-switch at
        # exactly the moment the operator needs it most.
        dd_data = data.get("drawdown_guard")
        if isinstance(dd_data, dict):
            try:
                self.drawdown_guard.from_dict(dd_data)
            except Exception as e:
                logger.warning("drawdown_guard restore failed: %s", e)
        if data.get("paused_by_drawdown"):
            self._paused_by_drawdown = True

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

        # Post-collision-fix (2026-04-19): the old per-instance counter
        # that needed to be advanced past restored IDs is gone. Deal
        # IDs are now globally-unique YYYYMMDDHHMM-RRRR strings minted
        # on demand by core.ids — no state to resynchronise on restart.

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
                    user_id=self.user_id,
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
        """Mark the bot as stopped in its state file.

        Delegates to StateIO.mark_stopped() which preserves the original
        "read, overwrite running/current_price, atomic rewrite" semantic
        — not a plain unlink, since the portal polls the file to detect
        stopped bots. Failures are logged (not swallowed) so a broken
        stop path stays visible.
        """
        self._state_io.mark_stopped()

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
        self._notify_thread.join(timeout=NOTIFY_DRAIN_TIMEOUT_S)

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def _tick(self):
        """Single iteration of the main loop."""
        # Prometheus instrumentation — best-effort. Failure to record
        # metrics must never kill the tick.
        try:
            from web import metrics as _m
            _m.record_tick(
                self._bot_slug or "unknown", self.config.mode.value,
            )
            _tick_ctx = _m.tick_duration_seconds.labels(
                bot_slug=self._bot_slug or "unknown",
            ).time()
            _tick_ctx.__enter__()
        except Exception:
            _tick_ctx = None

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
                    highs_per_tf=self._highs_per_tf,
                    lows_per_tf=self._lows_per_tf,
                )

            self._monitor_open_deals(price)
            self._update_liq_guard(price)

            # Drawdown guard — observes equity after open-deal monitoring
            # so unrealised PnL is already reflected in balance+PnL sums.
            # Triggers are translated into either a hard stop() or a
            # soft pause (skip new entries, keep managing open deals).
            self._update_drawdown_guard(price)
            if self._paused_by_drawdown:
                # Fall-through: still run sentinels and write state so
                # the portal reflects the paused state, just don't look
                # for new entries.
                pass

            # Manual trigger: the portal writes a sentinel file to force
            # an immediate deal open, bypassing schedule + indicators.
            self._check_manual_trigger(price)
            self._check_deal_sentinels(price)

            if is_open and not self._paused_by_drawdown:
                self._check_entry(price)

            # Write state for portal after every tick
            self._write_state(price, is_open)

            # Post-tick metric updates. Outside the metrics try so a
            # broken metric.set() doesn't hide a real tick error.
            try:
                from web import metrics as _m
                slug = self._bot_slug or "unknown"
                _m.set_balance(slug, self.state.balance_btc)
                _m.set_open_deals(
                    slug, len(self.state.get_open_deals_snapshot()),
                )
                peak = self.drawdown_guard.peak_value
                if peak and peak > 0:
                    equity = self._current_equity_btc(price)
                    dd_pct = max(0.0, (peak - equity) / peak * 100)
                    _m.set_drawdown_pct(slug, dd_pct)
            except Exception:
                pass

            # Successful tick completed — end any transient-error streak
            # and clear the persistent-notify latch so a future streak
            # can fire a fresh notification. Cumulative _tick_error_count
            # is intentionally NOT reset here (it feeds log-verbosity
            # rate-limiting which is a per-run protection, not per-streak).
            if self._consecutive_tick_errors > 0:
                self._consecutive_tick_errors = 0
                self._persistent_notify_sent = False

        except NotImplementedError:
            # LiveEngine's _place_market_order raises this when the
            # operator flipped dry_run off before Phase 3. The whole
            # point is to make real-order refusal LOUD — never swallow
            # it into the tick-loop retry.
            raise
        except Exception as e:
            self._tick_error_count += 1
            self._consecutive_tick_errors += 1
            try:
                from web import metrics as _m
                # Pass the exception itself — classify_error maps it to
                # a bounded-cardinality label so Prometheus doesn't grow
                # a time-series per exception subclass.
                _m.record_tick_error(self._bot_slug or "unknown", e)
            except Exception:
                pass

            # Classify once so every downstream consumer (log line,
            # Telegram notification, future metrics tag) reads the same
            # transient-vs-persistent verdict.
            err = classify_exception(
                e,
                exchange=self.config.exchange.value,
                endpoint="tick",
                symbol=self.config.pair,
                retry_attempt=self._consecutive_tick_errors,
                max_retries=TICK_ERROR_PERSISTENT_THRESHOLD,
            )
            log_line = format_log_line(err, bot=self._bot_slug or "unknown")

            # First N errors: log the full traceback for debugging.
            # After that fall back to the structured line so a broken
            # exchange endpoint can't flood the log.
            if self._tick_error_count <= TICK_ERROR_TRACEBACK_LIMIT:
                logger.exception("Tick failure %s", log_line)
            else:
                logger.error("Tick failure %s", log_line)

            # Notification gate. Transient errors stay silent while the
            # streak is still within the retry window — a single 429 or
            # momentary network blip recovers on the next tick and never
            # needs to reach Telegram. Non-transient errors (auth, bugs
            # in our own code) notify on the first occurrence because no
            # further retry will help without operator action. The latch
            # caps one streak at one persistent notification so a
            # prolonged outage doesn't repeat-spam the channel.
            should_notify = (
                not self._persistent_notify_sent
                and (
                    not err.is_transient
                    or self._consecutive_tick_errors >= TICK_ERROR_PERSISTENT_THRESHOLD
                )
            )
            if should_notify:
                self._persistent_notify_sent = True
                self._notify(
                    self.notifier.notify_error_persistent,
                    self.config.name,
                    err,
                )
        finally:
            if _tick_ctx is not None:
                try:
                    _tick_ctx.__exit__(None, None, None)
                except Exception:
                    pass

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
        ttl = max(WICK_TTL_MIN_S, min(self.poll_interval * 2.0, WICK_TTL_MAX_S))
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

    def _current_equity_btc(self, price: float) -> float:
        """Balance + unrealised PnL across every open deal.

        Used for the drawdown guard's ``equity`` metric. Uses a state
        snapshot so we never hold the PaperState lock across the PnL
        calculation — each deal's calculate_pnl only touches its own
        ordered list, which is immutable once the deal is in open_deals.
        """
        open_deals = self.state.get_open_deals_snapshot()
        unrealised = 0.0
        for deal in open_deals.values():
            try:
                pnl_btc, _ = deal.calculate_pnl(price)
            except Exception:
                pnl_btc = 0.0
            unrealised += pnl_btc
        return self.state.balance_btc + unrealised

    def _update_drawdown_guard(self, price: float) -> None:
        """Feed the current metric reading into the drawdown guard and
        apply the configured action (pause / stop) on first trigger."""
        cfg = self.drawdown_guard.config
        if not cfg.enabled:
            return

        if cfg.metric == "balance":
            value = self.state.balance_btc
        else:  # equity
            value = self._current_equity_btc(price)

        triggered = self.drawdown_guard.update(value)
        if not triggered:
            return

        # Only take action on the first trigger — subsequent ticks leave
        # us in the already-applied state rather than spamming stop()
        # or duplicate notifications.
        if self._paused_by_drawdown:
            return

        reason = self.drawdown_guard.trigger_reason or "drawdown"
        logger.error("Drawdown guard fired (%s) — action=%s", reason, cfg.action)

        self._notify(
            self.notifier.notify_error,
            self.config.name,
            f"Drawdown guard: {reason} (action={cfg.action})",
        )

        if cfg.action == "stop":
            logger.error("Stopping engine due to drawdown")
            self.running = False
        # For "pause" we just flip the flag — open deals stay managed.
        self._paused_by_drawdown = True

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

        The portal writes these to
        ``logs/<user_id>/{slug}.deal_{action}_{deal_id}`` via
        ``paths.user_logs_dir(user.id)`` (see web/routes/deals.py:184).
        The engine must scan the same user-scoped directory; a bare
        ``Path("logs")`` would miss every sentinel in Phase-2 layout
        — bug found 2026-04-19 during operator deal-close testing.
        """
        slug = self.config.name.lower().replace(" ", "_")
        log_dir = paths.user_logs_dir(self.user_id)
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
                self._deduct_balance(fee, f"manual_close:{deal_id}")
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

    def _format_indicator_log(self) -> str | None:
        """Build the per-tick "Indicators —" log line, restricted to the
        indicator types this bot actually uses.

        Returns None when nothing useful would be printed — either the
        snapshot hasn't been populated yet (first ticks, insufficient
        candle history) or the bot is configured exclusively with
        indicators whose values don't land in the snapshot today (e.g.
        PARABOLIC_SAR, BOLLINGER). A silent tick is preferable to a
        misleading "?" placeholder.
        """
        snap = self._last_snapshot
        if not snap:
            return None
        active = self._active_indicator_types
        parts: list[str] = []

        if "RSI" in active and "rsi_14" in snap:
            parts.append(f"RSI: {snap['rsi_14']:.2f}")
        # EMA_CROSS isn't a first-class indicator type in the engine
        # taxonomy, but configs that do compute EMA9/EMA21 benefit from
        # seeing both values together. Support the alias so operators
        # who hand-roll an entry on that pair see them.
        if "EMA_CROSS" in active:
            if "ema_9" in snap:
                parts.append(f"EMA9: {snap['ema_9']:.2f}")
            if "ema_21" in snap:
                parts.append(f"EMA21: {snap['ema_21']:.2f}")
        if "MACD" in active and "macd_histogram" in snap:
            parts.append(f"MACD hist: {snap['macd_histogram']:.4f}")
        if "BOLLINGER" in active and "bb_pct_b" in snap:
            parts.append(f"BB %B: {snap['bb_pct_b']:.2f}")
        if "PARABOLIC_SAR" in active and "psar" in snap:
            trend = snap.get("psar_trend", "?")
            parts.append(f"PSAR: {snap['psar']:.2f} ({trend})")
        if "SUPERTREND" in active and "supertrend" in snap:
            direction = snap.get("supertrend_dir", "?")
            parts.append(f"ST: {snap['supertrend']:.2f} ({direction})")
        if "SUPPORT_RESISTANCE" in active:
            s_val = snap.get("sr_support")
            r_val = snap.get("sr_resistance")
            if s_val is not None or r_val is not None:
                s_str = f"S@{s_val:.0f}" if s_val is not None else "S@—"
                r_str = f"R@{r_val:.0f}" if r_val is not None else "R@—"
                parts.append(f"S&R: {s_str} {r_str}")
        if "QFL" in active and "qfl_base" in snap:
            parts.append(f"QFL base: {snap['qfl_base']:.2f}")
        if "MARKET_STRUCTURE" in active and "market_structure" in snap:
            parts.append(f"MS: {snap['market_structure']}")

        if not parts:
            return None
        return "Indicators — " + " | ".join(parts)

    def _check_entry(self, price: float):
        """Check if conditions are met to start a new deal."""
        # Use snapshot to avoid holding the lock during the check
        if len(self.state.get_open_deals_snapshot()) > 0:
            return

        bot_tf = self.config.timeframe
        if not self._closes_per_tf.get(bot_tf):
            logger.warning("No candle data for bot timeframe %s — skipping entry", bot_tf)
            return

        indicator_log = self._format_indicator_log()
        if indicator_log:
            logger.info(indicator_log)

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
        """Open a new paper deal at the current price.

        Ordering: persist to DB FIRST (so a cross-bot collision fails
        loud before we mutate in-memory state), then record balance
        deduction + notify. The DB create path uses retry-with-new-id;
        on exhaustion the open is refused and state stays clean.
        """
        deal_id    = self.state.new_deal_id()
        base_order = PaperOrder(
            order_number=1,
            price=price,
            size=self.config.dca.base_order_size,
            timestamp=datetime.now(UTC),
            order_type="base"
        )

        # Side comes from the bot config (`direction`) so short-bots
        # actually open short positions. Hardcoding "long" here silently
        # turned every short config into a long run — a real money-loss
        # bug for live trading. The isinstance check keeps tests that
        # pass MagicMock configs working — a stubbed config returns a
        # MagicMock for every attribute, so we only honour `direction`
        # when it's actually a string.
        direction = getattr(self.config, "direction", "long")
        side = (
            direction
            if isinstance(direction, str) and direction in ("long", "short")
            else "long"
        )

        deal = PaperDeal(
            id=deal_id,
            bot_name=self.config.name,
            symbol=self.config.pair,
            side=side,
            leverage=self.config.leverage.size,
            orders=[base_order],
            entry_trigger=entry_trigger if isinstance(entry_trigger, dict) else None,
        )

        # Slug-less engines (tests that skip DB wiring) persist nothing
        # but must still open the deal in-memory — so the create-retry
        # is only gated on having a slug. With a slug, refuse the open
        # entirely on DB failure so in-memory never diverges from DB.
        if self._bot_slug:
            if not self._db_create_deal_with_retry(deal):
                logger.error(
                    "Refusing to open deal — DB create failed for %s",
                    self.config.name,
                )
                return

        self.state.open_deal(deal)
        fee = self._calc_fee(base_order.size)
        # Use the possibly-rewritten deal.id (retry may have replaced it).
        self._deduct_balance(fee, f"entry_fee:{deal.id}")
        self._fees_paid_btc    += fee
        self._db_save_order(base_order, deal.id, fee)
        logger.info(f"Deal opened: {deal.id} at ${price:,.2f} (fee {fee:.8f} BTC)")

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

    def _update_deal_wick_trackers(self, tick_price: float) -> None:
        """Fold the current tick-price into every open deal's since-open
        wick trackers.

        Must run ONCE per tick, BEFORE ``_check_tp`` / ``_check_sl``
        fire, so the wick-based comparisons in those checks see the
        correct "wick high observed since THIS deal opened" value.

        Rationale: the bug we're fixing is that ``_check_tp`` used to
        read the FORMING candle's full high/low via ``_wick_high_low``.
        That value includes ticks the exchange printed BEFORE the deal
        was opened, which the deal had no visibility into — and
        therefore must not retroactively trigger. Tracking per-deal
        high/low since deal-open-time fixes that asymmetry: each deal
        only reacts to ticks that occurred during its own lifetime.

        This tracker is tick-only by construction. If a future tick
        observes a value higher than ``_wick_high_since_open`` (lower
        than ``_wick_low_since_open``) we raise (lower) it; otherwise
        no mutation. Deals that were just opened in this same tick
        already had their trackers seeded to ``avg_entry_price`` via
        ``PaperDeal.__post_init__`` or ``dict_to_deal``.
        """
        open_deals = self.state.get_open_deals_snapshot()
        for deal in open_deals.values():
            if tick_price > deal._wick_high_since_open:
                deal._wick_high_since_open = tick_price
            if tick_price < deal._wick_low_since_open:
                deal._wick_low_since_open = tick_price

    def _monitor_open_deals(self, price: float):
        """Monitor all open deals for DCA, TP and SL conditions.

        Per-tick DCA cap: at most MAX_DCA_PER_TICK deals get a DCA order
        on any single tick. A flash crash that drives price through
        several DCA-spacing levels in one 10-second window would otherwise
        fire every level back-to-back — the operator only sees the
        aggregate position size after the damage is done. Capping at
        1/tick lets each DCA land in state.json before the next one
        fires, giving both the operator and the drawdown guard a chance
        to see the deteriorating position.
        """
        # Per-deal wick tracking: roll the tick into every open deal's
        # since-open high/low BEFORE evaluating TP/SL. Ordering matters
        # — checks that fire in this loop must see the tracker updated
        # with the current tick. See ``_update_deal_wick_trackers`` for
        # the rapid-fire-TP regression this closes.
        self._update_deal_wick_trackers(price)

        snapshot = self.state.get_open_deals_snapshot()
        dca_count_this_tick = 0
        for deal_id, deal in snapshot.items():
            self._check_tp(deal, price)
            if deal_id not in self.state.open_deals:
                continue
            self._check_sl(deal, price)
            if deal_id not in self.state.open_deals:
                continue
            if dca_count_this_tick >= MAX_DCA_PER_TICK:
                logger.debug(
                    "Per-tick DCA cap reached (%d); deferring %s",
                    MAX_DCA_PER_TICK, deal_id,
                )
                continue
            if self._check_dca(deal, price):
                dca_count_this_tick += 1

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

        # Per-deal wick-high: max TICK-PRICE observed since THIS deal
        # opened, folded with the current tick. Pre-fix this read the
        # forming candle's full wick via ``_wick_high_low(price)`` —
        # which included ticks from BEFORE the deal was opened and
        # caused spurious TP fires on pre-existing wicks. The tracker
        # is seeded to ``avg_entry_price`` at deal-open and updated by
        # ``_update_deal_wick_trackers`` on every tick; ``max(..., price)``
        # guarantees the comparison always includes the current tick
        # regardless of whether the caller drove the tracker first
        # (monitor loop: yes; unit-test harness: usually no).
        wick_high = max(deal._wick_high_since_open, price)
        wick_hit = (
            getattr(self.config, "use_wick_simulation", True)
            and wick_high >= target_price
        )
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
            self._deduct_balance(fee, f"tp_fee:{deal.id}")
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

        # TP indicator groups — trigger TP even if price hasn't hit target.
        # Wrapped in try/except so a buggy indicator config or bad candle
        # data can never abort the monitoring tick — backtest_engine.py
        # already follows the same fail-soft contract.
        tp_groups = getattr(self.config.take_profit, 'indicator_groups', [])
        if tp_groups:
            bot_tf = self.config.timeframe
            try:
                tp_hit, tp_info = self.indicator_engine.check_tp_indicator_groups(
                    self._closes_per_tf, bot_tf,
                    highs_per_tf=self._highs_per_tf,
                    lows_per_tf=self._lows_per_tf,
                )
            except Exception as e:
                logger.debug(
                    "TP indicator eval error: %s", str(e)[:200],
                )
                tp_hit, tp_info = False, None
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
                self._deduct_balance(fee, f"tp_indicator_fee:{deal.id}")
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

        # Per-deal high since deal-open, folded with the current tick.
        # Used for trailing-peak updates ONLY — a wick-high observed
        # earlier in the deal's lifetime is legitimate memory for a
        # rising trailing peak.
        #
        # For the SL hit-check we use the LIVE TICK only, deliberately
        # NOT the ``_wick_low_since_open`` tracker. Reason: the tracker
        # captures the lowest tick since deal-open, which for a
        # trailing SL with a rising peak includes observations from
        # BEFORE the SL line rose. Comparing an old low against the
        # current (higher) SL line is anachronistic and triggers
        # spurious closes on any deal that trails up from entry.
        # Fixed-SL loses nothing from this either: the line is below
        # entry, so the current tick is always the meaningful signal
        # (a prior tick below the line would have already triggered
        # SL at that prior tick via the monitor loop). The tracker.low
        # field is retained for observability + symmetry, not for the
        # hit decision.
        wick_sim_on = getattr(self.config, "use_wick_simulation", True)
        wick_high = max(deal._wick_high_since_open, price) if wick_sim_on else price

        if sl_type == "trailing":
            # Initialize peak on first tick after deal opens.
            if deal._peak_price == 0.0:
                deal._peak_price = price
            # Trailing peak only rises from wicks observed during the
            # deal's lifetime — pre-fix it was allowed to jump to the
            # forming candle's full wick-high, which could be a wick
            # from before the deal opened.
            new_peak = max(deal._peak_price, wick_high)
            if wick_high > price and new_peak > deal._peak_price:
                logger.debug(
                    "Trailing peak updated via since-open wick: $%.2f (tick: $%.2f)",
                    wick_high, price,
                )
            deal._peak_price = new_peak
            sl_price = deal._peak_price * (1 - sl_pct / 100)
        else:
            sl_price = deal.avg_entry_price * (1 - sl_pct / 100)

        # SL hit-check: tick-only, no tracker.low memory (see comment
        # above on why tracker.low is anachronistic for trailing SL).
        # ``wick_hit`` is synthesised False for logging-shape
        # compatibility with the rest of the function — there is no
        # between-poll wick fill in the new model.
        wick_hit = False
        wick_low = price
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
            self._deduct_balance(fee, f"sl_fee:{deal.id}")
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

    def _check_dca(self, deal: PaperDeal, price: float) -> bool:
        """Check if a DCA order should be placed. Returns True iff one
        was actually placed this tick (so the caller can enforce the
        per-tick DCA cap across multiple open deals)."""
        if not getattr(self.config.dca, 'enabled', True):
            return False
        if not deal._dca_enabled:
            return False
        # max_orders=0 means "base order only, never DCA".
        if self.config.dca.max_orders <= 1:
            return False
        if deal.dca_count >= self.config.dca.max_orders - 1:
            return False

        last_order_price = deal.orders[-1].price
        step = self.config.dca.order_spacing_pct * (
            self.config.dca.step_scale ** deal.dca_count
        )
        next_dca_price   = last_order_price * (1 - step / 100)

        if price <= next_dca_price:
            multiplier = self.config.dca.multiplier ** deal.dca_count
            dca_size   = round(self.config.dca.base_order_size * multiplier, 8)

            # No config-driven cumulative cap here by design. Ladder sizing
            # is an operator decision surfaced as advisory warnings in the
            # portal wizard (see /api/bots/validate-config). The real
            # runtime brakes are the per-tick DCA cap above and the
            # balance guard in _deduct_balance, which refuses DCA fees
            # once the account can't fund them — so a runaway ladder
            # stops at insufficient-funds, not at an arbitrary multiple.

            dca_order = PaperOrder(
                order_number=deal.dca_count + 2,
                price=price,
                size=dca_size,
                timestamp=datetime.now(UTC),
                order_type="dca"
            )
            deal.orders.append(dca_order)
            fee = self._calc_fee(dca_size)
            self._deduct_balance(fee, f"dca_fee:{deal.id}")
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
            return True
        return False

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
