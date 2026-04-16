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
    left_bars: int | None = None,
    right_bars: int | None = None,
    value: str = "resistance",
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> bool:
    """Evaluate a support/resistance condition on the latest close.

    When left_bars/right_bars are provided, pivots are detected from
    high/low data (or falls back to closes). Otherwise uses the legacy
    lookback-based swing detection.

    Conditions: near_support, near_resistance, below_support,
    above_resistance, price_crossing_up, price_crossing_down,
    price_greater_than, price_lower_than.
    """
    lb = left_bars or lookback
    rb = right_bars or lookback
    min_required = max(lb, rb) * 10
    if len(closes) < min_required:
        raise ValueError(
            f"Support/Resistance requires at least {min_required} data points, "
            f"got {len(closes)}"
        )

    if left_bars is not None or right_bars is not None:
        hi = highs if highs and len(highs) == len(closes) else closes
        lo = lows if lows and len(lows) == len(closes) else closes
        res_levels, sup_levels = [], []
        for i in range(lb, len(closes) - rb):
            if hi[i] > max(hi[i - lb:i]) and hi[i] > max(hi[i + 1:i + rb + 1]):
                res_levels.append(hi[i])
            if lo[i] < min(lo[i - lb:i]) and lo[i] < min(lo[i + 1:i + rb + 1]):
                sup_levels.append(lo[i])
        support = _cluster(sup_levels, tolerance_pct)
        resistance = _cluster(res_levels, tolerance_pct)
    else:
        support, resistance = find_support_resistance(closes, lookback, tolerance_pct)

    price = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else price
    levels = support if value == "support" else resistance

    if condition == "price_crossing_up":
        return any(prev < lvl and price > lvl for lvl in levels)
    if condition == "price_crossing_down":
        return any(prev > lvl and price < lvl for lvl in levels)
    if condition == "price_greater_than":
        return any(price > lvl for lvl in levels)
    if condition == "price_lower_than":
        return any(price < lvl for lvl in levels)

    if condition == "near_support":
        return any(abs(price - lvl) / price * 100 <= proximity_pct for lvl in support)
    if condition == "near_resistance":
        return any(abs(price - lvl) / price * 100 <= proximity_pct for lvl in resistance)
    if condition == "below_support":
        return bool(support) and price < min(support, key=lambda lv: abs(price - lv))
    if condition == "above_resistance":
        return bool(resistance) and price > min(resistance, key=lambda lv: abs(price - lv))

    return False
