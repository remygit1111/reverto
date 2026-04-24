#!/bin/bash
# Reverto rollback script — audit r1-038.
#
# Reverts HEAD on the production server by N commits (default 1),
# or to a specific commit SHA, then restarts the portal. Guards
# against rolling back schema-migration commits without operator
# confirmation — those are forward-only in Reverto and need a DB
# restore, not a naïve code reset.
#
# Usage:
#   ./scripts/rollback.sh              # Rollback 1 commit
#   ./scripts/rollback.sh 2            # Rollback 2 commits
#   ./scripts/rollback.sh --to <sha>   # Rollback to a specific commit
#
# Must be run from the reverto project root on the production
# machine (SSH to the server first). See docs/runbook.md section
# "Rollback procedure" for the full flow, safety notes, and
# reverse-a-rollback steps.

set -euo pipefail

cd "$(dirname "$0")/.."

# ──────────────────────────────────────────────────────────────
# Parse arguments
# ──────────────────────────────────────────────────────────────

ROLLBACK_COUNT=1
TARGET_SHA=""

if [ "${1:-}" = "--to" ]; then
    if [ -z "${2:-}" ]; then
        echo "Error: --to requires a commit SHA argument" >&2
        exit 1
    fi
    TARGET_SHA="$2"
elif [ -n "${1:-}" ]; then
    if [[ "$1" =~ ^[0-9]+$ ]]; then
        ROLLBACK_COUNT="$1"
    else
        echo "Error: invalid argument '$1'. Use a number or --to <sha>" >&2
        exit 1
    fi
fi

# ──────────────────────────────────────────────────────────────
# Gather rollback info
# ──────────────────────────────────────────────────────────────

CURRENT_SHA=$(git rev-parse HEAD)
CURRENT_MSG=$(git log -1 --format="%s")

if [ -n "$TARGET_SHA" ]; then
    if ! TARGET=$(git rev-parse --verify "${TARGET_SHA}^{commit}" 2>/dev/null); then
        echo "Error: commit '$TARGET_SHA' not found" >&2
        exit 1
    fi
else
    if ! TARGET=$(git rev-parse --verify "HEAD~${ROLLBACK_COUNT}^{commit}" 2>/dev/null); then
        echo "Error: cannot rollback ${ROLLBACK_COUNT} commits — not enough history" >&2
        exit 1
    fi
fi

TARGET_MSG=$(git log -1 --format="%s" "$TARGET")

if [ "$CURRENT_SHA" = "$TARGET" ]; then
    echo "Already at target commit $TARGET. Nothing to do."
    exit 0
fi

# ──────────────────────────────────────────────────────────────
# Schema migration safety check
# ──────────────────────────────────────────────────────────────

# Check if any commits being rolled back touched core/database.py —
# those are forward-only migrations that can't be reversed with a
# naïve code reset.
MIGRATION_COMMITS=$(
    git log --oneline "${TARGET}..HEAD" -- core/database.py 2>/dev/null || true
)

if [ -n "$MIGRATION_COMMITS" ]; then
    echo ""
    echo "⚠️  WARNING — rolling back commits that touched core/database.py:"
    echo ""
    echo "$MIGRATION_COMMITS" | sed 's/^/    /'
    echo ""
    echo "Schema migrations are forward-only. If any commit above"
    echo "bumped SCHEMA_VERSION or altered tables, rolling back the"
    echo "code WITHOUT downgrading the DB will leave you in an"
    echo "inconsistent state."
    echo ""
    echo "Safer options:"
    echo "  1. Restore DB from backup first, then run this rollback"
    echo "  2. Write a manual SQL downgrade, then run rollback"
    echo "  3. Fix forward (new commit that reverts behaviour) instead"
    echo ""
    echo "See docs/runbook.md section 'Rollback procedure' for the"
    echo "restore-from-backup flow."
    echo ""
    read -r -p "Proceed anyway? [y/N] " REPLY
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ──────────────────────────────────────────────────────────────
# Confirmation prompt
# ──────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "ROLLBACK PLAN"
echo "═══════════════════════════════════════════════════════════"
echo "Current HEAD:  $CURRENT_SHA"
echo "Current msg:   $CURRENT_MSG"
echo ""
echo "Rollback to:   $TARGET"
echo "Target msg:    $TARGET_MSG"
echo ""
echo "Operations:"
echo "  1. git reset --hard $TARGET"
echo "  2. make restart"
echo "═══════════════════════════════════════════════════════════"
echo ""
read -r -p "Proceed? [y/N] " REPLY

if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# ──────────────────────────────────────────────────────────────
# Execute rollback
# ──────────────────────────────────────────────────────────────

echo ""
echo "→ Resetting git to $TARGET..."
git reset --hard "$TARGET"

echo ""
echo "→ Restarting portal..."
make restart

echo ""
echo "✅ Rollback complete. Portal at $TARGET."
echo ""
echo "Next steps:"
echo "  - Verify portal health in browser"
echo "  - Check logs: tail -30 logs/portal.log"
echo "  - Confirm bots still running: ps aux | grep main_paper"
echo "  - If issues persist: investigate or roll back further"
echo "  - Plan forward-fix: new commit that addresses the issue"
echo ""
echo "To reverse this rollback: git reflog + git reset --hard <prev-sha>"
