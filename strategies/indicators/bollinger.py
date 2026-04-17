# strategies/indicators/bollinger.py
# Bollinger Bands — volatility envelope around a moving average.

import statistics


def _wma(values: list[float], period: int) -> float:
    w = list(range(1, period + 1))
    return sum(v * wt for v, wt in zip(values[-period:], w)) / sum(w)


def _ema_val(values: list[float], period: int) -> float:
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = k * v + (1 - k) * ema
    return ema


def calculate_bollinger_bands(
    closes: list[float],
    period: int = 20,
    multiplier: float = 2.0,
    ma_type: str = "SMA",
) -> dict[str, float]:
    """Return the latest upper/middle/lower bands."""
    if len(closes) < period:
        raise ValueError(
            f"Bollinger requires at least {period} data points, got {len(closes)}"
        )

    window = closes[-period:]
    if ma_type == "EMA":
        middle = _ema_val(closes, period)
    elif ma_type == "WMA":
        middle = _wma(closes, period)
    else:
        middle = sum(window) / period
    std = statistics.pstdev(window) if period >= 2 else 0.0
    upper = middle + multiplier * std
    lower = middle - multiplier * std
    return {"upper": upper, "middle": middle, "lower": lower}


def check_bollinger_signal(
    closes: list[float],
    period: int = 20,
    multiplier: float = 2.0,
    condition: str = "price_below_lower",
    squeeze_threshold: float = 0.02,
    ma_type: str = "SMA",
    value: str = "lower",
) -> bool:
    """Evaluate a Bollinger-band based condition.

    Extended conditions:
        percent_b_below_0  : %B < 0 (price below lower band)
        percent_b_above_1  : %B > 1 (price above upper band)
        percent_b_below_20 : %B < 0.2 (near lower band)
        percent_b_above_80 : %B > 0.8 (near upper band)
    """
    bands = calculate_bollinger_bands(closes, period, multiplier, ma_type)
    price = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else price
    band = bands.get(value, bands["lower"])

    if condition == "price_crossing_up":
        return prev < band and price > band
    if condition == "price_crossing_down":
        return prev > band and price < band
    if condition == "price_greater_than":
        return price > band
    if condition == "price_lower_than":
        return price < band

    if condition == "price_below_lower":
        return price < bands["lower"]
    if condition == "price_above_upper":
        return price > bands["upper"]
    if condition == "price_below_middle":
        return price < bands["middle"]
    if condition == "price_above_middle":
        return price > bands["middle"]
    if condition == "squeeze":
        if bands["middle"] == 0:
            return False
        bandwidth = (bands["upper"] - bands["lower"]) / bands["middle"]
        return bandwidth < squeeze_threshold

    bw = bands["upper"] - bands["lower"]
    if bw > 0:
        pct_b = (price - bands["lower"]) / bw
    else:
        pct_b = 0.5

    if condition == "percent_b_below_0":
        return pct_b < 0
    if condition == "percent_b_above_1":
        return pct_b > 1
    if condition == "percent_b_below_20":
        return pct_b < 0.2
    if condition == "percent_b_above_80":
        return pct_b > 0.8

    return False
