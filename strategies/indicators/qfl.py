# strategies/indicators/qfl.py
# QFL (Quickfingersluc) Base Scanner — PineScript Zaphod logic.
#
# Detects "bases" (validated support lows) and signals when price
# cracks below the base after a confirmed pump.


def calculate_qfl_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    base_periods: int = 36,
    pump_periods: int = 8,
    pump_pct: float = 3.0,
    base_crack_pct: float = 3.0,
) -> dict:
    """Per-candle QFL series matching PineScript Zaphod logic.

    Returns dict with:
        base:         list[float|None] — current active base price
        buy_limit:    list[float|None] — buy limit (base * (1 - crack%))
        new_base:     list[bool]       — True on candle where new base detected
        highest_high: list[float|None] — highest high since base
    """
    n = len(lows)
    if len(highs) != n or len(closes) != n:
        raise ValueError("highs/lows/closes must have equal length")

    pp = min(pump_periods, base_periods - 1)
    pump_frac = pump_pct / 100
    crack_frac = base_crack_pct / 100

    base_series: list[float | None] = [None] * n
    buy_limit_series: list[float | None] = [None] * n
    new_base_series: list[bool] = [False] * n
    highest_high_series: list[float | None] = [None] * n

    cur_base: float | None = None
    cur_hh: float | None = None

    for i in range(n):
        start = max(0, i - base_periods + 1)
        lowest_low = min(lows[start: i + 1])

        new_base = False
        if i >= pp + 1:
            end_pp1 = max(1, i - pp)
            end_pp = i - pp + 1
            s_pp1 = max(0, end_pp1 - base_periods)
            s_pp = max(0, end_pp - base_periods)

            ll_pp1 = min(lows[s_pp1: end_pp1]) if end_pp1 > s_pp1 else float('inf')
            ll_pp = min(lows[s_pp: end_pp]) if end_pp > s_pp else float('inf')

            new_base = (ll_pp1 > ll_pp) and (ll_pp == lowest_low)

        hh_start = max(0, i - pp + 1)
        offset_high = max(highs[hh_start: i + 1])

        if new_base or cur_hh is None or highs[i] > cur_hh:
            cur_hh = offset_high

        if new_base:
            cur_base = lowest_low

        buy_limit = None
        if cur_base is not None and cur_hh is not None and cur_base > 0:
            pump_ok = (cur_hh - cur_base) / cur_base > pump_frac
            crack_ok = (cur_base - lows[i]) / cur_base > crack_frac
            if pump_ok and crack_ok:
                buy_limit = cur_base * (1 - crack_frac)

        base_series[i] = cur_base
        buy_limit_series[i] = buy_limit
        new_base_series[i] = new_base
        highest_high_series[i] = cur_hh

    return {
        "base": base_series,
        "buy_limit": buy_limit_series,
        "new_base": new_base_series,
        "highest_high": highest_high_series,
    }


def check_qfl_signal(
    closes: list[float],
    condition: str = "below_base",
    base_periods: int = 36,
    pump_periods: int = 8,
    pump_from_base_pct: float = 3.0,
    base_crack_pct: float = 3.0,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    **_kwargs,
) -> bool:
    """Evaluate a QFL condition on the latest candle."""
    valid_conds = ("below_base", "near_base", "base_retest")
    if condition not in valid_conds:
        raise ValueError(
            f"Unknown QFL condition: {condition!r}. Choose from {valid_conds}."
        )

    n = len(closes)
    if n < base_periods:
        raise ValueError(
            f"QFL requires at least {base_periods} data points, got {n}"
        )

    hi = highs if highs and len(highs) == n else closes
    lo = lows if lows and len(lows) == n else closes

    qfl = calculate_qfl_series(
        hi, lo, closes,
        base_periods=base_periods,
        pump_periods=pump_periods,
        pump_pct=pump_from_base_pct,
        base_crack_pct=base_crack_pct,
    )

    price = closes[-1]
    base = qfl["base"][-1]
    buy_limit = qfl["buy_limit"][-1]

    if condition == "below_base":
        return buy_limit is not None

    if base is None:
        return False

    if condition == "near_base":
        return abs(price - base) / base < 0.005

    if condition == "base_retest":
        return base * 0.998 <= price <= base * 1.002

    return False
