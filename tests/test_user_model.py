"""Tests for core/user.py.

Phase 1 of the multi-tenant migration introduces the User dataclass +
``get_default_user`` + ``get_user_by_id`` + ``get_user_by_username``.
These tests pin the module contract so Phase 2 can replace the zero-
I/O default with real session resolution without breaking call sites.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import database  # noqa: E402
from core.user import (  # noqa: E402
    DEFAULT_USER,
    User,
    get_default_user,
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

    def test_default_user_is_admin(self):
        assert DEFAULT_USER.id == 1
        assert DEFAULT_USER.username == "admin"
        assert DEFAULT_USER.active is True

    def test_get_default_user_returns_admin(self):
        """Zero-I/O fast path — must NOT query the DB."""
        u = get_default_user()
        assert u.id == 1
        assert u.username == "admin"


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
