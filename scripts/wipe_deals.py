#!/usr/bin/env python3
"""Destructive DB + state.json wipe — syncs both sources of truth.

Originally wiped only the ``deals`` + ``orders`` tables. Extended
2026-04-19 to also reset each user's per-bot ``state.json`` files —
leaving those untouched meant the portal kept showing "Active deals"
that no longer existed in the DB after a wipe, and the operator had
to run a second ad-hoc Python script to clean up.

The underlying architecture has two sources of truth (DB + state.json)
and the earlier cross-bot-deal-id collision bug was another symptom of
that same two-source design. This fix synchronises the two at wipe
time but does not address the root cause — see audit v25 notes.

Safety rails:
  * Interactive "Type WIPE to confirm" prompt.
  * Refuses to run if any bot process is still alive (pid-file signals
    via ``os.kill(pid, 0)``). Operator must stop every bot first.
  * Backs up each ``<path>.state.json`` to ``<path>.state.json.pre_wipe_backup``
    before resetting — overwriting any earlier backup so repeated
    wipes stay safe.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

BASE: Path = Path(__file__).resolve().parent.parent
DB_PATH: Path = BASE / "logs" / "reverto.db"

# Fields reset to their "fresh start" value on wipe. Balance has its
# own rule (use initial_balance_btc as the target) so it's handled
# separately in _reset_state_file. Other fields (bot_name, mode,
# exchange, pair, running, started_at, drawdown_guard, indicators…)
# are left untouched — the YAML config still drives them and the
# engine will overwrite them on its first tick after restart anyway.
_RESET_DEFAULTS: dict = {
    "total_pnl_btc":      0,
    "win_rate":           0.0,
    "open_deals_count":   0,
    "closed_deals_count": 0,
    "fees_paid_btc":      0,
    "open_deals":         [],
}
# closed_deals is only reset if the key already exists — older state
# files may not have it at all, and adding a new key on wipe would
# change the file's shape in a way the engine didn't write.
_RESET_OPTIONAL_KEYS: tuple[str, ...] = ("closed_deals",)

# Suffix appended to a state.json path for the pre-wipe backup. Kept
# as a module constant so tests can assert on the exact spelling.
_BACKUP_SUFFIX: str = ".pre_wipe_backup"


# ── pid-liveness + safety gate ───────────────────────────────────────────────

def _is_pid_alive(pid: int) -> bool:
    """Return True if the given PID is a running process.

    ``os.kill(pid, 0)`` sends a null signal — does nothing but raises
    if no process matches. ``ProcessLookupError`` means the pid is
    stale (the pid-file can be deleted on a subsequent clean start
    but this script treats a stale file as "not running"). A
    ``PermissionError`` means the process exists but belongs to
    another user — we treat that as alive to stay on the safe side.
    """
    if pid <= 0:
        return False
    try:
        # NOTE: os.kill(pid, 0) is POSIX-only. On Windows this would
        # raise OSError unconditionally; the refuse-to-run guard would
        # then fire on every call. Reverto's target platform is Linux
        # (WSL2 in development, Linux server in production). If a
        # Windows port is ever needed, replace with psutil.pid_exists()
        # which is cross-platform.
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _check_no_bots_running(base_dir: Path, user_ids) -> None:
    """Raise SystemExit if any pid-file points at a live process.

    Scans ``<base_dir>/logs/<user_id>/pids/*.pid`` for every active
    user. The first live pid aborts the wipe with a clear error;
    stale pid files (process no longer exists) are tolerated.
    """
    alive: list[tuple[str, int, Path]] = []
    for uid in user_ids:
        pid_dir = base_dir / "logs" / str(uid) / "pids"
        if not pid_dir.exists():
            continue
        for pid_file in pid_dir.glob("*.pid"):
            try:
                pid = int(pid_file.read_text().strip())
            except (ValueError, OSError):
                continue
            if _is_pid_alive(pid):
                alive.append((pid_file.stem, pid, pid_file))
    if alive:
        print("\nRefusing to wipe — bot processes still alive:")
        for slug, pid, pf in alive:
            try:
                rel = pf.relative_to(base_dir)
            except ValueError:
                rel = pf
            print(f"  {slug}  pid={pid}  ({rel})")
        print("\nStop every bot via the portal before running wipe-deals.")
        raise SystemExit(1)


# ── state.json reset ─────────────────────────────────────────────────────────

def _reset_state_file(path: Path) -> Path:
    """Reset deal-tracking fields in a single state.json.

    Backs the original up to ``<path>.pre_wipe_backup`` first (replace-
    overwrite OK so repeated wipes stay safe), then rewrites the file
    in-place. Returns the backup path for observability.

    Field treatment:
      * ``balance_btc`` → ``initial_balance_btc`` (or 0.1 BTC fallback).
      * Everything in ``_RESET_DEFAULTS`` → its default value.
      * Keys in ``_RESET_OPTIONAL_KEYS`` reset ONLY if present; absent
        keys stay absent so we don't materialise a shape the engine
        didn't write.
      * All other keys untouched.

    Raises the underlying ``OSError`` / ``json.JSONDecodeError`` on
    filesystem or parse failure — callers may wrap this if a batch
    wipe should continue past one bad file.
    """
    data = json.loads(path.read_text(encoding="utf-8"))

    backup = path.with_suffix(path.suffix + _BACKUP_SUFFIX)
    shutil.copy2(path, backup)

    data["balance_btc"] = data.get("initial_balance_btc", 0.1)
    for key, value in _RESET_DEFAULTS.items():
        data[key] = value
    for key in _RESET_OPTIONAL_KEYS:
        if key in data:
            data[key] = []

    # Engine's StateIO.write uses indent=2 — match it so diffs between
    # a freshly-written state.json and a wiped one stay minimal.
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return backup


def _wipe_state_files(base_dir: Path, user_ids) -> int:
    """Reset every ``<base_dir>/logs/<user_id>/*.state.json`` file.

    Returns the number of files reset (excluding any skipped because
    the user dir doesn't exist). Each reset is announced on stdout
    with its relative path + backup filename.
    """
    n = 0
    for uid in user_ids:
        logs_dir = base_dir / "logs" / str(uid)
        if not logs_dir.exists():
            continue
        for state_path in sorted(logs_dir.glob("*.state.json")):
            backup = _reset_state_file(state_path)
            try:
                rel = state_path.relative_to(base_dir)
            except ValueError:
                rel = state_path
            print(f"  Reset: {rel} (backup: {backup.name})")
            n += 1
    return n


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    if not DB_PATH.exists():
        print(f"No DB at {DB_PATH} — nothing to wipe.")
        return 0

    print("=" * 60)
    print(" Reverto deal-ledger + state.json wipe")
    print("=" * 60)
    print(f" DB target:    {DB_PATH.relative_to(BASE)}")
    print("               logs/<user_id>/*.state.json")
    print(" Tables:       orders, deals  (DELETE + VACUUM)")
    print(" State fields: balance → initial, pnl + fees → 0,")
    print("               open_deals + closed_deals → [], counts → 0")
    print(" Preserved:    users, chart_annotations, backtest_runs,")
    print("               bot YAML configs, other state.json fields")
    print(" Prerequisite: stop every bot via the portal first.")
    print()

    try:
        answer = input("Type WIPE to confirm destructive action: ").strip()
    except EOFError:
        print("No confirmation received — aborting.")
        return 1

    if answer != "WIPE":
        print(f"Got {answer!r}, expected 'WIPE' — aborting.")
        return 1

    # Resolve active users via the same helper the registry uses
    # (core.user.get_active_user_ids). Import happens inside main()
    # so the unit tests can drive _reset_state_file / _wipe_state_files
    # without wiring a DB up.
    sys.path.insert(0, str(BASE))
    from core.database import close_db, init_db, set_db_path
    from core.user import get_active_user_ids

    set_db_path(DB_PATH)
    init_db()
    user_ids = get_active_user_ids()

    # Safety gate — refuse BEFORE any destructive op if a bot is alive.
    _check_no_bots_running(BASE, user_ids)

    # DB wipe.
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        # Orders FK-reference deals; delete orders first so the deals
        # DELETE doesn't trip FK enforcement.
        with conn:
            cur = conn.execute("DELETE FROM orders")
            print(f"  orders:  {cur.rowcount} rows removed")
            cur = conn.execute("DELETE FROM deals")
            print(f"  deals:   {cur.rowcount} rows removed")
        conn.execute("VACUUM")
    finally:
        conn.close()
    close_db()

    # state.json wipe (after the DB so a cancelled DB wipe never
    # leaves state.json in a reset-but-DB-untouched position).
    print()
    print(" Resetting state.json files:")
    n_files = _wipe_state_files(BASE, user_ids)
    print(f"  state files reset: {n_files}")

    print()
    print("Wipe complete. Restart bots via the portal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
