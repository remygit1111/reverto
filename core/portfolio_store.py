"""Portfolio snapshot store.

Backs the Portfolio tab + the hourly snapshot scheduler. One row per
(user, exchange_account, capture instant); the table is append-only.
Rows store both the native balance + the USD-converted value so the
chart on /api/portfolio/history stays accurate to "what was true at
capture time" even if CoinGecko later revises the rate.

Why a separate module (mirroring core.exchange_account_store):
  * Keeps SQL contained — routes import this and never touch the
    ``portfolio_snapshots`` table directly.
  * Lets the standalone ``main_scheduler.py`` process insert rows
    without dragging in the FastAPI app or the engines.

The manual-snapshot rate-limit (``manual_allowed``) is enforced here
rather than at the route layer because the scheduler also writes
manual-source rows during operator-triggered captures from the API.
Centralising the gate means a future CLI tool that also writes
manual rows inherits the same cap automatically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.database import get_db

logger = logging.getLogger(__name__)


# Allowed values for the ``source`` column. The DB column has no
# CHECK constraint (keeps the migration purely additive); validation
# happens at the application layer so a typo in a script fails loudly
# before the row lands.
_VALID_SOURCES: tuple[str, ...] = ("auto", "manual")

# Rolling window for the manual rate-limit. Spec is "1 manual snapshot
# per user per rolling hour" — see module docstring in
# web/routes/portfolio.py for why rolling > calendar.
_MANUAL_WINDOW = timedelta(hours=1)


def create_snapshot(
    user_id: int,
    exchange_account_id: int,
    balance_native: float,
    currency: str,
    balance_usd: float,
    usd_rate: float,
    rate_source: str,
    source: str,
) -> int:
    """Insert one snapshot row and return its new id.

    ``source`` is one of ``"auto"`` (scheduler) or ``"manual"`` (operator
    pressed the Refresh button). ``rate_source`` mirrors what
    ``core.price_feed.get_usd_rate`` returned ("coingecko",
    "coingecko_cache", "bitget", "bitget_cache", or "identity").
    """
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {_VALID_SOURCES}, got {source!r}",
        )
    conn = get_db()
    with conn:
        cur = conn.execute(
            "INSERT INTO portfolio_snapshots "
            "(user_id, exchange_account_id, balance_native, currency, "
            " balance_usd, usd_rate, rate_source, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, exchange_account_id,
                float(balance_native), currency,
                float(balance_usd), float(usd_rate),
                rate_source, source,
            ),
        )
    return int(cur.lastrowid or 0)


def _row_to_dict(row) -> dict:
    """Convert a ``sqlite3.Row`` into a plain dict for the wire."""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "exchange_account_id": row["exchange_account_id"],
        "captured_at": row["captured_at"],
        "balance_native": float(row["balance_native"]),
        "currency": row["currency"],
        "balance_usd": float(row["balance_usd"]),
        "usd_rate": float(row["usd_rate"]),
        "rate_source": row["rate_source"],
        "source": row["source"],
    }


def latest_per_account(user_id: int) -> list[dict]:
    """Return the most recent snapshot for each of the user's exchange
    accounts, plus the metadata needed to render a row in the table
    (alias, exchange_type, market_type).

    Accounts with no snapshot yet are omitted — the route layer
    composes them with the full list of accounts so the UI shows
    "No snapshots yet" placeholders.

    SQL strategy: the correlated subquery picks the MAX(captured_at)
    per account, then we join the matching row + the account metadata.
    SQLite handles this comfortably at the row counts we expect
    (≤ 100 accounts/user/year).
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT
            ps.*,
            ea.alias         AS alias,
            ea.exchange_type AS exchange_type,
            ea.market_type   AS market_type
          FROM portfolio_snapshots ps
          JOIN exchange_accounts ea ON ea.id = ps.exchange_account_id
         WHERE ps.user_id = ?
           AND ps.captured_at = (
                SELECT MAX(captured_at)
                  FROM portfolio_snapshots ps2
                 WHERE ps2.exchange_account_id = ps.exchange_account_id
           )
         ORDER BY ea.exchange_type ASC, ea.alias ASC
        """,
        (user_id,),
    ).fetchall()

    result: list[dict] = []
    for row in rows:
        out = _row_to_dict(row)
        out["alias"] = row["alias"]
        out["exchange_type"] = row["exchange_type"]
        out["market_type"] = row["market_type"]
        result.append(out)
    return result


def history(
    user_id: int,
    since: datetime,
    until: Optional[datetime] = None,
) -> list[dict]:
    """Every snapshot for ``user_id`` between ``since`` and ``until``,
    oldest-first. ``until=None`` means "up to now".

    Used by the /api/portfolio/history route to build the time-series
    chart payload. The frontend aggregates across accounts per
    captured_at instant; we return the rows as-is so the route layer
    has the flexibility to group however it likes.
    """
    until = until if until is not None else datetime.now(timezone.utc)
    conn = get_db()
    # ``datetime(captured_at)`` normalises rows that landed via the
    # ``datetime('now')`` DEFAULT (space-separated) so they compare
    # cleanly against the operator-supplied ISO-with-T bounds.
    rows = conn.execute(
        "SELECT * FROM portfolio_snapshots "
        "WHERE user_id = ? "
        "  AND datetime(captured_at) >= datetime(?) "
        "  AND datetime(captured_at) <= datetime(?) "
        "ORDER BY captured_at ASC, exchange_account_id ASC",
        (user_id, since.isoformat(), until.isoformat()),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def manual_allowed(
    user_id: int, *, now: Optional[datetime] = None,
) -> tuple[bool, Optional[datetime]]:
    """Has the user used their manual snapshot in the last 60 minutes?

    Returns ``(True, None)`` when a new manual snapshot is allowed, or
    ``(False, next_allowed_at)`` when the operator is still within the
    cooldown — ``next_allowed_at`` is the UTC instant at which they
    can press the button again. The route layer surfaces this to the
    UI so the operator sees a countdown instead of an opaque 429.

    Rolling window, not calendar hour — see module docstring.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    cutoff = now - _MANUAL_WINDOW
    conn = get_db()
    # ``datetime(captured_at)`` + ``datetime(?)`` normalises both
    # sides regardless of whether the row's timestamp landed as
    # SQLite's default ``YYYY-MM-DD HH:MM:SS`` (from
    # ``datetime('now')`` on insert) or as the operator-supplied
    # ISO-with-T form a manual route or test fixture would write.
    row = conn.execute(
        "SELECT MAX(captured_at) AS last_manual "
        "FROM portfolio_snapshots "
        "WHERE user_id = ? AND source = 'manual' "
        "  AND datetime(captured_at) >= datetime(?)",
        (user_id, cutoff.isoformat()),
    ).fetchone()
    last_manual_str = row["last_manual"] if row is not None else None
    if not last_manual_str:
        return True, None
    # Parse — captured_at is stored as ISO-8601 by both DEFAULT
    # ``datetime('now')`` (SQLite, naive UTC) and by explicit
    # operator-side inserts (datetime.isoformat()).
    try:
        if last_manual_str.endswith("Z"):
            last_manual = datetime.fromisoformat(
                last_manual_str[:-1] + "+00:00",
            )
        else:
            last_manual = datetime.fromisoformat(last_manual_str)
    except ValueError:
        # Malformed timestamp in the DB — fail open rather than lock
        # the operator out indefinitely. The corrupt row is rare and
        # the worst case is one extra manual snapshot.
        logger.warning(
            "Unparseable captured_at %r for user %d manual gate; "
            "allowing snapshot.", last_manual_str, user_id,
        )
        return True, None
    # SQLite's ``datetime('now')`` returns a naive UTC string. Coerce
    # to UTC-aware so the subtraction below doesn't raise.
    if last_manual.tzinfo is None:
        last_manual = last_manual.replace(tzinfo=timezone.utc)
    next_allowed = last_manual + _MANUAL_WINDOW
    if next_allowed <= now:
        return True, None
    return False, next_allowed
