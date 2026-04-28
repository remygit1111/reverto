# paper/errors.py
# Structured error context for API failures in the paper engine tick loop.
# Collected once per exception so logging + Telegram notifications render
# the same severity / transient-vs-persistent / actionable-detail view.

from __future__ import annotations

import re
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


# Audit v27-12: pattern-based credential redaction for exchange-error
# strings. Applied BEFORE truncation so a credential at position 150
# can't survive the 200-char cap simply by being placed in the second
# half of the message. Patterns target the three credential shapes ccxt
# is most likely to surface in exception text:
#
#   1. Query-string credential params (``apiKey=...``, ``signature=...``).
#      ccxt occasionally embeds full request URLs in error messages,
#      and signed-request URLs carry the key + signature inline.
#   2. ``Authorization: Bearer <token>`` headers, which surface in some
#      auth-failure exception messages.
#   3. JWT-shaped tokens (three base64 segments dot-separated). Defence
#      in depth — Reverto doesn't currently use JWTs, but a future
#      exchange or middleware that does would have its tokens redacted
#      automatically.
#
# Replacement preserves the parameter name (``apiKey=[REDACTED]``) so
# the error message remains useful for debugging context — operators
# can still see "this was an apiKey-shaped failure" without seeing the
# key itself. Bearer / JWT matches collapse to a bare ``[REDACTED]``.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(api[_-]?key|secret|passphrase|signature|sign)=[^&\s]+",
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
)


def _redact_match(match: re.Match[str]) -> str:
    """Replace a credential match with a redacted form.

    For ``key=value`` shapes we keep the key on the left of the ``=``
    and replace only the value, so error context stays readable
    ("the apiKey param had a bad value") without exposing the secret.
    For shapes without an ``=`` (Bearer, JWT) we collapse the whole
    match to ``[REDACTED]``.
    """
    text = match.group(0)
    if "=" in text:
        key, _, _ = text.partition("=")
        return f"{key}=[REDACTED]"
    return "[REDACTED]"


def _redact_secrets(msg: str) -> str:
    """Strip credential-bearing fragments from a free-form error
    message before it lands in a log line or Telegram payload.

    Apply this BEFORE any truncation. Truncation alone is not a
    redaction — a 200-char cap leaves plenty of room for a full
    apiKey + signature pair in the second half of the string. The
    redaction-first ordering is what makes the cap safe.
    """
    for pattern in _CREDENTIAL_PATTERNS:
        msg = pattern.sub(_redact_match, msg)
    return msg


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
    # Redact credentials BEFORE truncation — see _redact_secrets
    # docstring for the ordering rationale (truncation-first would
    # leave a 200-char window in which a full apiKey + signature can
    # still survive into Telegram).
    msg = _redact_secrets(str(exc))[:_MESSAGE_CHAR_CAP]
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
