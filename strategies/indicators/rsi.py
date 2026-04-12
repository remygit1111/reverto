# strategies/indicators/rsi.py
# Relative Strength Index (RSI) indicator.
# Measures momentum — below 30 = oversold (buy signal),
# above 70 = overbought (sell signal).

import pandas as pd


def calculate_rsi(closes: list[float], period: int = 14) -> float:
    """
    Calculate the current RSI value from a list of closing prices.
    Returns the latest RSI value (0-100).
    Requires at least period + 1 data points.
    """
    if len(closes) < period + 1:
        raise ValueError(f"RSI requires at least {period + 1} data points, got {len(closes)}")

    series = pd.Series(closes)
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return round(float(rsi.iloc[-1]), 2)


def check_rsi_signal(closes: list[float], period: int = 14, threshold: str = "below_35") -> bool:
    """
    Check if RSI meets the configured threshold condition.
    Supported thresholds:
        below_30, below_35, below_40  → oversold buy signals
        above_60, above_65, above_70  → overbought sell signals
    Returns True if the condition is met.
    """
    rsi = calculate_rsi(closes, period)

    conditions = {
        "below_30": rsi < 30,
        "below_35": rsi < 35,
        "below_40": rsi < 40,
        "above_60": rsi > 60,
        "above_65": rsi > 65,
        "above_70": rsi > 70,
    }

    if threshold not in conditions:
        raise ValueError(f"Unknown RSI threshold: {threshold}. Choose from: {list(conditions.keys())}")

    return conditions[threshold]