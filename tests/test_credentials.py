# tests/test_credentials.py
# Tests voor core.credentials — UUID-keyed per-user Fernet-encrypted
# exchange credential blobs.
#
# Each test isolates the filesystem tree via monkey-patching of
# ``core.paths.BASE_DIR`` to a tmp_path. The multi-account layout
# stores keys at ``keys/<uid>.key`` and ciphertext at
# ``credentials/<uid>/<uuid>.enc``, so one knob on BASE_DIR redirects
# the whole sandbox.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import credentials, paths


_UUID_A = "aa11" * 8  # 32-char hex stand-in for uuid4().hex
_UUID_B = "bb22" * 8
_UUID_USER_2 = "cc33" * 8


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Redirect the per-user key + .enc tree into tmp_path so tests
    run in a sandbox without touching the repo's real credentials dir.
    """
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "_BASE_DIR", tmp_path)
    return tmp_path


class TestUserKey:

    def test_user_key_auto_created(self, tmp_store):
        key_path = paths.user_fernet_key_path(1)
        assert not key_path.exists()
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "ak", "sc", user_id=1,
        )
        assert key_path.exists()
        assert len(key_path.read_bytes()) > 0

    def test_user_key_reused_across_calls(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "ak", "sc", user_id=1,
        )
        first = paths.user_fernet_key_path(1).read_bytes()
        credentials.save_keys_by_uuid(
            _UUID_B, "kraken", "ak2", "sc2", user_id=1,
        )
        second = paths.user_fernet_key_path(1).read_bytes()
        assert first == second, "per-user key must not rotate between calls"

    def test_user_key_is_mode_0600(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "ak", "sc", user_id=1,
        )
        key_path = paths.user_fernet_key_path(1)
        mode = key_path.stat().st_mode & 0o777
        assert mode == 0o600, f"key mode is {oct(mode)}, expected 0600"


class TestSaveAndGet:
    def test_roundtrip(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "my-api-key", "my-secret",
            user_id=1,
        )
        got = credentials.get_keys_by_uuid(_UUID_A, user_id=1)
        assert got == {
            "api_key": "my-api-key",
            "api_secret": "my-secret",
            "passphrase": "",
        }

    def test_get_unknown_uuid_returns_none(self, tmp_store):
        assert credentials.get_keys_by_uuid(_UUID_A, user_id=1) is None

    def test_multiple_accounts_distinct_blobs(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "b-key", "b-sec",
            user_id=1,
        )
        credentials.save_keys_by_uuid(
            _UUID_B, "kraken", "k-key", "k-sec",
            user_id=1,
        )
        assert credentials.get_keys_by_uuid(_UUID_A, user_id=1) == {
            "api_key": "b-key", "api_secret": "b-sec", "passphrase": "",
        }
        assert credentials.get_keys_by_uuid(_UUID_B, user_id=1) == {
            "api_key": "k-key", "api_secret": "k-sec", "passphrase": "",
        }

    def test_two_accounts_same_exchange_isolated(self, tmp_store):
        # Multi-account: two Bitget accounts ("main" + "test") for the
        # same user must not collide. UUID-keyed storage is the
        # invariant that makes this work.
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "main-key", "main-sec",
            user_id=1, passphrase="main-pp",
        )
        credentials.save_keys_by_uuid(
            _UUID_B, "bitget", "test-key", "test-sec",
            user_id=1, passphrase="test-pp",
        )
        a = credentials.get_keys_by_uuid(_UUID_A, user_id=1)
        b = credentials.get_keys_by_uuid(_UUID_B, user_id=1)
        assert a["api_key"] == "main-key" and a["passphrase"] == "main-pp"
        assert b["api_key"] == "test-key" and b["passphrase"] == "test-pp"

    def test_passphrase_preserved(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "ak", "sc",
            user_id=1, passphrase="my-pass",
        )
        got = credentials.get_keys_by_uuid(_UUID_A, user_id=1)
        assert got == {
            "api_key": "ak", "api_secret": "sc", "passphrase": "my-pass",
        }

    def test_ciphertext_is_actually_encrypted(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "plain-text-key", "plain-text-secret",
            user_id=1, passphrase="plain-pp",
        )
        enc_path = paths.uuid_creds_path(1, _UUID_A)
        raw = enc_path.read_bytes()
        # Plaintext credentials must never appear verbatim in the blob.
        assert b"plain-text-key" not in raw
        assert b"plain-text-secret" not in raw
        assert b"plain-pp" not in raw


class TestDeleteKeys:
    def test_delete_existing(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "ak", "sc", user_id=1,
        )
        assert credentials.delete_keys_by_uuid(_UUID_A, user_id=1) is True
        assert credentials.get_keys_by_uuid(_UUID_A, user_id=1) is None

    def test_delete_unknown_returns_false(self, tmp_store):
        assert credentials.delete_keys_by_uuid(_UUID_A, user_id=1) is False

    def test_delete_leaves_other_entries(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "b", "B", user_id=1,
        )
        credentials.save_keys_by_uuid(
            _UUID_B, "kraken", "k", "K", user_id=1,
        )
        credentials.delete_keys_by_uuid(_UUID_A, user_id=1)
        assert credentials.get_keys_by_uuid(_UUID_A, user_id=1) is None
        assert credentials.get_keys_by_uuid(_UUID_B, user_id=1) == {
            "api_key": "k", "api_secret": "K", "passphrase": "",
        }


class TestDecryptFailure:
    def test_tampered_ciphertext_returns_none(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "ak", "sc", user_id=1,
        )
        path = paths.uuid_creds_path(1, _UUID_A)
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))
        assert credentials.get_keys_by_uuid(_UUID_A, user_id=1) is None

    def test_garbage_file_returns_none(self, tmp_store):
        path = paths.uuid_creds_path(1, _UUID_A)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a valid fernet token")
        assert credentials.get_keys_by_uuid(_UUID_A, user_id=1) is None


# ── Per-user isolation — the multi-tenant security property ───────────────


class TestPerUserIsolation:
    """user 2 must not be able to read user 1's exchange credentials.
    Each user has a distinct Fernet key + distinct .enc directory, so
    a UUID lookup under user 2's tree naturally misses user 1's blob."""

    def test_user_1_and_user_2_have_different_keys(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
        )
        credentials.save_keys_by_uuid(
            _UUID_USER_2, "bitget", "c", "d", user_id=2,
        )
        key1 = paths.user_fernet_key_path(1).read_bytes()
        key2 = paths.user_fernet_key_path(2).read_bytes()
        assert key1 != key2

    def test_get_keys_isolates_across_users(self, tmp_store):
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "u1-ak", "u1-sc", user_id=1,
        )
        credentials.save_keys_by_uuid(
            _UUID_USER_2, "bitget", "u2-ak", "u2-sc", user_id=2,
        )
        u1 = credentials.get_keys_by_uuid(_UUID_A, user_id=1)
        u2 = credentials.get_keys_by_uuid(_UUID_USER_2, user_id=2)
        assert u1 == {"api_key": "u1-ak", "api_secret": "u1-sc", "passphrase": ""}
        assert u2 == {"api_key": "u2-ak", "api_secret": "u2-sc", "passphrase": ""}

    def test_user_2_cannot_decrypt_user_1_blob(self, tmp_store):
        """Plant user 1's blob bytes under user 2's tree and try to
        read them with user 2's key — must return None (different
        Fernet keys) rather than leak plaintext."""
        credentials.save_keys_by_uuid(
            _UUID_A, "bitget", "secret-one", "secret-sec",
            user_id=1,
        )
        # Ensure user 2 has their own key on disk first.
        credentials.save_keys_by_uuid(
            _UUID_USER_2, "kraken", "x", "y", user_id=2,
        )

        u1_blob = paths.uuid_creds_path(1, _UUID_A).read_bytes()
        u2_path = paths.uuid_creds_path(2, _UUID_A)
        u2_path.write_bytes(u1_blob)

        assert credentials.get_keys_by_uuid(_UUID_A, user_id=2) is None


# ── Provider abstraction ───────────────────────────────────────────────────


class TestCredentialProviderInterface:
    """The provider seam (ABC + concrete Fernet impl) exists so a
    future signing-service backend can drop in without touching call
    sites. These tests pin the contract so a future refactor can't
    quietly remove the abstraction or skip a method."""

    def test_default_provider_is_fernet(self):
        provider = credentials.get_default_provider()
        assert isinstance(provider, credentials.FernetCredentialProvider)
        assert isinstance(provider, credentials.CredentialProvider)

    def test_credential_provider_abc_declares_full_surface(self):
        """A new backend must implement every method the existing
        callers already rely on. Pin the abstract method set so an
        ABC trim-down cannot silently break a future drop-in."""
        abstracts = credentials.CredentialProvider.__abstractmethods__
        assert abstracts == {
            "save_keys_by_uuid",
            "get_keys_by_uuid",
            "delete_keys_by_uuid",
            "rotate_user_key",
            "encrypt_for_user",
            "decrypt_for_user",
        }

    def test_module_shims_delegate_to_default_provider(
        self, tmp_store, monkeypatch,
    ):
        """``credentials.get_keys_by_uuid`` (module-level shim) must
        route through ``_default_provider`` — not have its own
        duplicated implementation. Swap the default for a stub and
        confirm the stub sees the call.
        """

        class _RecordingProvider(credentials.CredentialProvider):
            def __init__(self):
                self.calls: list[tuple] = []

            def save_keys_by_uuid(
                self, credentials_uuid, exchange_type, api_key,
                api_secret, user_id, *, passphrase="",
            ):
                self.calls.append((
                    "save_keys_by_uuid",
                    credentials_uuid, exchange_type, user_id, passphrase,
                ))

            def get_keys_by_uuid(self, credentials_uuid, user_id):
                self.calls.append(
                    ("get_keys_by_uuid", credentials_uuid, user_id),
                )
                return {
                    "api_key": "stub", "api_secret": "stub",
                    "passphrase": "",
                }

            def delete_keys_by_uuid(self, credentials_uuid, user_id):
                return True

            def rotate_user_key(self, user_id=1, retention_days=7):
                return {"user_id": user_id}

            def encrypt_for_user(self, user_id, plaintext):
                return f"enc:{plaintext}"

            def decrypt_for_user(self, user_id, ciphertext):
                assert ciphertext.startswith("enc:")
                return ciphertext[4:]

        original = credentials.get_default_provider()
        stub = _RecordingProvider()
        try:
            credentials.set_default_provider(stub)
            credentials.save_keys_by_uuid(
                _UUID_A, "bitget", "k", "s", user_id=9,
                passphrase="pp",
            )
            result = credentials.get_keys_by_uuid(_UUID_A, user_id=9)
        finally:
            credentials.set_default_provider(original)

        assert ("save_keys_by_uuid", _UUID_A, "bitget", 9, "pp") in stub.calls
        assert ("get_keys_by_uuid", _UUID_A, 9) in stub.calls
        assert result == {
            "api_key": "stub", "api_secret": "stub", "passphrase": "",
        }
