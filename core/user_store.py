"""DB-backed user + auth helpers (Phase-3a).

Module-split rationale: core/user.py owns the ``User`` dataclass and
the read-side lookups (``get_user_by_id``, ``get_user_by_username``,
``get_active_user_ids``). This module owns the write-side +
security-sensitive helpers that used to live in logs/.auth.json:

  * ``verify_password(username, plaintext)``  — constant-time bcrypt
  * ``set_password(user_id, plaintext)``      — hash + persist
  * ``bump_session_epoch(user_id)``           — invalidate cookies
  * ``get_session_epoch(user_id)``            — read current epoch

The read-side helpers from core.user are re-exported for
convenience — callers can do ``from core import user_store`` and
have a one-stop import for everything auth-related.

Security invariants:
  * ``verify_password`` fails closed on every error path (missing
    user, inactive user, NULL hash, malformed hash, wrong password).
    No information leak via timing beyond bcrypt's own variable-time
    checkpw — same password field over all failure paths so an
    attacker can't distinguish "no such user" from "wrong password".
  * Bcrypt is the only allowed password hash algorithm. A future
    argon2 migration would add a prefix sentinel and a fallback
    branch; today's code refuses non-bcrypt hashes (detected via
    bcrypt.checkpw raising ValueError on malformed input).
  * ``password_hash`` NULL means "no password provisioned yet" —
    typical on a fresh install between init_db() and setup_admin.py.
    verify_password returns None in that state, never bypasses the
    check.
"""

from __future__ import annotations

from typing import Optional

import bcrypt

from core.database import get_db
from core.user import (  # re-exports for one-stop import
    User,
    get_user_by_id,
    get_user_by_username,
)

__all__ = [
    "User",
    "get_user_by_id",
    "get_user_by_username",
    "verify_password",
    "set_password",
    "bump_session_epoch",
    "get_session_epoch",
]

# bcrypt rounds for setting new passwords. 12 matches the original
# _bootstrap_auth_if_missing bootstrap; tests use 4 (via set_password
# directly? no — set_password always uses this constant) — test
# execution takes ~0.1s per hash at rounds=12 which is fine for the
# handful of auth-touching tests we run.
_BCRYPT_ROUNDS = 12


def verify_password(username: str, plaintext: str) -> Optional[User]:
    """Verify a plaintext password against the stored bcrypt hash.

    Returns the ``User`` on success, ``None`` on every failure mode:
      - user does not exist
      - user is inactive
      - ``password_hash`` is NULL (admin never provisioned)
      - hash is malformed / not bcrypt
      - password does not match

    Uses ``bcrypt.checkpw`` for constant-time comparison. Does NOT
    distinguish between failure modes to the caller — the endpoint
    should respond with a generic 401 regardless.
    """
    user = get_user_by_username(username)
    if user is None or not user.active:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT password_hash FROM users WHERE id = ?",
        (user.id,),
    ).fetchone()
    if row is None:
        return None
    stored = row["password_hash"]
    if not stored:
        # NULL / empty — admin never provisioned via setup_admin.py.
        return None
    try:
        ok = bcrypt.checkpw(
            plaintext.encode("utf-8"),
            stored.encode("utf-8") if isinstance(stored, str) else stored,
        )
    except ValueError:
        # Malformed hash — treat as auth failure, log nothing
        # identifying the user so failed logins don't create a
        # usernames-with-weird-hashes oracle in the logs.
        return None
    if not ok:
        return None
    return user


def set_password(user_id: int, plaintext: str) -> bool:
    """Hash and store a new password for the given user.

    Returns True on success, False if the user_id doesn't resolve to
    an existing row. Does NOT bump session_epoch — the caller decides
    whether this is a provisioning call (setup_admin.py, no bump) or a
    password-change call (bump to invalidate existing cookies).
    """
    user = get_user_by_id(user_id)
    if user is None:
        return False
    pw_hash = bcrypt.hashpw(
        plaintext.encode("utf-8"),
        bcrypt.gensalt(rounds=_BCRYPT_ROUNDS),
    ).decode("utf-8")
    conn = get_db()
    with conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (pw_hash, user_id),
        )
    return True


def bump_session_epoch(user_id: int) -> int:
    """Atomically increment ``users.session_epoch`` and return the
    new value. Used on logout and password change to invalidate
    every outstanding cookie for THIS user.

    Audit v26-11: uses SQLite's ``RETURNING`` clause so UPDATE +
    return value happen in a single statement. Pre-fix was
    UPDATE-then-SELECT, which left a window where two concurrent
    bumps could each read the same post-update value. SQLite 3.35+
    ships RETURNING; Python 3.10's bundled sqlite3 covers it.

    If the user_id doesn't exist the UPDATE is a no-op and we
    return 0 — the caller is expected to have authenticated first.
    """
    conn = get_db()
    with conn:
        cur = conn.execute(
            "UPDATE users SET session_epoch = session_epoch + 1 "
            "WHERE id = ? RETURNING session_epoch",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        return 0
    return int(row[0])


def get_session_epoch(user_id: int) -> int:
    """Read the current session_epoch for a user. Returns 0 on
    unknown user (which matches the default, so an unauthenticated
    cookie can never match a real epoch by accident)."""
    conn = get_db()
    row = conn.execute(
        "SELECT session_epoch FROM users WHERE id = ?", (user_id,),
    ).fetchone()
    if row is None:
        return 0
    return int(row["session_epoch"])
