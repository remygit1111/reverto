# strategies/indicators/macd.py
# Moving Average Convergence Divergence (MACD) indicator.
# Used as confirmation for take profit signals.
# Positive histogram = bullish momentum → good time to take profit on longs

import pandas as pd


def calculate_macd(closes: list[float], fast: int = 12,
                   slow: int = 26, signal: int = 9) -> dict:
    """
    Calculate MACD line, signal line and histogram.
    Returns dict with macd, signal and histogram values.

    Requires at least 3 * slow candles for reliable output.
    EWM with adjust=False needs approximately 3x the slow period to converge.
    The original minimum of slow + signal (35 candles) was insufficient —
    MACD on 35 candles is heavily biased by EWM warm-up and produces
    unreliable signals.
    """
    min_required = slow * 3
    if len(closes) < min_required:
        raise ValueError(
            f"MACD requires at least {min_required} data points "
            f"(3 * slow={slow}) for reliable signals, got {len(closes)}"
        )

    series = pd.Series(closes)

    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()

    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return {
        "macd": round(float(macd_line.iloc[-1]), 4),
        "signal": round(float(signal_line.iloc[-1]), 4),
        "histogram": round(float(histogram.iloc[-1]), 4),
    }


def check_macd_signal(closes: list[float], condition: str = "histogram_positive") -> bool:
    """
    Check if MACD meets the configured condition.
    Supported conditions:
        histogram_positive → bullish momentum (good TP confirmation)
        histogram_negative → bearish momentum
        macd_above_signal  → bullish crossover
        macd_below_signal  → bearish crossover
    Returns True if the condition is met.
    """
    macd_data = calculate_macd(closes)

    conditions = {
        "histogram_positive": macd_data["histogram"] > 0,
        "histogram_negative": macd_data["histogram"] < 0,
        "macd_above_signal":  macd_data["macd"] > macd_data["signal"],
        "macd_below_signal":  macd_data["macd"] < macd_data["signal"],
    }

    if condition not in conditions:
        raise ValueError(
            f"Unknown MACD condition: {condition}. "
            f"Choose from: {list(conditions.keys())}"
        )

    return conditions[condition]
