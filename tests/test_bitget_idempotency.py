"""Tests for the Bitget idempotency / retry path in _with_order_retries.

The v21 fix separates OrderNotFound (safe to retry the place call) from
NetworkError (unknown state — re-raise as ExchangeNetworkError so the
caller can reconcile instead of double-placing).
"""

import sys
from unittest.mock import MagicMock

import ccxt
import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from exchanges.base_exchange import ExchangeNetworkError  # noqa: E402
from exchanges.bitget import BitgetExchange, _with_order_retries  # noqa: E402


def _make_exchange():
    exc = BitgetExchange.__new__(BitgetExchange)
    exc.client = MagicMock()
    exc.api_key = "k"
    exc.api_secret = "s"
    exc.paper = True
    return exc


class TestIdempotencyPaths:

    def test_order_not_found_allows_retry_to_succeed(self, monkeypatch):
        """Idempotency check returns OrderNotFound → place proceeds."""
        exc = _make_exchange()
        exc.client.create_order.side_effect = [
            ccxt.RateLimitExceeded("slow"),
            {"id": "O-1", "status": "closed", "side": "buy", "type": "market",
             "amount": 0.001, "price": None, "filled": 0.001, "timestamp": 1},
        ]
        exc.client.fetch_order.side_effect = ccxt.OrderNotFound("nope")
        monkeypatch.setattr("exchanges.bitget.time.sleep", lambda s: None)

        order = exc.place_market_order("BTC/USD", "buy", 0.001)
        assert order.id == "O-1"
        # fetch_order WAS called (once, before the 2nd attempt).
        assert exc.client.fetch_order.call_count == 1

    def test_network_error_during_idempotency_raises(self, monkeypatch):
        """NetworkError on fetch_order = unknown state → ExchangeNetworkError."""
        exc = _make_exchange()
        exc.client.create_order.side_effect = [
            ccxt.RateLimitExceeded("slow"),
            # 2nd attempt shouldn't be reached because the pre-retry
            # idempotency fetch blows up.
            {"id": "O-2", "status": "closed"},
        ]
        exc.client.fetch_order.side_effect = ccxt.NetworkError("dns fail")
        monkeypatch.setattr("exchanges.bitget.time.sleep", lambda s: None)

        with pytest.raises(ExchangeNetworkError, match="dns fail"):
            exc.place_market_order("BTC/USD", "buy", 0.001)
        # fetch_order hit once before the NetworkError surfaced.
        assert exc.client.fetch_order.call_count == 1

    def test_other_exception_on_fetch_order_proceeds(self, monkeypatch):
        """Any other exception on fetch_order is logged and retry proceeds."""
        exc = _make_exchange()
        exc.client.create_order.side_effect = [
            ccxt.RateLimitExceeded("slow"),
            {"id": "O-3", "status": "closed", "side": "buy", "type": "market",
             "amount": 0.001, "price": None, "filled": 0.001, "timestamp": 1},
        ]
        exc.client.fetch_order.side_effect = ValueError("weird")
        monkeypatch.setattr("exchanges.bitget.time.sleep", lambda s: None)

        order = exc.place_market_order("BTC/USD", "buy", 0.001)
        assert order.id == "O-3"


class TestIdempotencyHit:

    def test_existing_order_with_open_status_returned(self, monkeypatch):
        """When fetch_order returns a dict with a recognised status,
        we return it without placing again."""
        exc = _make_exchange()
        exc.client.create_order.side_effect = [
            ccxt.RateLimitExceeded("slow"),
            # 2nd create_order would normally be called but idempotency
            # should short-circuit us to the existing order.
            RuntimeError("should not be reached"),
        ]
        exc.client.fetch_order.return_value = {
            "id": "EX-1", "status": "open",
        }
        monkeypatch.setattr("exchanges.bitget.time.sleep", lambda s: None)

        result = _with_order_retries(
            "place_market_order",
            exc.client.create_order,
            "BTC/USD", "market", "buy", 0.001,
            client=exc.client, symbol="BTC/USD",
        )
        assert result.get("id") == "EX-1"
