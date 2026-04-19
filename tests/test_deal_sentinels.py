# tests/test_deal_sentinels.py
# Integration tests for deal edit / cancel / close sentinel files.
# Covers the engine's _check_deal_sentinels path: glob → parse → apply → unlink.

import os
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, UTC

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paper.paper_state import PaperState, PaperDeal, PaperOrder


def _make_deal(deal_id="202604191342-0001"):
    return PaperDeal(
        id=deal_id, bot_name="test_bot", symbol="BTC/USD",
        side="long", leverage=1,
        orders=[PaperOrder(order_number=1, price=80000.0, size=0.001,
                           timestamp=datetime.now(UTC), order_type="base")],
    )


def _make_engine(tmp_path):
    """Minimal PaperEngine mock with real state and sentinel checking."""
    from paper.paper_engine import PaperEngine
    cfg = MagicMock()
    cfg.name = "test bot"
    cfg.pair = "BTC/USD"
    cfg.mode.value = "paper"
    cfg.exchange.value = "bitget"
    cfg.leverage.enabled = False
    cfg.leverage.size = 1
    cfg.leverage.liquidation_guard.warn_pct = 15.0
    cfg.leverage.liquidation_guard.emergency_close_pct = 5.0
    cfg.take_profit.target_pct = 3.0
    cfg.take_profit.enabled = True
    cfg.take_profit.indicator_confirm = None
    cfg.take_profit.minimum_tp_pct = None
    cfg.stop_loss.type = "fixed"
    cfg.stop_loss.pct = 5.0
    cfg.dca.max_orders = 5
    cfg.dca.order_spacing_pct = 2.5
    cfg.dca.multiplier = 1.0
    cfg.dca.base_order_size = 0.001
    cfg.dca.taker_fee = 0.0006
    cfg.dca.step_scale = 1.0
    cfg.dca.enabled = True
    cfg.entry.indicators = []
    cfg.schedule.trading_windows = []
    cfg.schedule.blackout_dates = []
    cfg.schedule.timezone = "Europe/Amsterdam"
    cfg.telegram.notify_on = []
    cfg.ml.enabled = False

    engine = PaperEngine(
        config=cfg,
        exchange=MagicMock(),
        notifier=MagicMock(),
        state_file=str(tmp_path / "state.json"),
        manual_trigger_file=str(tmp_path / "trigger"),
    )
    return engine


class TestDealEditSentinel:
    def test_edit_sentinel_applies_overrides(self, tmp_path):
        engine = _make_engine(tmp_path)
        deal = _make_deal()
        engine.state.open_deal(deal)

        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        sentinel = log_dir / "test_bot.deal_edit_202604191342-0001"
        sentinel.write_text(json.dumps({
            "tp_override": {"enabled": True, "target_pct": 5.5},
            "sl_override": {"enabled": True, "type": "trailing", "pct": 3.0},
            "dca_enabled": False,
        }))

        engine._check_deal_sentinels(80000.0)

        d = engine.state.open_deals["202604191342-0001"]
        assert d._tp_override == {"enabled": True, "target_pct": 5.5}
        assert d._sl_override == {"enabled": True, "type": "trailing", "pct": 3.0}
        assert d._dca_enabled is False
        assert not sentinel.exists()

    def test_corrupt_json_does_not_crash(self, tmp_path):
        engine = _make_engine(tmp_path)
        deal = _make_deal()
        engine.state.open_deal(deal)

        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        sentinel = log_dir / "test_bot.deal_edit_202604191342-0001"
        sentinel.write_text("NOT JSON {{{")

        engine._check_deal_sentinels(80000.0)
        assert not sentinel.exists()
        assert deal._tp_override is None


class TestDealCloseSentinel:
    def test_close_sentinel_closes_deal(self, tmp_path):
        engine = _make_engine(tmp_path)
        deal = _make_deal()
        engine.state.open_deal(deal)

        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        sentinel = log_dir / "test_bot.deal_close_202604191342-0001"
        sentinel.write_text("")

        engine._check_deal_sentinels(82000.0)

        assert "202604191342-0001" not in engine.state.open_deals
        assert len(engine.state.closed_deals) == 1
        assert engine.state.closed_deals[0].close_reason == "manual"
        assert not sentinel.exists()


class TestConcurrentSentinels:
    def test_edit_then_close_both_processed(self, tmp_path):
        engine = _make_engine(tmp_path)
        deal = _make_deal()
        engine.state.open_deal(deal)

        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        # Write both — glob order is filesystem-dependent but both
        # should be consumed without crashing.
        (log_dir / "test_bot.deal_edit_202604191342-0001").write_text(
            json.dumps({"tp_override": {"target_pct": 9.9}})
        )
        (log_dir / "test_bot.deal_close_202604191342-0001").write_text("")

        engine._check_deal_sentinels(82000.0)

        # Both sentinels consumed
        assert not (log_dir / "test_bot.deal_edit_202604191342-0001").exists()
        assert not (log_dir / "test_bot.deal_close_202604191342-0001").exists()
        # The deal is closed (close sentinel wins if edit runs first)
        assert "202604191342-0001" not in engine.state.open_deals
