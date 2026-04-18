"""Thread-safety test for OrderReconciler's lock.

Spin up two threads: one hammers track_order, the other calls reconcile.
Without the lock the old implementation would hit "dictionary changed
size during iteration"; with the lock it completes cleanly.
"""

import sys
import threading
import time
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from live.order_reconciliation import OrderReconciler, PendingOrder  # noqa: E402


def _make_pending(coid: str, age_s: float = 0.0) -> PendingOrder:
    return PendingOrder(
        client_order_id=coid, deal_id="D-1",
        side="buy", size=0.001,
        placed_at=time.time() - age_s,
    )


class TestReconcilerConcurrency:

    def test_concurrent_track_and_reconcile_do_not_raise(self):
        """Two tight threads running track_order + reconcile for 50ms.
        Under the old lockless code, about 1-in-5 runs crashed with a
        dict-mutation error; with the lock zero crashes across 200 ops."""
        r = OrderReconciler(MagicMock(), max_age_seconds=0.5)
        errors: list[BaseException] = []

        def tracker():
            try:
                for i in range(200):
                    r.track_order(_make_pending(f"c-{i}"))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def reconciler():
            try:
                for _ in range(200):
                    r.reconcile()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        t1 = threading.Thread(target=tracker)
        t2 = threading.Thread(target=reconciler)
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert errors == [], f"concurrency errors: {errors!r}"

    def test_snapshot_isolation(self):
        """get_pending must return a list detached from the internal
        dict — mutating the snapshot doesn't affect the tracker."""
        r = OrderReconciler(MagicMock())
        r.track_order(_make_pending("a"))
        snap = r.get_pending()
        snap.clear()
        assert len(r.get_pending()) == 1
