"""ML entry filter for Reverto.

Wraps a persisted classifier (typically XGBoost, but any estimator
exposing ``predict_proba`` and ``feature_names_in_`` works) and
returns a win-probability + enter/skip decision for a feature dict.

Fail-open by design: missing model, failed load, or predict-time
error all return ``(1.0, True)`` — the engine should keep its
baseline behaviour when ML is unavailable. The filter is an
OPTIONAL gate on top of the indicator logic, not a replacement.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "models"


class EntryFilter:
    """Gate-keeper for entry signals based on a trained classifier.

    Parameters
    ----------
    threshold:
        Minimum ``predict_proba`` of the positive (win) class required
        for ``should_enter`` to return True. Default 0.55 is a mild
        bias toward the classifier's opinion; raise to 0.6+ for a
        stricter gate on high-frequency bots, lower toward 0.5 to
        effectively use it as a tie-breaker only.
    """

    def __init__(self, threshold: float = 0.55) -> None:
        self.threshold = threshold
        self.model: Optional[object] = None
        self._load_model()

    def _load_model(self) -> None:
        model_file = MODEL_PATH / "entry_filter.pkl"
        if not model_file.exists():
            # No model yet — fresh install, pre-training, or
            # operator has deliberately removed the file.
            return
        try:
            # joblib is pulled lazily so environments without the
            # ML extras can still import EntryFilter (it will just
            # always fail-open).
            import joblib
            self.model = joblib.load(model_file)
            logger.info("Entry filter model loaded from %s", model_file)
        except Exception as e:
            logger.warning("Could not load entry filter: %s", str(e)[:200])
            self.model = None

    def predict(self, features: dict) -> tuple[float, bool]:
        """Return (win_probability, should_enter) for a feature dict.

        Fail-open contract: any predict-time error yields (1.0, True)
        so the engine continues on its indicator-only path rather
        than refusing every entry because the model is broken.
        """
        if self.model is None:
            return 1.0, True

        try:
            import pandas as pd
            X = pd.DataFrame([features])
            # Align the input frame with the exact feature order the
            # model was trained on. Missing columns default to 0 —
            # the training pipeline clamps NaN/Inf to 0 already, so
            # this preserves the same baseline.
            model_features = getattr(self.model, "feature_names_in_", None)
            if model_features is not None:
                X = X.reindex(columns=list(model_features), fill_value=0.0)

            proba = self.model.predict_proba(X)[0]
            # sklearn's binary classifiers return [p_negative, p_positive].
            win_prob = float(proba[1]) if len(proba) >= 2 else float(proba[0])
            return win_prob, win_prob >= self.threshold
        except Exception as e:
            logger.warning("Entry filter predict failed: %s", str(e)[:200])
            return 1.0, True

    def is_available(self) -> bool:
        """True when a usable model is loaded. Useful for dashboards
        to show whether the ML gate is active or on fail-open."""
        return self.model is not None
