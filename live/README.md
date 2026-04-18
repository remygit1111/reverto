# Reverto Live Trading

## Phase 1 — Dry-run scaffolding

**Current status: DRY-RUN ONLY. No real orders are placed.**

`live/live_engine.py` inherits the full PaperEngine lifecycle —
indicator evaluation, DCA spacing, TP/SL monitoring, sentinels,
drawdown guard — and overrides only the order-execution surface.
Every "order" is logged to `engine.get_live_order_log()` and a
synthetic fill dict is returned so state bookkeeping stays identical
to paper mode.

## Safety rails

| Rail | Where | Effect |
|------|-------|--------|
| Max base order size | `LiveEngine.__init__` preflight | Refuses bots whose DCA base order size exceeds the cap (default `0.001 BTC`). Raises `ValueError` before the notify worker starts. |
| Worst-case DCA cap | `LiveEngine._preflight` (`MAX_DCA_SIZE_VS_BASE = 50`) | Refuses configs whose final DCA order exceeds `50× base_order_size`. Accepts conservative ladders (e.g. `mult=1.5 × max_orders=10` → `38×`), rejects geometric-growth explosions (e.g. `mult=2.0 × max_orders=10` → `512×`). |
| Cumulative position cap | `LiveEngine._preflight` (`DEFAULT_CUMULATIVE_MULTIPLIER = 20`) | Refuses configs whose summed base + every DCA exceeds `dca.max_cumulative_size` (or default `20× base` when unset). Catches ladders that fit per-order but blow the account across the series. |
| Drawdown guard | `core/drawdown_guard.py` + `BotConfig.drawdown_guard` | Pauses new entries (or stops the engine entirely) once equity drops `max_drawdown_pct` from peak. |
| Hard mode check | `main_live.py` rejects non-`live` configs; `main_paper.py` rejects `live` configs | A misconfigured bot can never boot under the wrong runner. |
| Dry-run default | `LiveEngine(dry_run=True)` + `main_live.py --dry-run` | Real-order path raises `NotImplementedError`. Flipping off dry-run is a deliberate opt-in. |
| Confirmation prompt | `main_live.py` stdin `y/N` | Skipped when `DRY_RUN=1` is set or `--dry-run` is on. |

## Usage

### Dry-run (recommended for now)

```bash
make live-dry BOT=<slug>
```

This sets `DRY_RUN=1`, passes `--dry-run` to `main_live.py`, and skips
the interactive confirmation so the bot can be started from a cron
job or process manager.

Direct invocation:

```bash
DRY_RUN=1 .venv/bin/python main_live.py --bot <slug> --dry-run
```

### Live launch (Phase 3+)

```bash
make live BOT=<slug>
```

Phase 1 will refuse real orders even from this path — `LiveEngine`
raises `NotImplementedError` on `dry_run=False`. The target exists
now so the operator-facing wiring (prompt, PID file, state file) is
battle-tested before Phase 3 turns real orders on.

## Drawdown guard

Opt-in per bot YAML:

```yaml
drawdown_guard:
  enabled: true
  max_drawdown_pct: 10.0
  metric: equity          # equity | balance
  action: pause           # pause | stop
```

| Field | Default | Meaning |
|-------|---------|---------|
| `enabled` | `false` | Master switch. Disabled bots see zero overhead. |
| `max_drawdown_pct` | `10.0` | Percentage drop from running peak that fires the trigger. |
| `metric` | `equity` | `equity` = balance + unrealised PnL across open deals. `balance` = realised balance only. |
| `action` | `pause` | `pause` = stop opening new deals, keep managing open ones. `stop` = halt the engine. |

Once triggered the guard stays triggered until the operator resets it
— there is no automatic "drawdown recovered" rebound. This is
deliberate: a recovery-then-bounce gap would otherwise let the engine
re-enter right into the next drawdown leg.

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| **1** | Scaffolding + dry-run + preflights + drawdown guard | **Current** |
| 2 | Dry-run parity vs paper for ≥ 2 weeks against a real exchange | Planned |
| 3 | Real order execution with minimal size + post-trade reconciliation | Planned |
| 4 | Full live trading + position reconciliation + automated fail-safes | Planned |

## What is NOT implemented yet

- Real order placement (Phase 3 wires `LiveEngine._place_market_order` to `exchange.place_market_order`)
- Position reconciliation against the exchange's reported state
- Partial fill handling
- Live credentials loading / rotation flow (see `core/credentials.py` for the read path)
- A portal UI toggle for the drawdown guard reset action

Each of these lands in the corresponding phase. Phase 1 ships the
scaffolding + safety rails so nothing real can slip through before the
rest is built.
