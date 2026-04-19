# tests/test_paper_state_persistence.py
# Cover the resume-from-state-file logic in PaperEngine._load_state.

import os
import sys
import json
from datetime import datetime, UTC
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paper.paper_engine import PaperEngine, _deal_to_dict
from paper.paper_state import PaperDeal, PaperOrder


def _order(price, size=0.001, t="base", n=1):
    return PaperOrder(order_number=n, price=price, size=size,
                      timestamp=datetime.now(UTC), order_type=t)


def _deal(deal_id="PAPER-0001", price=80000.0, size=0.001, lev=1):
    return PaperDeal(id=deal_id, bot_name="tb", symbol="BTC/USD",
                     side="long", leverage=lev, orders=[_order(price, size)])


def _notifier():
    n = MagicMock()
    for m in ["notify_startup", "notify_shutdown", "notify_entry", "notify_dca",
              "notify_take_profit", "notify_stop_loss", "notify_error",
              "notify_schedule_open", "notify_schedule_close"]:
        setattr(n, m, MagicMock())
    return n


def _cfg():
    cfg = MagicMock()
    cfg.name = "tb"
    cfg.pair = "BTC/USD"
    cfg.mode.value = "paper"
    cfg.exchange.value = "bitget"
    cfg.leverage.enabled = False
    cfg.leverage.size = 1
    cfg.leverage.liquidation_guard.warn_pct = 15.0
    cfg.leverage.liquidation_guard.emergency_close_pct = 5.0
    cfg.take_profit.target_pct = 3.0
    cfg.take_profit.indicator_confirm = None
    cfg.take_profit.minimum_tp_pct = None
    cfg.stop_loss.type = "fixed"
    cfg.stop_loss.pct = 5.0
    cfg.dca.max_orders = 5
    cfg.dca.order_spacing_pct = 2.5
    cfg.dca.multiplier = 1.0
    cfg.dca.base_order_size = 0.001
    cfg.dca.taker_fee = 0.0006; cfg.dca.step_scale = 1.0
    cfg.entry.indicators = []
    cfg.schedule.trading_windows = []
    cfg.schedule.blackout_dates = []
    cfg.schedule.timezone = "Europe/Amsterdam"
    cfg.telegram.notify_on = []
    cfg.ml.enabled = False
    return cfg


def _make_engine(state_file, balance=0.1):
    return PaperEngine(
        config=_cfg(),
        exchange=MagicMock(),
        notifier=_notifier(),
        initial_balance_btc=balance,
        state_file=str(state_file),
    )


class TestNoStateFile:
    def test_clean_start_when_state_missing(self, tmp_path):
        sf = tmp_path / "missing.state.json"
        e = _make_engine(sf, balance=0.2)
        assert e.state.balance_btc == pytest.approx(0.2)
        assert len(e.state.open_deals) == 0
        assert len(e.state.closed_deals) == 0


class TestClosedDealsSurviveRestart:
    def test_closed_deals_restored(self, tmp_path):
        sf = tmp_path / "bot.state.json"

        # Run 1: open and close a winning deal.
        e1 = _make_engine(sf, balance=0.1)
        d = _deal("PAPER-0001", 80000.0)
        e1.state.open_deal(d)
        e1.state.close_deal(d.id, 80000.0 * 1.03, "tp")
        e1._write_state(80000.0 * 1.03, is_open=True)

        original_balance = e1.state.balance_btc
        original_pnl_btc = e1.state.closed_deals[0].pnl_btc

        # Run 2: a fresh engine pointed at the same state file.
        e2 = _make_engine(sf)
        assert len(e2.state.closed_deals) == 1
        restored = e2.state.closed_deals[0]
        assert restored.id == "PAPER-0001"
        assert restored.close_reason == "tp"
        assert restored.pnl_btc == pytest.approx(original_pnl_btc, rel=1e-6)
        assert e2.state.balance_btc == pytest.approx(original_balance, rel=1e-9)

    def test_new_ids_are_globally_unique_after_restore(self, tmp_path):
        """Post-collision-fix (2026-04-19): the old per-instance
        counter that needed to be re-synced past restored IDs is
        gone. Each ``new_deal_id()`` call mints a fresh globally-
        unique YYYYMMDDHHMM-RRRR id. This test pins the invariant
        that a fresh engine after restore still produces ids distinct
        from the restored ones — without any counter-sync logic."""
        from core.ids import DEAL_ID_RE

        sf = tmp_path / "bot.state.json"
        e1 = _make_engine(sf)
        for i in (1, 2, 7):
            d = _deal(f"PAPER-{i:04d}", 80000.0)
            e1.state.open_deal(d)
            e1.state.close_deal(d.id, 80000.0 * 1.01, "tp")
        e1._write_state(80000.0, is_open=True)

        e2 = _make_engine(sf)
        next_id = e2.state.new_deal_id()
        # New id has the new format...
        assert DEAL_ID_RE.match(next_id), (
            f"Expected YYYYMMDDHHMM-RRRR, got {next_id!r}"
        )
        # ...and doesn't collide with the restored legacy-format ids.
        assert next_id not in {"PAPER-0001", "PAPER-0002", "PAPER-0007"}


class TestOpenDealSurvivesRestart:
    def test_open_deal_restored_with_orders(self, tmp_path):
        sf = tmp_path / "bot.state.json"
        e1 = _make_engine(sf)
        d = _deal("PAPER-0001", 80000.0)
        # Add a DCA order so we verify the order list round-trips.
        d.orders.append(_order(78000.0, 0.001, "dca", 2))
        e1.state.open_deal(d)
        e1._write_state(78000.0, is_open=True)

        e2 = _make_engine(sf)
        assert "PAPER-0001" in e2.state.open_deals
        restored = e2.state.open_deals["PAPER-0001"]
        assert restored.is_open is True
        assert len(restored.orders) == 2
        assert restored.orders[0].order_type == "base"
        assert restored.orders[1].order_type == "dca"
        assert restored.orders[1].price == pytest.approx(78000.0)
        assert restored.dca_count == 1

    def test_open_deal_peak_price_restored(self, tmp_path):
        sf = tmp_path / "bot.state.json"
        e1 = _make_engine(sf)
        d = _deal("PAPER-0001", 80000.0)
        d._peak_price = 82500.0  # trailing stop watermark
        e1.state.open_deal(d)
        e1._write_state(82000.0, is_open=True)

        e2 = _make_engine(sf)
        restored = e2.state.open_deals["PAPER-0001"]
        assert restored._peak_price == pytest.approx(82500.0)


class TestBalanceAndFeesRestored:
    def test_balance_and_fees_round_trip(self, tmp_path):
        sf = tmp_path / "bot.state.json"
        e1 = _make_engine(sf, balance=0.1)
        e1.state.balance_btc = 0.12345678
        e1._fees_paid_btc = 0.00009876
        e1._write_state(80000.0, is_open=True)

        e2 = _make_engine(sf)
        assert e2.state.balance_btc == pytest.approx(0.12345678, abs=1e-9)
        assert e2._fees_paid_btc == pytest.approx(0.00009876, abs=1e-11)


class TestCorruptStateFile:
    def test_unparseable_state_starts_clean(self, tmp_path):
        sf = tmp_path / "bot.state.json"
        sf.write_text("{not valid json", encoding="utf-8")
        e = _make_engine(sf, balance=0.1)
        # Falls back to a clean PaperState with the configured balance.
        assert e.state.balance_btc == pytest.approx(0.1)
        assert len(e.state.open_deals) == 0
        assert len(e.state.closed_deals) == 0


class TestDbLedgerIntegration:
    """Integration test for the SQLite ledger wiring.

    Drives a PaperEngine through _open_deal, _check_dca, _check_tp and
    then asserts the DB shows the deal closed with matching pnl.
    """

    def test_open_dca_close_writes_to_db(self, tmp_path):
        from core import deal_store

        sf = tmp_path / "integration.state.json"
        engine = PaperEngine(
            config=_cfg(),
            exchange=MagicMock(),
            notifier=_notifier(),
            initial_balance_btc=0.1,
            state_file=str(sf),
            slug="integration_bot",
        )

        # Open a deal at 80k, then trigger a DCA at 78k, then TP at 82400.
        engine._open_deal(80000.0)
        deal = next(iter(engine.state.get_open_deals_snapshot().values()))
        # _check_dca: spacing 2.5% → next dca price ≈ 78000
        engine._check_dca(deal, 78000.0)
        assert deal.dca_count == 1

        # Snapshot realised pnl before the close_deal call nukes the object.
        expected_pnl, _ = deal.calculate_pnl(82400.0)

        engine._check_tp(deal, 82400.0)
        assert deal.id not in engine.state.open_deals

        rows = deal_store.get_deals(
            user_id=1, bot_slug="integration_bot", status="closed",
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == deal.id
        assert row["close_reason"] == "tp"
        assert row["pnl_btc"] == pytest.approx(expected_pnl, rel=1e-9)

        orders = deal_store.get_deal_orders(deal.id, user_id=1)
        assert len(orders) == 2
        assert orders[0]["order_type"] == "base"
        assert orders[1]["order_type"] == "dca"


class TestStateFileShapeIsFrontendCompatible:
    def test_orders_added_but_dashboard_fields_present(self, tmp_path):
        sf = tmp_path / "bot.state.json"
        e = _make_engine(sf)
        d = _deal("PAPER-0001", 80000.0)
        e.state.open_deal(d)
        e._write_state(80500.0, is_open=True)

        data = json.loads(sf.read_text(encoding="utf-8"))
        assert data["open_deals"][0]["order_count"] == 1
        assert "orders" in data["open_deals"][0]
        assert data["open_deals"][0]["orders"][0]["order_type"] == "base"
