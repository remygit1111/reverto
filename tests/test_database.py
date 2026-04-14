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
    for t in ("deals", "orders", "indicator_snapshots", "chart_annotations"):
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
