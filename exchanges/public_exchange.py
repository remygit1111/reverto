# exchanges/public_exchange.py
# Fetches public market data without API keys.
# Used by the paper engine and backtester to get real prices.

import ccxt
import logging
from exchanges.base_exchange import BaseExchange, Ticker, Position, Order
from core.circuit_breaker import CircuitBreaker, CircuitOpenError
from typing import Optional

logger = logging.getLogger(__name__)

# Audit pt-038 / pt-055 / r2-005: ccxt exception classes that are
# NOT self-healing — a verlopen API key, a revoked permission, an
# account suspended by the exchange, or a typo'd pair name will
# never resolve on its own. Treating these as transient (the pre-
# fix behaviour) ran them through the threshold-based ``5 fails →
# 60s cooldown → probe → fail → reopen`` cycle indefinitely; the
# wrong remedy for a non-self-healing condition.
#
# Conservative bias: when in doubt, classify as transient. A
# false-transient leads to noisy retries (recoverable); a false-
# permanent pages an operator at 03:00 for nothing (annoying and
# erodes trust in the alert).
#
# Hierarchy notes (verified against the installed ccxt):
#   * AuthenticationError → ExchangeError. PermissionDenied and
#     AccountSuspended are subclasses of AuthenticationError, so
#     ``isinstance(exc, AuthenticationError)`` would catch all
#     three; listing them explicitly here is documentation-as-
#     code so a future reader sees the full set.
#   * BadSymbol → BadRequest → ExchangeError. Pair-typo class.
#   * NetworkError, RateLimitExceeded, OnMaintenance,
#     DDoSProtection, RequestTimeout all live under NetworkError
#     and stay TRANSIENT — the network can come back, the rate
#     window expires, maintenance ends.
_PERMANENT_CCXT_ERRORS: tuple[type[Exception], ...] = (
    ccxt.AuthenticationError,
    ccxt.PermissionDenied,
    ccxt.AccountSuspended,
    ccxt.BadSymbol,
)


def _is_permanent_error(exc: BaseException) -> bool:
    """Classify an exception as permanent (operator-action required)
    vs transient (self-healing).

    Returns True for ``ccxt.AuthenticationError`` and its
    subclasses + ``ccxt.BadSymbol``; False for everything else,
    including non-ccxt exceptions (programming bugs,
    KeyboardInterrupt, etc.). The conservative-bias default
    means a bug in our own code never permanently trips the
    breaker.

    KRITIEK: this is the single owner of the permanent / transient
    mapping. If a future ccxt version adds new auth-related
    exception classes, update ``_PERMANENT_CCXT_ERRORS``; the
    breaker primitive itself stays domain-agnostic.
    """
    return isinstance(exc, _PERMANENT_CCXT_ERRORS)


def _make_permanent_open_callback(exchange_name: str):
    """Build a one-shot callback that delegates to the LiveProvider
    when the breaker for ``exchange_name`` first transitions into
    PERMANENT_OPEN.

    The actual fan-out (Telegram broadcast to every connected user)
    is owned by the live provider — BuiltinLiveProvider in Phase 2,
    the real reverto-live plugin from Phase 3 onwards. If no provider
    is available the framework logs a CRITICAL message so operators
    still see the breaker latched even when alerts are unreachable
    (Aanpak 3 — loud failure, never silent, for safety infra).

    The callback is idempotent at the breaker layer (see
    ``CircuitBreaker._enter_permanent_open``) — a verlopen API key
    triggers ONE provider call per service lifetime, not one per
    failed retry. Operator clears via ``breaker.reset()`` or by
    restarting after fixing the root cause.

    Phase 2 Task 2.8 moved the Telegram fan-out body from this file
    to BuiltinLiveProvider.on_breaker_permanent_open. See
    docs/task_2_8_design_analysis.md for the design rationale.
    """
    def _callback(breaker_name: str, reason: str) -> None:
        from core.plugin_loader import load_live_provider

        provider = load_live_provider()
        if provider is not None:
            try:
                provider.on_breaker_permanent_open(breaker_name, reason)
            except Exception:  # noqa: BLE001 — provider failure must
                # not propagate back into CircuitBreaker (the breaker
                # already wraps this in its own try/except, but a
                # contained log here is the actionable signal).
                logger.exception(
                    "CircuitBreaker '%s' permanent-open: provider "
                    "callback failed. Exchange: %s. Reason: %s",
                    breaker_name, exchange_name, reason,
                )
            return

        # No provider — CRITICAL so the latched breaker does not
        # disappear silently. Operator must watch logs in this case.
        logger.critical(
            "CRITICAL: CircuitBreaker '%s' for %s is PERMANENT OPEN "
            "but no LiveProvider is registered for fan-out. "
            "Reason: %s. Operator action: investigate API credentials "
            "or pair config; restart the service or call "
            "breaker.reset() to clear. Telegram alerts are NOT being "
            "sent for this breaker latch.",
            breaker_name, exchange_name, reason,
        )
    return _callback


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
            # Audit pt-038 / pt-055 / r2-005: notifier callback
            # for permanent-open transitions. Lazy notifier
            # construction inside the callback closure keeps
            # module import cheap + dev-mode-friendly.
            on_permanent_open=_make_permanent_open_callback(
                exchange_name,
            ),
        )
        _BREAKERS[exchange_name] = b
    return b


class PublicExchange(BaseExchange):
    """
    Read-only exchange connection for public market data.
    No API key required — used for paper trading and backtesting.
    Supports Bitget and Kraken via ccxt.

    THREAD-SAFETY (audit r1-068):
        ccxt clients are not thread-safe. ``web/app.py`` owns one
        module-level PublicExchange per exchange + serialises
        every call behind ``_price_lock`` via
        ``asyncio.to_thread``. Do NOT share a PublicExchange
        instance across OS threads without holding the same lock
        — see ``exchanges/bitget.py`` docstring for the full
        rationale. The wrapper's ``_breaker_for`` circuit
        breakers are module-scope-per-exchange and reuse the
        same ccxt client, so they sit under the same
        serialisation contract.
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
        except Exception as e:
            # Audit pt-038 / pt-055 / r2-005: classify before
            # reporting. Permanent errors (auth, bad-symbol) trip
            # the breaker into PERMANENT_OPEN immediately, bypassing
            # the threshold + cooldown auto-recovery. Non-ccxt
            # exceptions classify as transient by conservative
            # default — a bug in our code never trips permanent.
            breaker.record_failure(permanent=_is_permanent_error(e))
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
        except Exception as e:
            # Audit pt-038 / pt-055 / r2-005: classify before
            # reporting. Same contract as get_ticker above.
            breaker.record_failure(permanent=_is_permanent_error(e))
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
