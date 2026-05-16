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


# ── pt-038 + pt-055 + r2-005 — permanent vs transient classification ─────


class TestCircuitBreakerPermanentState:
    """Class-of-issue: permanent errors (auth, permissions, bad
    symbol) must NOT be treated as transient with auto-recovery
    cooldown.

    Closes pt-038 + pt-055 + r2-005 — three audit-records, one
    issue. The pre-fix breaker treated every exception identically;
    a verlopen API key (``ccxt.AuthenticationError``) drove an
    infinite ``5 fails → 60s cooldown → probe → 401 → reopen``
    cycle instead of a single Telegram alert + stay-open-until-
    operator-acts.

    If any of these tests fail, the breaker has regressed to
    failure-count-only behaviour and the infinite-cycle bug is
    back. Test names start with the assertion they pin so a CI
    failure log immediately surfaces the contract violation.
    """

    def test_record_failure_permanent_opens_immediately(self):
        """One permanent failure trips the breaker straight into
        PERMANENT_OPEN — no threshold counting. Pre-fix the same
        exception class would have counted as 1/5 transient
        failures."""
        b = CircuitBreaker(name="t", failure_threshold=5)
        b.record_failure(permanent=True)
        assert b.is_open() is True
        assert b.is_permanently_open() is True

    def test_record_failure_transient_uses_threshold(self):
        """Backwards compatibility: transient failures still need
        N consecutive hits to open. The default ``permanent=False``
        keyword preserves pre-fix behaviour byte-for-byte for
        callers that were never updated."""
        b = CircuitBreaker(name="t", failure_threshold=5)
        for _ in range(4):
            b.record_failure()
        assert b.is_open() is False  # 4 < 5
        assert b.is_permanently_open() is False
        b.record_failure()
        assert b.is_open() is True
        # KRITIEK: transient OPEN is NOT permanent. Without this
        # assertion, the two states could collapse and a network
        # blip would page the operator.
        assert b.is_permanently_open() is False

    def test_permanent_open_survives_cooldown_elapse(self, monkeypatch):
        """THE class-of-issue assertion. Pre-fix code: cooldown
        elapses → ``is_open()`` resets the counter and admits the
        next call as a probe → probe fails again → infinite cycle.
        Post-fix: PERMANENT_OPEN short-circuits the cooldown-
        elapse branch in ``is_open()``."""
        b = CircuitBreaker(name="t", cooldown_seconds=60.0)
        now = {"t": 1000.0}
        monkeypatch.setattr(
            "core.circuit_breaker.time.time", lambda: now["t"],
        )
        b.record_failure(permanent=True)
        assert b.is_open() is True

        # Simulate one full hour of elapsed wall-time. Pre-fix
        # this would have auto-transitioned to half-open.
        now["t"] = 1000.0 + 3600.0
        assert b.is_open() is True
        assert b.is_permanently_open() is True

    def test_permanent_open_survives_record_success(self):
        """A fluke success — e.g. a cached response on a proxy
        layer that gets through after a 401, or a ccxt-side retry
        that lands on a half-stale endpoint — must NOT clear the
        permanent latch. The operator owns the recovery decision
        via ``reset()``, not the upstream's flake patterns."""
        b = CircuitBreaker(name="t")
        b.record_failure(permanent=True)
        b.record_success()
        # Counter resets, but the latch holds.
        assert b.is_open() is True
        assert b.is_permanently_open() is True

    def test_reset_clears_permanent_latch(self):
        """``reset()`` is the ONLY exit from PERMANENT_OPEN. After
        the operator has rotated the API key (or otherwise
        addressed the permanent fault), ``reset()`` returns the
        breaker to fully CLOSED and the next call passes through
        as if no incident had occurred."""
        b = CircuitBreaker(name="t")
        b.record_failure(permanent=True)
        assert b.is_permanently_open() is True
        b.reset()
        assert b.is_open() is False
        assert b.is_permanently_open() is False

    def test_callback_fires_exactly_once_on_permanent_open(self):
        """Idempotency: 100 permanent failures = ONE callback fire,
        not 100. Without this guard a verlopen key Telegram-spams
        the operator on every retry. Pinned because the spam
        regression would only be visible in a live deploy."""
        calls: list[tuple[str, str]] = []
        b = CircuitBreaker(
            name="t",
            on_permanent_open=lambda n, r: calls.append((n, r)),
        )
        for _ in range(100):
            b.record_failure(permanent=True)
        assert len(calls) == 1, (
            f"pt-038/055/r2-005: callback must be idempotent — "
            f"100 permanent failures fired {len(calls)} callback(s); "
            "expected exactly 1. Operator gets Telegram-spammed "
            "without idempotency."
        )
        assert calls[0][0] == "t"

    def test_callback_exception_does_not_break_breaker(self):
        """Defence-in-depth: a notifier crash MUST NOT prevent the
        breaker from staying permanent-open. The callback is wrapped
        in try/except inside ``_enter_permanent_open``; a raise here
        would propagate up to the call-site and accidentally re-
        open the very condition we're trying to latch."""
        def _bad_callback(name: str, reason: str) -> None:
            raise RuntimeError("notifier exploded")

        b = CircuitBreaker(name="t", on_permanent_open=_bad_callback)
        # Must not raise — the callback exception is swallowed +
        # logged inside the breaker.
        b.record_failure(permanent=True)
        assert b.is_permanently_open() is True

    def test_transient_then_permanent_immediate_open(self):
        """Sequence: 3 transient failures (below threshold), then
        1 permanent. Must open immediately on the permanent — the
        permanent latch ignores the transient counter state.
        Without this, a system that had been hovering near the
        threshold could trigger PERMANENT_OPEN at an unexpected
        moment driven by transient noise."""
        b = CircuitBreaker(name="t", failure_threshold=5)
        for _ in range(3):
            b.record_failure()
        assert b.is_open() is False
        b.record_failure(permanent=True)
        assert b.is_open() is True
        assert b.is_permanently_open() is True


# ── public_exchange callsite classifier wiring ────────────────────────────


class TestPublicExchangePermanentClassifier:
    """Class-of-issue: ``exchanges/public_exchange.py`` must classify
    ccxt exceptions correctly when reporting to its breaker. The
    breaker primitive is domain-agnostic; the exchange-specific
    permanent / transient decision lives at the call-site
    classifier ``_is_permanent_error``.

    Closes pt-038 + pt-055 + r2-005 (call-site half).
    """

    def test_authentication_error_is_permanent(self):
        """A 401 from ccxt is the canonical permanent — verlopen
        API key. Without this classification a verlopen key drives
        infinite retry cycles."""
        from exchanges.public_exchange import _is_permanent_error
        import ccxt

        assert _is_permanent_error(ccxt.AuthenticationError("x")) is True

    def test_permission_denied_is_permanent(self):
        """API key revoked an endpoint permission — operator must
        re-issue / re-authorise. Subclass of AuthenticationError
        in ccxt's hierarchy, but listed explicitly in the
        classifier tuple as documentation-as-code."""
        from exchanges.public_exchange import _is_permanent_error
        import ccxt

        assert _is_permanent_error(ccxt.PermissionDenied("x")) is True

    def test_account_suspended_is_permanent(self):
        """Exchange-side suspension — recovery requires operator-
        side action with the exchange, not retries."""
        from exchanges.public_exchange import _is_permanent_error
        import ccxt

        assert _is_permanent_error(ccxt.AccountSuspended("x")) is True

    def test_bad_symbol_is_permanent(self):
        """Pair typo / unsupported pair — retrying the same string
        will never succeed. Permanent-classify so the breaker
        latches instead of cycling."""
        from exchanges.public_exchange import _is_permanent_error
        import ccxt

        assert _is_permanent_error(ccxt.BadSymbol("x")) is True

    def test_network_error_is_transient(self):
        """Backwards compat: NetworkError stays transient — single
        hit, not yet open. The network can come back."""
        from exchanges.public_exchange import _is_permanent_error
        import ccxt

        assert _is_permanent_error(ccxt.NetworkError("x")) is False

    def test_rate_limit_exceeded_is_transient(self):
        """Conscious decision: RateLimitExceeded suggests sustained
        load but is recoverable on its own (rate window resets).
        Permanent-classify here would page the operator on a
        load-spike that resolves itself in 60s."""
        from exchanges.public_exchange import _is_permanent_error
        import ccxt

        assert _is_permanent_error(ccxt.RateLimitExceeded("x")) is False

    def test_request_timeout_is_transient(self):
        from exchanges.public_exchange import _is_permanent_error
        import ccxt

        assert _is_permanent_error(ccxt.RequestTimeout("x")) is False

    def test_unknown_exception_defaults_transient(self):
        """Conservative bias for the call-site classifier: a non-
        ccxt exception (programming bug, KeyboardInterrupt, etc.)
        is treated as transient. Threshold-based behaviour absorbs
        these without permanently latching the breaker — a bug in
        our own code must NOT permanently disable the exchange
        path on a single trigger."""
        from exchanges.public_exchange import _is_permanent_error

        assert _is_permanent_error(RuntimeError("x")) is False
        assert _is_permanent_error(KeyError("x")) is False
        assert _is_permanent_error(KeyboardInterrupt()) is False

    def test_breaker_factory_wires_permanent_callback(self):
        """``_breaker_for`` must construct breakers with the
        ``on_permanent_open`` callback set so the operator alert
        path actually fires when the latch trips. A future refactor
        that omits the callback would silently re-open the
        Telegram-noise side of the audit."""
        from exchanges.public_exchange import _breaker_for, _BREAKERS

        # Use a fresh exchange name so a previous test's breaker
        # cache entry doesn't poison the assertion.
        _BREAKERS.pop("test-classifier-fixture", None)
        b = _breaker_for("test-classifier-fixture")
        assert b._on_permanent_open is not None, (
            "pt-038/055/r2-005: _breaker_for must wire "
            "on_permanent_open; without the callback, a permanent "
            "error reaches the latch but no operator alert fires."
        )


class TestPermanentOpenProviderDelegation:
    """Phase 2 Task 2.8: the framework's permanent-open callback
    delegates the fan-out to the loaded LiveProvider, and logs
    CRITICAL (never silently) when no provider is available."""

    def test_provider_callback_invoked_on_permanent_open(
        self, mock_live_provider,
    ):
        """When a provider is loaded, the framework callback calls
        provider.on_breaker_permanent_open(name, reason) and does
        NOT fall through to the CRITICAL no-provider branch."""
        from exchanges.public_exchange import _make_permanent_open_callback

        cb = _make_permanent_open_callback("bitget")
        cb("public-bitget", "expired API key")

        mock_live_provider.on_breaker_permanent_open.assert_called_once_with(
            "public-bitget", "expired API key",
        )

    def test_provider_exception_is_contained(self, mock_live_provider):
        """A provider that raises must not propagate back into the
        breaker — the wrapper swallows and logs it."""
        from unittest.mock import MagicMock

        from exchanges.public_exchange import _make_permanent_open_callback

        mock_live_provider.on_breaker_permanent_open = MagicMock(
            side_effect=RuntimeError("plugin blew up")
        )
        cb = _make_permanent_open_callback("kraken")

        # Must not raise.
        cb("public-kraken", "auth failure")

        mock_live_provider.on_breaker_permanent_open.assert_called_once()

    def test_critical_log_when_provider_missing(self, caplog):
        """When load_live_provider() returns None, the callback logs
        a CRITICAL record so the latched breaker is never silent."""
        import logging
        from unittest.mock import patch

        from exchanges.public_exchange import _make_permanent_open_callback

        cb = _make_permanent_open_callback("bitget")

        with patch(
            "core.plugin_loader.load_live_provider",
            return_value=None,
        ):
            with caplog.at_level(
                logging.CRITICAL, logger="exchanges.public_exchange",
            ):
                cb("public-bitget", "no provider scenario")

        crit = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert crit, "expected a CRITICAL log when no provider is available"
        msg = crit[0].getMessage()
        assert "PERMANENT OPEN" in msg
        assert "no LiveProvider is registered" in msg
        assert "public-bitget" in msg
