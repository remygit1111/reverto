#!/bin/bash
# status.sh — Toon status van portal en alle bots

cd "$(dirname "$0")"

PID_DIR="logs/pids"

echo "══════════════════════════════════"
echo "  REVERTO — Process Status"
echo "══════════════════════════════════"

if [ ! -d "$PID_DIR" ]; then
    echo "  Nothing running (no pids/ directory)"
    exit 0
fi

found=0
for pid_file in "$PID_DIR"/*.pid; do
    [ -f "$pid_file" ] || continue
    found=1
    name=$(basename "$pid_file" .pid)
    PID=$(cat "$pid_file")
    if kill -0 "$PID" 2>/dev/null; then
        echo "  ● $name    RUNNING  (PID $PID)"
    else
        echo "  ○ $name    STOPPED  (stale PID $PID)"
    fi
done

if [ $found -eq 0 ]; then
    echo "  Nothing running"
fi

echo "══════════════════════════════════"
