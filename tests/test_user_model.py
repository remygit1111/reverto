"""Tests for core/user.py.

Phase-3a wires session-based user resolution; the ``DEFAULT_USER``
admin stub was removed in audit r1-051 once r1-001 closed the last
API-key caller. These tests pin the User dataclass + DB-backed
lookup contract so future Phase-3 work doesn't silently regress.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import database  # noqa: E402
from core.user import (  # noqa: E402
    User,
    get_active_user_ids,
    get_user_by_id,
    get_user_by_username,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    """Every test gets a fresh DB so the seeded admin row + any extra
    users land in a predictable state."""
    database.set_db_path(tmp_path / "user.db")
    database.init_db()
    yield
    database.close_db()


# ── Dataclass contract ─────────────────────────────────────────────────────

class TestUserDataclass:

    def test_construct_with_fields(self):
        u = User(id=42, username="alice")
        assert u.id == 42
        assert u.username == "alice"
        assert u.active is True  # default

    def test_frozen_is_immutable(self):
        """frozen=True so request-scoped instances can't be mutated
        mid-flight. This is the safety invariant we need to keep as
        Phase 2 lands."""
        u = User(id=1, username="admin")
        with pytest.raises(Exception):  # FrozenInstanceError, but subclass of AttributeError
            u.id = 99  # type: ignore[misc]


# ── DB-backed lookups ──────────────────────────────────────────────────────

class TestGetUserById:

    def test_returns_admin_after_init_db(self):
        u = get_user_by_id(1)
        assert u is not None
        assert u.id == 1
        assert u.username == "admin"
        assert u.active is True

    def test_returns_none_for_unknown_id(self):
        assert get_user_by_id(999) is None

    def test_returns_none_for_negative_id(self):
        assert get_user_by_id(-1) is None


class TestGetUserByUsername:

    def test_returns_admin_by_name(self):
        u = get_user_by_username("admin")
        assert u is not None
        assert u.id == 1

    def test_returns_none_for_unknown_name(self):
        assert get_user_by_username("nobody") is None

    def test_finds_additional_user(self):
        """Seed a second user and verify lookup returns the right row."""
        conn = database.get_db()
        conn.execute(
            "INSERT INTO users (id, username, active) VALUES (2, 'bob', 1)"
        )
        conn.commit()
        u = get_user_by_username("bob")
        assert u is not None
        assert u.id == 2
        assert u.username == "bob"

    def test_respects_active_flag(self):
        """Deactivated users still resolve — Phase 2 will decide what
        to do with them at the session layer; the helper itself is
        just a lookup."""
        conn = database.get_db()
        conn.execute(
            "INSERT INTO users (id, username, active) VALUES (3, 'inactive', 0)"
        )
        conn.commit()
        u = get_user_by_username("inactive")
        assert u is not None
        assert u.active is False


class TestGetActiveUserIds:
    """``get_active_user_ids`` backs the Phase-2 cross-check in
    BotRegistry._scan_user_dirs. It must return only rows with
    active=1, and must stay cheap (one indexed PK scan)."""

    def test_default_seed_contains_admin(self):
        """After init_db the admin (id=1) is the only active user."""
        assert get_active_user_ids() == {1}

    def test_includes_all_active_users(self):
        conn = database.get_db()
        conn.execute("INSERT INTO users (id, username) VALUES (2, 'bob')")
        conn.execute("INSERT INTO users (id, username) VALUES (3, 'carol')")
        conn.commit()
        assert get_active_user_ids() == {1, 2, 3}

    def test_excludes_inactive_users(self):
        """active=0 rows must NOT land in the set — the registry's
        cross-check treats deactivated users like orphans."""
        conn = database.get_db()
        conn.execute(
            "INSERT INTO users (id, username, active) VALUES (5, 'frozen', 0)"
        )
        conn.execute(
            "INSERT INTO users (id, username) VALUES (6, 'thawed')"
        )
        conn.commit()
        ids = get_active_user_ids()
        assert 5 not in ids
        assert 6 in ids
        assert 1 in ids  # admin

    def test_returns_set_for_o1_membership(self):
        """Return type must be a set so callers can do ``x in ids``
        in O(1) instead of O(n). Minor contract pin — if someone
        ever changes it to a list the registry's hot-path perf
        quietly degrades."""
        result = get_active_user_ids()
        assert isinstance(result, set)
