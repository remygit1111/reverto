# Reverto scaling audit — May 2026

## Executive summary

Reverto today is a single-process FastAPI + SQLite + per-bot subprocess
stack on a single Hetzner VPS (2 vCPU AMD EPYC, 3.7 GiB RAM, 38 GiB SSD).
At the time of measurement: 1 active user, 1 paper bot running, portal
RSS 207 MiB, bot RSS 164 MiB, DB 408 KiB + 4.0 MiB WAL, total `logs/`
disk 7.8 MiB. The architecture is solid for the current operator and for
the first 10–20 users; the dominant scaling axis is **per-bot RAM**, not
DB size or request rate. At ~150 MiB private RSS per bot and ~2.5 bots
per user, the current VPS exhausts RAM somewhere around 18–22 paying
users.

Top three priorities, in order: (1) move from `subprocess`-per-bot to
an in-process bot runner (eliminates the per-bot interpreter overhead
and is the single largest unlock for tiers 2–4); (2) cap per-user
resources (max bots, log rotation at the engine, request-body limit)
because every "no limit" today is a future DoS vector once registration
opens; (3) plan the SQLite → Postgres + Redis + multi-worker shift for
tier 4, but do not pull it forward — the existing `r1-024/-025/-026/-027`
findings already document the exact code-paths that must change. Biggest
surprise: the codebase is **substantially more multi-tenant-ready** than
the operator may realise — `_request_user`, per-user paths, per-user
Fernet keys, per-user rate-limit keys, per-owner WS broadcasts, and
`user_id` FKs on every owned table are all in place. The bottleneck is
mostly subprocess RAM, not data-model rework.

## Methodology

- **Phase 1 — Live measurements (read-only)** on `reverto-prod-01` against
  the running portal (PID 80159) and one bot (PID 30564, `rsi_paper_test`,
  user_id=1, ~5 days uptime). Commands used: `ps`, `ss`, `df`, `free`,
  `du`, `wc -l`, `sqlite3 SELECT COUNT(*)`, `PRAGMA`-free reads via
  `sqlite_master` / `dbstat`. No writes, no synthetic load, no
  test-row inserts. The DB was queried while the WAL was active
  (4.0 MiB), which is normal under SQLite WAL — the figures below
  use the main-file size for "size at rest".
- **Phase 2 — Static code analysis** via `grep -rn` + targeted `Read`
  on `web/app.py`, `core/database.py`, `core/deal_store.py`,
  `web/routes/*`, `exchanges/*`, `ml/nightly_pipeline.py`,
  `ops/caddy/Caddyfile`, `Makefile`, `start.sh`. Cross-referenced
  against the existing `audit_findings` rows (352 total, 40+ scaling-
  relevant) so this document stays aligned with prior pentest /
  retro work.
- **Phase 3 — Mathematical projection** using Phase-1 baselines × the
  per-user assumptions specified in the audit brief (2–3 bots/user,
  2–4 WS/user, ~120 req/h, etc.). No load test was run on production.
  Where a number is projected rather than measured, it is flagged
  ("not measured, projected from X").

**Limitations** — no synthetic bots were started, so per-bot RAM /
broadcast / DB-write costs are projected from a single sample. Real-
world variance can be ±30–50 % depending on instrument count, deal
density and ccxt cache size. The numbers below are **planning
estimates, not guarantees**.

## Current state baseline (measured)

### VPS

| Item                    | Value                                       |
|-------------------------|---------------------------------------------|
| CPU                     | 2 × AMD EPYC-Rome (shared)                  |
| RAM                     | 3.7 GiB total, 1.2 GiB used, 2.1 GiB cache  |
| Swap                    | 0 B                                         |
| Disk (`/`)              | 38 GiB, 3.5 GiB used (10 %)                 |
| Uptime                  | 8 d 9 h, load average 0.05 / 0.09 / 0.04    |
| Provider tier (closest) | Hetzner CPX21 (3 vCPU shared, 4 GB, 80 GB)  |

### Reverto processes

| Item                              | Value                              |
|-----------------------------------|------------------------------------|
| Portal RSS / VSZ                  | 207 020 KiB / 707 016 KiB          |
| Portal threads                    | 5                                  |
| Portal open FDs                   | 35                                 |
| Bot (`rsi_paper_test`) RSS / VSZ  | 164 364 KiB / 373 096 KiB          |
| Bot threads                       | 3                                  |
| Active bot processes              | 1                                  |
| Active TCP `:8080` connections    | 8 (loopback to Caddy)              |

### Database

| Item                          | Value                                      |
|-------------------------------|--------------------------------------------|
| `logs/reverto.db`             | 404 KiB                                    |
| `logs/reverto.db-wal`         | 4.0 MiB (active checkpoint window)         |
| `logs/reverto.db-shm`         | 32 KiB                                     |
| Tables                        | 10 (users, deals, orders, audit_findings,  |
|                               | backtest_runs, chart_annotations,          |
|                               | dashboard_layouts, changelog_entries,      |
|                               | roadmap_phases, sqlite_sequence)           |
| Row counts                    | users=1, deals=28, orders=30, audit=352,   |
|                               | annotations=0, layouts=1, backtests=2      |
| Indexes                       | 16 (all owned tables have `user_id` index) |
| Largest table                 | `audit_findings` 172 KiB                   |
| WAL / synchronous mode        | WAL + `synchronous=NORMAL` (`busy_timeout` |
|                               | 5 s); see [core/database.py:394-408](../core/database.py#L394-L408) |

### Logs / state / config

| Item                              | Value                                |
|-----------------------------------|--------------------------------------|
| `logs/` total                     | 7.8 MiB                              |
| `logs/portal.log`                 | 128 KiB (rotated 5 MiB × 3)          |
| `logs/audit.log`                  | 14 KiB                               |
| `logs/audit.jsonl`                | 36 KiB                               |
| `logs/1/rsi_paper_test.log`       | **3.2 MiB / 44 444 lines / ~5 days** |
| `logs/1/rsi_paper_test.state.json`| 33 KiB (28 closed deals embedded)    |
| `backups/` (8 daily snapshots)    | 2.2 MiB                              |
| Per-user dir layout in place      | yes (`logs/<uid>/`,                  |
|                                   | `config/bots/<uid>/`,                |
|                                   | `keys/<uid>.key`)                    |

### Per-bot growth rate (measured-then-extrapolated)

- Bot log: **3.2 MiB / 5 d ≈ 640 KiB/day ≈ 19 MiB/month**
- State file: ~33 KiB after 28 deals; grows linearly with closed
  deals embedded inline. Already flagged: [PT-v4-FS-008](../docs/pentests)
  (logs unbounded), `v26-14` (DB + state.json dual-source).

## Tier 1: 1 → 50 users

**Status:** safe with action items — the bottleneck is RAM, not code.

**Reasoning.**
At ~2.5 bots/user, 50 users = **125 bot subprocesses**. Each bot ≈ 165 MiB
RSS (measured). Even with ~30 % shared-page deduplication by the Linux
kernel page-cache, conservative private RSS is ~140 MiB/bot.

- Bot RAM: 125 × 140 MiB ≈ **17.5 GiB**
- Portal RAM under WS load (50 users × 3 WS = 150 sockets): 207 MiB
  measured baseline + ~300 MiB JSON-serialisation churn ≈ **0.5 GiB**
- DB cache + OS overhead: ≈ **1 GiB**
- **Total ≈ 19 GiB → current VPS (3.7 GiB) is 5× short.**

DB and disk are not the blockers at this tier:

- Per-bot writes ≈ 1–3 deal/order persists per minute under active
  trading. 125 bots × 2 writes/min ≈ 4 writes/sec — well within
  SQLite WAL's serialised-writer ceiling (~1 000 writes/sec on this
  hardware). Writes are gated by `_write_lock` ([core/deal_store.py:32](../core/deal_store.py#L32))
  — that's a Python lock, not a SQLite one, but the throughput
  ceiling is fine here.
- Disk: 125 bots × 19 MiB/month ≈ 2.4 GiB/month logs, plus 125 × 33 KiB
  state files (≈ 4 MiB), plus a DB that grows at maybe 100 KiB/user/month.
  **38 GiB disk holds well over a year's data** at this tier.

The `watch_state_files` loop scans all bots across all users every 2 s
([web/app.py:3286-3350](../web/app.py#L3286-L3350)). At 125 bots that is 125 × ~33 KiB JSON
reads + parses per 2 s = ~2 MiB/s reads + a few hundred ms of CPU per
cycle. Fine on SSD; the polling pattern itself is inefficient but not
yet a knijper at this tier. Tracked as `r1-067`.

**Action items:**

- Move VPS to **Hetzner CCX33 (8 vCPU dedicated, 32 GiB RAM)** —
  ~€69 / month. CCX23 (16 GiB) is too tight for the projected 19 GiB
  working-set + headroom.
- Implement [PT-v4-FS-007](#findings-cross-reference) — `MAX_BOTS_PER_USER`
  hard cap (suggest 5; UI already cleanly handles "limit reached").
  Without this, one user's curiosity = portal OOM.
- Implement [PT-v4-FS-008](#findings-cross-reference) — engine-side bot-log
  rotation. Today bot logs grow unbounded; `make backup` won't save
  you when one bot fills the disk.
- Implement [PT-v4-NW-004](#findings-cross-reference) — global request-body
  size limit (suggest 1 MiB). Trivial Starlette middleware.
- Add per-user disk-quota check (refuse `start_bot` if user's
  `logs/<uid>/` exceeds e.g. 500 MiB). 5 LOC.
- Telegram-notify queue cap ([PT-v4-EI-005](#findings-cross-reference)).
- **Backup integrity**: `state.json` + `config/bots/` are NOT in
  `make backup` today ([PT-v4-EI-004](#findings-cross-reference)). At one user
  this is mildly bad; at 50 users a restore would silently lose
  every bot's live position. Fix before opening signups.

## Tier 2: 50 → 100 users

**Status:** safe with same architecture; needs vertical scale + queue.

**Reasoning.**
- Bot RAM: 250 bots × 140 MiB ≈ **35 GiB**
- Portal: ~1 GiB under load (250 × 3 = 750 active WS, summary every 2 s)
- DB cache + OS: ~1.5 GiB
- **Total ≈ 38 GiB → CCX33 (32 GiB) is too small; need CCX43 (16 vCPU,
  64 GiB, ~€138 / mo)**

The `watch_state_files` cycle scans 250 × 33 KiB = 8 MiB JSON every 2 s
plus per-user-summary computation. It's still serial; one slow `read_state`
blocks the whole loop. This is the first tier where `r1-067` is a
real perf issue (~200–500 ms per cycle), not a future concern.

WS broadcast: 250 × 33 KiB summary every 2 s ≈ 4 MiB/s sustained on
loopback to Caddy, plus the StateBroadcaster's per-user fan-out which
is currently O(connections × clients) and must hold the broadcaster
`_lock` ([web/app.py:3238](../web/app.py#L3238)) for the duration. Not catastrophic,
but the second `watch_state_files` cycle can overlap with the first.

DB writes: 250 bots × ~2 writes/min ≈ 8 writes/s — still inside
SQLite's comfort zone, but the `_write_lock` Python serialisation
([core/deal_store.py](../core/deal_store.py)) starts to show as
contention spikes during deal-event bursts (10–20 deals close in
the same minute when BTC moves 1 %).

**Action items:**

- Vertical scale to CCX43 (€138 / mo).
- Refactor `watch_state_files` to iterate concurrently per-user
  (gather), not serially per-bot. ~0.5 dev-day. Fixes `r1-067`.
- Move SlowAPI rate-limiter to Redis backend (`r1-026`) — at 100 users
  the in-memory bucket is acceptable but if you later scale uvicorn
  workers (>1) the limiter becomes per-worker, defeating its point.
- Switch Telegram notifier to a single owner-process pool, drop the
  per-bot `TelegramNotifier` instance — cuts ~10 MiB/bot. ~0.5 dev-day.
- Add a startup-throttle on `start_bot`: if 10+ bots are mid-init,
  queue subsequent starts. Today the portal can fork 50 simultaneous
  Python interpreters on a portal restart; that briefly doubles RAM
  pressure.
- Begin instrumenting Prometheus/loki — flying blind past 50 users
  is a bug-hunt nightmare.

## Tier 3: 100 → 500 users

**Status:** requires architectural shift — subprocess-per-bot is fatal.

**Reasoning.**
- Bot RAM at 500 users (1250 bots × 140 MiB) = **175 GiB**
- Even at €138 / mo CCX43 with 64 GiB you're 3× short, and Hetzner's
  largest dedicated-vCPU CCX is CCX63 (48 vCPU, 192 GiB, ~€470 / mo)
  — fits, but burns money to run 1250 redundant Python interpreters.
- This is the tier where the **bot consolidation refactor** has to
  ship: run all bots as asyncio tasks (or a small thread pool) inside
  one portal process, not as forked subprocesses.

After consolidation, projected RAM:

- Portal + 1250 bots-as-tasks ≈ 4–6 GiB total (each bot's
  per-instance heap is ~10–20 MiB excluding the Python interpreter
  + ccxt module set, which is shared once). **~30× reduction.**
- DB: 1250 bots × ~2 writes/min = ~40 writes/s — SQLite still copes
  but write-lock contention from `core/deal_store._write_lock` is a
  measurable latency spike. Plan migration to Postgres (`r1-017`)
  for tier-4.
- WS: 500 users × 3 = 1500 concurrent sockets. Single FastAPI
  process can sustain this; uvicorn's loop is the limit, not RAM.
  But this is the tier where multi-worker uvicorn becomes desirable
  (not strictly required — 1500 WS works on a single async loop).
- Exchange API rate limits: each user brings their own keys
  (per-user Bitget/Kraken creds) so the rate-limit budget is
  per-user, not global. The `_bitget_client` global / `_price_lock`
  (`r1-027`, `r1.1-001`, `v26-25`) is for **public** market data
  shared across users — fine. But: cold-storage ingestion calls
  CoinGecko shared-key, and we have no per-tier budget. Track now,
  cap before tier 3 if cold storage ships.

**Action items:**

- **Bot consolidation** (the big one). 5–10 dev-days. Replace
  `subprocess.Popen` in [web/app.py:1815](../web/app.py#L1815) and [web/app.py:1985](../web/app.py#L1985) with an
  asyncio-based bot runner that imports the engine in-process. Keep
  the subprocess path behind a feature flag for bisection during
  rollout. Pre-condition: BotRegistry and signal handling already
  isolate per-bot lifecycle by `(user_id, slug)`, so the in-process
  refactor maps cleanly.
- VPS: CCX33 stays adequate POST-consolidation (8 vCPU / 32 GiB ≈
  €69/mo for 500 users). Pre-consolidation: not viable at any
  reasonable cost.
- Redis: introduce as the second moving part. Targets: rate-limiter
  state (`r1-026`), session epoch invalidation, Telegram-notify
  outbox, future `BotRegistry` shared-state (`r1-024`).
- Postgres migration plan written (don't ship yet). `r1-017`.
- Bot graceful-shutdown is currently 18+ seconds (`r3-015`); at 500
  users a portal restart serially shutting bots is unacceptable.
  Parallelise.

## Tier 4: 500 → 1000 users

**Status:** requires Postgres + Redis + multi-worker portal.

**Reasoning.**

Post-bot-consolidation, the dominant constraints become:

- DB write throughput: 2500 bots × ~2 writes/min = ~85 writes/s
  sustained, with 200–500 writes/s burst during market events.
  SQLite WAL handles this on paper but `_write_lock`'s Python
  serialisation plus `synchronous=NORMAL` fsync overhead creates
  noticeable tail latency (p99 > 100 ms is achievable, p999 worse).
  Postgres is the right answer here.
- WS broadcast fan-out at 1000 × 3 = 3000 sockets: a single async
  loop is at its ceiling; broadcast latency degrades from <100 ms
  to multi-second under JSON-serialisation load. Move broadcast
  fanout to a dedicated worker (or to Redis pub/sub).
- Multi-worker uvicorn breaks: `BotRegistry` (`r1-024`),
  `LogBroadcaster`/`StateBroadcaster` (`r1-025`), in-memory
  rate-limiter (`r1-026`), `_bitget_client` + module-level locks
  (`r1-027`). All four findings explicitly state "single-process
  only". Going multi-worker without addressing them silently
  breaks correctness — not just perf.
- Module-level `_price_lock` (`r1.1-001`, `v26-25`) head-of-line
  blocks `/api/ticker` and `/api/price` against each other. With
  3000 WS clients pinging price endpoints, this becomes the
  single most contended resource in the portal.

Revenue context (operator brief: $3000+/month at 1000 users) supports
this scope. ~25–40 dev-days realistic budget.

**Action items:**

- **Postgres migration.** `r1-017` is the canonical finding. Write
  a `core/database.py` adapter that targets either backend, ship
  Postgres in staging, dual-write for one week, cut over.
  ~10 dev-days.
- **Redis introduction** (if not done at tier 3).
  - Move SlowAPI to Redis backend (`r1-026`).
  - Move BotRegistry shared-state to Redis (`r1-024`) so multiple
    portal workers can coordinate.
  - Move broadcaster fan-out to Redis pub/sub (`r1-025`).
  ~5 dev-days.
- **Multi-worker uvicorn** (e.g. 4 workers behind Caddy) — only
  AFTER `r1-024/-025/-026/-027` are fixed. ~2 dev-days of
  test-suite work to make sure nothing module-level survived.
- Bot runner becomes its own process, separate from the portal —
  portal worker = stateless API, bot worker = long-lived bot host.
  Communicate via Redis. ~5 dev-days.
- VPS: split portal + bot-host. CCX33 portal (€69) + CCX43 bots
  (€138) + Hetzner managed Postgres ~€30 + Redis €15 ≈ **€250 /
  month** at this tier.

## Tier 5: 1000 → 5000 users

**Status:** requires sharded multi-host + read replicas.

**Reasoning.**
- 12 500 bots-as-tasks at ~10 MiB each ≈ 125 GiB total bot RAM
  (post-consolidation). One CCX63 (192 GiB) holds it physically,
  but: a single-host failure takes 5000 users offline, and the
  GIL on a single bot-runner process is now contended even with
  asyncio (one tick of bot N delays bot N+1 by however long N's
  ccxt call takes).
- DB: ~400 sustained writes/s, 2k+ burst. Postgres on a managed
  4 vCPU / 8 GiB tier handles this with room. Read traffic
  (chart, history) wants a read replica.
- WS: 15 000 sockets. Multi-host with sticky-session or Redis
  pub/sub. Caddy on the front happily fans this; the portal
  workers behind it scale linearly.
- Exchange API fan-out: still per-user keys, no shared budget, so
  rate-limit headroom doesn't shrink. CoinGecko / public market
  data does need a shared budget — by this tier it's a single
  Redis-backed token-bucket, not a per-process lock.
- ML pipeline (`ml/nightly_pipeline.py`) is currently per-bot, run
  serially. At 12 500 bots a single nightly run does not finish in
  a single night. Either (a) parallelise on a worker pool, (b)
  cluster-by-strategy and re-train less often, (c) run on a
  separate spot-priced compute node. Most likely (a)+(c).

**Action items:**

- Shard bot-host workers by `user_id % N`. Each worker owns a
  fixed slice of users; portal API workers read shared state from
  Postgres + Redis. ~10 dev-days.
- Postgres read replica + connection pool (pgbouncer). ~3 dev-days.
- Move ML pipeline to a dedicated worker host that polls a job
  queue. ~5 dev-days.
- VPS: 2 × CCX23 portal (€41 × 2 = €82) + 4 × CCX43 bot-host
  (€138 × 4 = €552) + Postgres HA (~€100) + Redis (~€30) +
  ML node (~€60) ≈ **€820 / month**. Operator brief: revenue
  scales with users, this is acceptable.

## Cost projections

| Tier  | VPS / infra needed                                                    | Monthly cost EUR | Per-user assumptions                  | Notes                                                          |
|-------|-----------------------------------------------------------------------|------------------|---------------------------------------|----------------------------------------------------------------|
| 1–10  | Current CPX21 (3 vCPU shared, 4 GiB)                                  | ~€8              | 2.5 bots, 3 WS, 120 req/h             | Today. Headroom for ~15–18 paying users with current code.     |
| 50    | CCX33 (8 vCPU dedicated, 32 GiB) + backup target                      | ~€69 + ~€5 = €74 | same                                  | Vertical scale only. No code refactor required, just hardening |
| 100   | CCX43 (16 vCPU, 64 GiB) + backup                                      | ~€143            | same                                  | Subprocess-per-bot is now expensive but functional.            |
| 500   | CCX33 portal (€69) + bot consolidation = single CCX33 (€69) + Redis €15 | ~€85             | post-consolidation 10–20 MiB/bot       | **Big-bang refactor required**. Otherwise €470+ for 192 GiB.   |
| 1000  | CCX33 portal + CCX43 bot-host + Postgres + Redis                      | ~€250            | as above + Postgres                   | Multi-process portal; tenant-shared infra still feasible.      |
| 5000  | 2× portal + 4× bot-host + PG HA + Redis + ML node                     | ~€820            | sharded by user_id % N                | Operator's revenue at this tier (≈$15k/mo) easily justifies.   |

Cost ceilings the operator stated (≤€50 at tier ≤100, ≤€150 at tier
3, no cap at tier 4–5) are met **only after** the bot-consolidation
refactor at tier 3. Without it, tier 100 already needs €138 / mo and
tier 500 explodes to €470+.

## Quick wins (do now, cheap insurance)

Each item: what / why / effort / file:line / expected gain.

1. **`MAX_BOTS_PER_USER` cap** — what: refuse `start_bot` if user
   has ≥ N running bots; why: a single user with no malice (curious
   trial, copy-paste loop) can OOM today's VPS by spawning 25+ bots;
   effort: 0.5 dev-day; refs: [`web/app.py:1778-1840`](../web/app.py#L1778-L1840),
   audit finding **PT-v4-FS-007**; gain: closes
   the lowest-friction DoS path before public signups.
2. **Engine-side bot-log rotation** — what: cap `logs/<uid>/<slug>.log`
   at 50 MiB × 3 rotations inside `main_paper.py` / `main_live.py`;
   why: bot log grows ~640 KiB/day unbounded; one runaway logger
   exhausts disk in <2 months at current rate; effort: 0.5 dev-day;
   refs: `main_paper.py`, `main_live.py`, audit finding **PT-v4-FS-008**;
   gain: removes a slow-burn outage class.
3. **Global request-body limit** — what: 1 MiB `BodySizeLimitMiddleware`
   on the FastAPI app; why: today an attacker can POST 200 MiB JSON
   and pin RAM; effort: 0.25 dev-day; refs: [`web/app.py`](../web/app.py) (add to
   middleware chain near [3501](../web/app.py#L3501)), audit finding **PT-v4-NW-004**;
   gain: closes a memory-DoS vector pre-signups.
4. **Backup `state.json` + `config/bots/`** — what: include both in
   `scripts/backup.sh`; why: today a restore loses every running
   bot's live state and every bot config; effort: 0.5 dev-day;
   refs: `scripts/backup.sh`, audit finding **PT-v4-EI-004**; gain:
   lossless DR.
5. **Concurrent `watch_state_files`** — what: replace the serial
   per-bot loop with `asyncio.gather` inside the cycle; why: at 100
   bots the cycle already takes hundreds of ms, and one slow
   `read_state` (e.g. fs hiccup) blocks every other bot's UI
   refresh; effort: 0.5 dev-day; refs: [`web/app.py:3286-3350`](../web/app.py#L3286-L3350),
   audit finding **r1-067**; gain: smoother UI under load, removes
   a tier-2 head-of-line blocker.
6. **Per-user disk quota** — what: precompute `logs/<uid>/` size on
   `start_bot`, refuse if > 500 MiB; why: complements
   bot-log rotation; the bound exists per file, but a user with 10
   bots can still hit 500 MiB cumulative; effort: 0.5 dev-day; refs:
   [`web/app.py`](../web/app.py) `start_bot` block; gain: defence-in-depth.
7. **Telegram notify queue cap** — what: `asyncio.Queue(maxsize=…)`
   in `notifications/telegram.py`; why: today the queue is
   unbounded — a Telegram-API outage during a market crash → memory
   growth proportional to (closed deals × bots); effort: 0.5
   dev-day; refs: [`notifications/telegram.py`](../notifications/telegram.py),
   audit finding **PT-v4-EI-005**; gain: closes a future-OOM path.
8. **Promote `_extract_client_ip` test coverage to multi-tenant** —
   what: add tests asserting per-user rate-limit isolation under
   shared NAT (`/api/price` from two `user_id`s on the same IP must
   not collide); why: behaviour exists ([`web/app.py:2401-2420`](../web/app.py#L2401-L2420))
   but one regression revives **r1-044**; effort: 0.25 dev-day; gain:
   regression guard.
9. **Bot graceful-shutdown parallelisation** — what: when portal
   shuts down, send SIGTERM to all bots concurrently and join with
   a single 5 s timeout; why: today shutdown is ~18 s sequential
   (audit `r3-015`); at 50 bots that's >12 minutes; effort: 0.5
   dev-day; refs: [`web/app.py`](../web/app.py) lifespan handler [2650-2725](../web/app.py#L2650-L2725); gain: tier-2-survivable
   restarts.
10. **WAL checkpoint cron** — what: `PRAGMA wal_checkpoint(TRUNCATE)`
    every 24 h via cron; why: WAL is currently 4 MiB after 8 days;
    benign now but if bot-write rate climbs the WAL grows linearly
    until next natural checkpoint; effort: 0.1 dev-day; refs:
    `scripts/` new file; gain: keeps DB read-checkpoints predictable.

## Architectural shifts (plan for, don't do now)

Each item: what / trigger / approximate effort / dependency.

- **Bot consolidation (subprocess → in-process asyncio runner).**
  Trigger: third week of consistent ≥40 paying users, or memory
  pressure (>70 % steady) on tier-2 VPS. Effort: 5–10 dev-days.
  Dependency: none — `BotRegistry` already isolates per
  `(user_id, slug)`; the engine's `paper_engine.run` is already
  asyncio-friendly. Foundation for tier 3 onwards.
- **SQLite → Postgres migration.** Trigger: sustained >50 writes/s
  measured over a 1-hour window, OR multi-worker portal becomes
  necessary. Effort: 8–12 dev-days. Dependency: bot consolidation
  helps but isn't strictly required. Audit finding `r1-017`.
- **Redis as shared-state plane.** Trigger: first need to scale
  beyond one uvicorn worker. Effort: 4–6 dev-days. Targets the
  specific findings `r1-024/-025/-026/-027` — this is not a
  generic "let's add a cache" but the documented exit from
  module-level singletons.
- **Multi-worker portal (uvicorn -w 4).** Trigger: WebSocket
  delivery latency p95 > 1 s, OR portal CPU > 60 % steady. Hard
  pre-requisite: ALL of `r1-024/-025/-026/-027` resolved.
- **Sharded bot-host workers.** Trigger: tier 5 (>1000 active
  users). Effort: 8–10 dev-days. Dependency: bot consolidation
  shipped, Redis present.
- **ML pipeline distributed worker pool.** Trigger: nightly
  pipeline runtime > 4 hours. Effort: 5 dev-days. Dependency:
  none architectural, but needs an external job queue.

What we do **NOT** recommend doing:

- Microservices split (web / bots / API gateway / etc.). Reverto's
  monolith is a strength — single repo, single deploy, single
  observability surface. Stay monolith through tier 5; even at
  5000 users the architecture is "monolith + bot-host pool", not
  "service mesh".
- Premature Postgres at tier 1–2. SQLite WAL handles this
  workload well; the migration cost (8–12 dev-days) doesn't
  amortise until tier 3+.
- Replacing per-user `keys/<uid>.key` Fernet with KMS. Per-user
  Fernet is the right scale-out story until you hit a compliance
  requirement (SOC 2 or equivalent). That's not on the operator's
  visible roadmap.

## Findings cross-reference

The following existing `audit_findings` rows are directly relevant
to this audit. They are NOT duplicated as new tickets below — track
status against the existing IDs.

| finding_id        | severity | status    | scaling tier where it bites |
|-------------------|----------|-----------|-----------------------------|
| `r1-017`          | HIGH     | open      | 4 (Postgres migration)      |
| `r1-024`          | HIGH     | open      | 4 (BotRegistry shared)      |
| `r1-025`          | HIGH     | open      | 4 (broadcaster shared)      |
| `r1-026`          | MEDIUM   | open      | 3 (rate-limit Redis)        |
| `r1-027`          | MEDIUM   | open      | 4 (`_bitget_client` module) |
| `r1-029`          | MEDIUM   | open      | 3–4 (rate-budget central)   |
| `r1-067`          | MEDIUM   | open      | 2 (state-watcher serial)    |
| `r1.1-001`        | MEDIUM   | open      | 3 (`_price_lock` HOL)       |
| `B-01`            | MEDIUM   | open      | 2–3 (Bitget multi-bot RL)   |
| `r3-015`          | MEDIUM   | deferred  | 2 (graceful shutdown 18 s)  |
| `r2-006`          | MEDIUM   | deferred  | 3 (per-conn WS rate-limit)  |
| `v26-12`          | INFO     | deferred  | 4 (`_write_lock` ceiling)   |
| `v26-14`          | INFO     | deferred  | 3 (DB+state.json dual)      |
| `v26-25`          | INFO     | deferred  | 3 (price global serial)     |
| `PT-v4-FS-007`    | INFO     | deferred  | 1 (max-bots-per-user)       |
| `PT-v4-FS-008`    | INFO     | deferred  | 1 (bot-log unbounded)       |
| `PT-v4-NW-004`    | MEDIUM   | open      | 1 (request-body limit)      |
| `PT-v4-EI-004`    | LOW      | open      | 1 (state.json not in backup)|
| `PT-v4-EI-005`    | LOW      | deferred  | 1–2 (notify-queue cap)      |

## Summary table

| Tier | Hard blockers                                  | Knijpers                                                | Effort to reach            | Status                             |
|------|------------------------------------------------|---------------------------------------------------------|----------------------------|------------------------------------|
| 50   | RAM (current VPS)                              | unbounded logs, no max-bots, no body-limit              | VPS upgrade + 3 dev-days   | safe with action items             |
| 100  | RAM (CCX33)                                    | watch_state_files serial, telegram unbounded            | VPS upgrade + 2 dev-days   | safe                               |
| 500  | subprocess-per-bot RAM                         | `_price_lock` HOL, write-lock contention, shutdown 18 s | bot consolidation 5–10 dd  | requires architectural shift       |
| 1000 | single-process portal correctness (`r1-024…-027`) | DB write throughput, WS fan-out                         | PG + Redis + multi-worker, 25–40 dd | requires Postgres + Redis  |
| 5000 | single-host bot RAM, ML pipeline runtime       | Bitget public-data shared-budget, backup IO             | sharded host pool 25–35 dd | requires sharded multi-host        |

## Suggested follow-up tickets

### High priority (do before reaching 100 users)

- [ ] `MAX_BOTS_PER_USER` cap in `start_bot` — [`web/app.py:1778-1840`](../web/app.py#L1778-L1840) — 0.5 dd — closes **PT-v4-FS-007**
- [ ] Engine-side bot-log rotation — `main_paper.py`, `main_live.py` — 0.5 dd — closes **PT-v4-FS-008**
- [ ] Global request-body limit middleware — [`web/app.py`](../web/app.py) — 0.25 dd — closes **PT-v4-NW-004**
- [ ] Backup includes `state.json` + `config/bots/` — `scripts/backup.sh` — 0.5 dd — closes **PT-v4-EI-004**
- [ ] Per-user `logs/<uid>/` quota check on `start_bot` — [`web/app.py:1778`](../web/app.py#L1778) — 0.5 dd
- [ ] Telegram notify queue cap — [`notifications/telegram.py`](../notifications/telegram.py) — 0.5 dd — closes **PT-v4-EI-005**
- [ ] `watch_state_files` concurrent-per-user — [`web/app.py:3286-3350`](../web/app.py#L3286-L3350) — 0.5 dd — closes **r1-067**
- [ ] Parallel bot graceful-shutdown — [`web/app.py:2650-2725`](../web/app.py#L2650-L2725) — 0.5 dd — closes **r3-015**
- [ ] WAL checkpoint cron — `scripts/` (new) — 0.1 dd
- [ ] Prometheus + Grafana shipping — `ops/` (new) — 2 dd

### Medium priority (do before reaching 500 users)

- [ ] Migrate SlowAPI to Redis backend — [`web/app.py:2423`](../web/app.py#L2423) — 1 dd — closes **r1-026**
- [ ] Centralised Bitget rate-budget across bots — `core/exchange_budget.py` (new) — 2 dd — closes **B-01**, **r1-029**
- [ ] **Bot consolidation** (subprocess → in-process runner) — [`web/app.py:1778-1840`](../web/app.py#L1778-L1840), [`1929-2010`](../web/app.py#L1929-L2010), engine entrypoints — 5–10 dd — unlocks tier 3 economics
- [ ] Move `BotRegistry` shared state to Redis — [`web/app.py:1532-1710`](../web/app.py#L1532-L1710) — 3 dd — closes **r1-024**
- [ ] Move broadcasters to Redis pub/sub — [`web/app.py:3095-3275`](../web/app.py#L3095-L3275) — 3 dd — closes **r1-025**
- [ ] Per-user write-lock partitioning in `deal_store` — [`core/deal_store.py:32`](../core/deal_store.py#L32) — 2 dd — softens **v26-12**

### Long-term (plan for, trigger conditions noted)

- [ ] **SQLite → Postgres migration** — trigger: sustained >50 writes/s OR multi-worker need — 8–12 dd — closes **r1-017**
- [ ] Multi-worker uvicorn (-w 4) — trigger: portal CPU >60 % OR WS p95 >1 s — 2 dd (after r1-024/-025/-026/-027)
- [ ] Sharded bot-host workers (tenant routing by `user_id % N`) — trigger: >1000 paying users — 8–10 dd
- [ ] Postgres read replica + pgbouncer — trigger: chart/history read p95 > 200 ms — 3 dd
- [ ] ML pipeline distributed-worker — trigger: nightly run > 4 h — 5 dd
- [ ] Per-user CoinGecko / public-data rate-budget — trigger: cold-storage feature ships — 1.5 dd

---

*Audit performed 2026-05-03 evening on `reverto-prod-01`. Read-only
measurements + static analysis + mathematical projection. No load
tests, no synthetic bots, no production state mutated. Cross-
referenced against 352 existing `audit_findings` rows.*
