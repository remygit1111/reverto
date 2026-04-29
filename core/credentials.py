# core/credentials.py
# Encrypted opslag van exchange API keys.
#
# Sinds Phase-3a bevat deze module één path: **per-user exchange
# credentials**. De pre-Phase-3a system-level Fernet helpers
# (save_encrypted / load_encrypted / _system_fernet) zijn
# verwijderd in audit v26-06 — ze bedienden alleen logs/.auth.json,
# en admin-auth leeft nu in ``users.password_hash`` (bcrypt) in de
# DB. Zie commit d69fcd2 (v26-06) voor de delete.
#
# Per-user exchange credentials:
#   - Fernet key at  keys/<user_id>.key                  (chmod 0600)
#   - Payload at     credentials/<user_id>/<exchange>.enc (chmod 0600)
#   - save_keys / get_keys / has_keys / list_exchanges_with_keys /
#     delete_keys / rotate_fernet_key all operate on this tree.
#
# Threat model:
#   - Code injection / RCE → key files + ciphertext unusable without the
#     private key — but an attacker with shell access on the host has
#     both anyway, so Fernet is no defence against full host compromise.
#   - Disk dump without the host's filesystem permissions → ciphertext
#     alone is useless.
#   - Cross-user data leak on a multi-tenant host → user 2 cannot
#     decrypt user 1's .enc files because the per-user keys diverge.
#     THIS is the primary Phase-2 security property.
#
# Design notes:
#   - The per-user .enc file format is:
#       Fernet( utf-8( json({"api_key": ..., "api_secret": ...}) ) )
#     i.e. ONE encrypted blob per (user, exchange). Phase-1 used per-
#     field Fernet inside a single JSON store; per-file is simpler to
#     rotate and scopes the blast radius of a corrupt decrypt.
#   - user_id arguments are required on every public function (the
#     Phase-1 contract). Phase 2 now actually uses them.
#
# Phase-A wrap-up — provider abstraction:
#   The credential pipeline is split into a ``CredentialProvider`` ABC
#   plus a ``FernetCredentialProvider`` concrete implementation. The
#   existing module-level functions (``save_keys`` / ``get_keys`` / …)
#   are kept as thin shims that delegate to a process-wide default
#   provider instance, so every existing call site stays identical.
#   The seam exists for Phase-C: a forthcoming signing-service backend
#   that holds plaintext credentials out-of-process will register as
#   the default provider, and the rest of the app needs no diff.
#
# Known limitation — audit r1-010 (ACCEPTED):
#   After ``get_keys`` decrypts, the plaintext credentials live in
#   the calling engine's Python process heap for the lifetime of
#   that engine. A core-dump or live memory-scraping attack on the
#   host would expose them. Phase-C's signing-service design moves
#   credential use out-of-process, which resolves this category of
#   risk. Mitigations in place until Phase-C:
#     * Single-host deploy → only one attack surface (no shared
#       tenants on the same box).
#     * Subprocess env-var leak is closed (r1-023 allowlist).
#     * Fernet key files are 0600 under a 0700 dir.
#   Accepted risk for single-operator on a trusted host. Revisit
#   when VPS-deploy introduces new attacker profiles.

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
    exchange_creds_path,
    user_credentials_dir,
    user_fernet_key_path,
    user_keys_dir,
)

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent


# ── CredentialProvider ABC ─────────────────────────────────────────────────
#
# The interface every backend (Fernet today, signing-service in Phase-C)
# must satisfy. Methods deliberately mirror the historical free-function
# signatures so every existing call site stays a one-line delegate. A
# Phase-C provider will satisfy the same contract by talking to an
# out-of-process signing daemon — the rest of the codebase only ever
# sees this interface.


# ── r2-010: API-key format validation ─────────────────────────────────────
#
# Pre-fix ``save_keys`` accepted any non-empty strings and the format
# error only surfaced on the first authenticated exchange call —
# typically a bot-start event tens of seconds after the operator
# pressed Save. The save-to-error feedback loop was slow and the
# error message came from ccxt rather than from the save handler.
#
# Heuristic length-bounds catch the typical typo modes (truncated
# paste, wrong field pasted into the wrong input, partial copy)
# without locking us in to a specific format the exchange might
# evolve. The patterns are deliberately generous — exchanges have
# rotated their key formats more than once over the last few years
# and we'd rather fail-open on a future format than fail-closed on a
# legitimate key.
#
# Wire-up: ``FernetCredentialProvider.save_keys`` calls
# ``_validate_api_key_format`` before the encrypt step; failure
# raises ``CredentialFormatError`` (a ``ValueError`` subclass) with
# a hint about the expected shape and the actual length, so the
# operator has enough to diagnose the typo without leaking the key.

# Bitget API key — observed shape: alphanumeric, ~32-64 characters.
# Older keys were 32; newer Bitget v2 keys observed at 36-50.
_BITGET_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{16,128}$")
# Bitget API secret — also alphanumeric, similar length range.
_BITGET_API_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9]{16,128}$")
# Kraken API key — 56-character base64 (A-Z a-z 0-9 + /).
# Pattern accepts the 40-128 range to absorb format drift.
_KRAKEN_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9+/=]{40,128}$")
# Kraken API secret — 88-character base64. Same generous bounds.
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

    r2-010: catches obvious typos at credential-save time rather
    than at first authenticated exchange call. Patterns are
    intentionally permissive (16-128 chars, alphanumeric or base64)
    so a future format-rotation by the exchange does NOT lock out
    legitimate operators — the goal is to catch truncation /
    wrong-field-pasted / partial-copy mistakes, not to enforce a
    schema.

    Unknown exchanges fall through silently; the validator is
    additive and extends to new exchanges by adding a branch.
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
    # Unknown exchange — pass through unvalidated. Adding a future
    # exchange means appending a branch above.


class CredentialProvider(ABC):
    """Abstract backend for storing and retrieving exchange API
    credentials on a per-user basis.

    Implementations MUST be safe to call from multiple threads inside
    one process — the FernetCredentialProvider achieves this by being
    stateless aside from filesystem I/O, which is itself serialised by
    OS-level write semantics. They MUST NOT silently swallow decrypt
    failures (return ``None`` from ``get_keys`` instead) so a partial
    corruption never gets papered over as "no creds stored".
    """

    @abstractmethod
    def save_keys(
        self,
        exchange: str,
        api_key: str,
        api_secret: str,
        user_id: int,
        *,
        passphrase: str = "",
        _skip_format_validation: bool = False,
    ) -> None:
        """Persist ``(api_key, api_secret, optional passphrase)`` for
        ``(user_id, exchange)``. Atomic — a crash mid-write leaves
        either the prior payload intact or the new one fully written,
        never a half-written blob.

        ``_skip_format_validation`` is the test-only escape hatch
        described on ``FernetCredentialProvider.save_keys``; production
        routes never set it."""

    @abstractmethod
    def get_keys(
        self, exchange: str, user_id: int,
    ) -> Optional[dict]:
        """Return ``{'api_key', 'api_secret', 'passphrase'}`` for
        ``(user_id, exchange)``, or ``None`` when no payload exists or
        decryption failed. ``passphrase`` is always present in the
        returned dict (empty string when not stored) so callers can
        treat the shape uniformly."""

    @abstractmethod
    def has_keys(self, exchange: str, user_id: int) -> bool:
        """Cheap presence-check — does NOT attempt a decrypt round-trip
        (which would cost a Fernet load on every call). ``get_keys``
        may still return ``None`` for an existing-but-corrupt payload."""

    @abstractmethod
    def list_exchanges_with_keys(self, user_id: int) -> list[str]:
        """Sorted list of exchange slugs that have stored credentials
        for ``user_id``. Sorted output keeps UI lists deterministic."""

    @abstractmethod
    def delete_keys(self, exchange: str, user_id: int) -> bool:
        """Remove the credentials for ``(user_id, exchange)``. Returns
        True when something was deleted, False when the entry was
        already absent."""

    @abstractmethod
    def get_bitget_passphrase(self, user_id: int) -> str:
        """Return the Bitget passphrase for ``user_id``. Bitget-
        specific because no other supported exchange uses a passphrase
        — keeping it explicit avoids hiding exchange knowledge behind
        a generic ``get_passphrase(exchange, user_id)``. Raises
        ``ValueError`` when no passphrase can be sourced (caller treats
        that as 'refuse to boot live')."""

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
        above wrap a JSON payload of (api_key, api_secret, passphrase)
        through the same key, but other callers (Phase B TOTP seeds,
        future per-user secrets) only need a scalar string. Returned
        ciphertext is a UTF-8 string suitable for direct DB storage.
        """

    @abstractmethod
    def decrypt_for_user(self, user_id: int, ciphertext: str) -> str:
        """Decrypt a string previously produced by ``encrypt_for_user``.

        Raises on tamper / wrong key / corruption — callers MUST
        handle the failure path explicitly. Unlike ``get_keys`` we
        do NOT swallow decrypt failures here, because the caller
        almost always wants to distinguish "no value stored" (which
        they can detect at the DB layer with a NULL) from "stored
        value won't decrypt" (an integrity event worth surfacing).
        """


# ── FernetCredentialProvider (concrete, on-disk Fernet backend) ────────────


class FernetCredentialProvider(CredentialProvider):
    """On-disk Fernet-backed implementation. The historical default
    (and currently the only production backend). Phase-C will add a
    second implementation that delegates to an out-of-process signing
    service; all module-level shims will then transparently route to
    whichever provider is registered as the default.
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
            "Nieuw Fernet key gegenereerd voor user %d in %s — verlies "
            "dit bestand niet, anders zijn opgeslagen exchange keys "
            "voor deze user onbruikbaar.",
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

    def save_keys(
        self,
        exchange: str,
        api_key: str,
        api_secret: str,
        user_id: int,
        *,
        passphrase: str = "",
        _skip_format_validation: bool = False,
    ) -> None:
        """Versleutel en schrijf een api_key + api_secret (+ optional
        passphrase) voor ``(user_id, exchange)`` naar
        ``credentials/<user_id>/<exchange>.enc``.

        Atomic write via .tmp → os.replace so a crash mid-write never
        leaves a half-encrypted file.

        Audit r1-012: the passphrase field carries Bitget's third
        credential piece (user-chosen at API-key creation time). Kept
        optional on the signature because non-Bitget exchanges (Kraken
        today) don't use a passphrase. Empty string means "no passphrase
        stored" — get_keys then returns ``passphrase=""`` and the
        consumer-facing helper ``get_bitget_passphrase`` falls through
        to its env-var migration path.

        Audit r2-010: format-validate the api_key + api_secret BEFORE
        the encrypt step. A typo / truncation / wrong-field paste now
        raises ``CredentialFormatError`` at save-time instead of
        surfacing as a ccxt auth error tens of seconds later on the
        first exchange call. ``_skip_format_validation`` is an
        internal escape hatch for the test suite — many fixtures use
        placeholder values like ``"ak"`` / ``"sc"`` that the heuristic
        validator rightly rejects, but those tests exercise rotation /
        migration / I/O paths, not the format check itself. Production
        routes never set this flag; the leading underscore + keyword-
        only marker on the signature signal "test-only".
        """
        if not _skip_format_validation:
            _validate_api_key_format(exchange, api_key, api_secret)

        f = self._user_fernet(user_id)
        payload_dict: dict[str, str] = {
            "api_key": api_key,
            "api_secret": api_secret,
        }
        if passphrase:
            payload_dict["passphrase"] = passphrase
        payload = json.dumps(payload_dict).encode("utf-8")
        blob = f.encrypt(payload)

        dst = exchange_creds_path(user_id, exchange)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        tmp.write_bytes(blob)
        ensure_secret_file_mode(tmp)
        tmp.replace(dst)
        ensure_secret_file_mode(dst)
        logger.info(
            "Credentials opgeslagen voor user %d / %s",
            user_id, exchange,
        )

    def get_keys(
        self, exchange: str, user_id: int,
    ) -> Optional[dict]:
        """Decrypt en retourneer ``{'api_key', 'api_secret',
        'passphrase'}`` voor ``(user_id, exchange)``, of None als het
        bestand ontbreekt of niet decrypteerbaar is onder de huidige
        user-key.

        Geen exceptie naar boven — het aanroep-pad (engine boot,
        portal list) moet blijven werken als één paar credentials
        corrupt blijkt.

        Audit r1-012: ``passphrase`` is always present in the returned
        dict (empty string when not stored) so callers can treat the
        shape uniformly. Legacy .enc files written before the field
        existed decrypt into a dict without ``passphrase`` — we
        backfill with "" so the shape is stable.
        """
        path = exchange_creds_path(user_id, exchange)
        if not path.exists():
            return None
        try:
            f = self._user_fernet(user_id)
            raw = f.decrypt(path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, OSError, json.JSONDecodeError) as e:
            logger.error(
                "Kan credentials niet decrypten voor user %d / %s: %s",
                user_id, exchange, e,
            )
            return None
        if not isinstance(data, dict):
            return None
        # Keep only the expected fields — defensive against future
        # key-file tampering that shoves arbitrary keys into the blob.
        return {
            "api_key":    data.get("api_key", ""),
            "api_secret": data.get("api_secret", ""),
            "passphrase": data.get("passphrase", ""),
        }

    def get_bitget_passphrase(self, user_id: int) -> str:
        """Return the Bitget passphrase for ``user_id``, preferring
        the per-user credentials store over the legacy process-wide
        env-var.

        Audit r1-012 migration path:

          1. If ``get_keys("bitget", user_id)`` returned a non-empty
             ``passphrase`` field, use that.
          2. Otherwise fall back to ``BITGET_PASSPHRASE`` from the env
             (pre-r1-012 layout) and emit a deprecation warning so the
             operator sees the migration signal exactly once per
             bot-start.
          3. If neither source yields a passphrase, raise ``ValueError``
             — the caller (``_authenticated_exchange`` in
             ``main_live``) treats that as "refuse to boot live" and
             returns None.

        This helper is intentionally Bitget-specific. Kraken et al.
        don't use passphrases and the dispatch stays explicit rather
        than hiding exchange-specific knowledge behind a generic
        ``get_passphrase(exchange, user_id)``. A second exchange that
        needs a passphrase would get its own helper or a parameterised
        version; mid-migration that's more churn than it's worth.
        """
        stored = self.get_keys("bitget", user_id=user_id)
        if stored and stored.get("passphrase"):
            return stored["passphrase"]
        legacy = os.environ.get("BITGET_PASSPHRASE", "")
        if legacy:
            logger.warning(
                "BITGET_PASSPHRASE loaded from env-var for user=%d — "
                "deprecated (audit r1-012). Re-save Bitget credentials "
                "via /api/exchanges/bitget/keys to move the passphrase "
                "into the encrypted credentials store.",
                user_id,
            )
            return legacy
        raise ValueError(
            f"No Bitget passphrase available for user={user_id}: "
            "credentials-store has no passphrase field and "
            "BITGET_PASSPHRASE env-var is unset. Save credentials via "
            "POST /api/exchanges/bitget/keys with a passphrase field."
        )

    def has_keys(self, exchange: str, user_id: int) -> bool:
        """True als er een .enc bestand bestaat voor (user_id,
        exchange). Decrypt wordt NIET getest — dat kost een Fernet-
        rondgang per call en get_keys kan er alsnog None voor
        retourneren."""
        return exchange_creds_path(user_id, exchange).exists()

    def list_exchanges_with_keys(self, user_id: int) -> list[str]:
        """Lijst van exchange-namen met .enc files onder deze user.
        Sorted zodat UI-lijsten deterministisch zijn."""
        cred_dir = user_credentials_dir(user_id)
        if not cred_dir.exists():
            return []
        return sorted(p.stem for p in cred_dir.glob("*.enc"))

    def delete_keys(self, exchange: str, user_id: int) -> bool:
        """Verwijder credentials voor (user_id, exchange). Retourneert
        True als er iets verwijderd is. Ontbrekende files zijn een
        no-op (False)."""
        path = exchange_creds_path(user_id, exchange)
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError as e:
            logger.error(
                "Kan credentials niet verwijderen voor user %d / %s: %s",
                user_id, exchange, e,
            )
            return False
        logger.info(
            "Credentials verwijderd voor user %d / %s",
            user_id, exchange,
        )
        return True

    # ── Rotation (per-user key + .enc re-encrypt) ──

    def rotate_user_key(
        self,
        user_id: int = 1,
        retention_days: int = 7,
    ) -> dict:
        """Rotate a user's Fernet key and re-encrypt every .enc file
        under that user's credentials dir.

        Commit-order contract (critical for recoverability):
          1. Load every (user_id, exchange) blob under the OLD key.
          2. Snapshot the old key to a timestamped backup
             (``<user_id>.key.bak.YYYYMMDDHHMMSS`` — microseconds
             included so two rotations in the same second don't
             collide).
          3. Generate a fresh key and re-encrypt every payload in
             memory.
          4. Write new .enc tmp files AND new key tmp file.
          5. os.replace the key file FIRST, then each .enc. A crash
             between means NEW-key + OLD-creds → get_keys returns
             None, recoverable by restoring the .bak.<timestamp>. If
             we wrote creds first and crashed, the new key would
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
            # 1. Load every credential under the old key.
            exchanges = self.list_exchanges_with_keys(user_id)
            plaintext: dict[str, dict[str, str]] = {}
            for exchange in exchanges:
                decrypted = self.get_keys(exchange, user_id=user_id)
                if decrypted is None:
                    raise RuntimeError(
                        f"Cannot rotate user {user_id} — credentials "
                        f"for {exchange!r} failed to decrypt under "
                        "the current master key. Fix the key/cipher "
                        "mismatch first."
                    )
                plaintext[exchange] = decrypted

            # 2. Timestamped backup.
            timestamp = datetime.now(timezone.utc).strftime(
                "%Y%m%d%H%M%S%f",
            )
            backup_path = keyfile.with_suffix(
                keyfile.suffix + f".bak.{timestamp}",
            )
            shutil.copy2(keyfile, backup_path)
            ensure_secret_file_mode(backup_path)

            # 3 + 4. Fresh key, re-encrypt everything under it (in
            # memory), write every tmp file BEFORE committing any.
            new_key = Fernet.generate_key()
            new_fernet = Fernet(new_key)

            tmp_enc: dict[str, Path] = {}
            for exchange, pt in plaintext.items():
                blob = new_fernet.encrypt(
                    json.dumps(pt).encode("utf-8"),
                )
                dst = exchange_creds_path(user_id, exchange)
                tmp = dst.with_suffix(dst.suffix + ".tmp")
                tmp.write_bytes(blob)
                ensure_secret_file_mode(tmp)
                tmp_enc[exchange] = tmp

            tmp_key = keyfile.with_suffix(keyfile.suffix + ".tmp")
            tmp_key.write_bytes(new_key)
            ensure_secret_file_mode(tmp_key)

            # 5. Commit key first, then each .enc file.
            os.replace(tmp_key, keyfile)
            for exchange, tmp in tmp_enc.items():
                dst = exchange_creds_path(user_id, exchange)
                os.replace(tmp, dst)

            # 6. Retention cleanup.
            removed = cleanup_old_backups(keyfile, retention_days)

        logger.warning(
            "Fernet master key rotated for user %d. Backup at %s. "
            "Rotated exchanges: %s. Removed old backups: %d",
            user_id, backup_path, sorted(plaintext), len(removed),
        )
        return {
            "user_id": user_id,
            "rotated_at": datetime.now(timezone.utc).isoformat(),
            "backup_path": str(backup_path),
            "keys_rotated": sorted(plaintext.keys()),
            "backups_removed": removed,
        }

    # ── Generic per-user encrypt / decrypt (Phase B foundation) ──

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
#
# Every public name here is a thin delegate to the process-wide default
# provider. External callers (web/, scripts/, main_live.py) keep their
# existing ``from core import credentials; credentials.get_keys(...)``
# pattern; only the indirection target moves. Phase-C will swap the
# default to a signing-service-backed provider via
# ``set_default_provider`` without touching any call sites.

_default_provider: CredentialProvider = FernetCredentialProvider()


def get_default_provider() -> CredentialProvider:
    """Return the process-wide default credential provider. Tests
    that need to assert which backend is wired in can call this
    instead of probing module-private state."""
    return _default_provider


def set_default_provider(provider: CredentialProvider) -> None:
    """Replace the process-wide default provider. Intended for
    Phase-C swap-in (signing-service backend) and for tests that
    want to inject a fake. Callers MUST hold this swap to a single
    site at process start; mid-flight swaps would race against any
    in-flight credential operations."""
    global _default_provider
    _default_provider = provider


# ── Free-function shims (legacy public surface) ────────────────────────────


def get_fernet(user_id: int) -> Fernet:
    """Module-level shim — only valid for FernetCredentialProvider.
    A non-Fernet provider (Phase-C signing service) will raise
    ``AttributeError`` here, which is the correct failure mode: the
    caller is reaching for a Fernet handle the new backend doesn't
    expose."""
    return _default_provider.get_fernet(user_id)  # type: ignore[attr-defined]


def save_keys(
    exchange: str,
    api_key: str,
    api_secret: str,
    user_id: int,
    *,
    passphrase: str = "",
    _skip_format_validation: bool = False,
) -> None:
    _default_provider.save_keys(
        exchange, api_key, api_secret, user_id,
        passphrase=passphrase,
        _skip_format_validation=_skip_format_validation,
    )


def get_keys(exchange: str, user_id: int) -> Optional[dict]:
    return _default_provider.get_keys(exchange, user_id)


def get_bitget_passphrase(user_id: int) -> str:
    return _default_provider.get_bitget_passphrase(user_id)


def has_keys(exchange: str, user_id: int) -> bool:
    return _default_provider.has_keys(exchange, user_id)


def list_exchanges_with_keys(user_id: int) -> list[str]:
    return _default_provider.list_exchanges_with_keys(user_id)


def delete_keys(exchange: str, user_id: int) -> bool:
    return _default_provider.delete_keys(exchange, user_id)


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


# ── Module-private helpers (shared by every provider) ──────────────────────
#
# Kept at module scope rather than as classmethods because they're
# pure file-system primitives — a future provider impl that needs the
# same locking or backup-retention semantics can reuse them without
# inheriting from FernetCredentialProvider.


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
