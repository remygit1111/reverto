"""Tests for the ml/ package — features, market regime, entry filter.

The tests deliberately avoid importing xgboost / optuna so the suite
stays runnable on a bare install that skipped the ML extras.
"""

import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml import entry_filter, features as ml_features, market_regime  # noqa: E402


def _synthetic_candles(n: int = 120, *, base: float = 80000.0) -> list[dict]:
    """Build a deterministic OHLCV series long enough for every feature
    (MACD needs 3 * 26 = 78 bars). Gentle oscillation keeps the series
    non-degenerate so rolling std / skew don't collapse to zero."""
    out = []
    for i in range(n):
        close = base + (i * 5) + (10 if i % 3 else -10)
        out.append({
            "open":   close - 2,
            "high":   close + 8,
            "low":    close - 8,
            "close":  close,
            "volume": 1000.0 + (i % 7) * 50,
        })
    return out


# ── compute_features ────────────────────────────────────────────────────────

class TestComputeFeatures:

    def test_returns_dict_with_expected_keys(self):
        feats = ml_features.compute_features(_synthetic_candles(120))
        assert isinstance(feats, dict)
        # Spot-check one feature from each indicator family.
        for key in ("rsi", "macd_histogram", "bb_width", "atr_14",
                    "price_vs_sma20", "volume_ratio", "body_size"):
            assert key in feats, f"expected key {key!r} in features"

    def test_insufficient_data_returns_empty_dict(self):
        # 50 bars is below MIN_CANDLES (78) — should bail out cleanly.
        assert ml_features.compute_features(_synthetic_candles(50)) == {}
        assert ml_features.compute_features([]) == {}

    def test_all_values_are_finite_floats(self):
        feats = ml_features.compute_features(_synthetic_candles(120))
        assert feats, "expected non-empty feature dict"
        for k, v in feats.items():
            assert isinstance(v, float), f"{k} is {type(v).__name__}, not float"
            assert math.isfinite(v), f"{k} is non-finite ({v})"

    def test_deal_context_adds_time_features(self):
        """Passing a deal dict with opened_at adds the sin/cos time keys."""
        candles = _synthetic_candles(120)
        deal = {"opened_at": 1_700_000_000, "dca_count": 2}
        feats = ml_features.compute_features(candles, deal)
        for key in ("hour_sin", "hour_cos", "weekday_sin",
                    "weekday_cos", "dca_count"):
            assert key in feats
        assert feats["dca_count"] == 2.0

    def test_safe_float_handles_nan_and_inf(self):
        """Internal guard: NaN / Inf collapse to the fallback default."""
        assert ml_features._safe_float(float("nan")) == 0.0
        assert ml_features._safe_float(float("inf")) == 0.0
        assert ml_features._safe_float(float("-inf")) == 0.0
        assert ml_features._safe_float("not a number") == 0.0
        assert ml_features._safe_float(1.5) == 1.5

    def test_features_to_dataframe_stable_columns(self):
        """Varying feature sets across deals produce a ragged-safe frame."""
        rows = [
            {"a": 1.0, "b": 2.0},
            {"a": 3.0},  # missing b
            {},          # empty row — skipped from column-anchor but filled as zeros
        ]
        df = ml_features.features_to_dataframe(rows)
        assert list(df.columns) == ["a", "b"]
        assert df.iloc[1]["b"] == 0.0


# ── EntryFilter ─────────────────────────────────────────────────────────────

class TestEntryFilter:

    def test_no_model_fails_open(self, monkeypatch, tmp_path):
        """Missing entry_filter.pkl → predict returns (1.0, True)."""
        monkeypatch.setattr(entry_filter, "MODEL_PATH", tmp_path)
        flt = entry_filter.EntryFilter(threshold=0.55)
        assert flt.is_available() is False
        prob, enter = flt.predict({"rsi": 30.0})
        assert prob == 1.0
        assert enter is True

    def test_predict_with_dummy_model(self, monkeypatch, tmp_path):
        """A stubbed predict_proba surfaces through to the caller."""
        class _Stub:
            feature_names_in_ = ("rsi", "macd_histogram")

            def predict_proba(self, X):
                # Return high positive probability regardless of input.
                return [[0.2, 0.8]]

        flt = entry_filter.EntryFilter(threshold=0.55)
        flt.model = _Stub()
        assert flt.is_available() is True
        prob, enter = flt.predict({"rsi": 30.0, "macd_histogram": 0.5})
        assert prob == pytest.approx(0.8)
        assert enter is True

    def test_predict_exception_fails_open(self, monkeypatch):
        """If predict_proba raises, contract is still (1.0, True)."""
        class _Broken:
            feature_names_in_ = ("rsi",)

            def predict_proba(self, X):
                raise RuntimeError("boom")

        flt = entry_filter.EntryFilter(threshold=0.55)
        flt.model = _Broken()
        prob, enter = flt.predict({"rsi": 30.0})
        assert prob == 1.0
        assert enter is True


# ── market_regime ───────────────────────────────────────────────────────────

class TestMarketRegime:

    def test_detect_without_model_returns_unknown(self, monkeypatch, tmp_path):
        """No regime_model.pkl → "unknown" instead of a crash."""
        monkeypatch.setattr(market_regime, "MODEL_PATH", tmp_path)
        candles = _synthetic_candles(60)
        assert market_regime.detect_current_regime(candles) == "unknown"

    def test_compute_regime_features_shape(self):
        """Rolling windows drop the first ~20 rows; the rest is dense."""
        candles = _synthetic_candles(100)
        feats = market_regime.compute_regime_features(candles)
        assert not feats.empty
        assert list(feats.columns) == [
            "volatility", "trend_20", "trend_5", "vol_ratio", "return_skew",
        ]
        # No NaN survives the .dropna() at the end of the function.
        assert not feats.isna().any().any()

    def test_compute_regime_features_empty_input(self):
        assert market_regime.compute_regime_features([]).empty

    def test_train_and_detect_roundtrip(self, monkeypatch, tmp_path):
        """Full train → persist → detect cycle, no "unknown" fallback."""
        monkeypatch.setattr(market_regime, "MODEL_PATH", tmp_path)
        candles = _synthetic_candles(200)
        summary = market_regime.train_regime_model(candles, n_regimes=3)
        assert summary["n_regimes"] == 3
        assert Path(tmp_path, "regime_model.pkl").exists()
        assert Path(tmp_path, "regime_scaler.pkl").exists()
        regime = market_regime.detect_current_regime(candles)
        assert regime in market_regime.REGIMES.values()

    def test_train_on_empty_candles_raises(self):
        with pytest.raises(ValueError, match="Not enough candles"):
            market_regime.train_regime_model([])
