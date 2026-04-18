"""Tests for ClockMonitor wiring into LiveEngine._tick.

These verify that a clock-skew trigger makes LiveEngine skip order
actions for the affected tick while still writing state so the
dashboard reflects the paused condition.
"""

import sys
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
from live.live_engine import LiveEngine  # noqa: E402


@pytest.fixture
def live_config():
    return BotConfig(
        name="SkewBot",
        mode=Mode.LIVE,
        exchange=Exchange.BITGET,
        pair="BTC/USD",
        dca=DCAConfig(
            enabled=True, base_order_size=0.0005,
            max_orders=3, order_spacing_pct=1.5, multiplier=1.0,
        ),
        take_profit=TakeProfitConfig(enabled=True, target_pct=3.0),
    )


@pytest.fixture
def mock_exchange():
    mock = MagicMock()
    mock.get_ticker.return_value = MagicMock(mark_price=50000.0, last=50000.0)
    mock.get_ohlcv.return_value = [
        [1_000_000 + i * 60_000, 50000.0, 50100.0, 49900.0, 50050.0, 1.0]
        for i in range(100)
    ]
    return mock


@pytest.fixture
def mock_notifier():
    n = MagicMock()
    for m in [
        "notify_startup", "notify_shutdown", "notify_entry",
        "notify_dca", "notify_take_profit", "notify_stop_loss",
        "notify_error", "notify_stop", "notify_restart",
    ]:
        setattr(n, m, MagicMock())
    return n


class TestLiveEngineClockSkew:

    def test_clock_monitor_attached(
        self, live_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """ClockMonitor is instantiated with the passed tolerance
        and exposed as `engine.clock_monitor`."""
        eng = LiveEngine(
            config=live_config, exchange=mock_exchange, notifier=mock_notifier,
            state_file=str(tmp_path / "sk.state.json"),
            slug="skewbot", clock_skew_tolerance=2.5,
        )
        try:
            assert eng.clock_monitor is not None
            assert eng.clock_monitor.max_skew_seconds == 2.5
            assert eng._paused_by_clock_skew is False
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_skew_flips_paused_flag(
        self, live_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """When ClockMonitor reports NOT ok, the engine flips the
        paused-by-skew flag and skips entry signals for that tick."""
        eng = LiveEngine(
            config=live_config, exchange=mock_exchange, notifier=mock_notifier,
            state_file=str(tmp_path / "sk.state.json"),
            slug="skewbot",
        )
        try:
            # Force the monitor to report "out of tolerance" without
            # going through the real fetch_time code.
            eng.clock_monitor.check = lambda now=None: (12.5, False)

            # _check_entry must not fire when paused-by-skew.
            called = {"n": 0}
            orig_check_entry = eng._check_entry
            def _spy(price):
                called["n"] += 1
                return orig_check_entry(price)
            eng._check_entry = _spy

            eng._tick()
            assert eng._paused_by_clock_skew is True
            # _check_entry must not have been called this tick.
            assert called["n"] == 0
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_skew_within_tolerance_resumes(
        self, live_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """Once skew returns to within tolerance the flag clears."""
        eng = LiveEngine(
            config=live_config, exchange=mock_exchange, notifier=mock_notifier,
            state_file=str(tmp_path / "sk.state.json"),
            slug="skewbot",
        )
        try:
            eng._paused_by_clock_skew = True
            eng.clock_monitor.check = lambda now=None: (0.1, True)
            eng._tick()
            assert eng._paused_by_clock_skew is False
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_paused_state_in_write(
        self, live_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """state.json must include paused_by_clock_skew so the UI can
        surface the pause."""
        import json
        state_file = tmp_path / "sk.state.json"
        eng = LiveEngine(
            config=live_config, exchange=mock_exchange, notifier=mock_notifier,
            state_file=str(state_file), slug="skewbot",
        )
        try:
            eng._paused_by_clock_skew = True
            eng._write_state(50_000.0, is_open=True)
            data = json.loads(state_file.read_text())
            assert data.get("paused_by_clock_skew") is True
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)
