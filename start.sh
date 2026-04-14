#!/bin/bash
# start.sh — Start het Reverto web portal
# De bots start je via het portal of met:
#   python3 main_paper.py --config config/bots/xxx.yaml

cd "$(dirname "$0")"

# IMPORTANT: voor live/productie gebruik MOET REVERTO_API_KEY gezet zijn,
# anders genereert de portal bij elke restart een nieuwe ephemerale key
# en kan geen enkele client (browser/script) bij de control endpoints.
# Voorbeeld:
#   export REVERTO_API_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
#
# REVERTO_SECRET_KEY tekent de sessie-cookies. Zonder deze worden alle
# bestaande sessies ongeldig bij elke restart.
#   export REVERTO_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
#
# Voor localhost development (http://, geen TLS) moet de Secure-cookie
# vlag uit, anders dropt de browser de sessie-cookie stilletjes:
#   export REVERTO_INSECURE_COOKIES=1
# Voor productie achter een TLS reverse proxy: NIET zetten.

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
