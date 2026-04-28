"""TOTP 2FA helpers — Phase B foundation (PR 1 of 5).

Implements RFC 6238 (TOTP) with the operator-defined defaults from
the security-model resolution on 2026-04-28:

* Issuer: ``Reverto`` (static — same across all deploys so the
  authenticator-app entry stays stable when an operator rotates a
  user's seed).
* Algorithm: SHA-1 (RFC 6238 default; widest authenticator-app
  compatibility — Authy, Google Authenticator, 1Password, Aegis
  all default to SHA-1 and most do not surface algorithm in the
  enrollment UX, so SHA-256/SHA-512 silently break for users on
  legacy apps).
* Digits: 6 (standard authenticator-app format).
* Period: 30 seconds.
* Skew tolerance: ±1 window (90 s total acceptance — covers
  device-clock drift up to half a window in either direction).
* Secret length: 160 bits (RFC 6238 minimum), base32-encoded.

Secrets are encrypted at rest in ``users.totp_seed_encrypted`` using
the user's Fernet key — same per-user key the exchange-credentials
helpers use, so a future ``rotate_user_key`` rotates both domains
under one commit-order contract (no separate TOTP-rotation flow).

This module is purely structural in PR 1: no endpoint reads or writes
``users.totp_seed_encrypted`` yet, no login-flow consults the helpers.
PR 2 wires the enrollment endpoints; PR 3 makes TOTP-verify a gate on
``/auth/login``.
"""

from __future__ import annotations

import logging
import secrets

import pyotp

from core.credentials import get_default_provider

logger = logging.getLogger(__name__)

# ── Constants (operator-fixed — see docstring above) ───────────────────────

ISSUER = "Reverto"
ALGORITHM = "SHA1"
DIGITS = 6
PERIOD_SECONDS = 30
SKEW_TOLERANCE_WINDOWS = 1

# 160 bits / 5 bits per base32 char = 32 base32 chars.
# pyotp.random_base32 default produces this length already; the constant
# is documented separately so a future RFC 6238 revision (or an audit
# request to bump to 256 bits) has a single source of truth.
SECRET_BITS = 160
SECRET_BASE32_LEN = SECRET_BITS // 5  # = 32


# ── Secret + URI generation ────────────────────────────────────────────────


def generate_secret() -> str:
    """Generate a fresh 160-bit TOTP secret as a base32 string.

    Uses ``pyotp.random_base32`` which calls ``secrets.token_bytes``
    under the hood — cryptographically secure. Output is exactly
    ``SECRET_BASE32_LEN`` (32) characters, no padding, alphabet
    A–Z + 2–7 (RFC 4648 base32).

    Returned string is the canonical seed — same value goes to the
    QR/provisioning URI for the user's authenticator app AND to the
    encrypted DB column. Never log, never echo to clients post-
    enrollment.
    """
    return pyotp.random_base32(length=SECRET_BASE32_LEN)


def generate_provisioning_uri(secret: str, username: str) -> str:
    """Build the ``otpauth://`` URI for a QR-code enrollment.

    Authenticator apps parse this URI from a QR code and configure
    themselves automatically. Format produced by pyotp:

        otpauth://totp/Reverto:<username>?secret=<base32>&issuer=Reverto
                       &algorithm=SHA1&digits=6&period=30

    The ``issuer`` query-parameter and the issuer-prefix on the path
    are both included by pyotp — some authenticator apps read one
    and some read the other; including both is the compatibility
    posture every reference implementation takes.

    URL-encoding of special characters in ``username`` is handled by
    pyotp internally — at the LoginBody level v27-09 already
    restricts usernames to ``[a-zA-Z0-9_.-]+`` so no encoding is
    actually needed in practice, but pyotp's quoting keeps us safe
    against future username-policy widening.
    """
    totp = pyotp.TOTP(
        secret,
        digits=DIGITS,
        interval=PERIOD_SECONDS,
    )
    return totp.provisioning_uri(name=username, issuer_name=ISSUER)


# ── Code verification ──────────────────────────────────────────────────────


def verify_code(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code against the secret.

    Accepts ±``SKEW_TOLERANCE_WINDOWS`` for clock-drift, which gives
    an effective 90 s acceptance window centered on now. Returns
    ``True`` iff the code is valid for the current window or one
    adjacent window.

    Constant-time comparison is delegated to ``pyotp.TOTP.verify``,
    which uses ``hmac.compare_digest`` internally — no timing-side-
    channel between "wrong digit at position 0" and "wrong digit at
    position 5".

    Defence-in-depth shape-checks (``isdigit``, length) run BEFORE
    the HMAC comparison to short-circuit obvious garbage cheaply.
    These are not security-critical (the HMAC compare would also
    reject them, and constant-time is preserved either way) but
    they keep the hot path fast under bot-traffic that sends
    nonsense codes.
    """
    if not secret or not code:
        return False
    if not code.isdigit() or len(code) != DIGITS:
        return False

    totp = pyotp.TOTP(
        secret,
        digits=DIGITS,
        interval=PERIOD_SECONDS,
    )
    return totp.verify(code, valid_window=SKEW_TOLERANCE_WINDOWS)


# ── Per-user encrypt / decrypt (delegates to CredentialProvider) ───────────


def encrypt_seed_for_user(user_id: int, secret: str) -> str:
    """Encrypt a TOTP secret for storage in
    ``users.totp_seed_encrypted``.

    Uses the user's Fernet key via the default ``CredentialProvider``
    — same per-user key the exchange-credentials helpers use, so
    ``rotate_user_key`` rotates both domains together. Returns the
    ciphertext as a UTF-8 string suitable for direct DB storage.
    """
    return get_default_provider().encrypt_for_user(user_id, secret)


def decrypt_seed_for_user(user_id: int, encrypted: str) -> str:
    """Decrypt a stored TOTP secret using the user's Fernet key.

    Raises ``cryptography.fernet.InvalidToken`` (or related) on
    tamper / wrong key / corruption. Caller MUST handle that
    explicitly — a TOTP-seed that won't decrypt is an integrity
    event, not a "treat as not enrolled" condition (the DB column's
    NULL state already covers the latter).
    """
    return get_default_provider().decrypt_for_user(user_id, encrypted)


def generate_recovery_token() -> str:
    """Cryptographically random one-shot token for sensitive flows.

    Reserved for PR 2's recovery-code generation but defined here so
    its callsite uses the same RNG and length contract as the rest
    of the TOTP foundation. 32 hex chars = 128 bits of entropy.
    """
    return secrets.token_hex(16)
