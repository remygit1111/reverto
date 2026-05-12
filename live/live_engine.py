"""Live trading engine for Reverto — Phase 1 scaffolding.

Inherits PaperEngine's state management, DCA logic, TP/SL monitoring
and indicator evaluation wholesale. The only thing LiveEngine layers
on top is a real-order API surface — and in Phase 1 that surface is
still a dry-run: every "order" is logged to an in-memory list and a
synthetic fill dict is returned so the engine's internal bookkeeping
stays consistent.

Safety posture:
  * Runtime guards are the real defence: per-tick DCA cap, balance
    guard, drawdown guard, clock-skew monitor, liquidation guard,
    emergency-stop endpoint. These all live in PaperEngine or in
    sibling modules and keep running regardless of config values.
  * Static configuration caps have been removed — any ladder the
    operator can express in YAML boots. Risk analysis surfaces to
    the operator as advisory warnings in the portal wizard's Review
    step (see ``/api/bots/validate-config``), not as hard refusals.
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
        clock_skew_tolerance: float = DEFAULT_CLOCK_SKEW_TOLERANCE_S,
        user_id: int = 1,
        exchange_type: str = "",
    ) -> None:
        super().__init__(
            config=config,
            exchange=exchange,
            notifier=notifier,
            initial_balance_btc=initial_balance_btc,
            poll_interval=poll_interval,
            state_file=state_file,
            manual_trigger_file=manual_trigger_file,
            slug=slug,
            user_id=user_id,
            exchange_type=exchange_type,
        )

        self._live_exchange = exchange
        # Private backing field — dry_run is exposed as a read-only
        # property. Making it mutable invited accidental monkey-patching
        # that would silently enable real orders; the property blocks that.
        self.__dry_run = dry_run
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

        # Config profile: logged for forensic context, never enforced.
        # The portal wizard's Review step surfaces the same numbers as
        # advisory warnings via /api/bots/validate-config; here we just
        # want the values present in portal.log so a post-mortem can
        # cross-reference what the operator actually booted with.
        bos = config.dca.base_order_size
        multiplier = config.dca.multiplier
        max_orders = max(config.dca.max_orders, 1)
        worst_dca = bos * (multiplier ** max(max_orders - 1, 0))
        cumulative = sum(
            bos * (multiplier ** i) for i in range(max_orders)
        )
        base_multiple = worst_dca / bos if bos > 0 else 0.0
        cum_multiple = cumulative / bos if bos > 0 else 0.0
        logger.warning(
            "LiveEngine initialised — dry_run=%s skew_tol=%ss | "
            "config profile: base=%.8f BTC, worst_dca=%.8f (%.1f× base), "
            "cumulative=%.8f (%.1f× base), max_orders=%d, multiplier=%s",
            dry_run, clock_skew_tolerance, bos,
            worst_dca, base_multiple,
            cumulative, cum_multiple,
            max_orders, multiplier,
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
        instantiate a fresh engine. A mutable attribute could be
        flipped via monkey-patch or stray assignment — too easy a
        foot-gun for something that guards real money."""
        return self.__dry_run

    # Alias preserved for backwards compatibility with tests / portal
    # code that checked ``engine.is_dry_run``.
    @property
    def is_dry_run(self) -> bool:
        return self.__dry_run
