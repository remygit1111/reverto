"""User model for multi-tenant support.

Phase 1 of the multi-tenant migration introduces a users table + a
user_id NOT NULL FK on every owned table, but only seeds the single
``admin`` row (id=1). All portal requests currently resolve to this
admin user via ``get_default_user``. Phase 2 will wire session-based
user resolution so the User flows to the engine from the cookie,
and Phase 3 enables real sign-up.

Downstream code should accept a ``User`` (or at minimum a ``user_id``)
rather than reaching for a module-level default. That way the Phase-2
transition is a matter of swapping the ``_request_user`` dependency
instead of rewriting every store call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.database import get_db


@dataclass(frozen=True)
class User:
    """Immutable user record. frozen=True so a request-scoped instance
    can't be mutated in-flight — a Phase-2 session handler returns a
    fresh User per request, and accidental field assignment should
    raise instead of silently polluting another request's view."""

    id: int
    username: str
    active: bool = True

    def __repr__(self) -> str:  # pragma: no cover — cosmetic
        return f"User(id={self.id}, username={self.username!r})"


# Singleton for Phase-1 usage. Tests that need a different user can
# either construct User(...) directly or patch get_default_user.
DEFAULT_USER = User(id=1, username="admin")


def get_default_user() -> User:
    """Return the Phase-1 admin user.

    The body is deliberately NOT a DB lookup — we want a zero-I/O path
    so hot routes can `Depends(_request_user)` without taking a
    connection per request. The users table is kept in sync via
    init_db (seed row) and the invariant holds for the lifetime of the
    portal process.
    """
    return DEFAULT_USER


def get_user_by_id(user_id: int) -> Optional[User]:
    """Fetch a user by id from the database. Returns None when no row
    matches. Reads the ``active`` column as a bool so callers can gate
    on it in Phase 2 (e.g. refuse to run bots for deactivated users).

    Phase-1 callers don't need this helper — it exists so tests of the
    users table contract have something public to target, and so the
    Phase-2 session handler can resolve the FK it reads from the
    cookie without reaching into deal_store."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, active FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return User(
        id=int(row["id"]),
        username=str(row["username"]),
        active=bool(row["active"]),
    )


def get_user_by_username(username: str) -> Optional[User]:
    """Username lookup, used by session resolution in Phase 2. Kept
    next to get_user_by_id for symmetry — the users table's UNIQUE
    constraint on username guarantees at most one match."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, active FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not row:
        return None
    return User(
        id=int(row["id"]),
        username=str(row["username"]),
        active=bool(row["active"]),
    )


def get_active_user_ids() -> set[int]:
    """Return the set of user_ids for every row with ``active = 1``.

    Used by ``BotRegistry._scan_user_dirs`` to cross-check integer-
    named subdirs under ``config/bots/`` against the actual users
    table — orphan dirs (operator error, stale state, deactivated
    users) are thereby prevented from silently registering as real
    tenants. The query is a single indexed scan over the PK and
    the users table is tiny in Phase 1/2, so calling this per
    registry refresh (every 5 s at worst) is cheap.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id FROM users WHERE active = 1",
    ).fetchall()
    return {int(r["id"]) for r in rows}
