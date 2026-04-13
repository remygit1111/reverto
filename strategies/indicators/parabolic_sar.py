# strategies/indicators/parabolic_sar.py
# Parabolic SAR — trend-following Stop And Reverse indicator (Wilder).
#
# Classic Wilder uses candle high/low to track the extreme point. For
# Reverto's current close-based data model we approximate using the
# closes series: EP is max(close) during a bullish run, min(close)
# during a bearish run. This is a common simplification for bots that
# only have close data available. When Reverto's engine grows OHLC
# plumbing the formulas below can be upgraded to use true high/low.


def calculate_parabolic_sar(
    closes: list[float],
    initial_af: float = 0.02,
    max_af: float = 0.20,
) -> list[tuple[float, int]]:
    """Walk the closes series and return a list of (sar, trend) pairs,
    one per closing price. Trend is +1 (bullish) or -1 (bearish).

    The first two points bootstrap the trend direction from the sign
    of the first close change. Subsequent points follow Wilder's
    acceleration-factor recurrence:

        SAR[i] = SAR[i-1] + AF × (EP - SAR[i-1])

    The acceleration factor (AF) steps up by `initial_af` each time a
    new extreme point is made, capped at `max_af`. A SAR crossing the
    price flips the trend and resets AF to initial_af.
    """
    if len(closes) < 10:
        raise ValueError(
            f"Parabolic SAR requires at least 10 data points, got {len(closes)}"
        )

    result: list[tuple[float, int]] = []

    # Bootstrap: trend = sign(close[1] - close[0])
    if closes[1] >= closes[0]:
        trend = 1
        ep = closes[1]
        sar = closes[0]
    else:
        trend = -1
        ep = closes[1]
        sar = closes[0]
    af = initial_af
    result.append((sar, trend))  # index 0 placeholder
    result.append((sar, trend))  # index 1 after bootstrap

    for i in range(2, len(closes)):
        price = closes[i]
        new_sar = sar + af * (ep - sar)

        if trend == 1:
            # Bullish: SAR must stay below price. If it pierces price,
            # flip to bearish.
            if new_sar > price:
                trend = -1
                sar = ep  # on flip, SAR resets to the previous EP
                ep = price
                af = initial_af
            else:
                sar = new_sar
                if price > ep:
                    ep = price
                    af = min(af + initial_af, max_af)
        else:
            # Bearish: SAR must stay above price. If it pierces price,
            # flip to bullish.
            if new_sar < price:
                trend = 1
                sar = ep
                ep = price
                af = initial_af
            else:
                sar = new_sar
                if price < ep:
                    ep = price
                    af = min(af + initial_af, max_af)

        result.append((sar, trend))

    return result


def check_parabolic_sar_signal(
    closes: list[float],
    initial_af: float = 0.02,
    max_af: float = 0.20,
    condition: str = "bullish",
) -> bool:
    """Evaluate a Parabolic SAR condition on the latest candle.

    Supported conditions:
        bullish       : current trend is +1 (price above SAR)
        bearish       : current trend is -1 (price below SAR)
        bullish_flip  : previous trend was -1 and current is +1
        bearish_flip  : previous trend was +1 and current is -1
    """
    series = calculate_parabolic_sar(closes, initial_af, max_af)
    if len(series) < 2:
        raise ValueError("Parabolic SAR series too short for signal check")

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
        f"Unknown Parabolic SAR condition: {condition!r}. Choose from "
        "bullish / bearish / bullish_flip / bearish_flip."
    )
