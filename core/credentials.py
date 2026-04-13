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

import json
import logging
import os
import stat
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
