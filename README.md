# Reverto

BTC/USD inverse-perpetual DCA bot platform with a web portal, paper
engine, backtest engine, and a Phase-1 live-trading scaffold.

## Quick start

```bash
cd ~/reverto
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
make start          # launches the portal on :8080
```

Open [http://localhost:8080](http://localhost:8080), create a bot via
the wizard, and start it from the dashboard. The portal spawns one
`main_paper.py` subprocess per bot and keeps state in
`logs/<slug>.state.json`.

## Layout

```
core/        — shared infrastructure (SQLite ledger, guards, credentials)
paper/       — paper trading engine + in-memory state
live/        — live trading (Phase 1: dry-run scaffolding + preflights)
backtest/    — historical backtest engine with indicator groups
strategies/  — technical indicators + group evaluation
exchanges/   — ccxt wrappers (Bitget inverse swap, Kraken futures)
notifications/ — Telegram alerts
ml/          — optional ML pipeline (nightly entry-filter training)
web/         — FastAPI portal (REST + WebSocket + static UI)
tests/       — pytest suite (480+ tests, isolated SQLite per test)
notebooks/   — Jupyter exploratory analysis
scripts/     — operational CLI tools (credential rotation, etc.)
docs/        — architecture diagrams + runbook
```

## Commands

```bash
make start          # launch portal
make stop           # stop portal only (bots keep running)
make stop-all       # stop portal AND all bots
make restart        # restart portal, bots survive
make status         # show running processes
make log            # tail portal log
make log b=<slug>   # tail a bot log
make test           # run pytest
make lint           # ruff check
make backtest       # run backtest with the default config
make notebook       # launch Jupyter for notebooks/
make beep           # trigger a Telegram test notification

make live-dry BOT=<slug>   # Phase-1 dry-run of a live bot
make live BOT=<slug>       # Phase-3 real orders (refused until Phase 3 lands)
```

## Safety rails

- **DCA preflight** — live bots whose worst-case DCA exceeds 50× the
  base order or whose cumulative position exceeds 20× are refused
  at construction time.
- **Drawdown guard** — peak is persisted to `state.json` so restarts
  don't reset the kill-switch baseline.
- **Balance guard** — every fee debit pre-checks balance; insufficient
  funds logs + notifies instead of silently going negative.
- **Hard mode checks** — `main_paper.py` only accepts `mode: paper`,
  `main_live.py` only accepts `mode: live`. No cross-boot possible.
- **Emergency stop** — `POST /api/emergency-stop` + portal-menu button
  SIGTERMs every running bot with a confirmation prompt.
- **Idempotent order retries** — Bitget `place_*_order` injects a
  `clientOrderId` and checks the exchange for an existing order
  before retrying, closing the "rate-limited on confirmation →
  duplicate" race.

## Monitoring

- `GET /healthz` — liveness probe (200 OK, no auth, no rate-limit).
- `GET /readyz` — readiness probe; 503 when the SQLite ledger is
  unreachable.
- `GET /metrics` — Prometheus scrape (no auth). See `web/metrics.py`
  for the full metric catalogue.

## Documentation

- [Live Trading](live/README.md) — phases, dry-run usage, safety rails.
- [ML Pipeline](ml/README.md) — nightly training, notebook analysis.
- [Architecture](docs/architecture.md) — process model + tick flow.
- [Runbook](docs/runbook.md) — startup checklist, emergency procedures,
  credential rotation, common error fixes.
- [Deployment](docs/deployment.md) — bare-metal + Docker + Kubernetes.
- [Alert rules](docs/alerts.yml) — Prometheus alert template.
