# strategies/indicators/bollinger.py
# Bollinger Bands — volatility envelope around a simple moving average.
#
# Middle = SMA(closes, period)
# Upper  = Middle + multiplier × population std dev
# Lower  = Middle - multiplier × population std dev

import statistics


def calculate_bollinger_bands(
    closes: list[float],
    period: int = 20,
    multiplier: float = 2.0,
) -> dict[str, float]:
    """Return the latest upper/middle/lower bands.

    Uses population standard deviation (pstdev) which matches the
    canonical Bollinger definition. Requires at least `period` closes.
    """
    if len(closes) < period:
        raise ValueError(
            f"Bollinger requires at least {period} data points, got {len(closes)}"
        )

    window = closes[-period:]
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
) -> bool:
    """Evaluate a Bollinger-band based condition.

    Supported conditions:
        price_below_lower  : latest close < lower band  (mean-reversion buy)
        price_above_upper  : latest close > upper band  (mean-reversion sell)
        price_below_middle : latest close < middle SMA  (bearish bias)
        price_above_middle : latest close > middle SMA  (bullish bias)
        squeeze            : (upper - lower) / middle < squeeze_threshold
                             — bands are compressed, often precedes a breakout
    """
    bands = calculate_bollinger_bands(closes, period, multiplier)
    price = closes[-1]

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

    raise ValueError(
        f"Unknown Bollinger condition: {condition!r}. Choose from "
        "price_below_lower / price_above_upper / price_below_middle / "
        "price_above_middle / squeeze."
    )
