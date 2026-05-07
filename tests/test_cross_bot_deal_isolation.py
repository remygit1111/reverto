"""Regression guard for the cross-bot deal-id collision bug (2026-04-19).

The previous version of PaperState.new_deal_id() used a per-instance
counter "PAPER-NNNN" — every bot started at PAPER-0001, and as soon
as two bots ran at the same time their ids collided. That was not
visible because core/deal_store.save_deal used INSERT OR REPLACE: the
second writer silently overwrote the first one's row. Result: one
deal disappeared from the DB while it still sat in state.json, the
ML pipeline trained on a corrupted dataset, parity-compare reported
misleading counts.

These tests pin the three invariants the fix guarantees:
  1. Two bots that mint an id at exactly the same instant get
     different ids (10_000-slot random suffix).
  2. A forced DB collision raises IntegrityError — no silent
     clobber any more.
  3. The realistic scenario from the production bug: two bots open
     a deal within ms of each other; both rows stay in the DB.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import database, deal_store
from core.ids import DEAL_ID_RE, generate_deal_id
from paper.paper_engine import PaperEngine
from paper.paper_state import PaperDeal, PaperOrder


_FIXED_MINUTE = datetime(2026, 4, 19, 13, 42, 0, tzinfo=timezone.utc)


def _order(price=80000.0, size=0.001, n=1):
    return PaperOrder(
        order_number=n, price=price, size=size,
        timestamp=_FIXED_MINUTE, order_type="base",
    )


def _deal(deal_id: str) -> PaperDeal:
    return PaperDeal(
        id=deal_id, bot_name="tb", symbol="BTC/USD",
        side="long", leverage=1, orders=[_order()],
    )


@pytest.fixture
def fresh_db(tmp_path):
    """Per-test SQLite so collision behaviour is observable without
    cross-test leakage from the autouse conftest fixture."""
    database.set_db_path(tmp_path / "cross_bot.db")
    database.init_db()
    yield tmp_path
    database.close_db()


# ── 1. Generator-level uniqueness ───────────────────────────────────────────

class TestSameMinuteUniqueness:

    def test_two_bots_with_same_timestamp_get_different_ids(self):
        """Both bots mint IDs at exactly the same UTC-minute. 200 ids
        from that minute must stay nearly unique (10_000-slot random
        keyspace; birthday collision expectation ≈ 2 on 200 draws)."""
        bot_a_ids = {generate_deal_id(now_utc=_FIXED_MINUTE) for _ in range(100)}
        bot_b_ids = {generate_deal_id(now_utc=_FIXED_MINUTE) for _ in range(100)}

        for i in bot_a_ids | bot_b_ids:
            assert DEAL_ID_RE.match(i), f"Bad format: {i!r}"

        overlap = bot_a_ids & bot_b_ids
        # Same minute, random suffix → overlap probability ≈ 1%.
        # Assert ≤ 5 overlapping IDs — way above expected, far below
        # the "generator is broken" threshold.
        assert len(overlap) <= 5, (
            f"Too many overlapping IDs across bots: {overlap}"
        )


# ── 2. DB enforces uniqueness (INSERT raises, not silent REPLACE) ──────────

class TestDbEnforcesUniqueness:

    def test_insert_collision_raises_integrity_error(self, fresh_db):
        """Force two create_deal calls with the same id. Second must
        raise sqlite3.IntegrityError — the old INSERT OR REPLACE would
        have silently clobbered the first."""
        deal_a = _deal("202604191342-9999")
        deal_b = _deal("202604191342-9999")

        deal_store.create_deal(deal_a, "bot_a", "BOT A", user_id=1)

        with pytest.raises(sqlite3.IntegrityError):
            deal_store.create_deal(deal_b, "bot_b", "BOT B", user_id=1)

        # The original row is intact — the first bot's data is unchanged.
        rows = deal_store.get_deals(user_id=1)
        assert len(rows) == 1
        assert rows[0]["bot_slug"] == "bot_a"
        assert rows[0]["bot_name"] == "BOT A"

    def test_save_deal_upsert_updates_own_row(self, fresh_db):
        """save_deal(same-owner resave) must UPDATE, not raise — this
        is the DCA-update flow and breaking it would break every
        DCA-adding tick."""
        deal = _deal("202604191342-0001")
        deal_store.save_deal(deal, "bot_a", "BOT A", user_id=1)

        # Add a DCA-like order so total_size changes.
        deal.orders.append(PaperOrder(
            order_number=2, price=78000.0, size=0.002,
            timestamp=_FIXED_MINUTE, order_type="dca",
        ))
        deal_store.save_deal(deal, "bot_a", "BOT A", user_id=1)

        rows = deal_store.get_deals(user_id=1)
        assert len(rows) == 1
        assert rows[0]["total_size"] == pytest.approx(0.003)

    def test_save_deal_cross_owner_collision_raises(self, fresh_db):
        """A save_deal call for a row that belongs to a different
        bot_slug must NOT silently update someone else's row. The
        UPDATE finds 0 rows (bot_slug filter), the fallback INSERT
        hits the PRIMARY KEY constraint, IntegrityError bubbles up."""
        deal_store.save_deal(_deal("202604191342-5555"), "bot_a", "A", user_id=1)

        with pytest.raises(sqlite3.IntegrityError):
            deal_store.save_deal(
                _deal("202604191342-5555"), "bot_b", "B", user_id=1,
            )

        # bot_a's row untouched.
        rows = deal_store.get_deals(user_id=1)
        assert len(rows) == 1
        assert rows[0]["bot_slug"] == "bot_a"


# ── 3. The bug scenario: two paper bots open "simultaneously" ──────────────

class TestSimulatedTwoBotsConcurrentOpen:
    """Re-creates the exact parity-test scenario (rsi_paper_test +
    rsi_real_test both opened a deal within a couple of minutes and
    one row vanished). With the fix, both rows must land in the DB
    and both bots see their own deal.
    """

    def _make_engine(self, slug: str, tmp_path: Path) -> PaperEngine:
        cfg = MagicMock()
        cfg.name = slug
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
        cfg.dca.max_orders = 1  # base order only — keeps the fixture lean
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
        cfg.direction = "long"

        notifier = MagicMock()
        for m in (
            "notify_startup", "notify_shutdown", "notify_entry", "notify_dca",
            "notify_take_profit", "notify_stop_loss", "notify_error",
            "notify_schedule_open", "notify_schedule_close",
        ):
            setattr(notifier, m, MagicMock())

        return PaperEngine(
            config=cfg,
            exchange=MagicMock(),
            notifier=notifier,
            state_file=str(tmp_path / f"{slug}.state.json"),
            slug=slug,
        )

    def test_two_bots_open_deals_both_persist(self, fresh_db):
        """Simulate rsi_paper_test + rsi_real_test both opening a
        deal in the same minute. With the pre-fix code this is where
        the silent clobber happened. With the fix both rows persist
        — this is the core regression guard.
        """
        engine_a = self._make_engine("rsi_paper_test", fresh_db)
        engine_b = self._make_engine("rsi_real_test", fresh_db)
        try:
            # Pin the wall clock so both bots mint their id in the
            # same UTC minute — maximising collision odds to exercise
            # the retry path.
            with patch("core.ids.datetime") as mock_dt:
                mock_dt.now.return_value = _FIXED_MINUTE
                engine_a._open_deal(80000.0)
                engine_b._open_deal(80100.0)

            # Both engines have an open deal in memory.
            assert len(engine_a.state.open_deals) == 1
            assert len(engine_b.state.open_deals) == 1

            # Both rows in the DB under the right slug.
            rows_a = deal_store.get_deals(user_id=1, bot_slug="rsi_paper_test")
            rows_b = deal_store.get_deals(user_id=1, bot_slug="rsi_real_test")
            assert len(rows_a) == 1, (
                "paper bot's deal was silently clobbered — cross-bot "
                "collision regressed"
            )
            assert len(rows_b) == 1, (
                "live bot's deal missing — create_deal path broken"
            )

            # The ids are distinct (different random suffixes).
            assert rows_a[0]["id"] != rows_b[0]["id"]
            # Both ids match the new format.
            assert DEAL_ID_RE.match(rows_a[0]["id"])
            assert DEAL_ID_RE.match(rows_b[0]["id"])
        finally:
            for eng in (engine_a, engine_b):
                eng._notify_queue.put(None)
                eng._notify_thread.join(timeout=5)

    def test_open_refused_on_exhausted_retries(self, fresh_db):
        """Last-resort safety: if the generator hits the same suffix
        3 times in a row (absurdly unlikely — probability 1e-12), the
        engine must REFUSE the open instead of silently mutating
        in-memory state without a DB backing row."""
        engine = self._make_engine("stuck_bot", fresh_db)
        try:
            # Pre-plant a row with a specific id, then force the
            # generator to always return that id.
            deal_store.create_deal(
                _deal("202604191342-0007"), "someone_else", "SE", user_id=1,
            )
            with patch.object(
                engine.state, "new_deal_id",
                return_value="202604191342-0007",
            ):
                engine._open_deal(80000.0)

            # In-memory state must stay empty — no silent divergence.
            assert len(engine.state.open_deals) == 0
            # Other bot's row untouched.
            rows = deal_store.get_deals(user_id=1)
            assert len(rows) == 1
            assert rows[0]["bot_slug"] == "someone_else"
        finally:
            engine._notify_queue.put(None)
            engine._notify_thread.join(timeout=5)
