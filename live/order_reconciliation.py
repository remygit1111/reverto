"""Order reconciliation for live trading — Phase 3 scaffolding.

When Phase 3 lands the live engine will place real orders through
ccxt. Network hiccups, exchange rate-limits on the confirmation
leg, and partial fills all mean the engine can't just assume "I
placed an order, therefore the exchange has one." The OrderReconciler
tracks every order the engine intended to place and polls the
exchange until it can confirm one of four terminal states:

    filled    — the exchange reports a closed order; the engine can
                apply the fill to its state.
    cancelled — the exchange confirms the order was cancelled.
    failed    — the exchange returns an explicit error status.
    timeout   — after max_age_seconds without confirmation, the
                engine must stop entering new positions and ask the
                operator to reconcile manually (via portal UI / exchange
                dashboard).

Phase 1 ships the API surface and the in-memory tracking only — the
fetch_order polling call is commented out so this module stays
importable + testable without a live exchange client. Phase 3 wires
the `self.exchange.fetch_order` path on line ~120.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    """Tracks a single order awaiting confirmation from the exchange.

    ``client_order_id`` is the idempotency key (see exchanges/bitget.py
    ``_with_order_retries``); using the same id across retries lets us
    detect "exchange already has this order" instead of double-placing.
    """
    client_order_id: str
    deal_id: str
    side: str
    size: float
    placed_at: float
    status: str = "pending"
    exchange_order_id: Optional[str] = None
    error: Optional[str] = None
    fill_price: Optional[float] = None
    filled_size: Optional[float] = None
    extra: dict = field(default_factory=dict)


class OrderReconciler:
    """Tracks pending orders and polls the exchange for confirmation.

    Usage from LiveEngine (Phase 3):

        reconciler.track_order(PendingOrder(...))
        ...
        completed = reconciler.reconcile()
        for order in completed:
            if order.status == "filled":
                engine._apply_fill(order)
            elif order.status == "timeout":
                engine._notify_operator_manual_reconcile(order)
    """

    def __init__(
        self,
        exchange: Any,
        poll_interval: float = 2.0,
        max_age_seconds: float = 60.0,
    ) -> None:
        self.exchange = exchange
        self.poll_interval = poll_interval
        self.max_age_seconds = max_age_seconds
        self._pending: dict[str, PendingOrder] = {}

    def track_order(self, order: PendingOrder) -> None:
        """Register a freshly-placed order for reconciliation polling."""
        self._pending[order.client_order_id] = order

    def get_pending(self) -> list[PendingOrder]:
        """Snapshot of currently-tracked orders. Callers must not mutate
        the returned list — use ``track_order`` to add and ``reconcile``
        to remove entries."""
        return list(self._pending.values())

    def reconcile(self, now: Optional[float] = None) -> list[PendingOrder]:
        """Advance each pending order one reconciliation cycle.

        Returns orders that reached a terminal state this call (removed
        from internal tracking). The caller is responsible for applying
        fills / notifying on failures / stopping trading on timeouts.

        Phase 1 implementation: timeout check only. Phase 3 wires the
        commented-out fetch_order branch below.
        """
        now = now if now is not None else time.time()
        completed: list[PendingOrder] = []

        for coid, pending in list(self._pending.items()):
            age = now - pending.placed_at

            if age > self.max_age_seconds:
                pending.status = "timeout"
                pending.error = f"No confirmation after {age:.1f}s"
                completed.append(pending)
                del self._pending[coid]
                logger.error(
                    "Order %s TIMEOUT — manual reconcile needed (deal=%s)",
                    coid, pending.deal_id,
                )
                continue

            # Phase 3 real polling — uncomment and wire when live orders go on.
            #
            # try:
            #     status = self.exchange.fetch_order(coid, symbol)
            #     if status.get("status") == "closed":
            #         pending.status = "filled"
            #         pending.exchange_order_id = status.get("id")
            #         pending.fill_price = status.get("average")
            #         pending.filled_size = status.get("filled")
            #         completed.append(pending)
            #         del self._pending[coid]
            #     elif status.get("status") == "canceled":
            #         pending.status = "cancelled"
            #         completed.append(pending)
            #         del self._pending[coid]
            # except Exception as e:
            #     pending.error = str(e)[:200]

        return completed

    def clear(self) -> None:
        """Drop every pending order without reconciling. Used on engine
        shutdown so the reconciler doesn't outlive the engine process."""
        self._pending.clear()
