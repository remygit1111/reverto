# strategies/indicators/market_structure.py
# Market Structure — detects swing highs/lows and trend structure
# via Higher High / Higher Low (uptrend) or Lower High / Lower Low
# (downtrend) sequences, plus Break of Structure (BOS) signals.
#
# Works on close prices only. A "swing" is a local extremum relative
# to `lookback` candles on each side.


def _swing_points(
    closes: list[float], lookback: int = 3
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (swing_highs, swing_lows) as lists of (index, close).

    A swing high at index i requires closes[i] to be strictly greater
    than the `lookback` closes on either side. A swing low mirrors
    this with strictly less than.
    """
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    n = len(closes)
    for i in range(lookback, n - lookback):
        pivot = closes[i]
        left = closes[i - lookback : i]
        right = closes[i + 1 : i + 1 + lookback]
        if all(pivot > x for x in left) and all(pivot > x for x in right):
            highs.append((i, pivot))
        elif all(pivot < x for x in left) and all(pivot < x for x in right):
            lows.append((i, pivot))
    return highs, lows


def check_market_structure_signal(
    closes: list[float],
    lookback: int = 3,
    condition: str = "bullish_bos",
) -> bool:
    """Evaluate a market-structure condition on the latest candle.

    Supported conditions:
        bullish_bos        — latest close breaks above the most recent swing high
        bearish_bos        — latest close breaks below the most recent swing low
        higher_low         — most recent swing low > previous swing low
        lower_high         — most recent swing high < previous swing high
        bullish_structure  — HH + HL combined (last swings show uptrend)
        bearish_structure  — LH + LL combined (last swings show downtrend)
    """
    min_required = lookback * 10
    if len(closes) < min_required:
        raise ValueError(
            f"Market Structure requires at least {min_required} data points, "
            f"got {len(closes)}"
        )

    highs, lows = _swing_points(closes, lookback)
    latest = closes[-1]

    if condition == "bullish_bos":
        if not highs:
            return False
        return latest > highs[-1][1]

    if condition == "bearish_bos":
        if not lows:
            return False
        return latest < lows[-1][1]

    if condition == "higher_low":
        if len(lows) < 2:
            return False
        return lows[-1][1] > lows[-2][1]

    if condition == "lower_high":
        if len(highs) < 2:
            return False
        return highs[-1][1] < highs[-2][1]

    if condition == "bullish_structure":
        if len(highs) < 2 or len(lows) < 2:
            return False
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        return hh and hl

    if condition == "bearish_structure":
        if len(highs) < 2 or len(lows) < 2:
            return False
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        return lh and ll

    raise ValueError(
        f"Unknown Market Structure condition: {condition!r}. Choose from "
        "bullish_bos / bearish_bos / higher_low / lower_high / "
        "bullish_structure / bearish_structure."
    )
