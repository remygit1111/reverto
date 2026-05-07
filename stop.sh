#!/bin/bash
# stop.sh — Stop the Reverto portal (and optionally all bots).
#
# Default behaviour: stops ONLY the portal process. Bot subprocesses
# keep running — that's what you want on a portal restart (make
# restart) so trading positions are not lost by a UI refresh.
#
# Use `--all` to also stop all bot processes. That is the behaviour
# that make stop-all invokes, typically for a machine shutdown or
# when you genuinely want to bring everything down.

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
        # Wait until the process has actually stopped (max 8 seconds).
        # A graceful portal shutdown can take ~5s: uvicorn's
        # timeout_graceful_shutdown=5 waits on pending requests and
        # the lifespan handler takes another 2s for task cancellation.
        # 5s here was too tight — it occasionally produced an
        # unnecessary SIGKILL on the not-quite-finished process. 8s
        # gives a comfortable budget without slowing ops flows.
        for _ in $(seq 1 16); do
            sleep 0.5
            kill -0 "$pid" 2>/dev/null || break
        done
        # Force stop if the process is still running after 8 seconds
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
    # Stop all bots first, then the portal.
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
