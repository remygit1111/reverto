# Makefile — Reverto
# Gebruik: make <target>
# Vereist: GNU make, .venv aanwezig

PYTHON  := .venv/bin/python3
PORTAL  := logs/pids/portal.pid

.PHONY: help start stop restart status log test lint clean backtest

# ── Standaard target ──────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  REVERTO — beschikbare commando's"
	@echo ""
	@echo "  make start           Start het portal op de achtergrond"
	@echo "  make stop            Stop portal en alle bots"
	@echo "  make restart         Stop en herstart het portal"
	@echo "  make status          Toon welke processen draaien"
	@echo "  make log             Volg de portal log live (Ctrl+C om te stoppen)"
	@echo "  make log b=naam      Volg de log van een specifieke bot"
	@echo "  make test            Voer alle pytest tests uit"
	@echo "  make lint            Controleer code met ruff"
	@echo "  make backtest        Voer backtest uit met standaard config"
	@echo "  make backtest tf=4h  Backtest op 4h candles"
	@echo "  make clean           Verwijder stale PID bestanden en .tmp state files"
	@echo ""

# ── Portal start/stop/restart ─────────────────────────────────────────────────
start:
	@bash start.sh

stop:
	@bash stop.sh

restart: stop
	@sleep 1
	@bash start.sh

status:
	@bash status.sh

# ── Logs volgen ───────────────────────────────────────────────────────────────
log:
ifdef b
	@tail -f logs/$(b).log
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
