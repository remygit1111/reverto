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
#
# Retention:
#   - 7 days of daily backups
#   - 4 weeks of weekly backups   (Sundays)
#   - 3 months of monthly backups (1st of month)
# Older entries are pruned on each subsequent run.
#
# Designed for cron:
#   0 3 * * * cd /home/bot/reverto && ./scripts/backup.sh >> \
#       logs/backup.log 2>&1
# Or manual:
#   make backup
#
# On failure: exits non-zero and writes a stamp to
# backups/.last_error so the operator can spot it without
# trawling the full log.

set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_ROOT="backups"
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

if [ -d "credentials" ]; then
    cp -r credentials "${BACKUP_DIR}/credentials"
fi

# Per-user Fernet keys live under keys/<user_id>.key today
# (Phase-3a rename from logs/.credentials.key). Include both
# paths so a backup restored onto an older codebase still
# works — and a current restore finds its keys regardless.
if [ -d "keys" ]; then
    cp -r keys "${BACKUP_DIR}/keys"
fi

if [ -f "logs/.credentials.key" ]; then
    cp "logs/.credentials.key" "${BACKUP_DIR}/.credentials.key"
fi

if [ -f "logs/.auth.json" ]; then
    cp "logs/.auth.json" "${BACKUP_DIR}/.auth.json"
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
{
    echo "Reverto backup"
    echo "Created: ${TIMESTAMP} UTC"
    echo "Host: $(hostname)"
    echo "Git HEAD: $(git rev-parse HEAD 2>/dev/null || echo 'unknown')"
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
