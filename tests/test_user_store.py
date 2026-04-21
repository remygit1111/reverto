"""Tests voor core.user_store — DB-backed user + auth helpers.

Pin de contract van verify_password, set_password, bump_session_epoch
en get_session_epoch. De DB is per-test ge-isoleerd via de autouse
``_isolate_reverto_db`` fixture in conftest.py, dus elke test start
met een verse admin-seed en een NULL password_hash.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt
import pytest

from core import user_store
from core.database import get_db


_KNOWN_PW = "pytest-known-password-123"


class TestUserStoreReads:
    """Read-side helpers re-exported from core.user."""

    def test_get_user_by_id_returns_seeded_admin(self):
        admin = user_store.get_user_by_id(1)
        assert admin is not None
        assert admin.username == "admin"
        assert admin.role == "admin"
        assert admin.active is True
        # Fresh seed — epoch starts at 0.
        assert admin.session_epoch == 0

    def test_get_user_by_id_returns_none_for_unknown(self):
        assert user_store.get_user_by_id(999) is None

    def test_get_user_by_username_returns_seeded_admin(self):
        admin = user_store.get_user_by_username("admin")
        assert admin is not None
        assert admin.id == 1

    def test_get_user_by_username_returns_none_for_unknown(self):
        assert user_store.get_user_by_username("does-not-exist") is None


class TestVerifyPassword:
    """verify_password fails closed on every failure mode — no
    information leak that would let an attacker enumerate usernames
    or distinguish between "wrong password" and "no such user".
    """

    def test_succeeds_with_correct_credentials(self):
        user_store.set_password(1, _KNOWN_PW)
        user = user_store.verify_password("admin", _KNOWN_PW)
        assert user is not None
        assert user.id == 1

    def test_fails_with_wrong_password(self):
        user_store.set_password(1, _KNOWN_PW)
        assert user_store.verify_password("admin", "wrong") is None

    def test_fails_with_null_hash(self):
        """Fresh seed — admin has no password yet. Login must fail
        closed, not fall back to "no password = accept anything"."""
        assert user_store.verify_password("admin", "anything") is None

    def test_fails_with_nonexistent_user(self):
        assert user_store.verify_password("no-such-user", "pw") is None

    def test_fails_with_inactive_user(self):
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO users (username, active, role) "
                "VALUES ('frozen', 0, 'user')",
            )
        user_store.set_password(
            user_store.get_user_by_username("frozen").id, _KNOWN_PW,
        )
        # Even with the correct password, inactive users cannot log in.
        assert user_store.verify_password("frozen", _KNOWN_PW) is None

    def test_fails_gracefully_on_malformed_hash(self):
        """A row with a malformed password_hash (e.g. someone typed
        a plain string into the DB) must not crash — bcrypt.checkpw
        raises ValueError, and verify_password translates that to a
        generic None."""
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE users SET password_hash = 'not-a-bcrypt-hash' "
                "WHERE id = 1",
            )
        assert user_store.verify_password("admin", "anything") is None


class TestSetPassword:
    def test_updates_hash(self):
        assert user_store.set_password(1, _KNOWN_PW) is True
        conn = get_db()
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = 1",
        ).fetchone()
        # Stored hash must be a valid bcrypt verification target.
        assert bcrypt.checkpw(
            _KNOWN_PW.encode("utf-8"), row["password_hash"].encode("utf-8"),
        )

    def test_returns_false_on_unknown_user(self):
        assert user_store.set_password(999, "whatever") is False

    def test_overwrites_previous_hash(self):
        user_store.set_password(1, "first-password-123")
        user_store.set_password(1, "second-password-456")
        assert user_store.verify_password("admin", "first-password-123") is None
        assert user_store.verify_password("admin", "second-password-456") is not None


class TestSessionEpoch:
    def test_bump_increments(self):
        assert user_store.get_session_epoch(1) == 0
        assert user_store.bump_session_epoch(1) == 1
        assert user_store.bump_session_epoch(1) == 2
        assert user_store.get_session_epoch(1) == 2

    def test_bump_is_per_user(self):
        """Bumping user 1's epoch must not affect any other user.
        Pre-Phase-3a the epoch was a single integer in .auth.json
        — logging out one user invalidated everyone's cookies. The
        DB-per-row design fixes that."""
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO users (username, role) VALUES ('bob', 'user')",
            )
        bob = user_store.get_user_by_username("bob")
        assert user_store.get_session_epoch(bob.id) == 0

        user_store.bump_session_epoch(1)  # admin's epoch
        assert user_store.get_session_epoch(1) == 1
        assert user_store.get_session_epoch(bob.id) == 0

    def test_get_returns_zero_for_unknown_user(self):
        assert user_store.get_session_epoch(999) == 0

    def test_bump_returns_value_atomically(self):
        """Audit v26-11: bump_session_epoch gebruikt sinds v26-11
        een ``UPDATE ... RETURNING session_epoch`` statement, zodat
        de post-update waarde in één SQL-call terugkomt. Pre-fix
        was UPDATE gevolgd door aparte SELECT, met een race-window
        waarin twee threads dezelfde SELECT-waarde zouden lezen.

        Deze test dekt de functionele kant (retour = post-UPDATE
        waarde); de concurrency-garantie zit in het RETURNING
        statement zelf.
        """
        assert user_store.get_session_epoch(1) == 0
        # Elke bump moet zijn eigen unieke waarde retourneren.
        v1 = user_store.bump_session_epoch(1)
        v2 = user_store.bump_session_epoch(1)
        v3 = user_store.bump_session_epoch(1)
        assert v1 == 1
        assert v2 == 2
        assert v3 == 3
        # En de DB matcht met de laatste return.
        assert user_store.get_session_epoch(1) == 3

    def test_bump_for_unknown_user_is_noop(self):
        """No row matches — UPDATE does nothing, return value is 0.
        Callers that use this for cleanup shouldn't crash."""
        assert user_store.bump_session_epoch(999) == 0
