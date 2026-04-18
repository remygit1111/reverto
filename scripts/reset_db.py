#!/usr/bin/env python3
"""Reset the Reverto database + state files for the multi-tenant migration.

Run once before the first boot on the new v3 schema. The script is
idempotent: missing files are skipped silently, and existing ones get
a timestamped ``.pre_mt`` backup so the operator can revert if
something goes sideways during the first-boot init.

Operational contract:

    $ make reset-db    # backup + wipe
    $ make start       # portal boots, init_db() writes v3 schema

Bot YAML configs are intentionally NOT touched — those survive the
migration and are read fresh by the portal. Only the deal ledger and
per-bot state.json files are removed.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "logs" / "reverto.db"
LOG_DIR = BASE / "logs"


def _backup_path(original: Path) -> Path:
    """Stamp the backup with a UTC timestamp so repeated resets don't
    clobber each other — the operator may want to compare pre-v3
    snapshots later."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return original.with_suffix(original.suffix + f".pre_mt.{ts}")


def _backup_and_remove(path: Path) -> bool:
    """Copy ``path`` to a timestamped .pre_mt backup then unlink.
    Returns True when a file was actually removed."""
    if not path.exists():
        return False
    backup = _backup_path(path)
    shutil.copy2(path, backup)
    path.unlink()
    print(f"  backed up → {backup.name}")
    print(f"  removed    {path.name}")
    return True


def main() -> int:
    print(f"Reverto DB reset (BASE={BASE})")

    # 1. Main reverto.db ledger.
    if DB_PATH.exists():
        print(f"Resetting {DB_PATH.relative_to(BASE)}")
        _backup_and_remove(DB_PATH)
        # WAL + SHM sidecars — SQLite recreates both on the next open,
        # but leaving them around points at the wiped DB and confuses
        # recovery tools.
        for suffix in ("-wal", "-shm"):
            sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
                print(f"  removed    {sidecar.name}")
    else:
        print("No DB to reset (first boot)")

    # 2. Per-bot state.json files — the engines rebuild these from
    # zero on next start, so wiping them keeps the fresh DB and the
    # engines consistent.
    n_state = 0
    if LOG_DIR.exists():
        for state_file in sorted(LOG_DIR.glob("*.state.json")):
            print(f"Resetting state {state_file.relative_to(BASE)}")
            if _backup_and_remove(state_file):
                n_state += 1

    print()
    print(f"Done. Reset {1 if not DB_PATH.exists() else 0} DB + {n_state} state file(s).")
    print("Run 'make start' (or restart the portal) to reinitialise the schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
