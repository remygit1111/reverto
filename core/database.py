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
#
# Multi-tenant foundation (Fase 1, SCHEMA_VERSION = 3):
#   A ``users`` table was introduced and every OWNED table (deals, orders,
#   chart_annotations, backtest_runs) gained a NOT NULL FK on users(id).
#   Phase 1 only seeds one admin row (id=1); Phase 2 will wire session-
#   based user resolution. The migration from older schemas is destructive
#   (drop + recreate) — by design, because the pre-MT schema had no
#   user_id column and back-filling every historical row with user_id=1
#   before adding the NOT NULL constraint would have required a second
#   migration step with its own failure modes. See docs/architecture.md
#   "Multi-tenant foundation (Fase 1)".

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent
_DB_PATH: Path = _BASE_DIR / "logs" / "reverto.db"

# One connection per thread. sqlite3 raises if a connection created on thread
# A is used from thread B, and we have multiple threads touching the DB.
_connection_cache = threading.local()


# ── Schema definitions ─────────────────────────────────────────────────────
# Ordered: users first (everything else FK-references it).

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT NOT NULL UNIQUE,
        active      INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # Seed the single admin user. INSERT OR IGNORE keeps init_db
    # idempotent — re-running against a populated DB is a no-op.
    "INSERT OR IGNORE INTO users (id, username) VALUES (1, 'admin')",
    """
    CREATE TABLE IF NOT EXISTS deals (
        id          TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
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
        entry_trigger TEXT DEFAULT NULL,
        exit_trigger  TEXT DEFAULT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id          TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        deal_id     TEXT NOT NULL REFERENCES deals(id),
        -- ON DELETE CASCADE intentionally omitted: save_deal() uses
        -- INSERT OR REPLACE which internally DELETEs then re-INSERTs
        -- the parent row, and CASCADE would wipe all child orders on
        -- every DCA update. Application-level cleanup is used instead.
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
    CREATE TABLE IF NOT EXISTS chart_annotations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
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
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        bot_slug    TEXT NOT NULL,
        bot_name    TEXT NOT NULL,
        start_date  TEXT NOT NULL,
        end_date    TEXT NOT NULL,
        timeframe   TEXT NOT NULL,
        initial_balance_btc REAL NOT NULL,
        final_balance_btc   REAL,
        total_pnl_btc       REAL,
        total_pnl_pct       REAL,
        total_deals         INTEGER,
        winning_deals       INTEGER,
        losing_deals        INTEGER,
        win_rate            REAL,
        avg_duration_hours  REAL,
        max_duration_hours  REAL,
        total_fees_btc      REAL,
        max_drawdown_pct    REAL,
        profit_factor       REAL,
        sharpe_ratio        REAL,
        sortino_ratio       REAL,
        calmar_ratio        REAL,
        recovery_factor     REAL,
        expectancy_btc      REAL,
        avg_win_loss_ratio  REAL,
        omega_ratio         REAL,
        buy_hold_pnl_pct    REAL,
        max_consecutive_wins   INTEGER,
        max_consecutive_losses INTEGER,
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    # Indexes — users lookup + every (user_id, bot_slug) hot query path.
    "CREATE INDEX IF NOT EXISTS idx_deals_user_id ON deals(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_deals_user_bot ON deals(user_id, bot_slug)",
    "CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_deal_id ON orders(deal_id)",
    "CREATE INDEX IF NOT EXISTS idx_chart_annotations_user_id "
    "ON chart_annotations(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_chart_annotations_user_bot "
    "ON chart_annotations(user_id, bot_slug)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_user_id "
    "ON backtest_runs(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_user_bot "
    "ON backtest_runs(user_id, bot_slug)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_at "
    "ON backtest_runs(created_at DESC)",
)


# ── Owned tables (dropped during v<3→v3 migration) ─────────────────────────
# Order matters: drop children before parents so FKs don't complain even
# though we have foreign_keys=ON.
_OWNED_TABLES: tuple[str, ...] = (
    "orders",
    "chart_annotations",
    "backtest_runs",
    "deals",
    "users",  # dropped last + first to be recreated
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


# Schema version sentinel — stored in SQLite's built-in PRAGMA user_version.
# Bump this whenever a schema change lands. _migrate_schema inspects the
# stored value and applies the appropriate transition:
#   * < 3  → destructive drop-and-recreate (multi-tenant foundation).
#     Pre-MT deals/backtest_runs had no user_id column and the NOT NULL
#     constraint can't be added without a full table rewrite anyway.
#   * == 3 → no-op.
SCHEMA_VERSION = 3


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply any pending migration to the current DB.

    v3 (multi-tenant foundation) is a CLEAN SLATE migration: older
    schemas are dropped and recreated. We emit a WARNING level log so
    operators see it in portal.log even at default verbosity; the
    data-loss is intentional (documented in the module docstring and
    docs/architecture.md, and guarded by scripts/reset_db.py which
    backs up the old DB first).
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0] or 0
    if current == SCHEMA_VERSION:
        return
    if current < SCHEMA_VERSION:
        logger.warning(
            "Multi-tenant schema migration: dropping owned tables from "
            "schema v%d and recreating at v%d. All deal/order/annotation/"
            "backtest history will be wiped. Run scripts/reset_db.py "
            "first if you haven't already — it backs up the DB before "
            "calling init_db().",
            current, SCHEMA_VERSION,
        )
        for table in _OWNED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    else:
        # Future-you downgraded the code; we don't know the new tables.
        # Refuse to touch the DB rather than lose data silently.
        raise RuntimeError(
            f"DB schema is at version {current}, code expects {SCHEMA_VERSION}. "
            f"Roll forward the code or restore a matching DB snapshot.",
        )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db() -> None:
    """Bring the DB up to the current schema version + seed admin user.

    Migration-first: if the stored version is below SCHEMA_VERSION we
    drop the owned tables BEFORE running _SCHEMA_STATEMENTS, so the
    CREATE TABLE statements land on a clean slate. Idempotent at v3:
    re-running against an already-migrated DB is a no-op (the
    ``CREATE TABLE IF NOT EXISTS`` and ``INSERT OR IGNORE`` keep it
    safe).
    """
    conn = get_db()
    with conn:
        _migrate_schema(conn)
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
