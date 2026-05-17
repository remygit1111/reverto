# Reverto Operations Guide

Operational procedures for running Reverto. Use this as a reference when
something needs attention or for periodic maintenance tasks. For initial
setup see [INSTALL.md](INSTALL.md); for codebase architecture see
[architecture.md](architecture.md).

## First-time setup

On a fresh install (or after a destructive schema migration, see
"Schema migrations" below) the admin password must be set manually
before login is possible. The `users` table seeds an admin row, but
`password_hash` is `NULL`: `verify_password()` fails closed on NULL,
so without setup-admin every login returns 401.

**Step 1: install dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Step 2: set env vars via `.env` (one-time per host)**

```bash
cp .env.example .env
# Generate the security keys:
python3 -c 'import secrets; print("REVERTO_API_KEY=" + secrets.token_hex(32))' >> .env
python3 -c 'import secrets; print("REVERTO_SECRET_KEY=" + secrets.token_hex(32))' >> .env
# Open .env and fill in:
# - REVERTO_API_KEY / REVERTO_SECRET_KEY: generated above
# - REVERTO_INSECURE_COOKIES=1 for local dev over http://localhost;
#   leave empty in production behind a TLS reverse proxy
# - Exchange credentials (BITGET_*, KRAKEN_*) for live/paper bots
# - Telegram tokens if desired
```

`start.sh` sources `.env` before launching the portal process.
`make(1)` uses `/bin/sh` and does not read `.bashrc`, so `.env` is
the single source of truth, also after `make restart` from a new
SSH session. `.env` is in `.gitignore` and never leaves the host.

Without `REVERTO_API_KEY` / `REVERTO_SECRET_KEY` Reverto generates
ephemeral keys with a WARNING. That's OK for an initial trial but
loses all sessions on every restart.

**Step 3: initialize database**

```bash
make start          # runs init_db() which sets the schema to v4
# wait until "Portal started" appears in logs/portal.log
# then: Ctrl-C to stop
```

On a fresh install `init_db()` creates empty owned tables + the
admin seed without intervention. On an upgrade from an earlier
schema version see "Schema migrations" for the opt-in flow.

**Step 4: set admin password**

```bash
REVERTO_ADMIN_PW="a_strong_password" make setup-admin
```

This invokes `scripts/setup_admin.py` which writes a bcrypt hash
(rounds=12) to `users.password_hash` for user_id=1. Minimum length
is 12 characters (`PASSWORD_MIN_LENGTH` in `core/user_store.py`,
shared with `/api/auth/change-password`); shorter passwords are
rejected.

The script is idempotent. Re-invoking it with a different env var
overwrites the hash. Note: setup-admin does **not** bump
`session_epoch`, so existing sessions remain valid until they
expire or the user explicitly logs out.

**Step 5: login**

```bash
make start
make status     # confirm pid + log
```

Browse to `http://localhost:8080` and log in with username `admin`
and the password set above. Sanity check:
`curl http://localhost:8080/healthz` returns 200.

**Changing the password after setup**

Two paths:

- **Via portal** (recommended): log in, profile menu → Change
  Password. This calls `/api/auth/change-password`, which also
  bumps `session_epoch` so every other open session is logged out
  immediately.
- **Via CLI**: `REVERTO_ADMIN_PW="new" make setup-admin`, fast
  but does not bump the epoch, so old sessions remain valid.


## Schema migrations

Reverto versions its DB schema via `PRAGMA user_version`. On
startup `init_db()` detects the current version and migrates if
needed.

**Non-destructive migrations** (`ADD COLUMN`, new tables without
existing-data conflicts) run automatically on `make start`. No
opt-in, no backup needed, since nothing is being dropped.

**Destructive migrations** (DROP + CREATE of owned tables) require
an explicit operator opt-in since audit v26-10 (2026-04-20). This
prevents a routine `make start` after a version upgrade from
silently wiping every deal/order/user. When a destructive
migration is required `init_db()` refuses and shows:

```
[FATAL] Destructive schema migration required (v3 → v4). This will
DROP owned tables (deals, orders, annotations, backtest_runs, and
user password/role/session_epoch data).
To proceed, restart with REVERTO_DESTRUCTIVE_MIGRATE=1 set. A
pre-migration backup will be created automatically at
logs/pre-migration-backup-YYYYMMDD-HHMMSS.db.
See docs/OPERATIONS.md section 'Schema migrations' for details
including restore procedure.
```

To proceed:

1. **Read the release notes** to confirm which data will be wiped
   (in Phase-3a for example: all deal/order/annotation/backtest
   history + users.password_hash).

2. **Consider a manual backup** on top of the automatic one. The
   guard takes a backup itself (see step 4), but an extra manual
   snapshot of important data never hurts:

   ```bash
   sqlite3 logs/reverto.db ".backup logs/manual-backup-$(date +%Y%m%d-%H%M).db"
   ```

3. **Start with the opt-in env var**:

   ```bash
   REVERTO_DESTRUCTIVE_MIGRATE=1 make start
   ```

4. **Locate the pre-migration backup**. The guard writes
   automatically to `logs/pre-migration-backup-YYYYMMDD-HHMMSS.db`
   via `sqlite3.Connection.backup()` (WAL-aware). Keep this file
   until you are sure the new schema works correctly.

5. **Post-migration: re-run setup-admin** if the destructive
   migration touched the users table (as v3 to v4 Phase-3a does).
   Without a fresh password_hash, login is impossible again. See
   "First-time setup" step 4.

### Restore procedure

If the destructive migration was unwanted (e.g. operator set the
opt-in by accident, or the new schema turns out to have bugs):

```bash
# 1. Stop the portal (Ctrl-C in the make start terminal, or make stop)
make stop

# 2. Replace the current DB with the pre-migration backup
cp logs/pre-migration-backup-YYYYMMDD-HHMMSS.db logs/reverto.db

# 3. Downgrade code to the version from before the migration
git log --oneline    # find the last pre-migration commit
git checkout <sha>   # or check out the branch from before the upgrade

# 4. Start again. init_db() sees a DB at the old version and the
#    code expects the same version, so no migration is needed.
make start
```

The backup is a full SQLite file; `sqlite3 <backup>.db
'PRAGMA user_version'` shows which schema version it is on.


## Rollback procedure

When a deploy causes a regression, use the rollback script
(`scripts/rollback.sh`) to revert to a known-good state. The
ad-hoc `git revert && make deploy` flow is replaced with a single
scripted entry point that includes safety checks for
schema-migration commits + confirmation prompts.

### Quick rollback (most common)

From the production server:

```bash
cd ~/reverto
make rollback
```

This rolls back **1 commit**, resets git HEAD, and restarts the
portal. Bots keep running during the restart (only the portal
process is affected). The script prints a full plan and waits
for your `y` before doing anything destructive.

### Rolling back further

```bash
make rollback ARGS=3              # last 3 commits
make rollback ARGS="--to abc123"  # to a specific SHA
```

### Schema-migration WARNING

If any commit being rolled back touched `core/database.py`, the
script halts and requires explicit confirmation. Schema migrations
are forward-only in Reverto. Older code does not know how to
read data written against a newer schema, so a naive rollback
leaves the DB + code out of sync.

**If you get the migration warning, choose one:**

- **Option A: Restore DB from backup (safest):**

  ```bash
  # 1. Stop the portal first
  cd ~/reverto
  make stop

  # 2. Restore the pre-migration backup
  cp logs/pre-migration-backup-YYYYMMDD-HHMMSS.db logs/reverto.db

  # 3. Now the rollback is safe
  make rollback
  ```

- **Option B: Fix forward (preferred for most cases):**

  Instead of rolling back, write a new commit that addresses the
  regression. Keeps schema + code aligned and leaves a cleaner
  git history. Schema rollback is the exception, not the rule.

### What rollback does NOT do

- **Does NOT** touch bots; they keep running with their existing
  subprocesses + state files.
- **Does NOT** restore the DB from backup (see the
  schema-migration section above).
- **Does NOT** push to the remote. The reset is local-only until
  you explicitly `git push --force-with-lease origin main`. Only
  push if you want other operators pulling the reverted state.

### Reverse a rollback

If you want to undo the rollback itself:

```bash
git reflog              # find the previous HEAD
git reset --hard <prev-sha>
make restart
```

### Verify rollback success

After every rollback:

1. Open the portal in a browser; confirm the UI loads.
2. `tail -30 logs/portal.log`; confirm the portal started cleanly.
3. `ps aux | grep main_paper`; confirm bots are still running.
4. Test the specific flow that was broken pre-rollback.


## Backup and restore

Reverto automates daily on-host backups of the SQLite database
and the encrypted credentials tree.
Off-host replication (rsync to a secondary machine, S3, etc.)
is a VPS-follow-up, not in this procedure.

### What gets backed up

- `logs/reverto.db`: SQLite database (users, bots, deals,
  orders, chart-annotations, backtest-runs).
- `credentials/`: per-user encrypted `.enc` files.
- `keys/`: per-user Fernet master keys (required to decrypt
  the `.enc` files; losing these makes the backup useless).
- `logs/.credentials.key`: legacy master key (copied if still
  present on disk).
- `logs/.auth.json`: legacy auth-state (same deal).

NOT backed up (regenerable or tracked elsewhere):

- Code itself: `git`.
- Bot YAML configs (`config/bots/`): also in git.
- Bot state files (`logs/*/state.json`): regenerated from DB.
- Bot log files (`logs/*/*.log`): operational history, large,
  acceptable to lose.
- `.venv/`: `pip install -r requirements.txt` reproduces.

### Credentials in backups: what to expect

A common point of confusion on a freshly-deployed VPS: the first
few daily backups contain **empty** `credentials/` and `keys/`
directories. **This is correct behaviour, not a bug.** What you
should see at each lifecycle stage:

**Stage 1: fresh VPS, no exchange credentials saved yet.** The
`credentials/<user_id>/` and `keys/` directories may exist (the
portal lazily creates `credentials/<user_id>/` whenever any code
path in `core.credentials` queries it, including the
`/api/exchanges` listing endpoint that the SPA hits on first load),
but they're empty: no `.enc` files, no `.key` files. Your backup
will faithfully copy these empty directories. Audit r3-010 captured
this (operators were briefly confused, runbook now clarifies).

**Stage 2: you've saved Bitget or Kraken credentials via
`/api/exchanges/{name}/keys`.** First save also generates the
per-user Fernet master key. Your next daily backup contains:

- `credentials/<user_id>/bitget.enc`: Fernet-encrypted JSON blob
  carrying api_key + api_secret + (Bitget only) passphrase.
- `credentials/<user_id>/kraken.enc`: same shape, no passphrase.
- `keys/<user_id>.key`: per-user Fernet master key (0600). **You
  cannot decrypt the `.enc` files without this**; losing the
  matching key file makes the backup unusable.

The `.enc` extension signals "encrypted blob"; never `.json` (which
would imply plaintext). If you see a plaintext `.json` under
`credentials/`, that's a bug; file an issue.

**Stage 3: you've rotated the Fernet key (security ops, audit
trail, or post-incident).** `rotate_fernet_key(user_id)` writes a
backup of the previous key alongside the new one:

- `keys/<user_id>.key`: current key.
- `keys/<user_id>.key.bak.YYYYMMDDHHMMSS`: previous key, kept by
  the rotation routine for recovery from a half-completed rotation.
  These accumulate; cleanup is operator-driven (older than 7 days
  is safe to remove on a healthy install; search `rotate_fernet_key`
  in `core/credentials.py` for the contract).

Your backup includes ALL of `keys/`, including the `.bak.*` history.

**Verify your current backup contents:**

```bash
# Pick the most recent backup
LATEST=$(ls -t ~/reverto/backups/ | grep -E "^20[0-9]{2}-" | head -1)

# Inspect its contents and the manifest
ls -la ~/reverto/backups/${LATEST}/
cat ~/reverto/backups/${LATEST}/MANIFEST.txt
ls -la ~/reverto/backups/${LATEST}/credentials/ 2>/dev/null \
    || echo "(credentials/ not in this backup, fresh-VPS expected)"
ls -la ~/reverto/backups/${LATEST}/keys/ 2>/dev/null \
    || echo "(keys/ not in this backup, fresh-VPS expected)"
```

The `credentials/` and `keys/` directories are skipped entirely
when they don't exist on the source filesystem (see `backup.sh:88,
96`). The backup directory simply lacks them. They appear once
the portal has materialised the `credentials/<user_id>/` path
(typically the first time a logged-in user opens the Exchanges
page) or once you save a credential.

**Restore-time implication.** Restoring an old backup taken before
you saved any credentials yields an empty `credentials/` tree
post-restore. If you want post-backup-time credentials back, you'll
re-save them via the portal UI. The encryption key (`keys/<uid>.key`)
restored from the backup is necessary anyway, since freshly-saved
creds are encrypted under the per-user key that the running code
loads from disk.

### Scheduling the daily backup

On your production host:

```bash
crontab -e
```

Add (replace `/path/to/reverto` with your actual installation
directory, and adjust the user comment to match your setup):

```
# Reverto daily backup at 03:00 UTC
0 3 * * * cd /path/to/reverto && ./scripts/backup.sh >> \
    logs/backup.log 2>&1
```

Cron runs the script as the service user (e.g. `bot`);
permissions (600 files, 700 dirs) are applied inside the script
so the cron-environment UMASK doesn't matter.

### Manual backup

```bash
cd ~/reverto
make backup
```

Output: `backups/YYYY-MM-DD-HHMMSS/` with a `MANIFEST.txt`
listing each file + its size, the host, the git HEAD, and the
SQLite schema version (audit r3-008). The schema-version line
surfaces during `make restore` so you can spot a forward/backward
schema gap before confirming the restore.

### Failure monitoring and concurrency

`scripts/backup.sh` writes `backups/.last_error` (a single line:
UTC timestamp + the script exit code) on **any** non-zero exit —
not only the missing-database case, but also a failed SQLite
online-backup, a failed `cp`, a MANIFEST write error, a retention
prune error, or an interruption (Ctrl-C / SIGTERM). A clean run
removes the file as its last step, so a stale `.last_error`
unambiguously means "the most recent backup did not complete".
Monitor it from cron, e.g.:

```
# alert if the stamp exists OR the newest backup dir is >25h old
*/30 * * * * test -f ~/reverto/backups/.last_error && \
    echo "reverto backup FAILED: $(cat ~/reverto/backups/.last_error)" | \
    mail -s "reverto backup" you@example.com
```

(PT-v4-EI-002.) A correctly *declined* concurrent run does **not**
write `.last_error` — see below.

Only one `backup.sh` runs at a time: it takes an exclusive
`flock` on `/var/lock/reverto-backup.lock` (override with the
`REVERTO_BACKUP_LOCK` env var; a repo-local fallback is used if
that path is not writable). A second invocation while one is
running (cron + a manual `make backup`, say) exits non-zero
immediately with an "already running" message and changes
nothing. (PT-v4-EI-003.)

The per-user encrypted `credentials/` and `keys/` trees are
copied with a brief wait for any in-progress Fernet key rotation
to finish, then staged to a `.tmp` and atomically renamed, so a
restore never sees a half-rotated or half-copied credential set.
(PT-v4-EI-001.)

### Retention policy

The script prunes older backups on every run:

- **Daily backups** kept for 7 days.
- **Weekly backups** (any snapshot whose date falls on a
  Sunday) kept for 28 days.
- **Monthly backups** (the 1st of each month) kept for 90 days.

Everything outside those windows is removed. Pre-restore
snapshots created by `scripts/restore.sh` live under
`backups/pre-restore-<ts>/` and are **not** affected by the
retention prune (they stay until you delete them manually).

Steady-state disk footprint is ~100 MB × (7 + 4 + 3) ≈ 1.5 GB
today; adjust `RETAIN_DAILY` / `RETAIN_WEEKLY` / `RETAIN_MONTHLY`
at the top of `scripts/backup.sh` if a different cadence is
desired.

### Restore procedure

Prerequisites: portal must be stopped (the script refuses to
run against a live PID). Restoring under a live portal would
let the engine commit a WAL frame on top of the restored DB.

```bash
cd ~/reverto
make stop
ls backups/                             # list available snapshots
make restore BACKUP=backups/2026-04-24-030000
```

The script:

1. Validates the backup directory (`reverto.db` must be present).
2. Takes a **pre-restore snapshot** of current state so an
   accidental restore is itself reversible.
3. Prints the restore plan + MANIFEST and waits for a `y`.
4. Restores the DB, credentials, keys, and any legacy files.
5. Fixes permissions (0600 on sensitive files, 0700 on dirs).

After the restore:

```bash
make start
tail -20 logs/portal.log                # confirm clean startup
```

### Reversing a restore

If the restore was a mistake, run it against the pre-restore
snapshot the script saved:

```bash
ls backups/pre-restore-*                # find the snapshot
make stop
make restore BACKUP=backups/pre-restore-<ts>
make start
```

### Testing restores (recommended monthly)

A backup that hasn't been test-restored is a wish, not a
backup. Once a month (or before any VPS migration) verify:

1. Take a fresh backup: `make backup`.
2. On a non-production machine copy the repo elsewhere,
   drop a recent backup dir onto it, run `make restore`, start
   the portal, confirm login + bot-list work.

The pre-restore snapshot guarantees the test on a non-production machine
can be undone.

### Off-host backup (future)

This guide covers on-host backups only. Off-host replication is
an off-host replication path (rsync to a separate machine or
an S3-compatible endpoint) so a full-host failure (disk
corruption, ransomware, lost VPS) doesn't take the backups
with it. Tracked on the VPS roadmap.


## TOTP recovery (operator-side fallback)

When a user has TOTP enabled but loses access to their authenticator
app (lost phone, app deleted, secret corrupted), the operator can
reset TOTP via the admin-reset wrapper script.

**Procedure (recommended, produces audit-log entry):**

```bash
# 1. SSH to the VPS as the bot user
ssh bot@reverto.bot
cd ~/reverto

# 2. Run the wrapper. It validates the user exists + currently has
#    TOTP enabled, prompts for confirmation, writes an audit-log
#    entry, and clears the encrypted seed.
.venv/bin/python scripts/totp_admin_reset.py --username <name>

# Or with the recovery reason recorded in the audit entry:
.venv/bin/python scripts/totp_admin_reset.py \
  --username <name> --reason "lost phone"

# Or scripted (skips the confirmation prompt):
.venv/bin/python scripts/totp_admin_reset.py \
  --username <name> --reason "..." --yes
```

The wrapper writes a `totp_admin_reset` event to
`logs/audit.jsonl` (and the per-user split at
`logs/<user_id>/audit.jsonl`) BEFORE the DB UPDATE, so a forensic
investigator chasing a "who reset whose TOTP" question has the
same trail they would for any other `totp_*` event. The
`reason` field is captured verbatim. Verify with
`tail -1 ~/reverto/logs/audit.jsonl` after running.

The user can now log in with their password only, and can re-enrol
via Profile → Enable TOTP with a new authenticator-app entry.

**When to use:**

- Operator's own lockout (you are the admin).
- User-requested TOTP reset.

**Security:**

Requires SSH access to the VPS plus sudo rights on the bot user
account. Not exposed via the portal (no reset endpoint in the UI).
Both the audit row and the SSH login log capture the recovery.

**Emergency fallback, raw SQL (no audit-log entry):**

If the wrapper cannot run (broken Python environment,
`logs/` filesystem unwritable, or `init_db()` itself failing)
the raw SQL path remains:

```bash
# Verify the user has TOTP enabled
sqlite3 ~/reverto/logs/reverto.db \
  "SELECT id, username, totp_seed_encrypted IS NOT NULL AS has_totp
   FROM users WHERE username = '<name>';"

# Reset TOTP (set the column to NULL)
sqlite3 ~/reverto/logs/reverto.db \
  "UPDATE users SET totp_seed_encrypted = NULL
   WHERE username = '<name>';"

# Verify the reset
sqlite3 ~/reverto/logs/reverto.db \
  "SELECT username, totp_seed_encrypted IS NOT NULL FROM users
   WHERE username = '<name>';"
# Expected: <name>|0
```

The raw SQL path produces **NO application-layer audit row**.
That's why it's the fallback, not the primary path. Operator
must record the reset out-of-band (incident log, ticket, email)
for compliance and forensic continuity. Treat as last resort.

**Validation (pt-150 fix):** wrapper tested on 2026-04-29 during
the `fix/pt-150-totp-admin-reset-wrapper` deploy. Audit-row
visible in `logs/audit.jsonl` with the operator-supplied reason
captured verbatim.


## Startup checklist (fresh machine)

See "First-time setup" at the top of this document for the full
flow (install → env vars → init_db → setup-admin → login).
Summary for those already familiar:

```bash
# 1. Venv + deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Env vars via .env (start.sh sources this before launching)
cp .env.example .env
python3 -c 'import secrets; print("REVERTO_API_KEY=" + secrets.token_hex(32))' >> .env
python3 -c 'import secrets; print("REVERTO_SECRET_KEY=" + secrets.token_hex(32))' >> .env
# Edit .env to add REVERTO_INSECURE_COOKIES=1 for localhost dev,
# plus exchange + Telegram credentials if needed.

# 3. Init database + set admin password
make start          # runs init_db(); stop after "Portal started"
REVERTO_ADMIN_PW="a_strong_password" make setup-admin

# 4. Start portal
make start
make status   # confirm pid + log
```

Sanity: `curl http://localhost:8080/healthz` returns 200.
Login: `http://localhost:8080`, username `admin` + the password
you set.


## Graceful shutdown

```bash
make stop        # stops portal only; bots keep running
make stop-all    # stops portal + every bot via SIGTERM
```

`make stop-all` waits up to 10s for each bot to flush Telegram
notifications via its `notify_stop` / `notify_shutdown` queue drain.


## Live bot dry-run via the portal (Phase 1)

Under Phase 1, live-mode bots are only allowed in dry-run: the
runner uses the real exchange client for ticker data but refuses
`_place_market_order` in non-dry-run. The portal now lets you
start this without going through `make live-dry`.

- **Overview**: bots with `mode: live` in their YAML show an orange
  **▶ Start dry-run** button (no green Start). Click → confirm in
  the dialog → portal spawns `main_live.py --bot <slug> --dry-run`
  with `DRY_RUN=1` in the env (the confirmation prompt is skipped).
- **Running state**: the "Running" pill gets a yellow banner
  **🟡 DRY RUN, no real orders placed** while the bot runs. This
  stays until Phase 3 allows real execution.
- **Stop / Restart**: work unchanged. Restart is mode-aware: a
  live bot restarts as dry-run again, a paper bot as paper.
- **API**: `POST /api/bots/<slug>/start-dry-run` (auth, rate-limited
  20/min, audited as `bot_start_dry_run`). Paper-mode bots are
  rejected by the helper with a clear error message instead of a
  silent subprocess exit.

`make live-dry BOT=<slug>` remains an alternative for ops flows
without the portal.


## Emergency stop

Portal → profile menu → **🛑 Emergency stop**. Confirm the dialog.
Or via API:

```bash
curl -X POST -H "X-API-Key: $REVERTO_API_KEY" \
     http://localhost:8080/api/emergency-stop
```

Effect: every running bot gets SIGTERM. Open positions on the exchange
are **not** auto-closed; the operator reconciles manually if needed.
Recommended when a drawdown alert fires or a suspected runaway DCA.


## Wipe deals (complete data reset)

Use case: finishing a parity test, staging reset, debug after a
corrupt state. Throws away **ALL** deal / order / annotation
history. NOT idempotent. Always back up `logs/reverto.db` first.

Preparation:

- Stop ALL running bots (`make stop-all` or via the portal).
- Check there are no live PID files left: `ls logs/<user_id>/pids/`.

Execution:

```bash
make wipe-deals
```

What it does:

1. Acquires an exclusive `fcntl.flock` on `logs/.wipe.lock`;
   concurrent wipe invocations are blocked (`RuntimeError:
   Another wipe operation is already in progress`).
2. Scans `logs/<user_id>/pids/*.pid` + `os.kill(pid, 0)`; if any
   process is alive, aborts with a summary of what is still
   running.
3. `DELETE FROM orders` → `DELETE FROM deals` (orders first
   because of the FK to deals) → `VACUUM`.
4. Resets every `logs/<user_id>/<slug>.state.json`: `balance_btc`
   back to `initial_balance_btc`, `open_deals=[]`,
   `closed_deals=[]`, `*_count=0`. Other fields are left
   untouched.

Backups:

- The script does NOT take a DB backup automatically. Run
  `cp logs/reverto.db logs/reverto.db.pre_wipe.$(date +%s)`
  beforehand if you want a rollback option.
- Per state.json, a `*.state.json.pre_wipe_backup` copy is
  written before the reset (overwritten on repeated wipes).

After the wipe: bots can be started again. They pick up the
reset state.json (open=0, closed=0).

If `RuntimeError: already in progress` persists indefinitely
without an actual wipe running: check `fuser logs/.wipe.lock`,
then optionally `rm logs/.wipe.lock` (the lock file is safe to
delete if no process is holding it open; flock is
kernel-advisory, stale files cause no harm).


## State file recovery

When a state file is corrupt (malformed JSON, partial write):

```bash
# 1. Identify corrupt file
ls -la logs/<slug>.state.json
# 2. Check for orphan .tmp
ls -la logs/<slug>.state.tmp
# 3. Start bot (engine's _load_state sweeps orphan .tmp and
#    falls back to clean state on JSON parse failure)
```

`_load_state` logs a warning on parse failure and starts with a clean
PaperState. The closed-deal history stored in `logs/reverto.db` is
still intact.


## Drawdown guard

Reset after a triggered guard:

```bash
curl -X POST -H "X-API-Key: $REVERTO_API_KEY" \
     http://localhost:8080/api/bots/<slug>/drawdown/reset
```

This rewrites `state.json` to `triggered=false, peak=null`. The bot
picks it up on the next tick (or a bot restart if the reset happens
while the process is stopped).

Guard config lives in the bot YAML:

```yaml
drawdown_guard:
  enabled: true
  max_drawdown_pct: 10.0     # % drop from peak that triggers
  metric: equity              # equity | balance
  action: pause               # pause (skip new entries) | stop
```


## Bot copy / export / import

### Duplicate bot

Portal → **Bots** → ⋮ menu on a bot card → **Duplicate**. The
prompt asks for a new slug (only `[A-Za-z0-9_-]+`). Server-side
copy: no deal history, no state, no credentials are copied
along. The duplicate starts with empty state and must be started
itself via the portal.

### Export bot config

Portal → **Bots** → ⋮ → **Export**. The browser downloads
`<slug>.yaml` with a metadata header (Reverto git SHA, export
timestamp, original slug). Strategy only: no credentials, no
state.

### Import bot config

Portal → **Bots** page → **Import Bot** button next to **New
Bot**. Upload a `.yaml` or `.yml` file. The prompt asks for a
target slug (default: filename without extension). Validation via
`config.models.BotConfig`; malformed YAML or schema conflict
shows a toast with the exact error message.

Name conflict (target slug already exists): response 409, the
prompt reappears for a different slug. The import only writes to
disk after a successful Pydantic validation, so a half-validated
config never lands on disk.

Use cases: strategy experiments without risk to the parity test,
sharing configs between environments, template workflows ("copy
'RSI 5m' to make an 'RSI 15m' variant").


## Log level override

Bot subprocesses log at INFO by default. For retrospective DEBUG
info (e.g. "why didn't this deal trigger?"):

```bash
REVERTO_LOG_LEVEL=DEBUG make restart
```

That (re)starts ALL bots with DEBUG level. Per-tick indicator
evaluations, timeframe snapshots, and internal state transitions
will then appear in `logs/<user_id>/<slug>.log`. Log files grow
~20× faster (~30 MB/day/bot vs. ~1.5 MB at INFO).

Switching back:

```bash
make restart
```

(without the env var) uses the default again.

The portal UI also has a filter dropdown per bot-log tab (ALL /
WARNING + ERROR). That is **client-side visibility**; it does
not affect what the engine writes to disk, only what the browser
shows.


## Credential rotation

```bash
# 1. Stop every bot so no engine reads mid-rotation
make stop-all

# 2. Rotate
.venv/bin/python scripts/rotate_credentials.py

# 3. Verify
cat logs/credentials.json | python3 -m json.tool     # ciphertext changed
ls -la logs/.credentials.key.bak                     # 7-day rollback copy

# 4. Restart portal
make start
```

If step 2 fails mid-way: the old key + old ciphertext are still valid
(rotate is atomic: key first, then creds). If the key rotated but the
creds didn't, restore the creds from `logs/credentials.json.tmp` or
re-save via the portal (Exchanges → Add Keys).


## Common errors + fixes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Portal 503 on `/readyz` | SQLite unreachable (disk full, locked) | `df -h logs/`, clear WAL (`sqlite3 logs/reverto.db 'PRAGMA wal_checkpoint(FULL)'`) |
| Bot refuses to start, "Invalid bot slug" | `--config` path or `--bot` slug contains `..`, `/`, or spaces | Rename YAML to `[A-Za-z0-9_-]+.yaml` |
| Live bot crashes with `BITGET_PASSPHRASE required` | Env var missing from `.env` (or portal was started before it was added) | Add `BITGET_PASSPHRASE=...` to `.env` and `make restart` |
| LiveEngine preflight: "Worst-case DCA" / "Cumulative DCA" | Multiplier × max_orders produces an order beyond cap | Lower `multiplier`, `max_orders`, or set `dca.max_cumulative_size` explicitly |
| Drawdown triggered unexpectedly | Peak anchored low after restart (pre-persistence YAML) | Verify `drawdown_guard.peak_value` in `state.json`; reset via API endpoint |
| Deal opened on wrong side | Old YAML missing `direction`; engine used to default to `long` | Set `direction: long` or `direction: short` in YAML explicitly |
| Every API call returns 401 | `REVERTO_API_KEY` missing from `.env` at portal start | Add to `.env` and `make restart` (ephemeral key is discarded) |
