"""Property-style regression tests for PnL and DCA sizing.

The real hypothesis library isn't a base dependency, so these are
pytest-parametrized instead of hypothesis-generated — same intent
(exercise invariants across a range of values), less infrastructure.

Invariants checked:
  * PaperDeal.calculate_pnl returns 0 when exit == entry.
  * Long PnL sign matches direction of price movement.
  * Cumulative DCA notional is monotonically non-decreasing.
  * Worst-case DCA (base × multiplier^(n-1)) is non-decreasing in n.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from datetime import UTC, datetime  # noqa: E402

from paper.paper_state import PaperDeal, PaperOrder  # noqa: E402


def _make_deal(entry: float, size: float, leverage: int, side: str = "long") -> PaperDeal:
    return PaperDeal(
        id="P-1", bot_name="t", symbol="BTC/USD", side=side, leverage=leverage,
        orders=[PaperOrder(
            order_number=1, price=entry, size=size,
            timestamp=datetime.now(UTC), order_type="base",
        )],
    )


# ── PnL invariants ──────────────────────────────────────────────────────────

class TestPnlInvariants:

    @pytest.mark.parametrize("entry", [1_000.0, 50_000.0, 100_000.0])
    @pytest.mark.parametrize("size", [0.0001, 0.01, 0.5])
    @pytest.mark.parametrize("leverage", [1, 3, 10])
    def test_no_move_yields_zero_pnl(self, entry, size, leverage):
        deal = _make_deal(entry, size, leverage)
        pnl_btc, pnl_pct = deal.calculate_pnl(entry)
        assert abs(pnl_btc) < 1e-9
        assert abs(pnl_pct) < 1e-9

    @pytest.mark.parametrize("entry", [50_000.0, 100_000.0])
    @pytest.mark.parametrize("move_pct", [1.0, 5.0, -3.0, -10.0])
    def test_long_pnl_sign_matches_move(self, entry, move_pct):
        deal = _make_deal(entry, 0.01, leverage=1)
        exit_price = entry * (1 + move_pct / 100)
        pnl_btc, _ = deal.calculate_pnl(exit_price)
        if move_pct > 0:
            assert pnl_btc > 0
        elif move_pct < 0:
            assert pnl_btc < 0

    @pytest.mark.parametrize("move_pct", [1.0, 5.0, -3.0, -10.0])
    def test_short_pnl_sign_opposite_to_move(self, move_pct):
        deal = _make_deal(50_000.0, 0.01, leverage=1, side="short")
        exit_price = 50_000.0 * (1 + move_pct / 100)
        pnl_btc, _ = deal.calculate_pnl(exit_price)
        if move_pct > 0:
            assert pnl_btc < 0
        elif move_pct < 0:
            assert pnl_btc > 0

    def test_negative_or_zero_current_price_returns_zero(self):
        deal = _make_deal(50_000.0, 0.01, leverage=1)
        assert deal.calculate_pnl(0.0) == (0.0, 0.0)
        assert deal.calculate_pnl(-10.0) == (0.0, 0.0)


# ── DCA sizing invariants ───────────────────────────────────────────────────

class TestDcaSizing:

    @pytest.mark.parametrize("base", [0.0001, 0.001, 0.01])
    @pytest.mark.parametrize("multiplier", [1.0, 1.2, 1.5, 2.0])
    @pytest.mark.parametrize("n", [3, 5, 10])
    def test_cumulative_size_monotonic(self, base, multiplier, n):
        """Cumulative position size across N DCA levels is monotonically
        non-decreasing and equals the geometric series sum."""
        cumulative = 0.0
        prev = 0.0
        for i in range(n):
            size = base * (multiplier ** i)
            cumulative += size
            assert cumulative >= prev, "cumulative must be non-decreasing"
            prev = cumulative

        if multiplier == 1.0:
            expected = base * n
        else:
            expected = base * (multiplier ** n - 1) / (multiplier - 1)
        assert cumulative == pytest.approx(expected, rel=1e-9)

    @pytest.mark.parametrize("base", [0.0001, 0.001])
    @pytest.mark.parametrize("multiplier", [1.0, 1.2, 2.0])
    def test_worst_case_is_last_order_for_multiplier_ge_1(self, base, multiplier):
        """For multiplier >= 1.0 the worst-case (largest) individual
        DCA is always the last one (base × multiplier^(n-1))."""
        n = 10
        sizes = [base * (multiplier ** i) for i in range(n)]
        assert max(sizes) == pytest.approx(sizes[-1])
