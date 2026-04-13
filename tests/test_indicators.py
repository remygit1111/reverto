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
