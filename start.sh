#!/bin/bash
# start.sh — Start the Reverto web portal
# Start bots via the portal or with:
#   python3 main_paper.py --config config/bots/xxx.yaml

cd "$(dirname "$0")"

# Load .env if present — env-vars for REVERTO_API_KEY /
# REVERTO_SECRET_KEY / REVERTO_INSECURE_COOKIES / exchange
# credentials live here. `make(1)` spawns /bin/sh which does not
# read .bashrc, so .env is the single source of truth for portal
# environment across dev and production.
#
# `set -a` auto-exports every variable assigned while the flag is
# active, so the values reach the Python subprocess below instead
# of only living in this shell. Flipped off after the source to
# avoid accidentally exporting anything subsequent shell code
# happens to assign.
if [ -f .env ]; then
    set -a
    # shellcheck source=/dev/null
    . ./.env
    set +a
fi

# REVERTO_API_KEY: without it, the portal generates a fresh
# ephemeral key on every restart → every existing client (browser
# session, script) loses access until the new key is redistributed.
# REVERTO_SECRET_KEY: signs session cookies — without it, every
# restart invalidates every open session.
# REVERTO_INSECURE_COOKIES=1: drops the Secure-flag on session
# cookies so localhost development over plain HTTP works. DO NOT
# set in production behind a TLS reverse proxy.
# Generate the key values with:
#   python3 -c 'import secrets; print(secrets.token_hex(32))'
# Drop them + exchange credentials into .env (see .env.example
# for the template).

PORTAL_PID_FILE="logs/pids/portal.pid"
mkdir -p logs/pids

# Check if the portal is already running
if [ -f "$PORTAL_PID_FILE" ]; then
    PID=$(cat "$PORTAL_PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "✅ Portal already running (PID $PID) — http://localhost:8080"
        exit 0
    else
        # Stale PID file — remove it
        rm -f "$PORTAL_PID_FILE"
    fi
fi

# Start the portal in the background
# Note: main_web.py writes the PID file itself via atexit. We do
# NOT write $! here anymore — that produced a race condition where
# the PID of the nohup shell process was stored instead of Python's.
#
# stdout/stderr go to portal.boot.log (NOT portal.log). Python's
# RotatingFileHandler in main_web.py writes to portal.log itself —
# if the shell also `>> portal.log` tee's it we'd get every line
# twice, because Python's StreamHandler also writes to stderr and
# that redirection would land in portal.log. portal.boot.log only
# captures pre-logger crashes (Python import errors etc.).
echo "🚀 Starting Reverto portal..."
nohup .venv/bin/python3 main_web.py >> logs/portal.boot.log 2>&1 &

# Wait until main_web.py has written its own PID file
for i in $(seq 1 10); do
    sleep 0.5
    if [ -f "$PORTAL_PID_FILE" ]; then
        PID=$(cat "$PORTAL_PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "✅ Portal started (PID $PID) — http://localhost:8080"
            exit 0
        fi
    fi
done

echo "❌ Portal failed to start — check logs/portal.log"
exit 1
