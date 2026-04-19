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

        On Bitget, position size is expressed in BTC (not USD contracts).
        The correct PnL formula for a BTC-denominated size is:

            PnL (BTC) = size * (exit - entry) / entry * leverage   [long]
            PnL (BTC) = size * (entry - exit) / entry * leverage   [short]

        This is equivalent to: PnL = size * pct_change * leverage

        pnl_pct is the return as a percentage of the margin (initial BTC committed).
        With no leverage: margin = size (full BTC value at entry).
        With leverage N:  margin = size / N.

        Returns (pnl_btc, pnl_pct)
        """
        if not self.orders or current_price <= 0:
            return 0.0, 0.0

        avg  = self.avg_entry_price
        size = self.total_size

        # Guard tegen division-by-zero als orders allemaal price=0 hebben
        # (kan voorkomen bij corrupte state of bij niet-geïnitialiseerde deals).
        if avg <= 0:
            return 0.0, 0.0

        if self.side == "long":
            pnl_btc = size * (current_price - avg) / avg * self.leverage
        else:
            pnl_btc = size * (avg - current_price) / avg * self.leverage

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
