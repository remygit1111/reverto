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
        # Pre-flight: base order size safety rail. Runs BEFORE super
        # init so a misconfigured bot never starts a notify worker or
        # touches the state file.
        bos = config.dca.base_order_size
        if bos > max_base_order_size:
            raise ValueError(
                f"Base order size {bos} exceeds max allowed "
                f"{max_base_order_size} for live trading. "
                f"Adjust config or raise --max-base-order-size."
            )

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
        self._dry_run = dry_run
        self._max_base_order_size = max_base_order_size
        # Every order that would have been placed lands here, both in
        # dry-run and (later) in Phase-3 live mode. Useful for the
        # portal dashboard and for post-run audits.
        self._live_order_log: list[dict] = []

        logger.warning(
            "LiveEngine initialised — dry_run=%s base_order_size=%s max=%s",
            dry_run, bos, max_base_order_size,
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
            "dry_run": self._dry_run,
            "timestamp": self._now_ts(),
        }
        self._live_order_log.append(order_info)

        if self._dry_run:
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
        internal list."""
        return list(self._live_order_log)

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run
