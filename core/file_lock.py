"""Advisory file-lock via fcntl.flock for cross-process serialisation.

Designed for portal <-> bot-subprocess coordination around shared
state files (state.json). Not a substitute for database transactions
— use this only when a process needs exclusive access to a file-based
resource for a brief period.

POSIX-only (fcntl). Reverto runs on Linux / WSL2, so this is fine;
porting to Windows would need a msvcrt-based replacement.

Usage:
    with exclusive_lock(Path("logs/1/bot.state.lock"), timeout=5):
        # Critical section — other processes waiting on same lock
        state = load_state(...)
        ...
"""

from __future__ import annotations

import fcntl
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class LockTimeoutError(Exception):
    """Raised when exclusive_lock cannot acquire the lock within the
    timeout budget."""


@contextmanager
def exclusive_lock(
    lock_path: Path,
    timeout: float = 5.0,
    poll_interval: float = 0.1,
) -> Iterator[None]:
    """Acquire an exclusive advisory lock on ``lock_path``.

    Creates the lock-file (and its parent dir) if they don't exist.
    Releases on context exit. If another process holds the lock, polls
    until ``timeout`` elapses — then raises ``LockTimeoutError``.

    The lock-file itself is NEVER deleted after release because a
    concurrent waiter may already have an open file-descriptor on it;
    deletion would invalidate their fcntl. Lock-files are small
    (0 bytes) and persist for the process tree's lifetime.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    # Open with 'a+' so the file is created on first use without
    # truncating an existing one (another waiter may be mid-flock on
    # the same path).
    fd = open(lock_path, "a+")
    try:
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Could not acquire lock on {lock_path} "
                        f"within {timeout}s — another process holds it."
                    )
                time.sleep(poll_interval)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        fd.close()
