"""Filesystem path helpers for the multi-tenant layout.

All user-scoped paths flow through this module so the on-disk
convention is defined in exactly one place. Tests can redirect the
whole tree by monkey-patching ``BASE_DIR`` — no need to stub each
helper individually.

Layout contract (Phase 2):

    config/bots/<user_id>/<slug>.yaml
    logs/<user_id>/<slug>.state.json
    logs/<user_id>/<slug>.log
    logs/<user_id>/<slug>.manual_trigger
    logs/<user_id>/pids/<slug>.pid
    keys/<user_id>.key                  (chmod 0600)
    credentials/<user_id>/<exchange>.enc (chmod 0600)

Directories are created on demand by the ``user_*_dir`` helpers and
marked 0700 where privacy matters (keys, credentials). Files land
at 0600 via the caller (credentials.py sets it on write).

System-level paths (audit log, portal PID, reverto.db) are NOT in
this module — they stay user-agnostic.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

# Resolved once at import; tests override by direct assignment on
# ``core.paths.BASE_DIR`` if they need to sandbox the layout.
BASE_DIR: Path = Path(__file__).resolve().parent.parent


# ── Directory helpers ─────────────────────────────────────────────────────


def _ensure_dir(path: Path, mode: int = 0o755) -> Path:
    """Create ``path`` (and parents) if missing. Best-effort chmod so a
    restrictive umask doesn't accidentally open the dir. Returns the
    path for ergonomic chaining."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def user_bots_dir(user_id: int) -> Path:
    """``config/bots/<user_id>/`` — all YAML bot configs for this user."""
    return _ensure_dir(BASE_DIR / "config" / "bots" / str(user_id))


def user_logs_dir(user_id: int) -> Path:
    """``logs/<user_id>/`` — parent of state/log/trigger files. 0755
    because subprocess.Popen needs execute bit and the operator may
    want to tail the logs manually."""
    return _ensure_dir(BASE_DIR / "logs" / str(user_id))


def user_pid_dir(user_id: int) -> Path:
    """``logs/<user_id>/pids/`` — per-user PID file directory.

    Splitting by user means two users with the same slug never collide
    on a pid file. 0755 like logs/ — PID files are not secret.
    """
    return _ensure_dir(user_logs_dir(user_id) / "pids")


def user_keys_dir() -> Path:
    """``keys/`` — root of the Fernet-key tree. 0700 because anyone
    who can read a key can decrypt the matching credentials blob."""
    return _ensure_dir(BASE_DIR / "keys", mode=0o700)


def user_credentials_dir(user_id: int) -> Path:
    """``credentials/<user_id>/`` — per-user encrypted-credentials tree.
    0700 for defence-in-depth against an umask that would otherwise
    leave the dir world-readable.

    Audit v24 carry-over LOW #4 (fixed): the parent ``credentials/``
    directory used to inherit the system umask (typically 0755) because
    ``Path.mkdir(parents=True)`` ignores the ``mode`` argument for
    intermediate parents. That leaked the user-id listing to any
    local account. We now ensure the parent explicitly at 0700 first
    before creating the child — both levels of the tree are now
    owner-only.
    """
    _ensure_dir(BASE_DIR / "credentials", mode=0o700)
    return _ensure_dir(
        BASE_DIR / "credentials" / str(user_id), mode=0o700,
    )


# ── File helpers ──────────────────────────────────────────────────────────


def bot_yaml_path(user_id: int, slug: str) -> Path:
    """``config/bots/<user_id>/<slug>.yaml``."""
    return user_bots_dir(user_id) / f"{slug}.yaml"


def bot_log_path(user_id: int, slug: str) -> Path:
    """``logs/<user_id>/<slug>.log`` — subprocess stdout/stderr."""
    return user_logs_dir(user_id) / f"{slug}.log"


def bot_state_path(user_id: int, slug: str) -> Path:
    """``logs/<user_id>/<slug>.state.json`` — engine state snapshot."""
    return user_logs_dir(user_id) / f"{slug}.state.json"


def bot_state_lock_path(user_id: int, slug: str) -> Path:
    """``logs/<user_id>/<slug>.state.lock`` — sibling lock file for
    cross-process coordination around state-file mutation.

    Held briefly by the portal (during offline-close state mutation)
    and by a starting bot (during _load_state) so the two processes
    serialise instead of racing on ``state.json``.
    """
    return user_logs_dir(user_id) / f"{slug}.state.lock"


def bot_pid_path(user_id: int, slug: str) -> Path:
    """``logs/<user_id>/pids/<slug>.pid`` — engine-process PID file."""
    return user_pid_dir(user_id) / f"{slug}.pid"


def bot_manual_trigger_path(user_id: int, slug: str) -> Path:
    """``logs/<user_id>/<slug>.manual_trigger`` — sentinel that the
    engine consumes to force-open a deal on the next tick."""
    return user_logs_dir(user_id) / f"{slug}.manual_trigger"


def user_fernet_key_path(user_id: int) -> Path:
    """``keys/<user_id>.key`` — per-user Fernet master key. The caller
    (core.credentials) is responsible for chmod 0600 on write; we
    return the path either way so tests can assert on permissions."""
    user_keys_dir()
    return BASE_DIR / "keys" / f"{user_id}.key"


def exchange_creds_path(user_id: int, exchange: str) -> Path:
    """``credentials/<user_id>/<exchange>.enc`` — encrypted payload for
    one exchange belonging to one user."""
    return user_credentials_dir(user_id) / f"{exchange}.enc"


# ── File-permission helper ────────────────────────────────────────────────


def ensure_secret_file_mode(path: Path) -> None:
    """Best-effort chmod 0600 on a file that's just been written. Used
    by credentials.py after each write to the key + .enc files so an
    overly permissive umask doesn't leak ciphertext readable to
    other users on the host."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
