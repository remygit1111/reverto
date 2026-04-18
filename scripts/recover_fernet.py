#!/usr/bin/env python3
"""Fernet rotation recovery tool.

Typical recovery sequence after a crashed rotation:

    # 1. Stop every bot (no engine should hold a cached Fernet)
    make stop-all

    # 2. Inspect the available backups
    .venv/bin/python scripts/recover_fernet.py --list

    # 3. Restore the most recent pre-rotation key
    .venv/bin/python scripts/recover_fernet.py --restore \\
        logs/.credentials.key.bak.20260418153022

    # 4. Verify credentials decrypt:
    .venv/bin/python -c "from core.credentials import get_keys; \\
        print(get_keys('bitget') is not None)"

    # 5. Restart portal
    make start

The `--restore` option copies the current key to a ``.pre-restore``
safety file before overwriting, so a botched restore is itself reversible.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Make the Reverto package importable from the scripts dir.
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.credentials import _KEY_FILE  # noqa: E402


def list_backups(keyfile: Path) -> int:
    """Print every backup for ``keyfile`` with its mtime. Newest last."""
    backups = sorted(
        keyfile.parent.glob(keyfile.name + ".bak.*"),
        key=lambda p: p.stat().st_mtime,
    )
    if not backups:
        print(f"No backups found in {keyfile.parent}", file=sys.stderr)
        return 1
    print(f"{'mtime (unix)':>16}  name")
    for b in backups:
        print(f"{int(b.stat().st_mtime):>16}  {b.name}")
    return 0


def restore_backup(backup: Path, keyfile: Path) -> int:
    """Copy ``backup`` over ``keyfile``, snapshotting the current key
    to ``<keyfile>.pre-restore`` first for rollback-of-rollback."""
    if not backup.exists():
        print(f"Backup not found: {backup}", file=sys.stderr)
        return 1

    if keyfile.exists():
        prestore = keyfile.with_suffix(keyfile.suffix + ".pre-restore")
        shutil.copy2(keyfile, prestore)
        print(f"Current key backed up to {prestore}")

    shutil.copy2(backup, keyfile)
    print(f"Restored {backup.name} → {keyfile}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fernet rotation recovery tool",
    )
    parser.add_argument(
        "--keyfile", type=Path, default=_KEY_FILE,
        help=f"Path to the key file (default: {_KEY_FILE})",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list", action="store_true", help="List available backups.",
    )
    group.add_argument(
        "--restore", type=Path, metavar="BACKUP",
        help="Restore this backup file over the current key.",
    )
    args = parser.parse_args()

    if args.list:
        return list_backups(args.keyfile)
    return restore_backup(args.restore, args.keyfile)


if __name__ == "__main__":
    sys.exit(main())
