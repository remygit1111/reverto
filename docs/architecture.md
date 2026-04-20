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

## Multi-tenant foundation (Fase 1)

Reverto is voorbereid op multi-tenant deployment. Schema versie 3
introduceert een `users` tabel en elke OWNED tabel (`deals`, `orders`,
`chart_annotations`, `backtest_runs`) heeft een `user_id INTEGER NOT
NULL REFERENCES users(id)` kolom + een composite index op
`(user_id, bot_slug)`.

Voor Fase 1 draait alles op `user_id=1` (de geseede admin row). De
request-laag resolvt via `_request_user` in `web/app.py` die een
`User` instance retourneert; Fase 2 zal die lookup aan de session
cookie hangen. Het store-interface (`core/deal_store.py`) vereist
`user_id` expliciet op elke functie — er zijn geen stille defaults.
Die keuze voorkomt dat een toekomstige callsite per ongeluk cross-
user data lekt.

### Tabellen met user_id FK

| Tabel              | Index                                |
|--------------------|--------------------------------------|
| deals              | idx_deals_user_id, idx_deals_user_bot |
| orders             | idx_orders_user_id                    |
| chart_annotations  | idx_chart_annotations_user_id, idx_chart_annotations_user_bot |
| backtest_runs      | idx_backtest_runs_user_id, idx_backtest_runs_user_bot |

### Migration contract

Van pre-MT (SCHEMA_VERSION ≤ 2) naar v3 is een **destructive drop +
recreate** — een `ALTER TABLE` die een NOT NULL FK kolom toevoegt
aan een bestaande tabel met rijen is in SQLite een volledige
table-rewrite met zijn eigen failure modes. `_migrate_schema` logt
een WARNING, dropt owned tabellen in FK-safe volgorde en laat
`_SCHEMA_STATEMENTS` het v3 schema installeren. `scripts/reset_db.py`
backupt `logs/reverto.db` + elke `*.state.json` naar `.pre_mt.<ts>`
voordat de eerste boot op v3 plaatsvindt.

### User resolution (engines)

`PaperEngine.__init__` en `LiveEngine.__init__` krijgen `user_id=1`
als default; `main_paper.py` en `main_live.py` geven het expliciet
mee. Elke `deal_store` call binnen de engine gebruikt
`self.user_id`. Fase 2 zal de user afleiden uit de bot-YAML folder
(per-user directory layout) zonder verdere signature-wijzigingen.

### Credentials (Fase 1 = global)

`core/credentials.py` heeft `user_id` verplicht gemaakt maar gebruikt
het nog niet — Phase 1 deelde één `logs/credentials.json` + één
Fernet master key tussen alle users. Phase 2 (hieronder) wire't de
per-user key files + per-exchange `.enc` files echt aan.

## Multi-tenant filesystem layout (Fase 2)

Alle user-specifieke assets zijn gescoped per `user_id`. Path
construction gebeurt via `core/paths.py`; nooit hardcoded strings.

| Artefact          | Pad                                                  |
|-------------------|------------------------------------------------------|
| Bot YAML config   | `config/bots/<user_id>/<slug>.yaml`                 |
| Engine state      | `logs/<user_id>/<slug>.state.json`                  |
| Subprocess log    | `logs/<user_id>/<slug>.log`                         |
| Manual-trigger    | `logs/<user_id>/<slug>.manual_trigger`              |
| Deal sentinels    | `logs/<user_id>/<slug>.deal_{edit,close,cancel}_*`  |
| PID file          | `logs/<user_id>/pids/<slug>.pid`                    |
| Per-user Fernet   | `keys/<user_id>.key`             (chmod 0600)       |
| Per-exchange enc  | `credentials/<user_id>/<exchange>.enc`  (0600)      |

Systeem-bestanden (`logs/reverto.db`, `logs/audit.log`,
`logs/portal.log`, `logs/.credentials.key`, `logs/.auth.json`,
`logs/.api_key_ephemeral`) blijven op hun bestaande locatie —
die zijn operator/system state, niet tenant data.

### Composite bot slug

De `BotRegistry` keyt op `(user_id, slug)` in plaats van alleen
`slug`. Twee verschillende users kunnen dezelfde slug-naam
gebruiken zonder conflict — hun state/log/pid/config bestanden
leven elk onder hun eigen `<user_id>/` subdir. `BotInfo.user_id`
is het nieuwe veld dat door elke file-path helper wordt gelezen.

### Per-user Fernet key — cryptografische isolatie

`core/credentials.py` onderhoudt twee onafhankelijke key-systemen:

- **Per-user keys** (`keys/<user_id>.key`) beschermen exchange
  credentials. Elke user heeft een eigen Fernet key, dus user 2
  kan user 1's `.enc` ciphertext fundamenteel niet decrypten
  zelfs bij volledige filesystem-toegang. Dit is de primaire
  security property van Fase 2.
- **System key** (`logs/.credentials.key`) blijft bestaan voor
  `save_encrypted` / `load_encrypted` — die encrypten de portal-
  auth blob (`logs/.auth.json`). Portal-login is operator-level
  state, geen tenant data.

`rotate_fernet_key(user_id=...)` is nu per-user: rotate user 1's
key + re-encrypt zijn hele `credentials/1/` tree zonder user 2 te
raken. De commit-order (key first, .enc files second) is
ongewijzigd zodat een crash mid-rotation recoverable blijft via
de timestamped `.bak.<ts>` backup.

### Migratie contract

Van Phase-1 flat layout naar Phase-2 per-user layout:

```bash
make reset-db        # als je ook de DB wilt resetten (v3 schema)
make migrate-fs      # verplaatst bot configs + state/log/pid + credentials
make start           # portal boot, registry scans config/bots/<uid>/
```

Het migratie-script (`scripts/migrate_to_user_fs.py`) is
idempotent: een tweede run op een al-gemigreerde layout doet
niks. System files (`reverto.db`, `audit.log`, etc.) worden
nooit aangeraakt. Zie `docs/runbook.md` "Filesystem migration
(Fase 2)" voor de stap-voor-stap.


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

## Deal-ID format (post-v25)

Deals hebben een globally-unique id met format `YYYYMMDDHHMM-RRRR`
(bv. `202604201430-8421`). De ISO-prefix geeft time-sortability en
gemakkelijk debuggen ("welke deal is dit?"); de 4-digit random
suffix voorkomt collisions binnen dezelfde minuut
(10 000 mogelijkheden → 1-in-10 000 per bot per minuut).

Generatie via `core/ids.py:generate_deal_id()`. Persistence via
`core.deal_store.create_deal()` — INSERT-only, collisions raisen
`sqlite3.IntegrityError`. De retry-on-collision logic leeft in
`paper/paper_engine.py:_db_create_deal_with_retry` (max 3 attempts,
mutatie van `deal.id` in-place zodat de caller na retry de nieuwe
id gebruikt).

Edge case: NTP-backward clock correcties kunnen de
`YYYYMMDDHHMM-` prefix herhalen. De UNIQUE constraint op `deals.id`
vangt dat en de retry regenereert de suffix. De compound
probability van 3 opeenvolgende collisions is ~1e-12 — effectief
onmogelijk.

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

## Operator interventions

### wipe-deals

Complete reset van deal / order / annotation / backtest history.
Behoudt users, bot-configs, credentials.

```bash
make wipe-deals
```

Weigert te draaien als er actieve bot-subprocesses zijn (via
pid-file scan + `os.kill(pid, 0)` liveness check). Neemt een
exclusieve `fcntl.flock` op `logs/.wipe.lock` om concurrent wipes
te voorkomen — twee parallelle wipe-deals processen kunnen elkaar
niet destructief kruisen. Zie `docs/runbook.md` voor de volledige
flow + recovery.

### Log level override

Standaard loggen bot-subprocesses op INFO. Voor retrospective
DEBUG-info:

```bash
REVERTO_LOG_LEVEL=DEBUG make restart
```

Werkt voor `main_paper.py` en `main_live.py`. Portal-UI heeft een
aparte dropdown-filter per bot-log tab (ALL / WARNING+ERROR) —
dat is client-side visibility, beïnvloedt niet wat naar disk
geschreven wordt.

### Bot config import / export / duplicate

Beschikbaar via het kebab-menu (⋮) per bot-card in de portal.

- **Export** produceert YAML met een metadata-header (Reverto
  versie, export timestamp, origineel slug). Geen credentials,
  geen state, geen deal-history — puur de strategy-config.
- **Import** valideert het geüploade YAML via
  `config.models.BotConfig` (volledige Pydantic schema-check).
  Slug-conflict → 409; operator kiest een andere naam.
- **Duplicate** is server-side, schoner dan export+import
  round-trip. Ook alleen strategy; de duplicate start met lege
  state en zonder history.

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
