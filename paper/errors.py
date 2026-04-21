# paper/errors.py
# Structured error context for API failures in the paper engine tick loop.
# Collected once per exception so logging + Telegram notifications render
# the same severity / transient-vs-persistent / actionable-detail view.

from __future__ import annotations

from dataclasses import dataclass

# ccxt exception class names that represent a retry-worthy failure. Using
# string names rather than class references keeps this module importable
# in test environments that stub ccxt, and lets mock exceptions with a
# matching class name classify correctly.
_TRANSIENT_CLASS_NAMES: frozenset[str] = frozenset({
    "RateLimitExceeded",
    "NetworkError",
    "DDoSProtection",
    "ExchangeNotAvailable",
    "RequestTimeout",
    "OnMaintenance",
})

# Conceptual HTTP status per ccxt class. ccxt does not consistently expose
# `http_status` on exception instances, so we hardcode the status that
# maps to each error-class's semantics. Classes without an intrinsic
# status (generic NetworkError, connection refused, etc.) stay None.
_STATUS_CODE_BY_CLASS: dict[str, int] = {
    "RateLimitExceeded": 429,
    "AuthenticationError": 401,
    "OnMaintenance": 503,
}

# Truncate free-form exception text to bound the log / Telegram payload.
# ccxt errors can embed full response bodies which may contain request-URL
# fragments or rate-limit payloads we don't want verbatim on an external
# channel.
_MESSAGE_CHAR_CAP = 200


@dataclass(frozen=True)
class TickerError:
    """Structured context for an exchange-API failure during a bot tick.

    Built from a raised exception by ``classify_exception`` and then
    consumed by structured logging + Telegram notifications. Holding the
    classification in one place keeps the "transient vs persistent" +
    severity decisions consistent across the log line and the user-
    facing message.
    """
    exchange: str
    endpoint: str
    symbol: str
    status_code: int | None
    error_class: str
    message: str
    retry_attempt: int
    max_retries: int
    is_transient: bool


def classify_exception(
    exc: BaseException,
    *,
    exchange: str,
    endpoint: str,
    symbol: str,
    retry_attempt: int,
    max_retries: int,
) -> TickerError:
    """Map a raised exception to a ``TickerError``.

    Classification walks the exception's MRO so subclasses of known ccxt
    transient bases (e.g. ``DDoSProtection`` extends ``NetworkError``)
    still resolve to ``is_transient=True``. Anything outside the known
    transient set — including non-ccxt exceptions like ``ValueError`` or
    ``AttributeError`` from our own code — is conservatively classified
    as persistent so a silent notification-suppress can't mask a real
    bug.
    """
    cls_name = type(exc).__name__
    is_transient = any(
        parent.__name__ in _TRANSIENT_CLASS_NAMES
        for parent in type(exc).__mro__
    )
    status_code = _STATUS_CODE_BY_CLASS.get(cls_name)
    if status_code is None:
        attr = getattr(exc, "http_status", None)
        if isinstance(attr, int):
            status_code = attr
    msg = str(exc)[:_MESSAGE_CHAR_CAP]
    return TickerError(
        exchange=exchange,
        endpoint=endpoint,
        symbol=symbol,
        status_code=status_code,
        error_class=cls_name,
        message=msg,
        retry_attempt=retry_attempt,
        max_retries=max_retries,
        is_transient=is_transient,
    )


def format_log_line(err: TickerError, *, bot: str) -> str:
    """Render a ``TickerError`` as a single-line structured log entry.

    Single-line format is chosen so ``grep status=429`` or
    ``grep transient=yes`` works directly against portal.log. The
    message field is the only free-form text and is double-quoted +
    newline-scrubbed so embedded spaces don't fragment the key=value
    parse.
    """
    status = err.status_code if err.status_code is not None else "n/a"
    transient = "yes" if err.is_transient else "no"
    safe_msg = err.message.replace('"', "'").replace("\n", " ")
    return (
        f"bot={bot} exchange={err.exchange} endpoint={err.endpoint} "
        f"symbol={err.symbol} status={status} class={err.error_class} "
        f"retry={err.retry_attempt}/{err.max_retries} "
        f"transient={transient} message=\"{safe_msg}\""
    )
