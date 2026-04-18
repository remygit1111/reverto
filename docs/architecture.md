# Reverto Architecture

## Process Model

```
┌──────────────────────────────────────────────────┐
│  Portal  (main_web.py)                           │
│  • FastAPI app (web/app.py)                      │
│  • BotRegistry — filesystem scan of config/bots/ │
│  • subprocess.Popen per bot start                │
│  • State-file poller for WebSocket push          │
└────────────────┬────────────────────────────────-┘
                 │ subprocess.Popen (SIGTERM to stop)
      ┌──────────┴──────────┐
      ▼                     ▼
┌────────────────┐    ┌────────────────┐
│ main_paper.py  │    │ main_live.py   │
│  (per bot)     │    │  (per bot)     │
│                │    │                │
│  - arg parse   │    │  - arg parse   │
│  - slug regex  │    │  - slug regex  │
│  - mode guard  │    │  - mode guard  │
│  - SIGTERM hdlr│    │  - confirm()   │
│                │    │  - auth excha. │
└───────┬────────┘    └───────┬────────┘
        │ constructs           │ constructs
        ▼                      ▼
┌──────────────────┐    ┌──────────────────┐
│ PaperEngine      │    │ LiveEngine       │
│ (paper/          │    │ (live/           │
│  paper_engine.py)│◄───┤  live_engine.py) │
│                  │    │                  │
│  - tick loop     │    │  - inherits all  │
│  - DCA caps      │    │  - DCA preflight │
│  - TP/SL checks  │    │  - dry-run log   │
│  - sentinels     │    │  - order reconc. │
│  - guards:       │    │  - clock monitor │
│    liquidation   │    └──────────────────┘
│    drawdown      │
│    schedule      │
└────────┬─────────┘
         │ writes
         ▼
┌──────────────────────────────────────┐
│  Shared state                         │
│  • logs/<slug>.state.json (atomic)    │
│  • logs/reverto.db (SQLite + WAL)     │
│  • logs/<slug>.manual_trigger, ...    │
└──────────────────────────────────────┘
```

## Tick flow

```
engine.start()
  └── while self.running:
        _tick()
         ├── Prometheus: record_tick() + tick_duration_seconds.time()
         ├── exchange.get_ticker() → price
         ├── guard.is_open()  [schedule]
         ├── _fetch_closes_if_needed()  [per-TF cache]
         ├── _refresh_wick_candle()
         ├── _monitor_open_deals(price)
         │     ├── _check_tp()
         │     ├── _check_sl()
         │     └── _check_dca()   [cap: MAX_DCA_PER_TICK]
         ├── _update_liq_guard()
         ├── _update_drawdown_guard()
         │     └── if triggered: pause / stop
         ├── _check_manual_trigger()  [portal sentinels]
         ├── _check_deal_sentinels()   [edit / close / cancel]
         ├── if schedule_open AND not paused_by_drawdown:
         │     _check_entry()  ─── _open_deal() on signal
         └── _write_state(price, is_open)   [atomic tmp+rename]
```

## State file lifecycle

`logs/<slug>.state.json` is the shared contract between the engine
subprocess and the portal. It's rewritten after every tick via
tmp-then-rename so a SIGKILL can never leave the UI reading a
half-written file. On startup, orphan `<slug>.state.*.tmp` files are
swept by `_load_state` before any deal hydration.

| Field                | Purpose                                         |
|----------------------|-------------------------------------------------|
| `running`            | Portal uses this to differentiate live vs dead  |
| `balance_btc`        | Current balance; restored on restart            |
| `fees_paid_btc`      | Cumulative fees for dashboard                   |
| `open_deals[]`       | Full deal serialisation — orders + peak + trig  |
| `closed_deals[]`     | Last N (CLOSED_DEALS_UI_CAP) for the UI         |
| `drawdown_guard`     | Peak + triggered + reason — **persisted**       |
| `paused_by_drawdown` | Blocks new entries until operator resets        |

## Key inter-module contracts

- **exchanges.base_exchange** — the `BaseExchange` ABC is the single
  interface every engine talks to. New exchanges subclass it; engines
  remain framework-agnostic.
- **core.drawdown_guard.DrawdownGuard** — engines feed equity or
  balance into `update()`; the guard returns True on trigger and the
  engine decides `pause` vs `stop`. `to_dict()`/`from_dict()` let the
  peak survive restarts.
- **core.credentials** — Fernet-encrypted at rest. `rotate_fernet_key`
  mass re-encrypts every credential atomically (see runbook).
- **live.order_reconciliation** — tracks pending orders + timeouts.
  Scaffolding only in Phase 1; Phase 3 wires the `fetch_order` polling
  branch.
- **web.metrics** — every counter / gauge / histogram is defined in
  one place. Engines instrument via `record_tick`, `set_balance`, etc.

## Extension points

- **New exchange**: subclass `BaseExchange`, register in
  `exchanges.public_exchange.CLIENTS`, add CCXT symbol map. ~150 LoC.
- **New indicator**: add module under `strategies/indicators/`,
  implement `check_<name>_signal`, register in `IndicatorEngine`.
- **New guard**: follow the `DrawdownGuard` shape — `update()`,
  `is_triggered`, `to_dict`/`from_dict`. Wire into `_tick` after the
  existing guards so it observes the post-monitor state.
