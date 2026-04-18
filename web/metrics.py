"""Prometheus metrics for Reverto.

Defines every counter/gauge/histogram in one module so engines and
routes import the same instances — critical for Prometheus clients
which register metrics in a global registry and will complain loudly
if the same metric name is redefined.

Metrics:

    reverto_bot_ticks_total{bot_slug,mode}      — Counter
    reverto_bot_tick_errors_total{bot_slug,kind}— Counter
    reverto_bot_balance_btc{bot_slug}           — Gauge
    reverto_bot_open_deals{bot_slug}            — Gauge
    reverto_bot_drawdown_pct{bot_slug}          — Gauge
    reverto_tick_duration_seconds{bot_slug}     — Histogram
    reverto_orders_placed_total{bot_slug,side,type,status} — Counter

The paper / live engines call the helpers below to record each
measurement so no module outside web/metrics.py knows the metric
names directly. That keeps label cardinality + naming changes a
single-file edit.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Bounded error-class mapping used by ``classify_error``. Keeping this
# small set prevents Prometheus label-cardinality explosion — a hostile
# exchange emitting exotic exception types would otherwise create a new
# time-series for each name. ccxt is imported lazily to keep
# web/metrics.py importable in environments (e.g. test hosts) without
# the full exchange stack.
_CCXT_ERROR_MAP: dict[type, str] = {}


def _load_ccxt_error_map() -> dict[type, str]:
    """Populate the ccxt exception → label map on first use."""
    global _CCXT_ERROR_MAP
    if _CCXT_ERROR_MAP:
        return _CCXT_ERROR_MAP
    try:
        import ccxt  # noqa: WPS433 — lazy
    except ImportError:
        return _CCXT_ERROR_MAP

    _CCXT_ERROR_MAP.update({
        ccxt.RateLimitExceeded:   "rate_limit",
        ccxt.InsufficientFunds:   "insufficient_funds",
        ccxt.NetworkError:        "network",
        ccxt.DDoSProtection:      "ddos",
        ccxt.ExchangeNotAvailable: "exchange_unavailable",
        ccxt.InvalidOrder:        "invalid_order",
        ccxt.OrderNotFound:       "order_not_found",
    })
    return _CCXT_ERROR_MAP


def classify_error(exc: BaseException) -> str:
    """Map an exception instance to a bounded-set label.

    Returns one of:
      ``rate_limit``, ``insufficient_funds``, ``network``, ``ddos``,
      ``exchange_unavailable``, ``invalid_order``, ``order_not_found``,
      ``not_implemented``, ``value_error``, ``key_error``, ``other``.
    """
    for exc_type, label in _load_ccxt_error_map().items():
        if isinstance(exc, exc_type):
            return label
    if isinstance(exc, NotImplementedError):
        return "not_implemented"
    if isinstance(exc, ValueError):
        return "value_error"
    if isinstance(exc, KeyError):
        return "key_error"
    return "other"

# ── Tick loop ────────────────────────────────────────────────────────

bot_ticks_total = Counter(
    "reverto_bot_ticks_total",
    "Total tick iterations executed by an engine.",
    ["bot_slug", "mode"],
)

bot_tick_errors_total = Counter(
    "reverto_bot_tick_errors_total",
    "Exceptions caught in the engine tick loop.",
    ["bot_slug", "kind"],
)

tick_duration_seconds = Histogram(
    "reverto_tick_duration_seconds",
    "Tick processing time in seconds.",
    ["bot_slug"],
    # Buckets tuned for the default 10s poll — most ticks should land
    # well under a second; anything above 5s is a real problem.
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ── Account + positions ──────────────────────────────────────────────

bot_balance_btc = Gauge(
    "reverto_bot_balance_btc",
    "Current engine balance in BTC (realised only — excludes open-deal PnL).",
    ["bot_slug"],
)

bot_open_deals = Gauge(
    "reverto_bot_open_deals",
    "Number of currently-open deals.",
    ["bot_slug"],
)

bot_drawdown_pct = Gauge(
    "reverto_bot_drawdown_pct",
    "Current drawdown percentage from the running peak.",
    ["bot_slug"],
)

# ── Orders ───────────────────────────────────────────────────────────

orders_placed_total = Counter(
    "reverto_orders_placed_total",
    "Orders placed (paper fills also count here for parity).",
    ["bot_slug", "side", "type", "status"],
)


# ── Helpers — prefer these over touching the raw metric objects ──────

def record_tick(bot_slug: str, mode: str) -> None:
    bot_ticks_total.labels(bot_slug=bot_slug, mode=mode).inc()


def record_tick_error(bot_slug: str, exc_or_kind) -> None:
    """Record a tick-loop exception.

    Accepts either an exception instance (preferred — classified to a
    bounded label via ``classify_error``) or a raw label string (for
    callers that already have a label in hand). String callers are
    responsible for keeping cardinality bounded themselves.
    """
    if isinstance(exc_or_kind, BaseException):
        kind = classify_error(exc_or_kind)
    else:
        kind = str(exc_or_kind)
    bot_tick_errors_total.labels(bot_slug=bot_slug, kind=kind).inc()


def set_balance(bot_slug: str, balance_btc: float) -> None:
    bot_balance_btc.labels(bot_slug=bot_slug).set(balance_btc)


def set_open_deals(bot_slug: str, n: int) -> None:
    bot_open_deals.labels(bot_slug=bot_slug).set(n)


def set_drawdown_pct(bot_slug: str, pct: float) -> None:
    bot_drawdown_pct.labels(bot_slug=bot_slug).set(pct)


def record_order(
    bot_slug: str, side: str, order_type: str, status: str
) -> None:
    orders_placed_total.labels(
        bot_slug=bot_slug, side=side, type=order_type, status=status,
    ).inc()
