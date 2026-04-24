"""Public chart / candles / price routes extracted from web/app.py.

Routes:
  GET /api/price                      — spot price via shared Bitget client
  GET /api/ticker/{pair}              — full ticker (price + 24h stats) for panel sidebar
  GET /api/chart/{pair}/{timeframe}   — cached OHLCV for the dashboard chart
  GET /api/candles/{pair}/{timeframe} — paginated OHLCV for the backtester

The helper functions (_fetch_ohlcv_page_with_retry, _fetch_ohlcv_range,
_parse_iso_utc, _normalize_chart_pair, _TF_SECONDS) stay in web/app.py
so that ``monkeypatch.setattr(webapp, "_fetch_ohlcv_page_with_retry",
...)`` in tests keeps working — pulling them into a separate module
would break the monkeypatch visibility since chart.py would have its
own local reference after import. The caches + locks follow the same
logic: they're module-level state in web/app.py, imported here.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

import web.app as webapp
from core.user import User
from web.app import (
    _bitget_client,
    _CANDLES_CACHE_LARGE_THRESHOLD,
    _CANDLES_CACHE_MAX,
    _CANDLES_CACHE_TTL,
    _CANDLES_CACHE_TTL_LARGE,
    _CANDLES_MAX_BARS,
    _candles_cache,
    _candles_lock,
    _CHART_CACHE_MAX,
    _CHART_CACHE_TTL,
    _CHART_CACHE_TTL_DEFAULT,
    _CHART_PAIRS_ALLOWLIST,
    _CHART_TIMEFRAMES,
    _chart_cache,
    _chart_lock,
    _normalize_chart_pair,
    _parse_iso_utc,
    _price_lock,
    _request_user,
    _ticker_cache,
    _TICKER_CACHE_MAX,
    _TICKER_CACHE_TTL,
    _ticker_lock,
    limiter,
    registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chart"])


@router.get("/api/price")
@limiter.limit("30/minute")
async def api_price(
    request: Request, user: User = Depends(_request_user),
):
    """Always-on BTC price endpoint. See web/app.py docstring (moved)
    for the rate-limit rationale.

    Fallback scope: when the Bitget ticker fetch fails we try to
    surface a price from the caller's OWN bots' state.json files.
    Filtering on ``user.id`` prevents cross-user leakage — Phase-1
    has a single user so the old unfiltered ``registry.all()`` was
    harmless, but with Phase-3 sessions it would have leaked another
    user's current_price.
    """
    try:
        async with _price_lock:
            ticker = await asyncio.to_thread(_bitget_client.fetch_ticker, "BTCUSD")
        price = ticker.get("last") or ticker.get("close") or 0.0
        return {"price": price, "pair": "BTC/USD", "source": "bitget"}
    except Exception:
        for bot in await registry.all(user_id=user.id):
            state = bot.read_state()
            if state.get("current_price"):
                return {"price": state["current_price"], "pair": "BTC/USD", "source": "bot"}
        # No fallback available within this user's scope. Previously
        # we returned {"price": 0.0, "source": "unavailable"} — the
        # frontend treated that as a real quote and painted an
        # empty chart. 503 is the right signal: upstream is down +
        # no cached backup.
        raise HTTPException(
            status_code=503, detail="price unavailable",
        )


@router.get("/api/ticker/{pair}")
@limiter.limit("60/minute")
async def api_ticker(
    request: Request, pair: str, user: User = Depends(_request_user),
):
    """Full ticker payload for a pair — price, 24h change (absolute +
    percent), 24h volume, high/low, bid/ask.

    Used by the Workspace chart-panel info-sidebar (polled every 5 s).
    A 10 s cache fronts the Bitget call so N panels showing the same
    pair only cost one upstream fetch per window, and Bitget's
    per-IP ticker-endpoint rate-limit isn't burned by simultaneous
    panels.

    Output shape mirrors the ccxt ticker dict after coercion so a
    future Kraken wiring can populate the same keys without touching
    the consumer.
    """
    normalized = _normalize_chart_pair(pair)
    # Audit r1.1-002: refuse unlisted symbols BEFORE they touch the LRU
    # cache or the exchange client. Without this a hostile authenticated
    # client could spam 32+ distinct bogus symbols and evict legitimate
    # entries (or pollute the upstream rate-budget with failed calls).
    if normalized not in _CHART_PAIRS_ALLOWLIST:
        raise HTTPException(
            status_code=400,
            detail=f"pair must be one of {sorted(_CHART_PAIRS_ALLOWLIST)}",
        )
    now = time.time()

    async with _ticker_lock:
        cached = _ticker_cache.get(normalized)
        if cached and cached[0] > now:
            _ticker_cache.move_to_end(normalized)
            return cached[1]
        if cached:
            _ticker_cache.pop(normalized, None)

    # _price_lock serialises every ccxt.fetch_ticker call because the
    # module-level _bitget_client shares state across async workers
    # (see /api/price). Holding the price-lock around to_thread keeps
    # /api/ticker from racing /api/price's cache-miss on the same
    # client. Exception path surfaces as 502 so the SPA can show the
    # sidebar's "—" fallback without a user-visible 5xx alarm.
    try:
        async with _price_lock:
            raw = await asyncio.to_thread(
                _bitget_client.fetch_ticker, normalized.replace("/", ""),
            )
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"ticker fetch failed: {str(e)[:200]}",
        )

    # ccxt's ticker dict has ``last``/``close`` for the mark price,
    # ``change`` for the 24h absolute delta, ``percentage`` for the
    # 24h percent (typically already × 100 — e.g. -0.18 means -0.18 %),
    # ``high``/``low``/``baseVolume`` for the 24h stats, ``bid``/``ask``
    # for the order-book tip, ``timestamp`` in ms. Every field is
    # coerced through ``_ticker_float`` so a missing key surfaces as
    # None instead of crashing the response, and the sidebar renders
    # an em-dash for each gap.
    payload = {
        "pair": normalized,
        "price": _ticker_float(raw.get("last") or raw.get("close")),
        "change_24h": _ticker_float(raw.get("change")),
        "change_pct_24h": _ticker_float(raw.get("percentage")),
        "volume_24h": _ticker_float(raw.get("baseVolume")),
        "high_24h": _ticker_float(raw.get("high")),
        "low_24h": _ticker_float(raw.get("low")),
        "bid": _ticker_float(raw.get("bid")),
        "ask": _ticker_float(raw.get("ask")),
        "timestamp": int(raw["timestamp"]) if raw.get("timestamp") else None,
    }

    async with _ticker_lock:
        _ticker_cache[normalized] = (now + _TICKER_CACHE_TTL, payload)
        _ticker_cache.move_to_end(normalized)
        while len(_ticker_cache) > _TICKER_CACHE_MAX:
            _ticker_cache.popitem(last=False)
    return payload


def _ticker_float(value):
    """Coerce a ccxt ticker field to float or None.

    ccxt returns numerics as float already, but a missing key maps to
    None and some exchanges occasionally emit a string. None-through
    lets the SPA render ``—`` without server-side interpolation.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.get("/api/chart/{pair}/{timeframe}")
@limiter.limit("40/minute")
async def api_chart(
    request: Request, pair: str, timeframe: str, limit: int = 200,
):
    """Public OHLCV endpoint backing the dashboard's live candlestick chart."""
    if timeframe not in _CHART_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"timeframe must be one of {', '.join(_CHART_TIMEFRAMES)}",
        )
    if limit < 10 or limit > 500:
        raise HTTPException(
            status_code=400, detail="limit must be between 10 and 500",
        )

    normalized = _normalize_chart_pair(pair)
    key = (normalized, timeframe, limit)
    now = time.time()

    async with _chart_lock:
        cached = _chart_cache.get(key)
        if cached:
            if cached[0] > now:
                _chart_cache.move_to_end(key)
                return cached[1]
            _chart_cache.pop(key, None)

    try:
        from exchanges.public_exchange import PublicExchange
        client = PublicExchange("bitget")
        raw = await asyncio.to_thread(
            client.get_ohlcv, normalized, timeframe, limit,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Exchange error: {str(e)[:200]}")

    payload = [
        {
            "time":   int(c[0] // 1000),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]) if len(c) > 5 and c[5] is not None else 0.0,
        }
        for c in raw
    ]
    ttl = _CHART_CACHE_TTL.get(timeframe, _CHART_CACHE_TTL_DEFAULT)
    async with _chart_lock:
        _chart_cache[key] = (now + ttl, payload)
        _chart_cache.move_to_end(key)
        while len(_chart_cache) > _CHART_CACHE_MAX:
            _chart_cache.popitem(last=False)
    return payload


@router.get("/api/candles/{pair}/{timeframe}")
@limiter.limit("20/minute")
async def api_candles(
    request: Request,
    pair: str,
    timeframe: str,
    start: str,
    end: str,
    limit: int = 5000,
):
    """Public OHLCV range endpoint backing the client-side backtester.

    _fetch_ohlcv_range is looked up via ``webapp`` so test monkeypatches
    of ``webapp._fetch_ohlcv_page_with_retry`` propagate to the actual
    paginated fetch. Direct imports would capture the pre-patch symbol.
    """
    from datetime import timezone
    if timeframe not in _CHART_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"timeframe must be one of {', '.join(_CHART_TIMEFRAMES)}",
        )
    if limit < 100:
        limit = 100
    if limit > _CANDLES_MAX_BARS:
        limit = _CANDLES_MAX_BARS

    try:
        start_dt = _parse_iso_utc(start)
        end_dt = _parse_iso_utc(end)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid timestamp: {e}")
    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="start must be before end")

    tf_s = webapp._TF_SECONDS[timeframe]
    span_s = (end_dt - start_dt).total_seconds()
    bar_count = int(span_s / tf_s)
    if bar_count > limit:
        trim_bars = bar_count - limit
        start_dt = start_dt.fromtimestamp(
            start_dt.timestamp() + trim_bars * tf_s, tz=timezone.utc,
        )

    normalized = _normalize_chart_pair(pair)
    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()
    key = (normalized, timeframe, start_iso, end_iso, limit)
    now = time.time()

    async with _candles_lock:
        cached = _candles_cache.get(key)
        if cached:
            if cached[0] > now:
                _candles_cache.move_to_end(key)
                return cached[1]
            _candles_cache.pop(key, None)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    try:
        from exchanges.public_exchange import PublicExchange
        client = PublicExchange("bitget")
        raw = await webapp._fetch_ohlcv_range(
            client, normalized, timeframe, start_ms, end_ms,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Exchange error: {str(e)[:200]}")

    payload = [
        {
            "time":   int(c[0] // 1000),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]) if len(c) > 5 and c[5] is not None else 0.0,
        }
        for c in raw
    ]
    gaps = 0
    if len(payload) >= 2:
        for j in range(1, len(payload)):
            if payload[j]["time"] - payload[j - 1]["time"] > tf_s * 2:
                gaps += 1
    if gaps:
        logger.warning(
            "Candle data has %d gaps for %s %s (%d bars, %s→%s)",
            gaps, normalized, timeframe, len(payload),
            start_dt.isoformat(), end_dt.isoformat(),
        )

    ttl = (
        _CANDLES_CACHE_TTL_LARGE
        if limit > _CANDLES_CACHE_LARGE_THRESHOLD
        else _CANDLES_CACHE_TTL
    )
    result = {"candles": payload, "gaps": gaps}
    async with _candles_lock:
        _candles_cache[key] = (now + ttl, result)
        _candles_cache.move_to_end(key)
        while len(_candles_cache) > _CANDLES_CACHE_MAX:
            _candles_cache.popitem(last=False)
    return result
