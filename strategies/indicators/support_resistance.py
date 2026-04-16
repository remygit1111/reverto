# strategies/indicators/support_resistance.py
# Wick-based pivot Support & Resistance — matches TradingView's
# ta.pivothigh / ta.pivotlow behavior. Every confirmed pivot is a
# distinct level; no clustering. Broken levels are excluded.


def find_support_resistance(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    left_bars: int = 15,
    right_bars: int = 15,
) -> tuple[list[float], list[float]]:
    """Return (active_support, active_resistance) lists.

    A pivot high (resistance) is confirmed at bar i when
    highs[i] > all highs in [i-left_bars, i) AND > all highs in (i, i+right_bars].
    Support is symmetric on lows.

    A level is "broken" once any close after the confirmation bar
    exceeds it (resistance) or falls below it (support). Only
    active (unbroken) levels are returned.
    """
    n = len(closes)
    if len(highs) != n or len(lows) != n:
        raise ValueError(
            f"highs/lows/closes must have equal length, "
            f"got {len(highs)}/{len(lows)}/{n}"
        )
    min_required = left_bars + right_bars + 1
    if n < min_required:
        raise ValueError(
            f"S&R requires at least {min_required} bars "
            f"(left_bars={left_bars} + right_bars={right_bars} + 1), "
            f"got {n}"
        )

    res_pivots: list[tuple[int, float]] = []
    sup_pivots: list[tuple[int, float]] = []

    for i in range(left_bars, n - right_bars):
        h = highs[i]
        if h > max(highs[i - left_bars:i]) and h > max(highs[i + 1:i + right_bars + 1]):
            res_pivots.append((i, h))
        lo = lows[i]
        if lo < min(lows[i - left_bars:i]) and lo < min(lows[i + 1:i + right_bars + 1]):
            sup_pivots.append((i, lo))

    active_res = [
        price for (idx, price) in res_pivots
        if not any(closes[j] > price for j in range(idx + 1, n))
    ]
    active_sup = [
        price for (idx, price) in sup_pivots
        if not any(closes[j] < price for j in range(idx + 1, n))
    ]

    return active_sup, active_res


def find_support_resistance_detailed(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    left_bars: int = 15,
    right_bars: int = 15,
) -> tuple[list[dict], list[dict]]:
    """Return (support_levels, resistance_levels) with pivot metadata.

    Each level is a dict: {"price": float, "pivot_index": int,
    "break_index": int | None}. break_index is the first bar where
    a close breaches the level, or None if still active.
    """
    n = len(closes)
    if len(highs) != n or len(lows) != n:
        raise ValueError("highs/lows/closes must have equal length")
    min_required = left_bars + right_bars + 1
    if n < min_required:
        return [], []

    res_pivots: list[tuple[int, float]] = []
    sup_pivots: list[tuple[int, float]] = []
    for i in range(left_bars, n - right_bars):
        h = highs[i]
        if h > max(highs[i - left_bars:i]) and h > max(highs[i + 1:i + right_bars + 1]):
            res_pivots.append((i, h))
        lo = lows[i]
        if lo < min(lows[i - left_bars:i]) and lo < min(lows[i + 1:i + right_bars + 1]):
            sup_pivots.append((i, lo))

    def _break_idx(idx: int, price: float, above: bool) -> int | None:
        for j in range(idx + 1, n):
            if above and closes[j] > price:
                return j
            if not above and closes[j] < price:
                return j
        return None

    res_out = [
        {"price": p, "pivot_index": i, "break_index": _break_idx(i, p, True)}
        for i, p in res_pivots
    ]
    sup_out = [
        {"price": p, "pivot_index": i, "break_index": _break_idx(i, p, False)}
        for i, p in sup_pivots
    ]
    return sup_out, res_out


def check_support_resistance_signal(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    left_bars: int = 15,
    right_bars: int = 15,
    proximity_pct: float = 1.0,
    condition: str = "price_crossing_down",
    value: str = "resistance",
) -> bool:
    """Evaluate an S&R condition against unbroken pivot levels.

    Conditions:
      near_support / near_resistance — price within proximity_pct
      below_support / above_resistance — price beyond nearest level
      price_crossing_up / price_crossing_down — prev ≶ level ≤≥ price
      price_greater_than / price_lower_than — simple comparison
    """
    support, resistance = find_support_resistance(
        highs, lows, closes, left_bars, right_bars,
    )

    price = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else price
    levels = support if value == "support" else resistance

    if condition == "price_crossing_up":
        return any(prev < lvl <= price for lvl in levels)
    if condition == "price_crossing_down":
        return any(prev > lvl >= price for lvl in levels)
    if condition == "price_greater_than":
        return any(price > lvl for lvl in levels)
    if condition == "price_lower_than":
        return any(price < lvl for lvl in levels)

    if condition == "near_support":
        return any(
            abs(price - lvl) / lvl * 100 <= proximity_pct
            for lvl in support
        ) if support else False
    if condition == "near_resistance":
        return any(
            abs(price - lvl) / lvl * 100 <= proximity_pct
            for lvl in resistance
        ) if resistance else False
    if condition == "below_support":
        return bool(support) and price < min(
            support, key=lambda lv: abs(price - lv))
    if condition == "above_resistance":
        return bool(resistance) and price > min(
            resistance, key=lambda lv: abs(price - lv))

    return False
