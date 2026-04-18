"""Tests for live/order_reconciliation.py.

Phase 1 scope: in-memory tracking + timeout path. The actual
exchange-polling branch is commented out in the module, so these
tests focus on the public API: track_order, reconcile, clear.
"""

import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from live.order_reconciliation import OrderReconciler, PendingOrder  # noqa: E402


def _pending(coid="order-1", age_s=0.0):
    """Helper — pending order with a controllable placed_at so the
    timeout path can be tested deterministically."""
    return PendingOrder(
        client_order_id=coid,
        deal_id="PAPER-0001",
        side="buy",
        size=0.001,
        placed_at=time.time() - age_s,
    )


class TestOrderReconcilerTracking:

    def test_track_order_stores_it(self):
        r = OrderReconciler(MagicMock(), max_age_seconds=60)
        r.track_order(_pending("abc"))
        pending = r.get_pending()
        assert len(pending) == 1
        assert pending[0].client_order_id == "abc"

    def test_get_pending_returns_shallow_copy(self):
        r = OrderReconciler(MagicMock())
        r.track_order(_pending("abc"))
        snap = r.get_pending()
        snap.append(_pending("tainted"))
        # Internal state unchanged.
        assert len(r.get_pending()) == 1

    def test_clear_removes_all(self):
        r = OrderReconciler(MagicMock())
        r.track_order(_pending("a"))
        r.track_order(_pending("b"))
        r.clear()
        assert r.get_pending() == []


class TestReconcileTimeout:

    def test_timeout_after_max_age(self):
        """An order older than max_age_seconds must move to status=timeout
        and leave the pending map."""
        r = OrderReconciler(MagicMock(), max_age_seconds=30.0)
        r.track_order(_pending("old", age_s=60.0))

        completed = r.reconcile()
        assert len(completed) == 1
        assert completed[0].status == "timeout"
        assert "No confirmation" in (completed[0].error or "")
        assert r.get_pending() == []

    def test_within_age_stays_pending(self):
        r = OrderReconciler(MagicMock(), max_age_seconds=30.0)
        r.track_order(_pending("fresh", age_s=5.0))

        completed = r.reconcile()
        assert completed == []
        assert len(r.get_pending()) == 1

    def test_reconcile_now_override(self):
        """Explicit ``now`` arg lets tests drive the clock without
        freezegun — useful for deterministic timeout checks."""
        r = OrderReconciler(MagicMock(), max_age_seconds=30.0)
        order = _pending("t")
        r.track_order(order)

        # Push `now` 60s into the future — order was placed at time.time()
        # so the delta is ~60s and must trigger the timeout.
        completed = r.reconcile(now=order.placed_at + 60)
        assert len(completed) == 1
        assert completed[0].status == "timeout"
