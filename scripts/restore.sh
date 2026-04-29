#!/bin/bash
# Reverto restore-from-backup — audit r1-022.
#
# Restores the database + credentials from a specified backup
# directory. REQUIRES the portal to be stopped so the restored
# DB isn't clobbered by an in-flight WAL commit.
#
# Usage:
#   ./scripts/restore.sh <backup-dir>
#   make restore BACKUP=backups/2026-04-24-030000
#
# Safety:
#   - Refuses to run if portal PID file reports a live process
#   - Creates a pre-restore snapshot of current state BEFORE
#     overwriting anything (so an accidental restore can itself
#     be reversed)
#   - Requires explicit 'y' confirmation

set -euo pipefail

cd "$(dirname "$0")/.."

# ──────────────────────────────────────────────────────────────
# Parse + validate arguments
# ──────────────────────────────────────────────────────────────

if [ -z "${1:-}" ]; then
    echo "Usage: $0 <backup-directory>"
    echo ""
    echo "Available backups:"
    # List only the auto-named date dirs, not pre-restore snapshots.
    if [ -d "backups" ]; then
        ls -1 backups/ 2>/dev/null \
            | grep -E "^20[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{6}$" \
            | sort -r | head -20 | sed 's/^/  backups\//'
    else
        echo "  (backups/ does not exist — run make backup first)"
    fi
    exit 1
fi

BACKUP_DIR="$1"

if [ ! -d "${BACKUP_DIR}" ]; then
    echo "ERROR: backup directory not found: ${BACKUP_DIR}" >&2
    exit 1
fi

if [ ! -f "${BACKUP_DIR}/reverto.db" ]; then
    echo "ERROR: no reverto.db in ${BACKUP_DIR} — invalid backup" >&2
    exit 1
fi

# ──────────────────────────────────────────────────────────────
# Audit r2-011: schema-version compatibility check.
#
# The MANIFEST stamps "Schema version: <N>" at backup time
# (backup.sh L158, audit r3-008). On restore, refuse if the
# backup was taken from a NEWER code-version than this checkout
# expects — schema migrations are additive but not all of them
# are idempotent under downgrade, so silently restoring a
# forward-version backup risks leaving the DB in a state the
# running code does not understand.
#
# Older backups (taken by code that pre-dates the stamp) carry
# no schema-version line; in that case we warn but do not block.
# Operators have explicit confirmation prompt below to gate the
# restore regardless.
# ──────────────────────────────────────────────────────────────

CURRENT_SCHEMA_VERSION="unknown"
_RESTORE_PY=".venv/bin/python3"
if [ ! -x "${_RESTORE_PY}" ]; then
    _RESTORE_PY=$(command -v python3 || true)
fi
if [ -n "${_RESTORE_PY}" ] && [ -x "${_RESTORE_PY}" ]; then
    CURRENT_SCHEMA_VERSION=$("${_RESTORE_PY}" -c \
        "import sys; sys.path.insert(0, '.'); from core.database import SCHEMA_VERSION; print(SCHEMA_VERSION)" \
        2>/dev/null || echo "unknown")
fi

BACKUP_SCHEMA_VERSION="unknown"
if [ -f "${BACKUP_DIR}/MANIFEST.txt" ]; then
    BACKUP_SCHEMA_VERSION=$(awk -F': ' \
        '/^Schema version:/ {print $2; exit}' \
        "${BACKUP_DIR}/MANIFEST.txt" 2>/dev/null || echo "unknown")
fi

if [ "${BACKUP_SCHEMA_VERSION}" = "unknown" ]; then
    echo "WARNING: backup MANIFEST has no schema-version stamp."
    echo "         (Pre-r3-008 backup; compatibility cannot be verified."
    echo "         Proceeding under operator confirmation below.)"
elif [ "${CURRENT_SCHEMA_VERSION}" = "unknown" ]; then
    echo "WARNING: cannot read current schema version (no python3?)."
    echo "         Skipping compatibility check; operator confirmation"
    echo "         below is the only gate."
elif [ "${BACKUP_SCHEMA_VERSION}" -gt "${CURRENT_SCHEMA_VERSION}" ] 2>/dev/null; then
    echo "ERROR: backup schema version ${BACKUP_SCHEMA_VERSION} is" >&2
    echo "       newer than the current code expects" >&2
    echo "       (${CURRENT_SCHEMA_VERSION}). Refusing to restore." >&2
    echo "" >&2
    echo "       Roll forward the code (git pull) to a checkout that" >&2
    echo "       knows schema v${BACKUP_SCHEMA_VERSION} before" >&2
    echo "       restoring this backup." >&2
    exit 1
elif [ "${BACKUP_SCHEMA_VERSION}" -lt "${CURRENT_SCHEMA_VERSION}" ] 2>/dev/null; then
    echo "NOTE: backup schema version ${BACKUP_SCHEMA_VERSION} is older"
    echo "      than the current code (${CURRENT_SCHEMA_VERSION})."
    echo "      Restore will succeed; init_db() will run any additive"
    echo "      migrations between v${BACKUP_SCHEMA_VERSION} and"
    echo "      v${CURRENT_SCHEMA_VERSION} on the next make start."
fi

# ──────────────────────────────────────────────────────────────
# Safety: portal must be stopped. If the PID file names a live
# process, refuse — restoring under a live portal would let the
# engine commit a WAL frame on top of the restored DB and end
# up with a mixed-state file.
# ──────────────────────────────────────────────────────────────

PID_FILE="logs/pids/portal.pid"
if [ -f "${PID_FILE}" ]; then
    PID=$(cat "${PID_FILE}" 2>/dev/null || echo "")
    if [ -n "${PID}" ] && kill -0 "${PID}" 2>/dev/null; then
        echo "ERROR: portal is still running (PID ${PID})" >&2
        echo "Stop it first: make stop" >&2
        exit 1
    fi
fi

# ──────────────────────────────────────────────────────────────
# Show restore plan
# ──────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "RESTORE PLAN"
echo "═══════════════════════════════════════════════════════════"
echo "Source:  ${BACKUP_DIR}"

if [ -f "${BACKUP_DIR}/MANIFEST.txt" ]; then
    echo ""
    echo "Manifest:"
    sed 's/^/  /' "${BACKUP_DIR}/MANIFEST.txt"
fi

echo ""
echo "Operations:"
echo "  1. Snapshot current state to backups/pre-restore-<ts>"
echo "  2. Restore logs/reverto.db from backup"
echo "  3. Restore credentials/ from backup (if present)"
echo "  4. Restore keys/ from backup (if present)"
echo "  5. Restore logs/.credentials.key / .auth.json (legacy)"
echo ""
echo "After restore you should:"
echo "  - make start"
echo "  - Verify via browser"
echo "  - Check logs/portal.log"
echo "═══════════════════════════════════════════════════════════"
echo ""
read -r -p "Proceed? [y/N] " REPLY
echo ""

if [[ ! "${REPLY}" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# ──────────────────────────────────────────────────────────────
# Pre-restore snapshot — lets the operator reverse an
# accidental restore. Lives outside the regular retention
# window (the prefix 'pre-restore-' keeps it out of the
# date-pattern glob in backup.sh's prune step).
# ──────────────────────────────────────────────────────────────

PRE_RESTORE_DIR="backups/pre-restore-$(date -u +"%Y-%m-%d-%H%M%S")"
mkdir -p "${PRE_RESTORE_DIR}"

echo "→ Snapshotting current state to ${PRE_RESTORE_DIR}..."

# Online backup via sqlite3 CLI or Python stdlib (same API).
_sqlite_backup() {
    local src="$1"
    local dst="$2"
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "${src}" ".backup '${dst}'"
    else
        local py=".venv/bin/python3"
        [ ! -x "${py}" ] && py=$(command -v python3)
        "${py}" -c "
import sqlite3, sys
s = sqlite3.connect(sys.argv[1])
d = sqlite3.connect(sys.argv[2])
with d:
    s.backup(d)
s.close(); d.close()
" "${src}" "${dst}"
    fi
}

if [ -f "logs/reverto.db" ]; then
    _sqlite_backup "logs/reverto.db" "${PRE_RESTORE_DIR}/reverto.db"
fi

if [ -d "credentials" ]; then
    cp -r credentials "${PRE_RESTORE_DIR}/credentials"
fi

if [ -d "keys" ]; then
    cp -r keys "${PRE_RESTORE_DIR}/keys"
fi

if [ -f "logs/.credentials.key" ]; then
    cp "logs/.credentials.key" "${PRE_RESTORE_DIR}/.credentials.key"
fi

if [ -f "logs/.auth.json" ]; then
    cp "logs/.auth.json" "${PRE_RESTORE_DIR}/.auth.json"
fi

chmod -R go-rwx "${PRE_RESTORE_DIR}" 2>/dev/null || true

# ──────────────────────────────────────────────────────────────
# Restore from backup
# ──────────────────────────────────────────────────────────────

echo "→ Restoring database..."
cp "${BACKUP_DIR}/reverto.db" "logs/reverto.db"
# Stale WAL + SHM sidecars would confuse SQLite; let the next
# portal startup recreate them.
rm -f "logs/reverto.db-wal" "logs/reverto.db-shm"

if [ -d "${BACKUP_DIR}/credentials" ]; then
    echo "→ Restoring credentials/..."
    rm -rf credentials
    cp -r "${BACKUP_DIR}/credentials" credentials
fi

if [ -d "${BACKUP_DIR}/keys" ]; then
    echo "→ Restoring keys/..."
    rm -rf keys
    cp -r "${BACKUP_DIR}/keys" keys
fi

if [ -f "${BACKUP_DIR}/.credentials.key" ]; then
    echo "→ Restoring logs/.credentials.key (legacy)..."
    cp "${BACKUP_DIR}/.credentials.key" "logs/.credentials.key"
fi

if [ -f "${BACKUP_DIR}/.auth.json" ]; then
    echo "→ Restoring logs/.auth.json (legacy)..."
    cp "${BACKUP_DIR}/.auth.json" "logs/.auth.json"
fi

# Permissions — 600 on sensitive files, 700 on dirs. Best-
# effort: restore on a platform without chmod perms support
# shouldn't break the restore itself.
chmod 600 logs/reverto.db 2>/dev/null || true
chmod 600 logs/.credentials.key 2>/dev/null || true
chmod 600 logs/.auth.json 2>/dev/null || true
chmod -R go-rwx credentials 2>/dev/null || true
chmod -R go-rwx keys 2>/dev/null || true

echo ""
echo "✅ Restore complete."
echo ""
echo "Pre-restore snapshot saved at: ${PRE_RESTORE_DIR}"
echo "Start portal: make start"
echo ""
echo "To reverse this restore:"
echo "  ./scripts/restore.sh ${PRE_RESTORE_DIR}"
