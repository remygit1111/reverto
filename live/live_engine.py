"""Live trading engine for Reverto — Phase 1 scaffolding.

Inherits PaperEngine's state management, DCA logic, TP/SL monitoring
and indicator evaluation wholesale. The only thing LiveEngine layers
on top is a real-order API surface — and in Phase 1 that surface is
still a dry-run: every "order" is logged to an in-memory list and a
synthetic fill dict is returned so the engine's internal bookkeeping
stays consistent.

Phase 1 safety rails:
  * ``max_base_order_size`` pre-flight — refuse bots with an oversized
    DCA base order before the engine even boots.
  * ``dry_run=True`` default — the only path that ever reaches ccxt
    is the explicit ``dry_run=False`` branch, which still raises
    NotImplementedError until Phase 3 wires it up.
  * Live bots must boot through ``main_live.py`` — ``main_paper.py``
    refuses ``mode=live`` configs and vice versa.

Phase sequence (see live/README.md):
  Phase 1 — scaffolding + dry-run (HERE)
  Phase 2 — dry-run parity vs paper for ≥2 weeks
  Phase 3 — real-order execution with minimal size
  Phase 4 — full live trading
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

from config.models import BotConfig
from core.clock_monitor import ClockMonitor
from exchanges.base_exchange import BaseExchange
from live.order_reconciliation import OrderReconciler
from notifications.telegram import TelegramNotifier
from paper.paper_engine import PaperEngine

logger = logging.getLogger(__name__)

# Default tolerance for clock-skew monitoring. Exchanges typically
# reject orders whose client-side timestamp drifts more than a few
# seconds from the server clock; 5s is a conservative default that
# doesn't trip on normal NTP jitter but catches broken clocks.
DEFAULT_CLOCK_SKEW_TOLERANCE_S = 5.0

# Hard cap on the base order size any live bot is allowed to boot with
# by default. Operators can bump it via the main_live.py CLI flag if
# they know what they're doing, but the default keeps an accidental
# fat-finger config from placing a bigger order than intended.
DEFAULT_MAX_BASE_ORDER_SIZE_BTC = 0.001

# Worst-case DCA order size ceiling expressed as a multiple of the base
# order. Catches geometric-growth fat-finger configs (mult=2.0 × 10
# orders → 512× base) while still allowing conservative ladders that
# intentionally scale into drawdown. The v20 audit set this to 10×,
# which refused legitimate 1.3–1.6 multipliers at max_orders ≥ 8 even
# though those are deliberate, not explosions. 50× accommodates those
# (e.g. mult=1.5 × 10 orders → 38× base, mult=1.4 × 11 orders → 29×)
# and still rejects anything approaching the fat-finger regime.
MAX_DCA_SIZE_VS_BASE = 50.0

# Default cumulative-position ceiling when the operator didn't set
# max_cumulative_size explicitly. 20 × base_order_size is a deliberately
# generous default — operators serious about live trading should pin a
# tighter value via the YAML.
DEFAULT_CUMULATIVE_MULTIPLIER = 20.0

# Bounded order-log ring buffer. A 24/7 bot can emit thousands of DCA
# entries per month; an unbounded list grows monotonically and eventually
# OOM's a long-running process. 10k entries ≈ a quarter of a year of
# typical activity, plenty for forensic review without risking memory.
LIVE_ORDER_LOG_MAXLEN = 10_000

# How often the OrderReconciler runs. Counted in tick iterations (not
# seconds) so a fast poll_interval automatically tightens the cadence.
# At the default 10s tick + 5 = every 50s, well under the reconciler's
# 60s timeout so slow-confirmation orders are still caught before the
# timeout path fires.
RECONCILE_EVERY_N_TICKS = 5


class LiveEngine(PaperEngine):
    """Paper engine sibling that plugs in a real exchange client.

    All trade decisions still flow through PaperEngine's loop — the
    indicator engine, DCA spacing, TP/SL logic, sentinels and
    drawdown guard are inherited unchanged. The two touch-points we
    override are the order placement API and the "current price"
    helper (paper uses a polled ticker that may not hit every live
    price refresh cadence).
    """

    def __init__(
        self,
        config: BotConfig,
        exchange: BaseExchange,
        notifier: TelegramNotifier,
        *,
        initial_balance_btc: float = 0.1,
        poll_interval: int = 10,
        state_file: Optional[str] = None,
        manual_trigger_file: Optional[str] = None,
        slug: Optional[str] = None,
        dry_run: bool = True,
        max_base_order_size: float = DEFAULT_MAX_BASE_ORDER_SIZE_BTC,
        clock_skew_tolerance: float = DEFAULT_CLOCK_SKEW_TOLERANCE_S,
    ) -> None:
        # Pre-flight safety rails: run BEFORE super().__init__ so a
        # misconfigured bot never starts a notify worker, touches the
        # state file, or initialises a DB row.
        self._preflight(config, max_base_order_size)

        super().__init__(
            config=config,
            exchange=exchange,
            notifier=notifier,
            initial_balance_btc=initial_balance_btc,
            poll_interval=poll_interval,
            state_file=state_file,
            manual_trigger_file=manual_trigger_file,
            slug=slug,
        )

        self._live_exchange = exchange
        # Private backing field — dry_run is exposed as a read-only
        # property. Making it mutable invited accidental monkey-patching
        # that would silently enable real orders; the property blocks that.
        self.__dry_run = dry_run
        self._max_base_order_size = max_base_order_size
        # Bounded order-log ring buffer (see LIVE_ORDER_LOG_MAXLEN). Older
        # entries are dropped automatically once the buffer fills up.
        self._live_order_log: deque = deque(maxlen=LIVE_ORDER_LOG_MAXLEN)

        # Clock-skew monitor — Phase-2+ guard against placing orders
        # with a drifted client clock. Fail-open on fetch errors so a
        # flaky time endpoint never locks trading out permanently.
        self.clock_monitor = ClockMonitor(
            exchange, max_skew_seconds=clock_skew_tolerance,
        )
        self._paused_by_clock_skew: bool = False

        # Order reconciler — Phase 1 scaffolding runs the timeout pass
        # only (the commented-out fetch_order branch lights up in
        # Phase 3). We tick it every RECONCILE_EVERY_N_TICKS iterations
        # so pending-order tracking is already wired + tested before
        # real orders go live.
        self.order_reconciler = OrderReconciler(
            exchange=self._live_exchange,
            poll_interval=2.0,
            max_age_seconds=60.0,
        )
        self._reconcile_tick_counter: int = 0

        logger.warning(
            "LiveEngine initialised — dry_run=%s base_order_size=%s max=%s skew_tol=%ss",
            dry_run, config.dca.base_order_size, max_base_order_size,
            clock_skew_tolerance,
        )

    # ------------------------------------------------------------------
    # Tick override — clock-skew gate
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Override of PaperEngine._tick that pre-checks exchange clock
        skew. When the local clock has drifted beyond tolerance we skip
        order-placement entirely for this tick; state writing still
        proceeds so the dashboard reflects the paused state.
        """
        skew, ok = self.clock_monitor.check()
        if not ok:
            if not self._paused_by_clock_skew:
                logger.error(
                    "Clock skew %.2fs exceeds tolerance — pausing orders",
                    skew,
                )
            self._paused_by_clock_skew = True
            # Skipping entry / DCA / order actions is simplest by
            # flipping the existing drawdown-pause flag — _check_entry
            # and _check_dca both honour it. We flip only for this
            # tick; the next tick re-checks skew.
            prev_drawdown = self._paused_by_drawdown
            self._paused_by_drawdown = True
            try:
                super()._tick()
            finally:
                self._paused_by_drawdown = prev_drawdown
            return

        if self._paused_by_clock_skew:
            logger.info("Clock skew back within tolerance (%.2fs) — resuming", skew)
        self._paused_by_clock_skew = False
        super()._tick()

        # Periodic reconciler pass. In Phase 1 this only surfaces
        # timeout-pending orders; Phase 3 wires the real fetch_order
        # branch. Running it here (not inside super()._tick) keeps
        # PaperEngine tick semantics untouched.
        self._reconcile_tick_counter += 1
        if self._reconcile_tick_counter >= RECONCILE_EVERY_N_TICKS:
            self._reconcile_tick_counter = 0
            self._run_reconciliation()

    def _run_reconciliation(self) -> None:
        """Advance the OrderReconciler one cycle and notify on terminal
        states (currently only ``timeout`` — Phase 3 adds filled /
        cancelled / failed).

        Exceptions are logged with full traceback but never propagate:
        reconciler issues must not kill the tick loop.
        """
        try:
            completed = self.order_reconciler.reconcile()
        except Exception as e:
            logger.exception("Reconciliation error: %s", str(e)[:200])
            return

        if not completed:
            return

        logger.info("Reconciler completed %d orders", len(completed))
        for order in completed:
            if order.status == "timeout":
                logger.error(
                    "Order %s timed out — manual reconcile needed",
                    order.client_order_id,
                )
                self._notify(
                    self.notifier.notify_error,
                    self.config.name,
                    f"Order timeout: {order.client_order_id}",
                )

    @staticmethod
    def _preflight(config: BotConfig, max_base_order_size: float) -> None:
        """Refuse dangerous DCA configs before the engine boots.

        Checks:
          1. base_order_size <= max_base_order_size (fat-finger cap).
          2. Worst-case DCA order (multiplier ** (max_orders-1)) stays
             within MAX_DCA_SIZE_VS_BASE × base — prevents geometric
             growth blowing past the cap.
          3. Cumulative position size across all DCA orders stays within
             config.dca.max_cumulative_size (or default 20x base).
        """
        bos = config.dca.base_order_size
        if bos > max_base_order_size:
            raise ValueError(
                f"Base order size {bos} exceeds max allowed "
                f"{max_base_order_size} for live trading. "
                f"Adjust config or raise --max-base-order-size."
            )

        multiplier = config.dca.multiplier
        max_orders = max(config.dca.max_orders, 1)
        worst_dca = bos * (multiplier ** max(max_orders - 1, 0))

        if worst_dca > bos * MAX_DCA_SIZE_VS_BASE:
            raise ValueError(
                f"Worst-case DCA order size {worst_dca:.8f} BTC exceeds "
                f"{MAX_DCA_SIZE_VS_BASE}× base ({bos}). "
                f"multiplier={multiplier}, max_orders={max_orders}. "
                f"Reduce multiplier or max_orders before live trading."
            )

        # Cumulative (base + every DCA) cap. Operator-set value wins;
        # otherwise we fall back to DEFAULT_CUMULATIVE_MULTIPLIER × base.
        cumulative_size = sum(
            bos * (multiplier ** i) for i in range(max_orders)
        )
        configured_cap = getattr(config.dca, "max_cumulative_size", None)
        cumulative_cap = (
            configured_cap
            if configured_cap is not None
            else bos * DEFAULT_CUMULATIVE_MULTIPLIER
        )

        if cumulative_size > cumulative_cap:
            raise ValueError(
                f"Cumulative DCA position size {cumulative_size:.8f} BTC "
                f"exceeds cap {cumulative_cap:.8f} BTC. "
                f"Configure dca.max_cumulative_size or reduce multiplier/max_orders."
            )

        logger.warning(
            "LiveEngine preflight OK: base=%s worst_dca=%.8f cumulative=%.8f cap=%.8f",
            bos, worst_dca, cumulative_size, cumulative_cap,
        )

    # ------------------------------------------------------------------
    # Order execution — Phase 1: dry-run only
    # ------------------------------------------------------------------

    def _now_ts(self) -> int:
        """Millisecond epoch — keeps live_order_log entries comparable
        across dry-run and future live modes."""
        return int(time.time() * 1000)

    def _place_market_order(
        self,
        side: str,
        size: float,
        price: Optional[float] = None,
    ) -> dict:
        """Place (or simulate) a market order.

        Dry-run: logs the intent, appends to ``_live_order_log`` and
        returns a synthetic fill so the caller's bookkeeping (balance,
        order list, notifications) works identically to paper mode.

        Live mode: currently raises NotImplementedError. Phase 3 wires
        this to ``self._live_exchange.place_market_order(...)``.
        """
        order_info = {
            "type": "market",
            "side": side,
            "size": size,
            "price_at_signal": price,
            "dry_run": self.__dry_run,
            "timestamp": self._now_ts(),
        }
        self._live_order_log.append(order_info)

        if self.__dry_run:
            logger.warning(
                "[DRY RUN] Would place %s market order: size=%s price~%s",
                side, size, price,
            )
            return {
                "id": f"DRYRUN-{order_info['timestamp']}",
                "status": "filled",
                "filled": size,
                "average": price,
                "dry_run": True,
            }

        # Phase 3 target — refuse the call until real execution is wired.
        raise NotImplementedError(
            "Real order execution not yet implemented. "
            "LiveEngine is in Phase 1 (dry-run only)."
        )

    def _get_current_price(self) -> float:
        """Fetch the latest price from the live exchange.

        PaperEngine's _tick() reads ticker.mark_price / ticker.last
        directly, so this helper is the hook that LiveEngine subclasses
        or tests can override (e.g. to drive deterministic prices in
        integration tests). Not used by the default tick loop yet —
        Phase 2 will switch it in once parity is verified.
        """
        ticker = self._live_exchange.get_ticker(self.config.pair)
        last = getattr(ticker, "last", None)
        if last is None and isinstance(ticker, dict):
            last = ticker.get("last")
        return float(last)

    def get_live_order_log(self) -> list[dict]:
        """Shallow copy of the in-memory order log. The portal can poll
        this to show a live-bot timeline without touching the engine's
        internal deque."""
        return list(self._live_order_log)

    @property
    def dry_run(self) -> bool:
        """Read-only view of the dry-run flag.

        Intentionally no setter: once the engine has been constructed
        with dry_run=True, the only way to switch to real orders is to
        instantiate a fresh engine (which goes through the preflight
        again). A mutable attribute could be flipped via monkey-patch
        or stray assignment — too easy a foot-gun for something that
        guards real money."""
        return self.__dry_run

    # Alias preserved for backwards compatibility with tests / portal
    # code that checked ``engine.is_dry_run``.
    @property
    def is_dry_run(self) -> bool:
        return self.__dry_run
