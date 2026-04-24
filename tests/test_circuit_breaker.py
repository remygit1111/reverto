"""Tests for core/circuit_breaker.py (audit r1-057).

The breaker is pure-Python with one ``time.time()`` call per
``is_open()`` invocation. All three transitions are covered:
closed → open on threshold-reached, open → half-open after
cooldown, half-open → closed on success or re-open on failure.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core.circuit_breaker import CircuitBreaker, CircuitOpenError  # noqa: E402


def test_starts_closed():
    b = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=1.0)
    assert b.is_open() is False


def test_opens_after_threshold_consecutive_failures():
    b = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=60.0)
    b.record_failure()
    assert b.is_open() is False
    b.record_failure()
    assert b.is_open() is False
    b.record_failure()
    assert b.is_open() is True


def test_success_resets_failure_counter():
    b = CircuitBreaker(name="test", failure_threshold=3, cooldown_seconds=60.0)
    b.record_failure()
    b.record_failure()
    b.record_success()  # counter back to 0
    b.record_failure()
    b.record_failure()
    # Three non-consecutive failures: still closed because success
    # dropped the streak.
    assert b.is_open() is False


def test_half_open_after_cooldown(monkeypatch):
    b = CircuitBreaker(name="test", failure_threshold=2, cooldown_seconds=10.0)
    # Fake clock so the test doesn't sleep.
    now = {"t": 1000.0}
    monkeypatch.setattr(
        "core.circuit_breaker.time.time", lambda: now["t"],
    )
    b.record_failure()
    b.record_failure()
    assert b.is_open() is True

    # Still within cooldown window — stays open.
    now["t"] = 1005.0
    assert b.is_open() is True

    # Window elapsed — breaker auto-transitions to half-open.
    now["t"] = 1010.5
    assert b.is_open() is False


def test_half_open_success_fully_closes(monkeypatch):
    # Open, cooldown elapses, probe call succeeds → back to closed.
    # Use a fake clock so the transition is deterministic.
    b = CircuitBreaker(name="test", failure_threshold=2, cooldown_seconds=10.0)
    now = {"t": 3000.0}
    monkeypatch.setattr(
        "core.circuit_breaker.time.time", lambda: now["t"],
    )
    b.record_failure()
    b.record_failure()
    assert b.is_open() is True

    # Cooldown window ends; is_open() transitions to half-open.
    now["t"] = 3010.5
    assert b.is_open() is False

    # Probe call succeeds → breaker stays closed + counter remains 0.
    b.record_success()
    assert b.is_open() is False
    # And a single fresh failure doesn't re-trip (threshold is 2).
    b.record_failure()
    assert b.is_open() is False


def test_half_open_failure_reopens_for_another_cooldown(monkeypatch):
    b = CircuitBreaker(name="test", failure_threshold=2, cooldown_seconds=10.0)
    now = {"t": 2000.0}
    monkeypatch.setattr(
        "core.circuit_breaker.time.time", lambda: now["t"],
    )
    b.record_failure()
    b.record_failure()
    assert b.is_open() is True

    now["t"] = 2015.0
    # Half-open probe: is_open returns False, counter reset.
    assert b.is_open() is False

    # Probe call fails — fresh cycle of failures.
    b.record_failure()
    assert b.is_open() is False  # just 1 of 2
    b.record_failure()
    # Threshold hit again; breaker opens at the NEW time.
    assert b.is_open() is True


def test_reset_forces_closed():
    b = CircuitBreaker(name="test", failure_threshold=1, cooldown_seconds=60.0)
    b.record_failure()
    assert b.is_open() is True
    b.reset()
    assert b.is_open() is False


def test_circuit_open_error_is_runtime_error():
    assert issubclass(CircuitOpenError, RuntimeError)
    # Handlers that catch CircuitOpenError specifically keep working
    # if a future change ever subclasses further.
    with pytest.raises(CircuitOpenError):
        raise CircuitOpenError("x")
