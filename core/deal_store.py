# core/deal_store.py
# High-level wrappers around the SQLite persistence layer.
#
# The paper engine writes every deal / order / close event here so the
# portal can serve historical data from a single queryable source. The
# JSON state file remains authoritative for live-state restart recovery;
# this module is the append-only ledger that sits alongside it.
#
# Every function commits its own transaction. Writes hold a module-level
# lock so the portal's async routes and the engine's monitor thread do
# not interleave writes on top of each other.
#
# Multi-tenant contract (Phase 1):
#   Every public function takes ``user_id`` as a required argument.
#   This is deliberately defaults-free — the classic mistake in a multi-
#   tenant system is a query that forgets its tenant filter and leaks
#   another user's data. Making the argument mandatory forces every
#   call site through review. Phase 1 always passes ``user_id=1``
#   (admin), but the wiring is ready for Phase 2 session-based
#   resolution without another refactor.

import json
import threading
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Optional

from core.database import get_db

if TYPE_CHECKING:  # avoid circular import with paper.paper_engine
    from paper.paper_state import PaperDeal, PaperOrder

_write_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _encode_trigger(value: Optional[dict]) -> Optional[str]:
    """Serialise a trigger dict for DB storage. None maps to NULL.
    Non-dict values are coerced to None so a corrupt caller cannot
    shove arbitrary JSON into the column."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return None


def _decode_trigger(value: Optional[str]) -> Optional[dict]:
    """Deserialise a stored trigger JSON string. Returns None on NULL
    or on any parse failure — trigger metadata is best-effort, never
    critical to deal logic."""
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ── Deals / orders ───────────────────────────────────────────────────────────
#
# INSERT vs UPDATE vs upsert — the cross-bot-collision fix (2026-04-19).
#
# Pre-fix: save_deal used INSERT OR REPLACE. If two bots generated the
# same deal.id (e.g. both produced "PAPER-0001" from per-instance
# counters) the second writer silently clobbered the first — no error,
# no warning, corrupt ledger.
#
# Post-fix: we split the write surface into three primitives:
#   * create_deal — INSERT only, raises IntegrityError on duplicate id.
#     Callers that know they're opening a new deal use this and retry
#     with a fresh id on collision. This is the only path that catches
#     cross-bot id reuse.
#   * update_deal — UPDATE by (id, user_id, bot_slug). No-op if no row
#     matches. Used for DCA updates where the row already exists.
#   * save_deal — backwards-compat upsert: tries UPDATE first, falls
#     back to INSERT if 0 rows were updated. Cross-owner collisions
#     still raise IntegrityError because the fallback INSERT hits the
#     PRIMARY KEY constraint.
#
# The new generator (core/ids.py) makes collisions astronomically rare
# in practice (per-minute 10_000-slot keyspace + DB PRIMARY KEY guard),
# but the three-way split keeps the DB as the authoritative source of
# truth — any future generator regression surfaces as IntegrityError
# instead of silent data loss.


def _deal_row_tuple(
    deal: "PaperDeal", bot_slug: str, bot_name: str, user_id: int,
) -> tuple:
    """Shared tuple-builder for create_deal / update_deal / save_deal so
    column order can only drift in one place. Column order matches
    both INSERT and UPDATE templates below."""
    status = "open" if deal.is_open else "closed"
    opened_at = deal.opened_at.isoformat() if deal.opened_at else _now_iso()
    closed_at = deal.closed_at.isoformat() if deal.closed_at else None
    initial_price = deal.orders[0].price if deal.orders else 0.0
    return (
        deal.id,
        user_id,
        bot_slug,
        bot_name,
        deal.side,
        status,
        deal.close_reason,
        opened_at,
        closed_at,
        initial_price,
        deal.avg_entry_price,
        deal.close_price,
        deal.total_size,
        deal.leverage,
        deal.pnl_btc,
        deal.pnl_pct,
        deal._peak_price,
        _encode_trigger(getattr(deal, "entry_trigger", None)),
        _encode_trigger(getattr(deal, "exit_trigger", None)),
    )


_INSERT_DEAL_SQL = """
    INSERT INTO deals (
        id, user_id, bot_slug, bot_name, side, status, close_reason,
        opened_at, closed_at, initial_price, avg_entry,
        close_price, total_size, leverage, pnl_btc, pnl_pct,
        peak_price, entry_trigger, exit_trigger
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_DEAL_SQL = """
    UPDATE deals
       SET bot_name = ?,
           side = ?,
           status = ?,
           close_reason = ?,
           opened_at = ?,
           closed_at = ?,
           initial_price = ?,
           avg_entry = ?,
           close_price = ?,
           total_size = ?,
           leverage = ?,
           pnl_btc = ?,
           pnl_pct = ?,
           peak_price = ?,
           entry_trigger = ?,
           exit_trigger = ?
     WHERE id = ? AND user_id = ? AND bot_slug = ?
"""


def create_deal(
    deal: "PaperDeal", bot_slug: str, bot_name: str, user_id: int,
) -> None:
    """INSERT a new deal row.

    Raises ``sqlite3.IntegrityError`` if a deal with the same id
    already exists — the caller is expected to either log the
    collision and retry with a regenerated id (paper_engine's open
    path) or skip silently (JSON→DB migration on restart).
    """
    row = _deal_row_tuple(deal, bot_slug, bot_name, user_id)
    with _write_lock:
        conn = get_db()
        with conn:
            conn.execute(_INSERT_DEAL_SQL, row)


def update_deal(
    deal: "PaperDeal", bot_slug: str, bot_name: str, user_id: int,
) -> int:
    """UPDATE an existing deal row scoped to (id, user_id, bot_slug).

    Returns the number of rows affected (0 or 1). A 0 return means the
    row doesn't exist — the caller decides whether to INSERT or log.
    Used by the DCA update path where the deal was already persisted
    on open and only avg_entry / total_size / peak_price need to move.
    """
    row = _deal_row_tuple(deal, bot_slug, bot_name, user_id)
    # UPDATE template order: SET columns first, then WHERE key columns.
    # Skip id/user_id/bot_slug in the SET list — those identify the row.
    update_params = (
        row[3],   # bot_name
        row[4],   # side
        row[5],   # status
        row[6],   # close_reason
        row[7],   # opened_at
        row[8],   # closed_at
        row[9],   # initial_price
        row[10],  # avg_entry
        row[11],  # close_price
        row[12],  # total_size
        row[13],  # leverage
        row[14],  # pnl_btc
        row[15],  # pnl_pct
        row[16],  # peak_price
        row[17],  # entry_trigger
        row[18],  # exit_trigger
        row[0],   # id (WHERE)
        row[1],   # user_id (WHERE)
        row[2],   # bot_slug (WHERE)
    )
    with _write_lock:
        conn = get_db()
        with conn:
            cur = conn.execute(_UPDATE_DEAL_SQL, update_params)
            return cur.rowcount


def save_deal(
    deal: "PaperDeal", bot_slug: str, bot_name: str, user_id: int,
) -> None:
    """Upsert: UPDATE if a matching row exists, else INSERT.

    Backwards-compatible wrapper kept so existing call-sites (tests,
    ``replay_deals_in_transaction``) don't need to know whether a row
    already exists. Cross-owner collisions — another user or bot
    holding the same id — still raise IntegrityError because the
    fallback INSERT hits the PRIMARY KEY constraint. This is the
    semantic the cross-bot-collision fix is built around: no silent
    clobber, loud failure.
    """
    if update_deal(deal, bot_slug, bot_name, user_id) > 0:
        return
    create_deal(deal, bot_slug, bot_name, user_id)


def replay_deals_in_transaction(
    deals: list, bot_slug: str, bot_name: str, user_id: int,
) -> int:
    """Bulk-INSERT-OR-REPLACE a list of PaperDeal objects + their orders
    inside a single SQLite transaction.

    Used by the JSON → SQLite migration in PaperEngine._load_state. If
    any row fails the entire migration rolls back, which prevents a
    half-migrated ledger when a corrupt deal lurks in the middle of
    the JSON. Returns the number of deals successfully replayed.
    """
    if not deals:
        return 0
    # Restart-replay runs on every boot, so most rows are already in the
    # DB — we want "add if missing" semantics, not clobber. INSERT OR
    # IGNORE skips matching rows without raising, and keeps the whole
    # batch atomic under the single `with conn` transaction.
    with _write_lock:
        conn = get_db()
        with conn:
            for deal in deals:
                row = _deal_row_tuple(deal, bot_slug, bot_name, user_id)
                conn.execute(
                    _INSERT_DEAL_SQL.replace(
                        "INSERT INTO deals", "INSERT OR IGNORE INTO deals", 1,
                    ),
                    row,
                )
                for order in deal.orders:
                    order_id = f"{deal.id}:{order.order_number}"
                    placed_at = order.timestamp.isoformat() if order.timestamp else _now_iso()
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO orders (
                            id, user_id, deal_id, bot_slug, order_number, order_type,
                            price, size, fee_btc, placed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            order_id,
                            user_id,
                            deal.id,
                            bot_slug,
                            order.order_number,
                            order.order_type,
                            order.price,
                            order.size,
                            0.0,  # fee unknown for historical replay
                            placed_at,
                        ),
                    )
    return len(deals)


def save_order(
    order: "PaperOrder",
    deal_id: str,
    bot_slug: str,
    user_id: int,
    fee_btc: float = 0.0,
) -> None:
    """INSERT a single order row under its parent deal.

    Orders don't have an update path — each tick either appends a new
    row (base order, then DCA N=2,3,...) or does nothing. The old
    INSERT OR REPLACE was a leftover from the cross-bot-collision bug
    era (same deal id across bots → overlapping order_ids → silent
    clobber). Replaced with a plain INSERT so any duplicate surfaces
    loudly as IntegrityError.
    """
    order_id = f"{deal_id}:{order.order_number}"
    placed_at = order.timestamp.isoformat() if order.timestamp else _now_iso()

    with _write_lock:
        conn = get_db()
        with conn:
            conn.execute(
                """
                INSERT INTO orders (
                    id, user_id, deal_id, bot_slug, order_number, order_type,
                    price, size, fee_btc, placed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    user_id,
                    deal_id,
                    bot_slug,
                    order.order_number,
                    order.order_type,
                    order.price,
                    order.size,
                    fee_btc,
                    placed_at,
                ),
            )


def close_deal(
    deal_id: str,
    close_price: float,
    close_reason: str,
    pnl_btc: float,
    pnl_pct: float,
    user_id: int,
    closed_at: Optional[str] = None,
    exit_trigger: Optional[dict] = None,
) -> None:
    """Mark a deal closed in the ledger. No-op if the row does not
    exist or belongs to a different user.

    The ``WHERE id = ? AND user_id = ?`` clause is the per-row tenant
    guard — a miswired caller that passes the wrong user_id gets a
    silent no-op instead of clobbering another tenant's deal.
    """
    closed_at_iso = closed_at or _now_iso()
    encoded = _encode_trigger(exit_trigger)
    with _write_lock:
        conn = get_db()
        with conn:
            conn.execute(
                """
                UPDATE deals
                   SET status = 'closed',
                       close_price = ?,
                       close_reason = ?,
                       pnl_btc = ?,
                       pnl_pct = ?,
                       closed_at = ?,
                       exit_trigger = COALESCE(?, exit_trigger)
                 WHERE id = ? AND user_id = ?
                """,
                (close_price, close_reason, pnl_btc, pnl_pct, closed_at_iso,
                 encoded, deal_id, user_id),
            )


def get_deals(
    user_id: int,
    bot_slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Return deal rows filtered by ``user_id`` (required) + optional
    bot_slug + status.

    Rows come back as plain dicts (all columns) sorted by opened_at DESC.
    """
    conn = get_db()
    query = "SELECT * FROM deals WHERE user_id = ?"
    params: list[object] = [user_id]
    if bot_slug is not None:
        query += " AND bot_slug = ?"
        params.append(bot_slug)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY opened_at DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(query, params).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["entry_trigger"] = _decode_trigger(d.get("entry_trigger"))
        d["exit_trigger"] = _decode_trigger(d.get("exit_trigger"))
        out.append(d)
    return out


def get_deal_orders(deal_id: str, user_id: int) -> list[dict]:
    """Return all orders for a deal, sorted by order_number ASC.
    Filters by ``user_id`` as a second line of defence — even though
    deal_id is a PK, callers that fetched a foreign deal_id and then
    asked us for its orders would otherwise leak the other tenant's
    order rows."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE deal_id = ? AND user_id = ? "
        "ORDER BY order_number ASC",
        (deal_id, user_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_orders_for_deal_ids(
    deal_ids: list[str], user_id: int,
) -> dict[str, list[dict]]:
    """Batch-fetch orders for multiple deals in ONE query.

    Replaces the N+1 pattern where ``/api/db/deals`` called
    ``get_deal_orders`` once per deal (audit r1-020). For N deals
    that was N+1 round-trips; this helper does one IN-list query
    and groups in Python. Returns ``{deal_id: [orders...]}`` with
    empty lists for ids that had no orders so callers can iterate
    without a KeyError guard.

    Scoped by ``user_id`` as a second line of defence — matches
    the per-deal helper's hygiene so a caller that passed foreign
    deal_ids gets back an empty list rather than another tenant's
    orders.
    """
    if not deal_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(deal_ids))
    rows = conn.execute(
        f"SELECT * FROM orders "
        f"WHERE deal_id IN ({placeholders}) AND user_id = ? "
        f"ORDER BY deal_id, order_number ASC",
        [*deal_ids, user_id],
    ).fetchall()
    result: dict[str, list[dict]] = {did: [] for did in deal_ids}
    for r in rows:
        d = dict(r)
        did = d.get("deal_id")
        if did in result:
            result[did].append(d)
    return result


def get_all_deals(user_id: int, limit: int = 500) -> list[dict]:
    """Shortcut for get_deals(user_id, None, None, limit)."""
    return get_deals(user_id, None, None, limit)


# ── Stats ────────────────────────────────────────────────────────────────────

def compute_stats(user_id: int, bot_slug: Optional[str] = None) -> dict:
    """Aggregate stats across closed deals.

    Returns a flat dict with totals, win rate, best/worst deal PnL, and
    total fees across every order for the matching deals. If there are
    no closed deals the result is all zeros plus a "note" field.
    """
    conn = get_db()

    if bot_slug is not None:
        deal_rows = conn.execute(
            "SELECT id, pnl_btc FROM deals "
            "WHERE user_id = ? AND status = 'closed' AND bot_slug = ?",
            (user_id, bot_slug),
        ).fetchall()
    else:
        deal_rows = conn.execute(
            "SELECT id, pnl_btc FROM deals "
            "WHERE user_id = ? AND status = 'closed'",
            (user_id,),
        ).fetchall()

    total = len(deal_rows)
    if total == 0:
        return {
            "total_deals":    0,
            "wins":           0,
            "losses":         0,
            "win_rate":       0.0,
            "avg_pnl_btc":    0.0,
            "best_deal":      0.0,
            "worst_deal":     0.0,
            "total_fees_btc": 0.0,
            "note":           "no deals",
        }

    pnls = [float(r["pnl_btc"] or 0.0) for r in deal_rows]
    wins = len([p for p in pnls if p > 0])
    losses = total - wins
    win_rate = round((wins / total) * 100, 2)
    avg_pnl = sum(pnls) / total
    best = max(pnls)
    worst = min(pnls)

    # Fees across every order belonging to the matched deals. user_id
    # filter on orders is belt-and-braces — the deal_id IN (...) clause
    # is already scoped to this user via the deal_rows lookup above.
    ids = [r["id"] for r in deal_rows]
    placeholders = ",".join("?" * len(ids))
    fee_row = conn.execute(
        f"SELECT COALESCE(SUM(fee_btc), 0) AS total FROM orders "
        f"WHERE deal_id IN ({placeholders}) AND user_id = ?",
        ids + [user_id],
    ).fetchone()
    total_fees = float(fee_row["total"] or 0.0)

    return {
        "total_deals":    total,
        "wins":           wins,
        "losses":         losses,
        "win_rate":       win_rate,
        "avg_pnl_btc":    avg_pnl,
        "best_deal":      best,
        "worst_deal":     worst,
        "total_fees_btc": total_fees,
    }


# ── Chart annotations ────────────────────────────────────────────────────────

def save_annotation(
    bot_slug: str,
    type_: str,
    timeframe: str,
    x1: int,
    user_id: int,
    y1: Optional[float] = None,
    x2: Optional[int] = None,
    y2: Optional[float] = None,
    label: Optional[str] = None,
    color: str = "#00d4aa",
) -> int:
    """Insert a chart annotation row and return its new autoincrement id.

    x1 / x2 are clamped to [0, 2_000_000_000] (Unix seconds, ~year 2033)
    as a defence-in-depth guard. The web AnnotationBody also enforces
    the same range, but anything calling save_annotation directly
    (tests, future internal callers) gets the same protection so a
    junk timestamp can never reach the SQLite row.
    """
    _TS_MAX = 2_000_000_000
    x1 = max(0, min(_TS_MAX, int(x1)))
    if x2 is not None:
        x2 = max(0, min(_TS_MAX, int(x2)))
    with _write_lock:
        conn = get_db()
        with conn:
            cur = conn.execute(
                """
                INSERT INTO chart_annotations (
                    user_id, bot_slug, type, timeframe, x1, y1, x2, y2, label, color
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, bot_slug, type_, timeframe,
                 x1, y1, x2, y2, label, color),
            )
            return int(cur.lastrowid or 0)


def list_annotations(
    bot_slug: str,
    user_id: int,
    timeframe: Optional[str] = None,
) -> list[dict]:
    """Return all annotations for a bot, optionally filtered by timeframe."""
    conn = get_db()
    if timeframe is None:
        rows = conn.execute(
            "SELECT * FROM chart_annotations WHERE user_id = ? AND bot_slug = ? "
            "ORDER BY id ASC",
            (user_id, bot_slug),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chart_annotations WHERE user_id = ? "
            "AND bot_slug = ? AND timeframe = ? "
            "ORDER BY id ASC",
            (user_id, bot_slug, timeframe),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_annotation(ann_id: int, user_id: int) -> bool:
    """Delete an annotation by id. Returns True if a row was removed.
    The user_id filter makes a no-op out of cross-tenant delete
    attempts — returning False leaks only the information that an
    annotation with that id either doesn't exist or doesn't belong
    to this user, which is the same response either way."""
    with _write_lock:
        conn = get_db()
        with conn:
            cur = conn.execute(
                "DELETE FROM chart_annotations WHERE id = ? AND user_id = ?",
                (ann_id, user_id),
            )
            return cur.rowcount > 0


# ── Backtest runs ────────────────────────────────────────────────────────────

_BACKTEST_COLS = (
    "user_id",
    "bot_slug", "bot_name", "start_date", "end_date", "timeframe",
    "initial_balance_btc", "final_balance_btc",
    "total_pnl_btc", "total_pnl_pct",
    "total_deals", "winning_deals", "losing_deals",
    "win_rate", "avg_duration_hours", "max_duration_hours",
    "total_fees_btc", "max_drawdown_pct",
    "profit_factor", "sharpe_ratio", "sortino_ratio",
    "calmar_ratio", "recovery_factor", "expectancy_btc",
    "avg_win_loss_ratio", "omega_ratio",
    "buy_hold_pnl_pct",
    "max_consecutive_wins", "max_consecutive_losses",
)


def _to_float(v) -> Optional[float]:
    """Accept int / float / numeric-string; drop Infinity and NaN so the
    stored row can be read back without JSON serialization headaches."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / ±Inf
        return None
    return f


def save_backtest_run(
    bot_slug: str, bot_name: str, params: dict, summary: dict, user_id: int,
) -> int:
    """Persist one completed backtest run and return its row id.

    `params` carries the user-facing inputs (start/end/timeframe/
    balance) and `summary` carries the engine output (the summary
    and ratios blocks from RevertoBacktest._buildResults, flattened).
    Infinity / NaN ratios become NULL so sqlite is happy.
    """
    s = summary or {}
    row = {
        "user_id": user_id,
        "bot_slug": bot_slug,
        "bot_name": bot_name,
        "start_date": params.get("start_date", ""),
        "end_date": params.get("end_date", ""),
        "timeframe": params.get("timeframe", ""),
        "initial_balance_btc": _to_float(params.get("initial_balance_btc")) or 0.0,
        "final_balance_btc":   _to_float(s.get("final_balance_btc")),
        "total_pnl_btc":       _to_float(s.get("total_pnl_btc")),
        "total_pnl_pct":       _to_float(s.get("total_pnl_pct")),
        "total_deals":         int(s.get("total_deals") or 0),
        "winning_deals":       int(s.get("wins") or 0),
        "losing_deals":        int(s.get("losses") or 0),
        "win_rate":            _to_float(s.get("win_rate")),
        "avg_duration_hours":  _to_float(s.get("avg_duration_hours")),
        "max_duration_hours":  _to_float(s.get("max_duration_hours")),
        "total_fees_btc":      _to_float(s.get("total_fees_btc")),
        "max_drawdown_pct":    _to_float(s.get("max_drawdown_pct")),
        "profit_factor":       _to_float(s.get("profit_factor")),
        "sharpe_ratio":        _to_float(s.get("sharpe_ratio")),
        "sortino_ratio":       _to_float(s.get("sortino_ratio")),
        "calmar_ratio":        _to_float(s.get("calmar_ratio")),
        "recovery_factor":     _to_float(s.get("recovery_factor")),
        "expectancy_btc":      _to_float(s.get("expectancy_btc")),
        "avg_win_loss_ratio":  _to_float(s.get("avg_win_loss_ratio")),
        "omega_ratio":         _to_float(s.get("omega_ratio")),
        "buy_hold_pnl_pct":    _to_float(s.get("buy_hold_pnl_pct")),
        "max_consecutive_wins":   int(s.get("max_consecutive_wins") or 0),
        "max_consecutive_losses": int(s.get("max_consecutive_losses") or 0),
    }
    placeholders = ", ".join("?" for _ in _BACKTEST_COLS)
    columns = ", ".join(_BACKTEST_COLS)
    values = tuple(row[c] for c in _BACKTEST_COLS)
    with _write_lock:
        conn = get_db()
        with conn:
            cur = conn.execute(
                f"INSERT INTO backtest_runs ({columns}) VALUES ({placeholders})",
                values,
            )
            return int(cur.lastrowid)


def get_backtest_runs(bot_slug: str, user_id: int, limit: int = 50) -> list[dict]:
    """Return the N most recent backtest runs for a single bot."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM backtest_runs WHERE user_id = ? AND bot_slug = ? "
        "ORDER BY id DESC LIMIT ?",
        (user_id, bot_slug, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_backtest_runs(user_id: int, limit: int = 100) -> list[dict]:
    """Return the N most recent backtest runs for this user."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM backtest_runs WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_backtest_run(run_id: int, user_id: int) -> bool:
    """Delete a single backtest run by ID. Returns True if a row was
    deleted. The user_id filter makes cross-tenant deletes a no-op."""
    with _write_lock:
        conn = get_db()
        with conn:
            cur = conn.execute(
                "DELETE FROM backtest_runs WHERE id = ? AND user_id = ?",
                (run_id, user_id),
            )
            return cur.rowcount > 0


def delete_annotations_for(
    bot_slug: str, user_id: int, timeframe: Optional[str] = None,
) -> int:
    """Bulk-delete annotations for a bot, optionally scoped to a timeframe.

    Returns the number of rows removed. Used by the "Clear all" toolbar
    button — scoping by timeframe avoids nuking annotations on other
    timeframes the user may still want.
    """
    with _write_lock:
        conn = get_db()
        with conn:
            if timeframe is None:
                cur = conn.execute(
                    "DELETE FROM chart_annotations WHERE user_id = ? AND bot_slug = ?",
                    (user_id, bot_slug),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM chart_annotations WHERE user_id = ? "
                    "AND bot_slug = ? AND timeframe = ?",
                    (user_id, bot_slug, timeframe),
                )
            return cur.rowcount
