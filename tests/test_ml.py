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


# ── market_regime: malformed / partial candles ──────────────────────────────

class TestMarketRegimeMalformed:
    """Previous crash path: compute_regime_features() dereferenced
    c["close"] / c["volume"] directly, so any malformed dict would kill
    the whole pipeline with a KeyError. We now return an empty frame and
    let detect_current_regime fall back to "unknown" cleanly."""

    def test_malformed_candles_returns_empty(self):
        result = market_regime.compute_regime_features([{}])
        assert result.empty

    def test_missing_volume_key_handled(self):
        result = market_regime.compute_regime_features([{"close": 100}])
        assert result.empty

    def test_missing_close_key_handled(self):
        result = market_regime.compute_regime_features([{"volume": 10}])
        assert result.empty

    def test_none_entry_handled(self):
        """Non-dict entries should also land in the fail-soft path."""
        result = market_regime.compute_regime_features([None, None])
        assert result.empty


# ── safe_load_model / atomic_dump_model ─────────────────────────────────────

class TestModelIO:
    """Thin regression tests for the pickle-RCE guard and the atomic
    dump helper. These are smoke-level — joblib already has its own
    extensive suite, we just need to pin the safety checks that Reverto
    layers on top of it."""

    def test_safe_load_rejects_path_outside_root(self, tmp_path):
        """A path that resolves outside ``allowed_root`` must be refused
        even when the file exists — mitigates symlink / absolute-path
        tricks that would otherwise hand joblib.load arbitrary files."""
        from ml.model_io import safe_load_model

        outside = tmp_path / "escape.pkl"
        outside.write_bytes(b"not-a-real-pickle")

        # Use a different tmp dir as the allowlist root.
        root = tmp_path / "models"
        root.mkdir()

        assert safe_load_model(outside, allowed_root=root) is None

    def test_safe_load_missing_file_returns_none(self, tmp_path):
        from ml.model_io import safe_load_model
        assert safe_load_model(tmp_path / "nope.pkl", allowed_root=tmp_path) is None

    def test_atomic_dump_roundtrip(self, tmp_path):
        """Write + read back a trivial picklable payload and verify the
        resulting file is neither zero-sized nor leaves a .tmp sibling."""
        from ml.model_io import atomic_dump_model, safe_load_model

        target = tmp_path / "x.pkl"
        atomic_dump_model({"hello": 1}, target)

        assert target.exists() and target.stat().st_size > 0
        assert not (tmp_path / "x.pkl.tmp").exists()

        loaded = safe_load_model(target, allowed_root=tmp_path)
        assert loaded == {"hello": 1}

    def test_atomic_dump_cleans_up_on_failure(self, tmp_path, monkeypatch):
        """When joblib.dump raises, the .tmp file must be removed so the
        next run doesn't trip over a partial write."""
        from ml import model_io

        def _boom(obj, path):
            # Pretend to start a dump so the .tmp file exists, then raise.
            path.write_bytes(b"partial")
            raise RuntimeError("simulated disk full")

        monkeypatch.setattr(model_io, "os", model_io.os)  # no-op, keeps import visible
        # Patch joblib.dump inside the helper's namespace. atomic_dump_model
        # imports joblib lazily, so we patch the joblib module directly.
        import joblib
        monkeypatch.setattr(joblib, "dump", _boom)

        target = tmp_path / "y.pkl"
        with pytest.raises(RuntimeError, match="simulated disk full"):
            model_io.atomic_dump_model({"hi": 2}, target)

        assert not target.exists()
        assert not (tmp_path / "y.pkl.tmp").exists()
