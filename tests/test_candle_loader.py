"""Tests for ml/candle_loader.py.

The loader has two surfaces: the public ``load_candles_for_deal``
entry point and the ``_fetch_candles_ccxt`` ccxt wrapper. Tests here
monkeypatch the ccxt layer so no network I/O happens and the cache
path is redirected to tmp_path so the real ml/candle_cache/ is never
touched.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ml.candle_loader as loader  # noqa: E402
from ml.candle_loader import (  # noqa: E402
    clear_cache,
    load_candles_for_deal,
    _cache_path,
    _to_epoch_seconds,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

T_OPEN = int(datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc).timestamp())
ONE_HOUR = 3600


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the module-level CACHE_DIR at a tmp_path for the test
    duration so real ml/candle_cache/ stays untouched."""
    cache_dir = tmp_path / "candle_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(loader, "CACHE_DIR", cache_dir)
    yield cache_dir


def _mock_candles(n: int, start_ts: int = T_OPEN - 100 * ONE_HOUR) -> list[dict]:
    """Generate n consecutive 1h candles starting at start_ts."""
    return [
        {
            "timestamp": start_ts + i * ONE_HOUR,
            "open":   80_000.0 + i * 10,
            "high":   80_100.0 + i * 10,
            "low":    79_900.0 + i * 10,
            "close":  80_000.0 + i * 10,
            "volume": 1.0,
        }
        for i in range(n)
    ]


# ── _to_epoch_seconds ───────────────────────────────────────────────────────

class TestEpochCoercion:

    def test_int_passthrough(self):
        assert _to_epoch_seconds(T_OPEN) == T_OPEN

    def test_iso_string_with_tz(self):
        assert _to_epoch_seconds("2026-04-18T12:00:00+00:00") == T_OPEN

    def test_iso_string_without_tz_treated_as_utc(self):
        assert _to_epoch_seconds("2026-04-18T12:00:00") == T_OPEN

    def test_malformed_returns_none(self):
        assert _to_epoch_seconds("not-a-date") is None
        assert _to_epoch_seconds(None) is None
        assert _to_epoch_seconds({"nope": 1}) is None


# ── load_candles_for_deal — guard paths ────────────────────────────────────

class TestLoadCandlesGuards:

    def test_missing_opened_at_returns_empty(self, isolated_cache):
        assert load_candles_for_deal({"id": "X"}, use_cache=False) == []

    def test_unknown_timeframe_returns_empty(self, isolated_cache):
        deal = {"id": "X", "opened_at": T_OPEN}
        assert load_candles_for_deal(deal, timeframe="99y", use_cache=False) == []


# ── load_candles_for_deal — fetch path ─────────────────────────────────────

class TestLoadCandlesFetch:

    def test_fetch_success_returns_candles(self, isolated_cache, monkeypatch):
        mock = _mock_candles(100)

        def _fake_fetch(symbol, timeframe, since_ms, limit=200):
            return mock
        monkeypatch.setattr(loader, "_fetch_candles_ccxt", _fake_fetch)

        deal = {"id": "D-1", "opened_at": T_OPEN}
        out = load_candles_for_deal(deal, lookback_periods=100, use_cache=False)
        assert len(out) == 100
        assert out[0]["timestamp"] < out[-1]["timestamp"]
        assert out[-1]["timestamp"] <= T_OPEN

    def test_fetch_failure_returns_empty(self, isolated_cache, monkeypatch):
        monkeypatch.setattr(
            loader, "_fetch_candles_ccxt",
            lambda *a, **kw: [],
        )
        deal = {"id": "D-1", "opened_at": T_OPEN}
        assert load_candles_for_deal(deal, use_cache=False) == []

    def test_filters_candles_before_entry(self, isolated_cache, monkeypatch):
        """Candles after opened_at must not leak into the feature
        window — the classifier must never see future bars."""
        pre = _mock_candles(60, start_ts=T_OPEN - 60 * ONE_HOUR)
        post = _mock_candles(40, start_ts=T_OPEN + ONE_HOUR)
        monkeypatch.setattr(
            loader, "_fetch_candles_ccxt",
            lambda *a, **kw: pre + post,
        )
        deal = {"id": "D-1", "opened_at": T_OPEN}
        out = load_candles_for_deal(deal, lookback_periods=100, use_cache=False)
        assert all(c["timestamp"] <= T_OPEN for c in out)

    def test_lookback_periods_respected(self, isolated_cache, monkeypatch):
        mock = _mock_candles(200, start_ts=T_OPEN - 200 * ONE_HOUR)
        monkeypatch.setattr(loader, "_fetch_candles_ccxt", lambda *a, **kw: mock)

        deal = {"id": "D-1", "opened_at": T_OPEN}
        out = load_candles_for_deal(deal, lookback_periods=50, use_cache=False)
        assert len(out) == 50


# ── Cache behaviour ─────────────────────────────────────────────────────────

class TestCacheHit:

    def test_write_after_fetch(self, isolated_cache, monkeypatch):
        mock = _mock_candles(100)
        monkeypatch.setattr(loader, "_fetch_candles_ccxt", lambda *a, **kw: mock)

        deal = {"id": "D-1", "opened_at": T_OPEN}
        load_candles_for_deal(deal, lookback_periods=100)

        cache_file = _cache_path("BTC/USD", "1h", "2026-04-18")
        assert cache_file.exists()
        df = pd.read_csv(cache_file)
        assert "timestamp" in df.columns
        assert len(df) == 100

    def test_hit_skips_fetch(self, isolated_cache, monkeypatch):
        """Warm cache + call: the fetcher MUST NOT be invoked."""
        # First call populates cache.
        mock = _mock_candles(100)
        monkeypatch.setattr(loader, "_fetch_candles_ccxt", lambda *a, **kw: mock)
        deal = {"id": "D-1", "opened_at": T_OPEN}
        load_candles_for_deal(deal, lookback_periods=100)

        # Second call: swap the fetcher for a tripwire.
        called = {"n": 0}

        def _boom(*a, **kw):
            called["n"] += 1
            return []
        monkeypatch.setattr(loader, "_fetch_candles_ccxt", _boom)

        out = load_candles_for_deal(deal, lookback_periods=100)
        assert len(out) == 100
        assert called["n"] == 0

    def test_corrupt_cache_falls_back_to_fetch(self, isolated_cache, monkeypatch):
        """A mangled CSV in the cache must NOT take down the loader;
        it should log debug and fetch anew."""
        cache_file = _cache_path("BTC/USD", "1h", "2026-04-18")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("not,a,valid,csv\n\x00\x01\x02")

        mock = _mock_candles(100)
        monkeypatch.setattr(loader, "_fetch_candles_ccxt", lambda *a, **kw: mock)

        deal = {"id": "D-1", "opened_at": T_OPEN}
        out = load_candles_for_deal(deal, lookback_periods=100)
        assert len(out) == 100

    def test_insufficient_cache_coverage_refetches(
        self, isolated_cache, monkeypatch,
    ):
        """Cache hits only partially (< 90 % of requested window) →
        refetch and overwrite. Guards against silently training on
        short windows from a previous partial write."""
        # Seed cache with only 20 candles — well below the 90-of-100
        # threshold for lookback=100.
        sparse = _mock_candles(20, start_ts=T_OPEN - 20 * ONE_HOUR)
        cache_file = _cache_path("BTC/USD", "1h", "2026-04-18")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(sparse).to_csv(cache_file, index=False)

        full = _mock_candles(100)
        called = {"n": 0}

        def _fetch(*a, **kw):
            called["n"] += 1
            return full
        monkeypatch.setattr(loader, "_fetch_candles_ccxt", _fetch)

        deal = {"id": "D-1", "opened_at": T_OPEN}
        out = load_candles_for_deal(deal, lookback_periods=100)
        assert len(out) == 100
        assert called["n"] == 1  # Refetch happened.

    def test_clear_cache_removes_files(self, isolated_cache):
        cache_file = _cache_path("BTC/USD", "1h", "2026-04-18")
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(_mock_candles(5)).to_csv(cache_file, index=False)
        assert cache_file.exists()

        n = clear_cache()
        assert n == 1
        assert not cache_file.exists()


# ── _fetch_candles_ccxt — wraps PublicExchange ─────────────────────────────

class TestFetchCandlesCcxt:

    def test_returns_empty_on_exchange_error(self, monkeypatch):
        """Any exception out of ccxt → empty list, never propagates."""
        class _FakeExchange:
            def __init__(self, *a, **kw): pass
            def _symbol(self, s): return s
            class client:
                @staticmethod
                def fetch_ohlcv(*a, **kw):
                    raise RuntimeError("network down")
        monkeypatch.setattr(
            "exchanges.public_exchange.PublicExchange", _FakeExchange,
        )
        assert loader._fetch_candles_ccxt("BTC/USD", "1h", 0, limit=10) == []

    def test_coerces_ccxt_rows_to_dict(self, monkeypatch):
        """ccxt emits [ts_ms, o, h, l, c, v] arrays; the wrapper must
        produce the engine's dict shape with ts in seconds."""
        class _FakeExchange:
            def __init__(self, *a, **kw): pass
            def _symbol(self, s): return s

            class client:
                @staticmethod
                def fetch_ohlcv(symbol, timeframe, since=None, limit=None):
                    return [
                        [1_700_000_000_000, 80000.0, 80100.0, 79900.0, 80050.0, 1.5],
                    ]
        monkeypatch.setattr(
            "exchanges.public_exchange.PublicExchange", _FakeExchange,
        )
        rows = loader._fetch_candles_ccxt("BTC/USD", "1h", 0, limit=10)
        assert len(rows) == 1
        assert rows[0]["timestamp"] == 1_700_000_000
        assert rows[0]["close"] == 80050.0


# ── Cache path helper ───────────────────────────────────────────────────────

class TestCachePath:

    def test_sanitises_symbol_separators(self, isolated_cache):
        p: Optional[Path] = _cache_path("BTC/USD:BTC", "1h", "2026-04-18")
        assert "/" not in p.name
        assert ":" not in p.name
        assert p.suffix == ".csv"
