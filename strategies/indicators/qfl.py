# strategies/indicators/qfl.py
# QFL (Quickfingersluc) Base Scanner.
#
# A "base" is a local consolidation low that was quickly abandoned —
# the market dipped, then rebounded at least `crack_pct` percent within
# `base_candles` candles. Bases act like support levels that have been
# tested and immediately held. The signal fires when price returns to
# (or drops through) a previously-validated base, because a rejected
# base often acts as a springboard or, if cracked, as a trend reversal.
#
# Works on close prices only.


def _swing_lows(closes: list[float], lookback: int = 3) -> list[int]:
    """Return the indices of strict swing lows with `lookback` closes
    on each side."""
    out: list[int] = []
    for i in range(lookback, len(closes) - lookback):
        pivot = closes[i]
        left = closes[i - lookback : i]
        right = closes[i + 1 : i + 1 + lookback]
        if all(pivot < x for x in left) and all(pivot < x for x in right):
            out.append(i)
    return out


def find_qfl_bases(
    closes: list[float],
    lookback: int = 3,
    crack_pct: float = 3.0,
    base_candles: int = 5,
    max_bases: int = 5,
) -> list[float]:
    """Walk the closes series and return the latest `max_bases` valid
    QFL base prices, ordered oldest-to-newest.

    A swing low at index i is promoted to a "base" iff within the next
    `base_candles` candles the price rises at least `crack_pct`% above
    that swing low — proof the dip was rejected fast.
    """
    bases: list[float] = []
    lows = _swing_lows(closes, lookback)
    for idx in lows:
        base_price = closes[idx]
        window_end = min(idx + 1 + base_candles, len(closes))
        rebound = max(closes[idx + 1 : window_end], default=base_price)
        if rebound >= base_price * (1 + crack_pct / 100):
            bases.append(base_price)
    # Keep only the most recent `max_bases` entries
    return bases[-max_bases:]


def check_qfl_signal(
    closes: list[float],
    lookback: int = 3,
    crack_pct: float = 3.0,
    base_candles: int = 5,
    max_bases: int = 5,
    below_pct: float = 0.0,
    condition: str = "below_base",
    base_periods: int | None = None,
    pump_periods: int | None = None,
    pump_from_base_pct: float | None = None,
    base_crack_pct: float | None = None,
) -> bool:
    """Evaluate a QFL condition on the latest close.

    Supported conditions:
        below_base   — latest close is below any tracked base by at
                       least `below_pct`% (a "crack" of the base)
        near_base    — latest close is within 1% above any tracked base
                       (anticipation — price approaching the base)
        base_retest  — latest close is within 0.1% of a tracked base
                       from above (a clean retest after holding)
    """
    # Validate the condition name up-front so unknown conditions always
    # raise, even when no bases were detected.
    valid_conds = ("below_base", "near_base", "base_retest", "base_crack")
    if condition not in valid_conds:
        raise ValueError(
            f"Unknown QFL condition: {condition!r}. Choose from {valid_conds}."
        )

    bp = base_periods or (lookback * 20 + base_candles)
    if len(closes) < bp:
        raise ValueError(f"QFL requires at least {bp} data points, got {len(closes)}")

    if base_periods is not None:
        pfb = pump_from_base_pct or 3.0
        bcp = base_crack_pct or crack_pct
        base_val = min(closes[-base_periods:])
        pump_window = closes[-base_periods:]
        has_pump = max(pump_window) >= base_val * (1 + pfb / 100)
        price = closes[-1]
        if condition == "base_crack":
            return has_pump and price < base_val * (1 - bcp / 100)
        bases = [base_val] if has_pump else []
    else:
        bases = find_qfl_bases(closes, lookback, crack_pct, base_candles, max_bases)
    price = closes[-1]

    if not bases:
        return False

    if condition == "below_base":
        for base in bases:
            threshold = base * (1 - below_pct / 100)
            if price < threshold:
                return True
        return False

    if condition == "near_base":
        # Within 1% above the base — anticipation of a touch
        for base in bases:
            if base <= price <= base * 1.01:
                return True
        return False

    # condition == "base_retest" (validated above)
    for base in bases:
        diff_pct = abs(price - base) / base * 100
        if diff_pct <= 0.1 and price >= base:
            return True
    return False
