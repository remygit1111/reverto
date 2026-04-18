# exchanges/bitget.py
# Bitget exchange implementation using ccxt.
# Handles inverse perpetual BTC/USD contracts.

import logging
import time
import uuid
from typing import Optional

import ccxt

from exchanges.base_exchange import (
    BaseExchange,
    ExchangeNetworkError,
    InsufficientFundsError,
    Order,
    Position,
    RateLimitError,
    Ticker,
)

logger = logging.getLogger(__name__)


def _generate_client_order_id() -> str:
    """Generate an idempotency key for an order placement.

    Each physical order attempt reuses the SAME client_order_id across
    retries. That lets Bitget recognise a retried placement as
    duplicate-of-an-already-accepted-order instead of accepting both —
    the canonical fix for the 'rate-limited on confirmation → retry
    double-places' race.
    """
    return f"reverto-{uuid.uuid4().hex[:16]}"


def _with_order_retries(op_name: str, fn, *args, client=None, symbol=None, **kwargs):
    """Run a ccxt order call with exponential backoff on rate-limit
    errors and translate the terminal failure mode into a Reverto
    domain exception.

    Retry budget is 3 attempts (0.5s → 1.0s → fail) which matches the
    /api/candles fetcher and Bitget's observed recovery window for a
    fresh burst. NetworkError is surfaced immediately — retrying a
    DNS / socket timeout just piles up dangling orders.

    Idempotency: if ``client`` and ``symbol`` are passed, a
    ``clientOrderId`` is injected into ``params`` and reused across
    retries. Before each retry (attempt > 0) we check whether the
    exchange already has an order with that id and short-circuit the
    retry to return the existing order. This prevents the classic
    "rate-limited on confirmation → retry places a duplicate" race.
    """
    client_order_id: Optional[str] = None
    if client is not None:
        params = kwargs.get("params") or {}
        client_order_id = params.get("clientOrderId") or _generate_client_order_id()
        params["clientOrderId"] = client_order_id
        kwargs["params"] = params

    last_rate_err: Exception | None = None
    for attempt in range(3):
        try:
            # Idempotency check: before a retry, ask the exchange whether
            # it already has an order with our clientOrderId. If yes, the
            # previous attempt actually landed even though we never saw
            # the confirmation — return it instead of placing again.
            #
            # We require the response to be a dict with a recognised
            # status so stubbed tests (and exchanges that return empty
            # dicts for "not found") don't trigger a false positive.
            if attempt > 0 and client is not None and client_order_id and symbol:
                try:
                    existing = client.fetch_order(client_order_id, symbol)
                except Exception:
                    existing = None
                if (
                    isinstance(existing, dict)
                    and existing.get("status") in {"open", "closed", "filled", "partial"}
                ):
                    logger.warning(
                        "Bitget %s: idempotency hit for %s — returning existing order",
                        op_name, client_order_id,
                    )
                    return existing

            return fn(*args, **kwargs)
        except ccxt.InsufficientFunds as e:
            raise InsufficientFundsError(str(e)[:200]) from e
        except ccxt.RateLimitExceeded as e:
            last_rate_err = e
            if attempt == 2:
                break
            wait = 0.5 * (2 ** attempt)
            logger.warning(
                "Bitget %s rate-limited (attempt %d/3): %s — retry in %.1fs",
                op_name, attempt + 1, str(e)[:200], wait,
            )
            time.sleep(wait)
        except ccxt.NetworkError as e:
            raise ExchangeNetworkError(str(e)[:200]) from e
        except ccxt.BaseError as e:
            logger.error(
                "Bitget %s failed: %s", op_name, str(e)[:200],
            )
            raise
    raise RateLimitError(
        str(last_rate_err)[:200] if last_rate_err else "Bitget rate-limit exhausted"
    ) from last_rate_err


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
        data = self.client.fetch_ticker(self._symbol(symbol)) or {}
        # Safe dict access — a malformed ccxt response (or an exchange
        # API change) previously crashed with KeyError on data["bid"].
        # Every top-level field now defaults to 0.0 / None on missing.
        info = data.get("info") or {}
        return Ticker(
            symbol=symbol,
            bid=float(data.get("bid") or 0.0),
            ask=float(data.get("ask") or 0.0),
            last=float(data.get("last") or 0.0),
            mark_price=info.get("markPrice"),
            funding_rate=info.get("fundingRate"),
            timestamp=data.get("timestamp") or 0,
        )

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list:
        """Fetch OHLCV candles. Bitget's inverse-swap endpoint caps a
        single request at 200 bars — the ccxt wrapper accepts larger
        limits but silently truncates, which produced spurious gaps in
        backtest fetches. Defaulting to 200 keeps us inside the exchange
        contract without needing pagination for the common case."""
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
        raw = _with_order_retries(
            "place_market_order",
            self.client.create_order,
            self._symbol(symbol), "market", side, amount,
            client=self.client, symbol=self._symbol(symbol),
        )
        return self._parse_order(raw, symbol)

    def place_limit_order(self, symbol: str, side: str, amount: float, price: float) -> Order:
        raw = _with_order_retries(
            "place_limit_order",
            self.client.create_order,
            self._symbol(symbol), "limit", side, amount, price,
            client=self.client, symbol=self._symbol(symbol),
        )
        return self._parse_order(raw, symbol)

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            _with_order_retries(
                "cancel_order",
                self.client.cancel_order,
                order_id, self._symbol(symbol),
            )
            return True
        except (InsufficientFundsError, RateLimitError, ExchangeNetworkError) as e:
            logger.warning(
                "Bitget cancel_order failed for %s: %s",
                order_id, str(e)[:200],
            )
            return False
        except ccxt.BaseError as e:
            # Truncate at 200 chars so a verbose ccxt/exchange stack
            # frame can never flood the log line (or worse, echo a
            # token fragment through a downstream operator dashboard).
            logger.warning(
                "Bitget cancel_order failed for %s: %s",
                order_id, str(e)[:200],
            )
            return False

    def get_open_orders(self, symbol: str) -> list[Order]:
        raw_orders = self.client.fetch_open_orders(self._symbol(symbol))
        return [self._parse_order(o, symbol) for o in raw_orders]

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self.client.set_leverage(leverage, self._symbol(symbol))
            return True
        except Exception as e:
            logger.warning(
                "Bitget set_leverage failed for %s: %s",
                symbol, str(e)[:200],
            )
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
