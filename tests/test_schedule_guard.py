# tests/test_schedule_guard.py
# Tests voor ScheduleGuard, met focus op overnight trading windows.

import os
import sys
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.models import ScheduleConfig, ScheduleWindow
from core.schedule_guard import ScheduleGuard


def _guard(from_time: str, to_time: str, days=None) -> ScheduleGuard:
    days = days or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    cfg = ScheduleConfig(
        timezone="UTC",
        trading_windows=[
            ScheduleWindow.model_validate({"days": days, "from": from_time, "to": to_time})
        ],
        blackout_dates=[],
    )
    return ScheduleGuard(cfg)


def _at(guard: ScheduleGuard, hh: int, mm: int = 0):
    """Patch guard.now() to return a fixed UTC time on a Wednesday."""
    fake = datetime(2026, 4, 15, hh, mm, tzinfo=ZoneInfo("UTC"))  # 2026-04-15 = Wed
    return patch.object(guard, "now", return_value=fake)


class TestOvernightWindow:
    """Trading window van 22:00 tot 06:00 (over middernacht)."""

    def test_late_evening_open(self):
        g = _guard("22:00", "06:00")
        with _at(g, 23, 0):
            assert g.is_open() is True

    def test_early_morning_open(self):
        g = _guard("22:00", "06:00")
        with _at(g, 3, 0):
            assert g.is_open() is True

    def test_midday_closed(self):
        g = _guard("22:00", "06:00")
        with _at(g, 12, 0):
            assert g.is_open() is False

    def test_exact_start_open(self):
        g = _guard("22:00", "06:00")
        with _at(g, 22, 0):
            assert g.is_open() is True

    def test_exact_end_open(self):
        g = _guard("22:00", "06:00")
        with _at(g, 6, 0):
            assert g.is_open() is True

    def test_just_after_end_closed(self):
        g = _guard("22:00", "06:00")
        with _at(g, 6, 1):
            assert g.is_open() is False


class TestSameDayWindow:
    """Sanity check: existing non-overnight windows keep working."""

    def test_within(self):
        g = _guard("09:00", "17:00")
        with _at(g, 12, 0):
            assert g.is_open() is True

    def test_before(self):
        g = _guard("09:00", "17:00")
        with _at(g, 8, 0):
            assert g.is_open() is False

    def test_after(self):
        g = _guard("09:00", "17:00")
        with _at(g, 18, 0):
            assert g.is_open() is False
