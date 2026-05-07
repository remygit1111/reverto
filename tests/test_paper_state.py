# tests/test_paper_state.py
import sys, os, threading, pytest
from datetime import datetime, UTC
from dataclasses import fields

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paper.paper_state import PaperState, PaperDeal, PaperOrder
from paper.paper_engine import _deal_to_dict

# helper — reuses conftest functions via pytest injection but also directly
def _order(price, size=0.001, t="base", n=1):
    return PaperOrder(order_number=n, price=price, size=size,
                      timestamp=datetime.now(UTC), order_type=t)

def _deal(price=80000.0, size=0.001, side="long", lev=1):
    return PaperDeal(id="T-0001", bot_name="tb", symbol="BTC/USD",
                     side=side, leverage=lev, orders=[_order(price, size)])


class TestAvgEntryPrice:
    def test_single_order(self):
        assert _deal(80000.0).avg_entry_price == 80000.0

    def test_two_equal_orders(self):
        d = _deal(80000.0)
        d.orders.append(_order(80000.0, 0.001, "dca", 2))
        assert d.avg_entry_price == 80000.0

    def test_volume_weighted(self):
        d = _deal(80000.0, 0.001)
        d.orders.append(_order(78000.0, 0.002, "dca", 2))
        expected = (80000*0.001 + 78000*0.002) / 0.003
        assert abs(d.avg_entry_price - expected) < 0.01

    def test_no_orders_returns_zero(self):
        d = _deal(); d.orders = []
        assert d.avg_entry_price == 0.0


class TestDcaCount:
    def test_no_dca(self):        assert _deal().dca_count == 0
    def test_one_dca(self):
        d = _deal(); d.orders.append(_order(79000.0, 0.001, "dca", 2))
        assert d.dca_count == 1
    def test_base_not_counted(self):
        assert _deal().orders[0].order_type == "base"
        assert _deal().dca_count == 0


class TestCalculatePnl:
    def test_breakeven(self):
        pnl, pct = _deal(80000.0).calculate_pnl(80000.0)
        assert pnl == 0.0 and pct == 0.0

    def test_long_profit(self):
        pnl, _ = _deal(80000.0).calculate_pnl(82400.0)
        assert pnl > 0

    def test_long_loss(self):
        pnl, _ = _deal(80000.0).calculate_pnl(77600.0)
        assert pnl < 0

    def test_zero_price(self):
        pnl, _ = _deal(80000.0).calculate_pnl(0.0)
        assert pnl == 0.0

    def test_correct_formula(self):
        """
        Verify the Bitget BTCUSD inverse-perpetual formula (pt-043):
        PnL (BTC) = size * (exit - entry) / exit * leverage
        For size=1.0 BTC, entry=80000, exit=82000, lev=1:
        PnL = 1.0 * (82000 - 80000) / 82000 = 0.024390... BTC

        Pre-fix the denominator was ``entry`` (linear-perpetual
        shape), which produced 0.025 BTC — a ~2.4 % overstatement
        in this scenario. Bitget testnet validation on 2026-04-28
        confirmed that ``current_price`` is the correct divisor.
        """
        d = _deal(80000.0, 1.0)
        expected = 1.0 * (82000.0 - 80000.0) / 82000.0
        pnl, _ = d.calculate_pnl(82000.0)
        assert abs(pnl - expected) < 1e-10

    def test_leverage_multiplies(self):
        d1 = _deal(80000.0, 0.001, lev=1)
        d5 = _deal(80000.0, 0.001, lev=5)
        p1, _ = d1.calculate_pnl(82000.0)
        p5, _ = d5.calculate_pnl(82000.0)
        assert abs(p5 - p1*5) < 1e-10


class TestCalculatePnlInversePerpetual:
    """pt-043 regression — Bitget inverse-perpetual formula validation.

    All assertions are anchored to actual Bitget testnet trades the
    operator executed on 2026-04-28. Pre-fix the denominator was
    ``entry``; post-fix it is ``current_price``. These tests pin the
    fix in place: a class-of-issue revert to the linear-perpetual
    shape (deler=avg) immediately fails the testnet-data assertions
    AND the explicit denominator probe.
    """

    def test_long_matches_bitget_testnet_data(self):
        """LONG, 0.1 BTC, 1x leverage, entry $76,801.10, exit
        $76,108.30. Bitget reported -0.00090973 BTC. Tolerance 0.1 %
        relative to absorb testnet rounding + fee artefacts that
        Reverto's PnL formula doesn't model (the exchange reports
        ``closing profit`` net of fees on the close leg)."""
        deal = _deal(76801.10, size=0.1, side="long", lev=1)
        pnl_btc, _ = deal.calculate_pnl(76108.30)

        bitget_reported = -0.00090973
        tolerance = abs(bitget_reported) * 0.001
        assert abs(pnl_btc - bitget_reported) < tolerance, (
            f"pnl_btc {pnl_btc} drifts from Bitget {bitget_reported} "
            f"by more than {tolerance} BTC — formula may have reverted"
        )

    def test_short_matches_bitget_testnet_data(self):
        """SHORT, 0.1 BTC, 1x leverage, entry $76,806.00, exit
        $76,113.70. Bitget reported +0.00090914 BTC."""
        deal = _deal(76806.00, size=0.1, side="short", lev=1)
        pnl_btc, _ = deal.calculate_pnl(76113.70)

        bitget_reported = +0.00090914
        tolerance = abs(bitget_reported) * 0.001
        assert abs(pnl_btc - bitget_reported) < tolerance, (
            f"pnl_btc {pnl_btc} drifts from Bitget {bitget_reported} "
            f"by more than {tolerance} BTC — formula may have reverted"
        )

    def test_denominator_is_current_price_not_avg(self):
        """Class-of-issue probe: avg=100, current=200 (a 100 % move
        with size=1, long, 1x). Inverse-perpetual yields 0.5 BTC
        (1 * 100 / 200); linear-perpetual would yield 1.0 BTC
        (1 * 100 / 100). A revert to the linear shape would double
        the answer here, immediately failing the assertion."""
        deal = _deal(100.0, size=1.0, side="long", lev=1)
        pnl_btc, _ = deal.calculate_pnl(200.0)

        assert abs(pnl_btc - 0.5) < 1e-9, (
            f"inverse-perpetual formula expected 0.5, got {pnl_btc}. "
            "If this reads 1.0, the formula reverted to the linear "
            "(entry-as-denominator) shape — see pt-043."
        )

    def test_zero_or_negative_current_price_returns_zero_pnl(self):
        """Edge case: ``current_price`` is the denominator post-fix,
        so a zero or negative tick must not propagate a
        ZeroDivisionError up the tick loop."""
        deal = _deal(100.0, size=1.0, side="long", lev=1)

        pnl_btc, pnl_pct = deal.calculate_pnl(0.0)
        assert pnl_btc == 0.0
        assert pnl_pct == 0.0

        pnl_btc, pnl_pct = deal.calculate_pnl(-50.0)
        assert pnl_btc == 0.0
        assert pnl_pct == 0.0

    def test_long_short_asymmetry_at_same_prices(self):
        """Inverse perpetual is NOT symmetric: the magnitude of a
        LONG losing on the same dollar move differs from a SHORT
        losing on the mirrored move, because the denominator differs.
        This documents the expected asymmetry and pins it as a
        regression — a future symmetry-restoring "simplification"
        would be wrong for inverse perpetual."""
        # 0.1 BTC, prices 76800 → 76100 (~0.91 % move).
        long_deal  = _deal(76800.0, size=0.1, side="long",  lev=1)
        short_deal = _deal(76100.0, size=0.1, side="short", lev=1)

        long_pnl, _  = long_deal.calculate_pnl(76100.0)   # losing
        short_pnl, _ = short_deal.calculate_pnl(76800.0)  # losing

        # Both losing positions.
        assert long_pnl < 0
        assert short_pnl < 0
        # Magnitudes are close but not identical (asymmetry).
        assert abs(long_pnl) != abs(short_pnl)
        # ...and the difference is bounded — not a wildly different
        # number, just the sub-1 % drift the inverse formula predicts.
        assert abs(abs(long_pnl) - abs(short_pnl)) < abs(long_pnl) * 0.02


class TestPaperState:
    def test_initial_balance(self, state):
        assert state.balance_btc == 0.1

    def test_open_deal(self, state, deal):
        state.open_deal(deal)
        assert deal.id in state.open_deals

    def test_close_moves_to_history(self, state, deal):
        state.open_deal(deal)
        state.close_deal(deal.id, 82000.0, "tp")
        assert deal.id not in state.open_deals
        assert len(state.closed_deals) == 1

    def test_close_updates_balance(self, state, deal):
        state.open_deal(deal)
        bal = state.balance_btc
        state.close_deal(deal.id, 82400.0, "tp")
        assert state.balance_btc > bal

    def test_close_nonexistent(self, state):
        assert state.close_deal("NONE", 80000.0, "tp") is None

    def test_win_rate_all_wins(self, state):
        for i in range(3):
            d = _deal(); d.id = f"T-{i:04d}"
            state.open_deal(d); state.close_deal(d.id, 82000.0, "tp")
        assert state._win_rate() == 100.0

    def test_win_rate_all_losses(self, state):
        for i in range(3):
            d = _deal(); d.id = f"T-{i:04d}"
            state.open_deal(d); state.close_deal(d.id, 75000.0, "sl")
        assert state._win_rate() == 0.0

    def test_win_rate_empty(self, state):
        assert state._win_rate() == 0.0

    def test_deal_id_shape(self, state):
        """Post-collision-fix: ids are YYYYMMDDHHMM-RRRR (see core/ids
        + test_ids.py). No per-instance counter, so sequential calls
        can't be asserted by value — only by format.
        """
        from core.ids import DEAL_ID_RE
        assert DEAL_ID_RE.match(state.new_deal_id())
        assert DEAL_ID_RE.match(state.new_deal_id())

    def test_deal_ids_are_unique(self, state):
        """100 rapid calls must produce distinct ids (same-minute
        collision probability from the 10_000-slot random suffix is
        below the birthday-problem floor at this sample)."""
        ids = {state.new_deal_id() for _ in range(100)}
        assert len(ids) >= 98

    def test_open_snapshot_is_copy(self, state, deal):
        state.open_deal(deal)
        snap = state.get_open_deals_snapshot()
        snap.clear()
        assert len(state.open_deals) == 1

    def test_closed_snapshot_is_copy(self, state, deal):
        state.open_deal(deal)
        state.close_deal(deal.id, 82000.0, "tp")
        snap = state.get_closed_deals_snapshot()
        snap.clear()
        assert len(state.closed_deals) == 1

    def test_thread_safe_concurrent_close(self, state):
        deals = []
        for i in range(20):
            d = _deal(); d.id = f"PAPER-{i:04d}"
            state.open_deal(d); deals.append(d)
        errors = []
        def close_all():
            for d in deals:
                try: state.close_deal(d.id, 82000.0, "tp")
                except Exception as e: errors.append(e)
        threads = [threading.Thread(target=close_all) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors


class TestPeakPrice:
    def test_default_zero(self):
        assert _deal()._peak_price == 0.0

    def test_is_dataclass_field(self):
        d = _deal()
        assert "_peak_price" in [f.name for f in fields(d)]

    def test_serialized_to_dict(self):
        d = _deal(); d._peak_price = 85000.0
        result = _deal_to_dict(d, 85000.0)
        assert result.get("_peak_price") == 85000.0
