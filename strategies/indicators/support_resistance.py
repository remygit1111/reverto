# strategies/indicators/support_resistance.py
# PineScript fixnan(pivothigh/pivotlow) style S&R.
# Per candle: one active resistance, one active support.
# The value "steps" when a new pivot is confirmed.


def _ema(values: list[float], period: int) -> list[float]:
    """Simple EMA matching PineScript's ema() — adjust=False."""
    out = [0.0] * len(values)
    if not values:
        return out
    k = 2.0 / (period + 1)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = k * values[i] + (1 - k) * out[i - 1]
    return out


def _volume_osc(volumes: list[float]) -> list[float]:
    """Volume EMA oscillator: 100 * (EMA5 - EMA10) / EMA10."""
    short = _ema(volumes, 5)
    long = _ema(volumes, 10)
    n = len(volumes)
    osc = [0.0] * n
    for i in range(n):
        if long[i] != 0:
            osc[i] = 100.0 * (short[i] - long[i]) / long[i]
    return osc


def calculate_sr_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    left_bars: int = 15,
    right_bars: int = 15,
    volumes: list[float] | None = None,
    volume_threshold: float = 0.0,
    min_touches: int = 1,
) -> tuple[list[float | None], list[float | None]]:
    """Return per-candle (resistance_series, support_series).

    Equivalent to PineScript's fixnan(ta.pivothigh)[1] / fixnan(ta.pivotlow)[1].
    A pivot high at bar p is confirmed at bar p + right_bars.
    The value carries forward (fixnan) until a new pivot replaces it.

    volume_threshold: if > 0, pivot only valid when volume EMA osc > threshold.
    min_touches: pivot only becomes active after price tests the level N times.
    """
    n = len(closes)
    if len(highs) != n or len(lows) != n:
        raise ValueError(
            f"highs/lows/closes must have equal length, "
            f"got {len(highs)}/{len(lows)}/{n}"
        )

    vol_osc: list[float] | None = None
    if volumes and volume_threshold > 0 and len(volumes) == n:
        vol_osc = _volume_osc(volumes)

    resistance_series: list[float | None] = [None] * n
    support_series: list[float | None] = [None] * n

    cur_res: float | None = None
    cur_sup: float | None = None
    res_touches = 0
    sup_touches = 0

    for i in range(n):
        p = i - right_bars
        if p >= left_bars:
            h = highs[p]
            if (h > max(highs[p - left_bars:p])
                    and h > max(highs[p + 1:p + right_bars + 1])):
                vol_ok = vol_osc is None or vol_osc[p] > volume_threshold
                if vol_ok:
                    cur_res = h
                    res_touches = 1

            lo = lows[p]
            if (lo < min(lows[p - left_bars:p])
                    and lo < min(lows[p + 1:p + right_bars + 1])):
                vol_ok = vol_osc is None or vol_osc[p] > volume_threshold
                if vol_ok:
                    cur_sup = lo
                    sup_touches = 1

        # Count touches for min_touches filter
        if cur_res is not None and min_touches > 1:
            if abs(highs[i] - cur_res) / cur_res < 0.005:
                res_touches += 1
        if cur_sup is not None and min_touches > 1:
            if abs(lows[i] - cur_sup) / cur_sup < 0.005:
                sup_touches += 1

        resistance_series[i] = cur_res if res_touches >= min_touches else None
        support_series[i] = cur_sup if sup_touches >= min_touches else None

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
    volumes: list[float] | None = None,
    volume_threshold: float = 0.0,
    min_touches: int = 1,
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
        volumes=volumes,
        volume_threshold=volume_threshold,
        min_touches=min_touches,
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
