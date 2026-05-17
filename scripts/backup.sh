#!/bin/bash
# Reverto daily backup — audit r1-022.
#
# Creates a timestamped backup under backups/<YYYY-MM-DD-HHMMSS>/
# containing:
#   - logs/reverto.db                (via sqlite3 .backup — safe
#                                     under concurrent writes)
#   - credentials/                   (per-user .enc files + .key)
#   - logs/.credentials.key          (legacy master Fernet key,
#                                     if still present)
#   - logs/.auth.json                (pre-Phase-3a auth-state,
#                                     if still present)
#   - logs/<uid>/<slug>.state.json   (per-bot runtime state — added
#                                     PT-v4-EI-004; without these
#                                     a restore loses every running
#                                     deal's PnL/balance/peak/queue)
#   - config/bots/<uid>/<slug>.yaml  (per-bot configs — added
#                                     PT-v4-EI-004; without these a
#                                     restored DB references YAMLs
#                                     that don't exist on disk)
#
# Retention:
#   - 7 days of daily backups
#   - 4 weeks of weekly backups   (Sundays)
#   - 3 months of monthly backups (1st of month)
# Older entries are pruned on each subsequent run.
#
# Designed for cron (replace /path/to/reverto with your actual
# installation directory):
#   0 3 * * * cd /path/to/reverto && ./scripts/backup.sh >> \
#       logs/backup.log 2>&1
# Or manual:
#   make backup
#
# On failure: exits non-zero and writes a stamp to
# backups/.last_error so the operator can spot it without
# trawling the full log.

set -euo pipefail

cd "$(dirname "$0")/.."

# PT-v4-EI-003: concurrent-execution guard. -xn = take an
# exclusive lock; if another backup.sh already holds it, exit 1
# immediately rather than racing on credentials/ + the dated
# backup dir. fd 200 stays open for the script's whole lifetime,
# so the lock is auto-released on any exit (normal, error, or
# signal). flock creates the lock file on first run; no setup
# needed. Path is overridable via REVERTO_BACKUP_LOCK so the
# regression test can isolate it (production default unchanged).
# Falls back to a repo-local lock file if the chosen path is not
# writable (e.g. a non-root dev environment without /var/lock).
_LOCK_FILE="${REVERTO_BACKUP_LOCK:-/var/lock/reverto-backup.lock}"
if ! { exec 200>"${_LOCK_FILE}"; } 2>/dev/null; then
    _LOCK_FILE="backups/.backup.lock"
    mkdir -p backups
    exec 200>"${_LOCK_FILE}"
fi
flock -xn 200 || {
    echo "$(date -u +%FT%TZ): backup.sh already running (lock ${_LOCK_FILE}); exiting" >&2
    exit 1
}

BACKUP_ROOT="backups"

# PT-v4-EI-002: monitoring contract. Previously only the
# DB-missing branch stamped .last_error; every other failure
# (sqlite3 .backup, cp, MANIFEST write, retention prune,
# interruption) exited silently under set -euo pipefail, so
# cron-side mtime monitoring missed almost every real failure.
# This EXIT trap stamps backups/.last_error with a UTC timestamp
# + exit code on ANY non-zero exit (incl. SIGINT=130, SIGTERM=143
# - an interrupted backup MUST be flagged for a cron-driven job).
# $LINENO is intentionally NOT used: inside an EXIT trap it does
# not reliably point at the failing line, so it would mislead;
# the exit code is the actionable signal. The success path still
# explicitly rm -fs .last_error after a clean completion. The
# pre-existing DB-missing branch keeps its own echo; the trap
# then re-stamps on exit (more recent stamp wins) - harmless.
mkdir -p "${BACKUP_ROOT}"
trap '_rc=$?; if [ "${_rc}" -ne 0 ]; then echo "$(date -u +%FT%TZ): backup.sh exited ${_rc}" > "${BACKUP_ROOT}/.last_error"; fi' EXIT

TIMESTAMP=$(date -u +"%Y-%m-%d-%H%M%S")
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"

# Retention in days.
RETAIN_DAILY=7
RETAIN_WEEKLY=28   # 4 weeks
RETAIN_MONTHLY=90  # ~3 months

mkdir -p "${BACKUP_DIR}"

# ──────────────────────────────────────────────────────────────
# Database backup via SQLite online-backup API. Safe while
# Reverto is running — .backup coordinates with the WAL so no
# corrupt snapshot on concurrent writes.
# ──────────────────────────────────────────────────────────────

DB_PATH="logs/reverto.db"

if [ ! -f "${DB_PATH}" ]; then
    echo "ERROR: ${DB_PATH} not found" >&2
    echo "$(date -u +%FT%TZ): missing database" > "${BACKUP_ROOT}/.last_error"
    exit 1
fi

# Online backup via sqlite3 CLI when available, else fall back
# to Python's stdlib sqlite3 module. Both use the same online-
# backup API (https://www.sqlite.org/backup.html) so the
# result is identical — WAL-aware, concurrent-write-safe.
BACKUP_DB="${BACKUP_DIR}/reverto.db"
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "${DB_PATH}" ".backup '${BACKUP_DB}'"
else
    # Prefer the project's venv Python so the module lookup
    # matches what the portal uses at runtime.
    PY=".venv/bin/python3"
    if [ ! -x "${PY}" ]; then
        PY=$(command -v python3)
    fi
    "${PY}" -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
src.close(); dst.close()
" "${DB_PATH}" "${BACKUP_DB}"
fi

# ──────────────────────────────────────────────────────────────
# Static-file copies. credentials/ is recursive; the loose
# keys + .auth.json are best-effort (missing in Phase-3a+).
# ──────────────────────────────────────────────────────────────

# PT-v4-EI-001: credentials/ and keys/ are mutated AS A SET by
# the per-user Fernet rotation in core/credentials.py
# (rotate_user_key re-encrypts every credentials/<uid>/*.enc,
# then os.replace's keys/<uid>.key). A naive `cp -r` running
# mid-rotation captures a mix of old-key and new-key ciphertext
# that no single key can decrypt.
#
# The finding's literal suggested fix ("flock matching the lock
# the rotation flow already takes") is not implementable here:
# the rotation does NOT lock credentials/<uid>/ — it takes an
# fcntl advisory lock on an EPHEMERAL per-user sentinel
# keys/<uid>.key.lock (created on entry via touch, unlinked on
# exit) using LOCK_EX|LOCK_NB. There is no single stable lock
# path covering the whole tree, and core/credentials.py is out
# of scope for this PR, so a bash flock cannot share the
# rotation's mutex. Worse, flock-ing the rotation's own
# LOCK_NB sentinel from here would make a concurrent rotation
# fail outright ("Another Fernet rotation is already running"),
# and the unlink-on-exit makes it inode-racy anyway. A
# unilateral flock on a new path would be pure false safety.
#
# Implemented instead (the finding's own alternative, made
# rotation-aware): (a) wait, bounded, for any in-progress
# rotation to release its sentinel — rotation completes in well
# under a second for a hobbyist key set; (b) stage the copy in
# a .tmp dir then atomically rename, so a restore never sees a
# partially-copied tree. Residual window (a rotation starting
# mid-copy) is acceptable for a LOW finding: cron runs at 03:00
# and rotation is rare + operator-initiated. See the PR notes
# for the full rationale on the path-dynamic deviation.
_wait_for_rotation_idle() {
    local _waited=0
    local _max=30
    while compgen -G "keys/*.key.lock" > /dev/null 2>&1; do
        if [ "${_waited}" -ge "${_max}" ]; then
            echo "WARNING: Fernet rotation sentinel still present after ${_max}s; proceeding with credentials/keys copy anyway" >&2
            break
        fi
        sleep 1
        _waited=$((_waited + 1))
    done
}

_stage_copy() {
    # _stage_copy <src-dir> <final-dest>: copy <src-dir> into a
    # sibling .tmp of <final-dest> then atomically rename, so an
    # interrupted copy never leaves a half-tree at <final-dest>.
    local _src="$1"
    local _dest="$2"
    local _tmp="${_dest}.tmp"
    rm -rf "${_tmp}"
    cp -r "${_src}" "${_tmp}"
    mv "${_tmp}" "${_dest}"
}

if [ -d "credentials" ] || [ -d "keys" ]; then
    _wait_for_rotation_idle
fi

if [ -d "credentials" ]; then
    _stage_copy credentials "${BACKUP_DIR}/credentials"
fi

# Per-user Fernet keys live under keys/<user_id>.key today
# (Phase-3a rename from logs/.credentials.key). Include both
# paths so a backup restored onto an older codebase still
# works - and a current restore finds its keys regardless.
if [ -d "keys" ]; then
    _stage_copy keys "${BACKUP_DIR}/keys"
fi

if [ -f "logs/.credentials.key" ]; then
    cp "logs/.credentials.key" "${BACKUP_DIR}/.credentials.key"
fi

if [ -f "logs/.auth.json" ]; then
    cp "logs/.auth.json" "${BACKUP_DIR}/.auth.json"
fi

# ──────────────────────────────────────────────────────────────
# Per-bot runtime state + configs (PT-v4-EI-004). state.json
# carries the engine's resume-able view of each open deal +
# balance + drawdown peak + closed-deals tail. The bot YAMLs
# under config/bots/<uid>/ are the source-of-truth strategy
# config that the engine reads on start. Restoring the DB
# without these leaves each bot referencing a YAML that's gone
# and resumes its in-memory state from defaults — not what the
# operator expects.
#
# State files: logs/<user_id>/<slug>.state.json. Use ``cp
# --parents`` so the user_id/ directory structure is preserved
# inside the backup (logs/1/foo.state.json → BACKUP_DIR/logs/1/
# foo.state.json) — restore.sh's reverse-copy then lands the
# files at their original paths without extra logic.
# ──────────────────────────────────────────────────────────────

if [ -d "logs" ]; then
    # State files live one level deep under logs/<uid>/. The
    # ``-quit`` short-circuits when no matches exist, so the
    # subsequent ``find ... -exec`` skips the cp invocation
    # entirely on a fresh install with no bots yet.
    if find logs -mindepth 2 -maxdepth 2 \
            -type f -name '*.state.json' -print -quit \
            | grep -q . 2>/dev/null; then
        find logs -mindepth 2 -maxdepth 2 \
            -type f -name '*.state.json' \
            -exec cp --parents {} "${BACKUP_DIR}/" \;
    fi
fi

# Per-user bot YAMLs. ``cp -r`` preserves config/bots/<uid>/<slug>.yaml
# layout. Skip silently when the directory is missing (fresh
# install, or operator who hasn't created any bots yet).
if [ -d "config/bots" ]; then
    mkdir -p "${BACKUP_DIR}/config"
    cp -r config/bots "${BACKUP_DIR}/config/bots"
fi

# ──────────────────────────────────────────────────────────────
# Permissions: sensitive files/dirs → 600/700. Operator-read
# only so a log-shipping pipeline can't accidentally tar the
# backup tree as world-readable.
# ──────────────────────────────────────────────────────────────

find "${BACKUP_DIR}" -type f -exec chmod 600 {} \;
find "${BACKUP_DIR}" -type d -exec chmod 700 {} \;

# ──────────────────────────────────────────────────────────────
# Manifest — small text file recording what landed in the
# backup + the source host + git HEAD. Restore reads this to
# show the operator what they're about to apply.
# ──────────────────────────────────────────────────────────────

MANIFEST="${BACKUP_DIR}/MANIFEST.txt"

# Audit r3-008: stamp the SQLite schema version into the manifest.
# Mirrors the CLI + Python-stdlib fallback pattern used for the DB
# .backup itself (lines 64-81 above) so the manifest stays populated
# regardless of which code-path produced the snapshot. Probes the
# BACKUP_DB (just-copied snapshot) rather than the source DB so the
# manifest reflects exactly what landed in the backup. Falls back to
# "unknown" if both paths fail — the manifest must never error out
# and abort the backup itself.
SCHEMA_VERSION_VALUE="unknown"
if command -v sqlite3 >/dev/null 2>&1; then
    SCHEMA_VERSION_VALUE=$(sqlite3 "${BACKUP_DB}" 'PRAGMA user_version;' 2>/dev/null || echo "unknown")
else
    # Same Python resolution as the DB-backup fallback above: prefer
    # the project's venv, then the system python3.
    _SCHEMA_PY=".venv/bin/python3"
    if [ ! -x "${_SCHEMA_PY}" ]; then
        _SCHEMA_PY=$(command -v python3 || true)
    fi
    if [ -n "${_SCHEMA_PY}" ] && [ -x "${_SCHEMA_PY}" ]; then
        SCHEMA_VERSION_VALUE=$("${_SCHEMA_PY}" -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute('PRAGMA user_version').fetchone()[0])
c.close()
" "${BACKUP_DB}" 2>/dev/null || echo "unknown")
    fi
fi

{
    echo "Reverto backup"
    echo "Created: ${TIMESTAMP} UTC"
    echo "Host: $(hostname)"
    echo "Git HEAD: $(git rev-parse HEAD 2>/dev/null || echo 'unknown')"
    echo "Schema version: ${SCHEMA_VERSION_VALUE}"
    echo "Files:"
    find "${BACKUP_DIR}" -type f -not -name MANIFEST.txt \
        -printf "  %p (%s bytes)\n" | sort
} > "${MANIFEST}"
chmod 600 "${MANIFEST}"

# ──────────────────────────────────────────────────────────────
# Retention — prune older backups while preserving weekly
# (Sunday) and monthly (1st of month) snapshots.
# ──────────────────────────────────────────────────────────────

NOW_EPOCH=$(date -u +%s)

# Directory name pattern: ^20\d\d-\d\d-\d\d-\d{6}$
# Using a find+while loop so spaces in paths can't break us.
find "${BACKUP_ROOT}" -maxdepth 1 -type d -name "20*-*" -print0 \
    | while IFS= read -r -d '' dir; do
    basename=$(basename "${dir}")
    date_str=$(echo "${basename}" | cut -d'-' -f1-3)

    # Skip anything that doesn't parse as a date.
    dir_epoch=$(date -d "${date_str}" +%s 2>/dev/null || echo "")
    if [ -z "${dir_epoch}" ]; then
        continue
    fi

    age_days=$(( (NOW_EPOCH - dir_epoch) / 86400 ))

    # Fresh enough to keep under daily retention.
    if [ "${age_days}" -le "${RETAIN_DAILY}" ]; then
        continue
    fi

    dom=$(echo "${date_str}" | cut -d'-' -f3)
    # %u — day-of-week, Monday=1..Sunday=7.
    dow=$(date -d "${date_str}" +%u 2>/dev/null || echo "0")

    # Monthly (1st of month) — keep up to RETAIN_MONTHLY days.
    if [ "${dom}" = "01" ] && [ "${age_days}" -le "${RETAIN_MONTHLY}" ]; then
        continue
    fi

    # Weekly (Sunday) — keep up to RETAIN_WEEKLY days.
    if [ "${dow}" = "7" ] && [ "${age_days}" -le "${RETAIN_WEEKLY}" ]; then
        continue
    fi

    rm -rf "${dir}"
    echo "Pruned: ${dir}"
done

# ──────────────────────────────────────────────────────────────
# Success — clear stale error stamp + print a summary line so
# the cron log is readable.
# ──────────────────────────────────────────────────────────────

rm -f "${BACKUP_ROOT}/.last_error"

SIZE=$(du -sh "${BACKUP_DIR}" | cut -f1)
echo "Backup complete: ${BACKUP_DIR} (${SIZE})"
