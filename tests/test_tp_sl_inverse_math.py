"""Tests for inverse-perpetual TP/SL math (pt-041) and side-aware
exit logic (pt-064).

Both findings were surfaced by docs/architecture-investigation-tp-dca-correctness.md.

This file pins the contract of the new ``core/inverse_perp_math.py``
helpers AND verifies the engine's exit decisions for both long and
short deals. The crucial regression test is
``test_realized_pnl_matches_configured_tp_after_fire_long`` — the
test that "lets pt-041 hide" per the investigation memo. Without it,
a future revert to the linear-target shape goes unnoticed because
the existing test suite only asserts ``pnl > 0`` on TP fire, not
``pnl ≈ tp_pct``.

Worked example from the investigation memo (Part 3.4):
  Long position at $63,000, 1 DCA fill at $61,000 (equal sizes).
  avg_entry = (63000 + 61000) / 2 = $62,000.
  Pre-fix linear target = 62000 * 1.03 = $63,860.
  Post-fix inverse target = 62000 / 0.97 = $63,917.526.
  Realized PnL at the post-fix target = exactly 3.00% (vs ~2.91%
  pre-fix when the linear target was hit).
"""

from __future__ import annotations

import math
import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.inverse_perp_math import (
    compute_sl_target_price,
    compute_tp_target_price,
)
from paper.paper_engine import PaperEngine
from paper.paper_state import PaperDeal, PaperOrder


# ── Test helpers (mirror tests/test_trading_engine.py) ──────────────────────


def _order(price, size=0.001, order_type="base", order_number=1):
    return PaperOrder(
        order_number=order_number, price=price, size=size,
        timestamp=datetime.now(UTC), order_type=order_type,
    )


def _deal(price=63000.0, size=0.001, side="long", lev=1):
    return PaperDeal(
        id="T-0001", bot_name="tb", symbol="BTC/USD",
        side=side, leverage=lev, orders=[_order(price, size)],
    )


def _notifier():
    n = MagicMock()
    for m in [
        "notify_startup", "notify_shutdown", "notify_entry", "notify_dca",
        "notify_take_profit", "notify_stop_loss", "notify_error",
        "notify_schedule_open", "notify_schedule_close",
    ]:
        setattr(n, m, MagicMock())
    return n


def _engine(sl_type="fixed", sl_pct=5.0, tp_pct=3.0, max_orders=5,
            spacing=2.5, mult=1.0, base_size=0.001):
    cfg = MagicMock()
    cfg.name = "tb"
    cfg.pair = "BTC/USD"
    cfg.mode.value = "paper"
    cfg.exchange.value = "bitget"
    cfg.leverage.enabled = False
    cfg.leverage.size = 1
    cfg.leverage.liquidation_guard.warn_pct = 15.0
    cfg.leverage.liquidation_guard.emergency_close_pct = 5.0
    cfg.take_profit.target_pct = tp_pct
    cfg.take_profit.indicator_confirm = None
    cfg.take_profit.minimum_tp_pct = None
    cfg.stop_loss.type = sl_type
    cfg.stop_loss.pct = sl_pct
    cfg.dca.max_orders = max_orders
    cfg.dca.order_spacing_pct = spacing
    cfg.dca.multiplier = mult
    cfg.dca.base_order_size = base_size
    cfg.dca.taker_fee = 0.0006
    cfg.dca.step_scale = 1.0
    cfg.entry.indicators = []
    cfg.schedule.trading_windows = []
    cfg.schedule.blackout_dates = []
    cfg.schedule.timezone = "Europe/Amsterdam"
    cfg.telegram.notify_on = []
    cfg.ml.enabled = False
    return PaperEngine(
        config=cfg, exchange=MagicMock(),
        notifier=_notifier(), initial_balance_btc=0.1,
    )


# ── Pure helper unit tests ─────────────────────────────────────────────────


class TestComputeTpTarget:
    """``compute_tp_target_price`` derives the inverse-perp price that
    yields exactly ``tp_pct`` realized PnL — symmetric with
    ``PaperDeal.calculate_pnl`` in paper/paper_state.py."""

    def test_long_3pct_known_value(self):
        # 63000 / 0.97 = 64948.45...
        target = compute_tp_target_price(63000.0, 3.0, "long")
        assert math.isclose(target, 64948.4536, rel_tol=1e-6)

    def test_short_3pct_known_value(self):
        # 63000 / 1.03 = 61165.05...
        target = compute_tp_target_price(63000.0, 3.0, "short")
        assert math.isclose(target, 61165.0485, rel_tol=1e-6)

    def test_long_5pct_known_value(self):
        # 63000 / 0.95 = 66315.79
        target = compute_tp_target_price(63000.0, 5.0, "long")
        assert math.isclose(target, 66315.7894, rel_tol=1e-6)

    def test_short_5pct_known_value(self):
        # 63000 / 1.05 = 60000.0
        target = compute_tp_target_price(63000.0, 5.0, "short")
        assert math.isclose(target, 60000.0, rel_tol=1e-6)

    def test_long_target_is_above_entry(self):
        for tp_pct in (0.5, 1.0, 3.0, 5.0, 10.0, 20.0):
            target = compute_tp_target_price(63000.0, tp_pct, "long")
            assert target > 63000.0, f"long TP target must be above entry at tp={tp_pct}"

    def test_short_target_is_below_entry(self):
        for tp_pct in (0.5, 1.0, 3.0, 5.0, 10.0, 20.0):
            target = compute_tp_target_price(63000.0, tp_pct, "short")
            assert target < 63000.0, f"short TP target must be below entry at tp={tp_pct}"

    def test_unknown_side_raises(self):
        with pytest.raises(ValueError, match="Unknown deal side"):
            compute_tp_target_price(63000.0, 3.0, "neutral")

    def test_pre_fix_linear_target_is_strictly_lower_for_long(self):
        """Document the asymmetry: post-fix inverse target sits ABOVE
        the pre-fix linear target. A future revert to the linear shape
        would make this assertion fail (target moves down → tests
        fire prematurely)."""
        avg = 63000.0
        for tp_pct in (1.0, 3.0, 5.0, 10.0):
            linear_target = avg * (1 + tp_pct / 100)
            inverse_target = compute_tp_target_price(avg, tp_pct, "long")
            assert inverse_target > linear_target


class TestComputeSlTarget:
    """``compute_sl_target_price`` derives the inverse-perp price that
    yields exactly ``-sl_pct`` realized PnL."""

    def test_long_5pct_known_value(self):
        # 63000 / 1.05 = 60000.0
        target = compute_sl_target_price(63000.0, 5.0, "long")
        assert math.isclose(target, 60000.0, rel_tol=1e-6)

    def test_short_5pct_known_value(self):
        # 63000 / 0.95 = 66315.79
        target = compute_sl_target_price(63000.0, 5.0, "short")
        assert math.isclose(target, 66315.7894, rel_tol=1e-6)

    def test_long_target_is_below_entry(self):
        for sl_pct in (0.5, 1.0, 5.0, 10.0, 20.0):
            target = compute_sl_target_price(63000.0, sl_pct, "long")
            assert target < 63000.0

    def test_short_target_is_above_entry(self):
        for sl_pct in (0.5, 1.0, 5.0, 10.0, 20.0):
            target = compute_sl_target_price(63000.0, sl_pct, "short")
            assert target > 63000.0

    def test_unknown_side_raises(self):
        with pytest.raises(ValueError, match="Unknown deal side"):
            compute_sl_target_price(63000.0, 5.0, "ambiguous")


# ── pt-041 regression: realized PnL matches configured pct ─────────────────


class TestRealizedPnlMatchesConfiguredPct:
    """The crucial regression — at TP-target-hit, the deal's
    ``calculate_pnl`` should return ~tp_pct. Pre-fix this would have
    returned ``tp_pct/(1+tp_pct/100)`` — see investigation memo
    Part 3.

    Tests cover both the helper-derived target AND the engine path
    end-to-end so a refactor that decouples them surfaces both ways.
    """

    @pytest.mark.parametrize("tp_pct", [1.0, 3.0, 5.0, 10.0, 20.0])
    def test_long_realized_pnl_matches_tp_pct_via_helper(self, tp_pct):
        avg = 63000.0
        target = compute_tp_target_price(avg, tp_pct, "long")
        deal = _deal(avg, side="long")
        pnl_btc, pnl_pct = deal.calculate_pnl(target)
        assert pnl_pct == pytest.approx(tp_pct, abs=1e-6), (
            f"long TP {tp_pct}% delivered {pnl_pct:.6f}% realized "
            f"(target=${target:.2f}, avg=${avg:.2f})"
        )

    @pytest.mark.parametrize("tp_pct", [1.0, 3.0, 5.0, 10.0, 20.0])
    def test_short_realized_pnl_matches_tp_pct_via_helper(self, tp_pct):
        avg = 63000.0
        target = compute_tp_target_price(avg, tp_pct, "short")
        deal = _deal(avg, side="short")
        pnl_btc, pnl_pct = deal.calculate_pnl(target)
        assert pnl_pct == pytest.approx(tp_pct, abs=1e-6), (
            f"short TP {tp_pct}% delivered {pnl_pct:.6f}% realized "
            f"(target=${target:.2f}, avg=${avg:.2f})"
        )

    @pytest.mark.parametrize("sl_pct", [1.0, 5.0, 10.0])
    def test_long_realized_pnl_matches_sl_pct_via_helper(self, sl_pct):
        avg = 63000.0
        target = compute_sl_target_price(avg, sl_pct, "long")
        deal = _deal(avg, side="long")
        pnl_btc, pnl_pct = deal.calculate_pnl(target)
        # SL is a *loss* — realized pct should be negative sl_pct.
        assert pnl_pct == pytest.approx(-sl_pct, abs=1e-6)

    @pytest.mark.parametrize("sl_pct", [1.0, 5.0, 10.0])
    def test_short_realized_pnl_matches_sl_pct_via_helper(self, sl_pct):
        avg = 63000.0
        target = compute_sl_target_price(avg, sl_pct, "short")
        deal = _deal(avg, side="short")
        pnl_btc, pnl_pct = deal.calculate_pnl(target)
        assert pnl_pct == pytest.approx(-sl_pct, abs=1e-6)

    def test_engine_long_tp_fire_realizes_configured_pct(self):
        """End-to-end: engine fires TP, closed_deal carries
        realized pnl_pct ≈ configured tp_pct.

        This is the test that "lets pt-041 hide" — pre-fix the engine
        would close at the linear target with realized PnL of
        ``tp_pct/(1 + tp_pct/100)``. Post-fix the realization matches
        the configuration.
        """
        e = _engine(tp_pct=3.0)
        d = _deal(80000.0, side="long")
        e.state.open_deal(d)
        target = compute_tp_target_price(80000.0, 3.0, "long")
        e._check_tp(d, target)
        closed = e.state.closed_deals[0]
        assert closed.close_reason == "tp"
        assert closed.pnl_pct == pytest.approx(3.0, abs=1e-6)


# ── Investigation-memo worked example ──────────────────────────────────────


class TestInvestigationMemoWorkedExample:
    """Reproduces the exact scenario from
    docs/architecture-investigation-tp-dca-correctness.md Part 3.4:

      Long, base 0.001 BTC at $63,000, 1 equal-size DCA at $61,000,
      TP=3 %.

      avg_entry = (63000 + 61000) / 2 = $62,000.
      Pre-fix linear target  = 62000 × 1.03 = $63,860.
      Post-fix inverse target = 62000 / 0.97 = $63,917.526.
      Pre-fix realized at $63,860 = 2.913 %.
      Post-fix realized at $63,917.53 = exactly 3.00 %.
    """

    def test_avg_entry_after_dca_is_62000(self):
        d = _deal(63000.0, side="long")
        d.orders.append(_order(61000.0, 0.001, "dca", 2))
        assert d.avg_entry_price == pytest.approx(62000.0, abs=1e-6)

    def test_post_fix_target_is_63917_53(self):
        target = compute_tp_target_price(62000.0, 3.0, "long")
        assert target == pytest.approx(63917.5258, rel=1e-6)

    def test_pre_fix_target_is_strictly_lower(self):
        pre_fix = 62000.0 * 1.03
        post_fix = compute_tp_target_price(62000.0, 3.0, "long")
        assert pre_fix < post_fix
        assert pre_fix == pytest.approx(63860.0, abs=1e-6)

    def test_realized_pnl_at_post_fix_target_is_3pct(self):
        """The whole point of pt-041's fix."""
        d = _deal(63000.0, size=0.001, side="long")
        d.orders.append(_order(61000.0, 0.001, "dca", 2))
        target = compute_tp_target_price(d.avg_entry_price, 3.0, "long")
        _, pnl_pct = d.calculate_pnl(target)
        assert pnl_pct == pytest.approx(3.0, abs=1e-6)

    def test_realized_pnl_at_pre_fix_target_is_2_913pct(self):
        """Documents the bug magnitude — at the OLD linear target,
        realized pnl_pct is the asymmetric value tp/(1+tp/100). This
        test exists so a future audit comparing pre/post numbers has
        the regression value pinned in code."""
        d = _deal(63000.0, size=0.001, side="long")
        d.orders.append(_order(61000.0, 0.001, "dca", 2))
        pre_fix_target = d.avg_entry_price * 1.03
        _, pnl_pct = d.calculate_pnl(pre_fix_target)
        # 3 / 1.03 = 2.91262135...
        assert pnl_pct == pytest.approx(2.91262135, rel=1e-6)


# ── pt-064: side-aware exit comparisons ────────────────────────────────────


class TestCheckTpDirectionAware:
    """``_check_tp`` reads ``deal.side`` and compares appropriately.

    Long: trigger when price >= target (target above entry).
    Short: trigger when price <= target (target below entry).
    """

    def test_long_fires_at_or_above_target(self):
        e = _engine(tp_pct=3.0)
        d = _deal(80000.0, side="long")
        e.state.open_deal(d)
        target = compute_tp_target_price(80000.0, 3.0, "long")
        e._check_tp(d, target)
        assert d.id not in e.state.open_deals

    def test_long_no_fire_below_target(self):
        e = _engine(tp_pct=3.0)
        d = _deal(80000.0, side="long")
        e.state.open_deal(d)
        target = compute_tp_target_price(80000.0, 3.0, "long")
        e._check_tp(d, target - 1.0)
        assert d.id in e.state.open_deals

    def test_short_fires_at_or_below_target(self):
        e = _engine(tp_pct=3.0)
        d = _deal(80000.0, side="short")
        e.state.open_deal(d)
        target = compute_tp_target_price(80000.0, 3.0, "short")
        e._check_tp(d, target)
        assert d.id not in e.state.open_deals

    def test_short_no_fire_above_target(self):
        e = _engine(tp_pct=3.0)
        d = _deal(80000.0, side="short")
        e.state.open_deal(d)
        target = compute_tp_target_price(80000.0, 3.0, "short")
        e._check_tp(d, target + 1.0)
        assert d.id in e.state.open_deals

    def test_short_does_not_fire_on_adverse_move(self):
        """Pre-pt-064 regression: a short bot at $80,000 would have
        had its TP target derived as ``avg * 1.03 = $82,400`` (long
        formula). At a 3 % adverse move (price rising to $82,400)
        the OLD code would have fired TP — closing the deal at a
        ~3 % LOSS for a short. Post-fix the short branch gives a
        target BELOW entry and the adverse rise produces no fire."""
        e = _engine(tp_pct=3.0)
        d = _deal(80000.0, side="short")
        e.state.open_deal(d)
        # Adverse move: price rises 3 % (a loss for a short).
        adverse_price = 80000.0 * 1.03
        e._check_tp(d, adverse_price)
        assert d.id in e.state.open_deals, (
            "short TP must NOT fire on a price RISE (the adverse "
            "direction for a short) — regression of pt-064"
        )


class TestCheckSlDirectionAware:
    """``_check_sl`` mirror of TP for the SL line."""

    def test_long_fires_at_or_below_sl(self):
        e = _engine(sl_type="fixed", sl_pct=5.0)
        d = _deal(80000.0, side="long")
        e.state.open_deal(d)
        sl_line = compute_sl_target_price(80000.0, 5.0, "long")
        e._check_sl(d, sl_line)
        assert d.id not in e.state.open_deals

    def test_long_no_fire_above_sl(self):
        e = _engine(sl_type="fixed", sl_pct=5.0)
        d = _deal(80000.0, side="long")
        e.state.open_deal(d)
        sl_line = compute_sl_target_price(80000.0, 5.0, "long")
        e._check_sl(d, sl_line + 1.0)
        assert d.id in e.state.open_deals

    def test_short_fires_at_or_above_sl(self):
        e = _engine(sl_type="fixed", sl_pct=5.0)
        d = _deal(80000.0, side="short")
        e.state.open_deal(d)
        sl_line = compute_sl_target_price(80000.0, 5.0, "short")
        e._check_sl(d, sl_line)
        assert d.id not in e.state.open_deals

    def test_short_no_fire_below_sl(self):
        e = _engine(sl_type="fixed", sl_pct=5.0)
        d = _deal(80000.0, side="short")
        e.state.open_deal(d)
        sl_line = compute_sl_target_price(80000.0, 5.0, "short")
        e._check_sl(d, sl_line - 1.0)
        assert d.id in e.state.open_deals

    def test_short_does_not_fire_on_favourable_move(self):
        """Pre-pt-064: a short bot at $80,000 had its SL line at
        ``avg * 0.95 = $76,000`` (long formula). A 5 % favourable
        move (price dropping to $76,000) would have closed the deal
        at a 5 % WIN as if it were an SL hit — wrong direction."""
        e = _engine(sl_type="fixed", sl_pct=5.0)
        d = _deal(80000.0, side="short")
        e.state.open_deal(d)
        # Favourable move: price drops 5 % (a win for a short).
        favourable_price = 80000.0 * 0.95
        e._check_sl(d, favourable_price)
        assert d.id in e.state.open_deals, (
            "short SL must NOT fire on a price DROP (the favourable "
            "direction for a short) — regression of pt-064"
        )


class TestCheckDcaDirectionAware:
    """``_check_dca`` honours ``deal.side`` for the comparison.

    NOTE: DCA spacing math (the ``last * (1 - step/100)`` formula) is
    out of scope for this PR — operator decided the long-direction
    spacing math stays. The short branch's *threshold* is still
    structurally wrong for inverse-perp shorts, so these tests pin
    only the *direction* of the comparison, not the magnitude.
    """

    def test_long_fires_below_threshold(self):
        e = _engine(spacing=2.5, max_orders=5)
        d = _deal(80000.0, side="long")
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.975)
        assert d.dca_count == 1

    def test_long_no_fire_above_threshold(self):
        e = _engine(spacing=2.5, max_orders=5)
        d = _deal(80000.0, side="long")
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.98)
        assert d.dca_count == 0

    def test_short_no_fire_on_drop(self):
        """Short DCA must NOT fire on a price drop (the favourable
        direction for a short — operator wants to ride the trend,
        not double down). Pre-pt-064 the long-comparison would have
        added DCA size at exactly that moment."""
        e = _engine(spacing=2.5, max_orders=5)
        d = _deal(80000.0, side="short")
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.975)
        assert d.dca_count == 0

    def test_short_fires_on_adverse_rise(self):
        """Short DCA fires on a price RISE (the adverse direction).
        The threshold for a 2.5 % step is mirrored above the last
        order — even though the spacing magnitude is still using
        the long formula, the *direction* check is correct."""
        e = _engine(spacing=2.5, max_orders=5)
        d = _deal(80000.0, side="short")
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 1.025)
        assert d.dca_count == 1
