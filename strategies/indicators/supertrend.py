# strategies/indicators/supertrend.py
# Supertrend — volatility-based trailing stop indicator that tracks
# the current trend direction.
#
# Algorithm:
#   1. ATR = rolling Average True Range over `atr_period` candles
#   2. Mid-price = (high + low) / 2
#   3. Basic Upper = mid + multiplier × ATR
#      Basic Lower = mid - multiplier × ATR
#   4. Final Upper/Lower bands apply trend-following smoothing so
#      the band only moves in the favourable direction while the
#      trend persists.
#   5. Trend flips when close crosses the opposite band.
#
# Requires OHLC data (needs high and low to compute True Range).


def _true_range(high: float, low: float, prev_close: float) -> float:
    """Wilder's True Range for a single candle."""
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close),
    )


def calculate_supertrend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> list[tuple[float, int]]:
    """Walk the OHLC series and return a list of (supertrend, trend)
    pairs, one per candle. `trend` is +1 (bullish) or -1 (bearish).

    Requires at least `atr_period + 1` candles.
    """
    n = len(closes)
    if n != len(highs) or n != len(lows):
        raise ValueError("highs, lows and closes must have identical length")
    if n < atr_period + 1:
        raise ValueError(
            f"Supertrend requires at least {atr_period + 1} candles, got {n}"
        )

    # True Range series
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = _true_range(highs[i], lows[i], closes[i - 1])

    # Simple ATR for the first window, then Wilder smoothing
    atr = [0.0] * n
    atr[atr_period] = sum(tr[1 : atr_period + 1]) / atr_period
    for i in range(atr_period + 1, n):
        atr[i] = (atr[i - 1] * (atr_period - 1) + tr[i]) / atr_period

    result: list[tuple[float, int]] = []
    # Pre-fill the warmup period with the first valid trend direction.
    prev_final_upper = 0.0
    prev_final_lower = 0.0
    prev_trend = 1

    for i in range(n):
        if i < atr_period:
            result.append((0.0, 1))
            continue

        mid = (highs[i] + lows[i]) / 2
        basic_upper = mid + multiplier * atr[i]
        basic_lower = mid - multiplier * atr[i]

        # Final upper: trending-follow smoothing. Only allow the band
        # to move down (tighter) while price stays below it.
        if i == atr_period:
            final_upper = basic_upper
            final_lower = basic_lower
            trend = 1 if closes[i] > basic_upper else -1
        else:
            final_upper = (
                basic_upper
                if (basic_upper < prev_final_upper or closes[i - 1] > prev_final_upper)
                else prev_final_upper
            )
            final_lower = (
                basic_lower
                if (basic_lower > prev_final_lower or closes[i - 1] < prev_final_lower)
                else prev_final_lower
            )

            # Trend continuation / flip
            if prev_trend == 1:
                trend = -1 if closes[i] < final_lower else 1
            else:
                trend = 1 if closes[i] > final_upper else -1

        supertrend_val = final_lower if trend == 1 else final_upper
        result.append((supertrend_val, trend))

        prev_final_upper = final_upper
        prev_final_lower = final_lower
        prev_trend = trend

    return result


def check_supertrend_signal(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    atr_period: int = 10,
    multiplier: float = 3.0,
    condition: str = "bullish",
) -> bool:
    """Evaluate a Supertrend condition on the latest candle.

    Supported conditions:
        bullish       : current trend == +1
        bearish       : current trend == -1
        bullish_flip  : previous trend == -1 and current == +1
        bearish_flip  : previous trend == +1 and current == -1
    """
    series = calculate_supertrend(highs, lows, closes, atr_period, multiplier)
    if len(series) < 2:
        raise ValueError("Supertrend series too short for signal check")

    _, prev_trend = series[-2]
    _, curr_trend = series[-1]

    if condition == "bullish":
        return curr_trend == 1
    if condition == "bearish":
        return curr_trend == -1
    if condition == "bullish_flip":
        return prev_trend == -1 and curr_trend == 1
    if condition == "bearish_flip":
        return prev_trend == 1 and curr_trend == -1

    raise ValueError(
        f"Unknown Supertrend condition: {condition!r}. Choose from "
        "bullish / bearish / bullish_flip / bearish_flip."
    )
