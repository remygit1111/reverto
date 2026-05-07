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
│                             (1447 lines, was 2374 pre-v22)
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

**Design principles:**

- Each route module is independent of other route modules —
  zero cross-imports inside `web/routes/`.
- All modules import shared state (limiter, registry, session
  helpers, `_BOT_SLUG_RE`, ...) only from `web.app`.
- The circular-import pattern works by calling `include_router()`
  at the bottom of `web/app.py` — at that point all module-level
  names in `web.app` are already defined.
- WebSocket endpoints (`/ws/logs/{slug}`, `/ws/state`) stay in
  `web/app.py` because `include_router` does not work cleanly
  with `BaseHTTPMiddleware` + async WS auth.

## Multi-tenant foundation (Phase 1)

Reverto is prepared for multi-tenant deployment. Schema version 3
introduces a `users` table, and every OWNED table (`deals`, `orders`,
`chart_annotations`, `backtest_runs`) has a `user_id INTEGER NOT
NULL REFERENCES users(id)` column plus a composite index on
`(user_id, bot_slug)`.

For Phase 1 everything runs on `user_id=1` (the seeded admin row).
The request layer resolves via `_request_user` in `web/app.py`,
which returns a `User` instance; Phase 2 will tie that lookup to
the session cookie. The store interface (`core/deal_store.py`)
requires `user_id` explicitly on every function — there are no
silent defaults. That choice prevents a future call site from
accidentally leaking cross-user data.

### Tables with a user_id FK

| Table              | Index                                |
|--------------------|--------------------------------------|
| deals              | idx_deals_user_id, idx_deals_user_bot |
| orders             | idx_orders_user_id                    |
| chart_annotations  | idx_chart_annotations_user_id, idx_chart_annotations_user_bot |
| backtest_runs      | idx_backtest_runs_user_id, idx_backtest_runs_user_bot |

### Migration contract

From pre-MT (SCHEMA_VERSION ≤ 2) to v3 is a **destructive drop +
recreate** — an `ALTER TABLE` that adds a NOT NULL FK column to an
existing table with rows is, in SQLite, a full table rewrite with
its own failure modes. `_migrate_schema` logs a WARNING, drops
owned tables in FK-safe order, and lets `_SCHEMA_STATEMENTS`
install the v3 schema. `scripts/reset_db.py` backs up
`logs/reverto.db` and every `*.state.json` to `.pre_mt.<ts>`
before the first boot on v3.

### User resolution (engines)

`PaperEngine.__init__` and `LiveEngine.__init__` receive
`user_id=1` as default; `main_paper.py` and `main_live.py` pass
it explicitly. Every `deal_store` call inside the engine uses
`self.user_id`. Phase 2 will derive the user from the bot-YAML
folder (per-user directory layout) without further signature
changes.

### Credentials (Phase 1 = global)

`core/credentials.py` requires `user_id` but does not yet use it
— Phase 1 shared one `logs/credentials.json` + one Fernet master
key between all users. Phase 2 (below) actually wires up the
per-user key files and per-exchange `.enc` files.

## Multi-tenant filesystem layout (Phase 2)

All user-specific assets are scoped per `user_id`. Path
construction goes through `core/paths.py`; never hardcoded strings.

| Artefact          | Path                                                 |
|-------------------|------------------------------------------------------|
| Bot YAML config   | `config/bots/<user_id>/<slug>.yaml`                 |
| Engine state      | `logs/<user_id>/<slug>.state.json`                  |
| Subprocess log    | `logs/<user_id>/<slug>.log`                         |
| Manual-trigger    | `logs/<user_id>/<slug>.manual_trigger`              |
| Deal sentinels    | `logs/<user_id>/<slug>.deal_{edit,close,cancel}_*`  |
| PID file          | `logs/<user_id>/pids/<slug>.pid`                    |
| Per-user Fernet   | `keys/<user_id>.key`             (chmod 0600)       |
| Per-exchange enc  | `credentials/<user_id>/<exchange>.enc`  (0600)      |

System files (`logs/reverto.db`, `logs/audit.log`,
`logs/portal.log`, `logs/.credentials.key`,
`logs/.api_key_ephemeral`) stay in their existing location — they
are operator/system state, not tenant data. Phase-3a removed
`logs/.auth.json` from the runtime paths; on the first
`init_db()` it is automatically archived to
`.auth.json.pre_phase3.<ts>`.

### Composite bot slug

The `BotRegistry` keys on `(user_id, slug)` instead of just
`slug`. Two different users can use the same slug name without
conflict — their state/log/pid/config files each live under their
own `<user_id>/` subdir. `BotInfo.user_id` is the new field that
every file-path helper reads.

### Per-user Fernet key — cryptographic isolation

`core/credentials.py` maintains two independent key systems:

- **Per-user keys** (`keys/<user_id>.key`) protect exchange
  credentials. Each user has their own Fernet key, so user 2
  fundamentally cannot decrypt user 1's `.enc` ciphertext even
  with full filesystem access. This is the primary security
  property of Phase 2.
- **System key** (`logs/.credentials.key`) remains for
  `save_encrypted` / `load_encrypted` — generic Fernet helpers
  for any system-level encrypted files outside the
  exchange-credentials scope. Phase-3a deprecated the portal-auth
  blob (`.auth.json`); password_hash + session_epoch now live in
  `users` (DB-backed, via `core.user_store`).

`rotate_fernet_key(user_id=...)` is now per-user: rotate user 1's
key + re-encrypt their entire `credentials/1/` tree without
touching user 2. The commit order (key first, .enc files second)
is unchanged so a crash mid-rotation remains recoverable via the
timestamped `.bak.<ts>` backup.

### Migration contract

From Phase-1 flat layout to Phase-2 per-user layout:

```bash
make reset-db        # if you also want to reset the DB (v3 schema)
make migrate-fs      # moves bot configs + state/log/pid + credentials
make start           # portal boots, registry scans config/bots/<uid>/
```

The migration script (`scripts/migrate_to_user_fs.py`) is
idempotent: a second run on an already-migrated layout is a
no-op. System files (`reverto.db`, `audit.log`, etc.) are never
touched. See `docs/OPERATIONS.md` "Filesystem migration
(Phase 2)" for the step-by-step.


## Persistence layer (post-v22 refactor)

```
paper/
├── paper_engine.py        ← Engine orchestrator + tick loop
│                             (1397 lines, was 1542 pre-v22)
├── paper_state.py         ← PaperState, PaperDeal, PaperOrder
└── state_io.py            ← StateIO class (NEW v22)
    ├── load()             — with orphan .tmp cleanup
    ├── write()            — atomic tmp + replace
    ├── mark_stopped()     — preserves the other fields
    ├── cleanup_orphan_tmps()
    └── deal_to_dict / dict_to_deal
```

`StateIO` is responsible for all `state.json` persistence.
`PaperEngine._load_state` / `_write_state` / `_clear_state`
delegate to `self._state_io`. Per-bot file isolation makes
locking unnecessary (a bot always has only one writer).

Backwards-compat: `paper.paper_engine` re-exports `_deal_to_dict`
and `_dict_to_deal` as aliases so existing tests can keep using
the old import paths.

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

Deals have a globally-unique id with format `YYYYMMDDHHMM-RRRR`
(e.g. `202604201430-8421`). The ISO prefix gives time-sortability
and easy debugging ("which deal is this?"); the 4-digit random
suffix prevents collisions inside the same minute (10 000
possibilities → 1-in-10 000 per bot per minute).

Generation via `core/ids.py:generate_deal_id()`. Persistence via
`core.deal_store.create_deal()` — INSERT-only, collisions raise
`sqlite3.IntegrityError`. The retry-on-collision logic lives in
`paper/paper_engine.py:_db_create_deal_with_retry` (max 3
attempts, mutating `deal.id` in place so the caller uses the new
id after a retry).

Edge case: NTP-backward clock corrections can repeat the
`YYYYMMDDHHMM-` prefix. The UNIQUE constraint on `deals.id`
catches that and the retry regenerates the suffix. The compound
probability of 3 consecutive collisions is ~1e-12 — effectively
impossible.

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

Complete reset of deal / order / annotation / backtest history.
Preserves users, bot configs, credentials.

```bash
make wipe-deals
```

Refuses to run when there are active bot subprocesses (via
pid-file scan + `os.kill(pid, 0)` liveness check). Takes an
exclusive `fcntl.flock` on `logs/.wipe.lock` to prevent
concurrent wipes — two parallel wipe-deals processes cannot
destructively interleave. See `docs/OPERATIONS.md` for the full
flow + recovery.

### Log level override

By default, bot subprocesses log at INFO. For retrospective DEBUG
info:

```bash
REVERTO_LOG_LEVEL=DEBUG make restart
```

Works for `main_paper.py` and `main_live.py`. The portal UI has a
separate dropdown filter per bot-log tab (ALL / WARNING+ERROR) —
that is client-side visibility, it does not affect what is
written to disk.

### Bot config import / export / duplicate

Available via the kebab menu (⋮) on each bot card in the portal.

- **Export** produces YAML with a metadata header (Reverto
  version, export timestamp, original slug). No credentials, no
  state, no deal history — purely the strategy config.
- **Import** validates the uploaded YAML via
  `config.models.BotConfig` (full Pydantic schema check).
  Slug conflict → 409; operator picks another name.
- **Duplicate** is server-side, cleaner than an export+import
  round-trip. Also strategy-only; the duplicate starts with
  empty state and no history.

### Admin provisioning (post-Phase-3a)

On a fresh install or after a destructive schema migration (v<4
→ v4, or future similar bumps), `users.password_hash` for the
seeded admin row is `NULL`. Login is blocked until
`scripts/setup_admin.py` has been run — typically via:

```bash
REVERTO_ADMIN_PW="<password>" make setup-admin
```

The script writes a bcrypt hash (rounds=12) to
`users.password_hash` for user_id=1. Without this step every
login returns 401 because `verify_password()` in
`core/user_store.py` fails closed on a NULL hash (see
`docs/security-model.md` Part 3.3).

Destructive schema migrations themselves have, since audit v26-10
(2026-04-20), required an explicit operator opt-in via
`REVERTO_DESTRUCTIVE_MIGRATE=1` on `make start`, with an
auto-generated pre-migration backup at
`logs/pre-migration-backup-YYYYMMDD-HHMMSS.db`. See
`docs/OPERATIONS.md` "Schema migrations" for the full flow +
restore procedure.

## Authentication architecture (Phase B)

Reverto's authentication is a layered stack. Layer 1 ships at every
deploy; Layer 2 is opt-in per user; Layer 3 binds the result to a
session cookie that the rest of the portal validates on every
request.

### Layer 1 — Password authentication

- Bcrypt-hashed passwords (rounds=12) stored in
  `users.password_hash`.
- Constant-time verification via `bcrypt.checkpw`. The unknown-user
  / NULL-hash / inactive-user branches all run a dummy
  `bcrypt.checkpw` against `_DUMMY_BCRYPT_HASH` so the wall-time of
  a failed login does not leak whether the username exists
  (audit pt-101 closure).
- Per-user rate limiting: 10 failed attempts inside a 15-minute
  sliding window flips the account to a 429 with a rounded
  `Retry-After` header. Backoff is exponential (`0.1 * 2^count`,
  capped at 30 s) so a typo pays 0.1 s but a campaign escalates.
- HIBP Pwned-Passwords k-anonymity check on password change blocks
  known-breached passwords without leaking the new password to the
  network.

### Layer 2 — Two-factor authentication (TOTP, opt-in)

Optional second factor for users who enable it via
`POST /auth/totp/setup` → scan QR → `POST /auth/totp/verify`. The
seed lives encrypted at rest with the user's Fernet key (consistent
with the exchange-credential pattern, Phase 2 per-user filesystem).

- RFC 6238 time-based one-time passwords (`pyotp`).
- 30-second window with ±1-window skew tolerance.
- 160-bit secrets (32-character base32).
- Per-user Fernet encryption at rest in
  `users.totp_seed_encrypted`.

**Enrollment flow:**
1. Authenticated user `POST /auth/totp/setup` → server generates a
   fresh secret, server-renders an SVG QR (no CDN-loaded JS QR
   library — see `docs/security-model.md` for the supply-chain
   rationale), and sets a 10-min uid-bound pending-state cookie.
2. User scans the QR with an authenticator app.
3. `POST /auth/totp/verify` with the first 6-digit code.
4. On success the encrypted secret is committed to
   `users.totp_seed_encrypted` and the pending cookie is cleared.

**Login flow with TOTP enabled:**
1. `POST /auth/login` with username + password.
2. If valid, the response carries `requires_totp: true` and the
   server sets a 2-min pending-login-TOTP cookie. No session cookie
   is issued yet.
3. `POST /auth/login/totp` with the 6-digit code.
4. On success the full session cookie is issued and the failed-
   login counter is reset (the reset moved here from
   `/auth/login` in Phase B PR 4 so a password-cracker who fails
   the TOTP step cannot reset the counter for free).

**Disable flow:**
Requires BOTH the current password AND a current valid TOTP code
— a stolen session alone or a stolen device alone is insufficient.
For operator-side recovery when a user has lost the authenticator
app, see `docs/runbook.md` "TOTP recovery".

### Layer 3 — Session cookies

- HttpOnly + Secure + SameSite=Strict.
- Signed via `itsdangerous.URLSafeTimedSerializer` with a
  per-purpose salt so the three cookie types cannot be replayed
  across each other.
- Three cookie types:
  - **session** (24h TTL) — issued on full login success.
  - **pending-totp-enrollment** (10 min, uid-bound) — issued by
    `/auth/totp/setup`, consumed by `/auth/totp/verify`.
  - **pending-login-totp** (2 min) — issued by `/auth/login` when
    `user.totp_enabled`, consumed by `/auth/login/totp`.
- Validation on every request (`web.app._request_user`) checks: a
  valid signature, the cookie's claimed user_id resolves to an
  active row, and the cookie's session_epoch matches the row's
  current epoch. The epoch counter lets logout / password-change
  invalidate every cookie for that user atomically. See
  `docs/security-model.md` §6.4 for the full bump-vs-no-bump
  matrix.

### Files

- Endpoint logic: `web/routes/auth.py`
- TOTP helpers: `core/totp.py`
- User + auth store: `core/user_store.py`
- Cookie management + session validation: `web/app.py`

Detailed design rationale + threat model:
`docs/security-model.md` Part 3.3 + Part 6.

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

## Refactor roadmap — deliberately deferred

- **paper_engine.py TickLoop/DealMonitor extract** — considered in
  v22, deferred. All tick-related logic sits in one class today.
  Splitting would require extracting `_monitor_open_deals`,
  `_check_entry`, `_check_dca`, `_check_tp`, `_check_sl` — high
  risk of state-synchronisation bugs without direct architectural
  gain. Revisit when the file grows past 2000 lines.
- **_close_deal_at_price DRY refactor** — TP and SL branches have
  structurally different flows (SL trailing peak, TP indicator
  groups). Refactor risk > gain.
- **Wick-slippage cap** — changes PnL semantics; existing tests
  anchor the current behaviour. Worthwhile in Phase 3 once
  paper/live divergence can be measured against real slippage.
- **Decimal precision for DCA sizing** — Phase 3 blocker for
  exchange fill reconciliation. Exchange min-qty rounding
  compensates for the float drift in practice until then.
- **Split web/app.py further** — 1447 lines remain (middleware +
  lifespan + shared helpers + WS + BotRegistry + auth primitives).
  Further extraction is conceivable (session helpers →
  `web/auth_primitives.py`, WS → `web/websockets.py`), but
  coupling with middleware makes this non-trivial. Not urgent
  after the v22 36% reduction.
