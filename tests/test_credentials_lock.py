"""Tests for Fernet rotation locking + backup retention + reverse-order commit.

Spec items:
  * rotate_fernet_key uses _rotation_lock — concurrent rotations are refused.
  * Backups are timestamped (not a single .bak file).
  * cleanup_old_backups deletes backups older than retention_days.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from core import credentials as creds  # noqa: E402


@pytest.fixture
def routed_store(tmp_path, monkeypatch):
    key = tmp_path / ".credentials.key"
    store = tmp_path / "credentials.json"
    monkeypatch.setattr(creds, "_KEY_FILE", key)
    monkeypatch.setattr(creds, "_STORE_FILE", store)
    return key, store


class TestRotationLock:

    def test_second_rotation_refused_while_first_holds_lock(self, routed_store):
        key, store = routed_store
        creds.save_keys("bitget", "a", "b")

        # Acquire the lock manually — simulating another in-progress rotation.
        cm = creds._rotation_lock(key)
        cm.__enter__()
        try:
            with pytest.raises(RuntimeError, match="Another Fernet rotation"):
                creds.rotate_fernet_key(credentials_file=store, keyfile=key)
        finally:
            cm.__exit__(None, None, None)

    def test_lock_released_after_successful_rotate(self, routed_store):
        """After a successful rotate the lock file is gone, so a second
        rotate succeeds."""
        key, store = routed_store
        creds.save_keys("bitget", "a", "b")

        creds.rotate_fernet_key(credentials_file=store, keyfile=key)
        # Immediate second call must succeed.
        creds.rotate_fernet_key(credentials_file=store, keyfile=key)

        # Two timestamped backups now (one from each rotation).
        backups = list(key.parent.glob(key.name + ".bak.*"))
        assert len(backups) == 2


class TestTimestampedBackups:

    def test_each_rotation_creates_a_fresh_backup(self, routed_store):
        key, store = routed_store
        creds.save_keys("bitget", "a", "b")

        creds.rotate_fernet_key(credentials_file=store, keyfile=key)
        time.sleep(1.1)  # ensure the timestamp strftime differs
        creds.rotate_fernet_key(credentials_file=store, keyfile=key)

        backups = sorted(key.parent.glob(key.name + ".bak.*"))
        assert len(backups) == 2
        # Names end in a 20-digit UTC timestamp (sec + microseconds).
        for b in backups:
            stamp = b.name.rsplit(".bak.", 1)[1]
            assert stamp.isdigit() and len(stamp) == 20


class TestBackupRetention:

    def test_cleanup_removes_only_old_backups(self, routed_store):
        key, store = routed_store
        creds.save_keys("bitget", "a", "b")

        # Create a rotate + two fake "old" backup files.
        creds.rotate_fernet_key(credentials_file=store, keyfile=key)

        old = key.with_suffix(key.suffix + ".bak.20200101000000")
        old.write_bytes(b"fake old key")
        # Push mtime 30 days into the past.
        past = time.time() - 30 * 86400
        import os as _os
        _os.utime(old, (past, past))

        removed = creds.cleanup_old_backups(key, retention_days=7)
        assert old.name in removed
        assert not old.exists()
        # Today's real backup is still there.
        recent = list(key.parent.glob(key.name + ".bak.2026*"))
        assert len(recent) == 1


class TestCrashRecovery:

    def test_key_is_replaced_before_creds(self, routed_store, monkeypatch):
        """Regression pin: the rotation flips the key file first so a
        crash between the two replaces is recoverable from the .bak."""
        key, store = routed_store
        creds.save_keys("bitget", "a", "b")
        original_key = key.read_bytes()

        # Monkeypatch os.replace to capture ordering.
        import os as _os
        order: list[str] = []
        real_replace = _os.replace

        def tracking_replace(src, dst):
            order.append(Path(dst).name)
            return real_replace(src, dst)

        monkeypatch.setattr(_os, "replace", tracking_replace)
        creds.rotate_fernet_key(credentials_file=store, keyfile=key)

        # Expect: key file flipped BEFORE creds file.
        key_idx = order.index(key.name)
        store_idx = order.index(store.name)
        assert key_idx < store_idx, (
            "key must be replaced before creds — "
            "any other order leaves an unrecoverable intermediate state"
        )
        # And the bytes actually changed.
        assert key.read_bytes() != original_key
