#!/usr/bin/env python3
"""Migrate the on-disk layout to the multi-tenant (Phase 2) structure.

Moves the Phase-1 flat layout:

    config/bots/*.yaml
    logs/*.state.json
    logs/*.log
    logs/*.manual_trigger
    logs/pids/*.pid
    logs/.credentials.key        (stays in place — system key)
    logs/credentials.json        (converted to per-exchange .enc files)

into the Phase-2 per-user layout under user_id=1:

    config/bots/1/*.yaml
    logs/1/*.state.json
    logs/1/*.log
    logs/1/*.manual_trigger
    logs/1/pids/*.pid
    keys/1.key                   (new user key)
    credentials/1/<exchange>.enc

Idempotent: re-running against an already-migrated layout is a no-op.
System files (``reverto.db``, ``audit.log``, ``portal.log``, the
ephemeral API-key file, the system Fernet key, authentication blob)
are intentionally left where they are — those are operator/system
state, not tenant data.

Before running: stop every bot via the portal. A live migration
during a write-heavy tick can leave state.json files half-moved.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
USER_ID = 1

# Files under logs/ that are NOT per-bot state and must stay put.
_SYSTEM_LOG_FILES = {
    "reverto.db", "reverto.db-wal", "reverto.db-shm",
    "audit.log", "audit.log.1", "audit.log.2", "audit.log.3",
    "portal.log",
    ".api_key_ephemeral",
    ".credentials.key", "credentials.json",
    ".auth.json",
}

# Patterns that should stay at their legacy locations under logs/.
_SYSTEM_LOG_PREFIXES = (
    "audit.log.",        # rotated audit log variants
    ".credentials.key.", # fernet backups + lock file
    "credentials.json.", # tmp files from atomic writes
    "portal.log.",       # rotated portal log
)


def _is_system_log_file(name: str) -> bool:
    if name in _SYSTEM_LOG_FILES:
        return True
    return any(name.startswith(prefix) for prefix in _SYSTEM_LOG_PREFIXES)


def _move(src: Path, dst: Path) -> bool:
    """Move src→dst idempotently. Refuses to overwrite an existing
    destination — that's a sign the migration was already run and the
    src is stale, OR a name collision between two bots. Either way
    bail loudly so the operator inspects."""
    if not src.exists():
        return False
    if dst.exists():
        print(f"  SKIP   {src.name} → already exists at {dst.relative_to(BASE)}")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    print(f"  moved  {src.relative_to(BASE)} → {dst.relative_to(BASE)}")
    return True


def migrate_bot_configs() -> int:
    """config/bots/*.yaml → config/bots/<USER_ID>/*.yaml."""
    print("[configs] scanning config/bots/")
    src_dir = BASE / "config" / "bots"
    if not src_dir.exists():
        print("  (no config/bots/ dir — skip)")
        return 0
    dst_dir = src_dir / str(USER_ID)
    moved = 0
    for yaml_file in sorted(src_dir.glob("*.yaml")):
        # Skip anything already inside a user subdir.
        if yaml_file.parent != src_dir:
            continue
        if _move(yaml_file, dst_dir / yaml_file.name):
            moved += 1
    print(f"[configs] {moved} moved")
    return moved


def migrate_logs_and_state() -> int:
    """logs/*.{state.json,log,manual_trigger} → logs/<USER_ID>/*.* .
    System files (reverto.db, audit.log, ...) stay where they are."""
    print("[logs] scanning logs/")
    src_dir = BASE / "logs"
    if not src_dir.exists():
        print("  (no logs/ dir — skip)")
        return 0
    dst_dir = src_dir / str(USER_ID)
    moved = 0
    for entry in sorted(src_dir.iterdir()):
        if not entry.is_file():
            continue
        if _is_system_log_file(entry.name):
            continue
        # Only move known per-bot artefacts. Anything else stays
        # where it is so we don't swallow operator-placed files.
        if not any(
            entry.name.endswith(suffix)
            for suffix in (
                ".state.json", ".log", ".manual_trigger",
            )
        ):
            continue
        if _move(entry, dst_dir / entry.name):
            moved += 1
    print(f"[logs] {moved} moved")
    return moved


def migrate_pid_files() -> int:
    """logs/pids/*.pid → logs/<USER_ID>/pids/*.pid."""
    print("[pids] scanning logs/pids/")
    src_dir = BASE / "logs" / "pids"
    if not src_dir.exists():
        print("  (no logs/pids/ — skip)")
        return 0
    dst_dir = BASE / "logs" / str(USER_ID) / "pids"
    moved = 0
    for pid_file in sorted(src_dir.glob("*.pid")):
        if _move(pid_file, dst_dir / pid_file.name):
            moved += 1
    print(f"[pids] {moved} moved")

    # V24 LOW #5 — na een succesvolle migratie mag de legacy src_dir
    # leeg blijven staan. De portal verwacht pid-files onder het
    # user-scoped pad; een achterblijvende lege dir verwart nieuwe
    # operators ("waarom staat hier nog een pids/?"). Alleen rmdir'en
    # als er écht niets meer in staat — operator-placed bestanden
    # willen we niet stilletjes weggooien.
    try:
        if not any(src_dir.iterdir()):
            src_dir.rmdir()
            print(f"[pids] removed empty {src_dir}")
    except OSError as e:
        print(f"[pids] could not remove {src_dir}: {e}")
    return moved


def migrate_credentials() -> int:
    """logs/credentials.json + logs/.credentials.key →
    credentials/<USER_ID>/<exchange>.enc + keys/<USER_ID>.key.

    The per-field Fernet values inside credentials.json are decrypted
    under the old global key, then re-encrypted under a fresh per-user
    key. The old ciphertext + JSON store is LEFT in place (the system
    key / .auth.json still live there) — the operator removes them
    manually once comfortable.
    """
    print(f"[credentials] converting logs/credentials.json → credentials/{USER_ID}/")
    old_store = BASE / "logs" / "credentials.json"
    old_key = BASE / "logs" / ".credentials.key"

    if not old_store.exists():
        print("  (no credentials.json — skip)")
        return 0
    if not old_key.exists():
        print("  WARN: credentials.json exists but .credentials.key doesn't — cannot decrypt")
        return 0

    # Import lazily so the script still runs on a bare machine that
    # hasn't installed cryptography (no bots would run there either).
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as e:
        print(f"  ERROR: cryptography not installed: {e}")
        return 0

    try:
        raw_store = json.loads(old_store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ERROR: cannot read credentials.json: {e}")
        return 0

    try:
        old_fernet = Fernet(old_key.read_bytes())
    except Exception as e:
        print(f"  ERROR: cannot load old Fernet key: {e}")
        return 0

    # Decrypt each (api_key, api_secret) pair under the old key.
    plaintext: dict[str, dict[str, str]] = {}
    for exchange, entry in raw_store.items():
        try:
            ak = old_fernet.decrypt(
                entry["api_key"].encode("ascii"),
            ).decode("utf-8")
            sec = old_fernet.decrypt(
                entry["api_secret"].encode("ascii"),
            ).decode("utf-8")
        except (InvalidToken, KeyError, ValueError) as e:
            print(f"  WARN: decrypt failed for {exchange}: {e}")
            continue
        plaintext[exchange] = {"api_key": ak, "api_secret": sec}

    if not plaintext:
        print("  (no decryptable entries — skip)")
        return 0

    # Write re-encrypted blobs via the Phase-2 save_keys so the on-
    # disk layout (keys/1.key + credentials/1/<exchange>.enc, 0600)
    # matches exactly what the runtime expects.
    sys.path.insert(0, str(BASE))
    from core.credentials import save_keys  # noqa: E402

    moved = 0
    for exchange, payload in plaintext.items():
        save_keys(
            exchange, payload["api_key"], payload["api_secret"],
            user_id=USER_ID,
        )
        print(f"  converted {exchange} → credentials/{USER_ID}/{exchange}.enc")
        moved += 1
    print(f"[credentials] {moved} converted")
    return moved


def main() -> int:
    print("=== Multi-tenant filesystem migration ===")
    print(f"BASE: {BASE}")
    print(f"Target user_id: {USER_ID}")
    print()

    migrate_bot_configs()
    print()
    migrate_logs_and_state()
    print()
    migrate_pid_files()
    print()
    migrate_credentials()
    print()
    print("✓ Migration complete.")
    print("  Restart bots via the portal to pick up the new layout.")
    print("  Inspect logs/.credentials.key + logs/credentials.json; the")
    print("  system key is still used for .auth.json, and the old store")
    print("  is kept as a manual backup until you remove it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
