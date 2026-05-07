# tests/test_liquidation_guard.py
# Tests for the LiquidationGuard formula.

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.liquidation_guard import calculate_liquidation_price


class TestLiquidationPrice:

    def test_no_leverage_returns_zero(self):
        """Without leverage there is no liquidation risk."""
        assert calculate_liquidation_price(80000.0, 1) == 0.0
        assert calculate_liquidation_price(80000.0, 0) == 0.0

    def test_long_liq_below_entry(self):
        """Liquidation price for a long must sit below the entry."""
        liq = calculate_liquidation_price(80000.0, 2, "long")
        assert liq < 80000.0
        assert liq > 0.0

    def test_short_liq_above_entry(self):
        """Liquidation price for a short must sit above the entry."""
        liq = calculate_liquidation_price(80000.0, 2, "short")
        assert liq > 80000.0

    def test_higher_leverage_closer_to_entry(self):
        """Higher leverage = liquidation price closer to entry."""
        liq_2x  = calculate_liquidation_price(80000.0, 2,  "long")
        liq_5x  = calculate_liquidation_price(80000.0, 5,  "long")
        liq_10x = calculate_liquidation_price(80000.0, 10, "long")
        assert liq_10x > liq_5x > liq_2x

    def test_bitget_screenshot_verification(self):
        """
        Verify against the real Bitget value from the screenshot:
        Entry: $70,978.8 | Leverage: 2x | Isolated | Long
        Bitget shows: $35,810.90
        Acceptable deviation: < 1% (for a warning system)
        """
        liq = calculate_liquidation_price(70978.8, 2, "long")
        bitget_actual = 35810.90
        deviation_pct = abs(liq - bitget_actual) / bitget_actual * 100
        assert deviation_pct < 1.0, (
            f"Liquidation price {liq} deviates {deviation_pct:.2f}% from "
            f"Bitget's actual value {bitget_actual}"
        )

    def test_2x_long_approx_half_entry(self):
        """At 2x leverage the liquidation price is roughly 50% of the entry."""
        liq = calculate_liquidation_price(80000.0, 2, "long")
        # Must sit between 48% and 52% of entry
        assert 0.48 * 80000.0 < liq < 0.52 * 80000.0

    def test_10x_long_approx_90pct_drop(self):
        """At 10x leverage the liquidation price is roughly 90% of the entry (10% drop)."""
        liq = calculate_liquidation_price(80000.0, 10, "long")
        # Must sit between 8% and 12% of entry
        assert 0.88 * 80000.0 < liq < 0.95 * 80000.0
