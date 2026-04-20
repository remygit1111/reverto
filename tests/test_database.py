# tests/test_database.py
# Covers the SQLite persistence layer: schema init, deal/order round-trips,
# filters, annotations CRUD, and the compute_stats aggregation.
#
# Each test gets a fresh DB in tmp_path via the autouse db_path fixture —
# the real logs/reverto.db is never touched.
#
# Post-MT (schema v3): every deal_store call takes user_id as a keyword
# argument. These tests pin user_id=1 (admin seed) throughout; tests
# specifically for cross-user isolation live in test_user_model.py.

import os
import sys
from datetime import datetime, UTC

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import database, deal_store
from paper.paper_state import PaperDeal, PaperOrder


_UID = 1  # seeded admin row


@pytest.fixture(autouse=True)
def db_path(tmp_path):
    """Point core.database at a fresh DB for every test."""
    database.set_db_path(tmp_path / "test.db")
    database.init_db()
    yield
    database.close_db()


def _order(n=1, price=80000.0, size=0.001, order_type="base"):
    return PaperOrder(
        order_number=n, price=price, size=size,
        timestamp=datetime.now(UTC), order_type=order_type,
    )


def _deal(deal_id="PAPER-0001", price=80000.0, orders=None, is_open=True):
    return PaperDeal(
        id=deal_id, bot_name="tb", symbol="BTC/USD",
        side="long", leverage=1,
        orders=orders if orders is not None else [_order(1, price)],
        is_open=is_open,
    )


def test_init_db_creates_tables():
    conn = database.get_db()
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for t in ("users", "deals", "orders", "chart_annotations", "backtest_runs"):
        assert t in names


def test_save_and_get_deal_roundtrip():
    d = _deal("PAPER-0001", 80000.0)
    deal_store.save_deal(d, bot_slug="tb", bot_name="tb", user_id=_UID)
    rows = deal_store.get_deals(user_id=_UID, bot_slug="tb")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "PAPER-0001"
    assert row["status"] == "open"
    assert row["initial_price"] == pytest.approx(80000.0)
    assert row["total_size"] == pytest.approx(0.001)
    assert row["bot_name"] == "tb"
    assert row["user_id"] == _UID


def test_close_deal_updates_status():
    d = _deal("PAPER-0002", 80000.0)
    deal_store.save_deal(d, "tb", "tb", user_id=_UID)
    deal_store.close_deal(
        "PAPER-0002", close_price=82400.0, close_reason="tp",
        pnl_btc=0.00003, pnl_pct=3.0, user_id=_UID,
    )
    rows = deal_store.get_deals(user_id=_UID, status="closed")
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "closed"
    assert r["close_reason"] == "tp"
    assert r["close_price"] == pytest.approx(82400.0)
    assert r["pnl_btc"] == pytest.approx(0.00003)
    assert r["closed_at"] is not None


def test_save_and_get_orders():
    d = _deal("PAPER-0003", 80000.0)
    deal_store.save_deal(d, "tb", "tb", user_id=_UID)
    o1 = _order(1, 80000.0, 0.001, "base")
    o2 = _order(2, 78000.0, 0.002, "dca")
    deal_store.save_order(o1, "PAPER-0003", "tb", user_id=_UID, fee_btc=0.0000006)
    deal_store.save_order(o2, "PAPER-0003", "tb", user_id=_UID, fee_btc=0.0000012)

    orders = deal_store.get_deal_orders("PAPER-0003", user_id=_UID)
    assert [o["order_number"] for o in orders] == [1, 2]
    assert orders[0]["order_type"] == "base"
    assert orders[1]["order_type"] == "dca"
    assert orders[1]["price"] == pytest.approx(78000.0)


def test_get_deals_filters():
    deal_store.save_deal(_deal("PAPER-0001"), "bot_a", "A", user_id=_UID)
    deal_store.save_deal(_deal("PAPER-0002"), "bot_b", "B", user_id=_UID)
    d3 = _deal("PAPER-0003", is_open=False)
    d3.close_reason = "tp"
    d3.closed_at = datetime.now(UTC)
    deal_store.save_deal(d3, "bot_a", "A", user_id=_UID)

    assert len(deal_store.get_deals(user_id=_UID, bot_slug="bot_a")) == 2
    assert len(deal_store.get_deals(user_id=_UID, bot_slug="bot_b")) == 1
    assert len(deal_store.get_deals(user_id=_UID, status="open")) == 2
    assert len(deal_store.get_deals(user_id=_UID, status="closed")) == 1


def test_annotation_crud():
    new_id = deal_store.save_annotation(
        bot_slug="tb", type_="line", timeframe="1h",
        x1=1_700_000_000, user_id=_UID, y1=80000.0, label="entry",
    )
    assert new_id > 0
    items = deal_store.list_annotations("tb", user_id=_UID)
    assert len(items) == 1
    assert items[0]["label"] == "entry"
    assert items[0]["timeframe"] == "1h"

    # filter by timeframe
    assert len(deal_store.list_annotations("tb", user_id=_UID, timeframe="1h")) == 1
    assert len(deal_store.list_annotations("tb", user_id=_UID, timeframe="4h")) == 0

    assert deal_store.delete_annotation(new_id, user_id=_UID) is True
    assert deal_store.list_annotations("tb", user_id=_UID) == []
    # second delete is a no-op
    assert deal_store.delete_annotation(new_id, user_id=_UID) is False


def test_compute_stats_basic():
    # No deals → zeros + note
    empty = deal_store.compute_stats(user_id=_UID)
    assert empty["total_deals"] == 0
    assert empty.get("note") == "no deals"

    # Three closed deals: 2 wins, 1 loss.
    for i, pnl in enumerate([0.002, 0.004, -0.001], start=1):
        d = _deal(f"PAPER-{i:04d}", is_open=False)
        d.close_reason = "tp" if pnl > 0 else "sl"
        d.closed_at = datetime.now(UTC)
        d.close_price = 80000.0
        d.pnl_btc = pnl
        d.pnl_pct = pnl * 100
        deal_store.save_deal(d, "tb", "tb", user_id=_UID)
        # Attach an order with a fee so total_fees_btc is non-zero.
        deal_store.save_order(
            _order(1, 80000.0), f"PAPER-{i:04d}", "tb",
            user_id=_UID, fee_btc=0.0000006,
        )

    stats = deal_store.compute_stats(user_id=_UID, bot_slug="tb")
    assert stats["total_deals"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["win_rate"] == pytest.approx(66.67, abs=0.01)
    assert stats["best_deal"] == pytest.approx(0.004)
    assert stats["worst_deal"] == pytest.approx(-0.001)
    assert stats["total_fees_btc"] == pytest.approx(3 * 0.0000006, rel=1e-6)


# ── Backtest runs ─────────────────────────────────────────────────────────────

def _sample_summary():
    return {
        "total_pnl_btc":  0.002,
        "total_pnl_pct":  2.0,
        "total_deals":    12,
        "wins":           8,
        "losses":         4,
        "win_rate":       66.67,
        "avg_duration_hours": 4.2,
        "max_duration_hours": 11.1,
        "total_fees_btc": 0.000072,
        "max_drawdown_pct": 3.5,
        "profit_factor":  1.8,
        "sharpe_ratio":   1.2,
        "sortino_ratio":  1.5,
        "calmar_ratio":   float("inf"),  # must be coerced to NULL
        "recovery_factor": 2.4,
        "expectancy_btc": 0.00003,
        "avg_win_loss_ratio": 1.9,
        "omega_ratio":    float("nan"),  # must be coerced to NULL
        "buy_hold_pnl_pct": 1.1,
        "max_consecutive_wins": 5,
        "max_consecutive_losses": 2,
    }


def test_save_and_fetch_backtest_runs():
    params = {
        "start_date": "2024-01-01T00:00:00Z",
        "end_date":   "2024-06-30T23:59:00Z",
        "timeframe":  "1h",
        "initial_balance_btc": 0.1,
    }
    row_id = deal_store.save_backtest_run(
        "btc", "BTC bot", params, _sample_summary(), user_id=_UID,
    )
    assert row_id > 0

    # Second run so we can assert ordering
    deal_store.save_backtest_run(
        "btc", "BTC bot", params, _sample_summary(), user_id=_UID,
    )
    deal_store.save_backtest_run(
        "eth", "ETH bot", params, _sample_summary(), user_id=_UID,
    )

    btc_runs = deal_store.get_backtest_runs("btc", user_id=_UID)
    assert len(btc_runs) == 2
    # id desc ordering
    assert btc_runs[0]["id"] > btc_runs[1]["id"]
    assert btc_runs[0]["bot_name"] == "BTC bot"
    # NaN / Inf round-tripped as NULL
    assert btc_runs[0]["calmar_ratio"] is None
    assert btc_runs[0]["omega_ratio"] is None
    # Regular numbers stored verbatim
    assert btc_runs[0]["profit_factor"] == pytest.approx(1.8)
    assert btc_runs[0]["total_deals"] == 12

    all_runs = deal_store.get_all_backtest_runs(user_id=_UID)
    assert len(all_runs) == 3
    # Mixed-slug query returns rows from both bots
    assert {r["bot_slug"] for r in all_runs} == {"btc", "eth"}


# ── Config model toggle serialisation ─────────────────────────────────────────

def test_config_toggle_serialisation():
    """Verify that enabled/disabled toggles produce the right config shapes."""
    from config.models import BotConfig
    import yaml

    yaml_tp_disabled = """
bot:
  name: toggle-test
  mode: paper
  exchange: bitget
  pair: BTC/USD
  contract_type: inverse_perpetual
  leverage: {enabled: false, size: 1}
  dca:
    base_order_size: 0.001
    max_orders: 1
    order_spacing_pct: 2.5
    multiplier: 1.0
    step_scale: 1.0
    enabled: false
  entry: {indicators: []}
  take_profit: {target_pct: 3.0, enabled: false}
  stop_loss: {type: none, pct: 5.0}
  schedule: {enabled: false, timezone: Europe/Amsterdam, trading_windows: [], blackout_dates: []}
"""
    data = yaml.safe_load(yaml_tp_disabled)["bot"]
    cfg = BotConfig(**data)
    assert cfg.take_profit.enabled is False
    assert cfg.stop_loss.type == "none"
    assert cfg.dca.enabled is False
    assert cfg.dca.max_orders == 1
    assert cfg.schedule.enabled is False
    assert cfg.schedule.trading_windows == []


# ── Entry / exit trigger persistence (audit v17) ─────────────────────────────

def test_entry_trigger_persistence():
    """save_deal() stores entry_trigger as JSON; get_deals() decodes it."""
    trigger = {
        "group_id": 2, "group_name": "Group 2",
        "indicators": ["RSI", "SUPPORT_RESISTANCE"],
    }
    d = _deal("PAPER-TR1", 80000.0)
    d.entry_trigger = trigger
    deal_store.save_deal(d, "tb", "tb", user_id=_UID)

    rows = deal_store.get_deals(user_id=_UID, bot_slug="tb")
    assert len(rows) == 1
    assert rows[0]["entry_trigger"] == trigger


def test_exit_trigger_persistence():
    """close_deal() with exit_trigger writes structured reason; get_deals() decodes it."""
    d = _deal("PAPER-TR2", 80000.0)
    deal_store.save_deal(d, "tb", "tb", user_id=_UID)
    deal_store.close_deal(
        "PAPER-TR2", close_price=82400.0, close_reason="tp",
        pnl_btc=0.00003, pnl_pct=3.0, user_id=_UID,
        exit_trigger={"type": "indicator_tp", "group_name": "TP Group 1"},
    )
    rows = deal_store.get_deals(user_id=_UID, status="closed")
    assert rows[0]["exit_trigger"] == {
        "type": "indicator_tp", "group_name": "TP Group 1",
    }


def test_trigger_roundtrip_replay():
    """replay_deals_in_transaction preserves entry_trigger on batch migration."""
    d1 = _deal("PAPER-TR3", 80000.0)
    d1.entry_trigger = {"group_name": "ASAP", "indicators": ["ASAP"]}
    d2 = _deal("PAPER-TR4", 81000.0, is_open=False)
    d2.entry_trigger = {"group_name": "Group 1", "indicators": ["RSI"]}
    d2.exit_trigger = {"type": "price_tp"}
    d2.close_price = 83430.0
    d2.close_reason = "tp"

    deal_store.replay_deals_in_transaction([d1, d2], "tb", "tb", user_id=_UID)

    rows = {r["id"]: r for r in deal_store.get_deals(user_id=_UID, bot_slug="tb")}
    assert rows["PAPER-TR3"]["entry_trigger"]["group_name"] == "ASAP"
    assert rows["PAPER-TR4"]["entry_trigger"]["indicators"] == ["RSI"]
    assert rows["PAPER-TR4"]["exit_trigger"] == {"type": "price_tp"}


def test_trigger_null_when_missing():
    """A deal without triggers still round-trips — None is valid."""
    d = _deal("PAPER-TR5", 80000.0)
    assert d.entry_trigger is None
    assert d.exit_trigger is None
    deal_store.save_deal(d, "tb", "tb", user_id=_UID)
    rows = deal_store.get_deals(user_id=_UID, bot_slug="tb")
    assert rows[0]["entry_trigger"] is None
    assert rows[0]["exit_trigger"] is None


# ── Indicator groups YAML round-trip (audit v17) ─────────────────────────────

def test_indicator_groups_yaml_roundtrip():
    """BotConfig with indicator_groups survives YAML dump → parse."""
    from config.models import BotConfig
    import yaml

    src = """
bot:
  name: Indi group test
  mode: paper
  exchange: bitget
  pair: BTC/USD
  contract_type: inverse_perpetual
  leverage: {enabled: false, size: 1}
  dca:
    base_order_size: 0.001
    max_orders: 5
    order_spacing_pct: 2.5
    multiplier: 1.0
    step_scale: 1.0
    enabled: true
  entry:
    indicators: []
    indicator_groups:
      - id: 1
        name: Group 1
        indicators:
          - {type: RSI, timeframe: 1h, period: 14, threshold: below_29}
      - id: 2
        name: Group 2
        indicators:
          - {type: RSI, timeframe: 4h, period: 14, threshold: below_35}
  take_profit:
    target_pct: 3.0
    price_enabled: true
    indicator_groups:
      - id: 1
        name: TP Group 1
        indicators:
          - {type: RSI, timeframe: 1h, period: 14, threshold: above_70}
  stop_loss: {type: none, pct: 5.0}
  schedule: {enabled: false, timezone: Europe/Amsterdam, trading_windows: [], blackout_dates: []}
"""
    cfg = BotConfig(**yaml.safe_load(src)["bot"])
    # Entry groups: 2 groups, each with 1 indicator
    assert len(cfg.entry.indicator_groups) == 2
    assert cfg.entry.indicator_groups[0].name == "Group 1"
    assert cfg.entry.indicator_groups[1].indicators[0].timeframe == "4h"
    # TP groups: price still on, plus one indicator group
    assert cfg.take_profit.price_enabled is True
    assert len(cfg.take_profit.indicator_groups) == 1
    assert cfg.take_profit.indicator_groups[0].name == "TP Group 1"
    # Dump → reparse round-trip
    data = cfg.model_dump(by_alias=True)
    cfg2 = BotConfig(**data)
    assert cfg2.entry.indicator_groups[1].indicators[0].threshold == "below_35"
    assert cfg2.take_profit.indicator_groups[0].indicators[0].threshold == "above_70"


# ── Schema migration ─────────────────────────────────────────────────────────

def test_migrate_schema_sets_user_version():
    """init_db() bumps PRAGMA user_version to SCHEMA_VERSION on a
    fresh install (the autouse fixture gives us exactly that)."""
    conn = database.get_db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == database.SCHEMA_VERSION
    assert database.SCHEMA_VERSION >= 3


def test_migrate_schema_idempotent():
    """Calling init_db() twice must not crash and must not change
    user_version — we've already migrated on the first pass."""
    database.init_db()
    database.init_db()
    conn = database.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION


def test_migrate_from_pre_mt_schema_drops_and_recreates(tmp_path):
    """Pre-MT DB (user_version < 3, no user_id column) must be wiped
    clean by init_db so every table lands at v3 with the FK. The
    migration is destructive by design — ALTER TABLE can't add a
    NOT NULL FK on an existing table that has rows."""
    legacy_db = tmp_path / "legacy.db"
    import sqlite3
    raw = sqlite3.connect(str(legacy_db))
    raw.execute(
        """
        CREATE TABLE deals (
            id TEXT PRIMARY KEY,
            bot_slug TEXT NOT NULL,
            bot_name TEXT NOT NULL,
            status TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            initial_price REAL NOT NULL,
            total_size REAL NOT NULL
        )
        """
    )
    raw.execute(
        "INSERT INTO deals (id, bot_slug, bot_name, status, opened_at, "
        "initial_price, total_size) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("LEGACY-1", "tb", "tb", "open", "2024-01-01T00:00:00Z",
         80000.0, 0.001),
    )
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()

    database.set_db_path(legacy_db)
    database.init_db()

    conn = database.get_db()
    # Schema rebuilt: deals.user_id now exists, legacy row is gone,
    # users admin seed is in place.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(deals)").fetchall()}
    assert "user_id" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 0
    assert conn.execute(
        "SELECT username FROM users WHERE id = 1"
    ).fetchone()[0] == "admin"


def test_migrate_refuses_future_schema(tmp_path):
    """If the stored version is NEWER than SCHEMA_VERSION we refuse
    to run — the code may be rolled back but the DB isn't, and
    silently touching a schema we don't understand is worse than
    crashing at startup."""
    future_db = tmp_path / "future.db"
    import sqlite3
    raw = sqlite3.connect(str(future_db))
    raw.execute(f"PRAGMA user_version = {database.SCHEMA_VERSION + 1}")
    raw.commit()
    raw.close()

    database.set_db_path(future_db)
    with pytest.raises(RuntimeError, match="schema is at version"):
        database.init_db()


# ── Users table contract ─────────────────────────────────────────────────────

def test_admin_user_seeded():
    """init_db must always leave users(id=1, username='admin') in place."""
    conn = database.get_db()
    row = conn.execute(
        "SELECT username, active FROM users WHERE id = 1"
    ).fetchone()
    assert row is not None
    assert row["username"] == "admin"
    assert row["active"] == 1


def test_deals_requires_user_id_fk():
    """deals.user_id is NOT NULL — raw INSERT without it must raise."""
    import sqlite3 as _sqlite3
    conn = database.get_db()
    with pytest.raises(_sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deals (id, bot_slug, bot_name, status, opened_at, "
            "initial_price, total_size) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("X", "tb", "tb", "open", "2024-01-01T00:00:00Z", 80000.0, 0.001),
        )


def test_deals_fk_enforced_for_unknown_user():
    """deals.user_id FK → users(id). Unknown user_id must raise (foreign
    key enforcement is on via PRAGMA foreign_keys=ON)."""
    import sqlite3 as _sqlite3
    conn = database.get_db()
    with pytest.raises(_sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deals (id, user_id, bot_slug, bot_name, status, "
            "opened_at, initial_price, total_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("X", 999, "tb", "tb", "open",
             "2024-01-01T00:00:00Z", 80000.0, 0.001),
        )


def test_user_bot_index_present():
    """The (user_id, bot_slug) composite index must exist — that's the
    hot query path for every per-bot read in deal_store."""
    conn = database.get_db()
    idx_names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_deals_user_bot" in idx_names
    assert "idx_backtest_runs_user_bot" in idx_names
    assert "idx_chart_annotations_user_bot" in idx_names


def test_username_unique_constraint():
    """UNIQUE on users.username so username collisions fail fast at
    insert time instead of surfacing as silent "wrong row" bugs later."""
    import sqlite3 as _sqlite3
    conn = database.get_db()
    with pytest.raises(_sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (username) VALUES ('admin')")


def test_deals_user_isolation():
    """Two users each own one deal with the same bot_slug. get_deals
    for user A must not leak user B's row — this is the core multi-
    tenant safety guarantee."""
    conn = database.get_db()
    conn.execute("INSERT INTO users (id, username) VALUES (2, 'bob')")
    conn.commit()

    d_admin = _deal("P-ADM", 80000.0)
    d_bob   = _deal("P-BOB", 80000.0)
    deal_store.save_deal(d_admin, "shared_slug", "bot", user_id=1)
    deal_store.save_deal(d_bob,   "shared_slug", "bot", user_id=2)

    rows_admin = deal_store.get_deals(user_id=1, bot_slug="shared_slug")
    rows_bob   = deal_store.get_deals(user_id=2, bot_slug="shared_slug")
    assert [r["id"] for r in rows_admin] == ["P-ADM"]
    assert [r["id"] for r in rows_bob]   == ["P-BOB"]


# ── DB-path versioning (regression for the 3.13 CI flake) ───────────────────


class TestDbPathVersioning:
    """Regression: ``set_db_path`` must invalidate thread-local
    connection caches across worker threads.

    conftest.py's autouse ``_isolate_reverto_db`` fixture calls
    ``set_db_path(tmp)`` + ``init_db()`` before every test and
    ``close_db()`` on teardown. ``close_db()`` only closes the caller
    thread's conn — TestClient's anyio worker-pool threads live
    longer than a single test and keep a cached conn open against the
    previous test's tmp-DB. Without version-based invalidation, the
    worker's next request reads/writes to a stale SQLite file and
    flakes surface as phantom 401/"user not found" responses.

    The original symptom: Python 3.13 CI had
    ``TestPerUserSessionEpoch.test_fresh_login_after_logout_works``
    fail with 401 deterministically (3.12 + WSL2 got lucky on GC
    timing for the old tmp dirs).
    """

    def test_set_db_path_bumps_version(self, tmp_path):
        before = database._DB_PATH_VERSION
        database.set_db_path(tmp_path / "a.db")
        assert database._DB_PATH_VERSION > before

    def test_get_db_reopens_after_path_change_in_other_thread(
        self, tmp_path,
    ):
        """Worker thread opens a conn to path A. Main thread changes
        path to B via ``set_db_path``. Worker's next ``get_db`` must
        return a conn to B — pre-fix it stayed on A because
        ``close_db`` only closed the main thread's handle.
        """
        import threading as _threading

        database.set_db_path(tmp_path / "a.db")
        database.init_db()

        conns: list[int] = []
        ready = _threading.Event()
        go = _threading.Event()

        def worker() -> None:
            # First call on this thread: open + cache against path A.
            conn1 = database.get_db()
            conns.append(id(conn1))
            ready.set()
            # Wait until main thread bumps the path.
            go.wait(timeout=2.0)
            # Second call: version mismatch → drop stale + reopen.
            conn2 = database.get_db()
            conns.append(id(conn2))

        t = _threading.Thread(target=worker)
        t.start()
        assert ready.wait(timeout=2.0), "worker thread stalled"

        # Bump the path from the main thread.
        database.set_db_path(tmp_path / "b.db")
        database.init_db()
        go.set()
        t.join(timeout=2.0)

        assert len(conns) == 2
        assert conns[0] != conns[1], (
            "worker reused stale connection after path change — "
            "version-based invalidation not working"
        )

    def test_close_db_resets_version_for_caller_thread(self, tmp_path):
        """After ``close_db`` the next ``get_db`` opens a fresh conn
        even if the path didn't change — the cached version is reset
        to -1 so the mismatch branch fires."""
        database.set_db_path(tmp_path / "a.db")
        database.init_db()
        conn1 = database.get_db()
        database.close_db()
        conn2 = database.get_db()
        assert id(conn1) != id(conn2)
