# core/database.py
# SQLite persistence layer for Reverto.
#
# The paper engine keeps its live state in a JSON file (source of truth for
# restart recovery). This module provides a durable, queryable ledger on top
# of that — every deal, order and chart annotation lands in logs/reverto.db
# so the portal can surface history, compute stats, and store user-drawn
# annotations without having to rescan every JSON state file.
#
# Thread-safety: sqlite3.Connection objects are not safe to share across
# threads by default. The paper engine runs a daemon notify thread alongside
# its monitor loop, and the portal runs async routes on top of a threadpool —
# so we keep one connection per thread via threading.local(). Writes are
# additionally serialised with a module-level lock (SQLite WAL allows
# concurrent reads but serialises writers anyway; the lock just keeps the
# Python side well-behaved under the portal's async + engine's threaded use).

import sqlite3
import threading
from pathlib import Path

_BASE_DIR = Path(__file__).parent.parent
_DB_PATH: Path = _BASE_DIR / "logs" / "reverto.db"

# One connection per thread. sqlite3 raises if a connection created on thread
# A is used from thread B, and we have multiple threads touching the DB.
_connection_cache = threading.local()


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS deals (
        id          TEXT PRIMARY KEY,
        bot_slug    TEXT NOT NULL,
        bot_name    TEXT NOT NULL,
        side        TEXT NOT NULL DEFAULT 'long',
        status      TEXT NOT NULL,
        close_reason TEXT,
        opened_at   TEXT NOT NULL,
        closed_at   TEXT,
        initial_price REAL NOT NULL,
        avg_entry   REAL,
        close_price REAL,
        total_size  REAL NOT NULL,
        leverage    INTEGER DEFAULT 1,
        pnl_btc     REAL,
        pnl_pct     REAL,
        peak_price  REAL,
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id          TEXT PRIMARY KEY,
        deal_id     TEXT NOT NULL REFERENCES deals(id),
        bot_slug    TEXT NOT NULL,
        order_number INTEGER NOT NULL,
        order_type  TEXT NOT NULL,
        price       REAL NOT NULL,
        size        REAL NOT NULL,
        fee_btc     REAL DEFAULT 0,
        placed_at   TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS indicator_snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_slug    TEXT NOT NULL,
        timeframe   TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        rsi         REAL,
        ema_fast    REAL,
        ema_slow    REAL,
        macd        REAL,
        macd_signal REAL,
        macd_hist   REAL,
        bb_upper    REAL,
        bb_middle   REAL,
        bb_lower    REAL,
        supertrend  REAL,
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chart_annotations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_slug    TEXT NOT NULL,
        type        TEXT NOT NULL,
        timeframe   TEXT NOT NULL,
        x1          INTEGER NOT NULL,
        y1          REAL,
        x2          INTEGER,
        y2          REAL,
        label       TEXT,
        color       TEXT DEFAULT '#00d4aa',
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_deals_bot_slug ON deals(bot_slug)",
    "CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_deal_id ON orders(deal_id)",
    "CREATE INDEX IF NOT EXISTS idx_indicator_snapshots_bot_slug "
    "ON indicator_snapshots(bot_slug)",
    "CREATE INDEX IF NOT EXISTS idx_chart_annotations_bot_slug "
    "ON chart_annotations(bot_slug)",
)


def get_db() -> sqlite3.Connection:
    """Return the calling thread's cached connection, creating it lazily.

    On first access for a thread the connection is opened, WAL mode is
    enabled (so readers never block on a writer), foreign keys are turned
    on, and row_factory is set to sqlite3.Row so callers can use
    dict-style access.
    """
    conn = getattr(_connection_cache, "conn", None)
    if conn is not None:
        return conn

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # busy_timeout: when a write hits a locked DB (another writer in
    # progress) wait up to 5s for the lock instead of failing with
    # SQLITE_BUSY immediately. Combined with the per-process
    # threading.Lock in deal_store this means a parallel test run or
    # a transient burst of writes from multiple bots no longer
    # surfaces as an exception in the engine tick.
    conn.execute("PRAGMA busy_timeout=5000")
    # synchronous=NORMAL is the SQLite-recommended pairing with WAL:
    # ~10x write throughput vs FULL with no risk of corruption (only
    # of losing the most recent commit on an OS-level crash, which is
    # acceptable here — the JSON state file is the live-state source
    # of truth and the DB ledger is append-only history).
    conn.execute("PRAGMA synchronous=NORMAL")
    _connection_cache.conn = conn
    return conn


def init_db() -> None:
    """Create all tables + indexes in a single transaction. Idempotent."""
    conn = get_db()
    with conn:
        for stmt in _SCHEMA_STATEMENTS:
            conn.execute(stmt)


def close_db() -> None:
    """Close the current thread's cached connection if one exists."""
    conn = getattr(_connection_cache, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _connection_cache.conn = None


def set_db_path(path: Path) -> None:
    """Point the module at a different DB file (used by tests).

    Closes the current thread's cached connection so the next get_db()
    call reopens against the new path. Tests swap this to a tmp_path
    fixture so they never touch the real logs/reverto.db.
    """
    global _DB_PATH
    close_db()
    _DB_PATH = Path(path)
