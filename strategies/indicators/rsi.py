# strategies/indicators/rsi.py
# Relative Strength Index (RSI) indicator.

import re

import pandas as pd


_THRESHOLD_RE = re.compile(r"^(cross_above|cross_below|above|below)_(\d+)$")


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    """Return the latest RSI value (0-100)."""
    if len(closes) < period + 1:
        raise ValueError(
            f"RSI requires at least {period + 1} data points, got {len(closes)}"
        )

    series = pd.Series(closes)
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    avg_loss_safe = avg_loss.replace(0, float("nan"))
    rs = avg_gain / avg_loss_safe

    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100)

    return round(float(rsi.iloc[-1]), 2)


def _rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Return full RSI series for divergence detection."""
    if len(closes) < period + 1:
        raise ValueError(
            f"RSI requires at least {period + 1} data points, got {len(closes)}"
        )
    series = pd.Series(closes)
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    avg_loss_safe = avg_loss.replace(0, float("nan"))
    rs = avg_gain / avg_loss_safe
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100)
    return [round(float(v), 2) for v in rsi.tolist()]


def check_rsi_signal(closes: list[float], period: int = 14,
                     threshold: str = "below_35") -> bool:
    """Check if RSI meets the configured threshold condition.

    Extended conditions beyond the threshold grammar:
        rsi_cross_above_50     : centerline cross up
        rsi_cross_below_50     : centerline cross down
        rsi_bullish_divergence : price lower low + RSI higher low
        rsi_bearish_divergence : price higher high + RSI lower high
    """
    if threshold == "rsi_cross_above_50":
        if len(closes) < period + 2:
            return False
        prev = calculate_rsi(closes[:-1], period)
        curr = calculate_rsi(closes, period)
        return prev < 50 and curr >= 50

    if threshold == "rsi_cross_below_50":
        if len(closes) < period + 2:
            return False
        prev = calculate_rsi(closes[:-1], period)
        curr = calculate_rsi(closes, period)
        return prev > 50 and curr <= 50

    if threshold == "rsi_bullish_divergence":
        lookback = 5
        if len(closes) < period + lookback + 1:
            return False
        rsi = _rsi_series(closes, period)
        return closes[-1] < closes[-lookback] and rsi[-1] > rsi[-lookback]

    if threshold == "rsi_bearish_divergence":
        lookback = 5
        if len(closes) < period + lookback + 1:
            return False
        rsi = _rsi_series(closes, period)
        return closes[-1] > closes[-lookback] and rsi[-1] < rsi[-lookback]

    match = _THRESHOLD_RE.match(threshold)
    if not match:
        raise ValueError(
            f"Unknown RSI threshold: {threshold!r}. Expected one of "
            "below_<N>, above_<N>, cross_below_<N>, cross_above_<N>, "
            "rsi_cross_above_50, rsi_cross_below_50, "
            "rsi_bullish_divergence, rsi_bearish_divergence."
        )

    op = match.group(1)
    value = int(match.group(2))
    if not (1 <= value <= 99):
        raise ValueError(
            f"RSI threshold value must be between 1 and 99, got {value}"
        )

    if op == "below":
        return calculate_rsi(closes, period) < value
    if op == "above":
        return calculate_rsi(closes, period) > value

    if len(closes) < period + 2:
        raise ValueError(
            f"RSI crossing conditions require at least {period + 2} "
            f"data points, got {len(closes)}"
        )
    prev = calculate_rsi(closes[:-1], period)
    curr = calculate_rsi(closes, period)

    if op == "cross_above":
        return prev < value and curr >= value
    return prev > value and curr <= value
