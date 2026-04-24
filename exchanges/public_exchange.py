# exchanges/public_exchange.py
# Fetches public market data without API keys.
# Used by the paper engine and backtester to get real prices.

import ccxt
import logging
from exchanges.base_exchange import BaseExchange, Ticker, Position, Order
from core.circuit_breaker import CircuitBreaker, CircuitOpenError
from typing import Optional

logger = logging.getLogger(__name__)

# Audit r1-057: one breaker per upstream exchange so a Bitget
# outage doesn't trip the Kraken path too. Module-scope so every
# PublicExchange instance for the same exchange name shares the
# same state — otherwise each instance would have its own counter
# and the breaker would never trip under load.
_BREAKERS: dict[str, CircuitBreaker] = {}


def _breaker_for(exchange_name: str) -> CircuitBreaker:
    b = _BREAKERS.get(exchange_name)
    if b is None:
        b = CircuitBreaker(
            name=f"public-{exchange_name}",
            failure_threshold=5,
            cooldown_seconds=60.0,
        )
        _BREAKERS[exchange_name] = b
    return b


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
        """Fetch current price data for a symbol.

        Wrapped in the per-exchange circuit breaker (audit r1-057).
        ``CircuitOpenError`` surfaces when the upstream has failed
        too many times in a row — callers should translate to 503.
        """
        breaker = _breaker_for(self.exchange_name)
        if breaker.is_open():
            raise CircuitOpenError(
                f"{self.exchange_name} public ticker breaker open",
            )
        try:
            data = self.client.fetch_ticker(self._symbol(symbol))
        except Exception:
            breaker.record_failure()
            raise
        breaker.record_success()

        # mark_price comes as a string from Bitget's info dict (e.g. "71027.5").
        # Convert defensively: fall back to None if absent, empty, or unparseable.
        raw_mark = data.get("info", {}).get("markPrice")
        try:
            mark_price = float(raw_mark) if raw_mark else None
        except (ValueError, TypeError):
            mark_price = None

        return Ticker(
            symbol=symbol,
            bid=data["bid"] or 0.0,
            ask=data["ask"] or 0.0,
            last=data["last"] or 0.0,
            mark_price=mark_price,
            funding_rate=data.get("info", {}).get("fundingRate"),
            timestamp=data["timestamp"]
        )

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        """Fetch OHLCV candles for a symbol.

        Same breaker as get_ticker (audit r1-057) — one sustained
        outage trips both paths, preventing retry-storms on the
        chart + candles endpoints.
        """
        breaker = _breaker_for(self.exchange_name)
        if breaker.is_open():
            raise CircuitOpenError(
                f"{self.exchange_name} public ohlcv breaker open",
            )
        try:
            result = self.client.fetch_ohlcv(
                self._symbol(symbol), timeframe, limit=limit,
            )
        except Exception:
            breaker.record_failure()
            raise
        breaker.record_success()
        return result

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
