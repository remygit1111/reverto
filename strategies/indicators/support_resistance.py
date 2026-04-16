# strategies/indicators/support_resistance.py
# PineScript fixnan(pivothigh/pivotlow) style S&R.
# Per candle: one active resistance, one active support.
# The value "steps" when a new pivot is confirmed.


def calculate_sr_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    left_bars: int = 15,
    right_bars: int = 15,
) -> tuple[list[float | None], list[float | None]]:
    """Return per-candle (resistance_series, support_series).

    Equivalent to PineScript's fixnan(ta.pivothigh)[1] / fixnan(ta.pivotlow)[1].
    A pivot high at bar p is confirmed at bar p + right_bars.
    The value carries forward (fixnan) until a new pivot replaces it.
    """
    n = len(closes)
    if len(highs) != n or len(lows) != n:
        raise ValueError(
            f"highs/lows/closes must have equal length, "
            f"got {len(highs)}/{len(lows)}/{n}"
        )

    resistance_series: list[float | None] = [None] * n
    support_series: list[float | None] = [None] * n

    cur_res: float | None = None
    cur_sup: float | None = None

    for i in range(n):
        p = i - right_bars
        if p >= left_bars:
            window_h = highs[p - left_bars: p + right_bars + 1]
            if len(window_h) == left_bars + right_bars + 1:
                h = highs[p]
                if (h > max(highs[p - left_bars:p])
                        and h > max(highs[p + 1:p + right_bars + 1])):
                    cur_res = h

            window_l = lows[p - left_bars: p + right_bars + 1]
            if len(window_l) == left_bars + right_bars + 1:
                lo = lows[p]
                if (lo < min(lows[p - left_bars:p])
                        and lo < min(lows[p + 1:p + right_bars + 1])):
                    cur_sup = lo

        resistance_series[i] = cur_res
        support_series[i] = cur_sup

    return resistance_series, support_series


def find_support_resistance(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    left_bars: int = 15,
    right_bars: int = 15,
) -> tuple[list[float], list[float]]:
    """Return (active_support, active_resistance) lists.

    Back-compat wrapper: returns a single-element list with the
    current (last) fixnan value, or empty if no pivot found yet.
    """
    res_series, sup_series = calculate_sr_series(
        highs, lows, closes, left_bars, right_bars,
    )
    active_sup = [sup_series[-1]] if sup_series[-1] is not None else []
    active_res = [res_series[-1]] if res_series[-1] is not None else []
    return active_sup, active_res


def find_support_resistance_detailed(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    left_bars: int = 15,
    right_bars: int = 15,
    max_levels: int = 3,
) -> tuple[list[dict], list[dict]]:
    """Return (support_levels, resistance_levels) with pivot metadata.

    Each level is a dict: {"price": float, "pivot_index": int,
    "break_index": int | None}. Kept for test back-compat.
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

    return sup_out[-max_levels:], res_out[-max_levels:]


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
    """Evaluate an S&R condition using fixnan pivot series."""
    n = len(closes)
    min_required = left_bars + right_bars + 1
    if n < min_required:
        raise ValueError(
            f"S&R requires at least {min_required} bars "
            f"(left_bars={left_bars} + right_bars={right_bars} + 1), "
            f"got {n}"
        )

    res_series, sup_series = calculate_sr_series(
        highs, lows, closes, left_bars, right_bars,
    )

    resistance = res_series[-1]
    support = sup_series[-1]
    price = closes[-1]
    prev = closes[-2] if n >= 2 else price

    if condition == "price_crossing_up":
        level = support if value == "support" else resistance
        if level is None:
            return False
        return prev < level <= price

    if condition == "price_crossing_down":
        level = support if value == "support" else resistance
        if level is None:
            return False
        return prev > level >= price

    if condition == "price_greater_than":
        level = support if value == "support" else resistance
        if level is None:
            return False
        return price > level

    if condition == "price_lower_than":
        level = support if value == "support" else resistance
        if level is None:
            return False
        return price < level

    if condition == "near_support":
        if support is None:
            return False
        return abs(price - support) / support * 100 <= proximity_pct

    if condition == "near_resistance":
        if resistance is None:
            return False
        return abs(price - resistance) / resistance * 100 <= proximity_pct

    if condition == "below_support":
        if support is None:
            return False
        return price < support

    if condition == "above_resistance":
        if resistance is None:
            return False
        return price > resistance

    return False
