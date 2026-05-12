"""Tests for Fernet rotation locking + backup retention + reverse-order commit.

Spec items:
  * rotate_fernet_key uses _rotation_lock — concurrent rotations on the
    SAME user are refused.
  * Backups are timestamped (not a single .bak file).
  * cleanup_old_backups deletes backups older than retention_days.
  * The replace order is key-before-creds so a crash mid-rotation
    leaves a recoverable intermediate state.
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from core import credentials as creds  # noqa: E402
from core import paths  # noqa: E402


_UUID_A = "aa11" * 8


@pytest.fixture
def routed_store(tmp_path, monkeypatch):
    """Redirect the filesystem tree at tmp_path. Returns the per-user
    key Path for the test to assert on directly."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(creds, "_BASE_DIR", tmp_path)
    return paths.user_fernet_key_path(1)


class TestRotationLock:

    def test_second_rotation_refused_while_first_holds_lock(self, routed_store):
        key = routed_store
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
            _skip_format_validation=True,
        )

        cm = creds._rotation_lock(key)
        cm.__enter__()
        try:
            with pytest.raises(RuntimeError, match="Another Fernet rotation"):
                creds.rotate_fernet_key(user_id=1)
        finally:
            cm.__exit__(None, None, None)

    def test_lock_released_after_successful_rotate(self, routed_store):
        """After a successful rotate the lock file is gone, so a second
        rotate succeeds."""
        key = routed_store
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
            _skip_format_validation=True,
        )

        creds.rotate_fernet_key(user_id=1)
        creds.rotate_fernet_key(user_id=1)

        backups = list(key.parent.glob(key.name + ".bak.*"))
        assert len(backups) == 2


class TestTimestampedBackups:

    def test_each_rotation_creates_a_fresh_backup(self, routed_store):
        key = routed_store
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
            _skip_format_validation=True,
        )

        creds.rotate_fernet_key(user_id=1)
        time.sleep(1.1)
        creds.rotate_fernet_key(user_id=1)

        backups = sorted(key.parent.glob(key.name + ".bak.*"))
        assert len(backups) == 2
        for b in backups:
            stamp = b.name.rsplit(".bak.", 1)[1]
            assert stamp.isdigit() and len(stamp) == 20


class TestBackupRetention:

    def test_cleanup_removes_only_old_backups(self, routed_store):
        key = routed_store
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
            _skip_format_validation=True,
        )

        creds.rotate_fernet_key(user_id=1)

        old = key.with_suffix(key.suffix + ".bak.20200101000000")
        old.write_bytes(b"fake old key")
        past = time.time() - 30 * 86400
        os.utime(old, (past, past))

        removed = creds.cleanup_old_backups(key, retention_days=7)
        assert old.name in removed
        assert not old.exists()
        recent = list(key.parent.glob(key.name + ".bak.2026*"))
        assert len(recent) == 1


class TestCrashRecovery:

    def test_key_is_replaced_before_creds(self, routed_store, monkeypatch):
        """Regression pin: the rotation flips the key file first so a
        crash between the two replaces is recoverable from the .bak."""
        key = routed_store
        creds.save_keys_by_uuid(
            _UUID_A, "bitget", "a", "b", user_id=1,
            _skip_format_validation=True,
        )
        enc_path = paths.uuid_creds_path(1, _UUID_A)
        original_key = key.read_bytes()

        order: list[str] = []
        real_replace = os.replace

        def tracking_replace(src, dst):
            order.append(Path(dst).name)
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", tracking_replace)
        creds.rotate_fernet_key(user_id=1)

        key_idx = order.index(key.name)
        enc_idx = order.index(enc_path.name)
        assert key_idx < enc_idx, (
            "key must be replaced before any .enc — "
            "any other order leaves an unrecoverable intermediate state"
        )
        assert key.read_bytes() != original_key
