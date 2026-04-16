# tests/test_indicators.py
import sys, os, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.indicators.rsi import calculate_rsi, check_rsi_signal
from strategies.indicators.ema import calculate_ema, check_ema_cross_signal
from strategies.indicators.macd import calculate_macd, check_macd_signal
from strategies.indicators.bollinger import (
    calculate_bollinger_bands,
    check_bollinger_signal,
)
from strategies.indicators.parabolic_sar import (
    calculate_parabolic_sar,
    check_parabolic_sar_signal,
)
from strategies.indicators.supertrend import (
    calculate_supertrend,
    check_supertrend_signal,
)
from strategies.indicators.market_structure import check_market_structure_signal
from strategies.indicators.support_resistance import (
    check_support_resistance_signal,
    find_support_resistance,
)
from strategies.indicators.qfl import check_qfl_signal, find_qfl_bases
from config.models import BotConfig
from pydantic import ValidationError


class TestRSI:
    def test_flat_market(self):
        """Constante prijzen → geen verliezen → avg_loss=0 → fillna(100) → RSI=100."""
        assert calculate_rsi([100.0]*20, 14) == 100.0

    def test_mixed_market_between_0_and_100(self):
        """Gemengde markt geeft RSI tussen 0 en 100."""
        # Afwisselend stijgen en dalen
        closes = [100.0 + (5 if i % 2 == 0 else -3) for i in range(20)]
        rsi = calculate_rsi(closes, 14)
        assert 0.0 < rsi < 100.0

    def test_only_gains(self):
        assert calculate_rsi([float(i) for i in range(1,25)], 14) == 100.0

    def test_only_losses(self):
        assert calculate_rsi([float(25-i) for i in range(25)], 14) == 0.0

    def test_between_0_and_100(self):
        import random; random.seed(42)
        closes = [100.0 + random.uniform(-5,5) for _ in range(50)]
        assert 0.0 <= calculate_rsi(closes, 14) <= 100.0

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="at least"):
            calculate_rsi([100.0]*5, 14)

    def test_signal_matches_value(self):
        closes = [100.0] + [100.0 - i*2 for i in range(1,25)]
        rsi = calculate_rsi(closes, 14)
        assert check_rsi_signal(closes, 14, "below_35") == (rsi < 35)

    def test_unknown_threshold_raises(self):
        with pytest.raises(ValueError, match="Unknown RSI threshold"):
            check_rsi_signal([float(i) for i in range(1,25)], 14, "invalid")

    def test_value_out_of_range_raises(self):
        with pytest.raises(ValueError, match="between 1 and 99"):
            check_rsi_signal([float(i) for i in range(1, 25)], 14, "below_100")

    def test_above_condition(self):
        """Rising prices → RSI high → above_50 should be True."""
        closes = [float(i) for i in range(1, 30)]
        assert check_rsi_signal(closes, 14, "above_50") is True

    def test_below_condition(self):
        """Falling prices → RSI low → below_50 should be True."""
        closes = [float(30 - i) for i in range(30)]
        assert check_rsi_signal(closes, 14, "below_50") is True

    def test_cross_above_fires_on_transition(self):
        """Down-trend → reversal: RSI should cross up through some low threshold."""
        down = [float(30 - i) for i in range(25)]  # RSI near 0
        up = down + [float(6 + i * 4) for i in range(10)]  # sharp rebound
        # At some point during the rebound, RSI crosses above 30.
        # Walk forward and assert the cross fires on exactly one tick.
        crosses = 0
        for end in range(16, len(up) + 1):
            window = up[:end]
            try:
                if check_rsi_signal(window, 14, "cross_above_30"):
                    crosses += 1
            except ValueError:
                pass
        assert crosses >= 1, "cross_above_30 should fire at least once on the rebound"

    def test_cross_below_fires_on_transition(self):
        """Up-trend → reversal: RSI should cross down through a high threshold."""
        up = [float(i) for i in range(25)]  # RSI near 100
        down = up + [float(24 - i * 2) for i in range(15)]  # sharp drop
        crosses = 0
        for end in range(16, len(down) + 1):
            window = down[:end]
            try:
                if check_rsi_signal(window, 14, "cross_below_70"):
                    crosses += 1
            except ValueError:
                pass
        assert crosses >= 1, "cross_below_70 should fire at least once on the drop"

    def test_cross_requires_extra_datapoint(self):
        """cross_* conditions need period + 2 closes, not just period + 1."""
        closes = [float(i) for i in range(1, 16)]  # period + 1 = 15 points
        with pytest.raises(ValueError, match="require at least"):
            check_rsi_signal(closes, 14, "cross_above_50")

    def test_cross_above_no_transition_returns_false(self):
        """RSI already above threshold, staying there → no cross."""
        closes = [float(i) for i in range(1, 30)]  # rising, RSI high throughout
        # Between the last two ticks RSI stays well above 50, so cross_above_50
        # must NOT fire.
        assert check_rsi_signal(closes, 14, "cross_above_50") is False


class TestEMA:
    def test_constant_series(self):
        assert calculate_ema([100.0]*30, 9) == 100.0

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError):
            calculate_ema([100.0]*5, 9)

    def test_cross_requires_3x_slow(self):
        with pytest.raises(ValueError, match="3 \\* slow"):
            check_ema_cross_signal([100.0]*30, fast=9, slow=21, signal="bullish")

    def test_unknown_signal_raises(self):
        with pytest.raises(ValueError, match="Unknown EMA signal"):
            check_ema_cross_signal([100.0]*70, signal="sideways")


class TestMACD:
    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="3 \\* slow"):
            calculate_macd([100.0]*50)

    def test_returns_required_keys(self):
        result = calculate_macd([100.0 + i*0.1 for i in range(80)])
        assert all(k in result for k in ["macd","signal","histogram"])

    def test_histogram_equals_macd_minus_signal(self):
        result = calculate_macd([100.0 + i*0.1 for i in range(80)])
        assert abs(result["histogram"] - (result["macd"] - result["signal"])) < 0.0001

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unknown MACD condition"):
            check_macd_signal([100.0]*80, "invalid")

    def test_constant_histogram_near_zero(self):
        result = calculate_macd([100.0]*80)
        assert abs(result["histogram"]) < 0.0001


class TestBollinger:
    def test_happy_path_below_lower(self):
        """20 flat candles then a sharp drop → price below the lower band."""
        closes = [100.0] * 20 + [80.0]
        assert check_bollinger_signal(
            closes, period=20, multiplier=2.0, condition="price_below_lower"
        ) is True

    def test_happy_path_above_upper(self):
        """20 flat candles then a sharp spike → price above the upper band."""
        closes = [100.0] * 20 + [120.0]
        assert check_bollinger_signal(
            closes, period=20, multiplier=2.0, condition="price_above_upper"
        ) is True

    def test_no_signal_flat_market(self):
        """All closes identical → std=0 → upper=lower=middle. Price equal to
        the band is not strictly below/above so no signal fires."""
        closes = [100.0] * 22
        assert check_bollinger_signal(
            closes, period=20, condition="price_below_lower"
        ) is False
        assert check_bollinger_signal(
            closes, period=20, condition="price_above_upper"
        ) is False

    def test_squeeze_detects_low_volatility(self):
        closes = [100.0] * 20 + [100.1]  # tiny movement = very tight bands
        assert check_bollinger_signal(
            closes, period=20, condition="squeeze", squeeze_threshold=0.05
        ) is True

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="at least"):
            calculate_bollinger_bands([100.0] * 5, period=20)

    def test_unknown_condition_returns_false(self):
        assert check_bollinger_signal([100.0] * 22, condition="invalid") is False


class TestParabolicSAR:
    def test_bullish_trend_detected(self):
        """Steadily rising prices → SAR stays below price → bullish."""
        closes = [float(100 + i) for i in range(30)]
        assert check_parabolic_sar_signal(closes, condition="bullish") is True
        assert check_parabolic_sar_signal(closes, condition="bearish") is False

    def test_bearish_trend_detected(self):
        closes = [float(130 - i) for i in range(30)]
        assert check_parabolic_sar_signal(closes, condition="bearish") is True
        assert check_parabolic_sar_signal(closes, condition="bullish") is False

    def test_bullish_flip_on_reversal(self):
        """Down-trend then sharp reversal should eventually produce a flip."""
        down = [float(130 - i) for i in range(20)]
        up = down + [float(110 + i * 3) for i in range(15)]
        flips = 0
        for end in range(11, len(up) + 1):
            try:
                if check_parabolic_sar_signal(up[:end], condition="bullish_flip"):
                    flips += 1
            except ValueError:
                pass
        assert flips >= 1

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="at least 10"):
            calculate_parabolic_sar([100.0, 101.0, 102.0])

    def test_unknown_condition_returns_false(self):
        data = [float(i) for i in range(15)]
        assert check_parabolic_sar_signal(data, condition="invalid") is False


class TestSupertrend:
    @staticmethod
    def _ohlc(closes, spread=0.5):
        """Build synthetic OHLC from a closes list with a fixed spread."""
        highs = [c + spread for c in closes]
        lows  = [c - spread for c in closes]
        return highs, lows, closes

    def test_bullish_trend_detected(self):
        closes = [float(100 + i) for i in range(30)]
        h, lo, c = self._ohlc(closes)
        assert check_supertrend_signal(h, lo, c, condition="bullish") is True
        assert check_supertrend_signal(h, lo, c, condition="bearish") is False

    def test_bearish_trend_detected(self):
        closes = [float(130 - i) for i in range(30)]
        h, lo, c = self._ohlc(closes)
        assert check_supertrend_signal(h, lo, c, condition="bearish") is True

    def test_bullish_flip_on_reversal(self):
        down = [float(130 - i) for i in range(20)]
        up = down + [float(110 + i * 3) for i in range(15)]
        h, lo, c = self._ohlc(up)
        flips = 0
        for end in range(12, len(up) + 1):
            try:
                if check_supertrend_signal(
                    h[:end], lo[:end], c[:end], condition="bullish_flip"
                ):
                    flips += 1
            except ValueError:
                pass
        assert flips >= 1

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError, match="identical length"):
            calculate_supertrend([1.0, 2.0, 3.0], [0.5, 1.5], [1.0, 2.0, 3.0])

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="at least"):
            calculate_supertrend([1.0, 2.0], [0.5, 1.5], [1.0, 2.0], atr_period=10)

    def test_unknown_condition_raises(self):
        closes = [float(100 + i) for i in range(30)]
        h, lo, c = self._ohlc(closes)
        with pytest.raises(ValueError, match="Unknown Supertrend"):
            check_supertrend_signal(h, lo, c, condition="invalid")


class TestMarketStructure:
    @staticmethod
    def _with_swings():
        """Build a 40-close series with two clear swing lows (HL pattern)
        and two swing highs so the trend is a textbook uptrend:
            low1 @ idx 5 = 98, high1 @ idx 12 = 108,
            low2 @ idx 19 = 100, high2 @ idx 26 = 112,
            final close = 113 (BOS above high2).
        """
        closes = [100.0] * 40
        # low1 at 5
        closes[3:8] = [101.0, 99.5, 98.0, 99.5, 101.0]
        # high1 at 12
        closes[10:15] = [106.0, 107.0, 108.0, 107.0, 106.0]
        # low2 at 19 (higher than low1)
        closes[17:22] = [103.0, 101.5, 100.0, 101.5, 103.0]
        # high2 at 26 (higher than high1)
        closes[24:29] = [110.0, 111.0, 112.0, 111.0, 110.0]
        # final: close above high2 to trigger BOS
        closes[-1] = 113.0
        return closes

    def test_bullish_bos_detected(self):
        closes = self._with_swings()
        assert check_market_structure_signal(closes, lookback=2, condition="bullish_bos") is True

    def test_higher_low_detected(self):
        closes = self._with_swings()
        assert check_market_structure_signal(closes, lookback=2, condition="higher_low") is True

    def test_bullish_structure_detected(self):
        closes = self._with_swings()
        assert check_market_structure_signal(
            closes, lookback=2, condition="bullish_structure"
        ) is True

    def test_no_signal_on_flat(self):
        closes = [100.0] * 40
        for cond in ("bullish_bos", "bearish_bos", "higher_low", "lower_high",
                     "bullish_structure", "bearish_structure"):
            assert check_market_structure_signal(closes, lookback=2, condition=cond) is False

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="at least"):
            check_market_structure_signal([100.0] * 10, lookback=3, condition="bullish_bos")

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unknown Market Structure"):
            check_market_structure_signal([100.0] * 40, lookback=3, condition="invalid")


class TestSupportResistance:
    @staticmethod
    def _with_levels(final: float = 100.0):
        """Build OHLC series with swing lows/highs for pivot detection."""
        n = 60
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        # Pivot high at bar 10 (high=110, confirmed by lb=3, rb=3)
        for j in range(7, 14):
            highs[j] = 105.0 + (3 - abs(j - 10)) * 2
        highs[10] = 110.0
        closes[10] = 109.0
        # Pivot low at bar 20
        for j in range(17, 24):
            lows[j] = 95.0 - (3 - abs(j - 20)) * 2
        lows[20] = 90.0
        closes[20] = 91.0
        closes[-1] = final
        return highs, lows, closes

    def test_single_confirmed_pivot(self):
        highs, lows, closes = self._with_levels(final=100.0)
        sup, res = find_support_resistance(highs, lows, closes,
                                           left_bars=3, right_bars=3)
        assert len(res) >= 1 or len(sup) >= 1

    def test_broken_pivot_excluded(self):
        highs, lows, closes = self._with_levels(final=120.0)
        # Close at 120 breaks the resistance at 110
        _, res = find_support_resistance(highs, lows, closes,
                                         left_bars=3, right_bars=3)
        assert all(r != 110.0 for r in res)

    def test_stair_step_levels(self):
        # Two pivot highs at nearly the same price — both should
        # be kept as distinct levels (no clustering).
        n = 60
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        highs[10] = 110.0; highs[30] = 110.5
        for j in range(7, 14): highs[j] = max(highs[j], 105.0)
        for j in range(27, 34): highs[j] = max(highs[j], 105.0)
        sup, res = find_support_resistance(highs, lows, closes,
                                           left_bars=3, right_bars=3)
        assert len(res) == 2

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="at least"):
            check_support_resistance_signal(
                [100.0]*5, [100.0]*5, [100.0]*5,
                left_bars=3, right_bars=3)

    def test_unknown_condition_returns_false(self):
        n = 60
        assert check_support_resistance_signal(
            [100.0]*n, [100.0]*n, [100.0]*n,
            left_bars=3, right_bars=3,
            condition="invalid") is False


class TestQFL:
    @staticmethod
    def _with_bases(final: float = 100.0):
        """80 closes with two validated QFL bases:
          - Swing low 94 at index 10, bounce to 100.5 within 5 candles
            (6.9% rebound > default 3% crack threshold)
          - Swing low 95 at index 30, bounce to 101.0 (6.3%)
        Everything else sits flat at 100.0."""
        closes = [100.0] * 80
        closes[8:14]  = [98.0, 96.0, 94.0, 96.0, 98.5, 100.5]
        closes[28:34] = [99.0, 97.0, 95.0, 97.0, 99.0, 101.0]
        closes[-1] = final
        return closes

    def test_bases_detected(self):
        closes = self._with_bases()
        bases = find_qfl_bases(closes, lookback=3, crack_pct=3.0, base_candles=5)
        assert bases == [94.0, 95.0]

    def test_below_base_detected(self):
        closes = self._with_bases(final=93.0)  # 1% below the 94 base
        assert check_qfl_signal(
            closes, lookback=3, condition="below_base"
        ) is True

    def test_near_base_detected(self):
        closes = self._with_bases(final=94.5)  # within 1% above the 94 base
        assert check_qfl_signal(
            closes, lookback=3, condition="near_base"
        ) is True

    def test_base_retest_detected(self):
        closes = self._with_bases(final=94.05)  # within 0.1% of the 94 base
        assert check_qfl_signal(
            closes, lookback=3, condition="base_retest"
        ) is True

    def test_no_signal_far_above(self):
        closes = self._with_bases(final=100.0)
        for cond in ("below_base", "near_base", "base_retest"):
            assert check_qfl_signal(closes, lookback=3, condition=cond) is False

    def test_crack_threshold_filters_weak_rebounds(self):
        """A swing low without enough rebound should NOT be promoted to a base."""
        closes = [100.0] * 80
        # Swing low 99 at index 10, weak 0.3% bounce → fails 3% crack
        closes[8:14] = [99.5, 99.2, 99.0, 99.1, 99.2, 99.3]
        bases = find_qfl_bases(closes, lookback=3, crack_pct=3.0, base_candles=5)
        assert bases == []

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="at least"):
            check_qfl_signal([100.0] * 20, lookback=3, condition="below_base")

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unknown QFL"):
            check_qfl_signal([100.0] * 80, lookback=3, condition="invalid")


class TestConfigValidation:
    def _base(self, **kw):
        return dict(name="test", mode="paper", exchange="bitget",
                    dca={"base_order_size":0.001},
                    take_profit={"target_pct":3.0}, **kw)

    def test_valid_config(self):
        cfg = BotConfig(**self._base())
        assert cfg.name == "test"

    def test_invalid_stop_loss_type(self):
        with pytest.raises(ValidationError):
            BotConfig(**self._base(stop_loss={"type":"traling","pct":5.0}))

    def test_invalid_notify_on(self):
        with pytest.raises(ValidationError):
            BotConfig(**self._base(telegram={"notify_on":["tp-hit"]}))

    def test_invalid_contract_type(self):
        with pytest.raises(ValidationError):
            BotConfig(**self._base(contract_type="linear_perpetual"))


# ── New parameter expansion tests ─────────────────────────────────────────────

class TestBollingerMA:
    def test_wma_produces_different_result(self):
        data = [float(i % 10 + 90) for i in range(30)]
        sma = calculate_bollinger_bands(data, period=20, multiplier=2.0)
        wma = calculate_bollinger_bands(data, period=20, multiplier=2.0, ma_type="WMA")
        assert sma["middle"] != wma["middle"]

    def test_ema_produces_different_result(self):
        data = [float(i % 10 + 90) for i in range(30)]
        sma = calculate_bollinger_bands(data, period=20, multiplier=2.0)
        ema = calculate_bollinger_bands(data, period=20, multiplier=2.0, ma_type="EMA")
        assert sma["middle"] != ema["middle"]

    def test_crossing_up(self):
        data = [100.0] * 25 + [80.0, 120.0]
        assert check_bollinger_signal(data, period=20, condition="price_crossing_up", value="upper")


class TestPSARCrossing:
    def test_price_crossing_up_alias(self):
        data = list(range(10, 25)) + list(range(25, 10, -1)) + list(range(10, 30))
        result = check_parabolic_sar_signal(data, condition="price_crossing_up")
        # Should not raise; just verify it runs
        assert isinstance(result, bool)


class TestSupertrendFlip:
    def test_from_down_to_up_alias(self):
        highs = [float(i + 1) for i in range(20)]
        lows = [float(i - 1) for i in range(20)]
        closes = [float(i) for i in range(20)]
        result = check_supertrend_signal(
            highs, lows, closes, atr_period=5,
            condition="from_down_to_up")
        assert isinstance(result, bool)


class TestSRLeftRightBars:
    def test_left_right_bars_detection(self):
        n = 161
        highs = [100.0] * n; lows = [100.0] * n; closes = [100.0] * n
        lows[80] = 90.0; closes[80] = 91.0
        result = check_support_resistance_signal(
            highs, lows, closes, left_bars=15, right_bars=15,
            condition="near_support", value="support",
            proximity_pct=2.0)
        assert isinstance(result, bool)

    def test_crossing_condition(self):
        n = 101
        highs = [100.0] * n; lows = [100.0] * n; closes = [100.0] * n
        lows[50] = 90.0; closes[50] = 91.0; closes[-1] = 88.0
        result = check_support_resistance_signal(
            highs, lows, closes, left_bars=10, right_bars=10,
            condition="price_crossing_down", value="support",
            proximity_pct=2.0)
        assert isinstance(result, bool)


class TestSRDetailed:
    def test_detailed_returns_pivot_and_break_index(self):
        from strategies.indicators.support_resistance import (
            find_support_resistance_detailed,
        )
        n = 40
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        # Pivot high at bar 10
        for j in range(7, 14):
            highs[j] = 105.0
        highs[10] = 112.0
        closes[10] = 111.0
        # Break resistance at bar 30
        closes[30] = 113.0
        sup, res = find_support_resistance_detailed(
            highs, lows, closes, left_bars=3, right_bars=3)
        assert len(res) >= 1
        r = res[0]
        assert r["pivot_index"] == 10
        assert r["price"] == 112.0
        assert r["break_index"] == 30

    def test_unbroken_level_has_null_break(self):
        from strategies.indicators.support_resistance import (
            find_support_resistance_detailed,
        )
        n = 40
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        highs[10] = 112.0
        for j in range(7, 14):
            highs[j] = max(highs[j], 105.0)
        sup, res = find_support_resistance_detailed(
            highs, lows, closes, left_bars=3, right_bars=3)
        assert len(res) >= 1
        assert res[0]["break_index"] is None


class TestSRBreakDetection:
    def test_resistance_broken_by_close(self):
        from strategies.indicators.support_resistance import (
            find_support_resistance_detailed,
        )
        n = 50
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        # Pivot high at bar 10 with price 110
        for j in range(7, 14):
            highs[j] = 105.0
        highs[10] = 110.0; closes[10] = 109.0
        # Close above 110 at bar 30
        closes[30] = 111.0
        _, res = find_support_resistance_detailed(
            highs, lows, closes, left_bars=3, right_bars=3)
        assert len(res) >= 1
        assert res[0]["break_index"] == 30

    def test_support_broken_by_close(self):
        from strategies.indicators.support_resistance import (
            find_support_resistance_detailed,
        )
        n = 50
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        # Pivot low at bar 15 with price 90
        for j in range(12, 19):
            lows[j] = 95.0
        lows[15] = 90.0; closes[15] = 91.0
        # Close below 90 at bar 35
        closes[35] = 89.0
        sup, _ = find_support_resistance_detailed(
            highs, lows, closes, left_bars=3, right_bars=3)
        assert len(sup) >= 1
        assert sup[0]["break_index"] == 35

    def test_unbroken_remains_active(self):
        from strategies.indicators.support_resistance import (
            find_support_resistance_detailed,
        )
        n = 50
        highs = [100.0] * n
        lows = [100.0] * n
        closes = [100.0] * n
        highs[10] = 110.0
        for j in range(7, 14):
            highs[j] = max(highs[j], 105.0)
        # Never breach 110 — all closes stay at 100
        _, res = find_support_resistance_detailed(
            highs, lows, closes, left_bars=3, right_bars=3)
        assert len(res) >= 1
        assert res[0]["break_index"] is None
