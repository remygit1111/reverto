"""Tests for notifications/telegram.py — persistent-error message format.

The notifier's send() is patched to capture the rendered message body
rather than hitting the real Telegram API. Each test pins one property
of the persistent-error layout: severity emoji, Reason / Context /
Action fields, exchange-specific status page, and auth-error pivot.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from notifications.telegram import TelegramNotifier  # noqa: E402
from paper.errors import TickerError  # noqa: E402


@pytest.fixture
def notifier(monkeypatch):
    """TelegramNotifier wired against fake token/chat env — send() is
    replaced per-test with a capturing stub."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    return TelegramNotifier()


def _ticker_err(**overrides) -> TickerError:
    """Build a TickerError with sensible defaults so each test only pins
    the fields it cares about. Mirrors the 429-after-5-retries example
    the user pasted in the task brief."""
    base = dict(
        exchange="bitget",
        endpoint="tick",
        symbol="BTC/USD:USD",
        status_code=429,
        error_class="RateLimitExceeded",
        message="Too Many Requests",
        retry_attempt=5,
        max_retries=5,
        is_transient=True,
    )
    base.update(overrides)
    return TickerError(**base)


def _capture_send(notifier):
    """Replace notifier.send with a stub that stores the last message
    body. Returns a dict so tests can read the captured value."""
    cap: dict[str, str] = {}

    def _send(message: str):
        cap["body"] = message
    notifier.send = _send
    return cap


class TestPersistentErrorSeverity:
    """Severity emoji + state label split by is_transient. Transient
    exhaustion renders as ⚠️ degraded because the engine is still
    retrying; non-transient renders as ⛔ stopped because no further
    retry will help."""

    def test_transient_exhausted_renders_as_degraded(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent("RSI Paper Test", _ticker_err())
        body = cap["body"]
        assert "⚠️" in body
        assert "Bot degraded" in body
        assert "⛔" not in body

    def test_non_transient_renders_as_stopped(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "RSI Paper Test",
            _ticker_err(
                error_class="AuthenticationError",
                status_code=401,
                is_transient=False,
            ),
        )
        body = cap["body"]
        assert "⛔" in body
        assert "Bot stopped" in body
        assert "⚠️" not in body


class TestPersistentErrorFields:
    """Every persistent-error message must carry Bot / Reason / Context /
    Action so the operator can triage without cross-referencing logs."""

    def test_contains_bot_reason_context_action(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent("RSI Paper Test", _ticker_err())
        body = cap["body"]
        assert "Bot     : RSI Paper Test" in body
        assert "Reason  :" in body
        assert "Context :" in body
        assert "Action  :" in body

    def test_context_mirrors_endpoint_symbol_and_retries(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "MyBot",
            _ticker_err(
                endpoint="fetchTicker",
                symbol="ETH/USDT:USDT",
                retry_attempt=5,
                max_retries=5,
            ),
        )
        assert "fetchTicker ETH/USDT:USDT — 5/5 retries failed" in cap["body"]

    def test_rate_limit_reason_mentions_429(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent("MyBot", _ticker_err())
        assert "429 Too Many Requests" in cap["body"]

    def test_network_error_reason_is_generic_network(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "MyBot",
            _ticker_err(
                error_class="NetworkError",
                status_code=None,
                message="connection refused",
            ),
        )
        assert "network/timeout" in cap["body"].lower()

    def test_authentication_error_reason_mentions_401(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "MyBot",
            _ticker_err(
                error_class="AuthenticationError",
                status_code=401,
                is_transient=False,
            ),
        )
        assert "authentication" in cap["body"].lower()
        assert "401" in cap["body"]

    def test_unknown_error_class_surfaces_class_name(self, notifier):
        """An exception type we don't have a dedicated message for must
        still surface class + truncated message in Reason so the user
        gets enough to start triage."""
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "MyBot",
            _ticker_err(
                error_class="SomeWeirdException",
                message="completely unexpected thing",
                is_transient=False,
            ),
        )
        assert "SomeWeirdException" in cap["body"]


class TestPersistentErrorAction:
    """Action line steers the user toward the likely fix. Transient
    failures point at the exchange's status page; auth failures point
    at API-key permissions; non-transient otherwise at portal logs."""

    def test_bitget_transient_action_points_at_status_page(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent("MyBot", _ticker_err())
        assert "status.bitget.com" in cap["body"]
        assert "Restart bot via portal" in cap["body"]

    def test_binance_transient_action_points_at_status_page(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "MyBot",
            _ticker_err(exchange="binance"),
        )
        assert "binance.statuspage.io" in cap["body"]

    def test_unknown_exchange_transient_action_is_generic(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "MyBot",
            _ticker_err(exchange="bybit"),
        )
        body = cap["body"]
        assert "Bybit" in body
        # No hardcoded status URL for bybit — fall back to generic.
        assert "status.bybit" not in body

    def test_auth_error_action_mentions_api_key(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "MyBot",
            _ticker_err(
                error_class="AuthenticationError",
                status_code=401,
                is_transient=False,
            ),
        )
        body = cap["body"]
        assert "API-key" in body
        # Auth errors must NOT point users at the exchange status page —
        # the problem is local (invalid creds), not a Bitget outage.
        assert "status.bitget.com" not in body

    def test_non_transient_unknown_action_points_at_portal_logs(self, notifier):
        cap = _capture_send(notifier)
        notifier.notify_error_persistent(
            "MyBot",
            _ticker_err(
                error_class="ValueError",
                status_code=None,
                is_transient=False,
            ),
        )
        assert "portal logs" in cap["body"].lower()


class TestPersistentErrorRespectsNotifyOn:
    """notify_on filter must apply to the new persistent path the same
    way it applies to the legacy notify_error — a user who opted out of
    'error' events must not receive the degraded/stopped message."""

    def test_event_error_disabled_suppresses_persistent_notify(
        self, monkeypatch,
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        n = TelegramNotifier(notify_on=["startup"])  # no "error"
        cap = _capture_send(n)
        n.notify_error_persistent("MyBot", _ticker_err())
        assert cap == {}, "error-disabled users must not receive persistent-notify"
