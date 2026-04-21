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

# Default legacy system-key path. Post-Phase-3a (audit v26-06) the
# system Fernet key at logs/.credentials.key is no longer read by
# runtime code (the .auth.json blob it encrypted has been replaced
# by users.password_hash in the DB). This script stays as a recovery
# tool for operators who still have `.bak.*` files on disk from the
# pre-3a era, and can also be pointed at a per-user key path via
# --keyfile for Phase-2 key-rotation rollbacks.
_LEGACY_KEY_FILE = Path(__file__).parent.parent / "logs" / ".credentials.key"


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
        "--keyfile", type=Path, default=_LEGACY_KEY_FILE,
        help=(
            f"Path to the key file (default: {_LEGACY_KEY_FILE} — "
            f"legacy system key; pass a per-user path via "
            f"keys/<uid>.key for Phase-2 rotations)."
        ),
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
