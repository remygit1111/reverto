#!/bin/bash
# start.sh — Start het Reverto web portal
# De bots start je via het portal of met:
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

# Check of portal al draait
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

# Start portal op achtergrond
# Note: het PID bestand wordt door main_web.py zelf geschreven via atexit.
# Wij schrijven hier GEEN $! meer — dat gaf een race condition waarbij
# het PID van het nohup shell-proces werd opgeslagen in plaats van Python.
#
# stdout/stderr gaan naar portal.boot.log (NIET portal.log). Python's
# RotatingFileHandler in main_web.py schrijft zelf naar portal.log —
# als de shell ook >> portal.log tee't krijgen we elke regel dubbel,
# omdat de StreamHandler van Python ook nog naar stderr schrijft en
# die redirection dan in portal.log landt. portal.boot.log vangt
# alleen pre-logger crashes op (Python import-fouten e.d.).
echo "🚀 Starting Reverto portal..."
nohup .venv/bin/python3 main_web.py >> logs/portal.boot.log 2>&1 &

# Wacht tot main_web.py zijn eigen PID bestand heeft geschreven
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
