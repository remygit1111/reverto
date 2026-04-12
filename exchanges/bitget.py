# exchanges/bitget.py
# Bitget exchange implementation using ccxt.
# Handles inverse perpetual BTC/USD contracts.

import ccxt
from typing import Optional
from exchanges.base_exchange import BaseExchange, Position, Order, Ticker


class BitgetExchange(BaseExchange):
    """
    Bitget implementation for Reverto.
    Uses ccxt to connect to Bitget's inverse perpetual futures API.
    """

    SYMBOL_MAP = {
        "BTC/USD": "BTC/USD:BTC"  # ccxt unified symbol for inverse perpetual
    }

    def __init__(self, api_key: str, api_secret: str, passphrase: str, paper: bool = False):
        super().__init__(api_key, api_secret, paper)

        self.client = ccxt.bitget({
            "apiKey": api_key,
            "secret": api_secret,
            "password": passphrase,
            "options": {
                "defaultType": "swap",
            }
        })

        if paper:
            self.client.set_sandbox_mode(True)

    def _symbol(self, symbol: str) -> str:
        """Convert simple symbol to ccxt unified symbol."""
        return self.SYMBOL_MAP.get(symbol, symbol)

    def get_ticker(self, symbol: str) -> Ticker:
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
        return self.client.fetch_ohlcv(self._symbol(symbol), timeframe, limit=limit)

    def get_position(self, symbol: str) -> Optional[Position]:
        positions = self.client.fetch_positions([self._symbol(symbol)])
        for p in positions:
            if p["contracts"] and p["contracts"] > 0:
                return Position(
                    symbol=symbol,
                    side=p["side"],
                    size=p["contracts"],
                    entry_price=p["entryPrice"] or 0.0,
                    mark_price=p["markPrice"] or 0.0,
                    liquidation_price=p["liquidationPrice"] or 0.0,
                    unrealized_pnl=p["unrealizedPnl"] or 0.0,
                    leverage=int(p["leverage"] or 1),
                    margin=p["initialMargin"] or 0.0
                )
        return None

    def get_balance(self) -> float:
        balance = self.client.fetch_balance()
        return balance.get("BTC", {}).get("free", 0.0)

    def place_market_order(self, symbol: str, side: str, amount: float) -> Order:
        raw = self.client.create_order(
            self._symbol(symbol), "market", side, amount
        )
        return self._parse_order(raw, symbol)

    def place_limit_order(self, symbol: str, side: str, amount: float, price: float) -> Order:
        raw = self.client.create_order(
            self._symbol(symbol), "limit", side, amount, price
        )
        return self._parse_order(raw, symbol)

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            self.client.cancel_order(order_id, self._symbol(symbol))
            return True
        except Exception:
            return False

    def get_open_orders(self, symbol: str) -> list[Order]:
        raw_orders = self.client.fetch_open_orders(self._symbol(symbol))
        return [self._parse_order(o, symbol) for o in raw_orders]

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self.client.set_leverage(leverage, self._symbol(symbol))
            return True
        except Exception:
            return False

    def _parse_order(self, raw: dict, symbol: str) -> Order:
        """Convert ccxt order dict to Reverto Order dataclass."""
        return Order(
            id=raw["id"],
            symbol=symbol,
            side=raw["side"],
            type=raw["type"],
            amount=raw["amount"] or 0.0,
            price=raw.get("price"),
            status=raw["status"],
            filled=raw.get("filled") or 0.0,
            timestamp=raw["timestamp"] or 0
        )