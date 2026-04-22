"""Tests for core.file_lock — advisory fcntl lock.

The lock is used to serialise portal offline-close state mutation
against a bot subprocess's startup _load_state. The tests here
exercise the context-manager contract in isolation; the
portal-integration side is covered in tests/test_web_routes.py.

Thread-based parallelism is enough to validate that a second caller
blocks on an already-held lock: fcntl.flock is fd-scoped, and each
open() returns a distinct fd even inside the same process, so two
threads that each open the lock-file and flock it behave like two
separate processes for the purpose of this contract.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.file_lock import LockTimeoutError, exclusive_lock


def test_exclusive_lock_acquires_and_releases(tmp_path):
    """Basic round-trip: acquire, release, re-acquire — no errors."""
    lock_path = tmp_path / "basic.lock"
    with exclusive_lock(lock_path, timeout=1.0):
        pass
    # Second acquisition on the same path must not hang or fail.
    with exclusive_lock(lock_path, timeout=1.0):
        pass


def test_exclusive_lock_blocks_second_caller(tmp_path):
    """Thread 1 holds the lock; thread 2 must wait until release."""
    lock_path = tmp_path / "contended.lock"
    holder_entered = threading.Event()
    release_holder = threading.Event()
    second_acquired_at: list[float] = []
    holder_released_at: list[float] = []

    def _holder():
        with exclusive_lock(lock_path, timeout=2.0):
            holder_entered.set()
            release_holder.wait(timeout=2.0)
            holder_released_at.append(time.monotonic())

    def _contender():
        holder_entered.wait(timeout=2.0)
        with exclusive_lock(lock_path, timeout=5.0, poll_interval=0.05):
            second_acquired_at.append(time.monotonic())

    t_holder = threading.Thread(target=_holder)
    t_contender = threading.Thread(target=_contender)
    t_holder.start()
    t_contender.start()

    assert holder_entered.wait(timeout=2.0)
    # Give the contender a moment to observe the lock as busy.
    time.sleep(0.3)
    release_holder.set()
    t_holder.join(timeout=2.0)
    t_contender.join(timeout=3.0)

    assert holder_released_at, "holder did not finish"
    assert second_acquired_at, "contender never acquired the lock"
    # Contender's acquisition must follow the holder's release.
    assert second_acquired_at[0] >= holder_released_at[0] - 0.01


def test_exclusive_lock_timeout_raises(tmp_path):
    """Holder keeps the lock past the contender's timeout — contender
    must get LockTimeoutError rather than hang forever."""
    lock_path = tmp_path / "timeout.lock"
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def _holder():
        with exclusive_lock(lock_path, timeout=2.0):
            holder_entered.set()
            release_holder.wait(timeout=5.0)

    t_holder = threading.Thread(target=_holder)
    t_holder.start()
    assert holder_entered.wait(timeout=2.0)

    try:
        with pytest.raises(LockTimeoutError) as excinfo:
            with exclusive_lock(lock_path, timeout=0.3, poll_interval=0.05):
                pytest.fail("lock must not be acquired while holder holds it")
        assert str(lock_path) in str(excinfo.value)
    finally:
        release_holder.set()
        t_holder.join(timeout=2.0)


def test_exclusive_lock_creates_parent_dir(tmp_path):
    """lock_path under a missing parent must get the dir created
    automatically — the caller shouldn't have to pre-ensure the tree."""
    missing_parent = tmp_path / "nested" / "sub"
    assert not missing_parent.exists()
    lock_path = missing_parent / "child.lock"

    with exclusive_lock(lock_path, timeout=1.0):
        assert missing_parent.is_dir()
        assert lock_path.exists()


def test_exclusive_lock_release_on_exception(tmp_path):
    """If the body raises, the lock must still release so subsequent
    callers aren't stuck waiting on the timeout."""
    lock_path = tmp_path / "raises.lock"

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with exclusive_lock(lock_path, timeout=1.0):
            raise _Boom("body failure")

    # Must succeed immediately — if the lock leaked we'd time out here.
    t0 = time.monotonic()
    with exclusive_lock(lock_path, timeout=0.5, poll_interval=0.05):
        pass
    assert time.monotonic() - t0 < 0.3, (
        "second caller waited too long — lock likely leaked on exception"
    )


def test_exclusive_lock_does_not_delete_lockfile(tmp_path):
    """Lock-file persists across releases: a concurrent waiter may
    already have an open fd on it, and deletion would invalidate the
    kernel-side lock identity for them."""
    lock_path = tmp_path / "persist.lock"
    with exclusive_lock(lock_path, timeout=1.0):
        pass
    assert lock_path.exists(), "lock-file must not be deleted on release"
