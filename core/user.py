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
    can't be mutated in-flight — the session handler returns a fresh
    User per request, and accidental field assignment should raise
    instead of silently polluting another request's view.

    Phase-3a: ``role`` and ``session_epoch`` moved into the DB from
    the old logs/.auth.json blob. ``role`` gates admin-only endpoints
    (Phase-3c scope); ``session_epoch`` invalidates all cookies for
    THIS user on logout / password change (was global pre-Phase-3).
    """

    id: int
    username: str
    active: bool = True
    role: str = "user"
    session_epoch: int = 0

    def __repr__(self) -> str:  # pragma: no cover — cosmetic
        return f"User(id={self.id}, username={self.username!r})"


# Singleton for Phase-1 fallback. Phase-3a: production-code resolves
# the real User via core.user_store.get_user_by_id (cookie uid).
# Kept for tests + tooling that want a zero-I/O User instance without
# a DB roundtrip — never use in a request-handler hot path.
DEFAULT_USER = User(id=1, username="admin", role="admin")


def get_default_user() -> User:
    """Return the Phase-1 admin stub.

    WARNING: Phase-3a introduced DB-backed user resolution. Production
    code paths should go through ``_request_user`` (HTTP) or
    ``_ws_extract_user_id`` (WS) which both consult the DB via
    ``core.user_store``. This helper survives as a zero-I/O fallback
    for tooling + tests; using it in a route handler would skip
    password/active/session_epoch checks and re-open the "always
    admin" hole the Phase-3a refactor closed.
    """
    return DEFAULT_USER


def _row_to_user(row) -> User:
    """Map a users-row (sqlite3.Row) to a User dataclass. Handles
    defaults for pre-v4 callers that may not have the new columns
    surfaced — in practice init_db's migration guarantees v4 shape,
    but the .get()-style access keeps the mapper robust if a stale
    connection somehow lingers."""
    keys = row.keys() if hasattr(row, "keys") else []
    return User(
        id=int(row["id"]),
        username=str(row["username"]),
        active=bool(row["active"]),
        role=str(row["role"]) if "role" in keys else "user",
        session_epoch=(
            int(row["session_epoch"]) if "session_epoch" in keys else 0
        ),
    )


def get_user_by_id(user_id: int) -> Optional[User]:
    """Fetch a user by id from the database. Returns None when no row
    matches. Reads the ``active`` column as a bool so callers can gate
    on it (e.g. refuse to run bots for deactivated users).
    """
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, active, role, session_epoch "
        "FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_user(row)


def get_user_by_username(username: str) -> Optional[User]:
    """Username lookup, used by session resolution. Kept next to
    get_user_by_id for symmetry — the users table's UNIQUE constraint
    on username guarantees at most one match."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, active, role, session_epoch "
        "FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not row:
        return None
    return _row_to_user(row)


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


def get_admin_user_ids() -> list[int]:
    """Return active admin user IDs.

    Used by the WS log broadcaster to fan system-wide events (the
    ``portal`` slug, which has no per-bot owner) out to privileged
    clients only. Single-indexed scan on the tiny users table, so
    cheap enough to call once per tail_logs iteration.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id FROM users WHERE active = 1 AND role = 'admin'",
    ).fetchall()
    return [int(r["id"]) for r in rows]
