"""Tests for core.exchange_account_store — CRUD + default-handling +
cascade-on-user-delete + cross-user isolation.

Each test runs under the auto-isolating tmp-DB fixture from conftest.py
(``_isolate_reverto_db``) AND a tmp credentials/keys tree (the
fixtures here monkey-patch ``core.paths.BASE_DIR``), so neither the
DB ledger nor the filesystem ever leak out of the test sandbox.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import credentials, database, exchange_account_store, paths
from core import user_store


@pytest.fixture
def fs_sandbox(tmp_path, monkeypatch):
    """Redirect credentials/keys to tmp_path. The DB ledger comes from
    conftest's autouse fixture."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "_BASE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def second_user(fs_sandbox):
    """Provision a second user (id != 1) so cross-user isolation tests
    have someone to be isolated FROM. user_store.create_user assigns
    auto-increment ids; the seeded admin is id=1."""
    conn = database.get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (username, role, active) "
            "VALUES ('alice', 'user', 1)",
        )
        row = conn.execute(
            "SELECT id FROM users WHERE username = 'alice'",
        ).fetchone()
    return int(row["id"])


_REALISTIC_BITGET_KEY = "ak" + "0" * 30  # 32 alphanumeric
_REALISTIC_BITGET_SEC = "sc" + "0" * 62  # 64 alphanumeric


class TestCreateAccount:

    def test_create_returns_id_and_persists(self, fs_sandbox):
        new_id = exchange_account_store.create_account(
            user_id=1,
            exchange_type="bitget",
            alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="my-pass",
        )
        assert new_id > 0
        account = exchange_account_store.get_account(new_id)
        assert account["alias"] == "main"
        assert account["exchange_type"] == "bitget"
        assert account["user_id"] == 1
        assert account["is_default"] is False

    def test_create_writes_credentials_blob(self, fs_sandbox):
        new_id = exchange_account_store.create_account(
            user_id=1,
            exchange_type="bitget",
            alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="my-pass",
        )
        creds = exchange_account_store.get_account_credentials(new_id)
        assert creds == {
            "api_key": _REALISTIC_BITGET_KEY,
            "api_secret": _REALISTIC_BITGET_SEC,
            "passphrase": "my-pass",
        }

    def test_create_rejects_unknown_exchange_type(self, fs_sandbox):
        with pytest.raises(exchange_account_store.AccountValidationError):
            exchange_account_store.create_account(
                user_id=1, exchange_type="ftx", alias="main",
                api_key="a", api_secret="b",
            )

    def test_create_rejects_duplicate_alias(self, fs_sandbox):
        exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        with pytest.raises(exchange_account_store.AccountValidationError):
            exchange_account_store.create_account(
                user_id=1, exchange_type="bitget", alias="main",
                api_key=_REALISTIC_BITGET_KEY,
                api_secret=_REALISTIC_BITGET_SEC,
                passphrase="p",
            )

    def test_same_alias_different_exchange_type_allowed(self, fs_sandbox):
        # The UNIQUE constraint is on (user, exchange_type, alias) —
        # so "main" for Bitget and "main" for Kraken coexist.
        bitget_id = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        kraken_id = exchange_account_store.create_account(
            user_id=1, exchange_type="kraken", alias="main",
            api_key="K" + "x" * 55,
            api_secret="S" + "x" * 87,
        )
        assert bitget_id != kraken_id
        assert exchange_account_store.get_account(bitget_id)["exchange_type"] == "bitget"
        assert exchange_account_store.get_account(kraken_id)["exchange_type"] == "kraken"


class TestListAndGet:

    def test_list_scoped_per_user(self, fs_sandbox, second_user):
        exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        exchange_account_store.create_account(
            user_id=second_user, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        u1 = exchange_account_store.list_accounts(1)
        u2 = exchange_account_store.list_accounts(second_user)
        assert len(u1) == 1
        assert len(u2) == 1
        assert u1[0]["user_id"] == 1
        assert u2[0]["user_id"] == second_user

    def test_get_unknown_returns_none(self, fs_sandbox):
        assert exchange_account_store.get_account(99999) is None

    def test_account_belongs_to_user(self, fs_sandbox, second_user):
        a1 = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="m",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        assert exchange_account_store.account_belongs_to_user(a1, 1) is True
        assert exchange_account_store.account_belongs_to_user(a1, second_user) is False


class TestSetDefault:

    def test_setting_default_unsets_previous_default(self, fs_sandbox):
        a = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
            is_default=True,
        )
        b = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="test",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
            is_default=True,
        )
        # Setting b as default at create-time must have unset a.
        assert exchange_account_store.get_account(a)["is_default"] is False
        assert exchange_account_store.get_account(b)["is_default"] is True

        # Now flip back.
        assert exchange_account_store.set_default(a) is True
        assert exchange_account_store.get_account(a)["is_default"] is True
        assert exchange_account_store.get_account(b)["is_default"] is False

    def test_default_is_per_exchange_type(self, fs_sandbox):
        # One default for Bitget, one for Kraken — both allowed.
        bg = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
            is_default=True,
        )
        kr = exchange_account_store.create_account(
            user_id=1, exchange_type="kraken", alias="main",
            api_key="K" + "x" * 55,
            api_secret="S" + "x" * 87,
            is_default=True,
        )
        assert exchange_account_store.get_account(bg)["is_default"] is True
        assert exchange_account_store.get_account(kr)["is_default"] is True

    def test_get_default_account(self, fs_sandbox):
        bg = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
            is_default=True,
        )
        got = exchange_account_store.get_default_account(1, "bitget")
        assert got is not None
        assert got["id"] == bg

    def test_get_default_account_returns_none_when_no_default(self, fs_sandbox):
        exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        assert exchange_account_store.get_default_account(1, "bitget") is None


class TestUpdate:

    def test_update_alias(self, fs_sandbox):
        a = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        assert exchange_account_store.update_account(a, alias="renamed") is True
        assert exchange_account_store.get_account(a)["alias"] == "renamed"

    def test_update_unknown_returns_false(self, fs_sandbox):
        assert exchange_account_store.update_account(9999, alias="x") is False

    def test_update_last_tested_at(self, fs_sandbox):
        a = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        ts = "2026-05-12T10:00:00+00:00"
        exchange_account_store.update_account(a, last_tested_at=ts)
        assert exchange_account_store.get_account(a)["last_tested_at"] == ts


class TestDelete:

    def test_delete_removes_row_and_blob(self, fs_sandbox):
        a = exchange_account_store.create_account(
            user_id=1, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        # Pull the credentials_uuid via direct DB read so we can check
        # the blob is gone after delete.
        conn = database.get_db()
        row = conn.execute(
            "SELECT credentials_uuid FROM exchange_accounts WHERE id = ?",
            (a,),
        ).fetchone()
        cred_uuid = row["credentials_uuid"]
        assert paths.uuid_creds_path(1, cred_uuid).exists()

        assert exchange_account_store.delete_account(a) is True

        assert exchange_account_store.get_account(a) is None
        assert not paths.uuid_creds_path(1, cred_uuid).exists()

    def test_delete_idempotent(self, fs_sandbox):
        assert exchange_account_store.delete_account(99999) is False


class TestCascadeOnUserDelete:

    def test_user_delete_cascades_to_accounts(self, fs_sandbox, second_user):
        a = exchange_account_store.create_account(
            user_id=second_user, exchange_type="bitget", alias="main",
            api_key=_REALISTIC_BITGET_KEY,
            api_secret=_REALISTIC_BITGET_SEC,
            passphrase="p",
        )
        conn = database.get_db()
        with conn:
            conn.execute("DELETE FROM users WHERE id = ?", (second_user,))
        # The exchange_accounts row should be gone via ON DELETE CASCADE.
        assert exchange_account_store.get_account(a) is None
