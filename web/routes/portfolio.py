"""Portfolio routes — total + per-account + per-bot views.

The Portfolio tab in the navbar reads from these endpoints. Rows are
captured every hour by the standalone ``main_scheduler.py`` process;
operators can additionally trigger a manual capture (rate-limited to
1 per rolling hour per user) via POST /api/portfolio/snapshot/manual.

Endpoint map:
  GET    /api/portfolio/latest                — latest snapshot per
                                                 account + totals
  GET    /api/portfolio/history?range=...     — time-series for charts
  GET    /api/portfolio/per-bot               — per-bot breakdown
                                                 (live bots only)
  POST   /api/portfolio/snapshot/manual       — operator-triggered
                                                 capture, 1/hour/user

Rate-limit design:
  The slowapi limits are the standard 60/minute (read) and 10/minute
  (write) buckets that mirror /api/exchange-accounts. The manual-
  snapshot 1/hour gate is enforced **inside** the handler via
  ``portfolio_store.manual_allowed`` rather than slowapi, because we
  need to surface the next-allowed-at timestamp to the operator —
  slowapi's response is just a generic 429 with no hint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core import (
    exchange_account_store,
    markets,
    paths,
    portfolio_store,
    price_feed,
)
from core.exchange_clients import (
    ExchangeClientError,
    build_authenticated_exchange,
)
from core.database import get_db
from core.user import User
from web.app import _audit, _request_actor, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["portfolio"])


# ── Helpers ────────────────────────────────────────────────────────────────


# Mapping of the ``range`` query param to a UTC timedelta. ``"all"`` is
# expressed as None so the store fetches every row.
_RANGE_DELTAS: dict[str, Optional[timedelta]] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


def _market_label(exchange_type: str, market_type: str) -> str:
    """Best-effort human label for the per-account table. Falls back
    to the raw key on lookup failure so an unknown combo still shows
    something readable instead of a blank cell."""
    try:
        return markets.get_market_config(exchange_type, market_type)[
            "display_label"
        ]
    except (ValueError, KeyError):
        return market_type


def _format_captured_at_iso(captured_at: Optional[str]) -> Optional[str]:
    """Convert a DB timestamp string into ISO 8601 with explicit
    UTC marker.

    SQLite's ``datetime('now')`` DEFAULT writes ``"2026-05-13
    20:00:02"`` — space-separated, no timezone suffix. JavaScript's
    ``new Date()`` parses that inconsistently across browsers (some
    treat it as local time, some as UTC). Convert to
    ``"2026-05-13T20:00:02Z"`` so the wire format is unambiguous
    and the portfolio chart's axis ticks + crosshair tooltip stay
    in sync.

    Pass-through:
      * ``""`` / ``None`` — return unchanged (caller decides how
        to render an absent timestamp).
      * Already contains a ``+`` offset or a trailing ``Z`` —
        return unchanged (don't double-stamp).
      * Already T-separated but no offset — append ``Z``.

    Defensive against a future schema migration that switches the
    DB column to ISO-on-write: any input that already looks
    well-formed is left alone.
    """
    if not captured_at:
        return captured_at
    s = captured_at.strip()
    if "T" not in s:
        s = s.replace(" ", "T", 1)
    if s.endswith("Z") or "+" in s or "-" in s[10:]:
        # Already carries explicit offset (Z, +HH:MM, or a
        # negative offset somewhere after the date part).
        return s
    return s + "Z"


def _live_bot_slugs(user_id: int) -> set[str]:
    """Read every YAML under ``config/bots/<user_id>/`` and return the
    slugs whose mode is ``live``.

    We scan YAML rather than join against a "bots" DB table because
    there is no such table — bot config is the YAML source of truth.
    Paper bots are intentionally excluded from the per-bot breakdown
    (paper has no real exposure).

    Malformed YAML is skipped silently — the broader system already
    surfaces those errors via load_bot_config; this helper doesn't
    need to second-guess.
    """
    user_dir = paths.user_bots_dir(user_id)
    if not user_dir.exists():
        return set()
    live: set[str] = set()
    for yaml_path in user_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        block = data.get("bot") if isinstance(data, dict) else None
        if not isinstance(block, dict):
            continue
        mode = block.get("mode")
        if isinstance(mode, str) and mode.lower() == "live":
            live.add(yaml_path.stem)
    return live


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("/api/portfolio/latest")
@limiter.limit("60/minute")
async def get_portfolio_latest(
    request: Request,
    user: User = Depends(_request_user),
):
    """Latest snapshot per account, plus totals + the manual-snapshot
    gate state. Empty-state friendly: a user with no snapshots yet
    gets ``{"accounts": [], "totals": {"balance_usd": 0.0, ...}}`` so
    the frontend can render its "No snapshots yet" placeholder
    without a 404.

    Wire shape:
      {
        "accounts": [
          {
            "account_id": int,
            "alias": str,
            "exchange_type": "bitget" | "kraken",
            "market_type": str,
            "market_label": str,
            "balance_native": float,
            "currency": str,
            "balance_usd": float,
            "usd_rate": float,
            "rate_source": str,
            "captured_at": ISO-8601,
            "source": "auto" | "manual"
          },
          ...
        ],
        "totals": {
          "balance_usd": float,
          "by_currency": {"BTC": float, "USDT": float, ...},
          "as_of": ISO-8601 | None
        },
        "manual_allowed": bool,
        "manual_next_allowed_at": ISO-8601 | None
      }
    """
    latest = portfolio_store.latest_per_account(user.id)

    accounts_out: list[dict] = []
    total_usd = 0.0
    by_currency: dict[str, float] = {}
    most_recent: Optional[str] = None
    for row in latest:
        market_label = _market_label(
            row["exchange_type"], row["market_type"],
        )
        accounts_out.append({
            "account_id": row["exchange_account_id"],
            "alias": row["alias"],
            "exchange_type": row["exchange_type"],
            "market_type": row["market_type"],
            "market_label": market_label,
            "balance_native": row["balance_native"],
            "currency": row["currency"],
            "balance_usd": row["balance_usd"],
            "usd_rate": row["usd_rate"],
            "rate_source": row["rate_source"],
            "captured_at": _format_captured_at_iso(row["captured_at"]),
            "source": row["source"],
        })
        total_usd += row["balance_usd"]
        by_currency[row["currency"]] = (
            by_currency.get(row["currency"], 0.0) + row["balance_native"]
        )
        if most_recent is None or row["captured_at"] > most_recent:
            most_recent = row["captured_at"]

    allowed, next_at = portfolio_store.manual_allowed(user.id)
    return {
        "accounts": accounts_out,
        "totals": {
            "balance_usd": total_usd,
            "by_currency": by_currency,
            "as_of": _format_captured_at_iso(most_recent),
        },
        "manual_allowed": allowed,
        "manual_next_allowed_at": (
            _format_captured_at_iso(next_at.isoformat())
            if next_at is not None else None
        ),
    }


@router.get("/api/portfolio/history")
@limiter.limit("60/minute")
async def get_portfolio_history(
    request: Request,
    range: str = Query(default="7d"),
    user: User = Depends(_request_user),
):
    """Time-series of total USD value over the requested range.

    Each emitted point aggregates across accounts for one
    ``captured_at`` instant — so the chart shows the operator's total
    portfolio, not per-account lines. Per-account values are still
    in the per_account dict so a hover tooltip can show the split.

    Valid range values: ``24h``, ``7d``, ``30d``, ``all``. Anything
    else returns 400 — preventing operators from picking a giant
    timespan accidentally is part of the contract.

    Wire shape:
      {
        "range": "7d",
        "points": [
          {
            "captured_at": ISO-8601,
            "total_usd": float,
            "per_account": {"<account_id>": float, ...}
          },
          ...
        ]
      }
    """
    if range not in _RANGE_DELTAS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown range {range!r}. "
                f"Valid: {', '.join(sorted(_RANGE_DELTAS.keys()))}"
            ),
        )
    now = datetime.now(timezone.utc)
    delta = _RANGE_DELTAS[range]
    # "all" → since=epoch. Using a date far enough in the past
    # (1970-01-01) is simpler than threading None through the store.
    since = (
        now - delta if delta is not None else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )

    rows = portfolio_store.history(user.id, since, now)

    # Group by captured_at. Multiple accounts captured in the same
    # tick share a captured_at down to the second precision SQLite
    # uses; that's exactly the row-set we want to aggregate.
    #
    # The dict key stays in the DB-native form for accurate
    # lexicographic ordering during the sort; ``_format_captured_at_iso``
    # only runs on the way out so the wire format is unambiguous
    # ISO 8601 UTC (``Z``-suffixed) — fixes the chart-timezone
    # mismatch on the frontend.
    grouped: dict[str, dict] = {}
    for r in rows:
        ts = r["captured_at"]
        entry = grouped.setdefault(
            ts, {"captured_at": ts, "total_usd": 0.0, "per_account": {}},
        )
        entry["total_usd"] += r["balance_usd"]
        entry["per_account"][str(r["exchange_account_id"])] = r["balance_usd"]

    points = sorted(grouped.values(), key=lambda x: x["captured_at"])
    for p in points:
        p["captured_at"] = _format_captured_at_iso(p["captured_at"])
    return {"range": range, "points": points}


@router.get("/api/portfolio/per-bot")
@limiter.limit("60/minute")
async def get_per_bot_breakdown(
    request: Request,
    user: User = Depends(_request_user),
):
    """Aggregate every deal in the ledger by bot_slug, restricted to
    bots whose YAML says mode=live.

    Returned fields per bot:
      bot_slug             — YAML filename stem
      bot_name             — most recent ``deals.bot_name`` for the slug
      open_positions_count — open deals
      open_position_value_usd
                           — sum(initial_price * total_size) for open
                             deals; rough approximation of capital
                             at-risk
      realized_pnl_usd     — sum(pnl_btc) for closed deals, converted
                             via the latest BTC/USD rate
      unrealized_pnl_usd   — sum(pnl_btc) for open deals, same rate
      total_pnl_usd        — realized + unrealized
      trade_count          — total deals (open + closed)

    Sorted by total_pnl_usd descending so the most-profitable bot
    sits at the top of the table.

    Why USD conversion happens here and not in the deals table:
      ``deals.pnl_btc`` is stored in BTC because that's the engine's
      native unit. The Portfolio tab speaks USD; converting on the
      read path keeps the storage neutral and lets the toggle on the
      frontend switch back to native without a re-fetch.

    The BTC/USD rate used here is the *current* rate from
    ``price_feed`` — not historical. A future enhancement could
    re-price each deal against its closing-time BTC/USD, but the
    portfolio table-row uses the current rate, so consistency wins
    over precision.
    """
    live_slugs = _live_bot_slugs(user.id)
    if not live_slugs:
        return {"bots": []}

    placeholders = ",".join(["?"] * len(live_slugs))
    conn = get_db()
    rows = conn.execute(
        f"""
        SELECT bot_slug, bot_name, status, initial_price,
               total_size, pnl_btc
          FROM deals
         WHERE user_id = ?
           AND bot_slug IN ({placeholders})
        """,
        (user.id, *sorted(live_slugs)),
    ).fetchall()

    try:
        btc_rate, _ = price_feed.get_usd_rate("BTC")
    except price_feed.PriceFeedError:
        # If we cannot price BTC at all, surface USD values as 0 but
        # still return the per-bot trade counts so the table renders.
        logger.warning(
            "Per-bot breakdown: BTC price unavailable; USD fields "
            "will be 0 for user %d", user.id,
        )
        btc_rate = 0.0

    by_slug: dict[str, dict] = {}
    for r in rows:
        slug = r["bot_slug"]
        b = by_slug.setdefault(slug, {
            "bot_slug": slug,
            "bot_name": r["bot_name"],
            "open_positions_count": 0,
            "open_position_value_usd": 0.0,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "total_pnl_usd": 0.0,
            "trade_count": 0,
        })
        b["bot_name"] = r["bot_name"]
        b["trade_count"] += 1
        pnl_btc = float(r["pnl_btc"] or 0.0)
        pnl_usd = pnl_btc * btc_rate
        if r["status"] == "open":
            b["open_positions_count"] += 1
            # Position value: initial_price (USD) * total_size (BTC).
            # initial_price is already in the deal's quote currency,
            # which is USD on the BTC/USD pair; multiplying by total
            # size in base gives the notional. Close enough for the
            # operator's at-a-glance view.
            b["open_position_value_usd"] += (
                float(r["initial_price"] or 0.0)
                * float(r["total_size"] or 0.0)
            )
            b["unrealized_pnl_usd"] += pnl_usd
        else:
            b["realized_pnl_usd"] += pnl_usd
        b["total_pnl_usd"] = b["realized_pnl_usd"] + b["unrealized_pnl_usd"]

    return {
        "bots": sorted(
            by_slug.values(),
            key=lambda x: x["total_pnl_usd"],
            reverse=True,
        ),
    }


@router.post("/api/portfolio/snapshot/manual")
@limiter.limit("10/minute")
async def trigger_manual_snapshot(
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Operator-triggered snapshot for every account this user owns.

    Rate-limited to 1 per rolling hour per user — enforced via
    ``portfolio_store.manual_allowed`` so a 429 response also carries
    the ``next_allowed_at`` field. The slowapi 10/minute gate above
    is a defence-in-depth that catches a burst from a single client
    before it ever touches the store.

    On success, returns:
      {"created": int, "accounts": [account_id, ...]}

    Per-account failures (network blip, broken creds) do NOT abort
    the batch — they just don't appear in the ``accounts`` list. The
    operator sees the count vs the total they have configured and
    can re-trigger from the UI in an hour.
    """
    allowed, next_at = portfolio_store.manual_allowed(user.id)
    if not allowed:
        _audit(
            "portfolio_manual_snapshot",
            "rate_limited", actor,
            user_id=user.id, request=request, result="denied",
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Manual snapshot limited to 1/hour",
                "next_allowed_at": (
                    _format_captured_at_iso(next_at.isoformat())
                    if next_at is not None else None
                ),
            },
        )

    accounts = exchange_account_store.list_accounts(user.id)
    created_ids: list[int] = []
    for acct in accounts:
        try:
            creds = exchange_account_store.get_account_credentials(
                int(acct["id"]),
            )
            if creds is None:
                logger.warning(
                    "Manual snapshot: account %d has unreadable creds",
                    acct["id"],
                )
                continue
            client = build_authenticated_exchange(
                acct["exchange_type"], acct["market_type"], creds,
            )
            balance_native = float(client.get_balance())
            currency = client.balance_currency
        except ExchangeClientError as e:
            logger.warning(
                "Manual snapshot: cannot build client for account "
                "%d: %s", acct["id"], e,
            )
            continue
        except Exception as e:  # noqa: BLE001 — ccxt raises many shapes
            logger.warning(
                "Manual snapshot: exchange call failed for account "
                "%d: %s", acct["id"], e,
            )
            continue
        try:
            usd_rate, rate_source = price_feed.get_usd_rate(currency)
        except price_feed.PriceFeedError as e:
            logger.warning(
                "Manual snapshot: cannot price %s for account %d: %s",
                currency, acct["id"], e,
            )
            continue
        snap_id = portfolio_store.create_snapshot(
            user_id=user.id,
            exchange_account_id=int(acct["id"]),
            balance_native=balance_native,
            currency=currency,
            balance_usd=balance_native * usd_rate,
            usd_rate=usd_rate,
            rate_source=rate_source,
            source="manual",
        )
        created_ids.append(snap_id)

    _audit(
        "portfolio_manual_snapshot",
        f"created={len(created_ids)}/{len(accounts)}", actor,
        user_id=user.id, request=request,
    )
    return {"created": len(created_ids), "accounts": created_ids}
