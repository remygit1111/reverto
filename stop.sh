#!/bin/bash
# stop.sh — Stop het Reverto portal (en optioneel alle bots).
#
# Standaard gedrag: stopt ALLEEN het portal proces. Bot subprocessen
# blijven draaien — dat is wat je wilt bij een portal-restart (make
# restart) zodat je trading posities niet verliest door een UI refresh.
#
# Gebruik `--all` om ook alle bot processen te stoppen. Dat is het
# gedrag dat make stop-all aanroept, typisch voor machine shutdown
# of wanneer je echt alles wil stilleggen.

cd "$(dirname "$0")"

PID_DIR="logs/pids"

stop_pidfile() {
    local pid_file="$1"
    local name="$2"
    [ -f "$pid_file" ] || return 0
    local pid
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        echo "🛑 Stopping $name (PID $pid)..."
        kill "$pid"
        # Wacht tot het proces daadwerkelijk gestopt is (max 5 seconden)
        for _ in $(seq 1 10); do
            sleep 0.5
            kill -0 "$pid" 2>/dev/null || break
        done
        # Forceer stop als proces nog draait na 5 seconden
        if kill -0 "$pid" 2>/dev/null; then
            echo "⚠️  $name did not stop gracefully — sending SIGKILL"
            kill -9 "$pid" 2>/dev/null
        fi
    fi
    rm -f "$pid_file"
}

MODE="portal"
if [ "$1" = "--all" ]; then
    MODE="all"
fi

if [ "$MODE" = "all" ]; then
    # Stop alle bots eerst, dan het portal.
    any_bots=0
    for pid_file in "$PID_DIR"/*.pid; do
        [ -f "$pid_file" ] || continue
        name=$(basename "$pid_file" .pid)
        [ "$name" = "portal" ] && continue
        stop_pidfile "$pid_file" "$name"
        any_bots=1
    done
    stop_pidfile "$PID_DIR/portal.pid" "portal"
    if [ "$any_bots" = "1" ]; then
        echo "✅ Portal and all bots stopped"
    else
        echo "✅ Portal stopped (no bots were running)"
    fi
else
    stop_pidfile "$PID_DIR/portal.pid" "portal"
    echo "✅ Portal stopped. Bots continue running."
fi
