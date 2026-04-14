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

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unknown Bollinger"):
            check_bollinger_signal([100.0] * 22, condition="invalid")


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

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unknown Parabolic SAR"):
            check_parabolic_sar_signal([float(i) for i in range(15)], condition="invalid")


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
        """Build a 60-close series with two swing lows around 98 and
        two swing highs around 108, letting us probe near/below/above
        behaviour by changing the final close."""
        closes = [100.0] * 60
        closes[3:8] = [101.0, 99.5, 98.0, 99.5, 101.0]      # low @ 5
        closes[10:15] = [106.0, 107.0, 108.0, 107.0, 106.0]  # high @ 12
        closes[17:22] = [101.0, 99.5, 98.2, 99.5, 101.0]    # low @ 19
        closes[24:29] = [106.0, 107.0, 108.1, 107.0, 106.0]  # high @ 26
        closes[-1] = final
        return closes

    def test_near_support_detected(self):
        closes = self._with_levels(final=98.3)  # within 1% of ~98
        assert check_support_resistance_signal(
            closes, lookback=2, proximity_pct=1.0, condition="near_support"
        ) is True

    def test_near_resistance_detected(self):
        closes = self._with_levels(final=108.0)
        assert check_support_resistance_signal(
            closes, lookback=2, proximity_pct=1.0, condition="near_resistance"
        ) is True

    def test_above_resistance_detected(self):
        closes = self._with_levels(final=115.0)
        assert check_support_resistance_signal(
            closes, lookback=2, condition="above_resistance"
        ) is True

    def test_below_support_detected(self):
        closes = self._with_levels(final=90.0)
        assert check_support_resistance_signal(
            closes, lookback=2, condition="below_support"
        ) is True

    def test_clustering_collapses_close_levels(self):
        # Two isolated peaks in a flat floor — both within 1% of each
        # other, no swing lows (flat equals aren't strict minima). The
        # clustering step must collapse the pair into a single level.
        closes = [100.0] * 60
        closes[10] = 108.0
        closes[30] = 108.5  # within 0.5% of 108.0 → same cluster
        support, resistance = find_support_resistance(
            closes, lookback=2, tolerance_pct=1.0
        )
        assert support == []
        assert len(resistance) == 1

    def test_insufficient_data_raises(self):
        with pytest.raises(ValueError, match="at least"):
            check_support_resistance_signal(
                [100.0] * 20, lookback=3, condition="near_support"
            )

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unknown Support/Resistance"):
            check_support_resistance_signal(
                [100.0] * 60, lookback=2, condition="invalid"
            )


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
