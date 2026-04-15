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

import threading
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Optional

from core.database import get_db

if TYPE_CHECKING:  # avoid circular import with paper.paper_engine
    from paper.paper_state import PaperDeal, PaperOrder

_write_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── Deals / orders ───────────────────────────────────────────────────────────

def save_deal(deal: "PaperDeal", bot_slug: str, bot_name: str) -> None:
    """INSERT OR REPLACE the given deal row.

    Called on open, on every DCA (so avg_entry + total_size stay current),
    and on restart replay from the JSON state file.
    """
    status = "open" if deal.is_open else "closed"
    opened_at = deal.opened_at.isoformat() if deal.opened_at else _now_iso()
    closed_at = deal.closed_at.isoformat() if deal.closed_at else None
    initial_price = deal.orders[0].price if deal.orders else 0.0

    with _write_lock:
        conn = get_db()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO deals (
                    id, bot_slug, bot_name, side, status, close_reason,
                    opened_at, closed_at, initial_price, avg_entry,
                    close_price, total_size, leverage, pnl_btc, pnl_pct,
                    peak_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deal.id,
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
                ),
            )


def replay_deals_in_transaction(
    deals: list,
    bot_slug: str,
    bot_name: str,
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
    with _write_lock:
        conn = get_db()
        with conn:  # one transaction for the whole batch
            for deal in deals:
                status = "open" if deal.is_open else "closed"
                opened_at = deal.opened_at.isoformat() if deal.opened_at else _now_iso()
                closed_at = deal.closed_at.isoformat() if deal.closed_at else None
                initial_price = deal.orders[0].price if deal.orders else 0.0
                conn.execute(
                    """
                    INSERT OR REPLACE INTO deals (
                        id, bot_slug, bot_name, side, status, close_reason,
                        opened_at, closed_at, initial_price, avg_entry,
                        close_price, total_size, leverage, pnl_btc, pnl_pct,
                        peak_price
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        deal.id,
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
                    ),
                )
                for order in deal.orders:
                    order_id = f"{deal.id}:{order.order_number}"
                    placed_at = order.timestamp.isoformat() if order.timestamp else _now_iso()
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO orders (
                            id, deal_id, bot_slug, order_number, order_type,
                            price, size, fee_btc, placed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            order_id,
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
    fee_btc: float = 0.0,
) -> None:
    """INSERT OR REPLACE a single order row under its parent deal."""
    order_id = f"{deal_id}:{order.order_number}"
    placed_at = order.timestamp.isoformat() if order.timestamp else _now_iso()

    with _write_lock:
        conn = get_db()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orders (
                    id, deal_id, bot_slug, order_number, order_type,
                    price, size, fee_btc, placed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
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
    closed_at: Optional[str] = None,
) -> None:
    """Mark a deal closed in the ledger. No-op if the row does not exist."""
    closed_at_iso = closed_at or _now_iso()
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
                       closed_at = ?
                 WHERE id = ?
                """,
                (close_price, close_reason, pnl_btc, pnl_pct, closed_at_iso, deal_id),
            )


def get_deals(
    bot_slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Return deal rows filtered by optional bot_slug + status.

    Rows come back as plain dicts (all columns) sorted by opened_at DESC.
    """
    conn = get_db()
    query = "SELECT * FROM deals"
    clauses: list[str] = []
    params: list[object] = []
    if bot_slug is not None:
        clauses.append("bot_slug = ?")
        params.append(bot_slug)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY opened_at DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_deal_orders(deal_id: str) -> list[dict]:
    """Return all orders for a deal, sorted by order_number ASC."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE deal_id = ? ORDER BY order_number ASC",
        (deal_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_deals(limit: int = 500) -> list[dict]:
    """Shortcut for get_deals(None, None, limit)."""
    return get_deals(None, None, limit)


# ── Stats ────────────────────────────────────────────────────────────────────

def compute_stats(bot_slug: Optional[str] = None) -> dict:
    """Aggregate stats across closed deals.

    Returns a flat dict with totals, win rate, best/worst deal PnL, and
    total fees across every order for the matching deals. If there are
    no closed deals the result is all zeros plus a "note" field.
    """
    conn = get_db()

    if bot_slug is not None:
        deal_rows = conn.execute(
            "SELECT id, pnl_btc FROM deals WHERE status = 'closed' AND bot_slug = ?",
            (bot_slug,),
        ).fetchall()
    else:
        deal_rows = conn.execute(
            "SELECT id, pnl_btc FROM deals WHERE status = 'closed'",
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

    # Fees across every order belonging to the matched deals.
    ids = [r["id"] for r in deal_rows]
    placeholders = ",".join("?" * len(ids))
    fee_row = conn.execute(
        f"SELECT COALESCE(SUM(fee_btc), 0) AS total FROM orders WHERE deal_id IN ({placeholders})",
        ids,
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
                    bot_slug, type, timeframe, x1, y1, x2, y2, label, color
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (bot_slug, type_, timeframe, x1, y1, x2, y2, label, color),
            )
            return int(cur.lastrowid or 0)


def list_annotations(
    bot_slug: str,
    timeframe: Optional[str] = None,
) -> list[dict]:
    """Return all annotations for a bot, optionally filtered by timeframe."""
    conn = get_db()
    if timeframe is None:
        rows = conn.execute(
            "SELECT * FROM chart_annotations WHERE bot_slug = ? ORDER BY id ASC",
            (bot_slug,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chart_annotations WHERE bot_slug = ? AND timeframe = ? "
            "ORDER BY id ASC",
            (bot_slug, timeframe),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_annotation(ann_id: int) -> bool:
    """Delete an annotation by id. Returns True if a row was removed."""
    with _write_lock:
        conn = get_db()
        with conn:
            cur = conn.execute(
                "DELETE FROM chart_annotations WHERE id = ?",
                (ann_id,),
            )
            return cur.rowcount > 0


# ── Backtest runs ────────────────────────────────────────────────────────────

_BACKTEST_COLS = (
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
    bot_slug: str, bot_name: str, params: dict, summary: dict
) -> int:
    """Persist one completed backtest run and return its row id.

    `params` carries the user-facing inputs (start/end/timeframe/
    balance) and `summary` carries the engine output (the summary
    and ratios blocks from RevertoBacktest._buildResults, flattened).
    Infinity / NaN ratios become NULL so sqlite is happy.
    """
    s = summary or {}
    row = {
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


def get_backtest_runs(bot_slug: str, limit: int = 50) -> list[dict]:
    """Return the N most recent backtest runs for a single bot."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM backtest_runs WHERE bot_slug = ? "
        "ORDER BY id DESC LIMIT ?",
        (bot_slug, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_backtest_runs(limit: int = 100) -> list[dict]:
    """Return the N most recent backtest runs across every bot."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_annotations_for(bot_slug: str, timeframe: Optional[str] = None) -> int:
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
                    "DELETE FROM chart_annotations WHERE bot_slug = ?",
                    (bot_slug,),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM chart_annotations WHERE bot_slug = ? AND timeframe = ?",
                    (bot_slug, timeframe),
                )
            return cur.rowcount
