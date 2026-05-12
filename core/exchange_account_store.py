"""Per-user exchange-account metadata store.

Backs the multi-account exchange-management feature. Every row in
``exchange_accounts`` describes one (user, exchange_type, alias) tuple
plus the UUID of the matching encrypted credentials blob at
``credentials/<user_id>/<uuid>.enc``.

Why this is a separate module:
  * ``core/credentials.py`` owns the on-disk encrypted blobs.
  * ``core/exchange_account_store.py`` (here) owns the DB rows that
    *name* those blobs and carry the operator-mutable metadata
    (alias, is_default, last_tested_at).
  * Splitting them means credential rotation can re-encrypt a tree of
    blobs without touching the DB; renaming an account doesn't touch
    the filesystem.

The functions in this module are the only writers — routes,
``main_live.py``, and the engines all go through here so the UUID
generation + atomic ``filesystem + DB`` pairing has exactly one
implementation.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Optional

from core import credentials
from core.database import get_db

logger = logging.getLogger(__name__)

# Soft caps mirror the Pydantic limits on the route bodies. Defence
# in depth — direct DB writes (e.g. via a maintenance script) still
# fail loudly if something writes 10kB into the ``alias`` column.
MAX_ALIAS_LEN = 64
MAX_API_KEY_LEN = 512
MAX_API_SECRET_LEN = 512
MAX_PASSPHRASE_LEN = 64

# Supported exchange-type slugs. Mirrors web/routes/exchanges.py and
# config.models.Exchange (the wizard dropdown). A future exchange
# lands here + in the credentials format validator + in
# exchanges/__init__.py.
KNOWN_EXCHANGE_TYPES: tuple[str, ...] = ("bitget", "kraken")


# ── Errors ────────────────────────────────────────────────────────────────


class AccountValidationError(ValueError):
    """Raised for caller-facing argument errors (unknown exchange_type,
    alias-too-long, duplicate alias). Subclasses ValueError so route
    handlers that catch ValueError → 400 keep working."""


# ── Helpers ───────────────────────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a ``sqlite3.Row`` into a plain dict for the wire.

    Never includes ``credentials_uuid`` in the public-facing dict —
    that's an internal pointer to the .enc filename and leaking it
    to the operator UI gains them nothing useful. Routes that need
    the UUID (engine boot, rotation tooling) read the DB row directly.
    """
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "exchange_type": row["exchange_type"],
        "alias": row["alias"],
        "created_at": row["created_at"],
        "last_tested_at": row["last_tested_at"],
        "is_default": bool(row["is_default"]),
    }


def _validate_inputs(
    exchange_type: str,
    alias: str,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    passphrase: Optional[str] = None,
) -> None:
    """Argument-shape checks shared by create + update. Length caps
    raise ``AccountValidationError`` (a ``ValueError`` subclass) so
    route handlers can surface them as HTTP 400 uniformly."""
    if exchange_type not in KNOWN_EXCHANGE_TYPES:
        raise AccountValidationError(
            f"Unknown exchange_type {exchange_type!r}. "
            f"Supported: {', '.join(KNOWN_EXCHANGE_TYPES)}"
        )
    if not alias or len(alias) > MAX_ALIAS_LEN:
        raise AccountValidationError(
            f"alias must be 1-{MAX_ALIAS_LEN} characters",
        )
    if api_key is not None and (
        not api_key or len(api_key) > MAX_API_KEY_LEN
    ):
        raise AccountValidationError(
            f"api_key must be 1-{MAX_API_KEY_LEN} characters",
        )
    if api_secret is not None and (
        not api_secret or len(api_secret) > MAX_API_SECRET_LEN
    ):
        raise AccountValidationError(
            f"api_secret must be 1-{MAX_API_SECRET_LEN} characters",
        )
    if passphrase is not None and len(passphrase) > MAX_PASSPHRASE_LEN:
        raise AccountValidationError(
            f"passphrase exceeds {MAX_PASSPHRASE_LEN} characters",
        )


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_account(
    user_id: int,
    exchange_type: str,
    alias: str,
    api_key: str,
    api_secret: str,
    *,
    passphrase: str = "",
    is_default: bool = False,
) -> int:
    """Create a new exchange account.

    Generates a fresh UUID, writes the encrypted credentials blob via
    ``credentials.save_keys_by_uuid``, then inserts the metadata row.
    Returns the new ``id``.

    Order matters: credentials are saved BEFORE the DB row, so if the
    blob write fails (e.g. disk full) we never end up with a DB row
    pointing at a missing file. If the DB insert fails after the blob
    landed, the orphan .enc file is harmless — a future rotation
    enumerates by ``exchange_accounts.credentials_uuid``, so an orphan
    UUID is never visited.

    Bitget requires a passphrase (audit r1-012); validate that at the
    route layer, not here — this store function is also reachable from
    tests/admin scripts that may legitimately want to skip the rule.
    """
    _validate_inputs(
        exchange_type, alias,
        api_key=api_key, api_secret=api_secret, passphrase=passphrase,
    )

    # Uniqueness on (user_id, exchange_type, alias) is enforced by the
    # DB UNIQUE constraint, but check up-front so callers see a clear
    # message rather than an opaque IntegrityError. Race window is
    # benign — a concurrent create that wins the insert will fail this
    # caller's INSERT, which we still translate to a clean error.
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM exchange_accounts "
        "WHERE user_id = ? AND exchange_type = ? AND alias = ?",
        (user_id, exchange_type, alias),
    ).fetchone()
    if existing is not None:
        raise AccountValidationError(
            f"alias {alias!r} already exists for "
            f"exchange_type={exchange_type}",
        )

    credentials_uuid = uuid.uuid4().hex

    # Save the blob first. No DB write has happened yet, so a write
    # failure (disk full, permission error) leaves no orphan row to
    # roll back. Pre-fix the credentials layer also raised
    # CredentialFormatError on malformed input; that regex check was
    # removed because legitimate Bitget keys started carrying
    # underscores and were getting rejected. Format-correctness is
    # now caught by the test-connection endpoint.
    credentials.save_keys_by_uuid(
        credentials_uuid, exchange_type, api_key, api_secret,
        user_id, passphrase=passphrase,
    )

    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO exchange_accounts "
                "(user_id, exchange_type, alias, credentials_uuid, "
                " is_default) VALUES (?, ?, ?, ?, ?)",
                (
                    user_id, exchange_type, alias, credentials_uuid,
                    1 if is_default else 0,
                ),
            )
            new_id = int(cur.lastrowid or 0)
            if is_default:
                # Unset any other default for this (user, exchange_type)
                # pair inside the same transaction so the at-most-one
                # invariant holds even under a concurrent create.
                conn.execute(
                    "UPDATE exchange_accounts SET is_default = 0 "
                    "WHERE user_id = ? AND exchange_type = ? "
                    "AND id != ?",
                    (user_id, exchange_type, new_id),
                )
    except sqlite3.IntegrityError as e:
        # The pre-check above lost a race; clean up the orphan blob
        # so an operator-driven retry doesn't accumulate .enc garbage.
        credentials.delete_keys_by_uuid(credentials_uuid, user_id)
        raise AccountValidationError(
            f"alias {alias!r} already exists for "
            f"exchange_type={exchange_type}",
        ) from e

    logger.info(
        "Created exchange account id=%d user=%d type=%s alias=%r",
        new_id, user_id, exchange_type, alias,
    )
    return new_id


def get_account(account_id: int) -> Optional[dict]:
    """Return the account metadata, or None if the row is absent.

    Does NOT include the credentials_uuid — callers that need the
    blob pointer must read the DB row directly via
    ``_get_account_credentials_uuid``."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM exchange_accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def _get_account_credentials_uuid(account_id: int) -> Optional[tuple[int, str]]:
    """Internal helper — return ``(user_id, credentials_uuid)`` for an
    account_id or None if absent. Used by ``get_account_credentials``
    and the delete path."""
    conn = get_db()
    row = conn.execute(
        "SELECT user_id, credentials_uuid FROM exchange_accounts "
        "WHERE id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    return (int(row["user_id"]), str(row["credentials_uuid"]))


def get_account_credentials(account_id: int) -> Optional[dict]:
    """Return the decrypted credentials dict for an account, or None
    if the account row doesn't exist or the blob can't be decrypted.

    The returned shape mirrors ``credentials.get_keys_by_uuid``:
    ``{'api_key', 'api_secret', 'passphrase'}``. Engines use this at
    boot to wire up the authenticated exchange client.
    """
    resolved = _get_account_credentials_uuid(account_id)
    if resolved is None:
        return None
    user_id, credentials_uuid = resolved
    return credentials.get_keys_by_uuid(credentials_uuid, user_id)


def list_accounts(user_id: int) -> list[dict]:
    """Return every account for ``user_id`` as a list of metadata
    dicts. Sorted by (exchange_type, alias) for deterministic UI
    rendering."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM exchange_accounts WHERE user_id = ? "
        "ORDER BY exchange_type ASC, alias ASC",
        (user_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_account(
    account_id: int,
    *,
    alias: Optional[str] = None,
    is_default: Optional[bool] = None,
    last_tested_at: Optional[str] = None,
) -> bool:
    """Update operator-mutable fields. Returns True if any column
    changed, False if the row was absent. ``exchange_type`` and
    ``credentials_uuid`` are intentionally NOT updateable — both are
    fundamental identity columns; renaming the exchange of an existing
    account is meaningless (the stored credentials wouldn't authenticate
    against the new exchange anyway) and rotating credentials goes
    through DELETE + CREATE which forces the operator to re-enter both
    api_key and api_secret rather than silently keeping a stale one.
    """
    conn = get_db()
    existing = conn.execute(
        "SELECT user_id, exchange_type FROM exchange_accounts "
        "WHERE id = ?",
        (account_id,),
    ).fetchone()
    if existing is None:
        return False

    user_id = int(existing["user_id"])
    exchange_type = str(existing["exchange_type"])

    sets: list[str] = []
    params: list = []
    if alias is not None:
        if not alias or len(alias) > MAX_ALIAS_LEN:
            raise AccountValidationError(
                f"alias must be 1-{MAX_ALIAS_LEN} characters",
            )
        sets.append("alias = ?")
        params.append(alias)
    if last_tested_at is not None:
        sets.append("last_tested_at = ?")
        params.append(last_tested_at)
    # is_default is handled in a transaction below so the
    # at-most-one-default invariant holds.

    if not sets and is_default is None:
        return False

    with conn:
        if sets:
            params.append(account_id)
            try:
                conn.execute(
                    f"UPDATE exchange_accounts SET {', '.join(sets)} "
                    "WHERE id = ?",
                    params,
                )
            except sqlite3.IntegrityError as e:
                raise AccountValidationError(
                    f"alias collision for "
                    f"(user={user_id}, exchange_type={exchange_type})",
                ) from e
        if is_default is True:
            # Set this account as default, unset every other in the
            # same (user, exchange_type) bucket. Single transaction.
            conn.execute(
                "UPDATE exchange_accounts SET is_default = 0 "
                "WHERE user_id = ? AND exchange_type = ? AND id != ?",
                (user_id, exchange_type, account_id),
            )
            conn.execute(
                "UPDATE exchange_accounts SET is_default = 1 "
                "WHERE id = ?",
                (account_id,),
            )
        elif is_default is False:
            conn.execute(
                "UPDATE exchange_accounts SET is_default = 0 "
                "WHERE id = ?",
                (account_id,),
            )

    return True


def delete_account(account_id: int) -> bool:
    """Remove the DB row AND the encrypted .enc file. Idempotent —
    a missing row returns False but does not raise.

    File deletion is best-effort: a stale .enc with no DB row is
    harmless (no code path enumerates by filename). Logging the failure
    means an operator can spot drift via the audit log if it ever
    matters."""
    resolved = _get_account_credentials_uuid(account_id)
    if resolved is None:
        return False
    user_id, credentials_uuid = resolved

    conn = get_db()
    with conn:
        conn.execute(
            "DELETE FROM exchange_accounts WHERE id = ?",
            (account_id,),
        )
    credentials.delete_keys_by_uuid(credentials_uuid, user_id)
    logger.info(
        "Deleted exchange account id=%d user=%d uuid=%s",
        account_id, user_id, credentials_uuid,
    )
    return True


def set_default(account_id: int) -> bool:
    """Mark ``account_id`` as the default for its (user, exchange_type)
    pair, unsetting any prior default. Returns True on success, False
    if the row is absent. Convenience wrapper around
    ``update_account(is_default=True)``."""
    return update_account(account_id, is_default=True)


def get_default_account(
    user_id: int, exchange_type: str,
) -> Optional[dict]:
    """Return the default account for ``(user_id, exchange_type)``,
    or None if none is flagged. Used by the bot wizard's "pick an
    account" default."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM exchange_accounts "
        "WHERE user_id = ? AND exchange_type = ? AND is_default = 1 "
        "LIMIT 1",
        (user_id, exchange_type),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def account_belongs_to_user(account_id: int, user_id: int) -> bool:
    """True iff ``account_id`` exists AND is owned by ``user_id``.
    Cross-user isolation gate — routes call this BEFORE returning the
    account dict so they can 404 a foreign account without leaking
    its existence."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM exchange_accounts "
        "WHERE id = ? AND user_id = ?",
        (account_id, user_id),
    ).fetchone()
    return row is not None
