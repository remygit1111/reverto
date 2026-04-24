"""Lightweight circuit-breaker for outbound exchange calls (audit r1-057).

When an upstream dependency (Bitget ticker / OHLCV endpoint) is
unhealthy, a bot that keeps retrying hammers the rate-budget and
delivers nothing but 5xx errors to its own caller. A circuit-breaker
opens after N consecutive failures, rejects new calls immediately
for a cooldown window, then lets a single probe through to test
recovery.

This module provides a pure-Python synchronous implementation with
no external dependencies (no ``pybreaker`` etc.). Thread-safety is
not enforced because the callers that matter (web/routes/chart.py
handlers dispatching to asyncio.to_thread, paper engine tick loops)
all serialise on their own locks. If a future caller invokes the
breaker from truly concurrent Python threads, wrap it in
``threading.Lock`` at the call-site.

Usage:

    from core.circuit_breaker import CircuitBreaker, CircuitOpenError

    _bitget_breaker = CircuitBreaker(name="bitget-public")

    def fetch_ticker(pair):
        if _bitget_breaker.is_open():
            raise CircuitOpenError("bitget-public circuit open")
        try:
            result = ccxt_call(pair)
        except Exception:
            _bitget_breaker.record_failure()
            raise
        _bitget_breaker.record_success()
        return result
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitOpenError(RuntimeError):
    """Raised when a caller tries to pass through an open breaker.

    Distinct exception class so ``except CircuitOpenError`` can
    translate the state to a 503 (upstream unavailable) rather than
    the 502 used for transient ccxt errors.
    """


class CircuitBreaker:
    """Simple failure-count circuit breaker with a fixed cooldown.

    States:
      * CLOSED — calls pass through; failures increment the counter.
      * OPEN   — ``is_open()`` returns True; calls should reject
        immediately. After ``cooldown_seconds`` elapses the breaker
        auto-transitions to half-open.
      * HALF-OPEN (implicit) — the post-cooldown ``is_open()`` call
        returns False, admitting the next call as a probe. The next
        ``record_success`` closes the breaker; the next
        ``record_failure`` re-opens it for another cooldown window.

    Instances are named so log lines + metrics can distinguish
    multiple breakers (e.g. one per exchange) without having to
    plumb custom loggers.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.name = name
        self.failure_threshold = int(failure_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self._failure_count = 0
        self._opened_at: Optional[float] = None

    def is_open(self) -> bool:
        """True while the breaker is blocking calls.

        Reading ``is_open()`` is side-effectful for the half-open
        transition: once the cooldown has elapsed it resets the
        counter so the next call can probe — the caller then runs
        the guarded work and feeds ``record_success`` /
        ``record_failure`` back in.
        """
        if self._opened_at is None:
            return False
        if time.time() - self._opened_at >= self.cooldown_seconds:
            self._opened_at = None
            self._failure_count = 0
            logger.info(
                "CircuitBreaker '%s': cooldown elapsed, half-open probe",
                self.name,
            )
            return False
        return True

    def record_success(self) -> None:
        """Signal that the guarded call succeeded.

        Resets the failure counter to zero; a streak of failures
        must be consecutive to trip the breaker.
        """
        if self._failure_count != 0:
            logger.debug(
                "CircuitBreaker '%s': success after %d failures",
                self.name, self._failure_count,
            )
        self._failure_count = 0

    def record_failure(self) -> None:
        """Signal that the guarded call raised or timed out.

        Increments the counter; trips the breaker on the
        ``failure_threshold``-th consecutive hit.
        """
        self._failure_count += 1
        if (
            self._opened_at is None
            and self._failure_count >= self.failure_threshold
        ):
            self._opened_at = time.time()
            logger.warning(
                "CircuitBreaker '%s' OPEN: %d consecutive failures, "
                "cooldown %.1fs",
                self.name, self._failure_count, self.cooldown_seconds,
            )

    def reset(self) -> None:
        """Force the breaker closed. For tests + operator tooling."""
        self._failure_count = 0
        self._opened_at = None
