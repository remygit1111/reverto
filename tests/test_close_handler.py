"""Tests for paper/close_handler.py — standalone deal close/cancel.

The handler backs two call-paths: the paper engine's tick-loop
(delegated from ``_check_deal_sentinels``) and the portal's
``DELETE /api/bots/{slug}/deals/{deal_id}`` offline branch. These
tests exercise the handler directly without the engine + portal
wrapping, so both paths inherit the assertions.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.database import get_db
from paper.close_handler import DealCloseHandler
from paper.paper_state import PaperDeal, PaperOrder, PaperState
from paper.state_io import StateIO


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_state_file(tmp_path):
    """Dedicated state.json path for each test — tmp_path makes it
    auto-cleaned and StateIO's atomic writes leave no residue."""
    return tmp_path / "state.json"


@pytest.fixture
def state_io(tmp_state_file):
    return StateIO(tmp_state_file, slug="testbot")


@pytest.fixture
def seed_user():
    """Seed a ``users(id=1)`` row so ``deal_store.close_deal`` can
    satisfy its user-id FK. The autouse ``_isolate_reverto_db`` in
    conftest.py already runs ``init_db()`` which seeds admin, but we
    guard against a refactor that would change that seed."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, role) "
        "VALUES (1, 'admin', 'admin')"
    )
    conn.commit()


@pytest.fixture
def deal_in_state():
    """Build a PaperState with one open deal. Entry price = 100.0,
    size = 0.0001 BTC, long leverage=1.
    Returns (state, deal_id) for direct assertions."""
    state = PaperState(initial_balance_btc=0.1)
    deal = PaperDeal(
        id="202604211900-0001",
        bot_name="testbot",
        symbol="BTC/USD",
        side="long",
        leverage=1,
        orders=[PaperOrder(
            order_number=1,
            price=100.0,
            size=0.0001,
            timestamp=datetime.now(UTC),
            order_type="base",
        )],
    )
    state.open_deals[deal.id] = deal
    return state, deal.id


def _make_handler(
    state, state_io, *, bot_slug="testbot", bot_name="testbot",
    taker_fee=0.0006, notifier=None, notify_enqueue=None,
):
    """DealCloseHandler factory with paper-friendly defaults. Tests
    override only what they care about."""
    return DealCloseHandler(
        user_id=1,
        bot_slug=bot_slug,
        bot_name=bot_name,
        state=state,
        state_io=state_io,
        taker_fee=taker_fee,
        notifier=notifier,
        notify_enqueue=notify_enqueue,
    )


# ── Close branch ────────────────────────────────────────────────────────

class TestCloseDeal:

    def test_close_moves_deal_from_open_to_closed(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io)

        result = handler.close_deal(deal_id, current_price=110.0)

        assert result["ok"] is True
        assert deal_id not in state.open_deals
        assert len(state.closed_deals) == 1
        assert state.closed_deals[0].id == deal_id
        assert state.closed_deals[0].close_reason == "manual"

    def test_close_writes_db_row(
        self, deal_in_state, state_io, seed_user,
    ):
        """``deal_store.close_deal`` is UPDATE-only; seed the open-deal
        row first so the close can target it. In production the
        row-create happens in ``_open_deal`` before the deal ever gets
        to the close path, so this seeding mirrors real flow."""
        from core import deal_store
        state, deal_id = deal_in_state
        # Seed the deals row so close_deal's UPDATE has a target.
        deal_store.create_deal(
            state.open_deals[deal_id], "testbot", "testbot", user_id=1,
        )

        handler = _make_handler(state, state_io)
        handler.close_deal(deal_id, current_price=110.0)

        conn = get_db()
        row = conn.execute(
            "SELECT status, close_reason, pnl_btc FROM deals WHERE id = ?",
            (deal_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "closed"
        assert row["close_reason"] == "manual"

    def test_close_calculates_pnl_from_entry_delta(
        self, deal_in_state, state_io, seed_user,
    ):
        """Long deal, entry 100, close 110, size 0.0001, lev 1.
        Inverse-perpetual formula (pt-043 fix):
        pnl_btc = 0.0001 * (110 - 100) / 110 * 1 ≈ 9.0909e-6."""
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io)

        result = handler.close_deal(deal_id, current_price=110.0)

        expected_pnl = 0.0001 * (110.0 - 100.0) / 110.0
        assert result["pnl_btc"] == pytest.approx(expected_pnl, rel=1e-9)
        # Percent of margin (size / leverage = 0.0001 / 1 = 0.0001 BTC).
        # pnl_pct = pnl_btc / margin * 100 ≈ 9.0909%
        expected_pct = (expected_pnl / (0.0001 / 1)) * 100
        assert result["pnl_pct"] == pytest.approx(expected_pct, rel=1e-9)

    def test_close_deducts_exit_fee_from_balance(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        balance_before = state.balance_btc
        handler = _make_handler(state, state_io, taker_fee=0.001)

        result = handler.close_deal(deal_id, current_price=110.0)

        # Fee = size * taker_fee = 0.0001 * 0.001 = 1e-7 BTC.
        # The state's close_deal applies PnL to balance (adds pnl_btc)
        # and then the handler deducts the fee. So final balance is:
        #   initial + pnl_btc - fee
        # PnL uses the inverse-perpetual formula (pt-043) — denominator
        # is ``current_price``, not ``entry``.
        expected_fee = 0.0001 * 0.001
        expected_pnl = 0.0001 * (110.0 - 100.0) / 110.0
        assert result["fee_btc"] == pytest.approx(expected_fee)
        assert state.balance_btc == pytest.approx(
            balance_before + expected_pnl - expected_fee, rel=1e-9,
        )

    def test_close_persists_state_json(
        self, deal_in_state, state_io, seed_user, tmp_state_file,
    ):
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io)
        handler.close_deal(deal_id, current_price=110.0)

        # state.json now on disk with the deal moved to closed_deals.
        assert tmp_state_file.exists()
        payload = json.loads(tmp_state_file.read_text(encoding="utf-8"))
        open_ids = [d["id"] for d in payload.get("open_deals", [])]
        closed_ids = [d["id"] for d in payload.get("closed_deals", [])]
        assert deal_id not in open_ids
        assert deal_id in closed_ids

    def test_close_returns_deal_shape_matching_read_state(
        self, deal_in_state, state_io, seed_user,
    ):
        """``result["deal"]`` must be the ``deal_to_dict`` output, so
        the portal can return it directly in an HTTP response body."""
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io)

        result = handler.close_deal(deal_id, current_price=110.0)

        deal_dict = result["deal"]
        assert deal_dict["id"] == deal_id
        assert deal_dict["close_reason"] == "manual"
        assert deal_dict["is_open"] is False
        assert "pnl_btc" in deal_dict


# ── Cancel branch ───────────────────────────────────────────────────────

class TestCancelDeal:

    def test_cancel_closes_without_realising_pnl(
        self, deal_in_state, state_io, seed_user,
    ):
        """Cancel drops the deal + records pnl 0 in the DB regardless
        of how far the price has moved. The balance is still adjusted
        by PaperState.close_deal (its contract), but the DB row shows
        zero because the exit trade didn't actually happen."""
        from core import deal_store
        state, deal_id = deal_in_state
        # Seed row so UPDATE in close_deal has something to hit.
        deal_store.create_deal(
            state.open_deals[deal_id], "testbot", "testbot", user_id=1,
        )

        handler = _make_handler(state, state_io)
        result = handler.close_deal(
            deal_id, current_price=110.0, action="cancel",
        )

        assert result["ok"] is True
        assert result["action"] == "cancel"
        assert result["pnl_btc"] == 0.0
        assert result["pnl_pct"] == 0.0

        # DB row records the zero pnl.
        conn = get_db()
        row = conn.execute(
            "SELECT close_reason, pnl_btc FROM deals WHERE id = ?",
            (deal_id,),
        ).fetchone()
        assert row["close_reason"] == "cancelled"
        assert row["pnl_btc"] == 0.0

    def test_cancel_does_not_deduct_exit_fee(
        self, deal_in_state, state_io, seed_user,
    ):
        """Cancel doesn't place an exit order; no fee."""
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io, taker_fee=0.001)

        result = handler.close_deal(
            deal_id, current_price=110.0, action="cancel",
        )

        assert result["fee_btc"] == 0.0


# ── Error paths ─────────────────────────────────────────────────────────

class TestErrorPaths:

    def test_unknown_deal_id_returns_error(
        self, deal_in_state, state_io, seed_user,
    ):
        state, _ = deal_in_state
        handler = _make_handler(state, state_io)

        result = handler.close_deal(
            "does-not-exist", current_price=110.0,
        )

        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_invalid_action_returns_error(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io)

        result = handler.close_deal(
            deal_id, current_price=110.0, action="nuke",
        )

        assert result["ok"] is False
        assert "invalid action" in result["error"].lower()
        # Deal is still open — error path didn't mutate state.
        assert deal_id in state.open_deals

    def test_nonpositive_price_returns_error(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io)

        result = handler.close_deal(deal_id, current_price=0.0)

        assert result["ok"] is False
        assert "> 0" in result["error"]
        assert deal_id in state.open_deals

    def test_unparseable_price_returns_error(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io)

        result = handler.close_deal(
            deal_id, current_price="not-a-number",
        )

        assert result["ok"] is False
        assert "invalid" in result["error"].lower()
        assert deal_id in state.open_deals


# ── Notifier integration ────────────────────────────────────────────────

class TestNotifier:

    def test_no_notifier_no_crash(
        self, deal_in_state, state_io, seed_user,
    ):
        """Portal path: handler is constructed with notifier=None so
        manual closes don't double-notify. Must not touch
        self.notifier in any code path."""
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io, notifier=None)

        result = handler.close_deal(deal_id, current_price=110.0)

        assert result["ok"] is True

    def test_close_fires_tp_style_notification(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        notifier = MagicMock()
        handler = _make_handler(state, state_io, notifier=notifier)

        handler.close_deal(deal_id, current_price=110.0)

        notifier.notify_take_profit.assert_called_once()

    def test_cancel_fires_sl_style_notification(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        notifier = MagicMock()
        handler = _make_handler(state, state_io, notifier=notifier)

        handler.close_deal(
            deal_id, current_price=110.0, action="cancel",
        )

        notifier.notify_stop_loss.assert_called_once()

    def test_notify_enqueue_used_when_provided(
        self, deal_in_state, state_io, seed_user,
    ):
        """Running-bot context passes ``self._notify`` as the queue
        hook so Telegram calls stay off the hot tick-loop path."""
        state, deal_id = deal_in_state
        notifier = MagicMock()
        enqueued: list[tuple] = []

        def _capture(fn, *args, **kwargs):
            enqueued.append((fn, args, kwargs))

        handler = _make_handler(
            state, state_io,
            notifier=notifier, notify_enqueue=_capture,
        )

        handler.close_deal(deal_id, current_price=110.0)

        # Queue received the call; notifier itself was NOT invoked
        # directly (matches the running-bot non-blocking contract).
        notifier.notify_take_profit.assert_not_called()
        assert len(enqueued) == 1
        assert enqueued[0][0] is notifier.notify_take_profit


# ── triggered_by dispatch ───────────────────────────────────────────────

class TestTriggeredByDispatch:
    """The handler fires a different Telegram method depending on
    where the close originated: the running-bot tick-loop path
    (default ``triggered_by='engine'``) uses the legacy TP/SL
    notifications so recipients see no change vs pre-refactor;
    portal-initiated closes (``triggered_by='portal'``) use dedicated
    ``notify_manual_close`` / ``notify_manual_cancel`` methods so the
    operator-initiated events are traceable on Telegram."""

    def test_close_engine_origin_fires_tp_notification(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        notifier = MagicMock()
        handler = _make_handler(state, state_io, notifier=notifier)

        # Default argument is triggered_by='engine'.
        handler.close_deal(deal_id, current_price=110.0)

        notifier.notify_take_profit.assert_called_once()
        notifier.notify_manual_close.assert_not_called()

    def test_close_portal_origin_fires_manual_close(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        notifier = MagicMock()
        handler = _make_handler(state, state_io, notifier=notifier)

        handler.close_deal(
            deal_id, current_price=110.0, triggered_by="portal",
        )

        notifier.notify_manual_close.assert_called_once()
        notifier.notify_take_profit.assert_not_called()
        # Args match the TelegramNotifier.notify_manual_close signature:
        # (bot_name, symbol, price, pnl_btc, pnl_pct).
        args, _ = notifier.notify_manual_close.call_args
        assert args[0] == "testbot"       # bot_name
        assert args[1] == "BTC/USD"       # symbol
        assert args[2] == 110.0           # price

    def test_cancel_engine_origin_fires_sl_notification(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        notifier = MagicMock()
        handler = _make_handler(state, state_io, notifier=notifier)

        handler.close_deal(
            deal_id, current_price=110.0, action="cancel",
        )

        notifier.notify_stop_loss.assert_called_once()
        notifier.notify_manual_cancel.assert_not_called()

    def test_cancel_portal_origin_fires_manual_cancel(
        self, deal_in_state, state_io, seed_user,
    ):
        state, deal_id = deal_in_state
        notifier = MagicMock()
        handler = _make_handler(state, state_io, notifier=notifier)

        handler.close_deal(
            deal_id, current_price=110.0,
            action="cancel", triggered_by="portal",
        )

        notifier.notify_manual_cancel.assert_called_once()
        notifier.notify_stop_loss.assert_not_called()

    def test_portal_origin_without_notifier_still_closes(
        self, deal_in_state, state_io, seed_user,
    ):
        """Portal path constructs the handler with ``notifier=None``
        when Telegram env-vars aren't set. The close must succeed
        regardless — Telegram is a side channel, not on the close
        path's critical route."""
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io, notifier=None)

        result = handler.close_deal(
            deal_id, current_price=110.0, triggered_by="portal",
        )
        assert result["ok"] is True
        assert deal_id not in state.open_deals

    def test_invalid_triggered_by_returns_error(
        self, deal_in_state, state_io, seed_user,
    ):
        """Typo'd origin values fail loud with an error dict rather
        than silently falling through to one branch or the other."""
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io)

        result = handler.close_deal(
            deal_id, current_price=110.0, triggered_by="cron",
        )
        assert result["ok"] is False
        assert "invalid triggered_by" in result["error"].lower()
        # State untouched by the error path.
        assert deal_id in state.open_deals

    def test_portal_origin_without_manual_methods_falls_back(
        self, deal_in_state, state_io, seed_user,
    ):
        """Older notifier implementations may not have
        ``notify_manual_close`` yet (test fixtures, third-party
        stubs). The close must skip the notification silently
        instead of crashing with AttributeError — matches the
        defensive ``getattr`` pattern in ``_maybe_notify_manual_*``.
        """
        # Spec-limited stub that lacks notify_manual_close /
        # notify_manual_cancel entirely.
        notifier = MagicMock(spec=["notify_take_profit", "notify_stop_loss"])
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io, notifier=notifier)

        result = handler.close_deal(
            deal_id, current_price=110.0, triggered_by="portal",
        )
        assert result["ok"] is True
        # Neither the legacy nor the new notifier fired — portal
        # origin falls through both branches when the method is
        # missing, which is the intended safe default.
        notifier.notify_take_profit.assert_not_called()
        notifier.notify_stop_loss.assert_not_called()


# ── State preservation across closes ────────────────────────────────────

class TestStatePreservation:

    def test_persist_preserves_existing_drawdown_field(
        self, deal_in_state, state_io, seed_user, tmp_state_file,
    ):
        """Pre-existing fields the engine wrote (drawdown_guard, pause
        flags, etc.) must survive a close — the handler does a read-
        merge-write rather than an overwrite. Without this, an
        operator-initiated close via the portal offline path would
        reset the drawdown baseline silently."""
        state, deal_id = deal_in_state
        # Seed state.json with a drawdown_guard field the handler
        # should NOT clobber.
        tmp_state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_state_file.write_text(json.dumps({
            "bot_name": "testbot",
            "balance_btc": 0.1,
            "drawdown_guard": {"peak_value": 0.15, "triggered": False},
            "paused_by_drawdown": False,
        }), encoding="utf-8")

        handler = _make_handler(state, state_io)
        handler.close_deal(deal_id, current_price=110.0)

        payload = json.loads(tmp_state_file.read_text(encoding="utf-8"))
        # Handler updated balance + deal lists.
        assert payload["balance_btc"] != 0.1  # pnl was applied
        assert len(payload["closed_deals"]) == 1
        # But did NOT drop the drawdown bookkeeping.
        assert payload["drawdown_guard"]["peak_value"] == 0.15
        assert payload["paused_by_drawdown"] is False


# ── Slug-less engine compatibility ──────────────────────────────────────

class TestSlugLessEngine:
    """Paper-engine constructor allows ``slug=None`` so minimal test
    fixtures can build an engine without a DB wiring. The handler
    must tolerate ``bot_slug=None`` by skipping the DB write while
    still mutating state + persisting state.json."""

    def test_no_slug_skips_db_but_closes_state(
        self, deal_in_state, state_io,
    ):
        state, deal_id = deal_in_state
        handler = _make_handler(state, state_io, bot_slug=None)

        result = handler.close_deal(deal_id, current_price=110.0)

        # State-side close happened.
        assert result["ok"] is True
        assert deal_id not in state.open_deals
        # DB row absent — there never was one because slug was None.
        conn = get_db()
        row = conn.execute(
            "SELECT id FROM deals WHERE id = ?", (deal_id,),
        ).fetchone()
        assert row is None
