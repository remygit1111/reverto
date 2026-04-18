"""Clock-skew monitor against an exchange's reported time.

Exchanges reject orders whose client-side timestamp drifts more than a
few seconds from the server clock. Before Phase 3 real-order execution
the live engine should confirm the local host's NTP sync is actually
working — a silently drifting clock can lead to every order being
rejected or, worse, stale orders firing at the wrong price level.

The monitor caches its last check for 60s so we don't hammer the
exchange's fetch_time endpoint on every tick. On any fetch failure
the monitor fails OPEN (returns within_tolerance=True) — refusing to
trade just because the time endpoint was slow is worse than trading
with a stale skew reading.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Cache TTL — a 60s window catches clock drift drift fast enough for
# practical live trading without adding appreciable request overhead.
_CACHE_TTL_SECONDS = 60.0


class ClockMonitor:
    """Exchange clock-skew watchdog.

    Usage:

        monitor = ClockMonitor(exchange)

        # In the tick loop, before placing orders:
        skew, ok = monitor.check()
        if not ok:
            logger.error("Refusing to trade, clock skew %.2fs", skew)
            return
    """

    def __init__(
        self,
        exchange: Any,
        max_skew_seconds: float = 5.0,
    ) -> None:
        self.exchange = exchange
        self.max_skew_seconds = max_skew_seconds
        self._last_check: float = 0.0
        self._last_skew: float = 0.0
        self._last_ok: bool = True

    def check(self, now: Optional[float] = None) -> tuple[float, bool]:
        """Return (skew_seconds, within_tolerance).

        Cached for ``_CACHE_TTL_SECONDS``. Fails open on fetch errors:
        if the exchange's time endpoint is unavailable we return the
        last known good reading rather than locking out trading.
        """
        now = now if now is not None else time.time()
        if now - self._last_check < _CACHE_TTL_SECONDS:
            return self._last_skew, self._last_ok

        try:
            client = getattr(self.exchange, "client", None)
            if client is None or not hasattr(client, "fetch_time"):
                logger.debug(
                    "Exchange has no fetch_time — clock check skipped"
                )
                self._last_check = now
                self._last_skew = 0.0
                self._last_ok = True
                return 0.0, True

            exchange_ms = client.fetch_time()
            local_ms = int(time.time() * 1000)
            skew_seconds = (exchange_ms - local_ms) / 1000.0

            self._last_check = now
            self._last_skew = skew_seconds
            self._last_ok = abs(skew_seconds) <= self.max_skew_seconds

            if not self._last_ok:
                logger.error(
                    "Clock skew %.2fs exceeds tolerance %.2fs",
                    skew_seconds, self.max_skew_seconds,
                )
            return skew_seconds, self._last_ok

        except Exception as e:
            # Fail open — better to trade on a stale-but-last-known-good
            # reading than to block trading because the time endpoint
            # briefly 500'd.
            logger.warning("Clock check failed: %s", str(e)[:200])
            self._last_check = now
            return self._last_skew, True

    @property
    def last_skew(self) -> float:
        return self._last_skew
