"""Have I Been Pwned (HIBP) password-breach check.

Used by ``/api/auth/change-password`` to refuse any new password that
appears in a public data-breach corpus. HIBP's k-anonymity protocol
(https://api.pwnedpasswords.com/range/{prefix}) guarantees the
plaintext never leaves this process:

  1. SHA-1 hash the password locally.
  2. Send only the first 5 hex chars of the hash to HIBP.
  3. HIBP returns ~500 suffix:count lines matching that prefix.
  4. We match locally — HIBP can't tell which full hash we asked about.

Fail-open contract
------------------
Any transport / parsing / timeout failure returns ``False`` (not
pwned). Reasons:

  * HIBP outages would otherwise block every password rotation and
    become a DoS vector against users who WANT to fix a weak
    password.
  * The check is an additive safety net on top of the 12-char
    minimum; it's not the only line of defence.
  * Silent-pass on failure is logged as a WARNING so operators can
    see the rate of HIBP unavailability in ``portal.log``.

Security invariants
-------------------
  * The plaintext password MUST NOT appear in any log line, exception
    message, or logger metadata. Only the 5-char prefix (which is
    what HIBP gets anyway) is safe to log.
  * No retries: a single-attempt budget bounds the latency impact of
    a slow HIBP response on the login / change-password path.
"""

from __future__ import annotations

import hashlib
import logging

import httpx

logger = logging.getLogger(__name__)

_HIBP_ENDPOINT = "https://api.pwnedpasswords.com/range/{prefix}"
# 3s is generous — HIBP median response is ~100-300ms. The budget
# exists so a hung connection on the change-password path doesn't
# freeze the event loop for a user who's waiting to save a new
# password.
_HIBP_TIMEOUT_S = 3.0
_HIBP_HEADERS = {
    # Pads the response body with random-length filler so an on-path
    # observer can't infer anything from response size beyond what
    # they'd learn from the prefix alone.
    "Add-Padding": "true",
    # HIBP etiquette asks for a descriptive UA so abusive callers can
    # be identified + contacted.
    "User-Agent": "Reverto/1.0",
}


def _split_hash(plaintext: str) -> tuple[str, str]:
    """SHA-1 the plaintext and return (prefix, suffix) hex-uppercased.

    HIBP's wire format is uppercase hex; upper-casing here ensures the
    response-matching does a case-exact string compare without a
    per-line ``.upper()``.
    """
    digest = hashlib.sha1(plaintext.encode("utf-8")).hexdigest().upper()
    return digest[:5], digest[5:]


async def is_password_pwned(plaintext: str) -> bool:
    """Return True iff the plaintext has been seen in a public breach.

    False is returned for every failure mode — HIBP unreachable, HTTP
    non-200, parse failure, timeout. Log-level WARNING on failure so
    silent skipping is observable in ``portal.log``.

    The ``plaintext`` argument NEVER appears in the logger output —
    only the 5-char hash prefix, which is also what gets sent to HIBP,
    so no additional information leaks through the log.
    """
    if not plaintext:
        return False

    prefix, suffix = _split_hash(plaintext)
    url = _HIBP_ENDPOINT.format(prefix=prefix)
    try:
        async with httpx.AsyncClient(timeout=_HIBP_TIMEOUT_S) as client:
            response = await client.get(url, headers=_HIBP_HEADERS)
    except httpx.TimeoutException:
        logger.warning(
            "HIBP check timed out (prefix=%s); allowing password (fail-open)",
            prefix,
        )
        return False
    except httpx.RequestError as e:
        # RequestError covers connection errors, DNS failures, TLS
        # handshake failures. `type(e).__name__` carries enough context
        # for triage without risking plaintext leakage via str(e).
        logger.warning(
            "HIBP check network error %s (prefix=%s); allowing password (fail-open)",
            type(e).__name__, prefix,
        )
        return False
    except Exception as e:  # noqa: BLE001 — defensive catch-all
        # httpx rarely raises anything outside the two classes above,
        # but a catch-all keeps an unexpected error from turning into
        # a 500 on the change-password path.
        logger.warning(
            "HIBP check unexpected error %s (prefix=%s); allowing password (fail-open)",
            type(e).__name__, prefix,
        )
        return False

    if response.status_code != 200:
        logger.warning(
            "HIBP returned HTTP %d (prefix=%s); allowing password (fail-open)",
            response.status_code, prefix,
        )
        return False

    try:
        body = response.text
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "HIBP response read failed %s (prefix=%s); allowing password (fail-open)",
            type(e).__name__, prefix,
        )
        return False

    # Response format: one `SUFFIX:COUNT` per line, separated by
    # CRLF. Any unparsable line is skipped — one malformed row must
    # not mask a matching row later in the body.
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, _tail = line.partition(":")
        if head == suffix:
            return True
    return False
