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
        except Exception as e:
            _bitget_breaker.record_failure(
                permanent=_is_permanent_error(e),
            )
            raise
        _bitget_breaker.record_success()
        return result

Permanent vs transient (audit pt-038 / pt-055 / r2-005). The
single-counter design above treats every exception identically: a
``ccxt.AuthenticationError`` on a verlopen API-key trips the same
"5 fails → 60s cooldown → probe → 401 → reopen" cycle as a
network blip. The 60s cooldown is the wrong remedy for a non-
self-healing condition; the breaker becomes a noise generator
instead of a safety primitive. The ``permanent=True`` arm to
``record_failure`` short-circuits the threshold and trips a
non-self-healing PERMANENT_OPEN state that survives cooldown
elapse + accidental success, releasable only via ``reset()`` (or
service restart). Operators are notified once via the optional
``on_permanent_open`` callback so a verlopen key surfaces as a
single Telegram alert rather than infinite retries.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class CircuitOpenError(RuntimeError):
    """Raised when a caller tries to pass through an open breaker.

    Distinct exception class so ``except CircuitOpenError`` can
    translate the state to a 503 (upstream unavailable) rather than
    the 502 used for transient ccxt errors.
    """


class CircuitBreaker:
    """Failure-count circuit breaker with permanent-state escape hatch.

    States:
      * CLOSED — calls pass through; failures increment the counter.
      * OPEN   — ``is_open()`` returns True; calls should reject
        immediately. After ``cooldown_seconds`` elapses the breaker
        auto-transitions to half-open.
      * HALF-OPEN (implicit) — the post-cooldown ``is_open()`` call
        returns False, admitting the next call as a probe. The next
        ``record_success`` closes the breaker; the next
        ``record_failure`` re-opens it for another cooldown window.
      * PERMANENT_OPEN (audit pt-038 / pt-055 / r2-005) — entered
        by a single ``record_failure(permanent=True)`` call. Does
        NOT auto-recover via cooldown; ``record_success`` does NOT
        clear it. Only ``reset()`` (operator action) returns the
        breaker to CLOSED. ``is_open()`` returns True; the
        companion probe ``is_permanently_open()`` distinguishes
        this state from a transient OPEN so callers / operators
        can render a different message ("operator action
        required") and surface in metrics.

    Instances are named so log lines + metrics can distinguish
    multiple breakers (e.g. one per exchange) without having to
    plumb custom loggers.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        on_permanent_open: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """Construct a breaker.

        ``on_permanent_open`` (audit pt-038 / pt-055 / r2-005) is
        an optional callback fired ONCE when the breaker first
        transitions into PERMANENT_OPEN. Signature:
        ``callback(breaker_name: str, reason: str) -> None``. The
        callback runs synchronously inside the ``record_failure``
        path; any exception it raises is caught + logged and does
        NOT propagate (a notifier crash must not prevent the
        breaker from staying tripped). Callbacks must be cheap +
        non-blocking — wire heavy work via a queue.
        """
        self.name = name
        self.failure_threshold = int(failure_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self._failure_count = 0
        self._opened_at: Optional[float] = None
        # Audit pt-038 / pt-055 / r2-005: PERMANENT_OPEN is a
        # separate latch so cooldown-recovery + record_success
        # both no-op while it's set. Default False — pre-fix
        # callers that never pass ``permanent=True`` see the
        # original behaviour byte-for-byte.
        self._permanent_open: bool = False
        self._on_permanent_open = on_permanent_open

    def is_open(self) -> bool:
        """True while the breaker is blocking calls.

        Reading ``is_open()`` is side-effectful for the half-open
        transition: once the cooldown has elapsed it resets the
        counter so the next call can probe — the caller then runs
        the guarded work and feeds ``record_success`` /
        ``record_failure`` back in.

        PERMANENT_OPEN short-circuits the cooldown-recovery
        branch: a permanently-open breaker NEVER auto-transitions
        to half-open, regardless of how much wall-time has
        elapsed.
        """
        # Audit pt-038 / pt-055 / r2-005: PERMANENT_OPEN takes
        # precedence over the cooldown-elapse arm. Without this
        # short-circuit, ``time.time() - self._opened_at >=
        # self.cooldown_seconds`` would eventually fire and reset
        # the breaker — exactly the infinite-cycle regression
        # this state was added to prevent.
        if self._permanent_open:
            return True
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

    def is_permanently_open(self) -> bool:
        """True iff the breaker is in PERMANENT_OPEN state.

        Distinct from ``is_open()`` so callers can:

        * Render a different operator-facing message
          ("operator action required" vs "retry shortly").
        * Return a different HTTP status code or Retry-After.
        * Distinguish the two states in Prometheus / log
          metrics without parsing log strings.

        Audit pt-038 / pt-055 / r2-005.
        """
        return self._permanent_open

    def record_success(self) -> None:
        """Signal that the guarded call succeeded.

        Resets the failure counter to zero; a streak of failures
        must be consecutive to trip the breaker.

        KRITIEK: success does NOT clear PERMANENT_OPEN (audit
        pt-038 / pt-055 / r2-005). Once a breaker has gone
        permanent — typically because a non-self-healing error
        like ``ccxt.AuthenticationError`` triggered the latch —
        the only way out is ``reset()``. This protects against
        upstream flakes where a 401 is followed by a 200 by
        accident (e.g. a cached response on a proxy layer or a
        ccxt-side retry that lands on a half-stale endpoint).
        """
        if self._failure_count != 0:
            logger.debug(
                "CircuitBreaker '%s': success after %d failures",
                self.name, self._failure_count,
            )
        self._failure_count = 0
        # Permanent-open survives success — the operator owns the
        # recovery decision. A success while permanent is logged
        # at DEBUG so the pattern is visible to operators chasing
        # an incident, but the latch stays set.

    def record_failure(self, *, permanent: bool = False) -> None:
        """Signal that the guarded call raised or timed out.

        ``permanent=False`` (default) → transient mode: increments
        the counter; trips the breaker into OPEN on the
        ``failure_threshold``-th consecutive hit, with auto-
        recovery after ``cooldown_seconds``.

        ``permanent=True`` (audit pt-038 / pt-055 / r2-005) →
        permanent mode: trips the breaker into PERMANENT_OPEN
        immediately, regardless of how many transient failures
        preceded. Caller is expected to pass ``permanent=True``
        only for errors that genuinely require operator action
        (verlopen API key, permission revocation, account
        suspension) — the classifier lives in the caller, not
        here, because exception-class heuristics belong with the
        domain (ccxt-aware code in ``exchanges/``) not with the
        domain-agnostic breaker primitive.
        """
        if permanent:
            self._enter_permanent_open(reason="permanent error")
            return
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

    def _enter_permanent_open(self, reason: str) -> None:
        """Transition to PERMANENT_OPEN. Idempotent.

        Idempotency matters: 100 consecutive AuthenticationErrors
        must trigger ONE callback fire, not 100 — otherwise the
        operator gets Telegram-spammed by a single verlopen key.
        """
        if self._permanent_open:
            return
        self._permanent_open = True
        # Stamp ``_opened_at`` too so any code path that reads it
        # for "is breaker tripped" sees the trip; ``is_open()``
        # short-circuits on ``_permanent_open`` first so the
        # cooldown-elapse logic never runs against this stamp.
        self._opened_at = time.time()
        logger.error(
            "CircuitBreaker '%s' PERMANENT OPEN: %s. "
            "Operator action required; auto-recovery disabled.",
            self.name, reason,
        )
        if self._on_permanent_open is not None:
            try:
                self._on_permanent_open(self.name, reason)
            except Exception as e:  # noqa: BLE001
                # Callback failure must NOT prevent the breaker
                # from staying permanent. The contract is "the
                # breaker is the source of truth"; a notifier
                # crash is a separate concern with its own
                # observability path (the .exception log line
                # below).
                logger.exception(
                    "CircuitBreaker '%s' on_permanent_open callback "
                    "raised: %s", self.name, e,
                )

    def reset(self) -> None:
        """Force the breaker fully closed.

        Clears BOTH the transient counter / opened_at AND the
        permanent latch. Use after operator-action (key rotation,
        config fix, exchange re-onboarding) or in tests. This is
        the ONLY exit from PERMANENT_OPEN.
        """
        self._failure_count = 0
        self._opened_at = None
        self._permanent_open = False
