# strategies/indicators/macd.py
# Moving Average Convergence Divergence (MACD) indicator.

import pandas as pd


def calculate_macd(closes: list[float], fast: int = 12,
                   slow: int = 26, signal: int = 9,
                   oscillator_ma_type: str = "EMA",
                   signal_ma_type: str = "EMA") -> dict:
    """Calculate MACD line, signal line and histogram."""
    min_required = slow * 3
    if len(closes) < min_required:
        raise ValueError(
            f"MACD requires at least {min_required} data points "
            f"(3 * slow={slow}) for reliable signals, got {len(closes)}"
        )

    series = pd.Series(closes)

    if oscillator_ma_type == "SMA":
        ma_fast = series.rolling(window=fast).mean()
        ma_slow = series.rolling(window=slow).mean()
    else:
        ma_fast = series.ewm(span=fast, adjust=False).mean()
        ma_slow = series.ewm(span=slow, adjust=False).mean()

    macd_line = ma_fast - ma_slow

    if signal_ma_type == "SMA":
        signal_line = macd_line.rolling(window=signal).mean()
    else:
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()

    histogram = macd_line - signal_line

    hist_list = histogram.dropna().tolist()

    return {
        "macd": round(float(macd_line.iloc[-1]), 4),
        "signal": round(float(signal_line.iloc[-1]), 4),
        "histogram": round(float(histogram.iloc[-1]), 4),
        "macd_prev": round(float(macd_line.iloc[-2]), 4) if len(macd_line) >= 2 else 0.0,
        "histogram_series": hist_list,
    }


def check_macd_signal(closes: list[float],
                      condition: str = "histogram_positive",
                      use_percentile: bool = False) -> bool:
    """Check if MACD meets the configured condition.

    use_percentile
        Normalises the current histogram value against the maximum
        absolute histogram seen in the last 100 candles (or fewer if
        the series is shorter). This makes the same "histogram_positive"
        or "histogram_negative" threshold comparable across quiet and
        volatile market regimes — without normalisation, a $100 MACD
        reading at a trending $80k BTC is effectively the same sign
        strength as a $10 reading at a flat $5k BTC, and the raw
        comparison is dominated by price scale.

        Edge case: when the recent window contains only zeros (a perfectly
        flat market, the `histogram_series` yields max_abs == 0), we fall
        back to `hist_val = 0`. A zero histogram is not a positive or
        negative signal, so this short-circuits into a "no signal"
        state — safer than dividing by zero or propagating NaN through
        the condition check.
    """
    macd_data = calculate_macd(closes)

    if condition == "macd_cross_above_zero":
        return macd_data["macd_prev"] < 0 and macd_data["macd"] >= 0

    if condition == "macd_cross_below_zero":
        return macd_data["macd_prev"] > 0 and macd_data["macd"] <= 0

    hist_val = macd_data["histogram"]
    if use_percentile and macd_data["histogram_series"]:
        lookback = min(100, len(macd_data["histogram_series"]))
        recent = macd_data["histogram_series"][-lookback:]
        max_abs = max(abs(h) for h in recent)
        # Guard: flat histogram → no scale reference → no signal.
        hist_val = hist_val / max_abs if max_abs > 0 else 0

    conditions = {
        "histogram_positive": hist_val > 0,
        "histogram_negative": hist_val < 0,
        "macd_above_signal":  macd_data["macd"] > macd_data["signal"],
        "macd_below_signal":  macd_data["macd"] < macd_data["signal"],
    }

    if condition not in conditions:
        raise ValueError(
            f"Unknown MACD condition: {condition}. "
            f"Choose from: {list(conditions.keys())}"
        )

    return conditions[condition]
