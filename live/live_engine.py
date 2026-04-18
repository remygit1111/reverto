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
from exchanges.base_exchange import BaseExchange
from notifications.telegram import TelegramNotifier
from paper.paper_engine import PaperEngine

logger = logging.getLogger(__name__)

# Hard cap on the base order size any live bot is allowed to boot with
# by default. Operators can bump it via the main_live.py CLI flag if
# they know what they're doing, but the default keeps an accidental
# fat-finger config from placing a bigger order than intended.
DEFAULT_MAX_BASE_ORDER_SIZE_BTC = 0.001

# Worst-case DCA order size ceiling expressed as a multiple of the base
# order. With base=0.001 and this set to 10x, a DCA multiplier that
# would produce a final order above 0.01 BTC is refused at construction.
# Catches geometric-growth configs (multiplier=2.0 + many max_orders)
# that would otherwise exceed the operator's intent by orders of
# magnitude. Applied on top of max_base_order_size.
MAX_DCA_SIZE_VS_BASE = 10.0

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

        logger.warning(
            "LiveEngine initialised — dry_run=%s base_order_size=%s max=%s",
            dry_run, config.dca.base_order_size, max_base_order_size,
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
