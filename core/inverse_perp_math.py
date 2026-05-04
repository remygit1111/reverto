"""Pure helpers for inverse-perpetual price math.

Reverto trades BTC/USD inverse-perpetual contracts (Bitget). Realized
PnL on inverse perp is computed as

    pnl_btc = size * (current_price - entry_price) / current_price * leverage    # long
    pnl_btc = size * (entry_price - current_price) / current_price * leverage    # short

(see ``paper/paper_state.py`` ``calculate_pnl``).

Audit finding pt-041 — surfaced by
``docs/architecture-investigation-tp-dca-correctness.md`` — flagged
that TP/SL **target** prices were derived with linear-perpetual math
(``avg * (1 + p)``) while PnL was realized inversely. The asymmetry
caused configured-tp_pct to deliver a different *realized* pnl_pct
than the operator expected — at TP=5% on a long, ~4.76% realized
instead of 5.00%.

These helpers fix the asymmetry by deriving the trigger price from
the *inverse* formula. Verification: solving
``pnl_pct = (target - avg) / target * leverage == p`` for ``target``
gives ``target = avg / (1 - p)`` for longs (``leverage=1`` reduction
shown for clarity; full leverage cancels symmetrically). For shorts
the mirror is ``target = avg / (1 + p)``.

Pure functions live here (no engine state, no I/O) so both
``paper/paper_engine.py`` and ``backtest/backtest_engine.py`` consume
them — backtest's results then match live realized PnL by
construction.
"""

from __future__ import annotations


_VALID_SIDES: frozenset[str] = frozenset({"long", "short"})


def compute_tp_target_price(
    avg_entry: float, tp_pct: float, side: str,
) -> float:
    """Inverse-perp price that yields exactly ``tp_pct`` realized PnL.

    Long: ``target = avg_entry / (1 - tp_pct/100)``.
    Short: ``target = avg_entry / (1 + tp_pct/100)``.

    Worked example (long, tp=3 %, avg=63 000):

      target = 63 000 / 0.97 ≈ 64 948.45

      Realized PnL at the target:
        pnl_btc = size × (64 948.45 − 63 000) / 64 948.45 × leverage
                = size × 0.030000… × leverage
        pnl_pct (margin = size/leverage) = leverage² × 3.00 %

      So at leverage=1 the realized return is exactly the configured
      3 % — symmetric with ``calculate_pnl`` in
      ``paper/paper_state.py``.

    Pre-fix the formula was ``avg * (1 + tp_pct/100)`` (linear-perp
    shape). At tp_pct=3 % long that yielded a target of avg×1.03; the
    inverse PnL evaluation at that target gave only
    ``tp_pct/(1+tp_pct/100)`` ≈ 2.913 % realized — see pt-041.

    Raises ``ValueError`` for unrecognised ``side`` (not ``"long"`` or
    ``"short"``) so a typo in a future config field surfaces loudly
    instead of silently routing through a long-only branch.
    """
    if side not in _VALID_SIDES:
        raise ValueError(
            f"Unknown deal side {side!r} — expected one of {sorted(_VALID_SIDES)}"
        )
    if side == "long":
        return avg_entry / (1 - tp_pct / 100)
    return avg_entry / (1 + tp_pct / 100)


def compute_sl_target_price(
    avg_entry: float, sl_pct: float, side: str,
) -> float:
    """Inverse-perp price that yields exactly ``-sl_pct`` realized
    PnL (SL is a configured *loss* threshold).

    Long: ``target = avg_entry / (1 + sl_pct/100)``.
    Short: ``target = avg_entry / (1 - sl_pct/100)``.

    Worked example (long, sl=5 %, avg=63 000):

      target = 63 000 / 1.05 = 60 000

      Realized PnL at the target:
        pnl_btc = size × (60 000 − 63 000) / 60 000 × leverage
                = size × (−0.05) × leverage
        pnl_pct = leverage² × (−5 %)

      Configured ``-5 %`` matches realized ``-5 %`` at leverage=1 —
      symmetric with ``calculate_pnl`` and pt-041's intent.

    Raises ``ValueError`` for unrecognised ``side``.
    """
    if side not in _VALID_SIDES:
        raise ValueError(
            f"Unknown deal side {side!r} — expected one of {sorted(_VALID_SIDES)}"
        )
    if side == "long":
        return avg_entry / (1 + sl_pct / 100)
    return avg_entry / (1 - sl_pct / 100)
