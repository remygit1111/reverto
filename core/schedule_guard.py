# core/schedule_guard.py
# Determines whether Reverto is allowed to start new deals
# based on the trading schedule defined in the bot configuration.
# Running deals (DCA, TP, SL) are always allowed regardless of schedule.

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from config.models import ScheduleConfig

# Map short day names to Python weekday numbers (Monday = 0)
DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2,
    "thu": 3, "fri": 4, "sat": 5, "sun": 6
}


class ScheduleGuard:
    def __init__(self, schedule: ScheduleConfig):
        self.schedule = schedule
        self.tz = ZoneInfo(schedule.timezone)

    def now(self) -> datetime:
        """Returns the current time in the configured timezone."""
        return datetime.now(self.tz)

    def is_open(self) -> bool:
        """
        Returns True if Reverto is allowed to start new deals right now.
        Checks:
        1. Current date is not a blackout date
        2. Current day and time fall within a trading window
        """
        now = self.now()
        today_str = now.strftime("%Y-%m-%d")
        current_day = now.weekday()
        current_time = now.strftime("%H:%M")

        # Check blackout dates first
        if today_str in self.schedule.blackout_dates:
            return False

        # No trading windows configured → 24/7 operation. Blackout dates
        # still override so operators can exclude specific days even on a
        # windowless schedule.
        if not self.schedule.trading_windows:
            return True

        # Check if current time falls within any trading window
        for window in self.schedule.trading_windows:
            window_days = [DAY_MAP[d.lower()] for d in window.days]
            if current_day in window_days and self._time_in_window(
                current_time, window.from_time, window.to_time
            ):
                return True

        return False

    @staticmethod
    def _time_in_window(current: str, ft: str, tt: str) -> bool:
        """True als `current` (HH:MM) binnen [ft, tt] valt.

        Ondersteunt overnight windows: als ft > tt (b.v. 22:00 → 06:00)
        is het venster geldig als current >= ft OF current <= tt.
        Voor day-spanning windows checkt is_open() alleen de start-dag —
        een operator die Mon 22:00 → Tue 06:00 wil moet beide dagen in
        window.days zetten.
        """
        if ft <= tt:
            return ft <= current <= tt
        return current >= ft or current <= tt

    def status(self, is_open: Optional[bool] = None) -> dict:
        """
        Returns a detailed status dict for logging and Telegram notifications.

        is_open: optional cached result from a prior is_open() call.
        When provided, avoids a redundant evaluation on the same tick.
        If None, is_open() is called internally.
        """
        now = self.now()
        # Use the cached value if provided — prevents a third is_open() call
        # in the same tick when called from _check_schedule_transition()
        open_ = is_open if is_open is not None else self.is_open()
        next_open = self._next_open(now)

        return {
            "is_open": open_,
            "current_time": now.strftime("%Y-%m-%d %H:%M %Z"),
            "next_open": next_open,
            "message": (
                "🟢 Reverto active — new deals allowed"
                if open_ else
                f"🔴 Reverto resting — next window: {next_open}"
            )
        }

    def _next_open(self, now: datetime) -> str:
        """
        Finds the next trading window opening time from now.
        Looks up to 7 days ahead.
        """
        for days_ahead in range(8):
            future = now + timedelta(days=days_ahead)
            future_day = future.weekday()
            future_date_str = future.strftime("%Y-%m-%d")

            # Skip blackout dates
            if future_date_str in self.schedule.blackout_dates:
                continue

            for window in self.schedule.trading_windows:
                window_days = [DAY_MAP[d.lower()] for d in window.days]
                if future_day in window_days:
                    # If today, only count if opening time is still ahead
                    if days_ahead == 0:
                        current_time = now.strftime("%H:%M")
                        if window.from_time > current_time:
                            return f"{future_date_str} {window.from_time}"
                    else:
                        return f"{future_date_str} {window.from_time}"

        return "No upcoming trading window found"
