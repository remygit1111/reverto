# tests/test_credentials.py
# Tests voor core.credentials — per-user Fernet-encrypted exchange keys.
#
# Elke test isoleert de filesystem tree via monkey-patching van
# core.paths.BASE_DIR naar een tmp_path. De Phase-2 layout legt
# keys op keys/<uid>.key en ciphertext op credentials/<uid>/<exchange>.enc,
# dus één knop voor BASE_DIR schakelt de hele test-sandbox om.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import credentials, paths


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Redirect the whole Phase-2 tree (keys/ + credentials/) into
    tmp_path. Audit v26-06: de system-key helpers
    (save_encrypted/load_encrypted) zijn verwijderd post-Phase-3a,
    dus de oude _LOG_DIR/_KEY_FILE sandboxing is niet langer nodig."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "_BASE_DIR", tmp_path)
    return tmp_path


class TestUserKey:

    def test_user_key_auto_created(self, tmp_store):
        key_path = paths.user_fernet_key_path(1)
        assert not key_path.exists()
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        assert key_path.exists()
        assert len(key_path.read_bytes()) > 0

    def test_user_key_reused_across_calls(self, tmp_store):
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        first = paths.user_fernet_key_path(1).read_bytes()
        credentials.save_keys("kraken", "ak2", "sc2", user_id=1)
        second = paths.user_fernet_key_path(1).read_bytes()
        assert first == second, "per-user key must not rotate between calls"

    def test_user_key_is_mode_0600(self, tmp_store):
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        key_path = paths.user_fernet_key_path(1)
        mode = key_path.stat().st_mode & 0o777
        assert mode == 0o600, f"key mode is {oct(mode)}, expected 0600"


class TestSaveAndGet:
    def test_roundtrip(self, tmp_store):
        credentials.save_keys("bitget", "my-api-key", "my-secret", user_id=1)
        got = credentials.get_keys("bitget", user_id=1)
        assert got == {"api_key": "my-api-key", "api_secret": "my-secret"}

    def test_get_unknown_returns_none(self, tmp_store):
        assert credentials.get_keys("bitget", user_id=1) is None

    def test_multiple_exchanges(self, tmp_store):
        credentials.save_keys("bitget", "b-key", "b-sec", user_id=1)
        credentials.save_keys("kraken", "k-key", "k-sec", user_id=1)
        assert credentials.get_keys("bitget", user_id=1) == {
            "api_key": "b-key", "api_secret": "b-sec",
        }
        assert credentials.get_keys("kraken", user_id=1) == {
            "api_key": "k-key", "api_secret": "k-sec",
        }

    def test_ciphertext_is_actually_encrypted(self, tmp_store):
        credentials.save_keys(
            "bitget", "plain-text-key", "plain-text-secret", user_id=1,
        )
        enc_path = paths.exchange_creds_path(1, "bitget")
        raw = enc_path.read_bytes()
        # Secrets must never appear verbatim in the ciphertext blob.
        assert b"plain-text-key" not in raw
        assert b"plain-text-secret" not in raw


class TestHasKeys:
    def test_false_when_empty(self, tmp_store):
        assert credentials.has_keys("bitget", user_id=1) is False

    def test_true_after_save(self, tmp_store):
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        assert credentials.has_keys("bitget", user_id=1) is True
        assert credentials.has_keys("kraken", user_id=1) is False


class TestDeleteKeys:
    def test_delete_existing(self, tmp_store):
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        assert credentials.delete_keys("bitget", user_id=1) is True
        assert credentials.get_keys("bitget", user_id=1) is None
        assert credentials.has_keys("bitget", user_id=1) is False

    def test_delete_unknown_returns_false(self, tmp_store):
        assert credentials.delete_keys("bitget", user_id=1) is False

    def test_delete_leaves_other_entries(self, tmp_store):
        credentials.save_keys("bitget", "b", "B", user_id=1)
        credentials.save_keys("kraken", "k", "K", user_id=1)
        credentials.delete_keys("bitget", user_id=1)
        assert credentials.get_keys("bitget", user_id=1) is None
        assert credentials.get_keys("kraken", user_id=1) == {
            "api_key": "k", "api_secret": "K",
        }


class TestListExchanges:
    def test_empty(self, tmp_store):
        assert credentials.list_exchanges_with_keys(user_id=1) == []

    def test_after_save(self, tmp_store):
        credentials.save_keys("kraken", "k", "K", user_id=1)
        credentials.save_keys("bitget", "b", "B", user_id=1)
        # Sorted alphabetically — deterministic UI output.
        assert credentials.list_exchanges_with_keys(user_id=1) == [
            "bitget", "kraken",
        ]

    def test_after_delete(self, tmp_store):
        credentials.save_keys("bitget", "b", "B", user_id=1)
        credentials.save_keys("kraken", "k", "K", user_id=1)
        credentials.delete_keys("bitget", user_id=1)
        assert credentials.list_exchanges_with_keys(user_id=1) == ["kraken"]


class TestDecryptFailure:
    def test_tampered_ciphertext_returns_none(self, tmp_store):
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        path = paths.exchange_creds_path(1, "bitget")
        # Flip the first byte to break the Fernet MAC.
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))
        assert credentials.get_keys("bitget", user_id=1) is None

    def test_garbage_file_returns_none(self, tmp_store):
        path = paths.exchange_creds_path(1, "bitget")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a valid fernet token")
        assert credentials.get_keys("bitget", user_id=1) is None


# ── Per-user isolation — the Phase-2 security property ─────────────────────


class TestPerUserIsolation:
    """user 2 must not be able to read user 1's exchange credentials
    even when both have the same exchange configured. Each user has
    a distinct Fernet key + distinct .enc directory, so a path
    lookup for user 2 naturally misses user 1's blob."""

    def test_user_1_and_user_2_have_different_keys(self, tmp_store):
        credentials.save_keys("bitget", "a", "b", user_id=1)
        credentials.save_keys("bitget", "c", "d", user_id=2)
        key1 = paths.user_fernet_key_path(1).read_bytes()
        key2 = paths.user_fernet_key_path(2).read_bytes()
        assert key1 != key2

    def test_get_keys_isolates_across_users(self, tmp_store):
        credentials.save_keys("bitget", "u1-ak", "u1-sc", user_id=1)
        credentials.save_keys("bitget", "u2-ak", "u2-sc", user_id=2)

        u1 = credentials.get_keys("bitget", user_id=1)
        u2 = credentials.get_keys("bitget", user_id=2)
        assert u1 == {"api_key": "u1-ak", "api_secret": "u1-sc"}
        assert u2 == {"api_key": "u2-ak", "api_secret": "u2-sc"}

    def test_list_exchanges_scoped_per_user(self, tmp_store):
        credentials.save_keys("bitget", "a", "b", user_id=1)
        credentials.save_keys("kraken", "c", "d", user_id=2)
        assert credentials.list_exchanges_with_keys(user_id=1) == ["bitget"]
        assert credentials.list_exchanges_with_keys(user_id=2) == ["kraken"]

    def test_user_2_cannot_decrypt_user_1_blob(self, tmp_store):
        """Swap user 2's .enc to point at user 1's ciphertext, then try
        to read it with user 2's key. The decrypt must fail (different
        Fernet keys) and get_keys must return None rather than leak."""
        credentials.save_keys("bitget", "secret-one", "secret-sec", user_id=1)
        # Ensure user 2 has their own key on disk so get_keys under
        # user 2 doesn't generate a fresh one after we plant the blob.
        credentials.save_keys("kraken", "x", "y", user_id=2)

        u1_blob = paths.exchange_creds_path(1, "bitget").read_bytes()
        u2_path = paths.exchange_creds_path(2, "bitget")
        u2_path.write_bytes(u1_blob)

        # Decrypting user 1's ciphertext under user 2's Fernet key must
        # fail silently → None.
        assert credentials.get_keys("bitget", user_id=2) is None


# Audit v26-06: TestSystemEncryption (save_encrypted / load_encrypted
# roundtrip) is verwijderd omdat de helpers in core.credentials zelf
# zijn gedelete post-Phase-3a. Admin-auth leeft nu in
# users.password_hash (bcrypt), niet in een Fernet-encrypted
# .auth.json blob.
