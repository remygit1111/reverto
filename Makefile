# Makefile — Reverto
# Gebruik: make <target>
# Vereist: GNU make, .venv aanwezig

PYTHON  := .venv/bin/python3
PORTAL  := logs/pids/portal.pid

.PHONY: help setup start stop restart status log test lint clean backtest

# ── Standaard target ──────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  REVERTO — beschikbare commando's"
	@echo ""
	@echo "  make setup           Maak standaard bot YAML bestanden aan (idempotent)"
	@echo "  make start           Start het portal op de achtergrond"
	@echo "  make stop            Stop portal en alle bots"
	@echo "  make restart         Stop en herstart het portal"
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

# ── Setup: maak default YAML bot configs aan ─────────────────────────────────
# config/bots/*.yaml staat in .gitignore zodat persoonlijke keys/tuning
# niet per ongeluk gecommit worden. Dit target genereert de standaard
# configs voor een fresh clone. Bestaande files worden NIET overschreven.

define BTC_PAPER_YAML
bot:
  name: "BTC-DCA-Paper"
  mode: paper
  exchange: bitget
  pair: BTC/USD
  contract_type: inverse_perpetual

  leverage:
    enabled: false
    size: 1

  dca:
    base_order_size: 0.001
    max_orders: 5
    order_spacing_pct: 2.5
    multiplier: 1.5
    taker_fee: 0.0006

  entry:
    indicators:
      - type: RSI
        period: 14
        threshold: below_35

  take_profit:
    target_pct: 3.0

  stop_loss:
    type: trailing
    pct: 6.0

  ml:
    enabled: false

  schedule:
    timezone: "Europe/Amsterdam"
    trading_windows:
      - days: [mon, tue, wed, thu, fri]
        from: "09:00"
        to: "23:00"
      - days: [sat]
        from: "10:00"
        to: "20:00"

  telegram:
    notify_on: [startup, shutdown, entry, dca_trigger, tp_hit, sl_hit, liquidation_warn, schedule_open, schedule_close, error]
endef
export BTC_PAPER_YAML

define BTC_BACKTEST_YAML
bot:
  name: "BTC-DCA-Backtest"
  mode: backtest
  exchange: bitget
  pair: BTC/USD
  contract_type: inverse_perpetual

  leverage:
    enabled: false
    size: 1

  dca:
    base_order_size: 0.001
    max_orders: 5
    order_spacing_pct: 2.5
    multiplier: 1.5
    taker_fee: 0.0006

  entry:
    indicators:
      - type: RSI
        period: 14
        threshold: below_35

  take_profit:
    target_pct: 3.0

  stop_loss:
    type: trailing
    pct: 6.0

  ml:
    enabled: false

  schedule:
    timezone: "Europe/Amsterdam"
    trading_windows: []
    blackout_dates: []

  telegram:
    notify_on: []
endef
export BTC_BACKTEST_YAML

setup:
	@mkdir -p config/bots
	@if [ ! -f config/bots/btc_paper.yaml ]; then \
		printf '%s\n' "$$BTC_PAPER_YAML" > config/bots/btc_paper.yaml; \
		echo "  created config/bots/btc_paper.yaml"; \
	else \
		echo "  skipped config/bots/btc_paper.yaml (already exists)"; \
	fi
	@if [ ! -f config/bots/btc_backtest.yaml ]; then \
		printf '%s\n' "$$BTC_BACKTEST_YAML" > config/bots/btc_backtest.yaml; \
		echo "  created config/bots/btc_backtest.yaml"; \
	else \
		echo "  skipped config/bots/btc_backtest.yaml (already exists)"; \
	fi
	@echo "Setup klaar"

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
