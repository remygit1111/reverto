# strategies/indicators/parabolic_sar.py
# Parabolic SAR — trend-following Stop And Reverse indicator (Wilder).
# Uses high/low data for proper extreme point tracking.


def calculate_parabolic_sar(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    initial_af: float = 0.02,
    max_af: float = 0.20,
) -> list[tuple[float, int]]:
    """Walk the OHLC series and return (sar, trend) per bar.

    Trend is +1 (bullish / SAR below price) or -1 (bearish / SAR above price).
    Uses Wilder's original high/low-based EP tracking.
    """
    n = len(closes)
    if n < 10:
        raise ValueError(
            f"Parabolic SAR requires at least 10 data points, got {n}"
        )
    if len(highs) != n or len(lows) != n:
        raise ValueError("highs/lows/closes must have equal length")

    result: list[tuple[float, int]] = []

    if closes[1] >= closes[0]:
        trend = 1
        ep = highs[1]
        sar = lows[0]
    else:
        trend = -1
        ep = lows[1]
        sar = highs[0]
    af = initial_af
    result.append((sar, trend))
    result.append((sar, trend))

    for i in range(2, n):
        new_sar = sar + af * (ep - sar)

        if trend == 1:
            new_sar = min(new_sar, lows[i - 1], lows[i - 2] if i >= 3 else lows[i - 1])
            if new_sar > lows[i]:
                trend = -1
                sar = ep
                ep = lows[i]
                af = initial_af
            else:
                sar = new_sar
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + initial_af, max_af)
        else:
            new_sar = max(new_sar, highs[i - 1], highs[i - 2] if i >= 3 else highs[i - 1])
            if new_sar < highs[i]:
                trend = 1
                sar = ep
                ep = highs[i]
                af = initial_af
            else:
                sar = new_sar
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + initial_af, max_af)

        result.append((sar, trend))

    return result


def check_parabolic_sar_signal(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    initial_af: float = 0.02,
    max_af: float = 0.20,
    condition: str = "bullish",
) -> bool:
    """Evaluate a Parabolic SAR condition on the latest candle."""
    series = calculate_parabolic_sar(highs, lows, closes, initial_af, max_af)
    if len(series) < 2:
        raise ValueError("Parabolic SAR series too short for signal check")

    _, prev_trend = series[-2]
    _, curr_trend = series[-1]

    if condition in ("bullish", "price_greater_than"):
        return curr_trend == 1
    if condition in ("bearish", "price_lower_than"):
        return curr_trend == -1
    if condition in ("bullish_flip", "price_crossing_up"):
        return prev_trend == -1 and curr_trend == 1
    if condition in ("bearish_flip", "price_crossing_down"):
        return prev_trend == 1 and curr_trend == -1

    return False
