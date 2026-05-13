"""Tests for core.price_feed — CoinGecko primary + Bitget fallback +
in-process cache.

The module's outbound HTTP and ccxt calls are monkey-patched at the
import boundary (``httpx.get``, ``ccxt.bitget``) so no test ever
touches the network.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
import pytest  # noqa: E402

from core import price_feed  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_price_cache():
    """Drop the module-level cache before AND after every test so
    one test's hit doesn't leak into the next."""
    price_feed._clear_cache()
    yield
    price_feed._clear_cache()


class _FakeResp:
    """Minimal httpx.Response stand-in with the fields the module
    actually reads (``raise_for_status`` + ``json``)."""

    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=MagicMock(), response=MagicMock(),
            )

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class TestIdentityPath:

    def test_usd_returns_identity(self):
        rate, source = price_feed.get_usd_rate("USD")
        assert rate == 1.0
        assert source == "identity"

    def test_usd_lowercase_also_identity(self):
        rate, source = price_feed.get_usd_rate("usd")
        assert rate == 1.0
        assert source == "identity"


class TestCoinGeckoLookup:

    def test_happy_path(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _FakeResp({"bitcoin": {"usd": 65000.0}})

        monkeypatch.setattr(price_feed.httpx, "get", fake_get)
        rate, source = price_feed.get_usd_rate("BTC")
        assert rate == 65000.0
        assert source == "coingecko"
        # Pinned wire-shape assertions — let the spec catch a future
        # refactor that silently drops the User-Agent or the ids
        # param.
        assert captured["url"] == price_feed.COINGECKO_URL
        assert captured["params"] == {"ids": "bitcoin", "vs_currencies": "usd"}
        assert captured["headers"]["User-Agent"] == (
            price_feed.COINGECKO_USER_AGENT
        )

    def test_unknown_currency_falls_through_to_bitget(self, monkeypatch):
        # Currency with no CoinGecko mapping should skip primary AND
        # the Bitget fallback should also fail — we expect
        # PriceFeedError.
        fake_get = MagicMock(side_effect=AssertionError(
            "CoinGecko should NOT be called for an unmapped currency",
        ))
        monkeypatch.setattr(price_feed.httpx, "get", fake_get)
        # Stub Bitget to return None so the cascade ends in error.
        monkeypatch.setattr(
            price_feed, "_bitget_fallback_rate", lambda c: None,
        )
        with pytest.raises(price_feed.PriceFeedError):
            price_feed.get_usd_rate("UNKNOWN")

    def test_http_error_falls_through_to_bitget(self, monkeypatch):
        def fake_get(*args, **kwargs):
            raise httpx.ConnectError("network down")
        monkeypatch.setattr(price_feed.httpx, "get", fake_get)
        monkeypatch.setattr(
            price_feed, "_bitget_fallback_rate", lambda c: 64500.0,
        )
        rate, source = price_feed.get_usd_rate("BTC")
        assert rate == 64500.0
        assert source == "bitget"

    def test_500_falls_through_to_bitget(self, monkeypatch):
        monkeypatch.setattr(
            price_feed.httpx, "get",
            lambda *a, **k: _FakeResp({}, status=500),
        )
        monkeypatch.setattr(
            price_feed, "_bitget_fallback_rate", lambda c: 64500.0,
        )
        rate, source = price_feed.get_usd_rate("BTC")
        assert rate == 64500.0
        assert source == "bitget"

    def test_malformed_json_falls_through(self, monkeypatch):
        # CoinGecko returned 200 but the body has no usd key.
        monkeypatch.setattr(
            price_feed.httpx, "get",
            lambda *a, **k: _FakeResp({"bitcoin": {}}),
        )
        monkeypatch.setattr(
            price_feed, "_bitget_fallback_rate", lambda c: 64500.0,
        )
        rate, source = price_feed.get_usd_rate("BTC")
        assert source == "bitget"
        assert rate == 64500.0

    def test_both_sources_failing_raises(self, monkeypatch):
        monkeypatch.setattr(
            price_feed.httpx, "get",
            lambda *a, **k: _FakeResp({}, status=502),
        )
        monkeypatch.setattr(
            price_feed, "_bitget_fallback_rate", lambda c: None,
        )
        with pytest.raises(price_feed.PriceFeedError):
            price_feed.get_usd_rate("BTC")


class TestCache:

    def test_within_ttl_serves_from_cache(self, monkeypatch):
        call_count = {"n": 0}

        def fake_get(*args, **kwargs):
            call_count["n"] += 1
            return _FakeResp({"bitcoin": {"usd": 65000.0}})

        monkeypatch.setattr(price_feed.httpx, "get", fake_get)
        r1, s1 = price_feed.get_usd_rate("BTC")
        r2, s2 = price_feed.get_usd_rate("BTC")
        assert r1 == r2 == 65000.0
        assert call_count["n"] == 1
        assert s1 == "coingecko"
        # Second hit reports as a cache hit so the snapshot row
        # records "how the rate was obtained on the wire".
        assert s2 == "coingecko_cache"

    def test_expired_entry_refetches(self, monkeypatch):
        call_count = {"n": 0}

        def fake_get(*args, **kwargs):
            call_count["n"] += 1
            return _FakeResp(
                {"bitcoin": {"usd": 65000.0 + call_count["n"]}},
            )

        monkeypatch.setattr(price_feed.httpx, "get", fake_get)
        # Seed cache with a stale entry.
        past = datetime.now(timezone.utc) - timedelta(minutes=10)
        price_feed._CACHE["BTC"] = (50000.0, "coingecko", past)
        rate, source = price_feed.get_usd_rate("BTC")
        assert rate == 65001.0
        assert source == "coingecko"
        assert call_count["n"] == 1


class TestBitgetFallback:

    def test_usdt_short_circuit(self, monkeypatch):
        # ccxt should NOT be called for USDT — we trust 1.0 directly.
        fake_ccxt = MagicMock()
        fake_ccxt.bitget = MagicMock(
            side_effect=AssertionError("ccxt should not be called"),
        )
        monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)
        # Force CoinGecko to fail so we exercise the Bitget path.
        monkeypatch.setattr(
            price_feed.httpx, "get",
            lambda *a, **k: _FakeResp({}, status=500),
        )
        rate, source = price_feed.get_usd_rate("USDT")
        assert rate == 1.0
        assert source == "bitget"

    def test_btc_via_ticker(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.fetch_ticker.return_value = {"last": 64321.0}
        fake_ccxt = MagicMock()
        fake_ccxt.bitget.return_value = fake_client
        monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)
        # CoinGecko 500 → Bitget fallback kicks in.
        monkeypatch.setattr(
            price_feed.httpx, "get",
            lambda *a, **k: _FakeResp({}, status=500),
        )
        rate, source = price_feed.get_usd_rate("BTC")
        assert rate == 64321.0
        assert source == "bitget"
        fake_client.fetch_ticker.assert_called_once_with("BTC/USDT")

    def test_ccxt_raise_returns_none(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.fetch_ticker.side_effect = RuntimeError("auth blew up")
        fake_ccxt = MagicMock()
        fake_ccxt.bitget.return_value = fake_client
        monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)
        assert price_feed._bitget_fallback_rate("BTC") is None
