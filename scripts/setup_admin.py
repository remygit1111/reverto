#!/usr/bin/env python3
"""Phase-3a: set the admin password in the DB.

After the schema-v4 migration, ``users.password_hash`` is NULL for the
seeded admin row. Without a password nobody can log in
(``verify_password`` fails closed on NULL). This one-shot script
provisions the password via bcrypt.

Usage:

    # Non-interactive (CI / automation):
    REVERTO_ADMIN_PW=<password> python scripts/setup_admin.py

    # Interactive (typed prompt, no echo):
    python scripts/setup_admin.py

Exit codes:
    0  success
    1  admin row missing / password too short / passwords don't match
       / set_password returned False

Minimum password length is centralised in ``core.user_store`` as
``PASSWORD_MIN_LENGTH`` (audit v26-03). Shorter passwords are refused;
operator can change to something stronger later via the portal's
change-password endpoint.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

# Repo-root bootstrap so imports work no matter where the script is run from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import DatabaseMigrationError, init_db  # noqa: E402
from core.user_store import (  # noqa: E402
    PASSWORD_MIN_LENGTH,
    get_user_by_username,
    set_password,
)


def main() -> int:
    # Idempotent — ensures schema is at v4 and the admin row exists.
    # Safe to run even on a fully-migrated DB; this is a no-op apart
    # from the password UPDATE. A destructive migration (v<4 → v4)
    # will raise here unless REVERTO_DESTRUCTIVE_MIGRATE=1 is set;
    # setup-admin is the first thing an operator runs post-upgrade,
    # so we translate that into a clean stderr message instead of
    # a traceback.
    try:
        init_db()
    except DatabaseMigrationError as e:
        print(f"\n[FATAL] {e}\n", file=sys.stderr)
        return 1

    admin = get_user_by_username("admin")
    if admin is None:
        print(
            "ERROR: admin user not found in DB. Run `make start` "
            "first so init_db() seeds the admin row.",
            file=sys.stderr,
        )
        return 1

    # Audit v26-07: `os.environ.get(...)` returns an empty string when
    # the env-var is set to "" — pre-fix that silently skipped the
    # interactive prompt and then failed the length check with a
    # confusing error. Treat unset AND empty as "no password supplied"
    # and fall back to the interactive prompt.
    password = os.environ.get("REVERTO_ADMIN_PW") or None
    if password is None:
        print(f"Setting password for admin (user_id={admin.id})")
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm:      ")
        if password != confirm:
            print("ERROR: passwords don't match.", file=sys.stderr)
            return 1

    if len(password) < PASSWORD_MIN_LENGTH:
        print(
            f"ERROR: Password must be at least {PASSWORD_MIN_LENGTH} characters",
            file=sys.stderr,
        )
        return 1

    if not set_password(admin.id, password):
        print("ERROR: set_password returned False.", file=sys.stderr)
        return 1

    print(
        f"Password set for admin (user_id={admin.id}). "
        "You can now log in via the portal."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
