"""USD price-feed for portfolio valuation.

Primary source is CoinGecko's public ``simple/price`` endpoint; if
that fails (network error, 5xx, malformed JSON) we fall back to a
public ccxt Bitget ticker. Both sources are cached in-process with a
5 min TTL so a snapshot batch that prices BTC / USDT / USDC in
quick succession only pays one round-trip per currency.

Why two sources:
  CoinGecko is the canonical "what is BTC worth in USD" feed for
  portfolio displays. Its free tier is rate-limited (~30 req/min)
  but has no outage record we can rely on; an hourly scheduler tick
  needs a fallback so a CoinGecko 502 doesn't leave the operator
  staring at a stale snapshot for the next hour.

  Bitget's public ticker is the chosen fallback because we already
  wrap it via ``exchanges.public_exchange.PublicExchange``. It quotes
  BTC/USDT (not BTC/USD), so for non-USDT currencies we accept the
  small drift of USDT ≈ USD ≈ 1.0. The drift is logged in the
  ``rate_source`` column on the snapshot row so operators can spot
  the cases where the fallback fired.

Cache lifetime is short by design — the scheduler tick happens once
per hour, but a manual snapshot triggered mid-tick would otherwise
re-fetch every currency, and a single user pressing the Refresh
button shouldn't quadruple our outbound CoinGecko quota.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class PriceFeedError(Exception):
    """Raised when neither CoinGecko nor the Bitget fallback could
    return a usable USD rate. The caller (scheduler tick / manual
    snapshot route) decides whether to abort the whole batch or skip
    just this currency."""


# In-process cache. Key is the lowercase currency code, value is
# ``(rate, source, fetched_at)``. Module-level by design — the
# scheduler runs as a single long-lived process so the cache survives
# across the inner per-account loop without leaking across hourly
# ticks (the TTL is shorter than the tick interval).
_CACHE: dict[str, tuple[float, str, datetime]] = {}
_CACHE_TTL = timedelta(minutes=5)

# CoinGecko IDs for currencies we know about. Adding a new currency
# means: extend this dict + extend ``_bitget_fallback_rate``'s pair
# resolution + (optionally) extend ``_COINGECKO_IDS`` callers.
_COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "USDT": "tether",
    "USDC": "usd-coin",
}

# CoinGecko URL + request shape. Pinned as a constant so tests can
# assert against the exact wire format we send.
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_USER_AGENT = "Reverto/1.0 (https://reverto.bot)"
_COINGECKO_TIMEOUT_S = 10.0


def _cache_get(currency: str, *, now: datetime) -> Optional[tuple[float, str]]:
    """Return ``(rate, cached_source)`` if a fresh cache entry exists.

    ``cached_source`` is the original ``rate_source`` annotated with
    a ``_cache`` suffix so audit-trail readers can distinguish a
    cache hit from a live fetch. Returning the marked-up source rather
    than the raw one means the snapshot row records *how* the rate
    was actually obtained on the wire.
    """
    entry = _CACHE.get(currency.upper())
    if entry is None:
        return None
    rate, source, fetched_at = entry
    if now - fetched_at >= _CACHE_TTL:
        return None
    suffix = "" if source.endswith("_cache") else "_cache"
    return rate, f"{source}{suffix}"


def _cache_set(currency: str, rate: float, source: str, *, now: datetime) -> None:
    """Store a fresh cache entry. ``source`` is the canonical live-
    fetch source ("coingecko" or "bitget") — the cache suffix is
    appended on read, not on write, so a subsequent live fetch can
    overwrite without leaving a stale ``_cache`` annotation.
    """
    _CACHE[currency.upper()] = (rate, source, now)


def _clear_cache() -> None:
    """Test helper: drop every cached entry so a test that re-uses
    the module sees deterministic behaviour."""
    _CACHE.clear()


def get_usd_rate(currency: str) -> tuple[float, str]:
    """Return ``(rate, source)`` for ``{currency}/USD``.

    Sources, in priority order:
      * ``"identity"``       — currency is USD; rate is exactly 1.0.
      * ``"coingecko"``      — live fetch from CoinGecko.
      * ``"coingecko_cache"``— recent CoinGecko hit replayed from
                                in-process cache.
      * ``"bitget"``         — live fetch from public Bitget ticker.
      * ``"bitget_cache"``   — cached Bitget hit.

    Raises ``PriceFeedError`` when both sources fail. The caller
    (scheduler / route) decides whether to skip the offending account
    or fail the whole batch.

    The currency check is case-insensitive but the comparison key on
    the cache is always uppercase.
    """
    currency_u = (currency or "").strip().upper()
    if not currency_u:
        raise PriceFeedError("currency is empty")
    if currency_u == "USD":
        return 1.0, "identity"

    now = datetime.now(timezone.utc)
    cached = _cache_get(currency_u, now=now)
    if cached is not None:
        return cached

    # Primary: CoinGecko. ``None`` means transient failure — fall
    # through to Bitget.
    rate = _coingecko_lookup(currency_u)
    if rate is not None:
        _cache_set(currency_u, rate, "coingecko", now=now)
        return rate, "coingecko"

    rate = _bitget_fallback_rate(currency_u)
    if rate is not None:
        _cache_set(currency_u, rate, "bitget", now=now)
        return rate, "bitget"

    raise PriceFeedError(
        f"Both CoinGecko and Bitget price feeds failed for {currency_u}",
    )


def _coingecko_lookup(currency: str) -> Optional[float]:
    """One HTTP GET against the CoinGecko simple-price endpoint.

    Returns the USD rate on success, or ``None`` on any failure mode:
    unknown currency (no CoinGecko id mapping), network error, non-2xx
    status, malformed JSON, missing key in the response. Errors are
    logged at WARNING so an operator can correlate a Bitget-fallback
    row with the underlying CoinGecko reason.
    """
    coingecko_id = _COINGECKO_IDS.get(currency)
    if coingecko_id is None:
        logger.debug(
            "No CoinGecko id mapping for %s — skipping primary source",
            currency,
        )
        return None
    try:
        resp = httpx.get(
            COINGECKO_URL,
            params={"ids": coingecko_id, "vs_currencies": "usd"},
            headers={"User-Agent": COINGECKO_USER_AGENT},
            timeout=_COINGECKO_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(
            "CoinGecko lookup failed for %s: %s", currency, e,
        )
        return None
    rate = payload.get(coingecko_id, {}).get("usd")
    if not isinstance(rate, (int, float)) or rate <= 0:
        logger.warning(
            "CoinGecko returned no usable rate for %s: %r",
            currency, payload,
        )
        return None
    return float(rate)


def _bitget_fallback_rate(currency: str) -> Optional[float]:
    """Public Bitget ticker as a fallback USD rate.

    Bitget quotes futures pairs against USDT, so:
      * USDT → return 1.0 with a small but documented drift.
      * BTC, USDC, others → fetch ``<symbol>/USDT`` and treat the
        last price as the USD rate.

    Returns None on any failure (unknown pair, breaker open, network
    error). Errors are logged at WARNING.

    We deliberately use ``ccxt.bitget`` directly here, not the
    project's ``PublicExchange`` wrapper, because:
      * The wrapper's per-exchange circuit breaker is shared with the
        engine's market-data path. A scheduler tick that hits the
        breaker during an outage would tank the engine's chart fetch.
      * ``PublicExchange`` only knows about ``BTC/USD`` symbol
        rewriting — we need plain spot ``BTC/USDT`` here.
    Keeping the fallback isolated is defence-in-depth.
    """
    if currency == "USDT":
        return 1.0
    try:
        import ccxt
    except ImportError:
        logger.warning("ccxt not importable — no Bitget fallback available")
        return None
    pair = f"{currency}/USDT"
    try:
        client = ccxt.bitget({"options": {"defaultType": "spot"}})
        ticker = client.fetch_ticker(pair)
        last = ticker.get("last") if isinstance(ticker, dict) else None
    except Exception as e:  # noqa: BLE001 — ccxt raises many shapes
        logger.warning(
            "Bitget fallback failed for %s: %s", currency, e,
        )
        return None
    if not isinstance(last, (int, float)) or last <= 0:
        logger.warning(
            "Bitget fallback returned no usable last price for %s: %r",
            currency, ticker,
        )
        return None
    return float(last)
