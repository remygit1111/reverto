# strategies/indicators/support_resistance.py
# Swing-based Support & Resistance — clusters recent swing highs into
# resistance levels and swing lows into support levels, then checks
# whether the current price is near / through one of them.
#
# Operates on close prices only.


def _swing_points(
    closes: list[float], lookback: int = 3
) -> tuple[list[float], list[float]]:
    """Return (swing_highs, swing_lows) as lists of close prices."""
    highs: list[float] = []
    lows: list[float] = []
    for i in range(lookback, len(closes) - lookback):
        pivot = closes[i]
        left = closes[i - lookback : i]
        right = closes[i + 1 : i + 1 + lookback]
        if all(pivot > x for x in left) and all(pivot > x for x in right):
            highs.append(pivot)
        elif all(pivot < x for x in left) and all(pivot < x for x in right):
            lows.append(pivot)
    return highs, lows


def _cluster(levels: list[float], tolerance_pct: float) -> list[float]:
    """Collapse levels that are within `tolerance_pct` of each other
    into a single level (most recent occurrence wins).

    The input order matters — we assume chronological so the "most
    recent" tie-break actually picks the newer price.
    """
    clustered: list[float] = []
    for level in levels:
        merged = False
        for i, existing in enumerate(clustered):
            if existing == 0:
                continue
            diff_pct = abs(level - existing) / existing * 100
            if diff_pct <= tolerance_pct:
                clustered[i] = level  # most recent wins
                merged = True
                break
        if not merged:
            clustered.append(level)
    return clustered


def find_support_resistance(
    closes: list[float],
    lookback: int = 3,
    tolerance_pct: float = 0.5,
) -> tuple[list[float], list[float]]:
    """Return (support_levels, resistance_levels) clustered from
    swing points in `closes`."""
    highs, lows = _swing_points(closes, lookback)
    resistance = _cluster(highs, tolerance_pct)
    support = _cluster(lows, tolerance_pct)
    return support, resistance


def check_support_resistance_signal(
    closes: list[float],
    lookback: int = 3,
    tolerance_pct: float = 0.5,
    proximity_pct: float = 1.0,
    condition: str = "near_support",
) -> bool:
    """Evaluate a support/resistance condition on the latest close.

    Supported conditions:
        near_support     — current price within proximity_pct of a support level
        near_resistance  — current price within proximity_pct of a resistance level
        below_support    — current price is below the nearest support
                           (support broken, bearish)
        above_resistance — current price is above the nearest resistance
                           (resistance broken, bullish)
    """
    min_required = lookback * 20
    if len(closes) < min_required:
        raise ValueError(
            f"Support/Resistance requires at least {min_required} data points, "
            f"got {len(closes)}"
        )

    support, resistance = find_support_resistance(closes, lookback, tolerance_pct)
    price = closes[-1]

    if condition == "near_support":
        for level in support:
            if abs(price - level) / price * 100 <= proximity_pct:
                return True
        return False

    if condition == "near_resistance":
        for level in resistance:
            if abs(price - level) / price * 100 <= proximity_pct:
                return True
        return False

    if condition == "below_support":
        if not support:
            return False
        nearest = min(support, key=lambda lvl: abs(price - lvl))
        return price < nearest

    if condition == "above_resistance":
        if not resistance:
            return False
        nearest = min(resistance, key=lambda lvl: abs(price - lvl))
        return price > nearest

    raise ValueError(
        f"Unknown Support/Resistance condition: {condition!r}. Choose from "
        "near_support / near_resistance / below_support / above_resistance."
    )
