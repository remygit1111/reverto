# strategies/indicators/ema.py
# Exponential Moving Average (EMA) indicator.
# EMA crossover signals trend direction changes.
# Bullish cross: fast EMA crosses above slow EMA → buy signal
# Bearish cross: fast EMA crosses below slow EMA → sell signal

import pandas as pd


def calculate_ema(closes: list[float], period: int) -> float:
    """
    Calculate the current EMA value from a list of closing prices.
    Returns the latest EMA value.
    """
    if len(closes) < period:
        raise ValueError(f"EMA requires at least {period} data points, got {len(closes)}")

    series = pd.Series(closes)
    ema = series.ewm(span=period, adjust=False).mean()
    return round(float(ema.iloc[-1]), 2)


def check_ema_cross_signal(closes: list[float], fast: int = 9,
                            slow: int = 21, signal: str = "bullish") -> bool:
    """
    Check if a fast/slow EMA crossover signal is present.
    Looks at the last two candles to detect a fresh crossover.

    signal = "bullish" → fast crossed above slow (buy)
    signal = "bearish" → fast crossed below slow (sell)
    Returns True if the crossover just occurred.
    """
    if len(closes) < slow + 1:
        raise ValueError(f"EMA cross requires at least {slow + 1} data points")

    series = pd.Series(closes)

    fast_ema = series.ewm(span=fast, adjust=False).mean()
    slow_ema = series.ewm(span=slow, adjust=False).mean()

    # Check last two candles for crossover
    fast_prev, fast_curr = float(fast_ema.iloc[-2]), float(fast_ema.iloc[-1])
    slow_prev, slow_curr = float(slow_ema.iloc[-2]), float(slow_ema.iloc[-1])

    if signal == "bullish":
        # Fast crossed above slow
        return fast_prev <= slow_prev and fast_curr > slow_curr
    elif signal == "bearish":
        # Fast crossed below slow
        return fast_prev >= slow_prev and fast_curr < slow_curr
    else:
        raise ValueError(f"Unknown EMA signal: {signal}. Choose 'bullish' or 'bearish'")