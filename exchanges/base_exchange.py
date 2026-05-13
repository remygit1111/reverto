# exchanges/base_exchange.py
# Abstract base class that defines the interface all exchanges must implement.
# Any exchange added to Reverto must inherit from this class.

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


# ── Domain exceptions ────────────────────────────────────────────────────────
# Catch-these classes let callers (engine, tests, portal) distinguish
# between the actionable error modes without depending on ccxt's exception
# hierarchy directly. Exchange implementations translate ccxt errors
# into these at the boundary so the rest of Reverto stays framework-agnostic.

class ExchangeError(Exception):
    """Base class for all exchange-layer errors surfaced to Reverto."""


class InsufficientFundsError(ExchangeError):
    """Order rejected because the margin/balance is too low."""


class RateLimitError(ExchangeError):
    """Exchange refused the request due to rate-limit saturation
    after the built-in retry budget has been exhausted."""


class ExchangeNetworkError(ExchangeError):
    """Transient network or DNS failure reaching the exchange."""


@dataclass
class Position:
    """Represents an open position on an exchange."""
    symbol: str
    side: str                    # "long" or "short"
    size: float                  # Position size in contracts
    entry_price: float           # Average entry price in USD
    mark_price: float            # Current mark price in USD
    liquidation_price: float     # Liquidation price in USD
    unrealized_pnl: float        # Unrealized PnL in BTC (inverse contract)
    leverage: int                # Current leverage
    margin: float                # Margin used in BTC


@dataclass
class Order:
    """Represents a placed order."""
    id: str
    symbol: str
    side: str                    # "buy" or "sell"
    type: str                    # "market" or "limit"
    amount: float                # Order size in contracts
    price: Optional[float]       # None for market orders
    status: str                  # "open", "closed", "cancelled"
    filled: float                # Amount filled so far
    timestamp: int               # Unix timestamp in ms


@dataclass
class Ticker:
    """Represents current market price data."""
    symbol: str
    bid: float
    ask: float
    last: float
    mark_price: Optional[float]
    funding_rate: Optional[float]
    timestamp: int


class BaseExchange(ABC):
    """
    Abstract base class for all Reverto exchange integrations.
    Every exchange must implement these methods.

    ``market_type`` (Bitget spot/coin_m/usdt_m/usdc_m, Kraken
    spot/futures) selects which wallet the underlying ccxt client
    routes to. Subclasses pull the right ``ccxt_options`` /
    ``ccxt_params`` / ``balance_currency`` from
    ``core.markets.get_market_config`` and store the resolved
    settlement currency on ``self._balance_currency`` so
    ``get_balance`` reads from the right key in
    ``fetch_balance()``'s response.
    """

    def __init__(self, api_key: str, api_secret: str, paper: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper = paper
        # Subclasses overwrite these via core.markets.get_market_config
        # during their own __init__. Default values keep tests that
        # construct a mock without going through a real subclass from
        # AttributeError'ing on access.
        self.market_type: str = ""
        self._balance_currency: str = "BTC"

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    @abstractmethod
    def get_ticker(self, symbol: str) -> Ticker:
        """Get current price and funding rate for a symbol."""
        pass

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list:
        """
        Fetch OHLCV candles.
        Returns list of [timestamp, open, high, low, close, volume]
        """
        pass

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]:
        """Get current open position for a symbol. Returns None if no position."""
        pass

    @abstractmethod
    def get_balance(self) -> float:
        """Get available balance in the wallet's native settlement
        currency. Coin-margined inverse contracts and Kraken Futures
        return BTC; Bitget spot / USDT-M return USDT; Bitget USDC-M
        returns USDC; Kraken spot returns USD. The exact currency
        read from ``ccxt.fetch_balance()`` is in
        ``self._balance_currency`` — see ``core.markets``."""
        pass

    @property
    def balance_currency(self) -> str:
        """Native settlement currency for ``get_balance()``. Set by
        subclasses from ``core.markets.get_market_config``."""
        return self._balance_currency

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    @abstractmethod
    def place_market_order(self, symbol: str, side: str, amount: float) -> Order:
        """Place a market order. Side is 'buy' or 'sell'."""
        pass

    @abstractmethod
    def place_limit_order(self, symbol: str, side: str, amount: float, price: float) -> Order:
        """Place a limit order."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order. Returns True if successful."""
        pass

    @abstractmethod
    def get_open_orders(self, symbol: str) -> list[Order]:
        """Get all open orders for a symbol."""
        pass

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol. Returns True if successful."""
        pass

    # ------------------------------------------------------------------
    # Shared utilities (not abstract — same for all exchanges)
    # ------------------------------------------------------------------

    def calculate_liquidation_distance_pct(self, position: Position) -> float:
        """
        Calculate the percentage distance between current mark price
        and the liquidation price. Used by the LiquidationGuard.
        """
        if position.liquidation_price <= 0:
            return 100.0

        distance = abs(position.mark_price - position.liquidation_price)
        return round((distance / position.mark_price) * 100, 2)

    def is_paper(self) -> bool:
        """Returns True if this exchange is running in paper trading mode."""
        return self.paper

    def __repr__(self):
        mode = "PAPER" if self.paper else "LIVE"
        return f"{self.__class__.__name__}({mode})"
