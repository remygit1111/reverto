"""Filesystem hygiene helpers — audit pd-044.

Several modules use the classic atomic-write pattern: open a
``<target>.tmp`` file, write payload, ``Path.replace()`` onto the
live path. The rename is atomic, so a live reader never sees a
half-written blob. The tradeoff: if the process dies between the
write and the replace (kernel OOM, power loss, SIGKILL), the
``.tmp`` file is orphaned. No code will ever pick it up — the live
file was either never touched or already rotated in — but the
orphan sits on disk forever unless something cleans it up.

One startup sweep per process is the cheapest way to keep the
directory tidy. Scoped to directories Reverto owns (credentials,
logs); nowhere else should the portal touch ``*.tmp`` at boot.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_orphaned_tmp_files(*directories: Path) -> int:
    """Remove every ``*.tmp`` file found under ``directories``.

    Walks each tree recursively. Missing directories are skipped
    silently (the cleanup helper should never fail a boot over a
    not-yet-created path). Per-file ``unlink`` failures are logged
    at DEBUG and the walk continues — a single unreadable file
    must not block the rest of the sweep.

    Returns the count of files actually removed so the caller can
    log one summary line instead of N per-file lines.
    """
    removed = 0
    for directory in directories:
        if not directory.exists():
            continue
        try:
            for tmp_path in directory.rglob("*.tmp"):
                if not tmp_path.is_file():
                    # A directory happens to end in ``.tmp`` — leave it
                    # alone. The atomic-write pattern only produces
                    # file-shaped orphans.
                    continue
                try:
                    tmp_path.unlink()
                    removed += 1
                    logger.debug(
                        "Removed orphaned tmp file: %s", tmp_path,
                    )
                except OSError as e:
                    logger.debug(
                        "Could not remove %s: %s", tmp_path, e,
                    )
        except OSError as e:
            # rglob itself raising is rare — permission issue on
            # the top-level dir or a concurrent unlink of the dir.
            # Warn-level (not debug) because it means the sweep
            # couldn't even start for that directory.
            logger.warning(
                "Failed to scan %s for orphaned tmp files: %s",
                directory, e,
            )

    if removed > 0:
        logger.info(
            "Cleaned up %d orphaned .tmp file(s) at startup",
            removed,
        )
    return removed
