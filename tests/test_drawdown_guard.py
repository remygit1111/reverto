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
