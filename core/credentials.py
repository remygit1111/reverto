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

import fcntl
import json
import logging
import os
import shutil
import time
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

# ── Per-user Fernet key ────────────────────────────────────────────────────


def _load_or_create_user_key(user_id: int) -> bytes:
    """Return the raw Fernet key for ``user_id``, generating a fresh
    one on first use. The key file lands at ``keys/<user_id>.key``
    with mode 0600 and its parent dir 0700 so a permissive umask
    can't accidentally expose it to other local users."""
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


def _user_fernet(user_id: int) -> Fernet:
    return Fernet(_load_or_create_user_key(user_id))


def get_fernet(user_id: int) -> Fernet:
    """Public alias for _user_fernet — used by external tooling that
    wants to encrypt/decrypt a user-scoped payload itself."""
    return _user_fernet(user_id)


# ── Per-user exchange credentials (public API) ─────────────────────────────


def save_keys(
    exchange: str, api_key: str, api_secret: str, user_id: int,
    *,
    passphrase: str = "",
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
    """
    f = _user_fernet(user_id)
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
        "Credentials opgeslagen voor user %d / %s", user_id, exchange,
    )


def get_keys(exchange: str, user_id: int) -> Optional[dict]:
    """Decrypt en retourneer ``{'api_key', 'api_secret', 'passphrase'}``
    voor ``(user_id, exchange)``, of None als het bestand ontbreekt
    of niet decrypteerbaar is onder de huidige user-key.

    Geen exceptie naar boven — het aanroep-pad (engine boot, portal
    list) moet blijven werken als één paar credentials corrupt blijkt.

    Audit r1-012: ``passphrase`` is always present in the returned
    dict (empty string when not stored) so callers can treat the
    shape uniformly. Legacy .enc files written before the field
    existed decrypt into a dict without ``passphrase`` — we backfill
    with "" so the shape is stable.
    """
    path = exchange_creds_path(user_id, exchange)
    if not path.exists():
        return None
    try:
        f = _user_fernet(user_id)
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


def get_bitget_passphrase(user_id: int) -> str:
    """Return the Bitget passphrase for ``user_id``, preferring the
    per-user credentials store over the legacy process-wide env-var.

    Audit r1-012 migration path:

      1. If ``get_keys("bitget", user_id)`` returned a non-empty
         ``passphrase`` field, use that.
      2. Otherwise fall back to ``BITGET_PASSPHRASE`` from the env
         (pre-r1-012 layout) and emit a deprecation warning so the
         operator sees the migration signal exactly once per
         bot-start.
      3. If neither source yields a passphrase, raise ``ValueError``
         — the caller (``_authenticated_exchange`` in ``main_live``)
         treats that as "refuse to boot live" and returns None.

    This helper is intentionally Bitget-specific. Kraken et al.
    don't use passphrases and the dispatch stays explicit rather
    than hiding exchange-specific knowledge behind a generic
    ``get_passphrase(exchange, user_id)``. A second exchange that
    needs a passphrase would get its own helper or a parameterised
    version; mid-migration that's more churn than it's worth.
    """
    stored = get_keys("bitget", user_id=user_id)
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


def has_keys(exchange: str, user_id: int) -> bool:
    """True als er een .enc bestand bestaat voor (user_id, exchange).
    Decrypt wordt NIET getest — dat kost een Fernet-rondgang per call
    en get_keys kan er alsnog None voor retourneren."""
    return exchange_creds_path(user_id, exchange).exists()


def list_exchanges_with_keys(user_id: int) -> list[str]:
    """Lijst van exchange-namen met .enc files onder deze user. Sorted
    zodat UI-lijsten deterministisch zijn."""
    cred_dir = user_credentials_dir(user_id)
    if not cred_dir.exists():
        return []
    return sorted(p.stem for p in cred_dir.glob("*.enc"))


def delete_keys(exchange: str, user_id: int) -> bool:
    """Verwijder credentials voor (user_id, exchange). Retourneert True
    als er iets verwijderd is. Ontbrekende files zijn een no-op (False)."""
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
        "Credentials verwijderd voor user %d / %s", user_id, exchange,
    )
    return True


# ── Rotation (per-user key + .enc re-encrypt) ──────────────────────────────


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


def rotate_fernet_key(
    user_id: int = 1,
    retention_days: int = 7,
) -> dict:
    """Rotate a user's Fernet key and re-encrypt every .enc file under
    that user's credentials dir.

    Commit-order contract (critical for recoverability):
      1. Load every (user_id, exchange) blob under the OLD key.
      2. Snapshot the old key to a timestamped backup
         (``<user_id>.key.bak.YYYYMMDDHHMMSS`` — microseconds included
         so two rotations in the same second don't collide).
      3. Generate a fresh key and re-encrypt every payload in memory.
      4. Write new .enc tmp files AND new key tmp file.
      5. os.replace the key file FIRST, then each .enc. A crash between
         means NEW-key + OLD-creds → get_keys returns None, recoverable
         by restoring the .bak.<timestamp>. If we wrote creds first
         and crashed, the new key would exist only in memory and the
         on-disk new-key-encrypted .enc files would be unrecoverable.
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
        exchanges = list_exchanges_with_keys(user_id)
        plaintext: dict[str, dict[str, str]] = {}
        for exchange in exchanges:
            decrypted = get_keys(exchange, user_id=user_id)
            if decrypted is None:
                raise RuntimeError(
                    f"Cannot rotate user {user_id} — credentials for "
                    f"{exchange!r} failed to decrypt under the current "
                    "master key. Fix the key/cipher mismatch first."
                )
            plaintext[exchange] = decrypted

        # 2. Timestamped backup.
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        backup_path = keyfile.with_suffix(keyfile.suffix + f".bak.{timestamp}")
        shutil.copy2(keyfile, backup_path)
        ensure_secret_file_mode(backup_path)

        # 3 + 4. Fresh key, re-encrypt everything under it (in memory),
        # write every tmp file BEFORE committing any.
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
