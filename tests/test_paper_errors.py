"""Tests for paper/errors.py.

Covers the TickerError dataclass + classify_exception + format_log_line
helpers that back the structured error-context path in paper_engine.
The tests use plain Python classes whose __name__ matches the ccxt
class names; classify_exception's MRO walk resolves them identically
to a real ccxt.RateLimitExceeded / ccxt.NetworkError.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from paper.errors import (  # noqa: E402
    TickerError,
    classify_exception,
    format_log_line,
)


# Stand-ins for the ccxt exception hierarchy. Using plain subclasses keeps
# the test independent of a ccxt install and makes the transient-class
# MRO walk explicit at the test site.


class _NetworkError(Exception):
    pass


_NetworkError.__name__ = "NetworkError"


class _RateLimitExceeded(_NetworkError):
    pass


_RateLimitExceeded.__name__ = "RateLimitExceeded"


class _DDoSProtection(_NetworkError):
    pass


_DDoSProtection.__name__ = "DDoSProtection"


class _AuthenticationError(Exception):
    pass


_AuthenticationError.__name__ = "AuthenticationError"


class _OnMaintenance(_NetworkError):
    pass


_OnMaintenance.__name__ = "OnMaintenance"


def _classify(exc, **overrides):
    """Common classify_exception call with sensible defaults so each
    test only pins the fields it cares about."""
    kwargs = dict(
        exchange="bitget",
        endpoint="tick",
        symbol="BTC/USD:USD",
        retry_attempt=1,
        max_retries=5,
    )
    kwargs.update(overrides)
    return classify_exception(exc, **kwargs)


class TestClassifyExceptionTransient:
    """Known ccxt transient classes resolve to is_transient=True plus,
    where applicable, a canonical HTTP status."""

    def test_rate_limit_exceeded_is_transient_with_429(self):
        err = _classify(_RateLimitExceeded("Too Many Requests"))
        assert err.is_transient is True
        assert err.status_code == 429
        assert err.error_class == "RateLimitExceeded"

    def test_network_error_is_transient_without_status(self):
        err = _classify(_NetworkError("connection refused"))
        assert err.is_transient is True
        assert err.status_code is None
        assert err.error_class == "NetworkError"

    def test_ddos_protection_subclass_inherits_transient(self):
        """MRO walk catches subclasses of a transient base class."""
        err = _classify(_DDoSProtection("cloudflare challenge"))
        assert err.is_transient is True

    def test_on_maintenance_maps_to_503(self):
        err = _classify(_OnMaintenance("exchange under maintenance"))
        assert err.is_transient is True
        assert err.status_code == 503


class TestClassifyExceptionPersistent:
    """Anything outside the known transient set defaults to persistent —
    this is the conservative branch so new exception types never silently
    get suppressed."""

    def test_authentication_error_is_persistent_with_401(self):
        err = _classify(_AuthenticationError("invalid API key"))
        assert err.is_transient is False
        assert err.status_code == 401
        assert err.error_class == "AuthenticationError"

    def test_generic_value_error_is_persistent(self):
        err = _classify(ValueError("bad config"))
        assert err.is_transient is False
        assert err.status_code is None
        assert err.error_class == "ValueError"

    def test_attribute_error_is_persistent(self):
        """A bug in our own code (AttributeError during tick) must surface
        as persistent so it does not get suppressed as a retryable."""
        err = _classify(AttributeError("'NoneType' object has no attribute 'x'"))
        assert err.is_transient is False


class TestClassifyExceptionMessage:
    """Message capture preserves the first 200 chars verbatim and keeps
    the rest of the dataclass structured (no leakage into other fields)."""

    def test_message_preserves_first_200_chars(self):
        raw = "x" * 500
        err = _classify(_RateLimitExceeded(raw))
        assert err.message == "x" * 200

    def test_message_under_cap_not_padded(self):
        err = _classify(_RateLimitExceeded("short"))
        assert err.message == "short"

    def test_endpoint_and_symbol_pass_through(self):
        err = _classify(
            _RateLimitExceeded("x"),
            endpoint="fetchOHLCV",
            symbol="ETH/USDT:USDT",
        )
        assert err.endpoint == "fetchOHLCV"
        assert err.symbol == "ETH/USDT:USDT"

    def test_retry_counter_pass_through(self):
        err = _classify(
            _RateLimitExceeded("x"),
            retry_attempt=3,
            max_retries=5,
        )
        assert err.retry_attempt == 3
        assert err.max_retries == 5


class TestClassifyExceptionStatusOverride:
    """When ccxt exposes an http_status attribute we trust it over the
    class-name heuristic for any class not in the hardcoded map."""

    def test_http_status_attr_used_when_class_has_no_mapping(self):
        class _Custom(_NetworkError):
            pass
        _Custom.__name__ = "NetworkError"  # reuse the known name
        exc = _Custom("bad gateway")
        exc.http_status = 502
        err = _classify(exc)
        assert err.status_code == 502

    def test_hardcoded_status_wins_over_http_status_attr(self):
        """RateLimitExceeded is always 429 regardless of what the caller
        stamped onto the exception — the class-level mapping is
        authoritative for classes we explicitly know."""
        exc = _RateLimitExceeded("rate limit")
        exc.http_status = 999
        err = _classify(exc)
        assert err.status_code == 429


class TestFormatLogLine:
    """format_log_line renders key=value pairs in a single grep-friendly
    line. The message field is double-quoted and newline-scrubbed so an
    embedded newline can't fragment the parse."""

    def _sample(self, **overrides) -> TickerError:
        base = dict(
            exchange="bitget",
            endpoint="tick",
            symbol="BTC/USD:USD",
            status_code=429,
            error_class="RateLimitExceeded",
            message="Too Many Requests",
            retry_attempt=2,
            max_retries=5,
            is_transient=True,
        )
        base.update(overrides)
        return TickerError(**base)

    def test_contains_all_structured_fields(self):
        line = format_log_line(self._sample(), bot="rsi_paper_test")
        assert "bot=rsi_paper_test" in line
        assert "exchange=bitget" in line
        assert "endpoint=tick" in line
        assert "symbol=BTC/USD:USD" in line
        assert "status=429" in line
        assert "class=RateLimitExceeded" in line
        assert "retry=2/5" in line
        assert "transient=yes" in line
        assert 'message="Too Many Requests"' in line

    def test_non_transient_renders_as_no(self):
        line = format_log_line(
            self._sample(is_transient=False, error_class="AuthenticationError"),
            bot="mybot",
        )
        assert "transient=no" in line

    def test_none_status_renders_as_na(self):
        line = format_log_line(
            self._sample(status_code=None, error_class="NetworkError"),
            bot="mybot",
        )
        assert "status=n/a" in line

    def test_newline_in_message_scrubbed(self):
        """Multi-line ccxt payloads must collapse to a single line so the
        structured log stays one record per failure."""
        line = format_log_line(
            self._sample(message="line one\nline two"),
            bot="mybot",
        )
        assert "\n" not in line
        assert 'message="line one line two"' in line

    def test_double_quote_in_message_replaced(self):
        """Embedded double-quotes would close the message field early;
        swap to single so downstream parsers stay aligned."""
        line = format_log_line(
            self._sample(message='bad "response" body'),
            bot="mybot",
        )
        assert 'message="bad \'response\' body"' in line
