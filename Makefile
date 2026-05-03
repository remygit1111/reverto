# Makefile — Reverto
# Gebruik: make <target>
# Vereist: GNU make, .venv aanwezig

PYTHON  := .venv/bin/python3
PORTAL  := logs/pids/portal.pid

.PHONY: help setup start stop stop-all restart status log test lint clean backtest notebook beep live live-dry parity-compare reset-db migrate-fs wipe-deals setup-admin deploy deploy-marketing rollback backup restore

# ── Standaard target ──────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  REVERTO — beschikbare commando's"
	@echo ""
	@echo "  make setup           Maak standaard bot YAML bestanden aan (idempotent)"
	@echo "  make start           Start het portal op de achtergrond"
	@echo "  make stop            Stop alleen het portal (bots blijven draaien)"
	@echo "  make stop-all        Stop portal EN alle bots (machine shutdown)"
	@echo "  make restart         Herstart het portal, bots blijven draaien"
	@echo "  make status          Toon welke processen draaien"
	@echo "  make log             Volg de portal log live (Ctrl+C om te stoppen)"
	@echo "  make log b=naam      Volg de log van een specifieke bot"
	@echo "  make log a=1         Volg de audit log (start/stop/restart events)"
	@echo "  make test            Voer alle pytest tests uit"
	@echo "  make lint            Controleer code met ruff"
	@echo "  make backtest        Voer backtest uit met standaard config"
	@echo "  make backtest tf=4h  Backtest op 4h candles"
	@echo "  make clean           Verwijder stale PID bestanden en .tmp state files"
	@echo ""

# ── Setup: zorg dat de bots-directory bestaat ────────────────────────────────
# We genereren geen default YAML bestanden meer — bots worden via de web
# portal aangemaakt zodat een fresh clone niet meteen voorbeeld-bots heeft
# die per ongeluk gestart kunnen worden.

setup:
	@mkdir -p config/bots
	@echo "Create bots via the web portal."
	@echo "Setup klaar"

# ── Portal start/stop/restart ─────────────────────────────────────────────────
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

# ── Remote deployment (Reverto-Server) ───────────────────────────────────────
# Workflow: dev op Reverto-Dev merged PR naar main, deploy vanaf Dev via:
#   ssh bot@192.168.178.227 'cd ~/reverto && make deploy'
#
# Target doet ALLEEN git pull — geen automatische bot-restarts. De operator
# moet zelf via de portal-UI beslissen welke bots te herstarten (of via
# `make restart` voor het portal zelf). Bot-restart-automation vereist een
# aparte design-sessie over bot-state-preservation en het valt buiten de
# scope van deploy-triviality.
#
# Bij een destructive schema migration (zie docs/runbook.md "Schema
# migrations") weigert init_db() te boot'en zonder de expliciete
# REVERTO_DESTRUCTIVE_MIGRATE=1 opt-in; die flag wordt NOOIT in deze
# target gezet — destructive migrations horen expliciet, niet via een
# routine-deploy.
deploy:
	@echo ""
	@echo "  [deploy] Reverto-Server pulling latest main…"
	@echo ""
	@git pull origin main
	@echo ""
	@echo "  [deploy] git pull complete."
	@$(MAKE) seed-findings
	@echo "  [deploy] Next steps (manual):"
	@echo "    - Restart het portal als code-wijzigingen dat vereisen:"
	@echo "        make restart"
	@echo "    - Herstart relevante bots via de portal-UI"
	@echo "    - Bij schema-migration prompts: zie docs/runbook.md"
	@echo "      sectie 'Schema migrations' voor de opt-in flow"
	@echo ""

# ── Deploy marketing site (reverto.bot, static) ─────────────────────────────
# Rsyncs marketing/ to /var/www/reverto-marketing/ on the production VPS,
# sets ownership to caddy:caddy, and applies 755/644 permissions.
#
# Run this ON the VPS (the sudo chown/chmod calls are local). From
# Reverto-Dev (WSL2), SSH to the VPS first:
#     ssh bot@<vps> 'cd ~/reverto && git pull && make deploy-marketing'
#
# KRITIEK: the data/ subdirectory at /var/www/reverto-marketing/data/ is
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
	rsync -av --delete \
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
# destructive step. See docs/runbook.md section "Rollback procedure"
# for the full flow + safety notes.
rollback:
	@bash scripts/rollback.sh $(ARGS)

# ── Backup + restore — audit r1-022 ─────────────────────────────────────────
# `make backup` writes a timestamped snapshot to backups/<ts>/
# (DB + credentials + keys). Intended for cron (daily at 03:00 UTC)
# but also runnable ad-hoc. Retention: 7 days daily + 4 weeks
# weekly + 3 months monthly. See docs/runbook.md section
# "Backup and restore" for scheduling + off-host follow-ups.
#
# `make restore BACKUP=backups/<ts>` restores a specific snapshot.
# Portal must be stopped first; script takes a pre-restore
# snapshot of the current state before overwriting.
backup:
	@bash scripts/backup.sh

restore:
	@bash scripts/restore.sh $(BACKUP)

# ── Logs volgen ───────────────────────────────────────────────────────────────
log:
ifdef b
	@tail -f logs/$(b).log
else ifdef a
	@tail -f logs/audit.log
else
	@tail -f logs/portal.log
endif

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	@$(PYTHON) -m pytest tests/ -v

# ── Lint ──────────────────────────────────────────────────────────────────────
lint:
	@$(PYTHON) -m ruff check . 2>/dev/null || echo "ruff niet geinstalleerd — pip install ruff"

# ── Backtest ──────────────────────────────────────────────────────────────────
backtest:
	@$(PYTHON) main_backtest.py \
		--config config/bots/btc_backtest.yaml \
		--timeframe $(or $(tf),1h) \
		--limit $(or $(limit),1000) \
		--balance $(or $(bal),0.1)

# ── Opruimen ─────────────────────────────────────────────────────────────────
clean:
	@echo "Opruimen..."
	@find logs/pids -name "*.pid" 2>/dev/null | while read f; do \
		PID=$$(cat "$$f"); \
		kill -0 "$$PID" 2>/dev/null || (echo "  Verwijder stale PID: $$f" && rm -f "$$f"); \
	done
	@find logs -name "*.tmp" -delete 2>/dev/null && echo "  .tmp bestanden verwijderd" || true
	@find . -name "*Zone.Identifier" -delete 2>/dev/null && echo "  Zone.Identifier bestanden verwijderd" || true
	@echo "Klaar"
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
