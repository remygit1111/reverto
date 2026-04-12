#!/bin/bash
# stop.sh — Stop het Reverto portal en alle actieve bots

cd "$(dirname "$0")"

PID_DIR="logs/pids"

# Stop alle bots
for pid_file in "$PID_DIR"/*.pid; do
    [ -f "$pid_file" ] || continue
    name=$(basename "$pid_file" .pid)
    PID=$(cat "$pid_file")
    if kill -0 "$PID" 2>/dev/null; then
        echo "🛑 Stopping $name (PID $PID)..."
        kill "$PID"
        # Wacht tot het proces daadwerkelijk gestopt is (max 5 seconden)
        for i in $(seq 1 10); do
            sleep 0.5
            kill -0 "$PID" 2>/dev/null || break
        done
        # Forceer stop als proces nog draait na 5 seconden
        if kill -0 "$PID" 2>/dev/null; then
            echo "⚠️  $name did not stop gracefully — sending SIGKILL"
            kill -9 "$PID" 2>/dev/null
        fi
    fi
    rm -f "$pid_file"
done

echo "✅ All stopped"
