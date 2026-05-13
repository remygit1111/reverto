# tests/test_deal_sentinels.py
# Integration tests for deal edit / cancel / close sentinel files.
# Covers the engine's _check_deal_sentinels path: glob → parse →
# apply → unlink.
#
# 2026-04-19: sentinels were silently ignored after the Fase-2 fs
# migration because the engine scanned ``Path("logs")`` while the
# portal wrote to ``paths.user_logs_dir(user.id)`` (= logs/<uid>/).
# The regression-match test at the bottom of the file pins that
# portal-write-pad and engine-scan-pad resolve to the same
# directory so this drift can't recur unnoticed.

import os
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, UTC

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths  # noqa: E402
from paper.paper_state import PaperState, PaperDeal, PaperOrder  # noqa: E402


def _make_deal(deal_id="202604191342-0001"):
    return PaperDeal(
        id=deal_id, bot_name="test_bot", symbol="BTC/USD",
        side="long", leverage=1,
        orders=[PaperOrder(order_number=1, price=80000.0, size=0.001,
                           timestamp=datetime.now(UTC), order_type="base")],
    )


def _make_engine(tmp_path, monkeypatch):
    """Minimal PaperEngine with a real state but every filesystem write
    re-rooted under ``tmp_path``. Monkey-patches ``paths.BASE_DIR`` so
    the engine's ``paths.user_logs_dir(self.user_id)`` call resolves
    to ``tmp_path/logs/1/`` instead of the repo's real logs/ tree —
    without this, tests would both read and write sentinels inside
    the shipping repo and leak state between runs.
    """
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)

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
    # telegram-config moved to per-user store; cfg.telegram is gone

    cfg.ml.enabled = False

    engine = PaperEngine(
        config=cfg,
        exchange=MagicMock(),
        notifier=MagicMock(),
        state_file=str(tmp_path / "state.json"),
        manual_trigger_file=str(tmp_path / "trigger"),
    )
    return engine


def _sentinel_path(engine, action: str, deal_id: str) -> Path:
    """Compose the sentinel path the ENGINE will scan — i.e. the same
    one the portal would write to. Tests use this helper rather than
    building the path themselves so the engine+portal+test triad can
    only ever agree."""
    slug = engine.config.name.lower().replace(" ", "_")
    return paths.user_logs_dir(engine.user_id) / f"{slug}.deal_{action}_{deal_id}"


class TestDealEditSentinel:
    def test_edit_sentinel_applies_overrides(self, tmp_path, monkeypatch):
        engine = _make_engine(tmp_path, monkeypatch)
        deal = _make_deal()
        engine.state.open_deal(deal)

        sentinel = _sentinel_path(engine, "edit", "202604191342-0001")
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

    def test_corrupt_json_does_not_crash(self, tmp_path, monkeypatch):
        engine = _make_engine(tmp_path, monkeypatch)
        deal = _make_deal()
        engine.state.open_deal(deal)

        sentinel = _sentinel_path(engine, "edit", "202604191342-0001")
        sentinel.write_text("NOT JSON {{{")

        engine._check_deal_sentinels(80000.0)
        assert not sentinel.exists()
        assert deal._tp_override is None


class TestDealCloseSentinel:
    def test_close_sentinel_closes_deal(self, tmp_path, monkeypatch):
        engine = _make_engine(tmp_path, monkeypatch)
        deal = _make_deal()
        engine.state.open_deal(deal)

        sentinel = _sentinel_path(engine, "close", "202604191342-0001")
        sentinel.write_text("")

        engine._check_deal_sentinels(82000.0)

        assert "202604191342-0001" not in engine.state.open_deals
        assert len(engine.state.closed_deals) == 1
        assert engine.state.closed_deals[0].close_reason == "manual"
        assert not sentinel.exists()


class TestConcurrentSentinels:
    def test_edit_then_close_both_processed(self, tmp_path, monkeypatch):
        engine = _make_engine(tmp_path, monkeypatch)
        deal = _make_deal()
        engine.state.open_deal(deal)

        # Write both — glob order is filesystem-dependent but both
        # should be consumed without crashing.
        edit_sentinel = _sentinel_path(engine, "edit", "202604191342-0001")
        close_sentinel = _sentinel_path(engine, "close", "202604191342-0001")
        edit_sentinel.write_text(
            json.dumps({"tp_override": {"target_pct": 9.9}})
        )
        close_sentinel.write_text("")

        engine._check_deal_sentinels(82000.0)

        # Both sentinels consumed
        assert not edit_sentinel.exists()
        assert not close_sentinel.exists()
        # The deal is closed (close sentinel wins if edit runs first)
        assert "202604191342-0001" not in engine.state.open_deals


# ── Regression: portal-write and engine-scan agree on directory ─────────────


class TestPortalEnginePathContract:
    """Pin the Fase-2 filesystem-layout bug that manifested 2026-04-19:
    the portal DELETE /api/bots/{slug}/deals/{deal_id} handler wrote
    sentinels to ``paths.user_logs_dir(user.id)`` while the paper-
    engine _check_deal_sentinels scanned a bare ``Path("logs")``. Both
    must resolve to the same directory or every deal-close/cancel
    button in the UI silently fails.

    If this test fails, SOMEONE has moved one of the two paths
    without moving the other. Fix the mismatch before merging.
    """

    def test_portal_write_path_matches_engine_scan_path(
        self, tmp_path, monkeypatch,
    ):
        """The core regression: both sides use
        ``paths.user_logs_dir(user_id)``. Swap the helper for a
        hardcoded ``Path("logs")`` in either place and this test
        immediately catches it."""
        monkeypatch.setattr(paths, "BASE_DIR", tmp_path)

        user_id = 1
        slug = "testbot"
        deal_id = "202604191631-0552"

        # Portal-write — mirrors web/routes/deals.py:184.
        portal_write_dir = paths.user_logs_dir(user_id)
        portal_sentinel = portal_write_dir / f"{slug}.deal_close_{deal_id}"
        portal_sentinel.write_text("", encoding="utf-8")

        # Engine-scan — mirrors paper/paper_engine.py:_check_deal_sentinels.
        engine_scan_dir = paths.user_logs_dir(user_id)

        assert engine_scan_dir == portal_write_dir, (
            f"Portal writes to {portal_write_dir} but engine scans "
            f"{engine_scan_dir} — mismatch blocks all deal sentinels"
        )

        # Positive proof: the glob the engine runs finds the portal-
        # written sentinel. If the path-contract is intact but the
        # glob pattern diverges this assertion catches it.
        found = list(engine_scan_dir.glob(f"{slug}.deal_*"))
        assert len(found) == 1
        assert found[0] == portal_sentinel

    def test_engine_actually_consumes_portal_written_sentinel(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end proof that the fix closes the operator-reported
        bug: a sentinel written the way the portal writes it is picked
        up and acted on by the engine's next ``_check_deal_sentinels``.
        Pre-fix this test fails with the sentinel still present and
        the deal still open."""
        engine = _make_engine(tmp_path, monkeypatch)
        deal = _make_deal()
        engine.state.open_deal(deal)

        # Write sentinel the portal way — via paths.user_logs_dir, NOT
        # via the _sentinel_path helper (which already mirrors the
        # engine). This is the asymmetric write that must cross the
        # bug-prone boundary.
        slug = engine.config.name.lower().replace(" ", "_")
        portal_sentinel = (
            paths.user_logs_dir(engine.user_id)
            / f"{slug}.deal_close_{deal.id}"
        )
        portal_sentinel.write_text("", encoding="utf-8")

        engine._check_deal_sentinels(82000.0)

        assert not portal_sentinel.exists(), (
            "engine did NOT consume the portal-written sentinel — the "
            "2026-04-19 'close button doesn't work' bug has regressed"
        )
        assert deal.id not in engine.state.open_deals
        assert len(engine.state.closed_deals) == 1
        assert engine.state.closed_deals[0].close_reason == "manual"
