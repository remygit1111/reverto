"""Tests for core/paths.py — the multi-tenant filesystem helper.

Every helper is exercised through a monkey-patched ``core.paths.BASE_DIR``
pointing at tmp_path so the real repository tree is never touched.
The helpers auto-create parents with mkdir(exist_ok=True), so the
assertions focus on (a) the path string is correct and (b) secret
dirs (keys/, credentials/<uid>/) land at mode 0700.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths


@pytest.fixture(autouse=True)
def _sandboxed_base(tmp_path, monkeypatch):
    """Redirect BASE_DIR → tmp_path. Every helper reads BASE_DIR lazily
    (no module-level caching), so this one knob sandboxes the whole
    tree."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    yield tmp_path


# ── Path shape ──────────────────────────────────────────────────────────────


class TestPathShape:
    """Every helper must produce the documented layout under BASE_DIR."""

    def test_bot_yaml_path(self, _sandboxed_base):
        p = paths.bot_yaml_path(1, "rsi_test")
        assert p == _sandboxed_base / "config" / "bots" / "1" / "rsi_test.yaml"

    def test_bot_log_path(self, _sandboxed_base):
        p = paths.bot_log_path(1, "rsi_test")
        assert p == _sandboxed_base / "logs" / "1" / "rsi_test.log"

    def test_bot_state_path(self, _sandboxed_base):
        p = paths.bot_state_path(1, "rsi_test")
        assert p == _sandboxed_base / "logs" / "1" / "rsi_test.state.json"

    def test_bot_pid_path(self, _sandboxed_base):
        p = paths.bot_pid_path(1, "rsi_test")
        assert p == _sandboxed_base / "logs" / "1" / "pids" / "rsi_test.pid"

    def test_bot_manual_trigger_path(self, _sandboxed_base):
        p = paths.bot_manual_trigger_path(1, "rsi_test")
        assert p == (
            _sandboxed_base / "logs" / "1" / "rsi_test.manual_trigger"
        )

    def test_user_fernet_key_path(self, _sandboxed_base):
        p = paths.user_fernet_key_path(1)
        assert p == _sandboxed_base / "keys" / "1.key"

    def test_exchange_creds_path(self, _sandboxed_base):
        p = paths.exchange_creds_path(1, "bitget")
        assert p == _sandboxed_base / "credentials" / "1" / "bitget.enc"

    def test_user_ids_partition_cleanly(self, _sandboxed_base):
        """Two users land in separate trees — the core multi-tenant
        isolation invariant for filesystem paths."""
        assert paths.bot_yaml_path(1, "shared") != paths.bot_yaml_path(
            2, "shared",
        )
        assert paths.user_fernet_key_path(1) != paths.user_fernet_key_path(2)
        assert paths.exchange_creds_path(1, "bitget") != paths.exchange_creds_path(
            2, "bitget",
        )


# ── Directory creation + permissions ───────────────────────────────────────


class TestDirCreation:
    """Helpers must create parents on demand so callers never have to
    `mkdir(parents=True)` by hand."""

    def test_user_bots_dir_created(self, _sandboxed_base):
        d = paths.user_bots_dir(1)
        assert d.exists() and d.is_dir()

    def test_user_logs_dir_created(self, _sandboxed_base):
        d = paths.user_logs_dir(1)
        assert d.exists() and d.is_dir()

    def test_user_pid_dir_created(self, _sandboxed_base):
        d = paths.user_pid_dir(1)
        assert d.exists() and d.is_dir()
        # Parent logs/<uid>/ must also exist.
        assert d.parent.exists()

    def test_user_credentials_dir_is_0700(self, _sandboxed_base):
        d = paths.user_credentials_dir(1)
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700, f"credentials dir mode {oct(mode)}, expected 0700"

    def test_user_keys_dir_is_0700(self, _sandboxed_base):
        d = paths.user_keys_dir()
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700, f"keys dir mode {oct(mode)}, expected 0700"


# ── Idempotence ────────────────────────────────────────────────────────────


class TestIdempotence:
    """Calling the same helper twice must not raise — the Phase-2 boot
    path relies on repeated mkdirs during the registry-refresh loop."""

    def test_user_bots_dir_twice(self, _sandboxed_base):
        a = paths.user_bots_dir(1)
        b = paths.user_bots_dir(1)
        assert a == b and a.exists()

    def test_user_keys_dir_twice(self, _sandboxed_base):
        a = paths.user_keys_dir()
        b = paths.user_keys_dir()
        assert a == b

    def test_user_credentials_dir_twice(self, _sandboxed_base):
        a = paths.user_credentials_dir(1)
        b = paths.user_credentials_dir(1)
        assert a == b


# ── ensure_secret_file_mode ────────────────────────────────────────────────


class TestSecretFileMode:

    def test_chmod_0600(self, tmp_path):
        f = tmp_path / "secret"
        f.write_bytes(b"shh")
        paths.ensure_secret_file_mode(f)
        mode = f.stat().st_mode & 0o777
        assert mode == 0o600

    def test_missing_file_is_noop(self, tmp_path):
        """Best-effort — a missing file must not raise OSError out
        of the helper. Callers use it right after a .tmp → dst
        rename where the path may briefly not exist."""
        paths.ensure_secret_file_mode(tmp_path / "does-not-exist")


# ── Cross-module smoke: credentials land under the sandboxed tree ──────────


class TestIntegrationWithCredentials:
    """A sanity check that monkey-patching BASE_DIR actually redirects
    the credentials module's writes — without this, tests elsewhere
    would silently write to the real keys/ + credentials/ trees."""

    def test_save_keys_lands_under_sandbox(self, _sandboxed_base, monkeypatch):
        from core import credentials

        # credentials.py caches its own _BASE_DIR/_LOG_DIR/_KEY_FILE at
        # import time for the system-key fallback — patch them too so
        # the .auth.json path stays out of the real logs/.
        monkeypatch.setattr(credentials, "_BASE_DIR", _sandboxed_base)
        monkeypatch.setattr(credentials, "_LOG_DIR", _sandboxed_base / "logs")
        monkeypatch.setattr(
            credentials, "_KEY_FILE",
            _sandboxed_base / "logs" / ".credentials.key",
        )

        credentials.save_keys("bitget", "ak", "sc", user_id=7)
        enc = paths.exchange_creds_path(7, "bitget")
        key = paths.user_fernet_key_path(7)

        assert enc.exists()
        assert key.exists()
        # Both must live under the sandbox — not the real repo.
        assert str(enc).startswith(str(_sandboxed_base))
        assert str(key).startswith(str(_sandboxed_base))


# ── Regression — relative path computation ─────────────────────────────────


class TestRelativeToBase:
    """After the Phase-2 migration, ``BotInfo.config_file`` stores
    config/bots/<user_id>/<slug>.yaml as a path relative to BASE_DIR.
    This mirrors what the registry loader produces — tests pinned the
    old flat shape, so re-verify the new one here."""

    def test_yaml_is_relative_to_base(self, _sandboxed_base):
        full = paths.bot_yaml_path(1, "rsi_test")
        rel = full.relative_to(_sandboxed_base)
        assert rel == Path("config/bots/1/rsi_test.yaml")
