"""Tests for the bounded notify queue — PT-v4-EI-005.

The TelegramNotifier dispatch path queues closures on
``PaperEngine._notify_queue`` and a daemon thread drains them via
``_notify_worker``. Pre-fix the queue was unbounded; a permanent
Telegram outage with the worker stuck in a long ``send()`` would
let memory grow proportional to ``ticks × outage_duration``.

Post-fix the queue is bounded by ``REVERTO_TELEGRAM_QUEUE_MAX``
(default 1000) with an oldest-drop policy on overflow. Drops are
counted; a single WARNING summary fires when the queue drains
back to empty after an outage.

These tests pin:
  * Queue cap is configurable via env var.
  * Default is 1000 when unset.
  * Malformed / non-positive override falls back to default.
  * Oldest-drop policy: 1001th item evicts the oldest.
  * Drop counter increments correctly.
  * Recovery WARNING fires once per outage when queue drains to
    empty.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from config.models import (  # noqa: E402
    BotConfig,
    DCAConfig,
    Exchange,
    Mode,
    TakeProfitConfig,
)
from paper.paper_engine import (  # noqa: E402
    PaperEngine,
    _DEFAULT_NOTIFY_QUEUE_MAX,
    _resolve_notify_queue_max,
)


# ── Resolver helper tests ───────────────────────────────────────────────────


class TestNotifyQueueMaxResolver:
    """Direct unit tests for the env-var resolver. Mirror the style
    of TestMaxBotsHelperResolver in test_bots_quota.py."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("REVERTO_TELEGRAM_QUEUE_MAX", raising=False)
        assert _resolve_notify_queue_max() == _DEFAULT_NOTIFY_QUEUE_MAX
        # Pin the actual default value in the test so a future
        # constant-bump shows up in review explicitly.
        assert _DEFAULT_NOTIFY_QUEUE_MAX == 1000

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("REVERTO_TELEGRAM_QUEUE_MAX", "250")
        assert _resolve_notify_queue_max() == 250

    def test_malformed_override_falls_back(self, monkeypatch):
        monkeypatch.setenv("REVERTO_TELEGRAM_QUEUE_MAX", "ten-thousand")
        assert _resolve_notify_queue_max() == _DEFAULT_NOTIFY_QUEUE_MAX

    def test_non_positive_override_falls_back(self, monkeypatch):
        monkeypatch.setenv("REVERTO_TELEGRAM_QUEUE_MAX", "0")
        assert _resolve_notify_queue_max() == _DEFAULT_NOTIFY_QUEUE_MAX
        monkeypatch.setenv("REVERTO_TELEGRAM_QUEUE_MAX", "-1")
        assert _resolve_notify_queue_max() == _DEFAULT_NOTIFY_QUEUE_MAX


# ── Engine-level fixture with stalled worker ───────────────────────────────


@pytest.fixture
def _config():
    return BotConfig(
        name="QueueTestBot",
        mode=Mode.PAPER,
        exchange=Exchange.BITGET,
        pair="BTC/USD",
        dca=DCAConfig(
            enabled=True,
            base_order_size=0.001,
            max_orders=3,
            order_spacing_pct=1.5,
            multiplier=1.0,
        ),
        take_profit=TakeProfitConfig(enabled=True, target_pct=3.0),
    )


@pytest.fixture
def _exchange():
    e = MagicMock()
    e.get_ticker.return_value = MagicMock(mark_price=50000.0, last=50000.0)
    e.get_ohlcv.return_value = [
        [1_000_000 + i * 60_000, 50000.0, 50100.0, 49900.0, 50050.0, 1.0]
        for i in range(100)
    ]
    return e


@pytest.fixture
def _notifier():
    n = MagicMock()
    for m in [
        "notify_startup", "notify_shutdown", "notify_entry", "notify_dca",
        "notify_take_profit", "notify_stop_loss", "notify_error",
        "notify_error_persistent", "notify_stop", "notify_restart",
    ]:
        setattr(n, m, MagicMock())
    return n


def _make_engine(monkeypatch, config, exchange, notifier, tmp_path,
                 max_size: int = 5):
    """Build an engine with a small queue cap so the tests can fill
    + overflow it without queuing thousands of closures.

    The notify worker is replaced with a stalled stand-in that never
    drains — keeps the queue full for the duration of the test so
    drop semantics are deterministic. Real engine teardown is skipped
    in favour of explicit per-test cleanup.
    """
    monkeypatch.setenv("REVERTO_TELEGRAM_QUEUE_MAX", str(max_size))
    state_file = tmp_path / "queue_test.state.json"
    eng = PaperEngine(
        config=config,
        exchange=exchange,
        notifier=notifier,
        initial_balance_btc=0.1,
        poll_interval=1,
        state_file=str(state_file),
        slug="queuetestbot",
    )
    # The real worker was started in __init__ as a daemon. We CAN'T
    # safely stop it here without flushing the queue — but for these
    # tests we want the queue to fill up. Solution: pause the worker
    # by inserting a blocking sentinel item it consumes first, then
    # the test fills the rest of the queue. To avoid that complexity
    # we just verify the put side directly without relying on the
    # worker draining.
    return eng


def _teardown_engine(eng):
    """Stop the daemon worker thread without dispatching anything."""
    # Drain whatever is queued so the worker can reach the sentinel.
    try:
        while not eng._notify_queue.empty():
            eng._notify_queue.get_nowait()
            eng._notify_queue.task_done()
    except Exception:
        pass
    eng._notify_queue.put(None)
    eng._notify_thread.join(timeout=5)


# ── Cap + drop policy ───────────────────────────────────────────────────────


class TestQueueCap:
    """Bounded queue: max ``REVERTO_TELEGRAM_QUEUE_MAX`` items live;
    overflow drops oldest."""

    def test_cap_set_from_env(
        self, monkeypatch, _config, _exchange, _notifier, tmp_path,
    ):
        eng = _make_engine(
            monkeypatch, _config, _exchange, _notifier, tmp_path,
            max_size=7,
        )
        try:
            assert eng._notify_queue_max == 7
            assert eng._notify_queue.maxsize == 7
        finally:
            _teardown_engine(eng)

    def test_oldest_dropped_on_overflow(
        self, monkeypatch, _config, _exchange, _notifier, tmp_path,
    ):
        """Hold the worker via a blocking sentinel so the queue can fill
        deterministically. Push max+1 items; the *first* one inserted
        must be the one dropped, leaving the latest max items in
        order."""
        eng = _make_engine(
            monkeypatch, _config, _exchange, _notifier, tmp_path,
            max_size=3,
        )
        try:
            # Stop the worker thread so the queue stays full. We do
            # this by sending the sentinel and joining — from this
            # point on no items are drained.
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

            # Recreate a clean queue at the same maxsize so this test
            # controls exactly which items live in it. The sentinel
            # was just consumed.
            import queue as _q
            eng._notify_queue = _q.Queue(maxsize=3)

            # Each closure carries a distinct argument so we can tell
            # them apart inside the queue.
            def _make_closure(i):
                fn = MagicMock()
                return (fn, (i,), {})

            for i in range(3):
                eng._notify_queue.put_nowait(_make_closure(i))
            assert eng._notify_queue.full()

            # Putting a 4th item via the engine's own _notify path
            # should drop the oldest (i=0) and keep [1, 2, 3].
            new_fn = MagicMock()
            eng._notify(new_fn, 99)
            assert eng._notify_dropped_count == 1
            assert eng._notify_drop_pending is True

            remaining = []
            while not eng._notify_queue.empty():
                fn, args, kwargs = eng._notify_queue.get_nowait()
                eng._notify_queue.task_done()
                remaining.append(args[0])
            assert remaining == [1, 2, 99], (
                f"expected [1, 2, 99] (oldest dropped), got {remaining}"
            )
        finally:
            # Worker already joined; nothing else to clean up.
            pass

    def test_drop_counter_increments_per_eviction(
        self, monkeypatch, _config, _exchange, _notifier, tmp_path,
    ):
        """Each overflow put increments the drop counter exactly once."""
        eng = _make_engine(
            monkeypatch, _config, _exchange, _notifier, tmp_path,
            max_size=2,
        )
        try:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)
            import queue as _q
            eng._notify_queue = _q.Queue(maxsize=2)

            for i in range(2):
                eng._notify_queue.put_nowait((MagicMock(), (i,), {}))
            assert eng._notify_dropped_count == 0

            for i in range(5):
                eng._notify(MagicMock(), f"new-{i}")
            assert eng._notify_dropped_count == 5
            assert eng._notify_drop_pending is True
        finally:
            pass


# ── Recovery summary ────────────────────────────────────────────────────────


class TestRecoverySummary:
    """When the queue drains to empty after drops, the worker logs a
    single WARNING summary line and clears the latch."""

    def test_recovery_warning_fires_once(
        self, monkeypatch, _config, _exchange, _notifier, tmp_path, caplog,
    ):
        eng = _make_engine(
            monkeypatch, _config, _exchange, _notifier, tmp_path,
            max_size=3,
        )
        try:
            # Pretend a few drops happened during an outage.
            eng._notify_dropped_count = 7
            eng._notify_drop_pending = True

            # Push one normal notification — once the worker drains it,
            # the queue is empty AND drop_pending is True, so the
            # recovery summary should fire.
            with caplog.at_level(logging.WARNING, logger="paper.paper_engine"):
                fn = MagicMock()
                eng._notify(fn, "normal-after-recovery")
                # Wait for the worker to drain. The notify worker is a
                # daemon thread; poll for empty + latch-cleared.
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if not eng._notify_drop_pending:
                        break
                    time.sleep(0.05)
                fn.assert_called_once_with("normal-after-recovery")

            recovery_lines = [
                r for r in caplog.records
                if "Notify queue recovered" in r.getMessage()
            ]
            assert len(recovery_lines) == 1, (
                f"expected exactly one recovery WARNING, got "
                f"{[r.getMessage() for r in recovery_lines]}"
            )
            msg = recovery_lines[0].getMessage()
            assert "7 notification" in msg
            # Latch cleared so a subsequent burst can re-arm.
            assert eng._notify_drop_pending is False
            # Cumulative count is intentionally NOT reset (lifetime metric).
            assert eng._notify_dropped_count == 7
        finally:
            _teardown_engine(eng)

    def test_no_recovery_warning_when_no_drops(
        self, monkeypatch, _config, _exchange, _notifier, tmp_path, caplog,
    ):
        """A normal queue drain with no drops in the window must not
        fire the recovery summary — it would be misleading."""
        eng = _make_engine(
            monkeypatch, _config, _exchange, _notifier, tmp_path,
            max_size=5,
        )
        try:
            with caplog.at_level(logging.WARNING, logger="paper.paper_engine"):
                fn = MagicMock()
                eng._notify(fn, "regular")
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if eng._notify_queue.empty():
                        break
                    time.sleep(0.05)
                # Tiny extra wait to give the worker its loop epilogue.
                time.sleep(0.1)
                fn.assert_called_once_with("regular")

            recovery_lines = [
                r for r in caplog.records
                if "Notify queue recovered" in r.getMessage()
            ]
            assert recovery_lines == [], (
                f"unexpected recovery WARNING when no drops: "
                f"{[r.getMessage() for r in recovery_lines]}"
            )
        finally:
            _teardown_engine(eng)


# ── Concurrency guard: producer + consumer race on full queue ─────────────


class TestProducerConsumerRace:
    """Producer (engine tick) and consumer (notify worker) can race on
    the put_nowait → get_nowait fallback path. The implementation
    catches ``queue.Empty`` and retries the put. Pin that loop here so
    a future refactor doesn't lose the retry."""

    def test_concurrent_full_queue_eventually_drains(
        self, monkeypatch, _config, _exchange, _notifier, tmp_path,
    ):
        eng = _make_engine(
            monkeypatch, _config, _exchange, _notifier, tmp_path,
            max_size=3,
        )
        try:
            # Drive 50 notifications through a real (un-stalled)
            # worker. With cap=3 some will overflow; whatever drops
            # are recorded must equal items_sent - items_dispatched_or_held.
            sent = 0
            dispatched_count = 0

            def _on_call(*_a, **_k):
                nonlocal dispatched_count
                dispatched_count += 1

            fn = MagicMock(side_effect=_on_call)
            for _ in range(50):
                eng._notify(fn)
                sent += 1
            # Wait for the worker to drain everything that survived.
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if eng._notify_queue.empty() and not eng._notify_drop_pending:
                    break
                time.sleep(0.05)

            # Conservation: dispatched + dropped == sent.
            # (Allow drops==0 if the worker happened to be fast enough
            # that overflow never occurred — would still pass.)
            assert dispatched_count + eng._notify_dropped_count == sent, (
                f"dispatched {dispatched_count} + dropped "
                f"{eng._notify_dropped_count} != sent {sent}"
            )
        finally:
            _teardown_engine(eng)
