# strategies/indicators/rsi.py
# Relative Strength Index (RSI) indicator.
# Measures momentum — below 30 = oversold (buy signal),
# above 70 = overbought (sell signal).

import re

import pandas as pd


# Threshold grammar understood by check_rsi_signal:
#   below_<N>         → rsi < N           (current RSI below value)
#   above_<N>         → rsi > N           (current RSI above value)
#   cross_below_<N>   → prev > N and curr <= N   (RSI crossed down through value)
#   cross_above_<N>   → prev < N and curr >= N   (RSI crossed up through value)
# where N is an integer in [1, 99].
_THRESHOLD_RE = re.compile(r"^(cross_above|cross_below|above|below)_(\d+)$")


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    """
    Calculate the current RSI value from a list of closing prices.
    Returns the latest RSI value (0-100).
    Requires at least period + 1 data points.

    Handles edge cases:
    - All prices identical (no movement) → returns 50.0
    - All gains, no losses → returns 100.0
    - All losses, no gains → returns 0.0
    """
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

    # Replace zero avg_loss with NaN to avoid ZeroDivisionError
    avg_loss_safe = avg_loss.replace(0, float("nan"))
    rs = avg_gain / avg_loss_safe

    rsi = 100 - (100 / (1 + rs))

    # Fill NaN: no losses means RSI = 100 (all gain)
    rsi = rsi.fillna(100)

    return round(float(rsi.iloc[-1]), 2)


def check_rsi_signal(closes: list[float], period: int = 14,
                     threshold: str = "below_35") -> bool:
    """
    Check if RSI meets the configured threshold condition.

    Supported threshold grammar:
        below_<N>        : RSI is strictly below N
        above_<N>        : RSI is strictly above N
        cross_below_<N>  : RSI just crossed down through N
                           (previous RSI > N AND current RSI <= N)
        cross_above_<N>  : RSI just crossed up through N
                           (previous RSI < N AND current RSI >= N)

    The crossing conditions need two consecutive RSI values, so the
    closes list must contain at least `period + 2` points.
    """
    match = _THRESHOLD_RE.match(threshold)
    if not match:
        raise ValueError(
            f"Unknown RSI threshold: {threshold!r}. Expected one of "
            "below_<N>, above_<N>, cross_below_<N>, cross_above_<N> "
            "with 1 <= N <= 99."
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

    # Crossing conditions — need the previous RSI value too.
    if len(closes) < period + 2:
        raise ValueError(
            f"RSI crossing conditions require at least {period + 2} "
            f"data points, got {len(closes)}"
        )
    prev = calculate_rsi(closes[:-1], period)
    curr = calculate_rsi(closes, period)

    if op == "cross_above":
        return prev < value and curr >= value
    # cross_below
    return prev > value and curr <= value
