# exchanges/public_exchange.py
# Fetches public market data without API keys.
# Used by the paper engine and backtester to get real prices.

import ccxt
import logging
from exchanges.base_exchange import BaseExchange, Ticker, Position, Order
from typing import Optional

logger = logging.getLogger(__name__)


class PublicExchange(BaseExchange):
    """
    Read-only exchange connection for public market data.
    No API key required — used for paper trading and backtesting.
    Supports Bitget and Kraken via ccxt.
    """

    SYMBOL_MAPS = {
        "bitget": {
            "BTC/USD": "BTCUSD"
        },
        "kraken": {
            "BTC/USD": "BTC/USD:BTC"
        }
    }

    CLIENTS = {
        "bitget": ccxt.bitget,
        "kraken": ccxt.krakenfutures
    }

    def __init__(self, exchange_name: str):
        super().__init__("", "", paper=True)
        self.exchange_name = exchange_name.lower()

        if self.exchange_name not in self.CLIENTS:
            raise ValueError(f"Unsupported exchange: {exchange_name}. Choose 'bitget' or 'kraken'")

        self.client = self.CLIENTS[self.exchange_name]({
            "options": {"defaultType": "swap"}
        })

        logger.info(f"PublicExchange initialized: {self.exchange_name}")

    def _symbol(self, symbol: str) -> str:
        """Convert Reverto symbol to ccxt unified symbol."""
        return self.SYMBOL_MAPS.get(self.exchange_name, {}).get(symbol, symbol)

    def get_ticker(self, symbol: str) -> Ticker:
        """Fetch current price data for a symbol."""
        data = self.client.fetch_ticker(self._symbol(symbol))
        return Ticker(
            symbol=symbol,
            bid=data["bid"] or 0.0,
            ask=data["ask"] or 0.0,
            last=data["last"] or 0.0,
            mark_price=data.get("info", {}).get("markPrice"),
            funding_rate=data.get("info", {}).get("fundingRate"),
            timestamp=data["timestamp"]
        )

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        """Fetch OHLCV candles for a symbol."""
        return self.client.fetch_ohlcv(
            self._symbol(symbol), timeframe, limit=limit
        )

    # ------------------------------------------------------------------
    # Not supported without API key — raise clear errors
    # ------------------------------------------------------------------

    def get_position(self, symbol: str) -> Optional[Position]:
        raise NotImplementedError("get_position requires API key")

    def get_balance(self) -> float:
        raise NotImplementedError("get_balance requires API key")

    def place_market_order(self, symbol: str, side: str, amount: float) -> Order:
        raise NotImplementedError("place_market_order requires API key")

    def place_limit_order(self, symbol: str, side: str, amount: float, price: float) -> Order:
        raise NotImplementedError("place_limit_order requires API key")

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        raise NotImplementedError("cancel_order requires API key")

    def get_open_orders(self, symbol: str) -> list[Order]:
        raise NotImplementedError("get_open_orders requires API key")

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        raise NotImplementedError("set_leverage requires API key")