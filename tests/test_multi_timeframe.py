# tests/test_multi_timeframe.py
# Tests for the multi-timeframe plumbing in IndicatorEngine and
# BacktestEngine. Focus on the NEW contract:
#   - check_entry_signal(closes_per_tf, bot_timeframe)
#   - fail-closed when a required timeframe is missing
#   - BacktestEngine raises ValueError on missing tf data
#   - _ohlc_up_to() pointer-walk yields correct slices per tf

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from backtest.backtest_engine import BacktestCandle, BacktestEngine
from strategies.indicator_engine import IndicatorEngine


def _cfg(indicators=None, bot_tf="1h"):
    cfg = MagicMock()
    cfg.name = "mtf-test"
    cfg.pair = "BTC/USD"
    cfg.timeframe = bot_tf
    cfg.leverage.size = 1
    cfg.leverage.enabled = False
    cfg.take_profit.target_pct = 3.0
    cfg.take_profit.indicator_confirm = None
    cfg.stop_loss.type = "fixed"
    cfg.stop_loss.pct = 6.0
    cfg.dca.max_orders = 5
    cfg.dca.order_spacing_pct = 2.5
    cfg.dca.multiplier = 1.0
    cfg.dca.base_order_size = 0.001
    cfg.dca.taker_fee = 0.0006
    cfg.entry.indicators = indicators or []
    return cfg


def _rsi_indicator(tf=None, threshold="below_35"):
    ind = MagicMock()
    ind.type = "RSI"
    ind.timeframe = tf
    ind.period = 14
    ind.threshold = threshold
    ind.fast = None
    ind.slow = None
    ind.signal = None
    ind.condition = None
    return ind


def _make_candles(prices, start_ms=1_700_000_000_000, step_ms=3_600_000):
    return [
        BacktestCandle(
            timestamp=start_ms + i * step_ms,
            open=p, high=p * 1.001, low=p * 0.999, close=p, volume=100.0,
        )
        for i, p in enumerate(prices)
    ]


# ── IndicatorEngine: required_timeframes ─────────────────────────────────────

class TestRequiredTimeframes:
    def test_only_bot_tf_when_no_indicators(self):
        eng = IndicatorEngine(_cfg(indicators=[], bot_tf="1h"))
        assert eng.required_timeframes("1h") == {"1h"}

    def test_indicator_uses_bot_tf_by_default(self):
        ind = _rsi_indicator(tf=None)
        eng = IndicatorEngine(_cfg(indicators=[ind], bot_tf="4h"))
        assert eng.required_timeframes("4h") == {"4h"}

    def test_per_indicator_override_adds_tf(self):
        a = _rsi_indicator(tf="15m")
        b = _rsi_indicator(tf="1d")
        eng = IndicatorEngine(_cfg(indicators=[a, b], bot_tf="1h"))
        assert eng.required_timeframes("1h") == {"1h", "15m", "1d"}

    def test_bot_tf_always_present_even_if_all_overrides(self):
        ind = _rsi_indicator(tf="15m")
        eng = IndicatorEngine(_cfg(indicators=[ind], bot_tf="1h"))
        # Bot tf is always in the set even if no indicator uses it,
        # because the engine needs it for snapshot + TP confirmation.
        assert "1h" in eng.required_timeframes("1h")


# ── IndicatorEngine: fail-closed on missing tf ───────────────────────────────

class TestFailClosed:
    def test_missing_tf_blocks_entry(self):
        ind = _rsi_indicator(tf="4h")
        eng = IndicatorEngine(_cfg(indicators=[ind], bot_tf="1h"))
        # closes_per_tf has 1h but not 4h → the RSI indicator cannot
        # be evaluated → entry blocked.
        closes_per_tf = {"1h": [80000.0] * 50}
        assert eng.check_entry_signal(closes_per_tf, "1h") is False

    def test_empty_tf_list_blocks_entry(self):
        ind = _rsi_indicator(tf="1h")
        eng = IndicatorEngine(_cfg(indicators=[ind], bot_tf="1h"))
        # 1h present but empty list → still treated as missing
        assert eng.check_entry_signal({"1h": []}, "1h") is False

    def test_no_indicators_returns_true_without_closes(self):
        eng = IndicatorEngine(_cfg(indicators=[], bot_tf="1h"))
        # No indicators configured → always enter, closes irrelevant
        assert eng.check_entry_signal({}, "1h") is True

    def test_tp_confirmation_fail_closed_on_missing(self):
        cfg = _cfg(indicators=[])
        cfg.take_profit.indicator_confirm = "histogram_positive"
        eng = IndicatorEngine(cfg)
        # No 1h closes → TP stays held (return False = not confirmed)
        assert eng.check_tp_confirmation({}, "1h") is False

    def test_tp_no_confirmation_is_true(self):
        cfg = _cfg(indicators=[])
        cfg.take_profit.indicator_confirm = None
        eng = IndicatorEngine(cfg)
        assert eng.check_tp_confirmation({}, "1h") is True


# ── BacktestEngine: candles_per_tf validation ────────────────────────────────

class TestBacktestCandlesPerTf:
    def test_missing_driving_tf_raises(self):
        cfg = _cfg(bot_tf="1h")
        with pytest.raises(ValueError, match="1h"):
            BacktestEngine(config=cfg, candles_per_tf={"4h": _make_candles([80000.0] * 10)})

    def test_missing_indicator_tf_raises(self):
        cfg = _cfg(indicators=[_rsi_indicator(tf="4h")], bot_tf="1h")
        # Only 1h provided, but the RSI indicator wants 4h
        candles = _make_candles([80000.0] * 100)
        with pytest.raises(ValueError, match="4h"):
            BacktestEngine(config=cfg, candles_per_tf={"1h": candles})

    def test_extra_tf_is_fine(self):
        cfg = _cfg(bot_tf="1h")
        candles = _make_candles([80000.0] * 100)
        engine = BacktestEngine(
            config=cfg,
            candles_per_tf={"1h": candles, "4h": candles},
        )
        assert engine.bot_timeframe == "1h"
        assert set(engine.candles_per_tf.keys()) == {"1h", "4h"}


# ── BacktestEngine: pointer walk ─────────────────────────────────────────────

class TestClosesUpTo:
    def test_pointer_advances_monotonically(self):
        cfg = _cfg(bot_tf="1h")
        h1 = _make_candles([100.0, 101.0, 102.0, 103.0, 104.0],
                           start_ms=1000, step_ms=1000)
        h4 = _make_candles([200.0, 201.0, 202.0],
                           start_ms=1000, step_ms=2000)
        engine = BacktestEngine(
            config=cfg,
            candles_per_tf={"1h": h1, "4h": h4},
        )

        # cur_ts=1500 → 1h[0]=1000 closed, 4h[0]=1000 closed
        closes, _, _ = engine._ohlc_up_to(1500)
        assert closes["1h"] == [100.0]
        assert closes["4h"] == [200.0]

        # cur_ts=2500 → 1h[0,1] closed (ts 1000,2000), 4h[0] closed
        closes, _, _ = engine._ohlc_up_to(2500)
        assert closes["1h"] == [100.0, 101.0]
        assert closes["4h"] == [200.0]

        # cur_ts=3500 → 1h[0..2] closed, 4h[0,1] closed (ts 1000,3000)
        closes, highs, lows = engine._ohlc_up_to(3500)
        assert closes["1h"] == [100.0, 101.0, 102.0]
        assert closes["4h"] == [200.0, 201.0]
        # Highs/lows dicts are also populated — smoke check only
        assert len(highs["1h"]) == len(closes["1h"])
        assert len(lows["4h"]) == len(closes["4h"])

    def test_backtest_runs_with_multi_tf_no_indicators(self):
        """Full run with two timeframes and no indicators — sanity check
        that the driving-candle loop still closes deals on intra-candle
        TP levels when closes_per_tf gets rebuilt every tick."""
        cfg = _cfg(bot_tf="1h")
        # 100 flat candles then a spike high above the 3% TP target
        prices = [80000.0] * 100 + [82500.0] * 20
        h1 = _make_candles(prices)
        h4 = _make_candles(prices)  # dummy second tf
        engine = BacktestEngine(
            config=cfg,
            candles_per_tf={"1h": h1, "4h": h4},
            initial_balance_btc=0.1,
        )
        result = engine.run()
        # Entry happens post-warmup, TP fires when price reaches 82400
        assert result.total_deals >= 1
