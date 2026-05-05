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

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolved once at import; tests override by direct assignment on
# ``core.paths.BASE_DIR`` if they need to sandbox the layout.
BASE_DIR: Path = Path(__file__).resolve().parent.parent


# ── Directory helpers ─────────────────────────────────────────────────────


def _ensure_dir(
    path: Path,
    mode: int = 0o755,
    refuse_symlinks: bool = False,
) -> Path:
    """Create ``path`` (and parents) if missing. Best-effort chmod so a
    restrictive umask doesn't accidentally open the dir. Returns the
    path for ergonomic chaining.

    Args:
        path: Directory to ensure exists.
        mode: Permission bits (octal) to chmod the directory to.
        refuse_symlinks: If True and ``path`` exists as a symlink,
            raise RuntimeError instead of using it. Use for
            security-critical directories (keys, credentials) where
            a symlink could redirect secrets to an attacker- or
            operator-error-controlled location. Default False
            (warn-only) for non-security-critical directories so
            operator deploy-pain doesn't block portal startup.

    Raises:
        RuntimeError: When refuse_symlinks=True and path is a symlink.
    """
    # PT-v4-FS-002: symlink check happens BEFORE mkdir so a hostile
    # link can't be silently followed. mkdir(exist_ok=True) on an
    # existing symlink succeeds without touching the link itself.
    if path.is_symlink():
        if refuse_symlinks:
            raise RuntimeError(
                f"Refusing to use {path}: exists as symlink. "
                f"Possible permission-drift, deploy-error, or symlink "
                f"attack. Inspect target manually and remove the symlink "
                f"if it's no longer intended."
            )
        logger.warning(
            "Path %s exists as symlink; chmod will follow the link "
            "target. Verify target permissions manually if security "
            "matters here.", path,
        )

    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError as e:
        # PT-v4-FS-002: was silently swallowed pre-fix. WARN so
        # operators see permission drift in the boot log instead of
        # discovering it months later when secrets leaked through a
        # 0755 dir.
        logger.warning(
            "chmod %o failed for %s: %s. Directory may have incorrect "
            "permissions; verify manually.", mode, path, e,
        )
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
    who can read a key can decrypt the matching credentials blob.

    PT-v4-FS-002: refuses to proceed if ``keys/`` is a symlink. Keys
    must live on the canonical path so a deploy-error or attacker
    can't redirect them to a less-protected location.
    """
    return _ensure_dir(
        BASE_DIR / "keys", mode=0o700, refuse_symlinks=True,
    )


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

    PT-v4-FS-002: both levels refuse symlinks for the same reason as
    user_keys_dir — secrets must live on the canonical path so a
    deploy-error or attacker can't redirect them.
    """
    _ensure_dir(
        BASE_DIR / "credentials", mode=0o700, refuse_symlinks=True,
    )
    return _ensure_dir(
        BASE_DIR / "credentials" / str(user_id),
        mode=0o700,
        refuse_symlinks=True,
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


def user_ml_results_path(user_id: int, slug: str) -> Path:
    """``ml/<user_id>/results_<slug>.json`` — ML nightly-pipeline
    output, scoped per-user. Audit r1-049: pre-fix the file landed
    in ``ml/results_<slug>.json`` (no user folder), so two tenants
    with the same bot-slug would overwrite each other's ML output.
    """
    parent = _ensure_dir(BASE_DIR / "ml" / str(user_id))
    return parent / f"results_{slug}.json"


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


# ── Bot purge — PT-v4-FS-001 ─────────────────────────────────────────────


# Tables that carry per-bot state keyed on (user_id, bot_slug). Listed
# here (not inline in purge_bot) so the order of deletion is explicit
# and a future schema addition can extend the list with a single
# obvious entry. Order matters for FK dependencies: orders REFERENCES
# deals(id), so we wipe orders first using a sub-select before the
# parent deals row is gone.
_BOT_SCOPED_TABLES: tuple[str, ...] = (
    "chart_annotations",
    "backtest_runs",
)


def purge_bot(user_id: int, slug: str) -> dict:
    """Remove every (user_id, slug)-scoped artefact except the YAML.

    PT-v4-FS-001 — pre-fix, ``DELETE /api/bots/{slug}`` only unlinked
    the YAML, leaving state.json + logs + DB rows behind. A user who
    deleted then recreated a bot with the same slug saw the engine
    rehydrate from the OLD state.json — inheriting balance, open
    deals, drawdown peak, DCA overrides and PnL history.

    Removes (best-effort):

    Filesystem (under ``logs/<user_id>/``):
      * ``<slug>.log`` plus any rotated ``<slug>.log.1`` .. ``log.N``
      * ``<slug>.state.json``
      * ``<slug>.state.lock``
      * ``<slug>.manual_trigger``
      * ``<slug>.deal_edit_*`` / ``<slug>.deal_close_*`` /
        ``<slug>.deal_cancel_*`` sentinel files
      * ``logs/<user_id>/pids/<slug>.pid``
      * ``ml/<user_id>/results_<slug>.json``

    Database:
      * ``orders`` rows whose parent deal matches (user_id, slug)
      * ``deals`` rows for (user_id, slug)
      * ``chart_annotations`` rows for (user_id, slug)
      * ``backtest_runs`` rows for (user_id, slug)

    Preserved by design (caller's responsibility, or permanent):
      * ``config/bots/<user_id>/<slug>.yaml`` — caller unlinks AFTER
        this helper returns, so a partial-failure purge still leaves
        the bot re-deletable for retry. YAML-last principle.
      * The audit log (``logs/audit.log`` /  ``logs/audit.jsonl``) —
        history is permanent.
      * User credentials, keys, account state, and any other-user /
        other-slug artefacts.

    Returns a summary dict::

        {
            "files_removed": int,
            "files_failed": list[str],     # paths that couldn't unlink
            "db_rows_removed": dict,       # per-table rowcount
            "warnings": list[str],         # non-fatal issues
        }

    Best-effort by contract: per-file unlink failures are logged at
    WARNING level and recorded in ``files_failed`` rather than raised.
    The DB step runs inside a single transaction so a partial-DB
    failure rolls back to "no rows removed" — half-deleted ledger
    state is worse than no deletion at all. Any DB-level exception is
    caught + recorded in ``warnings`` so the caller still has a chance
    to unlink the YAML and at least make the bot disappear from the
    registry.
    """
    summary: dict = {
        "files_removed": 0,
        "files_failed": [],
        "db_rows_removed": {},
        "warnings": [],
    }

    # ── Step 1: DB rows (single transaction) ────────────────────────
    # Lazy import keeps core.paths free of a hard dep on core.database
    # at module-load — ``paths`` is intentionally lightweight so
    # tools (e.g. backup.sh's restore-side python helper) can pull
    # it in without spinning up sqlite.
    try:
        from core.database import get_db
        conn = get_db()
        with conn:  # transactional — rolls back on any exception
            # orders → deals: drop child rows first via a subselect
            # so foreign-key checks stay happy under PRAGMA
            # foreign_keys=ON. Then drop the deals themselves, then
            # the bot-scoped sibling tables.
            cur = conn.execute(
                "DELETE FROM orders WHERE deal_id IN ("
                "  SELECT id FROM deals "
                "  WHERE user_id = ? AND bot_slug = ?"
                ")",
                (user_id, slug),
            )
            summary["db_rows_removed"]["orders"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM deals WHERE user_id = ? AND bot_slug = ?",
                (user_id, slug),
            )
            summary["db_rows_removed"]["deals"] = cur.rowcount
            for table in _BOT_SCOPED_TABLES:
                cur = conn.execute(
                    f"DELETE FROM {table} "
                    "WHERE user_id = ? AND bot_slug = ?",
                    (user_id, slug),
                )
                summary["db_rows_removed"][table] = cur.rowcount
    except Exception as e:
        logger.warning(
            "purge_bot: DB step failed for user=%d slug=%s: %s",
            user_id, slug, e,
        )
        summary["warnings"].append(f"db purge failed: {type(e).__name__}: {e}")

    # ── Step 2: filesystem ──────────────────────────────────────────
    # Build the candidate list eagerly so the unlink loop is a single
    # pass over a flat collection. Direct paths use the existing
    # helpers for consistency; glob-derived paths cover rotated logs
    # + sentinel files that don't have stable filenames.
    logs_dir = user_logs_dir(user_id)

    candidates: list[Path] = [
        bot_state_path(user_id, slug),
        bot_state_lock_path(user_id, slug),
        bot_manual_trigger_path(user_id, slug),
        bot_pid_path(user_id, slug),
        user_ml_results_path(user_id, slug),
    ]
    # Rotated logs: <slug>.log, <slug>.log.1, <slug>.log.2 …  The
    # ``*`` glob covers both the active file and every rotation.
    candidates.extend(logs_dir.glob(f"{slug}.log*"))
    # Deal-action sentinels — see paper_engine._check_deal_sentinels.
    for action in ("edit", "close", "cancel"):
        candidates.extend(logs_dir.glob(f"{slug}.deal_{action}_*"))

    # De-dupe in case a glob returns the same path the direct helper
    # already added (e.g. the active ``<slug>.log`` matches the
    # ``<slug>.log*`` glob). ``dict.fromkeys`` preserves order while
    # collapsing duplicates — easier to reason about under test than
    # ``set(list)`` which would shuffle.
    seen: dict[Path, None] = {}
    for p in candidates:
        seen.setdefault(p, None)

    for path in seen:
        try:
            if path.exists():
                path.unlink()
                summary["files_removed"] += 1
        except OSError as e:
            logger.warning(
                "purge_bot: could not unlink %s: %s", path, e,
            )
            summary["files_failed"].append(str(path))

    return summary
