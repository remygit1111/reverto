"""Tests for scripts/migrate_to_user_fs.py.

The migration moves Phase-1 flat assets under user 1/ subdirectories.
Each test sandboxes the filesystem by redirecting the module's
``BASE`` constant to tmp_path so the real repo tree is never touched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import migrate_to_user_fs as mig


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point migrate's BASE + the credentials module's filesystem
    root at tmp_path so every move/write lands inside the test fixture."""
    monkeypatch.setattr(mig, "BASE", tmp_path)
    from core import credentials, paths as path_mod
    monkeypatch.setattr(path_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "_BASE_DIR", tmp_path)
    # Audit v26-06: pre-Phase-3a _LOG_DIR / _KEY_FILE monkeypatches
    # gesandboxed de system-key voor .auth.json; die helpers zijn
    # verwijderd, dus alleen _BASE_DIR sandboxing blijft over.
    return tmp_path


def _write(path: Path, content: str = "placeholder") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── Bot configs ─────────────────────────────────────────────────────────────


class TestConfigMigration:

    def test_moves_flat_yaml_to_user_dir(self, sandbox):
        _write(sandbox / "config" / "bots" / "rsi.yaml", "bot: {}")
        _write(sandbox / "config" / "bots" / "btc.yaml", "bot: {}")
        moved = mig.migrate_bot_configs()
        assert moved == 2
        assert (sandbox / "config" / "bots" / "1" / "rsi.yaml").exists()
        assert (sandbox / "config" / "bots" / "1" / "btc.yaml").exists()
        # Source locations are gone.
        assert not (sandbox / "config" / "bots" / "rsi.yaml").exists()

    def test_leaves_already_migrated_yaml_alone(self, sandbox):
        _write(sandbox / "config" / "bots" / "1" / "rsi.yaml", "bot: {}")
        moved = mig.migrate_bot_configs()
        assert moved == 0

    def test_skip_on_collision(self, sandbox):
        """If user 1 already has a file with the same slug, abort —
        the operator needs to inspect before we clobber anything."""
        _write(sandbox / "config" / "bots" / "rsi.yaml", "new")
        _write(sandbox / "config" / "bots" / "1" / "rsi.yaml", "existing")
        moved = mig.migrate_bot_configs()
        assert moved == 0
        # Flat file still there (not moved).
        assert (sandbox / "config" / "bots" / "rsi.yaml").exists()
        # Existing 1/rsi.yaml preserved byte-for-byte.
        assert (sandbox / "config" / "bots" / "1" / "rsi.yaml").read_text() == "existing"


# ── State / log / trigger files ─────────────────────────────────────────────


class TestLogsMigration:

    def test_moves_state_log_and_trigger(self, sandbox):
        _write(sandbox / "logs" / "rsi.state.json", "{}")
        _write(sandbox / "logs" / "rsi.log", "line")
        _write(sandbox / "logs" / "rsi.manual_trigger", "")
        moved = mig.migrate_logs_and_state()
        assert moved == 3
        assert (sandbox / "logs" / "1" / "rsi.state.json").exists()
        assert (sandbox / "logs" / "1" / "rsi.log").exists()
        assert (sandbox / "logs" / "1" / "rsi.manual_trigger").exists()

    def test_leaves_system_files(self, sandbox):
        """reverto.db, audit.log, .credentials.key, etc. must stay at
        logs/ root — they are NOT per-bot state."""
        _write(sandbox / "logs" / "reverto.db", "sqlite")
        _write(sandbox / "logs" / "audit.log", "audit")
        _write(sandbox / "logs" / "portal.log", "portal")
        _write(sandbox / "logs" / ".credentials.key", "key")
        _write(sandbox / "logs" / "credentials.json", "{}")
        _write(sandbox / "logs" / ".auth.json", "bytes")
        _write(sandbox / "logs" / "audit.log.1", "rotated")
        _write(sandbox / "logs" / ".credentials.key.bak.20240101000000", "bak")

        moved = mig.migrate_logs_and_state()
        assert moved == 0
        # Every system file still where it was.
        for name in (
            "reverto.db", "audit.log", "portal.log", ".credentials.key",
            "credentials.json", ".auth.json", "audit.log.1",
            ".credentials.key.bak.20240101000000",
        ):
            assert (sandbox / "logs" / name).exists(), (
                f"{name} wrongly moved/dropped"
            )

    def test_ignores_unknown_suffix(self, sandbox):
        """Operator-placed files with unknown suffixes stay where they
        are so we never accidentally swallow manual backup notes."""
        _write(sandbox / "logs" / "notes.md", "# remember")
        moved = mig.migrate_logs_and_state()
        assert moved == 0
        assert (sandbox / "logs" / "notes.md").exists()


class TestPidMigration:

    def test_moves_pid_files(self, sandbox):
        _write(sandbox / "logs" / "pids" / "rsi.pid", "12345")
        moved = mig.migrate_pid_files()
        assert moved == 1
        assert (sandbox / "logs" / "1" / "pids" / "rsi.pid").exists()
        assert (sandbox / "logs" / "1" / "pids" / "rsi.pid").read_text() == "12345"

    def test_missing_pids_dir_no_error(self, sandbox):
        moved = mig.migrate_pid_files()
        assert moved == 0


# ── Credentials conversion ──────────────────────────────────────────────────


class TestCredentialsMigration:

    def _write_legacy_credentials(
        self, sandbox, payload: dict[str, tuple[str, str]],
    ) -> None:
        """Create a pre-MT logs/credentials.json + logs/.credentials.key
        with the given (exchange → (api_key, api_secret)) plaintext."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        f = Fernet(key)

        (sandbox / "logs").mkdir(parents=True, exist_ok=True)
        (sandbox / "logs" / ".credentials.key").write_bytes(key)

        store = {}
        for exchange, (api_key, api_secret) in payload.items():
            store[exchange] = {
                "api_key":    f.encrypt(api_key.encode()).decode("ascii"),
                "api_secret": f.encrypt(api_secret.encode()).decode("ascii"),
            }
        (sandbox / "logs" / "credentials.json").write_text(
            json.dumps(store), encoding="utf-8",
        )

    def test_no_credentials_file_is_noop(self, sandbox):
        moved = mig.migrate_credentials()
        assert moved == 0

    def test_converts_legacy_to_per_user_enc(self, sandbox):
        self._write_legacy_credentials(sandbox, {
            "bitget": ("ak-b", "sc-b"),
            "kraken": ("ak-k", "sc-k"),
        })
        moved = mig.migrate_credentials()
        assert moved == 2

        # Phase-2 artefacts exist.
        assert (sandbox / "keys" / "1.key").exists()
        assert (sandbox / "credentials" / "1" / "bitget.enc").exists()
        assert (sandbox / "credentials" / "1" / "kraken.enc").exists()

        # Re-read through the Phase-2 API returns the original plaintext.
        from core import credentials
        assert credentials.get_keys("bitget", user_id=1) == {
            "api_key": "ak-b", "api_secret": "sc-b",
        }
        assert credentials.get_keys("kraken", user_id=1) == {
            "api_key": "ak-k", "api_secret": "sc-k",
        }


# ── End-to-end idempotence ──────────────────────────────────────────────────


class TestIdempotence:

    def test_running_twice_is_safe(self, sandbox):
        """A re-run after everything already moved must produce no
        moves and not crash."""
        _write(sandbox / "config" / "bots" / "rsi.yaml", "bot: {}")
        _write(sandbox / "logs" / "rsi.state.json", "{}")
        _write(sandbox / "logs" / "pids" / "rsi.pid", "1")

        assert mig.main() == 0
        # Second run: everything is already at the destination, no src
        # files to move, nothing to convert.
        assert mig.main() == 0

        # Final state: nothing left at the flat locations.
        assert not (sandbox / "config" / "bots" / "rsi.yaml").exists()
        assert not (sandbox / "logs" / "rsi.state.json").exists()
        assert not (sandbox / "logs" / "pids" / "rsi.pid").exists()
        # But the moved files are all in place.
        assert (sandbox / "config" / "bots" / "1" / "rsi.yaml").exists()
        assert (sandbox / "logs" / "1" / "rsi.state.json").exists()
        assert (sandbox / "logs" / "1" / "pids" / "rsi.pid").exists()
