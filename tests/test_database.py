# tests/test_database.py
# Covers the SQLite persistence layer: schema init, deal/order round-trips,
# filters, annotations CRUD, and the compute_stats aggregation.
#
# Each test gets a fresh DB in tmp_path via the autouse db_path fixture —
# the real logs/reverto.db is never touched.

import os
import sys
from datetime import datetime, UTC

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import database, deal_store
from paper.paper_state import PaperDeal, PaperOrder


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
    for t in ("deals", "orders", "chart_annotations", "backtest_runs"):
        assert t in names


def test_save_and_get_deal_roundtrip():
    d = _deal("PAPER-0001", 80000.0)
    deal_store.save_deal(d, bot_slug="tb", bot_name="tb")
    rows = deal_store.get_deals(bot_slug="tb")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "PAPER-0001"
    assert row["status"] == "open"
    assert row["initial_price"] == pytest.approx(80000.0)
    assert row["total_size"] == pytest.approx(0.001)
    assert row["bot_name"] == "tb"


def test_close_deal_updates_status():
    d = _deal("PAPER-0002", 80000.0)
    deal_store.save_deal(d, "tb", "tb")
    deal_store.close_deal(
        "PAPER-0002", close_price=82400.0, close_reason="tp",
        pnl_btc=0.00003, pnl_pct=3.0,
    )
    rows = deal_store.get_deals(status="closed")
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "closed"
    assert r["close_reason"] == "tp"
    assert r["close_price"] == pytest.approx(82400.0)
    assert r["pnl_btc"] == pytest.approx(0.00003)
    assert r["closed_at"] is not None


def test_save_and_get_orders():
    d = _deal("PAPER-0003", 80000.0)
    deal_store.save_deal(d, "tb", "tb")
    o1 = _order(1, 80000.0, 0.001, "base")
    o2 = _order(2, 78000.0, 0.002, "dca")
    deal_store.save_order(o1, "PAPER-0003", "tb", fee_btc=0.0000006)
    deal_store.save_order(o2, "PAPER-0003", "tb", fee_btc=0.0000012)

    orders = deal_store.get_deal_orders("PAPER-0003")
    assert [o["order_number"] for o in orders] == [1, 2]
    assert orders[0]["order_type"] == "base"
    assert orders[1]["order_type"] == "dca"
    assert orders[1]["price"] == pytest.approx(78000.0)


def test_get_deals_filters():
    deal_store.save_deal(_deal("PAPER-0001"), "bot_a", "A")
    deal_store.save_deal(_deal("PAPER-0002"), "bot_b", "B")
    d3 = _deal("PAPER-0003", is_open=False)
    d3.close_reason = "tp"
    d3.closed_at = datetime.now(UTC)
    deal_store.save_deal(d3, "bot_a", "A")

    assert len(deal_store.get_deals(bot_slug="bot_a")) == 2
    assert len(deal_store.get_deals(bot_slug="bot_b")) == 1
    assert len(deal_store.get_deals(status="open")) == 2
    assert len(deal_store.get_deals(status="closed")) == 1


def test_annotation_crud():
    new_id = deal_store.save_annotation(
        bot_slug="tb", type_="line", timeframe="1h",
        x1=1_700_000_000, y1=80000.0, label="entry",
    )
    assert new_id > 0
    items = deal_store.list_annotations("tb")
    assert len(items) == 1
    assert items[0]["label"] == "entry"
    assert items[0]["timeframe"] == "1h"

    # filter by timeframe
    assert len(deal_store.list_annotations("tb", timeframe="1h")) == 1
    assert len(deal_store.list_annotations("tb", timeframe="4h")) == 0

    assert deal_store.delete_annotation(new_id) is True
    assert deal_store.list_annotations("tb") == []
    # second delete is a no-op
    assert deal_store.delete_annotation(new_id) is False


def test_compute_stats_basic():
    # No deals → zeros + note
    empty = deal_store.compute_stats()
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
        deal_store.save_deal(d, "tb", "tb")
        # Attach an order with a fee so total_fees_btc is non-zero.
        deal_store.save_order(
            _order(1, 80000.0), f"PAPER-{i:04d}", "tb", fee_btc=0.0000006,
        )

    stats = deal_store.compute_stats("tb")
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
    row_id = deal_store.save_backtest_run("btc", "BTC bot", params, _sample_summary())
    assert row_id > 0

    # Second run so we can assert ordering
    deal_store.save_backtest_run("btc", "BTC bot", params, _sample_summary())
    deal_store.save_backtest_run("eth", "ETH bot", params, _sample_summary())

    btc_runs = deal_store.get_backtest_runs("btc")
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

    all_runs = deal_store.get_all_backtest_runs()
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
    deal_store.save_deal(d, "tb", "tb")

    rows = deal_store.get_deals(bot_slug="tb")
    assert len(rows) == 1
    assert rows[0]["entry_trigger"] == trigger


def test_exit_trigger_persistence():
    """close_deal() with exit_trigger writes structured reason; get_deals() decodes it."""
    d = _deal("PAPER-TR2", 80000.0)
    deal_store.save_deal(d, "tb", "tb")
    deal_store.close_deal(
        "PAPER-TR2", close_price=82400.0, close_reason="tp",
        pnl_btc=0.00003, pnl_pct=3.0,
        exit_trigger={"type": "indicator_tp", "group_name": "TP Group 1"},
    )
    rows = deal_store.get_deals(status="closed")
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

    deal_store.replay_deals_in_transaction([d1, d2], "tb", "tb")

    rows = {r["id"]: r for r in deal_store.get_deals(bot_slug="tb")}
    assert rows["PAPER-TR3"]["entry_trigger"]["group_name"] == "ASAP"
    assert rows["PAPER-TR4"]["entry_trigger"]["indicators"] == ["RSI"]
    assert rows["PAPER-TR4"]["exit_trigger"] == {"type": "price_tp"}


def test_trigger_null_when_missing():
    """A deal without triggers still round-trips — None is valid."""
    d = _deal("PAPER-TR5", 80000.0)
    assert d.entry_trigger is None
    assert d.exit_trigger is None
    deal_store.save_deal(d, "tb", "tb")
    rows = deal_store.get_deals(bot_slug="tb")
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


# ── Schema migration (audit v18 MED) ─────────────────────────────────────────

def test_migrate_schema_sets_user_version():
    """init_db() bumps PRAGMA user_version to SCHEMA_VERSION on a
    fresh install (the autouse fixture gives us exactly that)."""
    conn = database.get_db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == database.SCHEMA_VERSION
    assert database.SCHEMA_VERSION >= 2


def test_migrate_schema_idempotent():
    """Calling init_db() twice must not crash and must not change
    user_version — we've already migrated on the first pass."""
    database.init_db()
    database.init_db()
    conn = database.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION


def test_migrate_schema_adds_trigger_columns_on_legacy_db(tmp_path):
    """Simulate a pre-v17 DB (no entry_trigger/exit_trigger columns,
    user_version=0) and verify _migrate_schema adds both columns and
    bumps the version pointer."""
    legacy_db = tmp_path / "legacy.db"

    # Rebuild a minimal "old" deals table without the trigger columns,
    # write user_version=0 explicitly to pretend we predate v17.
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
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()

    # Hand the path to the module + re-init.
    database.set_db_path(legacy_db)
    database.init_db()

    conn = database.get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(deals)").fetchall()}
    assert "entry_trigger" in cols
    assert "exit_trigger" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION


def test_migrate_schema_tolerates_existing_columns(tmp_path):
    """Legacy DB that already has the trigger columns but user_version=0
    (e.g. someone ran an older migration manually) must not crash on
    ALTER — the try/except swallows OperationalError, version bumps."""
    legacy_db = tmp_path / "partial.db"
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
            total_size REAL NOT NULL,
            entry_trigger TEXT,
            exit_trigger TEXT
        )
        """
    )
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()

    database.set_db_path(legacy_db)
    database.init_db()  # must not raise
    conn = database.get_db()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
