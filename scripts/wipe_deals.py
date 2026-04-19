#!/usr/bin/env python3
"""Destructive DB wipe — deals + orders tables.

Used once, after the cross-bot-deal-id-collision fix (2026-04-19), to
clear the already-corrupted deal ledger before operators boot on the
new YYYYMMDDHHMM-RRRR id format. Bot YAML configs + users + backtest
runs + annotations are all preserved.

Safety rails:
  * Interactive "Type WIPE to confirm" prompt — no accidental DELETE.
  * Requires ``WIPE`` on stdin; anything else aborts with exit 1.
  * Operator must stop all bots first; concurrent writes during wipe
    would be silently lost.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "logs" / "reverto.db"


def main() -> int:
    if not DB_PATH.exists():
        print(f"No DB at {DB_PATH} — nothing to wipe.")
        return 0

    print("=" * 60)
    print(" Reverto deal-ledger wipe")
    print("=" * 60)
    print(f" Target:       {DB_PATH.relative_to(BASE)}")
    print(" Tables:       orders, deals  (DELETE + VACUUM)")
    print(" Preserved:    users, chart_annotations, backtest_runs")
    print(" Prerequisite: stop every bot via the portal first —")
    print("               concurrent writes during wipe are silently lost.")
    print()

    try:
        answer = input("Type WIPE to confirm destructive action: ").strip()
    except EOFError:
        print("No confirmation received — aborting.")
        return 1

    if answer != "WIPE":
        print(f"Got {answer!r}, expected 'WIPE' — aborting.")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        # Orders reference deals via FK — delete orders first so
        # foreign-key checks don't block the deals delete.
        with conn:
            cur = conn.execute("DELETE FROM orders")
            print(f"  orders:  {cur.rowcount} rows removed")
            cur = conn.execute("DELETE FROM deals")
            print(f"  deals:   {cur.rowcount} rows removed")
        # VACUUM must run outside any transaction.
        conn.execute("VACUUM")
    finally:
        conn.close()

    print()
    print("Wipe complete. Restart bots via the portal — new deals will")
    print("use YYYYMMDDHHMM-RRRR ids (see core/ids.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
