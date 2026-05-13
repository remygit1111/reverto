# Makefile — Reverto
# Usage: make <target>
# Requires: GNU make, .venv present

PYTHON  := .venv/bin/python3
PORTAL  := logs/pids/portal.pid

.PHONY: help setup start stop stop-all restart status log test lint clean backtest notebook beep live live-dry parity-compare reset-db migrate-fs wipe-deals setup-admin seed-findings deploy deploy-marketing rollback backup restore scheduler-status scheduler-restart scheduler-logs

# ── Default target ───────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  REVERTO — available commands"
	@echo ""
	@echo "  make setup           Create default bot YAML files (idempotent)"
	@echo "  make start           Start the portal in the background"
	@echo "  make stop            Stop only the portal (bots keep running)"
	@echo "  make stop-all        Stop the portal AND all bots (machine shutdown)"
	@echo "  make restart         Restart the portal, bots keep running"
	@echo "  make status          Show which processes are running"
	@echo "  make log             Follow the portal log live (Ctrl+C to stop)"
	@echo "  make log b=name      Follow the log of a specific bot"
	@echo "  make log a=1         Follow the audit log (start/stop/restart events)"
	@echo "  make test            Run all pytest tests"
	@echo "  make lint            Check code with ruff"
	@echo "  make backtest        Run backtest with default config"
	@echo "  make backtest tf=4h  Backtest on 4h candles"
	@echo "  make clean           Remove stale PID files and .tmp state files"
	@echo ""

# ── Setup: ensure the bots directory exists ──────────────────────────────────
# We no longer generate default YAML files — bots are created via the web
# portal so a fresh clone does not immediately have example bots that
# could be started by accident.

setup:
	@mkdir -p config/bots
	@echo "Create bots via the web portal."
	@echo "Setup done"

# ── Portal start/stop/restart ────────────────────────────────────────────────
start:
	@bash start.sh

stop:
	@bash stop.sh

stop-all:
	@bash stop.sh --all

restart: stop
	@sleep 1
	@bash start.sh

status:
	@bash status.sh

# ── Audit-findings tracker sync ──────────────────────────────────────────────
# Idempotent re-import of data/findings_seed.yaml into the audit_findings
# DB table via INSERT OR IGNORE. Operator edits via the admin UI (status,
# notes, resolution_ref) are preserved — the script only touches new
# finding_ids. Safe to run repeatedly.
#
# Called automatically by `make deploy` so YAML additions (a new
# pentest batch) appear in the admin UI immediately after a `git pull`.
seed-findings:
	@echo ""
	@echo "  [seed-findings] Syncing data/findings_seed.yaml → audit_findings DB…"
	@$(PYTHON) scripts/seed_audit_findings.py
	@echo ""

# ── Remote deployment ────────────────────────────────────────────────────────
# Workflow: dev merges PR to main, then deploys via:
#   ssh bot@<host> 'cd ~/reverto && make deploy'
#
# Target ONLY does git pull — no automatic bot restarts. The operator
# decides via the portal UI which bots to restart (or via
# `make restart` for the portal itself). Bot-restart automation
# requires a separate design session about bot-state preservation
# and is out of scope of deploy triviality.
#
# On a destructive schema migration (see docs/OPERATIONS.md "Schema
# migrations"), init_db() refuses to boot without the explicit
# REVERTO_DESTRUCTIVE_MIGRATE=1 opt-in; that flag is NEVER set in
# this target — destructive migrations belong to an explicit step,
# not a routine deploy.
deploy:
	@echo ""
	@echo "  [deploy] pulling latest main…"
	@echo ""
	@git pull origin main
	@echo ""
	@echo "  [deploy] git pull complete."
	@$(MAKE) seed-findings
	@echo "  [deploy] Next steps (manual):"
	@echo "    - Restart the portal if code changes require it:"
	@echo "        make restart"
	@echo "    - Restart relevant bots via the portal UI"
	@echo "    - On schema-migration prompts: see docs/OPERATIONS.md"
	@echo "      section 'Schema migrations' for the opt-in flow"
	@echo ""

# ── Deploy marketing site (reverto.bot, static) ─────────────────────────────
# Rsyncs marketing/ to /var/www/reverto-marketing/ on the production VPS,
# sets ownership to caddy:caddy, and applies 755/644 permissions.
#
# Run this ON the VPS (the sudo chown/chmod calls are local). From a
# dev machine, SSH to the VPS first:
#     ssh bot@<vps> 'cd ~/reverto && git pull && make deploy-marketing'
#
# CRITICAL: the data/ subdirectory at /var/www/reverto-marketing/data/ is
# owned by bot:bot (the FastAPI process writes JSON snapshots there via
# core/marketing_export.py). Touching its ownership would break auto-
# export. PR 3 of the marketing-app split fixed a latent bug where the
# original recursive `chown -R caddy:caddy` would walk into data/ and
# break that on every redeploy. The current target excludes data/ from
# both the rsync and the chown/chmod walks via `-path ... -prune`.
#
# README.md and .git* are excluded from rsync so they don't end up
# served at https://reverto.bot/README.md.
deploy-marketing:
	@echo "Deploying marketing site to /var/www/reverto-marketing/..."
	@# Use -rlt instead of -av to avoid copying ownership/group attrs
	@# (target dir is caddy:caddy, source is bot:bot — would fail
	@# without sudo). Ownership is set explicitly via the chown step
	@# below. sudo on rsync is needed because the receiver writes
	@# .tmp files into the caddy-owned target dir.
	sudo rsync -rlt --delete \
		--exclude=README.md \
		--exclude='.git*' \
		--exclude='data' \
		marketing/ /var/www/reverto-marketing/
	@# Chown only the static files — data/ stays bot:bot.
	sudo find /var/www/reverto-marketing \
		-path /var/www/reverto-marketing/data -prune \
		-o \( -type d -o -type f \) -exec chown caddy:caddy {} \;
	sudo find /var/www/reverto-marketing \
		-path /var/www/reverto-marketing/data -prune \
		-o -type d -exec chmod 755 {} \;
	sudo find /var/www/reverto-marketing \
		-path /var/www/reverto-marketing/data -prune \
		-o -type f -exec chmod 644 {} \;
	@echo "Marketing site deployed (data/ ownership preserved as bot:bot)."

# ── Rollback — audit r1-038 ─────────────────────────────────────────────────
# Scripted rollback of the production portal. Resets HEAD by N commits
# (default 1) or to a specific SHA (ARGS="--to <sha>"), then restarts.
# Warns on schema-migration commits; the operator confirms each
# destructive step. See docs/OPERATIONS.md section "Rollback procedure"
# for the full flow + safety notes.
rollback:
	@bash scripts/rollback.sh $(ARGS)

# ── Backup + restore — audit r1-022 ─────────────────────────────────────────
# `make backup` writes a timestamped snapshot to backups/<ts>/
# (DB + credentials + keys). Intended for cron (daily at 03:00 UTC)
# but also runnable ad-hoc. Retention: 7 days daily + 4 weeks
# weekly + 3 months monthly. See docs/OPERATIONS.md section
# "Backup and restore" for scheduling + off-host follow-ups.
#
# `make restore BACKUP=backups/<ts>` restores a specific snapshot.
# Portal must be stopped first; script takes a pre-restore
# snapshot of the current state before overwriting.
backup:
	@bash scripts/backup.sh

restore:
	@bash scripts/restore.sh $(BACKUP)

# ── Tail logs ────────────────────────────────────────────────────────────────
log:
ifdef b
	@tail -f logs/$(b).log
else ifdef a
	@tail -f logs/audit.log
else
	@tail -f logs/portal.log
endif

# ── Tests ────────────────────────────────────────────────────────────────────
test:
	@$(PYTHON) -m pytest tests/ -v

# ── Lint ─────────────────────────────────────────────────────────────────────
lint:
	@$(PYTHON) -m ruff check . 2>/dev/null || echo "ruff not installed — pip install ruff"

# ── Backtest ─────────────────────────────────────────────────────────────────
backtest:
	@$(PYTHON) main_backtest.py \
		--config config/bots/btc_backtest.yaml \
		--timeframe $(or $(tf),1h) \
		--limit $(or $(limit),1000) \
		--balance $(or $(bal),0.1)

# ── Cleanup ─────────────────────────────────────────────────────────────────
clean:
	@echo "Cleaning up..."
	@find logs/pids -name "*.pid" 2>/dev/null | while read f; do \
		PID=$$(cat "$$f"); \
		kill -0 "$$PID" 2>/dev/null || (echo "  Removing stale PID: $$f" && rm -f "$$f"); \
	done
	@find logs -name "*.tmp" -delete 2>/dev/null && echo "  .tmp files removed" || true
	@find . -name "*Zone.Identifier" -delete 2>/dev/null && echo "  Zone.Identifier files removed" || true
	@echo "Done"
beep:
	@bash scripts/notify.sh

notebook:
	.venv/bin/jupyter notebook --no-browser --port=8888

# ── Live trading (Phase 1: dry-run only) ─────────────────────────────────────
# live      — launch a LIVE bot with interactive confirmation
# live-dry  — DRY_RUN=1 + --dry-run, no confirmation prompt, no real orders
#
# Both targets require BOT=<slug> so nothing can boot by accident.

live:
	@echo "⚠  Live trading requires explicit bot slug"
	@echo "Usage: make live BOT=slug_here"
	@test -n "$(BOT)" || (echo "BOT= required" && exit 1)
	$(PYTHON) main_live.py --bot $(BOT)

live-dry:
	@test -n "$(BOT)" || (echo "BOT= required" && exit 1)
	DRY_RUN=1 $(PYTHON) main_live.py --bot $(BOT) --dry-run

# ── Parity testing ────────────────────────────────────────────────────────────
# Compare a paper bot's deals vs a live-dry bot's deals. Used after
# running both side-by-side for ≥ 1 week to decide whether the paper
# engine is a faithful proxy for live execution.
#
# Usage:
#   make parity-compare PAPER=rsi_paper_test LIVE=rsi_real_test
#   make parity-compare PAPER=... LIVE=... SINCE=2026-04-18
parity-compare:
	@test -n "$(PAPER)" || (echo "Usage: make parity-compare PAPER=<slug> LIVE=<slug> [SINCE=YYYY-MM-DD]" && exit 1)
	@test -n "$(LIVE)"  || (echo "Usage: make parity-compare PAPER=<slug> LIVE=<slug> [SINCE=YYYY-MM-DD]" && exit 1)
	$(PYTHON) scripts/parity_compare.py --paper $(PAPER) --live $(LIVE) $(if $(SINCE),--since $(SINCE),)

# ── Multi-tenant migration helper ────────────────────────────────────────────
# reset-db  — destructive: backups logs/reverto.db + every *.state.json
#             to .pre_mt.<timestamp> and removes the originals. Run once
#             before the first boot on the new v3 schema.
reset-db:
	$(PYTHON) scripts/reset_db.py

# wipe-deals — destructive: empties the deals + orders tables AND
# resets every logs/<uid>/*.state.json to its "fresh start" shape
# (balance → initial, open_deals/closed_deals → [], counts + pnl → 0).
# Each state.json is backed up to <path>.pre_wipe_backup first.
# Used once after the cross-bot deal-id collision fix (2026-04-19):
# the pre-fix ledger had silently-overwritten rows, so restoring it
# has no value. Stop every bot via the portal BEFORE running; the
# script refuses if any pid-file points at a live process, and also
# prompts for "WIPE" to confirm.
wipe-deals:
	$(PYTHON) scripts/wipe_deals.py

# migrate-fs — Phase-2 layout migration (destructive but idempotent).
# Stop every bot via the portal before running. Moves per-bot config +
# state + log + pid files into user-scoped subdirectories, converts
# logs/credentials.json into per-exchange .enc files under a fresh
# per-user Fernet key at keys/1.key.
migrate-fs:
	$(PYTHON) scripts/migrate_to_user_fs.py

# setup-admin — Phase-3a post-migration password provisioning. Run
# ONCE after `make start` has seeded the admin row via init_db().
# Accepts REVERTO_ADMIN_PW env-var for automation, otherwise prompts.
# Without this step, nobody can log in (verify_password fails closed
# on a NULL password_hash).
setup-admin:
	$(PYTHON) scripts/setup_admin.py

# ── Portfolio snapshot scheduler (systemd service) ──────────────────────────
# The reverto-scheduler service runs main_scheduler.py as a long-
# running process that captures hourly portfolio snapshots. Install
# steps are in deploy/README.md; these targets are the day-to-day
# control surface.
scheduler-status:
	@sudo systemctl status reverto-scheduler

scheduler-restart:
	@sudo systemctl restart reverto-scheduler

scheduler-logs:
	@tail -f logs/scheduler.log
