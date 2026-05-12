"""Tests for core.credentials.rotate_fernet_key under the UUID-keyed
multi-account layout.

Rotation is per-user: a single call rotates one user's Fernet key +
re-encrypts every .enc file in that user's credentials/<uid>/ tree
regardless of which exchange-type each blob carries.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from core import credentials as creds  # noqa: E402
from core import paths  # noqa: E402


_UUID_A = "aa11" * 8
_UUID_B = "bb22" * 8


@pytest.fixture
def routed_store(tmp_path, monkeypatch):
    """Redirect the path tree at tmp_path. Returns the key file path
    so individual tests can read it directly."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(creds, "_BASE_DIR", tmp_path)
    return paths.user_fernet_key_path(1)


class TestRotateFernetKey:

    def test_preserves_credentials(self, routed_store):
        key = routed_store
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "api_abc", "secret_xyz",
            user_id=1,
        )
        creds.save_keys_by_uuid(
            _UUID_B, "kraken", "k_api", "k_secret",
            user_id=1,
        )

        before = creds.get_keys_by_uuid(_UUID_A, user_id=1)
        assert before == {
            "api_key": "api_abc", "api_secret": "secret_xyz",
            "passphrase": "",
        }

        old_key_bytes = key.read_bytes()
        creds.rotate_fernet_key(user_id=1)

        assert key.read_bytes() != old_key_bytes

        assert creds.get_keys_by_uuid(_UUID_A, user_id=1) == {
            "api_key": "api_abc", "api_secret": "secret_xyz",
            "passphrase": "",
        }
        assert creds.get_keys_by_uuid(_UUID_B, user_id=1) == {
            "api_key": "k_api", "api_secret": "k_secret",
            "passphrase": "",
        }

    def test_passphrase_survives_rotation(self, routed_store):
        # Save a Bitget credential WITH a passphrase, rotate, and
        # verify the passphrase decrypts cleanly under the new key.
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "ak", "sc",
            user_id=1, passphrase="rotate-me",
        )
        creds.rotate_fernet_key(user_id=1)
        stored = creds.get_keys_by_uuid(_UUID_A, user_id=1)
        assert stored == {
            "api_key": "ak", "api_secret": "sc",
            "passphrase": "rotate-me",
        }

    def test_backup_file_created(self, routed_store):
        key = routed_store
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
        )
        old_key_bytes = key.read_bytes()

        result = creds.rotate_fernet_key(user_id=1)

        backups = list(key.parent.glob(key.name + ".bak.*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == old_key_bytes
        assert result["backup_path"] == str(backups[0])
        assert result["user_id"] == 1

    def test_refuses_when_key_missing(self, routed_store):
        """No key file → nothing to rotate."""
        with pytest.raises(FileNotFoundError):
            creds.rotate_fernet_key(user_id=1)

    def test_ciphertext_actually_changes(self, routed_store):
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
        )
        enc_path = paths.uuid_creds_path(1, _UUID_A)
        ct_before = enc_path.read_bytes()

        creds.rotate_fernet_key(user_id=1)
        ct_after = enc_path.read_bytes()

        assert ct_before != ct_after

    def test_result_summary_shape(self, routed_store):
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
        )
        creds.save_keys_by_uuid(
            _UUID_B, "kraken", "c", "d", user_id=1,
        )
        result = creds.rotate_fernet_key(user_id=1)
        # UUIDs come back sorted; the exact pair we wrote is in there.
        assert sorted(result["keys_rotated"]) == sorted([_UUID_A, _UUID_B])
        assert "rotated_at" in result
        assert "backup_path" in result
        assert result["user_id"] == 1

    def test_rotating_user_1_leaves_user_2_alone(self, routed_store):
        """Per-user scoping: rotating user 1's key must NOT touch the
        user 2 tree."""
        _UUID_U2 = "cc33" * 8
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a1", "s1", user_id=1,
        )
        creds.save_keys_by_uuid(
            _UUID_U2, "bitget", "a2", "s2", user_id=2,
        )
        u2_key_before = paths.user_fernet_key_path(2).read_bytes()
        u2_enc_before = paths.uuid_creds_path(2, _UUID_U2).read_bytes()

        creds.rotate_fernet_key(user_id=1)

        assert paths.user_fernet_key_path(2).read_bytes() == u2_key_before
        assert paths.uuid_creds_path(2, _UUID_U2).read_bytes() == u2_enc_before
        assert creds.get_keys_by_uuid(_UUID_U2, user_id=2) == {
            "api_key": "a2", "api_secret": "s2", "passphrase": "",
        }
