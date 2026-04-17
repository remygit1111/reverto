"""Feature engineering for the Reverto ML pipeline.

Computes a compact set of technical + contextual features from an
OHLCV candle window (oldest-first). All values are returned as
plain floats so the dict can be fed straight into pandas or joblib
models without additional conversion. The guiding principle is
"everything a classifier could plausibly need about the last ~50
bars, no NaN, no Inf" — callers should not have to clean the
output before training.

The indicator helpers (calculate_rsi, calculate_bollinger_bands,
calculate_macd) return scalar snapshots of the CURRENT state, not
full series, so `*_prev` / `*_slope` features are built by re-running
the helper on a shifted window. That doubles the call count but
avoids having to reinvent parallel series logic inside this module.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

from strategies.indicators.bollinger import calculate_bollinger_bands
from strategies.indicators.macd import calculate_macd
from strategies.indicators.rsi import calculate_rsi

# Minimum candle-window we need to compute every feature reliably.
# MACD alone needs 3 * slow = 78 bars before it converges, so anything
# below that is an unsafe input — we return an empty dict so upstream
# ML code can short-circuit the sample rather than learn from noise.
MIN_CANDLES = 78


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce a value to a plain float, substituting `default` for
    NaN/Inf/non-numeric inputs. Keeps feature dicts finite so
    pandas.DataFrame(features) never inherits a stray NaN column."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    if math.isnan(v) or math.isinf(v):
        return float(default)
    return v


def compute_features(
    candles: list[dict],
    deal: Optional[dict] = None,
) -> dict:
    """Return a feature dict for the most recent candle in `candles`.

    Args:
        candles: oldest-first list of OHLCV dicts with the keys
            ``open``, ``high``, ``low``, ``close``, ``volume``.
            At least ``MIN_CANDLES`` entries are required — shorter
            windows produce an empty dict so training pipelines can
            skip the sample instead of silently learning from a
            half-warmed MACD.
        deal: optional deal-context dict. When provided, time and
            DCA features are added so the classifier can learn
            entry-time patterns.

    Returns:
        dict of feature_name → float. All values are finite; NaN /
        Inf are replaced with 0.0 via ``_safe_float``.
    """
    if not candles or len(candles) < MIN_CANDLES:
        return {}

    closes = np.asarray([c["close"] for c in candles], dtype=float)
    highs = np.asarray([c["high"] for c in candles], dtype=float)
    lows = np.asarray([c["low"] for c in candles], dtype=float)
    volumes = np.asarray([c["volume"] for c in candles], dtype=float)
    opens = np.asarray([c["open"] for c in candles], dtype=float)

    # ── Indicator snapshots ─────────────────────────────────────────────
    rsi_now = calculate_rsi(list(closes), 14)
    rsi_prev = calculate_rsi(list(closes[:-1]), 14)
    # Slope across 3 bars — mirrors what the audit-v17 RSI tests exercise.
    rsi_3back = calculate_rsi(list(closes[:-3]), 14) if len(closes) >= 17 else rsi_now

    bb = calculate_bollinger_bands(list(closes), 20, 2.0)
    bb_upper = bb["upper"]
    bb_lower = bb["lower"]
    bb_middle = bb["middle"]
    bb_width = (bb_upper - bb_lower) / (bb_middle + 1e-9)
    bb_range = bb_upper - bb_lower
    bb_pct_b = (closes[-1] - bb_lower) / bb_range if bb_range > 0 else 0.5

    macd_now = calculate_macd(list(closes))
    if len(closes) >= MIN_CANDLES + 3:
        macd_3back = calculate_macd(list(closes[:-3]))
        macd_slope = macd_now["histogram"] - macd_3back["histogram"]
    else:
        macd_slope = 0.0

    # ── Average True Range (14) ─────────────────────────────────────────
    # Wilder ATR in one line — True Range = max of 3 comparisons, then
    # 14-period rolling mean. We only need the latest value.
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ),
    )
    atr_14 = float(pd.Series(tr).rolling(14).mean().iloc[-1])

    # ── Moving averages / momentum ──────────────────────────────────────
    sma_20 = float(closes[-20:].mean())
    sma_50 = float(closes[-50:].mean()) if len(closes) >= 50 else sma_20

    # ── Candle-pattern primitives ───────────────────────────────────────
    last_open = float(opens[-1])
    last_close = float(closes[-1])
    last_high = float(highs[-1])
    last_low = float(lows[-1])
    body = abs(last_close - last_open)
    upper_wick = last_high - max(last_close, last_open)
    lower_wick = min(last_close, last_open) - last_low

    features: dict[str, float] = {
        # Momentum — RSI family.
        "rsi": _safe_float(rsi_now),
        "rsi_prev": _safe_float(rsi_prev),
        "rsi_slope": _safe_float(rsi_now - rsi_3back),
        "rsi_oversold": _safe_float(rsi_now < 30),
        "rsi_overbought": _safe_float(rsi_now > 70),

        # Bollinger — volatility envelope.
        "bb_width": _safe_float(bb_width),
        "bb_pct_b": _safe_float(bb_pct_b),
        "below_lower_bb": _safe_float(last_close < bb_lower),
        "above_upper_bb": _safe_float(last_close > bb_upper),

        # MACD — trend + momentum.
        "macd_histogram": _safe_float(macd_now["histogram"]),
        "macd_positive": _safe_float(macd_now["histogram"] > 0),
        "macd_slope": _safe_float(macd_slope),
        "macd_above_signal": _safe_float(macd_now["macd"] > macd_now["signal"]),

        # Trend — price-vs-MA ratios.
        "sma_20": _safe_float(sma_20),
        "price_vs_sma20": _safe_float(last_close / sma_20 - 1),
        "price_vs_sma50": _safe_float(last_close / sma_50 - 1),
        "trend_up": _safe_float(sma_20 > sma_50),

        # Rate-of-change across multiple horizons.
        "roc_5": _safe_float(last_close / closes[-6] - 1 if len(closes) >= 6 else 0.0),
        "roc_10": _safe_float(last_close / closes[-11] - 1 if len(closes) >= 11 else 0.0),
        "roc_20": _safe_float(last_close / closes[-21] - 1 if len(closes) >= 21 else 0.0),

        # Volatility.
        "atr_14": _safe_float(atr_14),
        "atr_pct": _safe_float(atr_14 / last_close if last_close else 0.0),
        "daily_range": _safe_float((last_high - last_low) / last_close if last_close else 0.0),

        # Volume — relative strength vs the 20-bar baseline.
        "volume_ratio": _safe_float(volumes[-1] / (volumes[-20:].mean() + 1e-9)),
        "volume_trend": _safe_float(volumes[-5:].mean() / (volumes[-20:].mean() + 1e-9)),

        # Candle-pattern fractions (body / wick sizes relative to close).
        "body_size": _safe_float(body / last_close if last_close else 0.0),
        "upper_wick": _safe_float(upper_wick / last_close if last_close else 0.0),
        "lower_wick": _safe_float(lower_wick / last_close if last_close else 0.0),
        "bull_candle": _safe_float(last_close > last_open),
    }

    # ── Deal-context features ───────────────────────────────────────────
    # Time-of-day and weekday are cyclic — encode sin/cos so a classifier
    # can learn continuity across the Sunday→Monday / 23:00→00:00 boundary.
    if deal:
        opened_at = deal.get("opened_at")
        if opened_at:
            try:
                ts = pd.Timestamp(opened_at, unit="s")
            except (TypeError, ValueError):
                ts = None
            if ts is not None and not pd.isna(ts):
                hour = ts.hour
                weekday = ts.weekday()
                features["hour_sin"] = _safe_float(np.sin(2 * np.pi * hour / 24))
                features["hour_cos"] = _safe_float(np.cos(2 * np.pi * hour / 24))
                features["weekday_sin"] = _safe_float(np.sin(2 * np.pi * weekday / 7))
                features["weekday_cos"] = _safe_float(np.cos(2 * np.pi * weekday / 7))
        features["dca_count"] = _safe_float(deal.get("dca_count", 0))

    return features


def features_to_dataframe(records: list[dict]) -> "pd.DataFrame":
    """Convert a list of ``compute_features`` outputs into a DataFrame
    with a stable column order — columns follow the first non-empty
    record, subsequent records fill missing keys with 0.0 so a varying
    feature set across deals never produces a ragged frame."""
    non_empty = [r for r in records if r]
    if not non_empty:
        return pd.DataFrame()
    columns = list(non_empty[0].keys())
    rows = []
    for r in records:
        rows.append([_safe_float(r.get(c, 0.0)) for c in columns])
    return pd.DataFrame(rows, columns=columns)
