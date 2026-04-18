# Reverto Architecture

## Process Model (post-v22 refactor)

```
┌──────────────────────────────────────────────────┐
│  Portal (main_web.py → web/app.py)               │
│  ├── Middleware: Auth → SecurityHeaders          │
│  ├── Lifespan: state watcher, log tailer         │
│  ├── WebSockets: /ws/logs/{slug}, /ws/state      │
│  ├── Routes: web/routes/*.py (8 modules, v22)    │
│  └── BotRegistry — filesystem scan config/bots/  │
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
│ + StateIO (v22)  │    │ + ClockMonitor   │
│                  │◄───┤ + OrderReconcile │
│  - tick loop     │    │                  │
│  - DCA per-tick  │    │  - dry-run log   │
│  - TP/SL checks  │    │  - skew gate     │
│  - sentinels     │    │  - reconcile/N   │
│  - guards:       │    └──────────────────┘
│    liquidation   │
│    drawdown      │     (config caps removed v25 —
│    schedule      │      wizard advisory warnings
│    balance       │      via /validate-config)
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

## Web layer structure (post-v22 refactor)

```
web/
├── app.py                 ← FastAPI app + middleware + WS + shared helpers
│                             (1447 regels, was 2374 pre-v22)
├── metrics.py             ← Prometheus metrics + classify_error
├── templates/
│   └── index.html         ← SPA shell
├── static/
│   ├── app.js             ← Client-side logic
│   └── style.css          ← Themed styling + print styles
└── routes/                ← Route modules (extracted in v22)
    ├── __init__.py
    ├── admin.py           ← /healthz /readyz /metrics
    │                        /api/emergency-stop
    │                        /api/portal/*
    ├── auth.py            ← /auth/login /auth/logout
    │                        /auth/status /api/auth/change-password
    ├── backtest.py        ← /api/backtest/*
    ├── bots.py            ← /api/bots/*
    │                        (CRUD + start/stop/restart)
    ├── chart.py           ← /api/price /api/chart /api/candles
    ├── deals.py           ← /api/bots/{slug}/deals/*
    │                        /api/db/deals /api/db/stats
    │                        /api/db/annotations
    ├── drawdown.py        ← /api/bots/{slug}/drawdown/reset
    └── exchanges.py       ← /api/exchanges/*
```

**Design principes:**

- Elke route module is onafhankelijk van andere route modules —
  zero cross-imports binnen `web/routes/`.
- Alle modules importeren gedeelde state (limiter, registry,
  session helpers, `_BOT_SLUG_RE`, ...) alleen uit `web.app`.
- Circulair-import patroon werkt door `include_router()` aan te
  roepen aan de bodem van `web/app.py` — op dat moment zijn alle
  module-level namen in `web.app` al gedefinieerd.
- WebSocket endpoints (`/ws/logs/{slug}`, `/ws/state`) blijven
  in `web/app.py` omdat `include_router` niet cleanly met
  `BaseHTTPMiddleware` + async WS auth samenwerkt.

## Persistence layer (post-v22 refactor)

```
paper/
├── paper_engine.py        ← Engine orchestrator + tick loop
│                             (1397 regels, was 1542 pre-v22)
├── paper_state.py         ← PaperState, PaperDeal, PaperOrder
└── state_io.py            ← StateIO class (NEW v22)
    ├── load()             — met orphan .tmp cleanup
    ├── write()            — atomic tmp + replace
    ├── mark_stopped()     — preserveert overige velden
    ├── cleanup_orphan_tmps()
    └── deal_to_dict / dict_to_deal
```

`StateIO` is verantwoordelijk voor alle `state.json` persistence.
`PaperEngine._load_state` / `_write_state` / `_clear_state` delegeren
naar `self._state_io`. Per-bot file isolation maakt locking overbodig
(een bot heeft altijd maar één writer).

Backwards-compat: `paper.paper_engine` re-exporteert `_deal_to_dict`
en `_dict_to_deal` als aliasen zodat bestaande tests de oude import-
paden kunnen blijven gebruiken.

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
- **New route domain**: add `web/routes/<domain>.py` with `router =
  APIRouter(tags=[...])`, register routes with `@router.get/post/...`
  + `@limiter.limit(...)` + `Depends(_request_actor)` where auth
  required. At the bottom of `web/app.py`, add `from web.routes import
  <domain> as _<domain>_routes` + `app.include_router(_<domain>_routes.router)`.
  Follow the existing patterns — see `web/routes/exchanges.py` for the
  minimal 3-endpoint template.

## Refactor roadmap — bewust uitgesteld

- **paper_engine.py TickLoop/DealMonitor extract** — overwogen v22,
  uitgesteld. Alle tick-gerelateerde logica zit nu in één klasse.
  Splitsen zou extract vereisen van `_monitor_open_deals`, `_check_entry`,
  `_check_dca`, `_check_tp`, `_check_sl` — hoog risico op state-
  synchronisatie bugs zonder directe architecturale winst. Herbeoordelen
  wanneer de file boven 2000 regels groeit.
- **_close_deal_at_price DRY refactor** — TP en SL branches hebben
  structureel verschillende flows (SL trailing peak, TP indicator groups).
  Refactor-risico > winst.
- **Wick-slippage cap** — verandert PnL semantiek; bestaande tests
  verankeren het huidige gedrag. Zinvol bij Phase 3 wanneer paper/live
  divergence via echte slippage gemeten kan worden.
- **Decimal precision voor DCA sizing** — Phase 3 blocker voor exchange
  fill reconciliation. Exchange min-qty rounding compenseert in de
  praktijk de float-drift tot dan.
- **web/app.py verder splitsen** — 1447 regels resteren (middleware +
  lifespan + shared helpers + WS + BotRegistry + auth-primitives). Verder
  extract denkbaar (session helpers → `web/auth_primitives.py`, WS →
  `web/websockets.py`), maar coupling met middleware maakt dit niet
  triviaal. Niet urgent na v22 36% reductie.
