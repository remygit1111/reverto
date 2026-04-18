"""Historical candle loader for the ML pipeline.

``compute_features`` needs an OHLCV window that ends at the deal's
entry tick. The paper/live engines only store entry PRICE in the DB —
not the candle context — so the nightly pipeline has to re-fetch
historical candles via ccxt.

This module is a thin wrapper around ``exchanges.public_exchange.PublicExchange``
with an on-disk CSV cache so repeated pipeline runs don't hammer the
Bitget public API. Cache granularity is per-day-per-(symbol, timeframe):
one parquet-like CSV file per UTC date, which gives maximum reuse for
feature-engineering runs that touch every deal opened that day.

CSV is used instead of parquet because (a) the files are tiny (<15 KB
for 200 bars), (b) pandas ships parquet via pyarrow which isn't in
``requirements-ml.txt``, and (c) a plain-text cache is trivial to
inspect + diff when a run goes sideways. The format is an
implementation detail; callers only see list[dict].
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "candle_cache"

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
    "4h":  14400,
    "1d":  86400,
}


# ── Cache path ──────────────────────────────────────────────────────────────


def _cache_path(symbol: str, timeframe: str, date: str) -> Path:
    """Cache path per day to maximise hit-rate across a pipeline run.

    Sanitises the symbol so ``BTC/USD:BTC`` doesn't create a directory.
    """
    safe_symbol = symbol.replace("/", "_").replace(":", "_")
    return CACHE_DIR / f"{safe_symbol}_{timeframe}_{date}.csv"


# ── Low-level ccxt fetch ────────────────────────────────────────────────────


def _fetch_candles_ccxt(
    symbol: str,
    timeframe: str,
    since_ms: int,
    limit: int = 200,
) -> list[dict]:
    """Fetch a raw OHLCV window via the public Bitget client.

    Uses ``PublicExchange("bitget")`` so no credentials are required —
    this is read-only public market data. Returns an empty list on
    any failure so the pipeline can skip the sample rather than
    propagating exchange errors.
    """
    try:
        from exchanges.public_exchange import PublicExchange
    except ImportError:  # pragma: no cover — exchanges always present
        logger.error("Cannot import PublicExchange")
        return []

    try:
        exchange = PublicExchange("bitget")
        # PublicExchange.get_ohlcv doesn't expose `since`, so drop down
        # one level to ccxt directly. The symbol mapping still goes
        # through PublicExchange._symbol so we stay consistent with
        # what the paper/live engines feed the engine.
        raw = exchange.client.fetch_ohlcv(
            exchange._symbol(symbol),
            timeframe,
            since=since_ms,
            limit=limit,
        )
    except Exception as e:
        logger.warning("Candle fetch failed (%s %s): %s", symbol, timeframe, e)
        return []

    candles: list[dict] = []
    for c in raw:
        try:
            candles.append({
                "timestamp": int(c[0] / 1000),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        except (IndexError, TypeError, ValueError) as e:
            # Tolerate individual corrupt rows; log once per fetch.
            logger.debug("Skipping malformed candle %r: %s", c, e)
    return candles


# ── Public API ──────────────────────────────────────────────────────────────


def load_candles_for_deal(
    deal: dict,
    symbol: str = "BTC/USD",
    timeframe: str = "1h",
    lookback_periods: int = 100,
    use_cache: bool = True,
) -> list[dict]:
    """Return the OHLCV window that preceded a deal's entry.

    Args:
        deal: dict carrying at minimum ``opened_at`` (epoch seconds or
            ISO datetime string). Extra keys are ignored.
        symbol: Reverto-internal pair identifier; ``PublicExchange``
            handles the ccxt translation (``BTC/USD`` → ``BTCUSD``
            inverse swap for Bitget).
        timeframe: candle resolution key (see ``TIMEFRAME_SECONDS``).
        lookback_periods: maximum number of candles to return. The
            engine's MIN_CANDLES is 78 so 100 gives a small cushion.
        use_cache: read/write the per-day CSV cache. Disable in tests
            that need deterministic fetch behaviour.

    Returns:
        Oldest-first list of candle dicts, each with the keys
        ``timestamp open high low close volume``. Last entry is the
        candle whose close-time is <= the deal's ``opened_at``.
        Empty list on any failure (missing timestamp, unknown
        timeframe, exchange error).
    """
    opened_at = _to_epoch_seconds(deal.get("opened_at"))
    if opened_at is None:
        logger.warning("Deal %r has no parseable opened_at — skip", deal.get("id"))
        return []

    if timeframe not in TIMEFRAME_SECONDS:
        logger.error("Unknown timeframe: %r", timeframe)
        return []

    tf_s = TIMEFRAME_SECONDS[timeframe]
    # Fetch a small buffer above lookback so the tail() filter still
    # yields the full requested window even when the exchange aligns
    # candle close-times to tf boundaries and drops one.
    fetch_limit = lookback_periods + 10
    since = opened_at - (lookback_periods * tf_s)
    since_ms = since * 1000

    cache_date = datetime.fromtimestamp(
        opened_at, tz=timezone.utc,
    ).strftime("%Y-%m-%d")
    cache_file = _cache_path(symbol, timeframe, cache_date)

    if use_cache and cache_file.exists():
        cached = _load_from_cache(cache_file, since, opened_at)
        if cached is not None and len(cached) >= int(lookback_periods * 0.9):
            # Trim to requested window — cache may cover a wider range
            # when the same day contains multiple deals.
            return cached[-lookback_periods:]

    candles = _fetch_candles_ccxt(
        symbol, timeframe, since_ms, limit=fetch_limit,
    )
    if not candles:
        return []

    if use_cache:
        _write_cache(cache_file, candles)

    # Keep only candles at or before the deal's entry — we never want
    # the classifier to see future bars.
    filtered = [c for c in candles if c["timestamp"] <= opened_at]
    return filtered[-lookback_periods:]


def clear_cache() -> int:
    """Remove every cached candle file. Returns the count deleted.
    Useful for tests and for forcing a fresh fetch when you suspect
    the cache has gone stale (e.g. after a ccxt upgrade)."""
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for f in CACHE_DIR.glob("*.csv"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


# ── Internals ───────────────────────────────────────────────────────────────


def _to_epoch_seconds(value) -> Optional[int]:
    """Accept epoch seconds (int/float) OR an ISO 8601 string and
    return an int in seconds. Returns None when the input is
    unparseable so callers skip the sample cleanly."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _load_from_cache(
    cache_file: Path, since: int, until: int,
) -> Optional[list[dict]]:
    """Read cache, filter to [since, until] window, return list[dict].
    Returns None on any error so the caller falls back to a fresh fetch."""
    try:
        df = pd.read_csv(cache_file)
    except Exception as e:
        logger.debug("Cache read failed (%s): %s", cache_file, e)
        return None
    if "timestamp" not in df.columns:
        logger.debug("Cache file %s has no timestamp column", cache_file)
        return None
    window = df[(df["timestamp"] >= since) & (df["timestamp"] <= until)]
    if window.empty:
        return None
    return window.to_dict("records")


def _write_cache(cache_file: Path, candles: list[dict]) -> None:
    """Merge-write candles into the per-day cache. Any existing rows
    with overlapping timestamps are overwritten — this keeps the
    cache compact when multiple deals on the same day trigger partial
    fetches that overlap."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        new_df = pd.DataFrame(candles)
        if cache_file.exists():
            try:
                old_df = pd.read_csv(cache_file)
                combined = (
                    pd.concat([old_df, new_df], ignore_index=True)
                    .drop_duplicates(subset="timestamp", keep="last")
                    .sort_values("timestamp")
                )
            except Exception:
                combined = new_df
        else:
            combined = new_df
        combined.to_csv(cache_file, index=False)
    except Exception as e:
        logger.debug("Cache write failed (%s): %s", cache_file, e)
