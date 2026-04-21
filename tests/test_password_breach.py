"""Tests for core.password_breach — HIBP k-anonymity lookup.

The unit tests mock ``httpx.AsyncClient`` so no real network calls
hit HIBP. A separate integration test under
``tests/integration/test_hibp_live.py`` exercises the live endpoint,
gated on ``RUN_INTEGRATION_TESTS=1`` so it only runs on request.

The repo doesn't run pytest-asyncio (sync-test codebase), so the
async target is driven by ``asyncio.run`` inside plain sync test
functions. That keeps the dependency surface the same as before
this feature — no new pytest plugin.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from core import password_breach

# ``password`` → SHA-1 → 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8.
# The HIBP API would return a body whose lines look like
#   <SUFFIX>:<COUNT>\r\n
# Everything below mimics that shape.
_PASSWORD_PLAINTEXT = "password"
_PASSWORD_PREFIX = "5BAA6"
_PASSWORD_SUFFIX = "1E4C9B93F3F0682250B6CF8331B7EE68FD8"

# A second suffix that is definitely NOT the one we're looking for —
# used to simulate a realistic response body full of other matches.
_OTHER_SUFFIX = "0000000000000000000000000000000000"


def _run(coro):
    """Drive an async callable from a sync test body."""
    return asyncio.run(coro)


class _MockResponse:
    """Minimal httpx.Response stand-in. Only surfaces the attributes
    the production code reads (``status_code``, ``.text``) — we don't
    reach into the real response model."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _MockClient:
    """Context-manager that captures ``get()`` call arguments and
    returns a pre-cooked response. Used in place of
    ``httpx.AsyncClient`` so the test has exact control over the
    transport layer without touching the network."""

    def __init__(self, *, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.get_calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        self.get_calls.append((url, dict(headers or {})))
        if self._raise is not None:
            raise self._raise
        return self._response


def _install_mock_client(monkeypatch, *, response=None, raise_exc=None):
    """Route every ``password_breach`` call through a captured
    ``_MockClient`` instance. Returns the instance so the test can
    assert on call shape (URL, headers)."""
    mock = _MockClient(response=response, raise_exc=raise_exc)

    def _factory(*args, **kwargs):
        return mock

    monkeypatch.setattr(password_breach.httpx, "AsyncClient", _factory)
    return mock


# ── Positive path ────────────────────────────────────────────────────────

class TestPwnedDetected:

    def test_pwned_password_is_detected(self, monkeypatch):
        """Response body contains the expected suffix → True."""
        body = (
            f"{_OTHER_SUFFIX}:1\r\n"
            f"{_PASSWORD_SUFFIX}:42\r\n"
            "AAAA22222222222222222222222222222:3\r\n"
        )
        _install_mock_client(
            monkeypatch, response=_MockResponse(200, body),
        )
        assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is True

    def test_count_of_one_still_rejects(self, monkeypatch):
        """Task-spec threshold: ANY hit rejects (count=1 still rejects)."""
        body = f"{_PASSWORD_SUFFIX}:1\r\n"
        _install_mock_client(monkeypatch, response=_MockResponse(200, body))
        assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is True

    def test_case_sensitive_match_on_upper(self, monkeypatch):
        """Module uppercases the SHA-1 hash; HIBP emits uppercase too,
        so a response whose suffix arrives in uppercase matches."""
        body = f"{_PASSWORD_SUFFIX}:1\r\n"
        _install_mock_client(monkeypatch, response=_MockResponse(200, body))
        assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is True


# ── Negative path ────────────────────────────────────────────────────────

class TestCleanPassword:

    def test_clean_password_not_detected(self, monkeypatch):
        """Response body has other suffixes but not ours → False."""
        body = (
            f"{_OTHER_SUFFIX}:99\r\n"
            "BBBB33333333333333333333333333333:1\r\n"
        )
        _install_mock_client(monkeypatch, response=_MockResponse(200, body))
        assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is False

    def test_empty_plaintext_short_circuits(self, monkeypatch):
        """Empty string never touches HIBP — returns False without
        even constructing the request (defensive; shouldn't happen in
        production because the route has a length check first)."""
        mock = _install_mock_client(
            monkeypatch, response=_MockResponse(200, ""),
        )
        assert _run(password_breach.is_password_pwned("")) is False
        assert mock.get_calls == []

    def test_empty_response_body_returns_false(self, monkeypatch):
        _install_mock_client(monkeypatch, response=_MockResponse(200, ""))
        assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is False


# ── Fail-open paths ──────────────────────────────────────────────────────

class TestFailOpen:

    def test_http_timeout_returns_false(self, monkeypatch, caplog):
        _install_mock_client(
            monkeypatch, raise_exc=httpx.TimeoutException("timeout"),
        )
        with caplog.at_level(logging.WARNING, logger="core.password_breach"):
            result = _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT))
        assert result is False
        assert any(
            "timed out" in rec.getMessage() and "fail-open" in rec.getMessage()
            for rec in caplog.records
        )

    def test_http_5xx_returns_false(self, monkeypatch, caplog):
        _install_mock_client(monkeypatch, response=_MockResponse(500, ""))
        with caplog.at_level(logging.WARNING, logger="core.password_breach"):
            assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is False
        assert any("HTTP 500" in rec.getMessage() for rec in caplog.records)

    def test_http_4xx_returns_false(self, monkeypatch, caplog):
        _install_mock_client(monkeypatch, response=_MockResponse(404, ""))
        with caplog.at_level(logging.WARNING, logger="core.password_breach"):
            assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is False
        assert any("HTTP 404" in rec.getMessage() for rec in caplog.records)

    def test_network_error_returns_false(self, monkeypatch, caplog):
        _install_mock_client(
            monkeypatch,
            raise_exc=httpx.ConnectError("dns fail"),
        )
        with caplog.at_level(logging.WARNING, logger="core.password_breach"):
            assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is False
        assert any("network error" in rec.getMessage() for rec in caplog.records)

    def test_malformed_response_body_returns_false(self, monkeypatch):
        """Body with no colon / no suffix-like lines — must not crash,
        must return False. The parser skips any line that doesn't
        match the suffix."""
        body = "garbage without the expected colon format\r\n"
        _install_mock_client(monkeypatch, response=_MockResponse(200, body))
        assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is False

    def test_unexpected_exception_returns_false(self, monkeypatch, caplog):
        """Any exception outside httpx's hierarchy still falls back
        via the defensive catch-all."""
        _install_mock_client(
            monkeypatch, raise_exc=RuntimeError("totally unexpected"),
        )
        with caplog.at_level(logging.WARNING, logger="core.password_breach"):
            assert _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT)) is False
        assert any(
            "unexpected error" in rec.getMessage() for rec in caplog.records
        )


# ── Request shape ────────────────────────────────────────────────────────

class TestRequestShape:

    def test_padding_header_is_sent(self, monkeypatch):
        """Add-Padding: true defends against response-size traffic
        analysis — asserting on the outbound header keeps a future
        refactor from dropping it silently."""
        mock = _install_mock_client(
            monkeypatch, response=_MockResponse(200, ""),
        )
        _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT))
        assert len(mock.get_calls) == 1
        _url, headers = mock.get_calls[0]
        assert headers.get("Add-Padding") == "true"

    def test_user_agent_is_sent(self, monkeypatch):
        """HIBP etiquette requires an identifying User-Agent."""
        mock = _install_mock_client(
            monkeypatch, response=_MockResponse(200, ""),
        )
        _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT))
        _url, headers = mock.get_calls[0]
        assert headers.get("User-Agent") == "Reverto/1.0"

    def test_url_uses_5char_prefix(self, monkeypatch):
        """k-anonymity invariant: only the first 5 hex chars of the
        SHA-1 hash go over the wire. A regression that sent the full
        hash would defeat the whole protocol."""
        mock = _install_mock_client(
            monkeypatch, response=_MockResponse(200, ""),
        )
        _run(password_breach.is_password_pwned(_PASSWORD_PLAINTEXT))
        url, _ = mock.get_calls[0]
        assert url.endswith(f"/range/{_PASSWORD_PREFIX}")
        # Full hash MUST NOT leak into the URL.
        assert _PASSWORD_SUFFIX not in url


# ── Plaintext leakage guard ──────────────────────────────────────────────

class TestNoPlaintextInLogs:
    """The highest-priority invariant: the module must never write
    the plaintext password to the logger, not even on error paths.
    One slip would undo the entire k-anonymity guarantee.

    Uses a unique synthetic plaintext (not "password") so the
    assertion doesn't false-positive against the literal English
    word "password" that appears in log lines like "allowing
    password (fail-open)".
    """

    # Chosen for uniqueness: contains no English words, no substring
    # that shows up in log message templates. If this string leaks
    # into a log record, it can ONLY have come from the plaintext
    # argument.
    _UNIQUE_PLAINTEXT = "pwnchk_unique_zz9_qwerty"

    def _assert_no_plaintext(self, caplog):
        for rec in caplog.records:
            assert self._UNIQUE_PLAINTEXT not in rec.getMessage()
            if rec.args:
                assert self._UNIQUE_PLAINTEXT not in str(rec.args)

    def test_plaintext_never_logged_on_success(self, monkeypatch, caplog):
        # The real suffix for the unique plaintext doesn't matter —
        # we only care that success + logging don't leak the input.
        _install_mock_client(
            monkeypatch,
            response=_MockResponse(200, "0000:1\r\n"),
        )
        with caplog.at_level(logging.DEBUG, logger="core.password_breach"):
            _run(password_breach.is_password_pwned(self._UNIQUE_PLAINTEXT))
        self._assert_no_plaintext(caplog)

    def test_plaintext_never_logged_on_timeout(self, monkeypatch, caplog):
        _install_mock_client(
            monkeypatch, raise_exc=httpx.TimeoutException("timeout"),
        )
        with caplog.at_level(logging.DEBUG, logger="core.password_breach"):
            _run(password_breach.is_password_pwned(self._UNIQUE_PLAINTEXT))
        self._assert_no_plaintext(caplog)

    def test_plaintext_never_logged_on_5xx(self, monkeypatch, caplog):
        _install_mock_client(monkeypatch, response=_MockResponse(503, ""))
        with caplog.at_level(logging.DEBUG, logger="core.password_breach"):
            _run(password_breach.is_password_pwned(self._UNIQUE_PLAINTEXT))
        self._assert_no_plaintext(caplog)

    def test_plaintext_never_logged_on_network_error(self, monkeypatch, caplog):
        _install_mock_client(
            monkeypatch, raise_exc=httpx.ConnectError("boom"),
        )
        with caplog.at_level(logging.DEBUG, logger="core.password_breach"):
            _run(password_breach.is_password_pwned(self._UNIQUE_PLAINTEXT))
        self._assert_no_plaintext(caplog)
