# core/credentials.py
# Encrypted opslag van exchange API keys.
#
# Master key wordt bij eerste gebruik gegenereerd en opgeslagen in
# logs/.credentials.key (chmod 600). Credentials zelf staan in
# logs/credentials.json met per-veld Fernet ciphertext. Beide files
# vallen onder het bestaande logs/ gitignore patroon, maar het is OK
# als de operator extra paranoid wil zijn en ze elders mount.
#
# Threat model:
#   - Code injection / RCE → master key + ciphertext zelf onleesbaar
#     zonder de private key file
#   - Disk dump zonder file system → ciphertext nutteloos
#   - Disk dump met file system → key file en ciphertext beide leesbaar,
#     dus encryption is geen verdediging tegen volledige host compromise.
#     Dat is by design — Fernet beschermt alleen tegen losse JSON dumps,
#     casual git pushes en log scrapers.

import fcntl
import json
import logging
import os
import shutil
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent
_LOG_DIR = _BASE_DIR / "logs"
_KEY_FILE = _LOG_DIR / ".credentials.key"
_STORE_FILE = _LOG_DIR / "credentials.json"


def _load_or_create_master_key() -> bytes:
    """Lees het master key bestand of genereer er een en schrijf hem
    met restrictieve mode (chmod 600)."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        os.chmod(_KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError as e:
        logger.warning("Kon chmod 600 niet zetten op %s: %s", _KEY_FILE, e)
    logger.warning(
        "Nieuw credentials master key gegenereerd in %s — verlies dit "
        "bestand niet, anders zijn opgeslagen exchange keys onbruikbaar.",
        _KEY_FILE,
    )
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_master_key())


def _read_store() -> dict:
    if not _STORE_FILE.exists():
        return {}
    try:
        return json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Kan credentials store niet lezen: %s", e)
        return {}


def _write_store(store: dict) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    tmp.replace(_STORE_FILE)
    try:
        os.chmod(_STORE_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass


def save_keys(exchange: str, api_key: str, api_secret: str) -> None:
    """Versleutel en sla een api_key + api_secret paar op voor `exchange`."""
    f = _fernet()
    store = _read_store()
    store[exchange] = {
        "api_key":    f.encrypt(api_key.encode("utf-8")).decode("ascii"),
        "api_secret": f.encrypt(api_secret.encode("utf-8")).decode("ascii"),
    }
    _write_store(store)
    logger.info("Credentials opgeslagen voor %s", exchange)


def get_keys(exchange: str) -> Optional[dict]:
    """Decrypt en retourneer {'api_key': ..., 'api_secret': ...} of None."""
    store = _read_store()
    entry = store.get(exchange)
    if not entry:
        return None
    f = _fernet()
    try:
        return {
            "api_key":    f.decrypt(entry["api_key"].encode("ascii")).decode("utf-8"),
            "api_secret": f.decrypt(entry["api_secret"].encode("ascii")).decode("utf-8"),
        }
    except (InvalidToken, KeyError, ValueError) as e:
        logger.error("Kan credentials niet decrypten voor %s: %s", exchange, e)
        return None


def has_keys(exchange: str) -> bool:
    """True als er een entry bestaat voor `exchange` (decrypt niet getest)."""
    return exchange in _read_store()


def list_exchanges_with_keys() -> list[str]:
    """Lijst van exchange namen waarvoor credentials zijn opgeslagen."""
    return sorted(_read_store().keys())


def save_encrypted(path: Path, data: dict) -> None:
    """Versleutel `data` (JSON serialiseerbaar) met de Reverto master key
    en schrijf het resultaat naar `path`. Herbruikt dezelfde Fernet key
    die ook de exchange credentials beschermt zodat er maar één
    key-bestand is om te bewaren.
    """
    f = _fernet()
    blob = f.encrypt(json.dumps(data).encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass


def load_encrypted(path: Path) -> Optional[dict]:
    """Decrypt een met `save_encrypted` geschreven file en retourneer
    de originele dict, of None als de file ontbreekt of onleesbaar is."""
    if not path.exists():
        return None
    try:
        blob = path.read_bytes()
        f = _fernet()
        raw = f.decrypt(blob)
        return json.loads(raw.decode("utf-8"))
    except (InvalidToken, ValueError, OSError, json.JSONDecodeError) as e:
        logger.error("Kan encrypted file %s niet lezen: %s", path, e)
        return None


@contextmanager
def _rotation_lock(keyfile: Path):
    """Process-level advisory lock around key rotation.

    Two concurrent ``rotate_fernet_key`` calls used to be able to both
    generate fresh keys and overwrite each other in unpredictable
    order, leaving the credentials file decrypted under whichever key
    lost the race. An fcntl advisory lock on a sentinel file gates
    the critical section; second caller gets a clear error instead of
    silent corruption.
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
    keyfile: Optional[Path] = None,
    retention_days: int = 7,
) -> list[str]:
    """Delete timestamped Fernet backups older than ``retention_days``.

    Backups are created by ``rotate_fernet_key`` as
    ``.credentials.key.bak.YYYYMMDDHHMMSS``. Over time they accumulate
    — a hobbyist rotating monthly ends up with hundreds of files. This
    helper is safe to call at the end of each rotation or from a cron
    job.
    """
    keyfile = keyfile or _KEY_FILE
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
    credentials_file: Optional[Path] = None,
    keyfile: Optional[Path] = None,
    retention_days: int = 7,
) -> dict:
    """Rotate the Fernet master key that protects every credential entry.

    Commit-order contract (critical for recoverability):
      1. Load every credential under the OLD key.
      2. Snapshot the old key to a timestamped backup
         (``.key.bak.YYYYMMDDHHMMSS``) — keyed copies are stored so
         multiple rotations don't overwrite the rollback image.
      3. Generate a fresh key and re-encrypt the credential values in
         memory under it.
      4. Write BOTH new ciphertext (tmp) AND new key (tmp) to disk.
      5. ``os.replace`` the key file FIRST, then the creds file. Why
         this order?  If we crash between the two replaces:
           * key: NEW, creds: OLD  → `get_keys` returns None (InvalidToken).
             Recovery = restore latest .bak.* over the key file, then
             either re-run rotation or continue running with the old
             key unchanged.  The creds on disk are still valid.
           * If we instead wrote creds first and crashed: creds: NEW
             (already encrypted with new key), key: OLD  → unrecoverable,
             since the new key lived only in memory.
         Writing the key first is the only order where the intermediate
         state is at least RECOVERABLE from the backup file.
      6. Sweep backups older than ``retention_days`` so the directory
         doesn't grow without bound.

    The whole function runs inside a file-level advisory lock so
    concurrent rotations fail fast instead of corrupting state.
    """
    keyfile = keyfile or _KEY_FILE
    credentials_file = credentials_file or _STORE_FILE

    if not keyfile.exists():
        raise FileNotFoundError(f"No master key at {keyfile} — nothing to rotate")

    with _rotation_lock(keyfile):
        # 1. Load everything under the old key.
        old_store = _read_store()
        plaintext: dict[str, dict[str, str]] = {}
        for name in old_store:
            decrypted = get_keys(name)
            if decrypted is None:
                raise RuntimeError(
                    f"Cannot rotate — credentials for {name!r} failed to decrypt "
                    "under the current master key. Fix the key/cipher mismatch first."
                )
            plaintext[name] = decrypted

        # 2. Timestamped backup so multiple rotations preserve history.
        # Include microseconds so two rotations in the same second don't
        # collide on the same filename. 20 char suffix fits in typical
        # filesystem name limits and stays lexicographically sortable.
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        backup_path = keyfile.with_suffix(keyfile.suffix + f".bak.{timestamp}")
        shutil.copy2(keyfile, backup_path)
        try:
            os.chmod(backup_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

        # 3 + 4. Fresh key, re-encrypt everything under it (in memory).
        new_key = Fernet.generate_key()
        new_fernet = Fernet(new_key)
        new_store: dict[str, dict[str, str]] = {}
        for name, pt in plaintext.items():
            new_store[name] = {
                k: new_fernet.encrypt(v.encode("utf-8")).decode("ascii")
                for k, v in pt.items()
            }

        # 5. Write both tmp files BEFORE committing either.
        tmp_key = keyfile.with_suffix(keyfile.suffix + ".tmp")
        tmp_key.write_bytes(new_key)
        try:
            os.chmod(tmp_key, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

        tmp_creds = credentials_file.with_suffix(credentials_file.suffix + ".tmp")
        tmp_creds.write_text(json.dumps(new_store, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp_creds, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

        # Commit key first, then creds. A crash between the two leaves
        # NEW-key + OLD-creds — recoverable by restoring the .bak.* file.
        os.replace(tmp_key, keyfile)
        os.replace(tmp_creds, credentials_file)

        # 6. Retention cleanup.
        removed = cleanup_old_backups(keyfile, retention_days)

    logger.warning(
        "Fernet master key rotated. Backup at %s. Rotated keys: %s. Removed old backups: %d",
        backup_path, list(new_store), len(removed),
    )
    return {
        "rotated_at": datetime.now(timezone.utc).isoformat(),
        "backup_path": str(backup_path),
        "keys_rotated": sorted(new_store.keys()),
        "backups_removed": removed,
    }


def delete_keys(exchange: str) -> bool:
    """Verwijder credentials voor `exchange`. Retourneert True als er
    iets verwijderd is."""
    store = _read_store()
    if exchange not in store:
        return False
    del store[exchange]
    _write_store(store)
    logger.info("Credentials verwijderd voor %s", exchange)
    return True
