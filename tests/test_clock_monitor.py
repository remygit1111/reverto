"""Tests for core/clock_monitor.py.

The monitor fails OPEN on exchange errors and caches its answer for
60s. The tests drive the clock deterministically so the cache TTL
doesn't make them flaky.
"""

import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from core.clock_monitor import ClockMonitor  # noqa: E402


def _exchange_with_skew(skew_seconds: float):
    """Build a mock exchange whose client.fetch_time returns a ms epoch
    offset by ``skew_seconds`` from the local clock."""
    mock = MagicMock()
    mock.client.fetch_time = MagicMock(
        return_value=int((time.time() + skew_seconds) * 1000)
    )
    return mock


class TestClockMonitor:

    def test_within_tolerance(self):
        mon = ClockMonitor(_exchange_with_skew(0.5), max_skew_seconds=5.0)
        skew, ok = mon.check()
        assert ok is True
        assert abs(skew) < 1.0

    def test_outside_tolerance(self):
        mon = ClockMonitor(_exchange_with_skew(30.0), max_skew_seconds=5.0)
        skew, ok = mon.check()
        assert ok is False
        assert skew > 5.0

    def test_fails_open_on_exchange_error(self):
        """If fetch_time raises, the monitor returns last-known-good
        (initially 0.0, ok=True) rather than locking out trading."""
        mock = MagicMock()
        mock.client.fetch_time = MagicMock(side_effect=RuntimeError("boom"))
        mon = ClockMonitor(mock, max_skew_seconds=5.0)
        skew, ok = mon.check()
        assert ok is True
        assert skew == 0.0

    def test_cache_avoids_repeat_calls(self):
        """Second call within TTL must not hit fetch_time again."""
        ex = _exchange_with_skew(0.0)
        mon = ClockMonitor(ex, max_skew_seconds=5.0)
        mon.check()
        mon.check()
        assert ex.client.fetch_time.call_count == 1

    def test_missing_fetch_time_skips_check(self):
        """Some exchange wrappers don't expose fetch_time; those must
        be treated as 'no data → fail open'."""
        mock = MagicMock()
        # Remove fetch_time so hasattr returns False.
        del mock.client.fetch_time
        mon = ClockMonitor(mock, max_skew_seconds=5.0)
        skew, ok = mon.check()
        assert ok is True
        assert skew == 0.0
