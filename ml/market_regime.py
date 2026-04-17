"""Market regime detection for Reverto.

Classifies the latest candle window into one of four regimes using a
KMeans clustering of volatility, trend-strength and volume-ratio
features. The labels are purely positional — the clustering is
unsupervised, so after training the caller should inspect the
centroid locations and remap the integer labels to the semantic
names that match their dataset. REGIMES below is a DEFAULT mapping
and should be treated as a best-effort label, not a contract.

Both the scaler and the model are persisted under ml/models/ so a
long-running paper engine can load them without retraining on every
tick. Missing-model calls fall back to "unknown" rather than raising
— regime detection is advisory signal, never a hard gate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "models"

# Positional labels — KMeans gives us clusters 0..N-1 with no inherent
# meaning. Consumers are free to remap these after inspecting centroids.
REGIMES = {
    0: "sideways",
    1: "trending_up",
    2: "trending_down",
    3: "volatile",
}


def compute_regime_features(candles: list) -> pd.DataFrame:
    """Return a DataFrame of rolling regime features, one row per
    candle after the rolling window has filled in (first ~20 rows
    are dropped by ``dropna``).

    Features:
        volatility  — 20-bar std of log-returns (magnitude of swings)
        trend_20    — 20-bar pct_change of close (directional drift)
        trend_5     — 5-bar pct_change (short-term acceleration)
        vol_ratio   — current volume vs 20-bar average
        return_skew — 20-bar skew of returns (fat-tail / asymmetry)
    """
    if not candles:
        return pd.DataFrame()

    closes = np.asarray([c["close"] for c in candles], dtype=float)
    volumes = np.asarray([c["volume"] for c in candles], dtype=float)

    if len(closes) < 2:
        return pd.DataFrame()

    returns = pd.Series(np.diff(closes) / closes[:-1])
    volumes_s = pd.Series(volumes)

    features = pd.DataFrame({
        "volatility": returns.rolling(20).std(),
        "trend_20": pd.Series(closes).pct_change(20),
        "trend_5": pd.Series(closes).pct_change(5),
        "vol_ratio": volumes_s / volumes_s.rolling(20).mean(),
        "return_skew": returns.rolling(20).skew(),
    }).dropna()

    return features


def train_regime_model(
    candles: list,
    n_regimes: int = 4,
) -> dict:
    """Fit a fresh KMeans regime classifier and persist scaler+model.

    Returns a summary dict with the number of clusters, the model's
    inertia (lower = tighter clusters) and the path where artifacts
    were written. Raises ValueError when there aren't enough candles
    to produce a single feature row — training on an empty matrix
    would silently produce a degenerate clusterer.
    """
    # Lazy imports so the module stays importable in environments
    # without scikit-learn (e.g. paper-only deploys that skipped
    # requirements-ml.txt). Tests that don't touch this function
    # won't blow up at import time.
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    import joblib

    MODEL_PATH.mkdir(parents=True, exist_ok=True)

    features = compute_regime_features(candles)
    if features.empty:
        raise ValueError(
            "Not enough candles to compute regime features "
            "(need > 20 after rolling windows settle)"
        )

    scaler = StandardScaler()
    X = scaler.fit_transform(features)

    # n_init=10 runs the algorithm 10 times with different seeds and
    # keeps the tightest result — one-shot KMeans is sensitive to
    # initialisation and can produce pathological clusterings.
    model = KMeans(n_clusters=n_regimes, random_state=42, n_init=10)
    model.fit(X)

    joblib.dump(scaler, MODEL_PATH / "regime_scaler.pkl")
    joblib.dump(model, MODEL_PATH / "regime_model.pkl")

    return {
        "n_regimes": n_regimes,
        "inertia": float(model.inertia_),
        "model_path": str(MODEL_PATH),
        "n_samples": int(len(features)),
    }


def detect_current_regime(candles: list) -> str:
    """Classify the final candle's regime using the most recently
    trained model. Returns "unknown" when any of the following hold:
    model files missing, feature matrix empty, loader raises.
    """
    scaler_file = MODEL_PATH / "regime_scaler.pkl"
    model_file = MODEL_PATH / "regime_model.pkl"
    if not scaler_file.exists() or not model_file.exists():
        return "unknown"

    try:
        import joblib
        scaler = joblib.load(scaler_file)
        model = joblib.load(model_file)
    except Exception as e:
        logger.warning("Could not load regime model: %s", str(e)[:200])
        return "unknown"

    features = compute_regime_features(candles)
    if features.empty:
        return "unknown"

    X = scaler.transform(features.iloc[[-1]])
    try:
        regime_id: Optional[int] = int(model.predict(X)[0])
    except Exception as e:
        logger.warning("Regime prediction failed: %s", str(e)[:200])
        return "unknown"

    return REGIMES.get(regime_id, "unknown")
