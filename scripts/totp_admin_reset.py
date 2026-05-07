#!/usr/bin/env python3
"""TOTP admin-reset wrapper with audit-log emission (audit pt-150).

The OPERATIONS-documented SQL recovery procedure (``UPDATE users SET
totp_seed_encrypted = NULL WHERE username = ?``) bypasses the
``/auth/totp/disable`` route and produces no application-layer
audit-log entry. That's a forensic blind spot: an operator with
SSH access — or an attacker who has compromised SSH — can clear a
user's TOTP secret without leaving a trace in
``logs/audit.jsonl``.

This wrapper closes the gap. It:

1. Validates the target user exists and currently has TOTP enabled.
2. Writes a ``totp_admin_reset`` audit-log entry BEFORE the DB
   write — failure-mode choice: a false-positive audit entry (the
   script crashes between audit-write and DB-update) is preferable
   to a silent change (the reverse ordering would leave an actual
   reset with no trail). Both halves of the dual-write that
   ``web/app.py::_audit`` does are reproduced (global JSONL +
   per-user JSONL under ``logs/<user_id>/audit.jsonl``) so the
   per-tenant audit pulls work the same as for any other
   ``totp_*`` event.
3. Performs the UPDATE via ``user_store.update_user_totp_seed``.
4. Confirms with an operator-readable success message.

Usage::

    # Interactive — confirmation prompt:
    .venv/bin/python scripts/totp_admin_reset.py --username admin

    # With a reason recorded in the audit entry:
    .venv/bin/python scripts/totp_admin_reset.py --username alice \\
        --reason "lost phone"

    # Scripted (skip the prompt):
    .venv/bin/python scripts/totp_admin_reset.py --username alice \\
        --reason "..." --yes

After reset the user logs in with password only and re-enrols TOTP
via Profile → Enable TOTP.

The raw SQL path documented in ``docs/OPERATIONS.md`` is retained as
an emergency fallback for cases where this wrapper cannot run
(broken Python environment, audit log filesystem unwritable). It
produces NO audit-log entry; operator must record the reset
out-of-band.

Exit codes:
    0  success — TOTP cleared, audit row written.
    1  user missing / TOTP not enabled / cancellation /
       set_password-equivalent helper returned False.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Repo-root bootstrap so the imports below resolve no matter where
# the script is invoked from. Mirrors scripts/setup_admin.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import paths  # noqa: E402
from core.database import (  # noqa: E402
    DatabaseMigrationError,
    init_db,
)
from core.user_store import (  # noqa: E402
    get_user_by_username,
    update_user_totp_seed,
)

logger = logging.getLogger("reverto.scripts.totp_admin_reset")

# Mirrors web/app.py audit-log layout. Kept in-sync with the rhav2-
# 001 hardening: 0o077 umask while creating, explicit chmod 0o640
# afterwards so the file ends up at the same permissions an
# in-portal _audit() write would produce.
_BASE_DIR = Path(__file__).resolve().parent.parent
_LOG_DIR = _BASE_DIR / "logs"
_AUDIT_FILE_MODE = 0o640


def _chmod_audit_file_if_exists(path: Path) -> None:
    """Best-effort chmod 0o640 — failures swallowed at DEBUG so an
    exotic filesystem (FAT, network mount) doesn't fail the audit
    write that just succeeded."""
    try:
        if path.exists():
            os.chmod(path, _AUDIT_FILE_MODE)
    except OSError as e:
        logger.debug("audit chmod failed for %s: %s", path, e)


def _write_audit_entry(
    user_id: int,
    username: str,
    reason: str,
) -> None:
    """Append the ``totp_admin_reset`` audit record to the global +
    per-user JSONL files, matching the schema produced by
    ``web/app.py::_audit``.

    The record carries the standard fields (``ts``, ``action``,
    ``slug``, ``user``, ``user_id``, ``ip``, ``result``,
    ``request_id``) plus a ``reason`` field with the operator-
    supplied context. The extra field is JSON-additive and parses
    cleanly under any consumer that already handles the standard
    schema (Loki, Vector, jq); JSONL ingesters don't care about
    unknown keys.

    Schema parity matters for forensic continuity: a security
    investigator filtering ``audit.jsonl`` for ``action ==
    "totp_*"`` events expects the wrapper-emitted line to look
    exactly like an in-portal totp_disabled / totp_enabled line,
    just with a different ``action`` value.
    """
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts": ts,
        "action": "totp_admin_reset",
        "slug": username,
        "user": "cli:totp_admin_reset",
        "user_id": user_id,
        # Local CLI use — no HTTP context. The string mirrors the
        # convention used elsewhere in the codebase for non-HTTP
        # audit emissions.
        "ip": "local",
        "result": "ok",
        # No request-id context for CLI-side emissions; the dash
        # matches what _audit emits for background-task callers
        # that have no HTTP request scope either.
        "request_id": "-",
        # pt-150 addition: reason field captures the operator's
        # context (lost phone, corrupted seed, etc.) so a future
        # auditor reading the JSONL has the same context the
        # operator typed when they ran the reset.
        "reason": reason,
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    global_path = _LOG_DIR / "audit.jsonl"
    user_path = paths.user_logs_dir(user_id) / "audit.jsonl"

    # Narrow umask while creating so a fresh file lands at 0o640
    # before any other process can open it (rhav2-001 contract).
    prev_umask = os.umask(0o077)
    try:
        with open(global_path, "a", encoding="utf-8") as f:
            f.write(line)
        _chmod_audit_file_if_exists(global_path)

        with open(user_path, "a", encoding="utf-8") as f:
            f.write(line)
        _chmod_audit_file_if_exists(user_path)
    finally:
        os.umask(prev_umask)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reset TOTP for a user (admin recovery path). Writes a "
            "totp_admin_reset audit-log entry BEFORE the DB update."
        ),
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Username whose TOTP secret should be cleared.",
    )
    parser.add_argument(
        "--reason",
        default="admin_recovery",
        help=(
            "Free-form reason recorded in the audit log. Default: "
            "'admin_recovery'. Examples: 'lost phone', "
            "'authenticator app deleted', 'corrupted secret'."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (for scripted use).",
    )
    args = parser.parse_args()

    # init_db() is idempotent post-migration. We need it because
    # the user-store helpers consult get_db() under the hood and
    # a fresh process has no connection yet.
    try:
        init_db()
    except DatabaseMigrationError as e:
        print(f"\n[FATAL] {e}\n", file=sys.stderr)
        return 1

    # Step 1: validate user exists.
    user = get_user_by_username(args.username)
    if user is None:
        print(
            f"ERROR: user '{args.username}' not found.",
            file=sys.stderr,
        )
        return 1

    # Step 2: validate user has TOTP enabled. Resetting a user who
    # never enrolled is a no-op DB-wise but would still emit an
    # audit row, polluting the trail. Refuse instead.
    if not user.totp_enabled:
        print(
            f"ERROR: user '{args.username}' does not have TOTP "
            "enabled. Nothing to reset.",
            file=sys.stderr,
        )
        return 1

    # Step 3: confirmation prompt unless --yes.
    if not args.yes:
        print(f"\nReset TOTP for user '{args.username}'?")
        print(f"Reason: {args.reason}")
        print(
            "After reset, user logs in with password only until "
            "they re-enrol TOTP via Profile → Enable TOTP.",
        )
        try:
            response = input("Type 'yes' to confirm: ").strip().lower()
        except EOFError:
            response = ""
        if response != "yes":
            print("Cancelled.")
            return 1

    # Step 4: write audit-log entry BEFORE the DB update. If the
    # script crashes between this write and the UPDATE on step 5,
    # we have a false-positive audit entry but no actual change —
    # which is a healthier failure mode than the reverse (silent
    # change with no trail).
    _write_audit_entry(
        user_id=user.id,
        username=args.username,
        reason=args.reason,
    )

    # Step 5: perform the update.
    if not update_user_totp_seed(user.id, None):
        print(
            f"ERROR: update_user_totp_seed returned False for "
            f"user_id={user.id}. Audit row was already written; "
            "investigate DB state and treat the audit as a "
            "false-positive if no change actually landed.",
            file=sys.stderr,
        )
        return 1

    print(f"\n✓ TOTP reset for user '{args.username}'.")
    print(f"  Reason logged: {args.reason}")
    print("  Audit event:   totp_admin_reset")
    print("  Next steps:")
    print("    1. User logs in with password only.")
    print("    2. User re-enrols via Profile → Enable TOTP.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
