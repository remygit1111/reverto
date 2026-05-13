# deploy/

Systemd unit files and their install notes.

## `reverto-scheduler.service`

Standalone service that runs `main_scheduler.py` — the hourly
portfolio-snapshot loop that backs the Portfolio tab in the portal.

### Install

```bash
sudo cp deploy/reverto-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reverto-scheduler
```

The first snapshot batch lands at the **next top-of-hour** after the
service starts. Until then the Portfolio tab renders the "No
snapshots yet" placeholder.

### Health checks

```bash
make scheduler-status     # systemctl status reverto-scheduler
make scheduler-logs       # tail -f logs/scheduler.log
make scheduler-restart    # systemctl restart reverto-scheduler
```

The PID file at `logs/pids/scheduler.pid` is written by the loop on
boot and removed by the atexit handler on clean shutdown. A stale
PID file after a `kill -9` is harmless — the next boot overwrites it.

### Disable

```bash
sudo systemctl stop reverto-scheduler
sudo systemctl disable reverto-scheduler
```

Existing snapshot rows are kept in `logs/reverto.db` — disabling the
service stops new captures but the historical chart on the Portfolio
tab keeps working from the rows already in the table.

### Notes

- The service runs as `bot:bot` against the same SQLite file the
  portal uses (`logs/reverto.db`). SQLite WAL mode lets both
  processes write without locking each other out — the scheduler
  writes append-only INSERTs while the portal serves the read path.
- `main_scheduler.py` calls `init_db()` at boot, so the service is
  safe to start independently of the portal. The migration is
  idempotent.
- A SIGTERM (from `systemctl stop` or `systemctl restart`) is
  caught inside the scheduler — it finishes the current snapshot
  batch before exiting cleanly, so no half-written rows.
