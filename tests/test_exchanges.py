# tests/test_exchanges.py
# Integration-ish tests for the BitgetExchange / KrakenExchange order path.
# ccxt is mocked at the client level so no real exchange calls are made;
# the tests exercise the translation from ccxt exceptions into Reverto
# domain exceptions, plus the rate-limit retry/backoff contract.

import os
import sys
from unittest.mock import MagicMock, patch

import ccxt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchanges.base_exchange import (  # noqa: E402
    ExchangeNetworkError,
    InsufficientFundsError,
    RateLimitError,
)
from exchanges.bitget import BitgetExchange  # noqa: E402
from exchanges.kraken import KrakenExchange  # noqa: E402


# ── Construction helpers — bypass ccxt's actual client init ──────────────────

def _make_bitget():
    """Build a BitgetExchange with a stubbed-out ccxt client so we can
    drive `create_order` / `cancel_order` from the test."""
    with patch("exchanges.bitget.ccxt.bitget") as mock_ctor:
        inst = MagicMock()
        mock_ctor.return_value = inst
        exc = BitgetExchange(
            api_key="k", api_secret="s", passphrase="p", paper=False,
        )
    exc.client = MagicMock()
    return exc


def _make_kraken():
    with patch("exchanges.kraken.ccxt.krakenfutures") as mock_ctor:
        inst = MagicMock()
        mock_ctor.return_value = inst
        exc = KrakenExchange(api_key="k", api_secret="s", paper=False)
    exc.client = MagicMock()
    return exc


def _order_dict(**overrides):
    """Minimum ccxt order shape that BitgetExchange._parse_order can
    turn into a Reverto Order dataclass."""
    d = {
        "id": "O-1",
        "side": "buy",
        "type": "market",
        "amount": 0.001,
        "price": 80000.0,
        "status": "open",
        "filled": 0.0,
        "timestamp": 1_700_000_000_000,
    }
    d.update(overrides)
    return d


# ── BitgetExchange: order error handling ────────────────────────────────────

class TestBitgetOrderErrorHandling:

    def test_market_order_insufficient_funds_translated(self):
        """ccxt.InsufficientFunds → InsufficientFundsError, no retry."""
        exc = _make_bitget()
        exc.client.create_order.side_effect = ccxt.InsufficientFunds("balance too low")
        with pytest.raises(InsufficientFundsError, match="balance too low"):
            exc.place_market_order("BTC/USD", "buy", 0.001)
        # Insufficient funds is NOT retried — one attempt only.
        assert exc.client.create_order.call_count == 1

    def test_market_order_rate_limit_retry_then_success(self, monkeypatch):
        """Two rate-limit errors, third attempt succeeds → caller gets Order."""
        exc = _make_bitget()
        exc.client.create_order.side_effect = [
            ccxt.RateLimitExceeded("slow down"),
            ccxt.RateLimitExceeded("slow down"),
            _order_dict(id="O-99"),
        ]
        monkeypatch.setattr("exchanges.bitget.time.sleep", lambda s: None)
        order = exc.place_market_order("BTC/USD", "buy", 0.001)
        assert order.id == "O-99"
        assert exc.client.create_order.call_count == 3

    def test_market_order_rate_limit_exhausted(self, monkeypatch):
        """Three consecutive RateLimitExceeded → RateLimitError raised."""
        exc = _make_bitget()
        exc.client.create_order.side_effect = ccxt.RateLimitExceeded("stop")
        monkeypatch.setattr("exchanges.bitget.time.sleep", lambda s: None)
        with pytest.raises(RateLimitError, match="stop"):
            exc.place_market_order("BTC/USD", "buy", 0.001)
        assert exc.client.create_order.call_count == 3

    def test_market_order_network_error_translated(self):
        """ccxt.NetworkError → ExchangeNetworkError, no retry."""
        exc = _make_bitget()
        exc.client.create_order.side_effect = ccxt.NetworkError("dns fail")
        with pytest.raises(ExchangeNetworkError, match="dns fail"):
            exc.place_market_order("BTC/USD", "buy", 0.001)
        assert exc.client.create_order.call_count == 1

    def test_limit_order_success(self):
        """Happy path: ccxt returns an order dict, we surface Reverto Order."""
        exc = _make_bitget()
        exc.client.create_order.return_value = _order_dict(
            id="L-1", type="limit", price=79500.0,
        )
        order = exc.place_limit_order("BTC/USD", "buy", 0.001, 79500.0)
        assert order.id == "L-1"
        assert order.type == "limit"
        assert order.price == 79500.0

    def test_cancel_order_success(self):
        """cancel_order returns True when ccxt doesn't raise."""
        exc = _make_bitget()
        exc.client.cancel_order.return_value = {"id": "O-1", "status": "canceled"}
        assert exc.cancel_order("O-1", "BTC/USD") is True

    def test_cancel_order_rate_limit_returns_false(self, monkeypatch):
        """cancel_order swallows domain exceptions and returns False —
        callers shouldn't have to try/except for best-effort cleanup."""
        exc = _make_bitget()
        exc.client.cancel_order.side_effect = ccxt.RateLimitExceeded("stop")
        monkeypatch.setattr("exchanges.bitget.time.sleep", lambda s: None)
        assert exc.cancel_order("O-1", "BTC/USD") is False

    def test_cancel_order_base_error_returns_false(self):
        """Non-mapped ccxt error (e.g. OrderNotFound) is logged + False."""
        exc = _make_bitget()
        exc.client.cancel_order.side_effect = ccxt.OrderNotFound("unknown")
        assert exc.cancel_order("O-X", "BTC/USD") is False


# ── KrakenExchange: same contract, same translation ─────────────────────────

class TestKrakenOrderErrorHandling:

    def test_market_order_insufficient_funds_translated(self):
        exc = _make_kraken()
        exc.client.create_order.side_effect = ccxt.InsufficientFunds("low")
        with pytest.raises(InsufficientFundsError):
            exc.place_market_order("BTC/USD", "buy", 0.001)

    def test_market_order_rate_limit_exhausted(self, monkeypatch):
        exc = _make_kraken()
        exc.client.create_order.side_effect = ccxt.RateLimitExceeded("stop")
        monkeypatch.setattr("exchanges.kraken.time.sleep", lambda s: None)
        with pytest.raises(RateLimitError):
            exc.place_market_order("BTC/USD", "buy", 0.001)
        assert exc.client.create_order.call_count == 3

    def test_market_order_network_error_translated(self):
        exc = _make_kraken()
        exc.client.create_order.side_effect = ccxt.NetworkError("timeout")
        with pytest.raises(ExchangeNetworkError):
            exc.place_market_order("BTC/USD", "buy", 0.001)

    def test_limit_order_success(self):
        exc = _make_kraken()
        exc.client.create_order.return_value = _order_dict(
            id="KL-1", type="limit", price=79500.0,
        )
        order = exc.place_limit_order("BTC/USD", "buy", 0.001, 79500.0)
        assert order.id == "KL-1"

    def test_cancel_order_success(self):
        exc = _make_kraken()
        exc.client.cancel_order.return_value = {"id": "KO-1"}
        assert exc.cancel_order("KO-1", "BTC/USD") is True

    def test_cancel_order_rate_limit_returns_false(self, monkeypatch):
        exc = _make_kraken()
        exc.client.cancel_order.side_effect = ccxt.RateLimitExceeded("stop")
        monkeypatch.setattr("exchanges.kraken.time.sleep", lambda s: None)
        assert exc.cancel_order("KO-1", "BTC/USD") is False
