# paper/paper_state.py
# Tracks all open deals, orders, balance and history for paper trading.
# Acts as an in-memory database for the paper engine.
# Thread-safe: all access to open_deals is protected by a Lock.

import threading
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, UTC


@dataclass
class PaperOrder:
    """Represents a single order within a paper deal."""
    order_number: int           # 1 = base order, 2+ = DCA orders
    price: float                # Fill price in USD
    size: float                 # Size in contracts
    timestamp: datetime         # When the order was filled
    order_type: str             # "base" or "dca"


@dataclass
class PaperDeal:
    """Represents a complete paper trading deal with all its orders."""
    id: str                     # Unique deal ID
    bot_name: str               # Which bot started this deal
    symbol: str                 # Trading pair
    side: str                   # "long" or "short"
    leverage: int               # Leverage used
    orders: list[PaperOrder] = field(default_factory=list)
    is_open: bool = True
    opened_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    closed_at: Optional[datetime] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None  # "tp", "sl", "manual"
    pnl_btc: float = 0.0
    pnl_pct: float = 0.0

    # Structured trigger metadata — what fired the entry / exit.
    # entry_trigger: {"group_id": int, "group_name": str, "indicators": list[str]}
    #   or {"group_name": "ASAP", ...} for bypass filters. None for manual opens.
    # exit_trigger: {"type": "price_tp"|"price_sl"|"indicator_tp"|"timeout"|
    #                        "manual"|"cancelled", ...}
    entry_trigger: Optional[dict] = None
    exit_trigger: Optional[dict] = None

    # Trailing stop peak price — stored in dataclass so it is part of the
    # deal object and survives serialization to/from state JSON.
    # 0.0 = not yet initialized; set to entry price on first tick.
    _peak_price: float = field(default=0.0, repr=False)

    # Per-deal overrides written by the portal's PATCH /api/bots/{slug}/deals/{id}
    # endpoint via a sentinel file. None = use bot-level config; a dict overrides.
    _tp_override: Optional[dict] = field(default=None, repr=False)
    _sl_override: Optional[dict] = field(default=None, repr=False)
    _dca_enabled: bool = field(default=True, repr=False)

    # Highest / lowest TICK-PRICE observed since this deal was opened.
    # Used by wick-simulation in paper trading so TP / SL / trailing-stop
    # checks only fire on wicks that happened AFTER the deal opened —
    # pre-existing wicks in the same forming candle do not retroactively
    # count.
    #
    # Rationale: the old code compared TP/SL targets against the
    # FORMING candle's full wick-high/low (via ``_wick_high_low``). In
    # live paper trading the tick loop polls faster than candles close,
    # so a deal opening mid-candle inherited the candle's pre-existing
    # wick as if it had lived through it — the "rapid-fire TP cycle"
    # bug observed on the RSI real-test bot (2026-04-xx).
    #
    # Only relevant for PAPER trading. Backtest still processes one
    # candle per step with the candle's full wick — there, per-deal
    # tracking adds no value because deals and candles advance in
    # lockstep.
    #
    # Initialisation: ``__post_init__`` sets both to ``avg_entry_price``
    # on freshly-constructed deals (so the trackers carry the entry
    # price as the "no ticks observed yet" baseline). ``dict_to_deal``
    # in paper.state_io restores any persisted value, falling back to
    # ``avg_entry_price`` for deals loaded from a pre-fix state file.
    _wick_high_since_open: float = field(default=0.0, repr=False)
    _wick_low_since_open: float = field(default=0.0, repr=False)

    def __post_init__(self):
        # Sentinel-default → seed from entry price. We check for 0.0
        # (not None) because dataclass + frozen=False + init=True
        # semantics only give us the float default; treating 0.0 as
        # "not yet set" is safe because a real deal never opens at 0.0
        # (avg_entry_price would be meaningless there).
        if self._wick_high_since_open == 0.0 and self.orders:
            self._wick_high_since_open = self.avg_entry_price
        if self._wick_low_since_open == 0.0 and self.orders:
            self._wick_low_since_open = self.avg_entry_price

    @property
    def total_size(self) -> float:
        """Total position size across all orders."""
        return sum(o.size for o in self.orders)

    @property
    def avg_entry_price(self) -> float:
        """Volume-weighted average entry price across all orders."""
        if not self.orders:
            return 0.0
        total_value = sum(o.price * o.size for o in self.orders)
        return total_value / self.total_size

    @property
    def dca_count(self) -> int:
        """Number of DCA orders placed so far."""
        return len([o for o in self.orders if o.order_type == "dca"])

    def calculate_pnl(self, current_price: float) -> tuple[float, float]:
        """
        Calculate unrealized PnL for Bitget BTCUSD inverse perpetual.

        On Bitget, position size is expressed in BTC. PT-v2 audit
        pt-043 flagged this formula as potentially incorrect for
        inverse perpetual; operator validated against Bitget testnet
        on 2026-04-28 (LONG + SHORT, 1x leverage, 0.1 BTC, 4-hour
        positions). Bitget's actual formula matches:

            PnL (BTC) = size * (current - entry) / current * leverage   [long]
            PnL (BTC) = size * (entry - current) / current * leverage   [short]

        Note: denominator is ``current_price``, NOT ``entry``. Pre-fix
        the formula used ``entry`` as denominator (the linear-perpetual
        shape), which produced ~1% under-statement per percent of
        price movement (e.g. a 5% move yielded a ~5% PnL error).

        Testnet validation reference (2026-04-28):
          * LONG  76801.10 → 76108.30, 0.1 BTC, 1x:
              Bitget reported -0.00090973 BTC,
              Reverto post-fix -0.00091028 BTC, match within 0.06 %.
          * SHORT 76806.00 → 76113.70, 0.1 BTC, 1x:
              Bitget reported +0.00090914 BTC,
              Reverto post-fix +0.00090956 BTC, match within 0.05 %.

        ``pnl_pct`` is the return as a percentage of the margin
        (initial BTC committed). With no leverage: margin = size (full
        BTC value at entry). With leverage N: margin = size / N.

        Returns (pnl_btc, pnl_pct).
        """
        # Guard up-front: missing orders OR a non-positive current
        # price both leave the formula undefined. The current_price
        # guard is load-bearing post-fix because ``current_price`` is
        # now the denominator on both branches; a ZeroDivisionError
        # here would crash the tick loop.
        if not self.orders or current_price <= 0:
            return 0.0, 0.0

        avg  = self.avg_entry_price
        size = self.total_size

        # Belt-and-braces: avg_entry_price returns 0.0 for an empty
        # orders list (covered above), but we keep this guard so a
        # corrupt state with all-zero order prices can't bleed a
        # division-by-zero through pnl_pct's margin calc.
        if avg <= 0:
            return 0.0, 0.0

        if self.side == "long":
            pnl_btc = size * (current_price - avg) / current_price * self.leverage
        else:
            pnl_btc = size * (avg - current_price) / current_price * self.leverage

        # Margin = BTC committed = size / leverage
        margin_btc = size / self.leverage
        pnl_pct = (pnl_btc / margin_btc) * 100

        return pnl_btc, pnl_pct


class PaperState:
    """
    In-memory state manager for paper trading.
    Tracks balance, open deals and closed deal history.

    Thread-safe: open_deals and balance_btc are protected by a Lock.
    """

    def __init__(self, initial_balance_btc: float = 0.1):
        self._lock = threading.Lock()
        self.balance_btc = initial_balance_btc
        self.initial_balance_btc = initial_balance_btc
        self.open_deals: dict[str, PaperDeal] = {}
        self.closed_deals: list[PaperDeal] = []

    def new_deal_id(self) -> str:
        """Generate a globally-unique deal ID (YYYYMMDDHHMM-RRRR).

        Replaces the old per-instance ``PAPER-NNNN`` counter. The
        per-bot counter silently collided across bots — two engines
        both starting at 0001 produced the same id, and the DB's
        INSERT OR REPLACE clobbered one row with the other (cross-bot
        deal-id collision bug, fixed 2026-04-19). The new ID is
        globally unique, time-sortable as a string, and validated
        through ``core.ids.DEAL_ID_RE`` at every ingress boundary.
        Lock-free: the generator is stateless, so no counter contention
        across the engine's notify + monitor threads.
        """
        from core.ids import generate_deal_id
        return generate_deal_id()

    def open_deal(self, deal: PaperDeal):
        """Register a new open deal."""
        with self._lock:
            self.open_deals[deal.id] = deal

    def close_deal(self, deal_id: str, close_price: float,
                   reason: str) -> Optional[PaperDeal]:
        """Close a deal and move it to history."""
        with self._lock:
            deal = self.open_deals.pop(deal_id, None)
            if not deal:
                return None

            deal.is_open = False
            deal.closed_at = datetime.now(UTC)
            deal.close_price = close_price
            deal.close_reason = reason
            deal.pnl_btc, deal.pnl_pct = deal.calculate_pnl(close_price)

            self.balance_btc += deal.pnl_btc
            self.closed_deals.append(deal)
            return deal

    def get_open_deals_snapshot(self) -> dict[str, PaperDeal]:
        """
        Return a shallow copy of open_deals for safe iteration.
        Use this when iterating deals in the engine to avoid holding
        the lock during the full monitoring loop.
        """
        with self._lock:
            return dict(self.open_deals)

    def get_closed_deals_snapshot(self) -> list[PaperDeal]:
        """
        Return a shallow copy of closed_deals for safe iteration.
        closed_deals is mutated by close_deal() inside the lock.
        Reading it outside the lock risks a RuntimeError on list resize.
        """
        with self._lock:
            return list(self.closed_deals)

    def total_pnl_btc(self) -> float:
        """Total realized PnL across all closed deals. Uses a snapshot for safety."""
        snap = self.get_closed_deals_snapshot()
        return sum(d.pnl_btc for d in snap)

    def summary(self) -> dict:
        """Returns a summary of current paper trading performance."""
        # Take a single consistent snapshot for all derived values
        closed_snap = self.get_closed_deals_snapshot()
        total_pnl   = sum(d.pnl_btc for d in closed_snap)
        win_rate    = self._win_rate(closed_snap)
        with self._lock:
            return {
                "balance_btc":         round(self.balance_btc, 8),
                "initial_balance_btc": self.initial_balance_btc,
                "total_pnl_btc":       total_pnl,
                "open_deals":          len(self.open_deals),
                "closed_deals":        len(closed_snap),
                "win_rate":            win_rate,
            }

    def _win_rate(self, closed_snap: list[PaperDeal] = None) -> float:
        """
        Calculate win rate across all closed deals.
        Accepts an optional pre-fetched snapshot to avoid double locking.
        """
        snap = closed_snap if closed_snap is not None else self.get_closed_deals_snapshot()
        if not snap:
            return 0.0
        wins = len([d for d in snap if d.pnl_btc > 0])
        return round((wins / len(snap)) * 100, 2)
