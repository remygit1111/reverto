# core/credentials.py
# Encrypted storage of exchange API keys.
#
# Multi-account model (feat/exchange-account-management):
#   - Fernet key at  keys/<user_id>.key                 (chmod 0600)
#   - Payload at     credentials/<user_id>/<uuid>.enc   (chmod 0600)
#   - save_keys_by_uuid / get_keys_by_uuid / delete_keys_by_uuid
#     operate on a credentials_uuid string that is generated when the
#     ``exchange_accounts`` row is created (see core.exchange_account_store).
#
# Pre-multi-account the payload key was the exchange name, which forced
# a one-credential-per-(user, exchange_type) layout. UUID-named files
# decouple credential storage from the (user, exchange_type) identity,
# so an operator can keep "Bitget main" alongside "Bitget test".
#
# Threat model:
#   - Code injection / RCE → key files + ciphertext unusable without the
#     private key — but an attacker with shell access on the host has
#     both anyway, so Fernet is no defence against full host compromise.
#   - Disk dump without the host's filesystem permissions → ciphertext
#     alone is useless.
#   - Cross-user data leak on a multi-tenant host → user 2 cannot
#     decrypt user 1's .enc files because the per-user keys diverge.
#     THIS is the primary multi-tenant security property.
#
# Design notes:
#   - The per-user .enc file format is:
#       Fernet( utf-8( json({"api_key": ..., "api_secret": ...,
#                            "passphrase": ...}) ) )
#     i.e. ONE encrypted blob per (user, account). Per-file is simpler to
#     rotate and scopes the blast radius of a corrupt decrypt.
#
# Provider abstraction:
#   The credential pipeline is split into a ``CredentialProvider`` ABC
#   plus a ``FernetCredentialProvider`` concrete implementation. The
#   module-level helpers (``save_keys_by_uuid`` etc) delegate to a
#   process-wide default provider; a future signing-service backend
#   can register as the default without touching call sites.
#
# Known limitation — audit r1-010 (ACCEPTED):
#   After ``get_keys_by_uuid`` decrypts, the plaintext credentials live
#   in the calling engine's Python process heap for the lifetime of
#   that engine. A core-dump or live memory-scraping attack on the
#   host would expose them. A future signing-service design moves
#   credential use out-of-process, which resolves this category of
#   risk. Mitigations in place:
#     * Single-host deploy → only one attack surface (no shared
#       tenants on the same box).
#     * Subprocess env-var leak is closed (r1-023 allowlist).
#     * Fernet key files are 0600 under a 0700 dir.
#   Accepted risk for single-operator on a trusted host.

import fcntl
import json
import logging
import os
import re
import shutil
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from core.paths import (
    ensure_secret_file_mode,
    user_credentials_dir,
    user_fernet_key_path,
    user_keys_dir,
    uuid_creds_path,
)

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent


# ── r2-010: API-key format validation ─────────────────────────────────────
#
# Heuristic length-bounds catch the typical typo modes (truncated
# paste, wrong field pasted into the wrong input, partial copy)
# without locking us in to a specific format the exchange might
# evolve. The patterns are deliberately generous — exchanges have
# rotated their key formats more than once over the last few years
# and we'd rather fail-open on a future format than fail-closed on a
# legitimate key.
#
# Wire-up: ``FernetCredentialProvider.save_keys_by_uuid`` calls
# ``_validate_api_key_format`` before the encrypt step; failure
# raises ``CredentialFormatError`` (a ``ValueError`` subclass) with
# a hint about the expected shape and the actual length, so the
# operator has enough to diagnose the typo without leaking the key.

# Bitget API key — observed shape: alphanumeric, ~32-64 characters.
_BITGET_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{16,128}$")
_BITGET_API_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9]{16,128}$")
# Kraken API key — 56-character base64; secret 88-char base64.
# Generous bounds to absorb format drift.
_KRAKEN_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9+/=]{40,128}$")
_KRAKEN_API_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9+/=]{40,256}$")


class CredentialFormatError(ValueError):
    """Raised when an exchange API key/secret pair does not match the
    expected format heuristic. See r2-010.

    Subclass of ``ValueError`` so existing call-sites that catch
    ``ValueError`` (Pydantic body validators, route 400-handlers)
    keep working without code changes.
    """


def _validate_api_key_format(
    exchange: str, api_key: str, api_secret: str,
) -> None:
    """Heuristic format check for exchange API key/secret pairs.

    r2-010: catches obvious typos at credential-save time rather than
    at first authenticated exchange call. Patterns are intentionally
    permissive so a future format-rotation by the exchange does NOT
    lock out legitimate operators — the goal is to catch truncation /
    wrong-field-pasted / partial-copy mistakes, not to enforce a
    schema. Unknown exchanges fall through silently.
    """
    if exchange == "bitget":
        if not _BITGET_API_KEY_PATTERN.match(api_key):
            raise CredentialFormatError(
                f"Bitget API key does not match expected format "
                f"(16-128 alphanumerics). Got {len(api_key)} chars. "
                f"Verify you copied the full key from the Bitget UI."
            )
        if not _BITGET_API_SECRET_PATTERN.match(api_secret):
            raise CredentialFormatError(
                f"Bitget API secret does not match expected format "
                f"(16-128 alphanumerics). Got {len(api_secret)} chars."
            )
    elif exchange == "kraken":
        if not _KRAKEN_API_KEY_PATTERN.match(api_key):
            raise CredentialFormatError(
                f"Kraken API key does not match expected format "
                f"(40-128 base64 chars). Got {len(api_key)} chars."
            )
        if not _KRAKEN_API_SECRET_PATTERN.match(api_secret):
            raise CredentialFormatError(
                f"Kraken API secret does not match expected format "
                f"(40-256 base64 chars). Got {len(api_secret)} chars."
            )
    # Unknown exchange — pass through unvalidated.


class CredentialProvider(ABC):
    """Abstract backend for storing and retrieving exchange API
    credentials on a per-user basis.

    Implementations MUST be safe to call from multiple threads inside
    one process — the FernetCredentialProvider achieves this by being
    stateless aside from filesystem I/O, which is itself serialised by
    OS-level write semantics. They MUST NOT silently swallow decrypt
    failures (return ``None`` from ``get_keys_by_uuid`` instead) so a
    partial corruption never gets papered over as "no creds stored".
    """

    @abstractmethod
    def save_keys_by_uuid(
        self,
        credentials_uuid: str,
        exchange_type: str,
        api_key: str,
        api_secret: str,
        user_id: int,
        *,
        passphrase: str = "",
        _skip_format_validation: bool = False,
    ) -> None:
        """Persist ``(api_key, api_secret, optional passphrase)`` under
        ``credentials_uuid`` for ``user_id``. Atomic — a crash mid-write
        leaves either the prior payload intact or the new one fully
        written, never a half-written blob.

        ``exchange_type`` is passed only so the format validator can
        apply exchange-specific heuristics; it is NOT stored in the
        encrypted blob (the DB row owns that mapping).

        ``_skip_format_validation`` is the test-only escape hatch;
        production routes never set it."""

    @abstractmethod
    def get_keys_by_uuid(
        self, credentials_uuid: str, user_id: int,
    ) -> Optional[dict]:
        """Return ``{'api_key', 'api_secret', 'passphrase'}`` for
        ``credentials_uuid`` under ``user_id``, or ``None`` when no
        payload exists or decryption failed. ``passphrase`` is always
        present in the returned dict (empty string when not stored)
        so callers can treat the shape uniformly."""

    @abstractmethod
    def delete_keys_by_uuid(
        self, credentials_uuid: str, user_id: int,
    ) -> bool:
        """Remove the credentials blob for ``credentials_uuid`` under
        ``user_id``. Returns True when something was deleted, False
        when the entry was already absent."""

    @abstractmethod
    def rotate_user_key(
        self, user_id: int = 1, retention_days: int = 7,
    ) -> dict:
        """Rotate the user's master key and re-encrypt every stored
        credential under the new key. Implementation must enforce a
        commit-order contract that keeps the old payloads recoverable
        on a crash mid-rotation."""

    @abstractmethod
    def encrypt_for_user(self, user_id: int, plaintext: str) -> str:
        """Encrypt arbitrary UTF-8 plaintext using the user's key.

        General-purpose primitive — the exchange-credential helpers
        above wrap a JSON payload, but other callers (TOTP seeds,
        per-user secrets) only need a scalar string. Returned
        ciphertext is a UTF-8 string suitable for direct DB storage."""

    @abstractmethod
    def decrypt_for_user(self, user_id: int, ciphertext: str) -> str:
        """Decrypt a string previously produced by ``encrypt_for_user``.

        Raises on tamper / wrong key / corruption — callers MUST
        handle the failure path explicitly. Unlike ``get_keys_by_uuid``
        we do NOT swallow decrypt failures here, because the caller
        almost always wants to distinguish "no value stored" (which
        they can detect at the DB layer with a NULL) from "stored
        value won't decrypt" (an integrity event worth surfacing)."""


# ── FernetCredentialProvider (concrete, on-disk Fernet backend) ────────────


class FernetCredentialProvider(CredentialProvider):
    """On-disk Fernet-backed implementation. The historical default
    (and currently the only production backend). A future signing-
    service backend will satisfy the same contract by talking to an
    out-of-process daemon; the rest of the codebase only sees the ABC.
    """

    # ── Per-user Fernet key ──

    def _load_or_create_user_key(self, user_id: int) -> bytes:
        """Return the raw Fernet key for ``user_id``, generating a
        fresh one on first use. The key file lands at
        ``keys/<user_id>.key`` with mode 0600 and its parent dir 0700
        so a permissive umask can't accidentally expose it to other
        local users."""
        user_keys_dir()  # ensures keys/ is 0700
        key_path = user_fernet_key_path(user_id)
        if key_path.exists():
            return key_path.read_bytes()
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        ensure_secret_file_mode(key_path)
        logger.warning(
            "Generated new Fernet key for user %d at %s — do not "
            "lose this file, otherwise stored exchange keys for "
            "this user become unusable.",
            user_id, key_path,
        )
        return key

    def _user_fernet(self, user_id: int) -> Fernet:
        return Fernet(self._load_or_create_user_key(user_id))

    def get_fernet(self, user_id: int) -> Fernet:
        """Public alias for ``_user_fernet`` — used by external tooling
        that wants to encrypt/decrypt a user-scoped payload itself."""
        return self._user_fernet(user_id)

    # ── Per-user exchange credentials (public API) ──

    def save_keys_by_uuid(
        self,
        credentials_uuid: str,
        exchange_type: str,
        api_key: str,
        api_secret: str,
        user_id: int,
        *,
        passphrase: str = "",
        _skip_format_validation: bool = False,
    ) -> None:
        """Encrypt and write the api_key + api_secret (+ optional
        passphrase) for ``(user_id, credentials_uuid)`` to
        ``credentials/<user_id>/<uuid>.enc``.

        Atomic write via .tmp → os.replace so a crash mid-write never
        leaves a half-encrypted file.

        Bitget's third credential piece (passphrase, user-chosen at
        API-key creation time) lands in the same blob. Kraken doesn't
        use one; empty string means "no passphrase stored".

        Format-validate the api_key + api_secret BEFORE the encrypt
        step so a typo / truncation / wrong-field paste raises
        ``CredentialFormatError`` at save-time instead of surfacing
        as a ccxt auth error tens of seconds later. The test suite
        sets ``_skip_format_validation`` for fixtures that use
        placeholder values; production routes never set it.
        """
        if not _skip_format_validation:
            _validate_api_key_format(exchange_type, api_key, api_secret)

        f = self._user_fernet(user_id)
        payload_dict: dict[str, str] = {
            "api_key": api_key,
            "api_secret": api_secret,
        }
        if passphrase:
            payload_dict["passphrase"] = passphrase
        payload = json.dumps(payload_dict).encode("utf-8")
        blob = f.encrypt(payload)

        dst = uuid_creds_path(user_id, credentials_uuid)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        tmp.write_bytes(blob)
        ensure_secret_file_mode(tmp)
        tmp.replace(dst)
        ensure_secret_file_mode(dst)
        logger.info(
            "Credentials saved for user %d / uuid %s (exchange_type=%s)",
            user_id, credentials_uuid, exchange_type,
        )

    def get_keys_by_uuid(
        self, credentials_uuid: str, user_id: int,
    ) -> Optional[dict]:
        """Decrypt and return ``{'api_key', 'api_secret', 'passphrase'}``
        for ``(user_id, credentials_uuid)``, or None if the file is
        missing or not decryptable under the current user-key.

        No exception bubbles up — the call path (engine boot, portal
        list) must keep working when one pair of credentials turns out
        to be corrupt. ``passphrase`` is always present (empty string
        when not stored) so callers see a stable shape.
        """
        path = uuid_creds_path(user_id, credentials_uuid)
        if not path.exists():
            return None
        try:
            f = self._user_fernet(user_id)
            raw = f.decrypt(path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, OSError, json.JSONDecodeError) as e:
            logger.error(
                "Cannot decrypt credentials for user %d / uuid %s: %s",
                user_id, credentials_uuid, e,
            )
            return None
        if not isinstance(data, dict):
            return None
        return {
            "api_key":    data.get("api_key", ""),
            "api_secret": data.get("api_secret", ""),
            "passphrase": data.get("passphrase", ""),
        }

    def delete_keys_by_uuid(
        self, credentials_uuid: str, user_id: int,
    ) -> bool:
        """Delete credentials for (user_id, credentials_uuid). Returns
        True if something was deleted, False when the entry was already
        absent."""
        path = uuid_creds_path(user_id, credentials_uuid)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as e:
            logger.error(
                "Cannot delete credentials for user %d / uuid %s: %s",
                user_id, credentials_uuid, e,
            )
            return False
        logger.info(
            "Credentials deleted for user %d / uuid %s",
            user_id, credentials_uuid,
        )
        return True

    # ── Rotation (per-user key + .enc re-encrypt) ──

    def _list_uuids_with_keys(self, user_id: int) -> list[str]:
        """Internal helper — enumerate the UUID stems of every .enc
        file in this user's credentials tree. Used by ``rotate_user_key``
        and a handful of tests. Sorted output keeps rotation logs
        deterministic."""
        cred_dir = user_credentials_dir(user_id)
        if not cred_dir.exists():
            return []
        return sorted(p.stem for p in cred_dir.glob("*.enc"))

    def rotate_user_key(
        self,
        user_id: int = 1,
        retention_days: int = 7,
    ) -> dict:
        """Rotate a user's Fernet key and re-encrypt every .enc file
        under that user's credentials dir.

        Commit-order contract (critical for recoverability):
          1. Load every (user_id, uuid) blob under the OLD key.
          2. Snapshot the old key to a timestamped backup.
          3. Generate a fresh key and re-encrypt every payload in
             memory.
          4. Write new .enc tmp files AND new key tmp file.
          5. os.replace the key file FIRST, then each .enc. A crash
             between means NEW-key + OLD-creds → get_keys_by_uuid
             returns None, recoverable by restoring the .bak.<ts>.
             If we wrote creds first and crashed, the new key would
             exist only in memory and the on-disk new-key-encrypted
             .enc files would be unrecoverable.
          6. Retention sweep.

        The whole function runs inside a file-level advisory lock so
        concurrent rotations fail fast instead of corrupting state.
        """
        keyfile = user_fernet_key_path(user_id)
        if not keyfile.exists():
            raise FileNotFoundError(
                f"No master key at {keyfile} — nothing to rotate",
            )

        with _rotation_lock(keyfile):
            uuids = self._list_uuids_with_keys(user_id)
            plaintext: dict[str, dict[str, str]] = {}
            for cred_uuid in uuids:
                decrypted = self.get_keys_by_uuid(cred_uuid, user_id=user_id)
                if decrypted is None:
                    raise RuntimeError(
                        f"Cannot rotate user {user_id} — credentials "
                        f"for uuid {cred_uuid!r} failed to decrypt under "
                        "the current master key. Fix the key/cipher "
                        "mismatch first."
                    )
                plaintext[cred_uuid] = decrypted

            timestamp = datetime.now(timezone.utc).strftime(
                "%Y%m%d%H%M%S%f",
            )
            backup_path = keyfile.with_suffix(
                keyfile.suffix + f".bak.{timestamp}",
            )
            shutil.copy2(keyfile, backup_path)
            ensure_secret_file_mode(backup_path)

            new_key = Fernet.generate_key()
            new_fernet = Fernet(new_key)

            tmp_enc: dict[str, Path] = {}
            for cred_uuid, pt in plaintext.items():
                blob = new_fernet.encrypt(
                    json.dumps(pt).encode("utf-8"),
                )
                dst = uuid_creds_path(user_id, cred_uuid)
                tmp = dst.with_suffix(dst.suffix + ".tmp")
                tmp.write_bytes(blob)
                ensure_secret_file_mode(tmp)
                tmp_enc[cred_uuid] = tmp

            tmp_key = keyfile.with_suffix(keyfile.suffix + ".tmp")
            tmp_key.write_bytes(new_key)
            ensure_secret_file_mode(tmp_key)

            os.replace(tmp_key, keyfile)
            for cred_uuid, tmp in tmp_enc.items():
                dst = uuid_creds_path(user_id, cred_uuid)
                os.replace(tmp, dst)

            removed = cleanup_old_backups(keyfile, retention_days)

        logger.warning(
            "Fernet master key rotated for user %d. Backup at %s. "
            "Rotated uuids: %d. Removed old backups: %d",
            user_id, backup_path, len(plaintext), len(removed),
        )
        return {
            "user_id": user_id,
            "rotated_at": datetime.now(timezone.utc).isoformat(),
            "backup_path": str(backup_path),
            "keys_rotated": sorted(plaintext.keys()),
            "backups_removed": removed,
        }

    # ── Generic per-user encrypt / decrypt ──

    def encrypt_for_user(self, user_id: int, plaintext: str) -> str:
        """Encrypt an arbitrary UTF-8 string with the user's Fernet
        key. Returns the ciphertext as a UTF-8 string ready for DB
        storage. Reuses the same per-user key the exchange-credentials
        helpers use, so a future ``rotate_user_key`` rotates BOTH
        domains — TOTP seeds and exchange creds — under the same
        commit-order contract."""
        f = self._user_fernet(user_id)
        return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt_for_user(self, user_id: int, ciphertext: str) -> str:
        """Decrypt a value previously produced by ``encrypt_for_user``.
        Raises ``cryptography.fernet.InvalidToken`` on tamper / wrong
        key / corruption — callers MUST distinguish "value not stored"
        (DB NULL, never reaches this method) from "stored value won't
        decrypt" (integrity event)."""
        f = self._user_fernet(user_id)
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


# ── Default provider singleton + module-level shims ────────────────────────

_default_provider: CredentialProvider = FernetCredentialProvider()


def get_default_provider() -> CredentialProvider:
    """Return the process-wide default credential provider. Tests
    that need to assert which backend is wired in can call this
    instead of probing module-private state."""
    return _default_provider


def set_default_provider(provider: CredentialProvider) -> None:
    """Replace the process-wide default provider. Intended for
    swap-in (signing-service backend) and for tests that want to
    inject a fake. Callers MUST hold this swap to a single site at
    process start; mid-flight swaps would race against any in-flight
    credential operations."""
    global _default_provider
    _default_provider = provider


# ── Free-function shims ────────────────────────────────────────────────────


def get_fernet(user_id: int) -> Fernet:
    """Module-level shim — only valid for FernetCredentialProvider.
    A non-Fernet provider (signing service) will raise
    ``AttributeError`` here, which is the correct failure mode: the
    caller is reaching for a Fernet handle the new backend doesn't
    expose."""
    return _default_provider.get_fernet(user_id)  # type: ignore[attr-defined]


def save_keys_by_uuid(
    credentials_uuid: str,
    exchange_type: str,
    api_key: str,
    api_secret: str,
    user_id: int,
    *,
    passphrase: str = "",
    _skip_format_validation: bool = False,
) -> None:
    _default_provider.save_keys_by_uuid(
        credentials_uuid, exchange_type, api_key, api_secret, user_id,
        passphrase=passphrase,
        _skip_format_validation=_skip_format_validation,
    )


def get_keys_by_uuid(
    credentials_uuid: str, user_id: int,
) -> Optional[dict]:
    return _default_provider.get_keys_by_uuid(credentials_uuid, user_id)


def delete_keys_by_uuid(
    credentials_uuid: str, user_id: int,
) -> bool:
    return _default_provider.delete_keys_by_uuid(credentials_uuid, user_id)


def rotate_fernet_key(
    user_id: int = 1,
    retention_days: int = 7,
) -> dict:
    """Module-level shim. Name kept as ``rotate_fernet_key`` (not
    ``rotate_user_key``) for backwards compatibility with the existing
    operator scripts and CLI surface; the provider method matches the
    abstract-interface name."""
    return _default_provider.rotate_user_key(
        user_id=user_id, retention_days=retention_days,
    )


# ── Module-private helpers ─────────────────────────────────────────────────


@contextmanager
def _rotation_lock(keyfile: Path):
    """Process-level advisory lock around key rotation.

    Two concurrent rotations could otherwise both generate fresh keys
    and overwrite each other in unpredictable order, leaving the
    credential files decrypted under whichever key lost the race. An
    fcntl advisory lock on a sentinel file gates the critical section;
    second caller gets a clear error instead of silent corruption.
    """
    lock_file = keyfile.with_suffix(keyfile.suffix + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch(mode=0o600)
    fd = open(lock_file, "w")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError(
                "Another Fernet rotation is already running. "
                "Wait for it to finish before retrying."
            ) from e
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()
        try:
            lock_file.unlink()
        except OSError:
            pass


def cleanup_old_backups(
    keyfile: Path,
    retention_days: int = 7,
) -> list[str]:
    """Delete timestamped Fernet backups older than ``retention_days``.

    Backups are created by ``rotate_fernet_key`` as
    ``<user_id>.key.bak.YYYYMMDDHHMMSS``. Over time they accumulate —
    a hobbyist rotating monthly ends up with hundreds of files. Safe
    to call at the end of each rotation or from a cron job.
    """
    cutoff = time.time() - retention_days * 86400
    removed: list[str] = []
    for backup in keyfile.parent.glob(keyfile.name + ".bak.*"):
        try:
            if backup.stat().st_mtime < cutoff:
                backup.unlink()
                removed.append(backup.name)
                logger.info("Removed old Fernet backup: %s", backup.name)
        except OSError as e:
            logger.warning(
                "Failed to remove %s: %s", backup, str(e)[:200],
            )
    return removed
