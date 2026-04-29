# Phase-4 scoping — multi-tenant readiness

> **Status note (Phase 4 has not started).** Reverto runs as a
> single-tenant single-host deploy today. This document captures
> the architectural prerequisites that must land before Reverto
> can support multiple tenants on a single host or scale across
> multiple hosts. The detailed scope decisions (auth flow for
> self-service signup, billing, regulatory positioning, etc.) are
> deferred until concrete user-demand exists; this document
> covers only the **infrastructure prerequisites** that block any
> multi-tenant rollout regardless of how those higher-level
> questions resolve.

Stand-van-zaken document, parallel to `docs/phase-3.md` (live-
trading scope) and `docs/security-model.md` Part 4 (security-
architecture roadmap, Phase A → G). The numeric "Phase 4" here
means "multi-tenant rollout" specifically — distinct from
security-model Phase D ("user-facing security") or Phase G
("SaaS-launch readiness"), which are about hardening rather than
scaling.

This document exists so:

1. The findings tracker entries that block Phase 4 are visible
   as a coherent set, not scattered observations across audit
   reports.
2. When Phase 4 work begins, the prerequisite list is already
   captured + scoped.
3. Single-tenant operators can confirm at a glance that none of
   these findings affect their current deploy.

## 1. Doel & status

Phase 4 is the work that turns Reverto from a single-tenant
operator-controlled deploy into a multi-tenant platform where
multiple users own their own bots, credentials, and audit trail
on shared infrastructure. The auth + per-user-filesystem
foundation (Phase-3a + Phase A wrap-up + Phase B) is already in
place — what stays open is the **process-coordination + storage
substrate** that lets multiple tenants coexist without stepping
on each other's locks, broadcasts, and rate-limits.

**Decision (2026-04-29).** Postgres migration + Redis
coordination are explicitly **deferred to Phase 4 kickoff**, not
implemented as standalone work. Reasoning:

* No current drukfactor — single-tenant deploy has no contention
  on SQLite or in-process locks.
* The migration is significant work (1-2 weeks each) and the
  Phase 4 design decisions (per-tenant schemas vs row-level
  tenancy; uvicorn workers vs separate processes; managed Redis
  vs self-hosted) can still influence the implementation
  approach.
* Doing the migration now would lock in choices before the
  product scope it serves is clear.

Tracker findings stay `status=open` with notes pointing here.

## 2. Prerequisite blockers

Six findings in the tracker block Phase 4 multi-tenant rollout.
Each is currently `open` because the work has cross-cutting
blast radius requiring dedicated planning.

### 2.1 Database layer (r1-017, HIGH)

**Finding.** SQLite blocks multi-host and multi-writer deploys.
SQLite WAL-mode handles single-process writes well but cannot
support concurrent Reverto instances sharing the same database
— there is no streaming replication, lock contention degrades
ungracefully past one writer, and the single-writer ceiling
caps throughput regardless of CPU headroom.

**Resolution direction.** Postgres migration. Tooling sketch:

* SQLAlchemy or psycopg2 adoption, replacing the per-thread
  `sqlite3.Connection` cache in `core/database.py`.
* Alembic for forward + backward schema migrations, replacing
  the destructive drop-and-recreate pattern at `_LAST_DESTRUCTIVE_VERSION = 4`.
* Data-migration script (SQLite → Postgres) covering the eight
  owned tables (users, deals, orders, chart_annotations,
  backtest_runs, dashboard_layouts, audit_findings,
  changelog_entries).
* Test fixtures need a Postgres-test-instance (testcontainers,
  pytest-postgresql, or a CI-side managed instance).
* Deploy infrastructure (managed RDS-style service, or self-
  hosted with daily PITR + WAL-shipping for backup).

**Effort estimate.** 1-2 weeks of focused work. Most of the
churn is mechanical (sqlite3 → SQLAlchemy syntax across the
~3000-5000 LoC of `core/database.py` + every `deal_store` /
`user_store` / `findings_store` callsite); the harder pieces
are the Alembic migration history (because v3 + v4 were
destructive in SQLite-land and Alembic needs reversible
forward-paths) and the test fixtures.

**Re-evaluation trigger.** Postgres migration starts when
**either** (a) Phase 4 multi-tenant scope becomes a planned
feature, **or** (b) SQLite contention becomes measurable
(concurrent-write errors in `portal.log`, `database is locked`
exceptions under load).

### 2.2 Process coordination (r1-024, HIGH; r1-025, HIGH)

**Findings.**

* **r1-024** — `BotRegistry` uses `asyncio.Lock` for bot
  lifecycle state. Under `uvicorn --workers 4`, each worker
  has an independent registry, so the same bot can be started
  twice from two different workers without either knowing.
* **r1-025** — `StateBroadcaster` and `LogBroadcaster` hold
  WebSocket clients in process-local memory. With multi-worker
  setup, `watch_state_files` on worker 2 broadcasts updates
  that worker 1's connected clients never receive.

**Resolution direction.** Replace in-process coordination
primitives with distributed equivalents:

* Redis or Postgres advisory locks for `BotRegistry` lifecycle
  transitions (start, stop, restart, delete).
* Redis pub/sub for state + log broadcasts. Each worker
  subscribes; engine writes publish; broadcasters fan out from
  their local subscriber set.
* Both replacements have well-defined interface seams already
  — `registry.get` / `registry.all` already takes `user_id`,
  and the broadcasters are encapsulated in `web/app.py`.

**Effort estimate.** 1-2 weeks (parallel-implementable with
the Postgres migration; the two paths share dependency on
having a Redis or Postgres-with-NOTIFY service in the deploy).

### 2.3 Rate-limit + per-bot coordination (r1-026, MEDIUM; r1-027, MEDIUM; r1-029, MEDIUM)

**Findings.**

* **r1-026** — `slowapi.Limiter` is in-memory by default. Each
  uvicorn worker has its own token buckets, so an attacker
  hitting `5 reqs/min` per worker effectively bypasses the
  intended global limit by N (where N = worker count). slowapi
  already supports `storage_uri="redis://..."`; the wire-up
  is one constructor argument.
* **r1-027** — Module-level `_bitget_client` + `_price_lock` in
  `web/app.py` serialise every `/api/price` call across the
  whole portal. Heavy dashboard polling queues behind it; with
  multi-worker deploys the lock is per-worker so the
  serialisation breaks AND each worker hits Bitget independently.
* **r1-029** — Each bot's ccxt client rate-limits independently.
  With 100 tenants × 3 bots, the per-client rate-limit windows
  do not aggregate — Bitget sees the cumulative load and starts
  429-ing without any single bot understanding why.

**Resolution direction.**

* Slowapi → Redis backend (one-line config change once Redis is
  in the deploy; no code rewrites needed).
* Per-process Bitget clients with a central rate-coordinator
  (Redis-backed token bucket, or per-host limit + cross-host
  coordination via shared cache).
* Per-tenant rate-budgets fed back to bots so cumulative load
  is visible — closes r1-029.

**Effort estimate.** Smaller than 2.1 / 2.2 — this is mostly
config + a token-bucket coordinator (~200 LoC + tests).

### 2.4 WebSocket coordination (r2-006, MEDIUM)

**Finding.** WebSocket endpoints (`/ws/logs/{slug}`, `/ws/state`
in `web/app.py`) have no per-user or per-connection cap. Slowapi
does not hook into the WebSocket protocol — its middleware sits
on the HTTP request-cycle, so neither the connection handshake
nor post-upgrade frames are throttled. An authenticated user
could open arbitrary numbers of concurrent WS connections, each
carrying its own entry in `LogBroadcaster._clients` /
`StateBroadcaster._clients` plus the `_user_map` dict, consuming
server memory and slowing the broadcast fan-out loop.

In single-tenant deploy this is self-DoS only — the operator is
the only authenticated user, and self-attacks are out of threat
model. At Phase 4 multi-tenant rollout, one tenant could degrade
WS-quality for all other tenants by opening many connections
from a single session-cookie.

**Resolution direction.**

* Per-user connection cap (e.g. 10 concurrent WS per user_id)
  enforced at the `_ws_extract_user_id` callsite. Reject excess
  with close code `4429` (custom Retry-After-equivalent for WS
  — the WebSocket close-code namespace `4xxx` is reserved for
  application-defined codes).
* The cap-counter must be coordinated across uvicorn workers —
  same Redis dependency as r1-024 / r1-025 / r1-026. Process-
  local enforcement is no enforcement under multi-worker deploy:
  4 workers × 10-cap each = 40 effective connections per user,
  defeating the limit.
* Optional defence-in-depth: ipaddr-keyed connection-rate (new
  connections per minute per IP) at the same callsite, mirroring
  the slowapi `5/minute` posture on `/auth/login`.

**Effort estimate.** Small once Redis is in the deploy (~150 LoC
+ 4-6 tests for cap-enforcement, race-condition between cap-check
and register, close-code handling). Without Redis: a process-
local implementation is single-worker-only and would need
rewriting at Phase 4. Hence deferred — implementing now would
ship the same architectural fault as the (already-deferred)
r1-026 slowapi in-memory limiter.

**Re-evaluation trigger.** Same as r1-026 — moves to "active"
when Phase 4 multi-tenant scope opens, OR when the deploy
switches to `uvicorn --workers N > 1` for any reason.

## 3. What stays single-host even in Phase 4

Some Reverto components are intentionally single-host even
after the multi-tenant migration:

* **Bot subprocess execution.** Each bot is its own Python
  subprocess with state.json on local disk. Distributing bots
  across hosts requires a scheduler (Kubernetes, Nomad,
  Phase C signing-service-side dispatch) and is **out of
  Phase 4 scope** — Phase 4 makes the *portal* multi-tenant,
  not the *engine fleet*.
* **Fernet key storage.** Per-user encryption keys live in
  `keys/<user_id>.key` on the host filesystem (chmod 0600
  under a 0700 dir). Multi-host deploys would need a key-vault
  solution (HashiCorp Vault, AWS KMS, or the Phase C signing-
  service holding keys out-of-process). Cross-references
  `docs/security-model.md` Phase C.
* **Audit logs.** `logs/audit.jsonl` is single-file append +
  per-user split under `logs/<user_id>/audit.jsonl`. Multi-
  host log aggregation needs a centralised log collector
  (Loki, Vector, DataDog agent) — out of Phase 4 scope until
  observability requirements force the move.

## 4. Phase 4 readiness work already complete

The following Phase 4 prerequisites have already landed and
do NOT block multi-tenant rollout:

* **Per-user filesystem isolation** — `core/paths.py` returns
  user-scoped paths (`credentials/<uid>/`, `keys/<uid>.key`,
  `logs/<uid>/`, `config/bots/<uid>/`). All write paths
  consistently key on `user_id`.
* **Per-user Fernet credentials** — Phase A wrap-up
  (`fix/phase-a-wrapup`). Each user has their own master key;
  cross-user decrypt is impossible by construction.
* **TOTP 2FA** — Phase B PR 1-5 (`feat/totp-foundation` →
  `feat/cookie-posture-regression-test`). Optional second
  factor with per-user encrypted seed at rest.
* **Per-user login rate-limit** — Phase B PR 4. 10 failed
  attempts in 15 minutes triggers a 429 with rounded
  Retry-After (post-pt-160).
* **Cookie-posture regression test** — Phase B PR 5. The
  `Secure + HttpOnly + SameSite=Strict` triple is pinned by
  CI so a future config edit cannot silently weaken it.
* **Bcrypt timing parity** — `pt-101` fix
  (`fix/phase-4-readiness-security-cluster` commit `b0582d7`).
  Unknown-user verify pays the same bcrypt cost as known-user
  wrong-password, closing the username-enumeration channel.
* **Audit-log permissions hardening** — `rhav2-001` fix
  (same commit). Audit files land at mode `0o640` via umask
  + explicit chmod, deterministic regardless of process umask.
* **Cross-cookie isolation** — Phase B PR 3 used
  `URLSafeTimedSerializer` with per-purpose salts so the
  three cookie types (session, pending-totp-enrollment,
  pending-login-totp) cannot be replayed across each other.

The takeaway: the **identity / authentication / per-user
isolation** work for Phase 4 is already done. What remains is
the **process-coordination + storage substrate** captured in
section 2.

## 5. Decision log

* **2026-04-29.** Postgres migration + Redis coordination
  explicitly deferred to Phase 4 kickoff. Tracker findings
  r1-017, r1-024, r1-025, r1-026, r1-027, r1-029 stay
  `status=open`; their notes are extended with a pointer
  back to this document and the date of the deferral
  decision so a future audit can reconstruct why these HIGH-
  severity findings sat open through the single-tenant
  lifetime of the project.
* **2026-04-29 (later).** r2-006 (No per-connection rate-limit
  on WebSocket throughput) status flipped from `in_progress`
  to `deferred`. Rationale: same architectural pattern as
  r1-026 — process-local enforcement is multi-worker-incoherent,
  and the auditor description explicitly notes "single-operator
  means self-DoS unrealistic". Captured in section 2.4 above.
  Tracker note extended with cross-reference. PR:
  `cleanup/r2-006-defer-to-phase-4`.

## 6. Re-evaluation triggers

Move work from "deferred" to "active" when **any** of:

1. **Multi-tenant user signup becomes a planned feature.**
   The signup flow itself is out of scope for this document,
   but the moment signup lands, the prerequisites in §2
   become deploy-blockers.
2. **SQLite contention becomes measurable.** Operator sees
   `database is locked` exceptions under normal load, or
   single-writer throughput caps the engine tick cadence.
3. **Single-host capacity is reached.** CPU / memory / network
   ceilings on the current host force a horizontal scale-out.
4. **Compliance requirement for multi-region deploy.** GDPR
   data-residency, EU-side latency, or regulatory custody
   requirements that force geographic distribution.

Until at least one of these triggers fires, the Phase 4
prerequisites remain `open` in the tracker with cross-reference
to this document, and no scheduling pressure attaches to them.

## 7. Verwijzingen

* [Phase-3 scoping](phase-3.md) — live-trading roadmap; Phase 3
  is independent of Phase 4 and ships first regardless.
* [security-model.md](security-model.md) Part 4 — the security-
  architecture Phase A → G roadmap. Phase G ("SaaS-launch
  readiness") overlaps with Phase 4 on the launch checklist
  but the security work itself is captured under Phase A → G.
* [architecture.md](architecture.md) "Multi-tenant foundation"
  + "Multi-tenant filesystem layout" — the per-user isolation
  work that's already in place (§4 above).
* [runbook.md](runbook.md) "TOTP recovery" + "First-time setup"
  — operator procedures that already account for per-user
  credentials.
* Findings tracker entries: r1-017, r1-024, r1-025, r1-026,
  r1-027, r1-029, r2-006. All currently `status=open` (the
  r1-* set) or `status=deferred` (r2-006), with notes pointing
  here.
