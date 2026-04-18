# Makefile — Reverto
# Gebruik: make <target>
# Vereist: GNU make, .venv aanwezig

PYTHON  := .venv/bin/python3
PORTAL  := logs/pids/portal.pid

.PHONY: help setup start stop stop-all restart status log test lint clean backtest notebook beep live live-dry parity-compare

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
