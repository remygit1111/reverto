"""Tests for core/drawdown_guard.py.

The guard is a standalone observer — no threads, no I/O, no exchange
client. These tests pin the contract that the paper_engine and
LiveEngine rely on: disabled → no-op, peak-tracking is monotonic,
trigger is idempotent, reset() clears every internal piece of state.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from core.drawdown_guard import DrawdownGuard, DrawdownGuardConfig


class TestDrawdownGuard:

    def test_disabled_never_triggers(self):
        """When ``enabled=False`` the guard must stay silent regardless
        of how catastrophic the value series is. Callers rely on this
        so they can wire the guard unconditionally."""
        g = DrawdownGuard(DrawdownGuardConfig(enabled=False))
        assert g.update(100.0) is False
        assert g.update(50.0) is False
        assert g.update(10.0) is False
        assert not g.is_triggered
        assert g.trigger_reason is None

    def test_tracks_peak_and_triggers_on_drawdown(self):
        """After establishing a peak of 100, a 5% draw stays silent but
        an 11% draw flips ``is_triggered`` to True."""
        g = DrawdownGuard(DrawdownGuardConfig(
            enabled=True, max_drawdown_pct=10.0,
        ))
        assert g.update(100.0) is False  # first reading = peak
        assert g.update(95.0) is False   # 5% drawdown
        assert not g.is_triggered

        assert g.update(89.0) is True    # 11% drawdown → trigger
        assert g.is_triggered
        assert g.trigger_reason is not None
        assert "11.00%" in g.trigger_reason

    def test_trigger_is_idempotent(self):
        """Once triggered, subsequent update() calls return True without
        overwriting the original trigger_reason — the engine should see
        a stable reason string for logging/notification."""
        g = DrawdownGuard(DrawdownGuardConfig(
            enabled=True, max_drawdown_pct=5.0,
        ))
        g.update(100.0)
        g.update(90.0)
        original_reason = g.trigger_reason

        assert g.update(50.0) is True
        assert g.update(100.0) is True  # even a recovery keeps us triggered
        assert g.trigger_reason == original_reason

    def test_new_peak_updates_baseline(self):
        """Peak should track the maximum value seen. A climb to 110
        means a later drop to 100 is measured against 110, not the
        initial 100."""
        g = DrawdownGuard(DrawdownGuardConfig(
            enabled=True, max_drawdown_pct=10.0,
        ))
        g.update(100.0)
        g.update(110.0)  # new peak
        assert g.peak_value == 110.0
        assert g.update(100.0) is False  # ~9.1% from 110 — under threshold

    def test_reset_clears_triggered_state(self):
        """reset() should drop triggered + reason + peak so the next
        update establishes a fresh baseline."""
        g = DrawdownGuard(DrawdownGuardConfig(
            enabled=True, max_drawdown_pct=5.0,
        ))
        g.update(100.0)
        g.update(90.0)  # triggers
        assert g.is_triggered

        g.reset()
        assert not g.is_triggered
        assert g.trigger_reason is None
        assert g.peak_value is None

        # Next reading is the new peak.
        g.update(80.0)
        assert g.peak_value == 80.0

    def test_zero_peak_does_not_crash(self):
        """Pathological but possible: a first reading of zero means a
        percentage drawdown isn't definable. The guard must return
        False without dividing by zero."""
        g = DrawdownGuard(DrawdownGuardConfig(
            enabled=True, max_drawdown_pct=10.0,
        ))
        assert g.update(0.0) is False
        assert g.update(-5.0) is False

    def test_config_metric_stop_action_are_accessible(self):
        """Engines read cfg.metric and cfg.action to decide which value
        to feed and how to react. Sanity-check the defaults + explicit
        values round-trip through Pydantic."""
        cfg_default = DrawdownGuardConfig()
        assert cfg_default.metric == "equity"
        assert cfg_default.action == "pause"

        cfg = DrawdownGuardConfig(
            enabled=True, max_drawdown_pct=12.5,
            metric="balance", action="stop",
        )
        assert cfg.metric == "balance"
        assert cfg.action == "stop"
        assert cfg.max_drawdown_pct == 12.5

    def test_invalid_action_rejected_by_config(self):
        """DrawdownGuardConfig is strict — an unknown action literal
        must raise a validation error."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DrawdownGuardConfig(action="annihilate")  # type: ignore[arg-type]


class TestDrawdownGuardPersistence:
    """Persistence is the fix for the v20 HIGH-LIVE finding: a bot
    restart would otherwise reset _peak_value to None, silently
    disabling the kill-switch for the next leg down."""

    def test_to_dict_captures_state(self):
        g = DrawdownGuard(DrawdownGuardConfig(enabled=True, max_drawdown_pct=10.0))
        g.update(100.0)
        g.update(90.0)  # triggers
        blob = g.to_dict()
        assert blob["peak_value"] == 100.0
        assert blob["triggered"] is True
        assert "Drawdown" in blob["trigger_reason"]

    def test_roundtrip_restores_state(self):
        """to_dict → from_dict round-trip preserves every critical field
        so a restarted engine sees the same peak + triggered state."""
        g1 = DrawdownGuard(DrawdownGuardConfig(enabled=True, max_drawdown_pct=5.0))
        g1.update(100.0)
        g1.update(94.0)  # triggers
        blob = g1.to_dict()

        g2 = DrawdownGuard(DrawdownGuardConfig(enabled=True, max_drawdown_pct=5.0))
        g2.from_dict(blob)

        assert g2.peak_value == 100.0
        assert g2.is_triggered is True
        assert g2.trigger_reason == g1.trigger_reason

        # New update() call must see the triggered state and short-
        # circuit to True without re-anchoring the peak.
        assert g2.update(200.0) is True
        assert g2.peak_value == 100.0

    def test_from_dict_ignores_missing_keys(self):
        """Older state.json snapshots (pre-persistence) produce empty
        or partial dicts. from_dict must treat those as 'no data' and
        reset to a clean slate without raising."""
        g = DrawdownGuard(DrawdownGuardConfig(enabled=True, max_drawdown_pct=10.0))
        g.from_dict({})
        assert g.peak_value is None
        assert g.is_triggered is False

    def test_from_dict_handles_null_peak(self):
        g = DrawdownGuard(DrawdownGuardConfig(enabled=True, max_drawdown_pct=10.0))
        g.from_dict({"peak_value": None, "triggered": False, "trigger_reason": None})
        assert g.peak_value is None


class TestDrawdownGuardThreadSafety:
    """The guard now takes an internal lock on update() / reset() /
    to_dict() / from_dict(). Spin up a few threads and verify no
    exception/inconsistent state emerges."""

    def test_concurrent_updates_do_not_raise(self):
        import threading

        g = DrawdownGuard(DrawdownGuardConfig(enabled=True, max_drawdown_pct=50.0))

        def worker(values):
            for v in values:
                g.update(v)

        t1 = threading.Thread(target=worker, args=(list(range(100, 200)),))
        t2 = threading.Thread(target=worker, args=(list(range(150, 250)),))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Peak must be monotonically non-decreasing; the exact value
        # depends on thread interleave but must be at least 249.
        assert g.peak_value is not None
        assert g.peak_value >= 249
