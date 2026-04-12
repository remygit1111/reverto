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
        Calculate unrealized PnL for an inverse perpetual contract.
        For inverse contracts: PnL (BTC) = size * (1/entry - 1/current)
        Leverage is applied as a multiplier on the base PnL.
        Returns (pnl_btc, pnl_pct)
        """
        if not self.orders or current_price <= 0:
            return 0.0, 0.0

        avg = self.avg_entry_price
        size = self.total_size

        if self.side == "long":
            pnl_btc = size * (1 / avg - 1 / current_price) * self.leverage
        else:
            pnl_btc = size * (1 / current_price - 1 / avg) * self.leverage

        margin_btc = size / avg
        pnl_pct = (pnl_btc / margin_btc) * 100
        return round(pnl_btc, 10), round(pnl_pct, 4)


class PaperState:
    """
    In-memory state manager for paper trading.
    Tracks balance, open deals and closed deal history.

    Thread-safe: open_deals and balance_btc are protected by a Lock.
    This allows the LiquidationGuard background thread to safely read
    positions without racing against the main engine loop.
    """

    def __init__(self, initial_balance_btc: float = 0.1):
        self._lock = threading.Lock()
        self.balance_btc = initial_balance_btc
        self.initial_balance_btc = initial_balance_btc
        self.open_deals: dict[str, PaperDeal] = {}
        self.closed_deals: list[PaperDeal] = []
        self._deal_counter = 0

    def new_deal_id(self) -> str:
        """Generate a unique deal ID."""
        with self._lock:
            self._deal_counter += 1
            return f"PAPER-{self._deal_counter:04d}"

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

            # Update balance
            self.balance_btc += deal.pnl_btc
            self.closed_deals.append(deal)
            return deal

    def get_open_deals_snapshot(self) -> dict[str, PaperDeal]:
        """
        Return a shallow copy of open_deals for safe iteration.
        Use this when iterating deals in the engine to avoid
        holding the lock during the full monitoring loop.
        """
        with self._lock:
            return dict(self.open_deals)

    def total_pnl_btc(self) -> float:
        """Total realized PnL across all closed deals."""
        return round(sum(d.pnl_btc for d in self.closed_deals), 10)

    def summary(self) -> dict:
        """Returns a summary of current paper trading performance."""
        with self._lock:
            return {
                "balance_btc": round(self.balance_btc, 8),
                "initial_balance_btc": self.initial_balance_btc,
                "total_pnl_btc": round(self.total_pnl_btc(), 8),
                "open_deals": len(self.open_deals),
                "closed_deals": len(self.closed_deals),
                "win_rate": self._win_rate(),
            }

    def _win_rate(self) -> float:
        """Calculate win rate across all closed deals."""
        if not self.closed_deals:
            return 0.0
        wins = len([d for d in self.closed_deals if d.pnl_btc > 0])
        return round((wins / len(self.closed_deals)) * 100, 2)
