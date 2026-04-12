# tests/test_indicators.py
import sys, os, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.indicators.rsi import calculate_rsi, check_rsi_signal
from strategies.indicators.ema import calculate_ema, check_ema_cross_signal
from strategies.indicators.macd import calculate_macd, check_macd_signal
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
