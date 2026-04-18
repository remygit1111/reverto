"""Tests for core.credentials.rotate_fernet_key.

Each test routes the credentials + keyfile paths at tmp_path so the
real logs/.credentials.key is never touched.
"""

import json
import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from core import credentials as creds  # noqa: E402


@pytest.fixture
def routed_store(tmp_path, monkeypatch):
    """Redirect the credentials module's paths at tmp_path. Return
    (keyfile, store_file) for the test to poke at directly."""
    key = tmp_path / ".credentials.key"
    store = tmp_path / "credentials.json"
    monkeypatch.setattr(creds, "_KEY_FILE", key)
    monkeypatch.setattr(creds, "_STORE_FILE", store)
    return key, store


class TestRotateFernetKey:

    def test_preserves_credentials(self, routed_store):
        """After rotate, get_keys must return the same plaintext values
        under the new master key."""
        key, store = routed_store
        creds.save_keys("bitget", "api_abc", "secret_xyz")
        creds.save_keys("kraken", "k_api", "k_secret")

        # Sanity: before rotate.
        before = creds.get_keys("bitget")
        assert before == {"api_key": "api_abc", "api_secret": "secret_xyz"}

        old_key_bytes = key.read_bytes()
        creds.rotate_fernet_key(credentials_file=store, keyfile=key)

        # Key file actually changed.
        assert key.read_bytes() != old_key_bytes

        # Plaintext survives the round-trip.
        assert creds.get_keys("bitget") == {
            "api_key": "api_abc", "api_secret": "secret_xyz",
        }
        assert creds.get_keys("kraken") == {
            "api_key": "k_api", "api_secret": "k_secret",
        }

    def test_backup_file_created(self, routed_store):
        key, store = routed_store
        creds.save_keys("bitget", "a", "b")
        old_key_bytes = key.read_bytes()

        result = creds.rotate_fernet_key(credentials_file=store, keyfile=key)

        # Backup filename is timestamped — glob for the expected pattern.
        backups = list(key.parent.glob(key.name + ".bak.*"))
        assert len(backups) == 1, f"expected 1 backup, got {backups}"
        assert backups[0].read_bytes() == old_key_bytes
        assert result["backup_path"] == str(backups[0])

    def test_refuses_when_key_missing(self, routed_store):
        """No key file → nothing to rotate."""
        key, store = routed_store
        # Fresh dirs — no key ever created.
        with pytest.raises(FileNotFoundError):
            creds.rotate_fernet_key(credentials_file=store, keyfile=key)

    def test_ciphertext_actually_changes(self, routed_store):
        """After rotate, the stored ciphertext bytes must differ — if
        they don't we silently didn't re-encrypt."""
        key, store = routed_store
        creds.save_keys("bitget", "a", "b")
        ct_before = json.loads(store.read_text())["bitget"]["api_key"]

        creds.rotate_fernet_key(credentials_file=store, keyfile=key)
        ct_after = json.loads(store.read_text())["bitget"]["api_key"]

        assert ct_before != ct_after

    def test_result_summary_shape(self, routed_store):
        key, store = routed_store
        creds.save_keys("bitget", "a", "b")
        creds.save_keys("kraken", "c", "d")
        result = creds.rotate_fernet_key(credentials_file=store, keyfile=key)
        assert result["keys_rotated"] == ["bitget", "kraken"]
        assert "rotated_at" in result
        assert "backup_path" in result
