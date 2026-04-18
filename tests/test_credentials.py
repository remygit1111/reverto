# tests/test_credentials.py
# Tests voor core.credentials — Fernet-encrypted exchange key store.
#
# Elke test isoleert de module-level paden (_KEY_FILE, _STORE_FILE,
# _LOG_DIR) via monkeypatch naar een tmp_path, zodat we nooit aan
# logs/credentials.json of logs/.credentials.key van de echte portal
# komen.

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import credentials


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Isolate the credentials module against a per-test tmp dir."""
    key_file = tmp_path / ".credentials.key"
    store_file = tmp_path / "credentials.json"
    monkeypatch.setattr(credentials, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(credentials, "_KEY_FILE", key_file)
    monkeypatch.setattr(credentials, "_STORE_FILE", store_file)
    return tmp_path


class TestMasterKey:
    def test_key_file_auto_created_on_first_use(self, tmp_store):
        key_file = tmp_store / ".credentials.key"
        assert not key_file.exists()
        # Any call that needs the fernet instance creates the key
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        assert key_file.exists()
        assert len(key_file.read_bytes()) > 0

    def test_key_file_reused_across_calls(self, tmp_store):
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        first = (tmp_store / ".credentials.key").read_bytes()
        credentials.save_keys("kraken", "ak2", "sc2", user_id=1)
        second = (tmp_store / ".credentials.key").read_bytes()
        assert first == second, "master key must not rotate between calls"


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
        credentials.save_keys("bitget", "plain-text-key", "plain-text-secret", user_id=1)
        raw = (tmp_store / "credentials.json").read_text(encoding="utf-8")
        # Secrets must never appear verbatim on disk
        assert "plain-text-key" not in raw
        assert "plain-text-secret" not in raw
        # But the store is valid JSON with a bitget entry
        parsed = json.loads(raw)
        assert "bitget" in parsed
        assert "api_key" in parsed["bitget"]


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
        assert credentials.get_keys("kraken", user_id=1) == {"api_key": "k", "api_secret": "K"}


class TestListExchanges:
    def test_empty(self, tmp_store):
        assert credentials.list_exchanges_with_keys(user_id=1) == []

    def test_after_save(self, tmp_store):
        credentials.save_keys("kraken", "k", "K", user_id=1)
        credentials.save_keys("bitget", "b", "B", user_id=1)
        # Sorted alphabetically
        assert credentials.list_exchanges_with_keys(user_id=1) == ["bitget", "kraken"]

    def test_after_delete(self, tmp_store):
        credentials.save_keys("bitget", "b", "B", user_id=1)
        credentials.save_keys("kraken", "k", "K", user_id=1)
        credentials.delete_keys("bitget", user_id=1)
        assert credentials.list_exchanges_with_keys(user_id=1) == ["kraken"]


class TestDecryptFailure:
    def test_tampered_ciphertext_returns_none(self, tmp_store):
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        # Tamper with the stored ciphertext — flip a byte
        store_path = tmp_store / "credentials.json"
        store = json.loads(store_path.read_text(encoding="utf-8"))
        store["bitget"]["api_key"] = "gAAAAAB_invalid_token_that_cannot_decrypt_=="
        store_path.write_text(json.dumps(store), encoding="utf-8")

        # get_keys must return None (no exception leaked to caller)
        result = credentials.get_keys("bitget", user_id=1)
        assert result is None

    def test_missing_field_returns_none(self, tmp_store):
        credentials.save_keys("bitget", "ak", "sc", user_id=1)
        store_path = tmp_store / "credentials.json"
        store = json.loads(store_path.read_text(encoding="utf-8"))
        del store["bitget"]["api_secret"]  # half-corrupt entry
        store_path.write_text(json.dumps(store), encoding="utf-8")

        assert credentials.get_keys("bitget", user_id=1) is None

    def test_corrupt_store_file_treated_as_empty(self, tmp_store):
        # Write junk where the JSON should be
        (tmp_store / "credentials.json").write_text("not json", encoding="utf-8")
        assert credentials.has_keys("bitget", user_id=1) is False
        assert credentials.get_keys("bitget", user_id=1) is None
        assert credentials.list_exchanges_with_keys(user_id=1) == []
