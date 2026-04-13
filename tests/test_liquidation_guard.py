# tests/test_liquidation_guard.py
# Tests voor de LiquidationGuard formule.

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.liquidation_guard import calculate_liquidation_price


class TestLiquidationPrice:

    def test_no_leverage_returns_zero(self):
        """Zonder leverage geen liquidatierisico."""
        assert calculate_liquidation_price(80000.0, 1) == 0.0
        assert calculate_liquidation_price(80000.0, 0) == 0.0

    def test_long_liq_below_entry(self):
        """Liquidatieprijs voor long moet onder de entry liggen."""
        liq = calculate_liquidation_price(80000.0, 2, "long")
        assert liq < 80000.0
        assert liq > 0.0

    def test_short_liq_above_entry(self):
        """Liquidatieprijs voor short moet boven de entry liggen."""
        liq = calculate_liquidation_price(80000.0, 2, "short")
        assert liq > 80000.0

    def test_higher_leverage_closer_to_entry(self):
        """Hogere leverage = liquidatieprijs dichter bij entry."""
        liq_2x  = calculate_liquidation_price(80000.0, 2,  "long")
        liq_5x  = calculate_liquidation_price(80000.0, 5,  "long")
        liq_10x = calculate_liquidation_price(80000.0, 10, "long")
        assert liq_10x > liq_5x > liq_2x

    def test_bitget_screenshot_verification(self):
        """
        Verifieer tegen de werkelijke Bitget waarde uit de screenshot:
        Entry: $70,978.8 | Leverage: 2x | Isolated | Long
        Bitget toont: $35,810.90
        Acceptabele afwijking: < 1% (voor een waarschuwingssysteem)
        """
        liq = calculate_liquidation_price(70978.8, 2, "long")
        bitget_actual = 35810.90
        deviation_pct = abs(liq - bitget_actual) / bitget_actual * 100
        assert deviation_pct < 1.0, (
            f"Liquidatieprijs {liq} wijkt {deviation_pct:.2f}% af van "
            f"Bitget's werkelijke waarde {bitget_actual}"
        )

    def test_2x_long_approx_half_entry(self):
        """Bij 2x leverage is de liquidatieprijs ongeveer 50% van de entry."""
        liq = calculate_liquidation_price(80000.0, 2, "long")
        # Moet tussen 48% en 52% van entry liggen
        assert 0.48 * 80000.0 < liq < 0.52 * 80000.0

    def test_10x_long_approx_90pct_drop(self):
        """Bij 10x leverage is de liquidatieprijs ongeveer 90% van de entry (10% daling)."""
        liq = calculate_liquidation_price(80000.0, 10, "long")
        # Moet tussen 8% en 12% van entry liggen
        assert 0.88 * 80000.0 < liq < 0.95 * 80000.0
