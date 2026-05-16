# Reverto Live Trading

## Phase 1: Dry-run scaffolding

**Current status: DRY-RUN ONLY. No real orders are placed.**

`live/live_engine.py` inherits the full PaperEngine lifecycle
(indicator evaluation, DCA spacing, TP/SL monitoring, sentinels,
drawdown guard) and overrides only the order-execution surface.
Every "order" is logged to `engine.get_live_order_log()` and a
synthetic fill dict is returned so state bookkeeping stays identical
to paper mode.

## Safety rails

Reverto layers runtime guards (enforced while trading) on top of
advisory configuration warnings (surfaced at setup time). The v25
refactor removed every static configuration cap from `LiveEngine`.
The engine now boots any well-formed ladder; risk surfaces to the
operator in the wizard's Review step instead of a `ValueError`.

### Runtime guards (always on)

| Rail | Where | Effect |
|------|-------|--------|
| Per-tick DCA cap | `PaperEngine._monitor_open_deals` | At most one DCA order is placed per tick, across all open deals. Blocks flash-crash cascades that would otherwise chain 5+ DCAs on a single candle. |
| Balance guard | `PaperEngine._deduct_balance` | Every fee debit pre-checks balance; insufficient-funds logs + notifies and refuses the order instead of going negative. The real brake on a runaway ladder. |
| Drawdown guard | `core/drawdown_guard.py` + `BotConfig.drawdown_guard` | Pauses new entries (or stops the engine entirely) once equity drops `max_drawdown_pct` from peak. Peak is persisted in `state.json`. |
| Clock-skew monitor | `LiveEngine._tick` + `ClockMonitor` | Skips order placement when the local clock drifts beyond `clock_skew_tolerance` (default 5 s). Fail-open on fetch errors. |
| Liquidation guard | `core/liquidation_guard.py` | Detects positions approaching liquidation distance and emergency-closes. |
| Hard mode check | `main_live.py` rejects non-`live` configs; `main_paper.py` rejects `live` configs | A misconfigured bot can never boot under the wrong runner. |
| Dry-run default | `LiveEngine(dry_run=True)` + `main_live.py --dry-run` | Real-order path raises `NotImplementedError`. Flipping off dry-run is a deliberate opt-in. |
| Confirmation prompt | `main_live.py` stdin `y/N` | Skipped when `DRY_RUN=1` is set or `--dry-run` is on. |
| Emergency stop | Portal → `POST /api/emergency-stop` | SIGTERMs every running bot from the portal menu. |

### Configuration advisory (wizard Review step)

`POST /api/bots/validate-config` analyses a bot YAML and returns
advisory warnings + a numeric summary. The wizard renders them above
the Save button so the operator sees the ladder shape before saving.
Nothing blocks the save; the operator decides.

| Warning | Threshold | Level |
|---------|-----------|-------|
| Worst-case DCA order | `> 50×` base | high |
| Worst-case DCA order | `> 20×` base | medium |
| Cumulative position  | `> 150×` base | high |
| Cumulative position  | `> 100×` base | medium |
| Live-mode base order size | `> 0.001 BTC` | high |
| Aggressive multiplier × many orders | `multiplier ≥ 2.0 AND max_orders ≥ 8` | high |
| Live mode without drawdown guard | `mode=live AND drawdown_guard.enabled=false` | medium |

Example: `mult=1.5 × max_orders=10` (worst 38× / cumulative 113× base)
boots with no warnings. `mult=2.0 × max_orders=10` (worst 512× /
cumulative 1023× base) boots, but the wizard shows two high-severity
warnings + flags the aggressive multiplier pattern.

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

Phase 1 will refuse real orders even from this path. `LiveEngine`
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

Once triggered the guard stays triggered until the operator resets it.
There is no automatic "drawdown recovered" rebound. This is
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
