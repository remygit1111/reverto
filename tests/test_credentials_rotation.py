"""Tests for core.credentials.rotate_fernet_key.

Phase-2 rotation is per-user: a single call rotates one user's Fernet
key + re-encrypts every .enc file in that user's credentials/<uid>/
tree. Tests redirect core.paths.BASE_DIR at tmp_path so the real
keys/ tree stays untouched.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from core import credentials as creds  # noqa: E402
from core import paths  # noqa: E402


@pytest.fixture
def routed_store(tmp_path, monkeypatch):
    """Redirect the Phase-2 path tree at tmp_path. Returns the key
    file path so individual tests can read it directly."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(creds, "_BASE_DIR", tmp_path)
    # Audit v26-06: pre-Phase-3a _LOG_DIR / _KEY_FILE monkeypatches
    # gesandboxed de system-key voor .auth.json; die helpers zijn
    # verwijderd, dus alleen _BASE_DIR sandboxing blijft over.
    return paths.user_fernet_key_path(1)


class TestRotateFernetKey:

    def test_preserves_credentials(self, routed_store):
        """After rotate, get_keys must return the same plaintext values
        under the new per-user Fernet key."""
        key = routed_store
        creds.save_keys("bitget", "api_abc", "secret_xyz", user_id=1)
        creds.save_keys("kraken", "k_api", "k_secret", user_id=1)

        # Sanity: before rotate.
        before = creds.get_keys("bitget", user_id=1)
        assert before == {"api_key": "api_abc", "api_secret": "secret_xyz"}

        old_key_bytes = key.read_bytes()
        creds.rotate_fernet_key(user_id=1)

        # Key file actually changed.
        assert key.read_bytes() != old_key_bytes

        # Plaintext survives the round-trip.
        assert creds.get_keys("bitget", user_id=1) == {
            "api_key": "api_abc", "api_secret": "secret_xyz",
        }
        assert creds.get_keys("kraken", user_id=1) == {
            "api_key": "k_api", "api_secret": "k_secret",
        }

    def test_backup_file_created(self, routed_store):
        key = routed_store
        creds.save_keys("bitget", "a", "b", user_id=1)
        old_key_bytes = key.read_bytes()

        result = creds.rotate_fernet_key(user_id=1)

        # Backup filename is timestamped — glob for the expected pattern.
        backups = list(key.parent.glob(key.name + ".bak.*"))
        assert len(backups) == 1, f"expected 1 backup, got {backups}"
        assert backups[0].read_bytes() == old_key_bytes
        assert result["backup_path"] == str(backups[0])
        assert result["user_id"] == 1

    def test_refuses_when_key_missing(self, routed_store):
        """No key file → nothing to rotate."""
        # Fresh dirs — no save_keys ever called → no key file.
        with pytest.raises(FileNotFoundError):
            creds.rotate_fernet_key(user_id=1)

    def test_ciphertext_actually_changes(self, routed_store):
        """After rotate, the stored .enc bytes must differ — if they
        don't we silently didn't re-encrypt."""
        creds.save_keys("bitget", "a", "b", user_id=1)
        enc_path = paths.exchange_creds_path(1, "bitget")
        ct_before = enc_path.read_bytes()

        creds.rotate_fernet_key(user_id=1)
        ct_after = enc_path.read_bytes()

        assert ct_before != ct_after

    def test_result_summary_shape(self, routed_store):
        creds.save_keys("bitget", "a", "b", user_id=1)
        creds.save_keys("kraken", "c", "d", user_id=1)
        result = creds.rotate_fernet_key(user_id=1)
        assert result["keys_rotated"] == ["bitget", "kraken"]
        assert "rotated_at" in result
        assert "backup_path" in result
        assert result["user_id"] == 1

    def test_rotating_user_1_leaves_user_2_alone(self, routed_store):
        """Per-user scoping: rotating user 1's key must NOT touch the
        user 2 tree. This is the isolation invariant Phase-2 added."""
        creds.save_keys("bitget", "a1", "s1", user_id=1)
        creds.save_keys("bitget", "a2", "s2", user_id=2)
        u2_key_before = paths.user_fernet_key_path(2).read_bytes()
        u2_enc_before = paths.exchange_creds_path(2, "bitget").read_bytes()

        creds.rotate_fernet_key(user_id=1)

        assert paths.user_fernet_key_path(2).read_bytes() == u2_key_before
        assert paths.exchange_creds_path(2, "bitget").read_bytes() == u2_enc_before
        # And user 2's plaintext still decrypts.
        assert creds.get_keys("bitget", user_id=2) == {
            "api_key": "a2", "api_secret": "s2",
        }
