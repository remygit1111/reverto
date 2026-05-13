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
#   Phase 1 only seeds one admin row (id=1); Phase 2 wired session-based
#   resolution to the composite (user_id, slug) key. The migration from
#   older schemas is destructive (drop + recreate) — by design, because
#   the pre-MT schema had no user_id column.
#
# Phase-3a DB-based auth (SCHEMA_VERSION = 4):
#   The ``users`` table gained three columns that used to live in
#   logs/.auth.json: ``password_hash`` (bcrypt, nullable — provisioned
#   via scripts/setup_admin.py post-migration), ``role`` ('admin'|'user'),
#   and ``session_epoch`` (per-user invalidation counter; was global).
#   v3 → v4 is ALSO destructive because the users table is re-created;
#   deals/orders/annotations/backtest_runs are on its FK chain so they
#   get wiped too. Operator SLA: run scripts/reset_db.py first (backup),
#   then `make start` (runs migration), then `make setup-admin` to set
#   the admin password. Without the third step, NOBODY can log in —
#   password_hash is NULL on the seeded admin row.

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class DatabaseMigrationError(Exception):
    """Raised when a schema migration cannot proceed safely.

    Typical cause: a destructive migration would drop user data
    (deals, orders, annotations, backtest_runs, users state)
    without explicit operator opt-in via the
    ``REVERTO_DESTRUCTIVE_MIGRATE`` environment variable.

    Audit v26 v26-10 regression guard. Pre-fix ``init_db()`` would
    silently DROP + recreate owned tables on a version mismatch,
    which meant a routine ``make start`` after upgrading the code
    could wipe live parity-test or production data without any
    operator acknowledgement.
    """
    pass


# Env-var name operators set to opt-in to destructive schema migration.
_DESTRUCTIVE_OPT_IN_ENV = "REVERTO_DESTRUCTIVE_MIGRATE"

_BASE_DIR = Path(__file__).parent.parent
_DB_PATH: Path = _BASE_DIR / "logs" / "reverto.db"

# Phase-3a: legacy auth blob that pre-migration installs used for
# admin credentials. Archived on first init_db() so operators don't
# keep a stale file that no runtime-path reads anymore.
_LEGACY_AUTH_FILE: Path = _BASE_DIR / "logs" / ".auth.json"
_LEGACY_INITIAL_PW_FILE: Path = _BASE_DIR / "logs" / ".initial_password"

# One connection per thread. sqlite3 raises if a connection created on thread
# A is used from thread B, and we have multiple threads touching the DB.
_connection_cache = threading.local()

# Monotonic counter bumped on every ``set_db_path`` call. Each thread's
# ``_connection_cache`` records the version its cached conn was opened
# under; on the next ``get_db`` the thread compares cached vs. current
# version and drops its stale conn if they diverge.
#
# Why this is needed: conftest.py's autouse ``_isolate_reverto_db``
# fixture calls ``set_db_path(tmp)`` + ``init_db()`` before every test,
# then ``close_db()`` on teardown. ``close_db()`` only closes the
# caller thread's conn — the TestClient's anyio worker-pool threads
# persist between tests with a cached conn pointing at the previous
# test's tmp-DB. Any read/write on that stale conn lands in the wrong
# SQLite file, and the test that triggered the path-change sees
# phantom "user not found" / "no row" responses.
#
# This surfaces deterministically on Python 3.13 (stricter
# ResourceWarning handling + different GC timing for the old tmp dirs);
# 3.12 + WSL2 happened to get lucky. No lock needed — a worker that
# races the counter just sees the mismatch on its *next* ``get_db``
# and self-corrects. A one-request window of stale reads is acceptable
# because production never calls ``set_db_path`` at runtime.
_DB_PATH_VERSION: int = 0


# ── Schema definitions ─────────────────────────────────────────────────────
# Ordered: users first (everything else FK-references it).

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        username               TEXT NOT NULL UNIQUE,
        password_hash          TEXT,
        role                   TEXT NOT NULL DEFAULT 'user',
        session_epoch          INTEGER NOT NULL DEFAULT 0,
        active                 INTEGER NOT NULL DEFAULT 1,
        created_at             TEXT NOT NULL DEFAULT (datetime('now')),
        -- v6 (feat/login-security-hardening): per-account failed-
        -- login tracking. Used by /auth/login for exponential
        -- backoff + per-account rate-limiting + anomaly logging.
        -- NOT NULL with DEFAULT 0 so fresh installs skip the
        -- v5→v6 ALTER path and existing installs auto-backfill.
        failed_login_count     INTEGER NOT NULL DEFAULT 0,
        last_failed_login_at   TEXT,
        -- v9 (feat/totp-foundation): TOTP 2FA seed, encrypted with the
        -- user's Fernet key. NULL = user has not enrolled. Phase B PR 2
        -- will add the enrollment flow that populates this column;
        -- Phase B PR 3 will require it for /auth/login. For Phase B
        -- PR 1 (this one) the column is purely structural — no read
        -- path consults it yet.
        totp_seed_encrypted    TEXT DEFAULT NULL
    )
    """,
    # Seed the single admin user. INSERT OR IGNORE keeps init_db
    # idempotent — re-running against a populated DB is a no-op.
    # password_hash stays NULL: the operator provisions it via
    # scripts/setup_admin.py post-migration. Without that step no
    # login succeeds (verify_password fails closed on NULL hash).
    "INSERT OR IGNORE INTO users (id, username, role) "
    "VALUES (1, 'admin', 'admin')",
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
    # v5 additive: changelog entries surfaced on /changelog, managed
    # via /admin/changelog. Unowned table (no user_id FK) — an entry
    # is a property of the product, not of a tenant.
    """
    CREATE TABLE IF NOT EXISTS changelog_entries (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        title             TEXT NOT NULL,
        description       TEXT NOT NULL,
        category          TEXT NOT NULL,
        is_published      INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        published_at      TEXT,
        source_commit_sha TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_changelog_entries_published "
    "ON changelog_entries(is_published, published_at DESC)",
    # v10 additive: roadmap phases surfaced on /roadmap, managed via
    # /admin → Roadmap. Unowned table (no user_id FK) — like
    # changelog, an entry is a property of the product, not of a
    # tenant. ``phase_key`` is the immutable machine identifier
    # (e.g. "phase-3a"); ``sort_order`` is operator-controlled via
    # drag-and-drop (multiples of 10 to allow inserting between
    # later without renumbering all). status CHECK constraint
    # mirrors the application-layer VALID_STATUSES — defence in
    # depth for direct-SQL writes.
    """
    CREATE TABLE IF NOT EXISTS roadmap_phases (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        phase_key         TEXT NOT NULL UNIQUE,
        display_name      TEXT NOT NULL,
        summary           TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','active','done')),
        sort_order        INTEGER NOT NULL DEFAULT 0,
        body_md           TEXT NOT NULL DEFAULT '',
        effort_estimate   TEXT NOT NULL DEFAULT '',
        in_progress_note  TEXT NOT NULL DEFAULT '',
        audit_checkpoint  TEXT NOT NULL DEFAULT '',
        is_published      INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
        published_at      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_roadmap_published "
    "ON roadmap_phases(is_published, sort_order)",
    "CREATE INDEX IF NOT EXISTS idx_roadmap_status "
    "ON roadmap_phases(status, sort_order)",
    # v7 additive: workspace dashboard layouts. One row per
    # (user_id, name). ``layout_json`` is opaque to the backend —
    # the frontend owns the panel schema; backend only validates
    # parseability + a 16 KB size cap (enforced in
    # core.dashboard_store.put_layout). Schema-ready for future
    # multi-layout UI via the ``name`` column, but the current
    # API surface only speaks to the default row.
    """
    CREATE TABLE IF NOT EXISTS dashboard_layouts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name        TEXT NOT NULL DEFAULT 'default',
        layout_json TEXT NOT NULL,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (user_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dashboard_layouts_user "
    "ON dashboard_layouts(user_id)",
    # v8 additive: audit/pentest findings tracker. One row per
    # finding, keyed by ``finding_id`` (e.g. r3-001, pt-043). The
    # markdown audit-docs remain authoritative for description/
    # rationale; this table tracks operator-mutable status + notes
    # so the admin UI can roll up open vs resolved without grepping
    # eight markdown files. ``description`` is denormalised from the
    # source doc at seed time so the admin list view does not need
    # to re-render markdown on every paint.
    """
    CREATE TABLE IF NOT EXISTS audit_findings (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        finding_id      TEXT NOT NULL UNIQUE,
        source_doc      TEXT NOT NULL,
        severity        TEXT NOT NULL CHECK (
                          severity IN ('CRITICAL','HIGH','MEDIUM','LOW','INFO')
                        ),
        status          TEXT NOT NULL CHECK (
                          status IN ('open','in_progress','resolved','accepted','deferred')
                        ),
        title           TEXT NOT NULL,
        description     TEXT NOT NULL DEFAULT '',
        resolution_ref  TEXT,
        notes           TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_findings_filter "
    "ON audit_findings(status, severity)",
    "CREATE INDEX IF NOT EXISTS idx_audit_findings_source "
    "ON audit_findings(source_doc)",
    # v11 / v12: per-user exchange account metadata. Replaces the
    # one-account-per-exchange-type assumption with a real n-to-one
    # model so an operator can keep "Bitget main" alongside
    # "Bitget test" before live trading. ``credentials_uuid`` is the
    # filename stem of the matching encrypted blob at
    # ``credentials/<user_id>/<uuid>.enc``. is_default is enforced as
    # at-most-one-per-(user,exchange_type) at the application layer
    # (see core.exchange_account_store.set_default) — SQLite partial
    # indexes are awkward and the application-layer guard is the
    # mutating-call boundary anyway.
    #
    # v12 added the ``market_type`` column (Bitget Spot vs Coin-M vs
    # USDT-M vs USDC-M, Kraken Spot vs Futures) and widened the
    # UNIQUE constraint to (user, exchange_type, market_type, alias)
    # so the same alias can sit on two different wallets. v11→v12 is
    # a destructive drop-and-recreate of this table only (the v11
    # rows had no market_type and a NOT NULL ALTER would force every
    # row to 'spot' — incorrect for Coin-M operators). The wider
    # owned-table destructive guard does NOT trigger; see
    # ``_apply_destructive_table_recreates``.
    """
    CREATE TABLE IF NOT EXISTS exchange_accounts (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        exchange_type    TEXT NOT NULL,
        market_type      TEXT NOT NULL,
        alias            TEXT NOT NULL,
        credentials_uuid TEXT NOT NULL,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        last_tested_at   TEXT,
        is_default       INTEGER NOT NULL DEFAULT 0,
        UNIQUE(user_id, exchange_type, market_type, alias)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_exchange_accounts_user_type "
    "ON exchange_accounts(user_id, exchange_type)",
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

    Version check: if ``set_db_path`` bumped ``_DB_PATH_VERSION`` since
    the cached conn was minted, the cached conn is pointing at a
    now-stale file path. Drop it + reopen against the current path.
    """
    conn = getattr(_connection_cache, "conn", None)
    cached_version = getattr(_connection_cache, "version", -1)
    if conn is not None and cached_version == _DB_PATH_VERSION:
        return conn

    # Stale cache (path changed) or first call on this thread. Close
    # any lingering handle before opening the new one so the old
    # sqlite file descriptor is released cleanly.
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass

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
    _connection_cache.version = _DB_PATH_VERSION
    return conn


# Schema version sentinel — stored in SQLite's built-in PRAGMA user_version.
# Bump this whenever a schema change lands. _migrate_schema inspects the
# stored value and applies the appropriate transition:
#   * < 3  → destructive drop-and-recreate (multi-tenant foundation).
#     Pre-MT deals/backtest_runs had no user_id column.
#   * < 4  → destructive drop-and-recreate (Phase-3a DB-based auth).
#     users gains password_hash/role/session_epoch — since every owned
#     table FK-references users, we drop the whole tree to recreate it
#     on a clean slate. Operator SLA: backup first, provision admin
#     password after (see scripts/setup_admin.py).
#   * < 5  → ADDITIVE: introduces the ``changelog_entries`` table. No
#     existing rows are touched; ``_SCHEMA_STATEMENTS`` runs every
#     ``init_db()`` with ``CREATE TABLE IF NOT EXISTS`` semantics so
#     the new table is created lazily on the next boot. The destructive
#     guard explicitly does NOT trigger on this path.
#   * < 6  → ADDITIVE: adds ``failed_login_count`` +
#     ``last_failed_login_at`` columns to ``users`` for login-
#     security-hardening (exponential backoff + per-account rate-
#     limit + anomaly logging). Uses ALTER TABLE ADD COLUMN — SQLite
#     supports this directly for columns with constant DEFAULT
#     values. Fresh installs never take this path because
#     ``_SCHEMA_STATEMENTS`` above already declares the v6 shape.
#   * < 7  → ADDITIVE: introduces ``dashboard_layouts`` for the
#     workspace-dashboard feature. No existing rows touched;
#     ``_SCHEMA_STATEMENTS`` runs with ``CREATE TABLE IF NOT EXISTS``
#     so the new table lands lazily on the next boot. Destructive
#     guard explicitly does NOT trigger — v5 was the last
#     destructive boundary.
#   * < 8  → ADDITIVE: introduces ``audit_findings`` for the admin
#     findings tracker. No existing rows touched; CREATE TABLE IF
#     NOT EXISTS lazily lands the new table on the next boot.
#   * < 9  → ADDITIVE: adds ``users.totp_seed_encrypted`` column for
#     Phase B TOTP 2FA foundation. ALTER TABLE ADD COLUMN with
#     DEFAULT NULL — every existing row reads back NULL ("user has
#     not enrolled"), no data wiped. The fresh-install schema above
#     declares the v9 shape directly so a clean install never takes
#     the ALTER path.
#   * < 10 → ADDITIVE: introduces the ``roadmap_phases`` table
#     backing the public ``/roadmap`` page + admin SPA tab. Like
#     ``changelog_entries`` it is unowned (no user_id FK). No
#     existing rows are touched; ``_SCHEMA_STATEMENTS`` runs every
#     ``init_db()`` with ``CREATE TABLE IF NOT EXISTS`` semantics
#     so the new table is created lazily on the next boot. The
#     destructive guard explicitly does NOT trigger — v5 was the
#     last destructive boundary and additive jumps fully above it
#     run silently.
#   * < 11 → ADDITIVE: introduces ``exchange_accounts`` for the
#     multi-account exchange-management feature. Replaces the
#     previous one-account-per-(user, exchange_type) layout in
#     ``credentials/<user_id>/<exchange>.enc`` with a UUID-named
#     blob keyed by an ``exchange_accounts`` row. No existing rows
#     are touched; CREATE TABLE IF NOT EXISTS lazily lands the new
#     table on the next boot. The destructive guard does NOT
#     trigger — v5 was the last destructive boundary.
#   * < 12 → TABLE-LOCAL DESTRUCTIVE: adds ``market_type`` to
#     ``exchange_accounts`` and widens the UNIQUE constraint to
#     (user, exchange_type, market_type, alias). Done by DROPping
#     and re-CREATEing only this one v11-introduced table —
#     existing v11 rows had no market_type and silently defaulting
#     them to 'spot' would have routed Coin-M operators to the
#     wrong wallet. Owned-table destructive guard does NOT trigger
#     because ``exchange_accounts`` is not on the ``_OWNED_TABLES``
#     list (no deals/orders FK against it). The operator's
#     ``.enc`` blobs are likewise wiped (see
#     ``_apply_destructive_table_recreates``) so the orphan-file
#     state stays clean.
#   * == 12 → no-op.
SCHEMA_VERSION = 12

# Version at which the last destructive drop-and-recreate landed. Any
# upgrade that crosses this boundary (stored ``user_version`` below it,
# running code at or above it) requires operator opt-in via
# ``REVERTO_DESTRUCTIVE_MIGRATE=1``. Version jumps fully above this line
# (e.g. v4 → v5) are additive and run silently.
_LAST_DESTRUCTIVE_VERSION = 4


# rha-002: bounds for ``PRAGMA user_version`` reads. SQLite stores
# user_version as a signed 32-bit integer, so any value outside
# [0, SCHEMA_VERSION] indicates either DB corruption or a forward-
# version DB that this code does not know how to migrate. The upper
# cap is generous (we accept "unknown future ≤ 999") but bounded so
# a corrupted page returning a random integer (e.g. 2147483647)
# fails closed instead of being treated as a future version.
_SCHEMA_VERSION_MIN = 0
_SCHEMA_VERSION_MAX = 999


class SchemaVersionError(DatabaseMigrationError):
    """Raised when ``PRAGMA user_version`` is out of range or beyond
    what the running code can migrate from.

    rha-002: pre-fix the inline ``conn.execute("PRAGMA user_version")
    .fetchone()[0] or 0`` accepted any integer (or a non-integer that
    truthiness-checked to itself), so a corrupted DB or a forward-
    version snapshot would either crash deep in the migration logic
    or silently treat a corrupted value as version 0 and trigger a
    destructive migration on real data.

    Subclass of ``DatabaseMigrationError`` so existing callers that
    catch the broader migration error keep working — this just
    refines the failure mode for telemetry / log triage.
    """


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Read ``PRAGMA user_version`` with range validation.

    Three layers of defence:

    1. **Type-check** — SQLite returns ``int`` for this PRAGMA, but
       a corrupted DB or a non-SQLite file masquerading as one could
       in theory return ``None`` or a string. Refuse anything non-int.
    2. **Bounds-check** — values outside ``[_SCHEMA_VERSION_MIN,
       _SCHEMA_VERSION_MAX]`` are corruption signals (random integer
       from a damaged page) rather than legitimate future versions.
    3. **Forward-compatibility** — values strictly above
       ``SCHEMA_VERSION`` mean the DB was migrated by a newer Reverto
       than this checkout knows how to handle. Refuse to touch the DB
       rather than risk a silent downgrade or destructive migration.

    A fresh install reads ``0`` from PRAGMA user_version (SQLite's
    default), which passes through here and lets ``_migrate_schema``
    take the destructive-or-additive branch as appropriate.
    """
    raw = conn.execute("PRAGMA user_version").fetchone()[0]
    if not isinstance(raw, int):
        raise SchemaVersionError(
            f"PRAGMA user_version returned non-integer ({type(raw).__name__}: "
            f"{raw!r}). Database may be corrupted; restore from a backup.",
        )
    if raw < _SCHEMA_VERSION_MIN:
        raise SchemaVersionError(
            f"PRAGMA user_version is {raw}, below minimum "
            f"{_SCHEMA_VERSION_MIN}. Database may be corrupted; restore "
            f"from a backup.",
        )
    if raw > _SCHEMA_VERSION_MAX:
        raise SchemaVersionError(
            f"PRAGMA user_version is {raw}, above the sanity cap "
            f"{_SCHEMA_VERSION_MAX}. This is almost certainly a "
            f"corrupted page rather than a legitimate future version. "
            f"Restore from a backup.",
        )
    if raw > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"DB schema is at version {raw}, code expects {SCHEMA_VERSION}. "
            f"This DB was migrated by a newer Reverto. Roll forward the "
            f"code or restore a matching DB snapshot before continuing.",
        )
    return raw


def _has_existing_owned_data(conn: sqlite3.Connection) -> bool:
    """True if any owned table already contains rows.

    Used to distinguish a fresh install (no data to lose → migration
    is just CREATE TABLE statements) from an upgrade of an existing
    install (where DROP actually destroys operator data). Fresh
    installs never trigger the destructive-migration guard.

    A table that does not exist yet is ignored (``OperationalError``
    on SELECT) — that means it's about to be created, not dropped.
    """
    for table in _OWNED_TABLES:
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} LIMIT 1",
            ).fetchone()
            if row and int(row[0]) > 0:
                return True
        except sqlite3.OperationalError:
            # Table doesn't exist — nothing to drop for this one.
            continue
    return False


def _create_pre_migration_backup() -> Path:
    """Create a WAL-aware SQLite backup before destructive migration.

    Uses ``sqlite3.Connection.backup()`` (not ``shutil.copy``) so
    WAL-mode databases are captured consistently. Without this,
    uncommitted pages in the WAL file would be missed by a plain
    file copy and the "backup" would be silently partial.

    Returns the path to the backup file. Caller is expected to log
    it prominently so the operator can find it post-migration.
    Format: ``logs/pre-migration-backup-YYYYMMDD-HHMMSS.db``.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = _DB_PATH.parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"pre-migration-backup-{timestamp}.db"

    # Open a fresh sqlite3 connection to the source — NOT the one
    # that `_migrate_schema` is using, because that connection is in
    # the middle of a transaction. A dedicated connection sees the
    # committed state, which is exactly what we want to snapshot.
    source = sqlite3.connect(str(_DB_PATH))
    try:
        dest = sqlite3.connect(str(backup_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return backup_path


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply any pending migration to the current DB.

    Every migration path we currently know is a CLEAN SLATE drop +
    recreate of the owned tree. Older in-line schema-alter attempts
    are avoided: SQLite's ALTER TABLE set is narrow, and back-filling
    NOT NULL / UNIQUE constraints on an existing table requires a full
    rewrite anyway.

    Audit v26 v26-10 guard: when the DB already contains owned-table
    data, the destructive path requires explicit operator opt-in via
    ``REVERTO_DESTRUCTIVE_MIGRATE=1``. A pre-migration backup is
    auto-created just before the DROP so the operator can roll back
    if the new schema turns out to be wrong. Fresh installs (no
    owned-table rows) skip the guard — there is no data to destroy.
    """
    # rha-002: range-validated read. Values outside the sane range or
    # beyond ``SCHEMA_VERSION`` raise ``SchemaVersionError`` (a
    # ``DatabaseMigrationError`` subclass) rather than slipping through
    # to a destructive migration on a corrupted PRAGMA value.
    current = _read_schema_version(conn)
    if current == SCHEMA_VERSION:
        return

    # Path 1: destructive — only when the stored version predates the
    # last destructive schema change. Every migration ≤ v4 used the
    # drop-and-recreate pattern; additive-only versions (v5+) never
    # take this branch.
    if current < _LAST_DESTRUCTIVE_VERSION:
        destructive = _has_existing_owned_data(conn)
        if destructive:
            if os.getenv(_DESTRUCTIVE_OPT_IN_ENV) != "1":
                raise DatabaseMigrationError(
                    f"Destructive schema migration required "
                    f"(v{current} → v{_LAST_DESTRUCTIVE_VERSION}). This "
                    f"will DROP owned tables (deals, orders, "
                    f"annotations, backtest_runs, and user password/"
                    f"role/session_epoch data).\n"
                    f"To proceed, restart with "
                    f"{_DESTRUCTIVE_OPT_IN_ENV}=1 set. A pre-migration "
                    f"backup will be created automatically at "
                    f"logs/pre-migration-backup-YYYYMMDD-HHMMSS.db.\n"
                    f"See docs/OPERATIONS.md section 'Schema migrations' "
                    f"for details including restore procedure."
                )
            backup_path = _create_pre_migration_backup()
            logger.warning(
                "Destructive migration v%d → v%d authorized via %s=1. "
                "Pre-migration backup created: %s",
                current, _LAST_DESTRUCTIVE_VERSION,
                _DESTRUCTIVE_OPT_IN_ENV, backup_path,
            )
        else:
            # Fresh install — no data to lose, guard doesn't apply.
            logger.info(
                "Schema initialisation from v%d to v%d (no existing "
                "owned-table data — fresh install, no backup needed).",
                current, SCHEMA_VERSION,
            )
        logger.warning(
            "Schema migration: dropping owned tables from v%d and "
            "recreating at v%d. Deal/order/annotation/backtest + user "
            "password/role/session_epoch data is wiped. After the "
            "migration, run scripts/setup_admin.py to provision the "
            "admin password (login is blocked until you do).",
            current, _LAST_DESTRUCTIVE_VERSION,
        )
        for table in _OWNED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")

    # Path 2: additive — every version jump that does not cross the
    # last destructive boundary. ``_SCHEMA_STATEMENTS`` runs in
    # ``init_db()`` below with ``CREATE TABLE IF NOT EXISTS`` /
    # ``CREATE INDEX IF NOT EXISTS``, so new tables and indexes land
    # idempotently without touching existing rows. Bumping
    # ``user_version`` is all that's needed here — no drops, no
    # backup, no operator opt-in.
    elif current < SCHEMA_VERSION:
        logger.info(
            "Additive schema migration v%d → v%d (no data touched).",
            current, SCHEMA_VERSION,
        )

    # Per-column additive migrations. ``CREATE TABLE IF NOT EXISTS``
    # won't rewrite an existing table when new columns appear in the
    # statement, so column additions require explicit ALTER TABLE.
    # SQLite's ADD COLUMN supports constant DEFAULT values (which is
    # all we need here); the try/except handles idempotency when this
    # runs on a DB that's already been migrated.
    if current < SCHEMA_VERSION:
        _apply_column_additions(conn)
        _apply_destructive_table_recreates(conn, current)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _apply_column_additions(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE ADD COLUMN migrations.

    v5 → v6: ``users.failed_login_count`` + ``users.last_failed_login_at``
    for login-security-hardening.
    v8 → v9: ``users.totp_seed_encrypted`` for Phase B TOTP 2FA.

    Each column is added in its own try/except so a mid-migration
    crash that added one column but not the next still converges on
    the next boot.
    """
    _add_column_if_missing(
        conn, "users",
        "failed_login_count INTEGER NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        conn, "users",
        "last_failed_login_at TEXT",
    )
    _add_column_if_missing(
        conn, "users",
        "totp_seed_encrypted TEXT DEFAULT NULL",
    )


def _apply_destructive_table_recreates(
    conn: sqlite3.Connection, current_version: int,
) -> None:
    """Drop and recreate tables whose schema change can't be expressed
    as an additive ALTER. The targeted table must NOT be on
    ``_OWNED_TABLES`` (which is the full destructive-migration guard
    domain). Use this path only for tables introduced in a recent
    version with no FK consumers and no operator-irreplaceable data.

    v11 → v12: ``exchange_accounts`` gains a ``market_type`` column
    and the UNIQUE constraint widens. ALTER TABLE ADD COLUMN
    NOT NULL needs a DEFAULT, and defaulting every existing row to
    ``'spot'`` would silently mis-route a Coin-M operator's bots to
    the wrong wallet (the v11 schema implicitly used USDT-M). Dropping
    + recreating instead, plus wiping the matching ``.enc`` blobs
    under ``credentials/<user>/``, forces the operator to recreate
    their account via the admin tile with an explicit market choice.
    The operator confirmed (feat/exchange-account-market-type) that
    no production rows need preservation.
    """
    if current_version < 12:
        # Best-effort wipe of the .enc blobs that used to back the
        # now-dropped rows. The DB drop will create orphan files
        # otherwise — harmless functionally (nothing enumerates by
        # filename) but noisy.
        try:
            row_iter = conn.execute(
                "SELECT user_id, credentials_uuid FROM exchange_accounts",
            ).fetchall()
            for row in row_iter:
                try:
                    # Lazy import keeps core.database self-contained
                    # for tools that don't need the credentials tree.
                    from core.paths import uuid_creds_path
                    enc = uuid_creds_path(
                        int(row["user_id"]), str(row["credentials_uuid"]),
                    )
                    if enc.exists():
                        enc.unlink()
                except (OSError, ImportError) as e:
                    logger.warning(
                        "v11→v12 migration: could not unlink stale "
                        ".enc file: %s", e,
                    )
        except sqlite3.OperationalError:
            # exchange_accounts didn't exist (truly fresh install) —
            # nothing to wipe; the CREATE TABLE in _SCHEMA_STATEMENTS
            # builds the v12 shape directly.
            pass
        else:
            logger.warning(
                "v11→v12 migration: dropping exchange_accounts table "
                "(market_type added, UNIQUE constraint widened). "
                "Every existing exchange account row + its encrypted "
                ".enc blob is wiped. Recreate accounts via the "
                "Exchanges admin tile.",
            )
            conn.execute("DROP TABLE IF EXISTS exchange_accounts")


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column_def: str,
) -> None:
    """Run ``ALTER TABLE ... ADD COLUMN`` and swallow the two benign
    OperationalError cases:

      * ``duplicate column name ...`` — already-present column on a
        re-run against an already-migrated DB. Idempotency.
      * ``no such table: ...`` — fires on fresh installs where the
        destructive path dropped the table and ``_SCHEMA_STATEMENTS``
        hasn't re-created it yet. The about-to-run CREATE TABLE
        declares the column in the fresh-install shape, so the ALTER
        is redundant here. Swallowing the error keeps the single
        migration path handling both fresh-install and upgrade
        without a separate "table exists" pre-check.

    Any other OperationalError (disk full, corruption, permissions)
    re-raises so the bug surfaces loudly instead of silently
    skipping."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "no such table" in msg:
            return
        raise


def _archive_legacy_auth_file() -> None:
    """Rename logs/.auth.json → logs/.auth.json.pre_phase3.<ts> on
    the first init_db() call that sees it. Phase-3a moved auth state
    into users.*; the old Fernet blob has no readers anymore, so
    archiving it (rather than unlinking) preserves the audit trail
    without leaving a misleading file that an operator might think
    still matters.

    Idempotent — if the file is absent (fresh install, or previous
    init_db() already archived it) this is a no-op. ``.initial_password``
    gets the same treatment since it was always a sidecar of .auth.json.
    """
    for src in (_LEGACY_AUTH_FILE, _LEGACY_INITIAL_PW_FILE):
        if not src.exists():
            continue
        dst = src.with_suffix(src.suffix + f".pre_phase3.{int(time.time())}")
        try:
            src.rename(dst)
            logger.warning(
                "Phase-3a migration: archived %s → %s. Use "
                "scripts/setup_admin.py to provision the admin password.",
                src.name, dst.name,
            )
        except OSError as e:
            logger.warning("could not archive %s: %s", src, e)


def init_db() -> None:
    """Bring the DB up to the current schema version + seed admin user.

    Migration-first: if the stored version is below SCHEMA_VERSION we
    drop the owned tables BEFORE running _SCHEMA_STATEMENTS, so the
    CREATE TABLE statements land on a clean slate. Idempotent at v4:
    re-running against an already-migrated DB is a no-op (the
    ``CREATE TABLE IF NOT EXISTS`` and ``INSERT OR IGNORE`` keep it
    safe). As a Phase-3a side-effect, logs/.auth.json is archived on
    first run so the legacy blob doesn't linger beside a DB-based
    auth flow.
    """
    conn = get_db()
    with conn:
        _migrate_schema(conn)
        for stmt in _SCHEMA_STATEMENTS:
            conn.execute(stmt)
    _archive_legacy_auth_file()


def close_db() -> None:
    """Close the current thread's cached connection if one exists."""
    conn = getattr(_connection_cache, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _connection_cache.conn = None
        _connection_cache.version = -1


def set_db_path(path: Path) -> None:
    """Point the module at a different DB file (used by tests).

    Bumps ``_DB_PATH_VERSION`` so every thread's cached connection
    is invalidated on its next ``get_db`` call — not just the caller
    thread. The old per-thread ``close_db`` path only closed the
    caller's conn, leaving anyio worker-pool threads holding stale
    handles to the previous tmp-DB across tests.
    """
    global _DB_PATH, _DB_PATH_VERSION
    close_db()
    _DB_PATH = Path(path)
    _DB_PATH_VERSION += 1
