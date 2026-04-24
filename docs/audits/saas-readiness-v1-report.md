# SaaS-Readiness Audit v1

**Classification:** Internal
**Status:** Audit report, v1 (2026-04-23)
**Scope:** Multi-tenant SaaS transition readiness (20 domains)
**Auditor:** Claude Code via `saas-readiness-audit-v1` prompt
**HEAD at audit time:** `4c2efc3` (main post-PR-57, feat/workspace-open-deals-panel merge)
**Baseline:** v26 report (2026-04-20) + v27 report Phase 1 (2026-04-22)

---

## Executive Summary

Reverto is **architecturally on the right track, but several single-tenant assumptions still need to be unwound** before a second non-admin user can be seeded safely. Phase-3a (DB-backed auth, per-user Fernet keys, composite `(user_id, slug)` registry, user-scoped filesystem) is shipped and solid. The v26+v27 findings cluster went mostly green: `v26-15h` (emergency-stop TypeError), `v26-01` (`_require_session` active check), `v26-02` (emergency-stop admin role), `v26-10` (destructive-migration opt-in), `v26-11` (`bump_session_epoch` atomicity), `v26-16` (per-user WS broadcaster filtering), `v26-17` (404 on unknown slug) are all closed. Phase-3a's design choice to make `user_id` a required parameter on every `core.deal_store` helper paid off — the read + write paths audited here are all properly tenant-scoped, and the one surviving pattern-level gap (v27-01 API-key stub) is explicit and documented.

The **three biggest migration blockers** on the path to multi-tenant SaaS are: (1) **v27-01** — the `REVERTO_API_KEY` fallback returns a hardcoded `DEFAULT_USER(id=1, role='admin')` stub that bypasses `user.active` and grants cross-tenant admin to any holder of the shared key (Phase B blocker, carry-over); (2) **architectural single-process assumptions** — `BotRegistry`, `StateBroadcaster`, `LogBroadcaster`, SlowAPI rate-limiter, `_bitget_client`, and the chart/candles LRU caches all live in Python process-local memory, so a multi-worker uvicorn deploy would race or bypass them (Phase C/G blocker); (3) **credential custody in main-app** — Fernet decryption, exchange-client instantiation, and real-order placement all live in `core.credentials` + `live/live_engine.py` on the same host as the portal, exactly the architecture that security-model.md Part 3.1 says must move to a separate signing-service before SaaS launch (Phase C blocker). Everything else is scoped MEDIUM or below.

**Top 5 findings overall:**

1. **r1-001 (HIGH)** — API-key path still returns hardcoded admin stub, bypassing `user.active` + granting cross-tenant admin. Carry-over of v27-01; upgraded to HIGH here because it is the single most impactful landmine for the first multi-user seed.
2. **r1-012 (HIGH)** — `BITGET_PASSPHRASE` read from process-wide env-var (`main_live.py:258`); live-mode bots for different tenants would share one passphrase. Per security-model.md Part 3.5, passphrase is user-scoped material and must move into per-user credentials store.
3. **r1-023 (HIGH)** — Portal subprocess passes `os.environ.copy()` into every bot subprocess (`web/app.py:927,1088`); every tenant's bot process inherits every other tenant's env-var, including Telegram + Bitget credentials.
4. **r1-041 (HIGH)** — `_state_mtimes` (`web/app.py:1830`) is keyed on `bot.slug` only, not `(user_id, slug)`. Two users with the same slug cross-contaminate the WS-push scheduler: user A's state-file change blocks user B's change-detection for that iteration.
5. **r1-055 (HIGH)** — Service-separation readiness: the entire credential decryption, scope-whitelisting, cap-enforcement, and order-placement surface that security-model.md Part 3.1 places in `reverto-signer` still lives in the main-app process. This is a category-level finding (no single file, whole-tree refactor).

**Phase-completion snapshot** (see Part 22 for the full matrix):

| Phase | Items scoped | Items done | % |
|-------|:------------:|:----------:|:---:|
| A — Foundation | ~10 | 7 | 70% |
| B — Auth hardening | ~7 | 2 | ~30% |
| C — Service separation | ~7 | 0 | 0% |
| D — User-facing security | ~6 | 0 | 0% |
| E — Defense layers | ~7 | 0 | 0% |
| F — Independent watchdog | ~4 | 0 | 0% |
| G — SaaS launch | ~8 | 0 | 0% |

Foundation (Phase A) is where most work has already landed; everything after is almost entirely ahead.

---

## Remediation status (post-VPS-0 sweep)

Sprint 1 (five HIGHs, individually merged) + Sprint 2 (eleven MEDIUM/LOWs bundled) + VPS-0 sweep (ten Phase-A hygiene items) have closed the following findings. Detailed sections below also carry inline **STATUS.** markers where they exist; short-entry findings (only in summary tables) are captured here.

| Finding | Severity | Branch | Status |
|---|---|---|---|
| r1-001 | HIGH | `fix/r1-001-api-key-respects-active` | RESOLVED |
| r1-002 | HIGH | `fix/r1-002-changelog-admin-role-gate` | RESOLVED |
| r1-012 | HIGH | `fix/r1-012-bitget-passphrase-per-user` | RESOLVED |
| r1-023 | HIGH | `fix/r1-023-subprocess-env-whitelist` | RESOLVED |
| r1-041 | HIGH | `fix/r1-041-state-mtimes-per-user` | RESOLVED |
| r1-004 | MEDIUM | `feat/sprint-2-audit-sweep` | RESOLVED |
| r1-007 | LOW    | `feat/sprint-2-audit-sweep` (bundled with r1-032) | RESOLVED |
| r1-020 | MEDIUM | `feat/sprint-2-audit-sweep` | RESOLVED |
| r1-032 | MEDIUM | `feat/sprint-2-audit-sweep` | RESOLVED |
| r1-042 | MEDIUM | `feat/sprint-2-audit-sweep` | RESOLVED |
| r1-051 | LOW    | `feat/sprint-2-audit-sweep` | RESOLVED |
| r1-052 | LOW    | `feat/sprint-2-audit-sweep` (already clean — no TODO-comments remain after v26-16) | RESOLVED |
| r1-053 | MEDIUM | `feat/sprint-2-audit-sweep` (3 E2E tests; /api/bots listing deferred) | RESOLVED |
| r1-054 | LOW    | `feat/sprint-2-audit-sweep` | RESOLVED |
| r1-056 | MEDIUM | `feat/sprint-2-audit-sweep` | RESOLVED |
| r1-058 | MEDIUM | `feat/sprint-2-audit-sweep` | RESOLVED |
| r1-075 | LOW    | `feat/sprint-2-audit-sweep` | RESOLVED |
| **r1-006** | LOW    | `fix/vps-0-sweep` (drop stale `u` field from cookie, resolve from uid) | RESOLVED |
| **r1-010** | LOW    | `fix/vps-0-sweep` (docstring — Phase-C dependency) | ACCEPTED |
| **r1-035** | LOW    | `fix/vps-0-sweep` (log API-key hint, not full value) | RESOLVED |
| **r1-043** | LOW    | `fix/vps-0-sweep` (per-user logout rate-limit key) | RESOLVED |
| **r1-047** | LOW    | verified clean post-v26-17; no bare `{"error":...}` responses remain | RESOLVED |
| **r1-048** | LOW    | `fix/vps-0-sweep` (inline docstring + `openapi_url=None`) | ACCEPTED |
| **r1-049** | MEDIUM | `fix/vps-0-sweep` (`paths.user_ml_results_path`, per-user folder) | RESOLVED |
| **r1-057** | LOW    | `fix/vps-0-sweep` (`core/circuit_breaker.py` wired into `PublicExchange`) | RESOLVED |
| **r1-059** | LOW    | `fix/vps-0-sweep` (`_validate_config_completeness` in lifespan) | RESOLVED |
| **r1-074** | MEDIUM | `fix/vps-0-sweep` (SHA-384 SRI on unpkg scripts) | RESOLVED |

Still open after VPS-0: r1-003, r1-005, r1-008, r1-009, r1-011, r1-013–r1-019, r1-021, r1-022, r1-024–r1-031, r1-033, r1-034, r1-036–r1-040, r1-044–r1-046, r1-050, r1-055, r1-060–r1-073, r1-076. Delta-findings r1.1-001 still open (Phase-C).

---

## Severity Definitions

| Severity | Meaning |
|----------|---------|
| CRITICAL | Multi-tenant deploy would give a concrete data-leak or security-breach **today**. MUST fix before any second user is seeded. |
| HIGH     | Significant migration-risk or operational-blocker. SHOULD fix before SaaS launch; several are hard-gated per phase. |
| MEDIUM   | Technical debt or consistency-issue. Scope in during Phase B/C/D with the relevant other work. |
| LOW      | Code-hygiene, style, or future-proofing. Nice-to-have; sweep-PR candidates. |
| INFO     | Observation worth documenting, not actionable today. |

---

## Part 1: Executive Summary of Findings

| ID | Severity | Domain | Finding | Ref |
|----|----------|--------|---------|-----|
| r1-001 | HIGH | Auth | API-key fallback returns hardcoded DEFAULT_USER, bypassing `active` + granting admin | v27-01 |
| r1-002 | HIGH | Auth | `_require_admin_user` in changelog.py still checks `user.id != 1` instead of role | changelog.py:55 |
| r1-003 | MEDIUM | Auth | No per-user API keys; single shared `REVERTO_API_KEY` has no rotation story | web/app.py:96 |
| r1-004 | MEDIUM | Auth | Rate-limiter keyed on `get_remote_address` without X-Forwarded-For parsing | web/app.py:1241; v26-08 |
| r1-005 | MEDIUM | Auth | No TOTP / 2FA layer (Phase B deliverable) | security-model.md 3.3 |
| r1-006 | LOW | Auth | Cookie payload carries stale `u` (username) unused for auth decisions | v27-10 |
| r1-007 | LOW | Auth | `LoginBody.username` has no character-class restriction | v27-09 |
| r1-008 | MEDIUM | Cred storage | Single-host blast radius: Fernet key + ciphertext on one machine | security-model.md 2.1 |
| r1-009 | MEDIUM | Cred storage | No `CredentialProvider` abstraction (Phase A deliverable still open) | security-model.md Part 4 A |
| r1-010 | LOW | Cred storage | Credential secrets live in process heap for duration of engine lifetime | core/credentials.py:137 |
| r1-011 | HIGH | Cred storage | Rotation works per-user but no exchange-key rotation flow exists | core/credentials.py |
| r1-012 | HIGH | Cred storage | `BITGET_PASSPHRASE` read from env-var, not per-user credentials | main_live.py:258 |
| r1-013 | CRITICAL | Service-sep | No signing-service. Main-app owns exchange secrets + trade-signing logic | live/live_engine.py; security-model.md 3.1 |
| r1-014 | HIGH | Service-sep | `live.live_engine.LiveEngine._place_market_order` NotImplementedError — unimplemented core | live/live_engine.py:281 |
| r1-015 | HIGH | Service-sep | `OrderReconciler.fetch_order` commented out; no reconciliation on real fills | live/order_reconciliation.py |
| r1-016 | MEDIUM | Service-sep | No mTLS between components; no CA infrastructure documented | security-model.md 3.1 |
| r1-017 | HIGH | DB fitness | SQLite not Postgres; multi-writer + multi-host blocked | core/database.py |
| r1-018 | MEDIUM | DB fitness | Destructive migrations pattern still used (v3, v4); no Alembic | core/database.py:440 |
| r1-019 | MEDIUM | DB fitness | `_write_lock` in `deal_store` serialises every write across threads | core/deal_store.py:32; v26-12 |
| r1-020 | MEDIUM | DB fitness | N+1 risk in `/api/db/deals` + Active Deals list (one `get_deal_orders` per deal) | web/routes/deals.py:69-78 |
| r1-021 | LOW | DB fitness | No index on `deals.status` for filtered queries — wait, exists | database.py:231 (already indexed) |
| r1-022 | MEDIUM | DB fitness | No backup retention automation beyond a documented cron sample | runbook "Backup procedure" |
| r1-023 | HIGH | Scale | Bot subprocess spawned via `env=os.environ.copy()` leaks all tenants' env-vars | web/app.py:927,1088 |
| r1-024 | HIGH | Scale | `BotRegistry` uses `asyncio.Lock` — single-process only, multi-worker races | web/app.py:744 |
| r1-025 | HIGH | Scale | `StateBroadcaster`/`LogBroadcaster` in-memory; multi-worker needs Redis pubsub | web/app.py:1771,1644 |
| r1-026 | MEDIUM | Scale | `slowapi.Limiter` in-memory; multi-worker defeats per-endpoint rate-limits | web/app.py:1241 |
| r1-027 | MEDIUM | Scale | Module-level `_bitget_client` + `_price_lock`: single choke-point for `/api/price` | web/app.py:401; v26-25 |
| r1-028 | LOW | Scale | LRU `_chart_cache` + `_candles_cache` are in-process | web/app.py |
| r1-029 | MEDIUM | Scale | No central Bitget rate-budget across bots (v27 B-01) | v27-backlog B-01 |
| r1-030 | INFO | Scale | State file I/O via local FS; NFS/shared-storage unsupported | paper/state_io.py |
| r1-031 | MEDIUM | Observability | Audit log is a single `logs/audit.log` file, no per-user segregation + no JSON | v27-11 + Part 4 A deliverable |
| r1-032 | MEDIUM | Observability | `_audit` uses pipe-delimited format; breaks if username has `|` | web/app.py:440 |
| r1-033 | MEDIUM | Observability | Prometheus metrics have no `user_id` label | web/metrics.py |
| r1-034 | LOW | Observability | No trace-IDs; no cross-service correlation plumbing | — |
| r1-035 | LOW | Observability | OSError fallback in API-key bootstrap logs the full key | web/app.py:113; v27-06 |
| r1-036 | INFO | Observability | `TickerError.message` truncation could leak URL with key tails | v27-12 |
| r1-037 | MEDIUM | Deployment | `make deploy` is a naked `git pull` — no zero-downtime, no health-gated rollout | Makefile:73 |
| r1-038 | MEDIUM | Deployment | No rollback procedure for code (git checkout documented, not scripted) | runbook |
| r1-039 | LOW | Deployment | Single host assumed; no HA/failover plan | — |
| r1-040 | LOW | Deployment | No staging environment defined beyond `Reverto-Dev` workstation | runbook |
| r1-041 | HIGH | State | `_state_mtimes` dict keyed on `bot.slug` only — cross-user collisions possible | web/app.py:1830 |
| r1-042 | MEDIUM | State | `BotInfo.read_state` falls back to `"paper"` mode default; never stamps user | web/app.py:621 |
| r1-043 | LOW | Rate-limit | `/auth/logout` at 10/min; but same user can still SWAMP `bump_session_epoch` | v26-04 fixed but design questionable |
| r1-044 | MEDIUM | Rate-limit | Per-user + per-exchange rate-limits not enforced | security-model.md 3.3 |
| r1-045 | MEDIUM | Rate-limit | Expensive `/api/candles` at 20/min; no cost-based throttling | web/routes/chart.py:148 |
| r1-046 | MEDIUM | API design | No API versioning (`/v1/` prefix missing); deprecation path undefined | all routes |
| r1-047 | LOW | API design | Error JSON shape inconsistent (some use `detail`, some `error`) | — |
| r1-048 | LOW | API design | No OpenAPI spec exposed (`docs_url=None` in FastAPI init) | web/app.py:1312 |
| r1-049 | MEDIUM | Legacy | ML `_persist_results` not user-scoped (v27 B-03 still open) | ml/nightly_pipeline.py:382 |
| r1-050 | LOW | Legacy | `web/app.py` at 2000+ lines; still carries middleware + lifespan + broadcasters + registry + helpers | architecture.md refactor-roadmap |
| r1-051 | LOW | Legacy | `DEFAULT_USER` stub still exported from `core.user` (post-Phase-3a residue) | core/user.py:51 |
| r1-052 | LOW | Legacy | Dangling `TODO(phase-3b)` comments in broadcaster helpers | v26-20 |
| r1-053 | MEDIUM | Tests | No cross-tenant data-isolation test that drives routes end-to-end as two users | tests/ |
| r1-054 | LOW | Tests | No regression test asserting `API-key → `user.active=0` → 401` | tests/ |
| r1-055 | HIGH | Service-sep | Whole trade-signing + credential surface lives in main-app (category-level) | security-model.md 3.1 |
| r1-056 | MEDIUM | Error handling | Several `except Exception: pass` swallow errors silently | web/app.py:1767-1768; paper/paper_engine |
| r1-057 | LOW | Error handling | No circuit-breaker around Bitget calls — per-bot ccxt client rate-limits independently | exchanges/bitget.py |
| r1-058 | MEDIUM | Config | No startup validation of required env-vars; missing config fails at first call | web/app.py:96,132 |
| r1-059 | LOW | Config | `.env.example` comprehensive but not cross-validated against `.env` on boot | — |
| r1-060 | MEDIUM | Deps | Requirements pinned by version, not `--hash` | requirements.txt; security-model.md 2.5 |
| r1-061 | INFO | Deps | Transitive deps non-blocking in CI — knowingly loose | .github/workflows/test.yml:146 |
| r1-062 | INFO | Deps | ccxt upgrade cadence still not automated (v26-13) | requirements.txt |
| r1-063 | MEDIUM | Type safety | No mypy config or pyproject.toml — ruff only (E/F/W) | ruff.toml |
| r1-064 | LOW | Type safety | Type-hints coverage inconsistent — routes fully typed, some helpers less so | web/app.py |
| r1-065 | LOW | Module bounds | Circular import pattern web/app.py ↔ routes workable but fragile | web/routes/*.py |
| r1-066 | MEDIUM | Module bounds | `web/app.py` hosts `BotRegistry`, broadcasters, lifespan, `start_bot` — too many concerns | web/app.py |
| r1-067 | MEDIUM | Concurrency | `watch_state_files` blocks on `bot.read_state()` serially; slow FS → back-pressure | web/app.py:1851 |
| r1-068 | LOW | Concurrency | Bitget ccxt clients not thread-safe — documented; no global check | exchanges/bitget.py |
| r1-069 | LOW | Migration | No Alembic or similar tool; destructive drop-and-recreate pattern | core/database.py |
| r1-070 | MEDIUM | Migration | No per-migration unit test; `_migrate_schema` has happy-path coverage only | tests/test_database.py |
| r1-071 | MEDIUM | Parity | LiveEngine real-order path still NotImplementedError; paper is authoritative | v26-15 carry-over |
| r1-072 | LOW | Parity | Parity tests exist but no automated nightly run comparing outputs | scripts/parity_compare.py |
| r1-073 | MEDIUM | CSRF | Relies solely on `SameSite=strict`; no token-based defense-in-depth | v27-05 |
| r1-074 | MEDIUM | Supply chain | Third-party CDN without SRI (lightweight-charts) | v27-04 |
| r1-075 | LOW | Headers | No HSTS emitted by portal itself | v27-08 |
| r1-076 | LOW | Headers | CSP `style-src 'unsafe-inline'` + `connect-src ws: wss:` wildcard | v27-07 |

---

## Part 2 — Multi-tenant Data Isolation

**Scope.** Every DB query in `core/*.py` and `web/routes/*.py`; user-scoping on SELECT/UPDATE/DELETE; route handlers' use of `user.id` vs hardcoded values.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-001 | HIGH | API-key fallback returns hardcoded DEFAULT_USER | web/app.py:365-367 | B |
| r1-002 | HIGH | changelog admin gate uses `user.id != 1` not role | web/routes/changelog.py:55 | B |
| r1-041 | HIGH | `_state_mtimes` cross-user cache-collision | web/app.py:1830-1857 | B |
| r1-049 | MEDIUM | ML `_persist_results` unscoped filename | ml/nightly_pipeline.py:384 | A/B (v27 B-03) |
| r1-053 | MEDIUM | No cross-tenant end-to-end route test | tests/ | A |

**Key findings in detail.**

#### r1-001 — API-key fallback returns admin stub (HIGH — carry-over of v27-01)

**Wat.** `_request_user` at `web/app.py:355-375` treats an `X-API-Key` match as "this request is the server admin":

```python
# web/app.py:365-367
provided = request.headers.get("X-API-Key")
if provided and secrets.compare_digest(provided, _API_KEY):
    return get_default_user()
raise HTTPException(status_code=401, detail="Not authenticated")
```

`get_default_user()` (`core/user.py:54-65`) returns the frozen module-level `DEFAULT_USER = User(id=1, username="admin", role="admin", active=True)`. No DB round-trip; no `active` check; no lookup of the actual admin row.

**Waarom.** Two consequences, both material:

1. An admin deactivated in the DB (`UPDATE users SET active=0 WHERE id=1`) is still fully authenticated on the API-key path. Every other auth path (cookie via `_request_user`, WS via `_ws_extract_user_id`, change-password via `_require_session`) refuses a deactivated user.
2. Every admin-gated route — `/api/emergency-stop` (role=admin guard), `/api/admin/bots/*`, `/api/admin/changelog/*` — is reachable by any script holding the portal's shared `REVERTO_API_KEY` because the stub hardcodes `role="admin"`. Under Phase-3b multi-user deploy a tenant that learns the key (operator slip, CI log capture, shared dev environment) holds cross-tenant admin.

**Waar.** See snippet above. `DEFAULT_USER` at `core/user.py:51`.

**Remediation.**
- **Short term (Phase B):** in the API-key branch, call `user_store.get_user_by_id(1)` and refuse if `user is None or not user.active`. One indexed DB lookup per API-key call is cheap; it closes the active-check gap today.
- **Medium term (Phase B):** introduce per-user API keys in a `user_api_keys` child table, hashed at rest, matched in constant time. Deprecate the module-global `REVERTO_API_KEY`.
- Document current behaviour in `docs/security-model.md` until (1) lands.

**Phase.** B (authentication hardening).

**STATUS.** RESOLVED in fix/r1-001-api-key-respects-active (Sprint 1).

#### r1-002 — Changelog admin gate uses user-id literal, not role (HIGH)

**Wat.** `_require_admin_user` in `web/routes/changelog.py:45-57` checks `user.id != 1`:

```python
def _require_admin_user(user: User = Depends(_request_user)) -> User:
    if user.id != 1:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
```

Inline TODO acknowledges Phase-3b swap.

**Waarom.** When the first non-admin user is seeded — their `id > 1`, `role == "user"` — the check still fires correctly by coincidence. But when the **second admin** is seeded (`role == "admin"`, `id == 2`), this check refuses them and they cannot reach admin changelog CRUD. The v26-02 fix on emergency-stop correctly uses `user.role != "admin"` (see `web/routes/admin.py:121`). The changelog check inconsistency is a latent role-model drift.

**Remediation.** Change to `if user.role != "admin":`. Match emergency-stop's wording. Low risk — test coverage on `tests/test_changelog_api.py` exercises this gate.

**Phase.** B.

**STATUS.** RESOLVED in fix/r1-002-changelog-admin-role-gate (Sprint 1).

#### r1-041 — `_state_mtimes` cross-user cache collision (HIGH)

**Wat.** The `watch_state_files` task at `web/app.py:1833-1896` keys its mtime cache on `bot.slug` alone:

```python
# web/app.py:1830
_state_mtimes: dict[str, float] = {}

# web/app.py:1857
if _state_mtimes.get(bot.slug) == mtime:
    continue
_state_mtimes[bot.slug] = mtime
```

**Waarom.** Two users may own bots with the same slug name (Phase 2 composite-key layout was designed around this). User A's state-file change would update `_state_mtimes["rsi_test"] = new_mtime`. User B's bot — also slugged `rsi_test` — would have its change suppressed because its mtime differs from user A's. The broadcaster fan-out would then be out-of-order or miss updates.

**Remediation.** Key on `(bot.user_id, bot.slug)`:

```python
_state_mtimes: dict[tuple[int, str], float] = {}
...
key = (bot.user_id, bot.slug)
if _state_mtimes.get(key) == mtime:
    continue
_state_mtimes[key] = mtime
```

Trivial change. Should pair with a regression test seeding two users with identical slugs.

**Phase.** B (blocker for first multi-user seed).

**STATUS.** RESOLVED in fix/r1-041-state-mtimes-per-user (Sprint 1).

#### r1-049 — ML `_persist_results` unscoped filename (MEDIUM — v27 B-03 carry)

**Wat.** `ml/nightly_pipeline.py:382-388`:
```python
def _persist_results(bot_slug: str, results: dict) -> Path:
    out_path = Path(__file__).parent / f"results_{bot_slug}.json"
    ...
```
Two users with the same slug would overwrite each other's ML output.

**Remediation.** Signature becomes `_persist_results(user_id: int, bot_slug: str, ...)` with filename `ml/<user_id>/results_{bot_slug}.json` using `core.paths.user_bots_dir(user_id).parent / "ml" / ...` or a new `paths.user_ml_results_path`. v26-18 already fixed the sibling `optimize_parameters` path — this is the symmetric output path.

**Phase.** A/B.

**STATUS.** RESOLVED in `fix/vps-0-sweep` (new `paths.user_ml_results_path(user_id, slug)`; `_persist_results` takes `user_id` as first arg and writes under `ml/<user_id>/results_<slug>.json`; regression test verifies two users with the same slug get separate files).

#### r1-053 — No cross-tenant end-to-end route test (MEDIUM)

**Wat.** `tests/test_web_routes.py` has `test_logout_bumps_only_callers_epoch` which inserts a second user and asserts only caller's epoch bumps. That's the only test that authenticates as two users and observes both. There's no test that:
- Creates user A, writes deal as A, authenticates as B, hits `/api/db/deals` and asserts B sees nothing of A's.
- Creates user A's bot, logs in as B, tries `/api/bots/<A's slug>`, asserts 404 (not 200 with A's state).
- Writes an annotation as A, attempts to DELETE it as B via `/api/db/annotations/<id>`, asserts no-op + audit log.

**Waarom.** The Phase-3a architecture is right (`user_id` is mandatory on every store call), but the guarantee is currently proven by code-reading, not by test. A future refactor that forgets a filter would not be caught.

**Remediation.** Add `tests/test_cross_tenant_isolation.py` with the three scenarios above. Reuses the existing `auth_client` fixture pattern + the second-user insert from `test_logout_bumps_only_callers_epoch`.

**Phase.** A.

**STATUS.** RESOLVED in feat/sprint-2-audit-sweep (tests/test_cross_tenant_isolation.py — 3 E2E tests for deals + annotations; /api/bots listing deferred pending FS-sandbox fixture).

**Items verified clean in this domain:**

- Every `core/deal_store.py` helper takes `user_id` as a required parameter with no defaults; every SELECT/UPDATE/DELETE carries a `WHERE user_id = ?` clause.
- Every route handler that touches deal/order/annotation/backtest/dashboard state pulls `user: User = Depends(_request_user)` and passes `user.id` to the store.
- `registry.get(user_id, slug)` enforces composite-key ownership; no route bypasses it for per-bot reads.
- `registry.all()` (unscoped) is only called from four sites: `admin.py` (admin role-gated), `admin_bots.py` (admin role-gated), `watch_state_files` (infra scan with per-owner delivery filter), `tail_logs` (infra scan with per-owner delivery filter). All four are intentional cross-user scans.
- Per-user `.enc` files under per-user Fernet keys — cryptographic isolation of exchange credentials is real, not nominal.

---

## Part 3 — Authentication & Session Management

**Scope.** Session cookie strategy, password hashing, TOTP readiness, API-key auth vs session auth, logout flow, account lockout, refresh tokens.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-001 | HIGH | API-key stub | web/app.py:365 | B |
| r1-003 | MEDIUM | Single shared REVERTO_API_KEY | web/app.py:96 | B |
| r1-004 | MEDIUM | Rate-limiter IP-only | web/app.py:1241 | B |
| r1-005 | MEDIUM | No TOTP layer | security-model.md 3.3 | B |
| r1-006 | LOW | Cookie carries unused `u` | v27-10 | B |
| r1-007 | LOW | Username character class | v27-09 | B |

#### r1-003 — Single shared `REVERTO_API_KEY` (MEDIUM)

**Wat.** `web/app.py:96` reads one process-wide env-var. Every script / CI tool / admin automation shares it. There is no rotation flow that doesn't require a portal restart, and no per-user API-key concept.

**Waarom.** In a multi-tenant SaaS each tenant that wants script/CI access needs its own key. Rotation of one tenant's key must not log out every other tenant's automation. Today's model can't express this.

**Remediation.** Phase B: add `user_api_keys` table with `(user_id, key_hash, label, created_at, last_used_at)`. Auth flow hashes the incoming header and matches in constant time. Replace the module-global `_API_KEY` with this lookup. Portal admin UI issues + rotates + revokes per-key. The existing `X-API-Key` header shape stays unchanged.

#### r1-005 — TOTP layer missing (MEDIUM — Phase B deliverable)

**Wat.** Security-model.md Part 3.3 makes TOTP verification a required step between `verify_password` and cookie minting. Current `auth_login` at `web/routes/auth.py:150-243` sets the cookie directly after bcrypt success.

**Remediation.** Phase B deliverable. Needs `users.totp_seed_encrypted` + `totp_dek_wrapped` columns, enroll/verify endpoints, `pyotp` or equivalent, and the login flow wired so TOTP is required for new logins once the user has enrolled. Old sessions untouched (cookie carries no TOTP state).

**Items verified clean in this domain:**

- `verify_password` is constant-time (bcrypt.checkpw + single-branch failure path; no user-enumeration via timing).
- Session cookie is signed (itsdangerous), `HttpOnly`, `SameSite=strict`, `Secure` in production; TTL 24h absolute.
- Per-user `session_epoch` invalidates only caller's cookies on logout + password-change (not global).
- `_verify_session_cookie` handles `BadSignature`/`SignatureExpired` explicitly + catch-all for malformed payloads.
- Exponential backoff + per-account rate-limit + anomaly log (`login-security-hardening` branch shipped) is real; covered by `tests/test_web_routes.py::TestLoginSecurityHardening`.
- HIBP k-anonymity password-breach check on password-change (`core/password_breach.py`).

---

## Part 4 — Credential Storage Architecture

**Scope.** `keys/<uid>.key` + `credentials/<uid>/*.enc` layout; Fernet rotation; envelope-encryption plan vs current state; single-host blast radius.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-008 | MEDIUM | Single-host blast-radius: key + ciphertext on one machine | security-model.md 2.1 | C |
| r1-009 | MEDIUM | No `CredentialProvider` abstraction (Phase A deliverable) | core/credentials.py | A |
| r1-010 | LOW | Decrypted secrets in process heap during engine lifetime | core/credentials.py:137 | C |
| r1-011 | HIGH | No exchange-key rotation flow | core/credentials.py | C/D |
| r1-012 | HIGH | `BITGET_PASSPHRASE` is env-var, not per-user credential | main_live.py:258 | C |

#### r1-012 — `BITGET_PASSPHRASE` is a process-wide env-var (HIGH)

**Wat.**
```python
# main_live.py:258-266
passphrase = os.environ.get("BITGET_PASSPHRASE", "")
if not passphrase:
    logger.error("BITGET_PASSPHRASE env var is required for live Bitget")
    return None
return BitgetExchange(
    api_key=keys["api_key"], api_secret=keys["api_secret"],
    passphrase=passphrase, paper=False,
)
```

`get_keys(name, user_id)` returns per-user api_key + api_secret from the `.enc` file, but the passphrase is a Bitget-specific third piece of credential material (user-chosen at API-key creation time on bitget.com) that's currently read from the portal process's shared environment.

**Waarom.** In a multi-tenant deploy every user has their own Bitget account with their own passphrase. Reading from one env-var means either (a) every tenant shares one Bitget account/passphrase (obviously wrong), or (b) live-mode fails for every tenant except the one whose passphrase happens to be in the env. Per security-model.md Part 3.5 "Passphrase: door user gekozen tijdens key-creation. Reverto vraagt deze apart, encrypt en slaat op in de signing-service (samen met api_key + api_secret)."

**Remediation.** Extend the `.enc` payload schema to carry `passphrase` alongside `api_key` + `api_secret`:

```python
# core/credentials.py save_keys signature extension
def save_keys(exchange, api_key, api_secret, user_id, *, passphrase: str = "") -> None
```

`get_keys` returns the triple; `main_live.py` reads from there. `/api/exchanges/{name}/keys` route body gets an optional `passphrase` field. Migration path for existing `.enc` files: add a `passphrase` key on next write (read-side treats missing as empty → live-mode refuses until user re-uploads keys with passphrase).

**Phase.** C (service-separation also moves the whole flow into the signing-service).

**STATUS.** RESOLVED in fix/r1-012-bitget-passphrase-per-user (Sprint 1).

#### r1-011 — No exchange-key rotation flow (HIGH)

**Wat.** `rotate_fernet_key(user_id)` exists and is well-engineered (commit-order contract, backup, advisory lock). But that's only the Reverto-side encryption-key rotation — it re-wraps existing ciphertext under a new Fernet key. It does NOT rotate the actual exchange API credentials. Security-model.md Part 2.7 scenario explicitly contemplates exchange-key rotation as a multi-step flow (pause trading, rotate, verify, kill old key). No such flow is implemented.

**Remediation.** Phase C/D: add `/api/exchanges/{name}/rotate` endpoint that:
1. Marks the user's bots as paused.
2. Accepts new api_key + api_secret + passphrase.
3. Tests with a balance-read call.
4. On success, overwrites the `.enc` file and unpauses.
5. On failure, keeps the old credentials intact.

**Phase.** D (user-facing security).

#### r1-009 — `CredentialProvider` interface (MEDIUM — Phase A deliverable)

**Wat.** Security-model.md Part 4 Phase A lists "`CredentialProvider` interface-abstractie toegevoegd in `core/credentials.py`: de huidige per-user Fernet-implementatie wordt een concrete implementation van een abstract interface, zonder nu een tweede implementation te bouwen." No such abstraction exists; `save_keys` + `get_keys` are bare module-level functions.

**Remediation.** Introduce `class CredentialProvider(ABC)` with `save_keys`, `get_keys`, `has_keys`, `delete_keys`, `list_exchanges_with_keys`, `rotate_fernet_key` as abstract methods. Current implementation becomes `FernetFileCredentialProvider`. Phase-C can then add `SigningServiceCredentialProvider` that RPC's to the signing-service without touching call sites. Small refactor, low risk.

**Phase.** A (Foundation).

---

## Part 5 — Service Separation Readiness

**Scope.** Monolith vs target three-component (web / signer / watchdog); code that must move; mTLS + request-signing; interface-ready seams today.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-013 | CRITICAL | No signing-service; secrets + trade-signing live in main-app | live/live_engine.py; security-model.md 3.1 | C |
| r1-014 | HIGH | LiveEngine real-order path still NotImplementedError | live/live_engine.py:281 | C (scoped) |
| r1-015 | HIGH | OrderReconciler.fetch_order commented out | live/order_reconciliation.py | C |
| r1-016 | MEDIUM | No mTLS infrastructure | security-model.md 3.1 | C |
| r1-055 | HIGH | Category-level: whole signing path in main-app | — | C |

#### r1-013 — No signing-service exists (CRITICAL — architectural)

**Wat.** Security-model.md Part 3.1 specifies a three-process architecture: `reverto-web` (main app, no secrets), `reverto-signer` (exchange credentials + scope whitelist + cap enforcement + order signing + approval verification), `reverto-watchdog` (independent balance drift detector). Today only the first exists; the second and third are not even scaffolded.

**Waarom.** In the target multi-tenant threat model, server compromise of the main-app must NOT grant the attacker the ability to place orders on behalf of any user. Without service-separation, today's main-app holds:
- All users' Fernet keys (`keys/*.key`).
- All users' exchange credentials (`credentials/<uid>/*.enc`).
- The ccxt clients that sign API requests to exchanges.
- The password hashes, session epochs, TOTP seeds (future).

A single RCE on this process is a full N-user custody breach. Security-model.md's Part 2.1 table explicitly lists "Lees alle exchange API-keys" as **Ja** in current state, **Nee** in target state because the signing-service replaces that. This is the largest structural gap.

**Remediation.** Phase C deliverable (security-model.md Part 4 C). Concrete sequence:
1. Introduce `core.credentials.CredentialProvider` abstraction (see r1-009) and `core.signing.SignerClient` scaffolding that today returns local-in-process calls, Phase-C redirects to RPC.
2. Stand up `reverto-signer` as a FastAPI app in a second Docker container, starting as a "dumb proxy" that forwards calls to the local exchange client. Everything stays in one host but separated by process boundary + mTLS.
3. Move `core/credentials.py` + `exchanges/base_exchange.py` + `exchanges/bitget.py` + `exchanges/kraken.py` into the signer container.
4. Main-app only holds the public `api_key` for display; every `place_trade`/`fetch_balance`/`cancel_order` call goes via mTLS RPC to the signer.
5. Add scope-whitelist enforcement in the signer (Phase D-ish; Phase C is enough if it ships the interface).

**Phase.** C.

#### r1-014 — LiveEngine real-order path NotImplementedError (HIGH, v26-15 carry)

**Wat.** `live/live_engine.py:281`:
```python
raise NotImplementedError(
    "Live order placement is Phase-3 work; dry-run only in Phase 1/2"
)
```

Live-mode bots can run in dry-run today, but no path exists for real-order placement. Paper-parity test (`scripts/parity_compare.py`) is what's used to argue paper ≈ live, but the actual live path is unexercised.

**Remediation.** Phase C gate deliberately blocks this — real-order path will only be re-enabled after the signer exists (so credentials don't live in main-app when real orders start flowing). Out of scope for Phase A/B.

**Phase.** C.

#### r1-015 — OrderReconciler `fetch_order` branch commented out (HIGH)

**Wat.** `live/order_reconciliation.py` has a scaffolded reconciler but the `fetch_order` poll branch is commented out pending Phase 3 integration. With no reconciliation, a silent fill on Bitget that the local state doesn't see would cause the engine's tick loop to place duplicate DCA orders (exchange-side idempotency via `clientOrderId` catches this, but the local state ledger diverges).

**Remediation.** Couples with r1-014; don't enable until the signer ships.

---

## Part 6 — Database Fitness

**Scope.** SQLite → Postgres migration readiness; schema versioning; destructive migration safeguards; connection pooling; transactions; N+1 patterns; backup/restore.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-017 | HIGH | SQLite, not Postgres | core/database.py | C |
| r1-018 | MEDIUM | Destructive migrations pattern still used | core/database.py:440 | C |
| r1-019 | MEDIUM | `_write_lock` serialises all DB writes | core/deal_store.py:32 | C |
| r1-020 | MEDIUM | N+1 risk in `/api/db/deals` + Active Deals list | web/routes/deals.py:69 | B |
| r1-022 | MEDIUM | No backup retention automation beyond docs sample | runbook | G |

#### r1-017 — SQLite (HIGH — Phase C blocker)

**Wat.** `core/database.py` uses SQLite in WAL mode with `busy_timeout=5000` and `synchronous=NORMAL`. This is fine for single-host single-process. For multi-tenant SaaS on multi-host infrastructure it is a blocker.

**Waarom.** SQLite doesn't support network access; multi-host deploys cannot share the DB. WAL-mode allows concurrent readers but only one writer; under bursty write loads (say 100 tenants each with 3 bots committing on each tick), the combined write rate easily saturates a single-writer SQLite. Postgres with pgbouncer is the standard answer.

**Remediation.** Phase C deliverable. Alembic-based migrations (replaces the current `_migrate_schema` bespoke flow). Connection pooling via `psycopg[pool]` or similar. `core.database.get_db()` becomes an abstraction layer; SQLAlchemy Core is the cheap upgrade path that keeps raw-SQL compatible.

**Phase.** C.

#### r1-019 — `_write_lock` serialises every write across threads (MEDIUM, v26-12 carry)

**Wat.** `core/deal_store.py:32` owns a module-level `threading.Lock()` that's acquired by every write path. Under portal-async + engine-daemon-threads + test-fixture threads, this is the single writer-serialiser.

**Waarom.** Measure before acting — the v26-12 finding is INFO today. But with 100+ bots committing DCA updates every tick, the Python-side lock bottleneck arrives before the DB's own. Relevant for Phase-C scalability planning.

**Remediation.** Once on Postgres, remove the module lock; let the DB do its thing. WAL-mode SQLite can also do concurrent writers via `BEGIN IMMEDIATE` but Phase C-move is cleaner.

#### r1-020 — N+1 in `/api/db/deals` (MEDIUM)

**Wat.**
```python
# web/routes/deals.py:69-78
def _query():
    deals = deal_store.get_deals(
        user_id=uid, bot_slug=bot_slug, status=status, limit=limit,
    )
    return [
        {"deal": d,
         "orders": deal_store.get_deal_orders(d["id"], user_id=uid)}
        for d in deals
    ]
```

Each `get_deal_orders` is a separate SQL query. For `limit=1000` that's 1001 queries.

**Remediation.** Add a `deal_store.get_deals_with_orders(user_id, bot_slug, status, limit)` helper that does one `LEFT JOIN orders` query and groups order rows into their parent deal. ~30 lines. Mentioned here as MEDIUM not HIGH because current UI paginates at 100.

**STATUS.** RESOLVED in feat/sprint-2-audit-sweep (batch-fetch via `get_orders_for_deal_ids`; one IN-list query instead of N+1, response shape unchanged).

#### r1-018 — Destructive migrations (MEDIUM, v26-10 guard in place)

**Wat.** v3 and v4 schema migrations drop + recreate owned tables. v26-10's guard (env-var opt-in + auto-backup) is live, but the pattern itself remains the only migration style. Future schema changes that actually need ALTER TABLE ADD COLUMN have to work around `_SCHEMA_STATEMENTS` at `database.py:108-280` being declarative-idempotent for table CREATEs.

**Remediation.** Phase C introduces Alembic; backfill versioned migration scripts for v3, v4, v5, v6, v7 as retrospective starting points; use forward-only additive from there. Preserves the "no data loss" property as a migration-time guarantee.

**Items verified clean in this domain:**

- Every owned table (`deals`, `orders`, `chart_annotations`, `backtest_runs`, `dashboard_layouts`) has a `user_id NOT NULL REFERENCES users(id)` FK plus an index on `(user_id, ...)`.
- `sqlite3.Row` factory is set; dict-style access in callers is safe.
- `get_db` caches connections per-thread with a version counter for test-isolation (handles the `set_db_path` + anyio worker-thread race documented at `database.py:86`).
- `PRAGMA foreign_keys=ON` is set on every connection.
- WAL checkpoint command exists in runbook.

---

## Part 7 — Scalability Bottlenecks

**Scope.** BotRegistry locks, WebSocket broadcasters, state-file I/O, file-locks, rate-limiter, caches.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-023 | HIGH | Subprocess env inheritance leaks tenants' env-vars | web/app.py:927,1088 | B |
| r1-024 | HIGH | BotRegistry single-process only | web/app.py:744 | C |
| r1-025 | HIGH | Broadcasters in-memory | web/app.py:1644,1771 | C |
| r1-026 | MEDIUM | SlowAPI Limiter in-memory | web/app.py:1241 | B |
| r1-027 | MEDIUM | Module-level `_bitget_client` + `_price_lock` | web/app.py:401 | C |
| r1-028 | LOW | `_chart_cache` + `_candles_cache` in-process | web/app.py | G |
| r1-029 | MEDIUM | No central Bitget rate-budget (v27 B-01) | v27-backlog | C |
| r1-030 | INFO | State file I/O via local FS | paper/state_io.py | G |

#### r1-023 — Subprocess env inheritance (HIGH)

**Wat.** `start_bot` and `start_bot_dry_run` both do:
```python
env = os.environ.copy()
env["PYTHONPATH"] = str(BASE_DIR)
# ...
with open(bot.log_file, "a") as log_out:
    proc = subprocess.Popen([...], env=env, ...)
```

Every env-var the portal process holds — including `BITGET_PASSPHRASE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `REVERTO_API_KEY`, `REVERTO_SECRET_KEY` — is passed into the bot subprocess.

**Waarom.** In a multi-tenant SaaS model each tenant's bot should have access only to their own secrets. Today a tenant who compromises their bot subprocess (via e.g. a malicious YAML config leading to a logic bug in the engine) reads every other tenant's Telegram token + every tenant's Bitget passphrase + the portal's admin API key. Shared-env is the multi-tenant anti-pattern.

**Remediation.** Filter env to a minimal allowlist:
```python
_BOT_ENV_ALLOWLIST = {
    "PATH", "HOME", "LANG", "LC_ALL", "TZ",
    "PYTHONPATH", "PYTHONUNBUFFERED",
    "REVERTO_LOG_LEVEL", "DRY_RUN",
}
env = {k: v for k, v in os.environ.items() if k in _BOT_ENV_ALLOWLIST}
```

Then inject the PER-USER values explicitly — the bot already resolves `get_keys(name, user_id)` internally for api_key+api_secret, so only `BITGET_PASSPHRASE` needs per-user plumbing (see r1-012). This finding couples with r1-012.

**Phase.** B.

**STATUS.** RESOLVED in fix/r1-023-subprocess-env-whitelist (Sprint 1).

#### r1-024 — BotRegistry single-process (HIGH — Phase C blocker)

**Wat.** `web/app.py:726-904` builds `BotRegistry` with `asyncio.Lock()` for `_bots`, `_starting`, and the refresh TTL. This works perfectly for one uvicorn worker; it does not work for `uvicorn --workers 4` on the same host.

**Waarom.** Multi-worker uvicorn is the standard horizontal scaling in a single-host deploy. Each worker would hold an independent `BotRegistry` with independent `_starting` sets and independent refresh timers. A `POST /api/bots/{slug}/start` can arrive at worker 2 while the user clicks a second time on worker 3 — both call `begin_start` against their own sets, both spawn. The pid-file sentinel pattern catches the second spawn at the OS level eventually, but that's racy.

**Remediation.** Phase C: move bot-lifecycle into the signing-service with one authoritative registry, or move `_starting` set into Redis with a SETNX pattern. Bots themselves are already OS processes managed via PID files on disk; only the cache + in-progress set leaks.

**Phase.** C.

#### r1-025 — Broadcasters in-memory (HIGH)

**Wat.** `LogBroadcaster._clients: dict[str, set[WebSocket]]` and `StateBroadcaster._clients: set[WebSocket]` hold WebSocket references in one process's memory. `watch_state_files` and `tail_logs` broadcast via these objects.

**Waarom.** Multi-worker: a client connects to worker 1 (`/ws/state`), `watch_state_files` runs on worker 2 and broadcasts there — worker 1's client never sees the update. Classic sticky-session or pubsub-fan-out problem.

**Remediation.** Phase C: introduce Redis pub/sub between workers. Each worker's broadcaster subscribes to the Redis channel; `watch_state_files` runs on exactly one worker (leader-elect) and PUBLISHes there. Alternative: pin WS connections to the broadcast leader (sticky-session at the reverse-proxy level), which is simpler but loses load-balancing.

**Phase.** C.

#### r1-026 — SlowAPI Limiter in-memory (MEDIUM)

**Wat.** `limiter = Limiter(key_func=get_remote_address)` at `web/app.py:1241`. In-memory default storage. Every worker has its own token bucket.

**Waarom.** Multi-worker: an attacker who hits `5/minute` on worker 1 can fire 5 more at worker 2. Per-endpoint rate-limits become per-endpoint-per-worker. SlowAPI supports Redis backend (`storage_uri="redis://..."`) — straightforward migration.

**Remediation.** Phase B: configure the `storage_uri` with a Redis URL; documented in security-model.md as "per-exchange rate-limiting — signing-service enforces" which implies a shared store.

**Phase.** B.

#### r1-027 — Module-level `_bitget_client` + `_price_lock` (MEDIUM)

**Wat.** `web/app.py:401` creates one ccxt client used by `/api/price`; `_price_lock` serialises calls. Under heavy dashboard poll, every price fetch queues behind this lock.

**Remediation.** Move to signing-service (Phase C); ccxt is a syncio lib so the pattern won't change, but it no longer blocks web-app routes.

---

## Part 8 — Observability

**Scope.** Logging, audit-log, metrics, error tracking, health-checks, multi-tenant filtering, trace-IDs.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-031 | MEDIUM | `audit.log` single cross-user file, not JSON-structured | v27-11 | A |
| r1-032 | MEDIUM | `_audit` uses pipe-delimited format | web/app.py:440 | A |
| r1-033 | MEDIUM | Prometheus metrics have no `user_id` label | web/metrics.py | G |
| r1-034 | LOW | No trace-IDs for cross-service correlation | — | C |
| r1-035 | LOW | OSError fallback logs the full API key | v27-06 | A |
| r1-036 | INFO | TickerError truncation may leak URL fragments | v27-12 | — |

#### r1-031 — Audit log not per-user / not JSON (MEDIUM — Phase A deliverable)

**Wat.**
```python
# web/app.py:440-442
def _audit(action: str, slug: str = "-", key_hint: str = "-") -> None:
    _audit_logger.info("%s | %s | %s", action, slug, key_hint)
```

Formatter: `"%(asctime)s | %(message)s"`. Everything lands in `logs/audit.log`, rotated at 5 MB × 3 backups = 20 MB total. No `user_id` field, no IP, no request-id, no result code.

**Waarom.** Security-model.md Part 4 Phase A: "Structured audit-logging: huidige `_audit(action, slug, actor)` in `web/app.py` uitbreiden naar JSON-structuur met timestamp, user_id, ip, result. Preparatie voor externe log-aggregator in Phase G." This is a named Phase A deliverable.

Consequences of not doing this before multi-tenant:
- Incident-response forensics ("what did user 42 do this week?") require grepping a mixed file; ambiguous when usernames overlap.
- GDPR right-to-export requires per-user audit lines; current format can't deliver.
- 20 MB total budget rotates away evidence under real tenant load (see v27 Phase 2 candidate note).

**Remediation.** Change to:
```python
def _audit(action: str, slug: str = "-", actor_id: Optional[int] = None,
          actor_username: str = "-", ip: str = "-",
          result: str = "ok", extra: dict = None) -> None:
    _audit_logger.info(json.dumps({
        "ts": datetime.now(UTC).isoformat(),
        "action": action, "slug": slug,
        "actor_id": actor_id, "actor": actor_username,
        "ip": ip, "result": result,
        **(extra or {}),
    }))
```

Raise the backupCount to cover expected tenant-scale evidence retention (e.g. 30 days).

**Phase.** A.

#### r1-032 — Pipe-delimited format breaks on `|` in username (MEDIUM)

**Wat.** Today's format is `timestamp | action | slug | key_hint`. If a future signup flow accepts a `|` in username, the column layout breaks silently.

**Remediation.** Covered by r1-031 (JSON).

#### r1-033 — Prometheus metrics lack `user_id` (MEDIUM — Phase G blocker)

**Wat.** `web/metrics.py` labels every counter/gauge on `bot_slug` + `mode`. With composite `(user_id, slug)` registry, two users running identical slugs produce indistinguishable metrics.

**Remediation.** Add `user_id` as a label on every metric that scopes to a bot. Prometheus cardinality: each tenant × bot × metric row. At a target of 100 tenants × 3 bots × 10 metrics = 3 000 series — within budget.

**Phase.** G.

**STATUS.** RESOLVED in feat/sprint-2-audit-sweep (core.user_store.validate_username — also closes r1-007).

---

## Part 9 — Deployment & Operations

**Scope.** Single-server vs multi-server readiness; secret management; zero-downtime deploys; rollback; config drift; Makefile environment portability.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-037 | MEDIUM | `make deploy` is naked `git pull` | Makefile:73 | G |
| r1-038 | MEDIUM | No scripted rollback | runbook | G |
| r1-039 | LOW | Single-host assumed; no HA | — | G |
| r1-040 | LOW | No staging tier beyond Reverto-Dev | runbook | G |

#### r1-037 — `make deploy` naked `git pull` (MEDIUM)

**Wat.**
```make
deploy:
    @git pull origin main
    @echo "[deploy] Next steps (manual): make restart, ..."
```

No health check, no canary, no automatic rollback; operator does manual restart.

**Waarom.** Today's workflow: operator SSHes in, runs `make deploy` (pulls), then `make restart` (restarts portal). Bots keep running via their own subprocesses — OK for code that only touches portal logic. But a schema-migration release or an engine-protocol change can't be rolled out this way without bot downtime.

**Remediation.** Phase G:
1. `make deploy` → `git fetch` + lint + `systemctl` managed services.
2. A real canary flow requires a second host (see r1-039).
3. Rollback: record pre-deploy commit SHA; `make deploy-rollback` checks out the SHA and restarts.

**Phase.** G.

**Items verified clean in this domain:**

- `.env.example` is comprehensive and git-ignored (.env).
- `start.sh` sources .env with `set -a/set +a` — env-vars reach subprocesses.
- `setup_admin` flow is scripted + documented (v26-19 fixed).
- Destructive-migration opt-in gate is live (v26-10).
- Dry-run mode exists (live-dry) for paper/live parity.

---

## Part 10 — Rate Limiting

**Scope.** Coverage; consistency; per-user vs per-IP; auth endpoint protection; expensive endpoint protection.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-004 | MEDIUM | Key func `get_remote_address` — no X-Forwarded-For | web/app.py:1241 | B |
| r1-026 | MEDIUM | SlowAPI in-memory | web/app.py:1241 | B |
| r1-044 | MEDIUM | No per-user + per-exchange rate-limits | security-model.md 3.3 | C |
| r1-045 | MEDIUM | `/api/candles` cost-unaware limit | web/routes/chart.py:148 | C |

58 `@limiter.limit` decorators cover every mutating + expensive route. Coverage is adequate; the gap is what's keyed.

#### r1-004 — Rate-limiter IP-only (MEDIUM — v26-08 carry)

**Wat.** `Limiter(key_func=get_remote_address)`. Behind nginx/caddy the remote address is the proxy's IP — every tenant shares one bucket. The v26-08 finding's inline comment acknowledges this as Phase-3b work.

**Remediation.** Swap to a custom `key_func` that parses X-Forwarded-For after trusting a proxy allowlist:

```python
def _rate_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd and request.client.host in _TRUSTED_PROXIES:
        return fwd.split(",")[0].strip()
    return get_remote_address(request)
```

Trusted-proxy allowlist reads from env.

**Phase.** B.

**STATUS.** RESOLVED in feat/sprint-2-audit-sweep (X-Forwarded-For rate-limit key).

#### r1-044 — No per-user or per-exchange rate-limits (MEDIUM)

**Wat.** All current limits are per-IP. Security-model.md Part 3.3 prescribes three-dimensional limits: per-IP (current), per-user (new, post-auth), per-exchange (new, in signing-service). No code paths implement the latter two.

**Remediation.** Phase B wires per-user (requires shared-storage limiter, see r1-026). Phase C puts per-exchange in the signing-service.

---

## Part 11 — API Design Consistency

**Scope.** REST conventions; HTTP methods; error format; status codes; versioning; OpenAPI.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-046 | MEDIUM | No API versioning prefix | all routes | G |
| r1-047 | LOW | Error JSON shape inconsistent | — | G |
| r1-048 | LOW | No OpenAPI docs (`docs_url=None`) | web/app.py:1312 | G |

#### r1-046 — No API versioning (MEDIUM)

**Wat.** Routes live at `/api/bots`, `/api/deals`, `/api/admin/*` without version prefix. The moment a tenant integrates against Reverto's API for automation, breaking changes become a coordination problem.

**Remediation.** Phase G: introduce `/api/v1/` prefix. Keep the current paths as aliases for one major release cycle. Document deprecation policy.

**Items verified clean:**

- HTTP method semantics are correct (GET read-only, POST/PUT/PATCH for mutations, DELETE for removal).
- Every mutating route carries `@limiter.limit`.
- Most routes raise `HTTPException(status_code, detail=...)` — consistent single key.
- Status codes are right (404, 409, 422, 413, 429, 500).
- `docs_url=None, redoc_url=None` is intentional — no spec leak in production — but a private spec endpoint would be nice for SDK generation.

---

## Part 12 — Legacy Code Identification

**Scope.** Single-tenant smells, hardcoded user refs, TODO/FIXME, dead code, duplication, god-objects, inconsistency.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-049 | MEDIUM | ML `_persist_results` unscoped (v27 B-03) | ml/nightly_pipeline.py:382 | A |
| r1-050 | LOW | `web/app.py` is ~2000+ lines, god-object shape | web/app.py | A |
| r1-051 | LOW | `DEFAULT_USER` stub still exported | core/user.py:51 | A |
| r1-052 | LOW | Dangling TODO(phase-3b) in broadcaster helpers | v26-20 | A |

Scan summary: no dead-code blocks I could find. Commented-out code is minimal (fetch_order in reconciliation is flagged). `web/app.py` still at 2000+ lines after v22's 36% reduction — v22 deliberately stopped at the current cut.

#### r1-051 — `DEFAULT_USER` still exported (LOW)

**Wat.** `core/user.py:51`:
```python
DEFAULT_USER = User(id=1, username="admin", role="admin")

def get_default_user() -> User:
    return DEFAULT_USER
```

Usage is limited to `_request_user` API-key fallback (r1-001). Once r1-001 is fixed the stub has no other callers except possibly tests. Worth a grep sweep + removal.

**STATUS.** RESOLVED in feat/sprint-2-audit-sweep (constant + helper deleted from `core/user.py`; test_user_model.py dropped the two dataclass-only tests that asserted the stub shape).

---

## Part 13 — Test Coverage for Multi-Tenant

**Scope.** User-isolation scenarios, cross-user leak tests, session-epoch tests, role-gate tests, concurrency tests, overall coverage.

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-053 | MEDIUM | No cross-tenant end-to-end route test | tests/ | A |
| r1-054 | LOW | No regression test for API-key-on-deactivated-admin | tests/ | B |

**Observations:**
- 1208 tests passing (verified in prior sessions).
- Coverage floor 80% enforced in CI.
- `tests/test_broadcasters.py` + `tests/test_cross_bot_deal_isolation.py` + `tests/test_admin_bots_routes.py` have two-user scenarios.
- `tests/test_web_routes.py::test_logout_bumps_only_callers_epoch` exercises per-user session_epoch.
- `tests/test_credentials.py` + `tests/test_credentials_lock.py` + `tests/test_credentials_rotation.py` verify per-user key isolation.

**Coverage gaps:**
- No test drives `/api/db/deals` as user B and asserts user A's deals are invisible.
- No test hits admin route as non-admin second user and asserts 403.
- No test confirms that deactivating admin (`active=0`) on the cookie path locks them out but on the API-key path grants access — the bug proving r1-001 exists.
- No test around `_state_mtimes` cache collision (r1-041).

**Remediation.** Add `tests/test_cross_tenant_isolation.py`:
```python
class TestDealIsolation:
    def test_user_b_cannot_see_user_a_deals(auth_client):
        # seed two users, write deal as A, query as B, assert empty
        ...

    def test_user_b_cannot_delete_user_a_annotation(auth_client):
        ...

class TestAdminGate:
    def test_non_admin_blocked_from_emergency_stop(auth_client): ...
    def test_non_admin_blocked_from_changelog_crud(auth_client): ...

class TestApiKeyActiveBypass:
    """Regression for r1-001."""
    def test_api_key_refuses_deactivated_admin(raw_client):
        # UPDATE users SET active=0 WHERE id=1
        # hit /api/bots with X-API-Key
        # assert 401 (will fail today → red to confirm the bug)
        ...
```

---

## Part 14 — Error Handling & Fail Modes

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-056 | MEDIUM | `except Exception: pass` swallows silently in WS paths | web/app.py:1767-1768 | A |
| r1-057 | LOW | No circuit-breaker around Bitget calls | exchanges/bitget.py | C |

**Observations:**
- Fail-closed defaults are present (see security-model.md Part 1.3): `_scan_user_dirs` fail-closed after N DB failures; `verify_password` NULL-hash refusal; destructive-migration opt-in.
- `exchanges/bitget.py` has bounded retries + idempotency (clientOrderId).
- Paper engine has `TICK_ERROR_PERSISTENT_THRESHOLD` + graceful error classification (`paper/errors.py`).
- Background tasks in lifespan have timeout-bounded cancellation (2s).

**Gaps:**
- A handful of `except Exception: pass` in WS disconnect handlers (`web/app.py:1766-1768`) are unlikely to hide real bugs but would silently swallow unexpected exceptions. Not a material risk.

---

## Part 15 — Config Management

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-058 | MEDIUM | No startup env-var validation | web/app.py:96,132 | A |
| r1-059 | LOW | .env.example not cross-validated against .env | — | A |

#### r1-058 — No startup validation (MEDIUM)

**Wat.** `REVERTO_API_KEY` missing → ephemeral fallback + WARNING. `REVERTO_SECRET_KEY` missing → ephemeral fallback + WARNING. But `BITGET_PASSPHRASE`, `TELEGRAM_BOT_TOKEN`, etc. are only checked when first used. A forgotten `BITGET_PASSPHRASE` in prod fails on the first live trade instead of at portal start.

**Remediation.** Add a `_validate_env_on_startup()` helper called from the top of `main_web.py`. Required: none hard-required today; optional-with-warning: all the exchange + telegram tokens + `REVERTO_ADMIN_PW` for first-install. Report a single summary log line "Env config: OK (A/B/C set)" or "Env config: MISSING BITGET_PASSPHRASE (live bots will fail)".

---

## Part 16 — Dependency Hygiene

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-060 | MEDIUM | Pin-by-version, not `--hash` | requirements.txt | A |
| r1-061 | INFO | Transitive deps non-blocking in CI (documented) | test.yml:146 | — |
| r1-062 | INFO | ccxt upgrade cadence manual | — | — |

#### r1-060 — No hash-pinning (MEDIUM)

**Wat.** Security-model.md Part 2.5 lists pin-by-hash as target state; current requirements.txt pins by version only. A supply-chain attacker who compromises a maintainer's PyPI account can push a backdoored patch release under the same version.

**Remediation.** `pip-compile --generate-hashes` against `requirements.in`. Adds ~30 lines per package; materially more robust.

**Phase.** A.

**STATUS.** RESOLVED in feat/sprint-2-audit-sweep (_validate_config in lifespan).

**Items verified clean:**

- `pip-audit --strict` in CI blocks on direct-dep CVEs.
- Smoke-import check catches accidental lazy-import drift.
- `requirements-ml.txt` constrains to `requirements.txt` (v26-26 fixed).

---

## Part 17 — Type Safety

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-063 | MEDIUM | No mypy config, no pyproject.toml | ruff.toml | A |
| r1-064 | LOW | Type hints coverage inconsistent | web/app.py | A |

#### r1-063 — No mypy (MEDIUM)

**Wat.** Ruff with `select = ["E", "F", "W"]` catches pyflakes + pycodestyle basics but no type errors. No `mypy.ini` or `pyproject.toml [tool.mypy]`. Routes are well-typed (Pydantic bodies, `User` dataclass, FastAPI Depends) but helpers in web/app.py have mixed coverage.

**Remediation.** Add `pyproject.toml` with mypy config pinned to `strict = false` initially. Focus on `core/`, `web/routes/`, route-facing helpers. Adding full strict mode on a 15 000 LoC codebase is a multi-day refactor; starting non-strict catches the obvious errors.

**Phase.** A.

---

## Part 18 — Module Boundaries

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-065 | LOW | Circular import pattern via bottom-of-file include | web/routes/*.py | A |
| r1-066 | MEDIUM | `web/app.py` hosts too many concerns | web/app.py | A |

#### r1-066 — `web/app.py` overload (MEDIUM)

**Wat.** `web/app.py` still contains: AuthMiddleware + SecurityHeadersMiddleware + session helpers + `_request_user`/`_request_actor` + `_create_session_cookie`/`_verify_session_cookie` + BotInfo class + BotRegistry class + `start_bot`/`stop_bot`/`restart_bot`/`start_bot_dry_run` + StateBroadcaster + LogBroadcaster + watch_state_files + tail_logs + ws_logs + ws_state + init_db boot + lifespan handler + audit logger + cache LRUs + price-related caches + `_compute_summary` + error-reporting helpers.

**Waarom.** v22 refactor extracted routes. The remaining module is a mix of infrastructure (WS, registry, lifespan) and app-specific helpers that could live in more-focused modules. Not a correctness issue; a Phase-C pre-refactor would reduce the surface area of what moves to the signing-service.

**Remediation.** Optional Phase-A splits:
- `web/auth_primitives.py` — session cookie helpers, `_request_user`, `_request_actor`, `_ws_extract_user_id`.
- `web/registry.py` — `BotInfo`, `BotRegistry`, `start_bot`, `stop_bot`, `restart_bot`.
- `web/websockets.py` — broadcaster classes + `ws_logs`/`ws_state` handlers + `watch_state_files`/`tail_logs`.

Each move is ~200-400 lines. The circular-include-at-bottom pattern already works; same pattern here.

---

## Part 19 — Concurrency Patterns

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-067 | MEDIUM | `watch_state_files` serial I/O blocks worker | web/app.py:1851 | C |
| r1-068 | LOW | Bitget ccxt clients not thread-safe (documented) | exchanges/bitget.py | — |

#### r1-067 — Serial `watch_state_files` I/O (MEDIUM)

**Wat.** The loop iterates `bots` and calls `bot.read_state()` serially. `read_state` does a blocking `open()` + `fh.read()` inside an event-loop coroutine without `asyncio.to_thread`. Under 100+ bots on shared storage, one slow stat or read blocks the entire broadcast cycle.

**Remediation.** Wrap each `read_state()` + `stat()` in `asyncio.to_thread`, or batch into `asyncio.gather`. For Phase-C, moving to Postgres + a `state` table removes the FS I/O entirely.

**Items verified clean:**

- Advisory file-lock pattern around state-file mutation (`core/file_lock.py` + `paper/close_handler.py`).
- Rotation lock in `core/credentials.py` (`_rotation_lock`).
- SIGTERM handler in `main_paper.py` / `main_live.py` gracefully stops engine.
- Portal lifespan cancels background tasks with 2s timeout.
- Cross-thread DB connection cache with version counter for test-isolation.

---

## Part 20 — Migration Safety

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-069 | LOW | No Alembic; destructive drop pattern | core/database.py | C |
| r1-070 | MEDIUM | No per-migration unit test | tests/test_database.py | C |

#### r1-070 — No migration tests (MEDIUM)

**Wat.** `tests/test_database.py` tests happy-path init, cache behaviour, connection handling. No test seeds a v3 DB + runs `_migrate_schema` + asserts that v4 columns land without data loss outside the destructive path. No test for v5 (changelog additive), v6 (failed_login columns), v7 (dashboard_layouts).

**Remediation.** Phase C: as Alembic replaces bespoke migration, each migration lands with a `tests/migrations/test_v<N>_to_v<N+1>.py` that seeds pre-state, runs migration, asserts post-state. Current SQLite snapshots can be committed as `tests/fixtures/schema_v<N>.db` binary blobs.

---

## Part 21 — Paper-vs-Live Parity

**Findings.**

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1-014 | HIGH | LiveEngine real-order NotImplementedError | live/live_engine.py:281 | C |
| r1-071 | MEDIUM | LiveEngine inherits Paper wholesale; real path unexercised | live/live_engine.py | C |
| r1-072 | LOW | parity_compare.py exists but no nightly automation | scripts/parity_compare.py | G |

`LiveEngine(PaperEngine)` inheritance means 99% of logic is identical — DCA spacing, TP/SL checks, drawdown guard, indicator engine. The two overrides are `_place_market_order` (NotImplemented today) and the ticker-refresh cadence. Paper-equivalence is a correct design choice; the unimplemented path is the Phase-C gate, not a parity bug.

`scripts/parity_compare.py` compares deal outcomes between paper + live-dry. Manual invocation; no CI run; no alert on divergence > threshold.

**Remediation.**
- r1-014/r1-071: couple with r1-013 (signing service). Real orders only after that lands.
- r1-072: add a cron job or GitHub Action that runs `scripts/parity_compare.py` weekly against production data and posts the summary to Slack.

**Phase.** C / G.

---

## Part 22: Cross-references to security-model.md

| Finding | Maps to security-model.md section | Phase |
|---------|-----------------------------------|-------|
| r1-001 — API-key stub | Part 3.3 (auth stack) | B |
| r1-002 — changelog role | Part 3.3 | B |
| r1-005 — TOTP | Part 3.3 | B |
| r1-008 — blast radius | Part 2.1 + Part 3.1 | C |
| r1-009 — CredentialProvider | Part 4 Phase A | A |
| r1-011 — key rotation | Part 2.7 | D |
| r1-012 — BITGET_PASSPHRASE | Part 3.5 | C |
| r1-013 — signing-service | Part 3.1 + 3.2 | C |
| r1-016 — mTLS | Part 3.1 | C |
| r1-017 — Postgres migration | Part 3.2 + Part 4 Phase C | C |
| r1-023 — subprocess env | Part 2.1 defense-in-depth | B |
| r1-031 — audit log structure | Part 4 Phase A | A |
| r1-044 — per-user + per-exchange rate-limits | Part 3.3 rate-limiting architectuur | B+C |
| r1-055 — category: whole trade-signing in main-app | Part 3.1 + 3.6 | C |

**Phase-completion percentages (best-estimate):**

- **Phase A (Foundation)** — ~70% complete.
  - Done: v26-15h, v26-01, v26-02, v26-10, v26-11, v26-16 (per-user WS filter), v26-17, v26-19, exchange-permissions docs partial.
  - Open: `CredentialProvider` interface (r1-009), structured audit log (r1-031), Exchange-call permission matrix (partial — runbook lists onboarding steps but no `docs/exchange-permissions.md` artifact), dependency hash-pinning (r1-060), mypy bootstrap (r1-063), ML `_persist_results` user-scoping (r1-049), web/app.py split (r1-066 optional).
- **Phase B (Authentication hardening)** — ~30% complete.
  - Done: password min-length constant (v26-03), `_require_session` active check (v26-01), `/auth/logout` rate-limit (v26-04), login-security-hardening (exponential backoff + per-account rate-limit + anomaly logging), HIBP breach check on password-change.
  - Open: TOTP layer (r1-005), TOTP rotation endpoint, TOTP integration in `/auth/login`, per-user API keys (r1-003), per-user rate-limiter + X-Forwarded-For parsing (r1-004, r1-026, r1-044), cookie-posture regression test (v26-22 ACCEPTED).
- **Phase C (Service separation)** — ~0%. No signing-service scaffolding, no mTLS infra, no Postgres.
- **Phase D (User-facing security)** — 0%. No PWA, WebAuthn, onboarding wizard, TOTP enrollment UI.
- **Phase E (Defense layers)** — 0%. No per-trade threshold, no caps, no performance scaling, no emergency floor, no anomaly detectors, no dead-man-switch.
- **Phase F (Watchdog)** — 0%. No second VPS, no read-only balance polling, no kill-signal protocol.
- **Phase G (SaaS launch)** — 0%. Current single-host deploy is Reverto-Server (Mele Quieter 4C). No cloud migration, no Terms of Service, no status page, no external review.

---

## Part 23: Remediation Priority

### Must fix before the first non-admin user is seeded (Phase-3b gate)

These would cause a concrete cross-tenant leak or access-control bypass the moment a second user exists.

1. **r1-001 (HIGH)** — Fix API-key fallback to DB-lookup + active check. One-line fix with high blast-radius reduction.
2. **r1-002 (HIGH)** — Swap changelog admin gate from `user.id != 1` to `user.role != "admin"`.
3. **r1-023 (HIGH)** — Filter subprocess env to allowlist before spawning bots.
4. **r1-041 (HIGH)** — Key `_state_mtimes` on `(user_id, slug)`.
5. **r1-012 (HIGH)** — Move `BITGET_PASSPHRASE` into per-user `.enc` payload (couples with r1-023).
6. **r1-053 (MEDIUM)** — Add cross-tenant isolation test coverage.

### Must fix before Phase C (signing-service migration)

The signing-service splits out credential custody, cap enforcement, and order signing. Before that lands:

7. **r1-009 (MEDIUM)** — `CredentialProvider` interface abstraction. Phase-A explicit deliverable; Phase-C depends on it.
8. **r1-017 (HIGH)** — Postgres migration + Alembic (Phase C deliverable).
9. **r1-024 (HIGH)** — Multi-worker-safe BotRegistry.
10. **r1-025 (HIGH)** — Redis pubsub for broadcasters.
11. **r1-026 (MEDIUM)** — Shared-storage rate-limiter.
12. **r1-031 (MEDIUM)** — Structured audit log.
13. **r1-029 (MEDIUM)** — Central Bitget rate-budget (v27 B-01).

### Must fix before multi-tenant SaaS launch (Phase G)

14. **r1-011 (HIGH)** — Exchange-key rotation flow.
15. **r1-013/r1-055 (CRITICAL/HIGH)** — Signing-service split complete. This is the largest piece of work — not a single fix but a Phase-C goal.
16. **r1-016 (MEDIUM)** — mTLS + CA infrastructure.
17. **r1-033 (MEDIUM)** — Per-user Prometheus labels.
18. **r1-037 (MEDIUM)** — Health-gated deploy + rollback.

### Sweep-PR candidates (cluster smaller items into one PR)

Cluster for a `audit/r1-sweep` PR — each is < 30 lines, well-scoped, minimal risk:

- **r1-002** changelog role check swap
- **r1-035** (v27-06) API-key leak in OSError branch — switch to stderr-only
- **r1-051** drop DEFAULT_USER stub after r1-001
- **r1-052** resolve TODO(phase-3b) comments in broadcaster helpers
- **r1-054** API-key-deactivated-admin regression test
- **r1-058** startup env-var validation
- **r1-020** N+1 in `/api/db/deals` → single JOIN query
- **r1-070** seed a v6→v7 migration test as a starter

### Defer / Info / Accepted

- **r1-030** (state FS — infra migration) — accepted until Phase C.
- **r1-036** (TickerError URL leak) — INFO, low prob.
- **r1-061** (transitive deps non-blocking) — intentional.
- **r1-062** (ccxt cadence) — documented; not automated.
- **r1-068** (ccxt thread-safety) — documented inline; no false assurance.
- **v26-22** (SameSite test drift) — ACCEPTED per v26 report Part 11.

---

## Part 24: Recommendations beyond findings

**Process.**
- Adopt a **bi-weekly mini-audit** between full audit cycles (v26 → v27 was quarterly; the delta grows faster than quarterly cadence can catch). 60-minute scoped review of `git log --since` for security-relevant diffs.
- Keep the `audit/v<N>-backlog.md` pattern — the v27-backlog has proven useful for capturing observations without requiring a full audit pass.
- **Each Phase deliverable MUST include an update to the Independence-matrix in security-model.md Part 3.7** (same as Part 7 Appendix mandates). A Phase-E caps implementation that doesn't cross-reference the independence-matrix has failed its own quality gate.

**Tooling.**
- Pre-commit hooks for mypy (Phase A item). Once `pyproject.toml` exists and mypy runs clean on the scoped modules, make it a commit-gate.
- Add `pip-licenses` to CI so license compliance is trackable from day one (v26 Phase-G lists this as informal; formalise early).
- Consider a CI job that rolls `make test` + `make lint` + `pip-audit` on a weekly schedule (not just on PR) so drift in deps or transient CVE surfaces faster than an operator notices.

**Operational patterns.**
- `docs/runbook.md` currently explains startup + migration + wipe-deals + rotation. Missing: **incident-response playbook** (security-model.md Part 6.2a lists this as a research-spoor item for Phase G). Concrete runbooks for: suspected server compromise, watchdog alert firing, exchange-side leak (credential exfiltration), user reporting unauthorized trades. Even a skeleton before Phase G will save time under pressure.
- Add a `make healthcheck` target that runs a scripted sanity-check: `/healthz` returns 200, `/readyz` returns 200 within 3s, `REVERTO_API_KEY` is set + matches `logs/.api_key_ephemeral`, `/api/bots` returns with the expected bot count, and every `logs/<uid>/pids/*.pid` has an alive process. Useful for automated post-deploy verification once Phase G adds the rollout flow.

**Documentation.**
- `docs/security-model.md` is the reference document; it reads well but the 1776-line length is getting unwieldy. Consider splitting into:
  - `security-model.md` — the spec (Part 1-3, current).
  - `security-model-roadmap.md` — Part 4 (phases) + Part 6 (open questions).
  - `security-non-decisions.md` — Part 5 explicitly out-of-scope items.
- `docs/exchange-permissions.md` (named in Phase A deliverables) does not exist yet. Worth writing — the onboarding step asks users to set the right Bitget/Kraken permission subset, and a per-call permission matrix makes that concrete.
- Document the "only-one-admin-today" assumption explicitly in the architecture doc. Several code paths (r1-002 changelog gate; the v26-02 narrative assumes id=1) quietly rely on it.

---

## Part 25: Limitations of this audit

**Scope depth variance.**
- **Deep-read:** security-model.md, v26/v27 reports, phase-3 doc, architecture, runbook, `web/app.py`, `web/routes/*.py`, `core/user.py`, `core/user_store.py`, `core/deal_store.py`, `core/database.py`, `core/credentials.py`, `core/paths.py`, `core/dashboard_store.py`, `core/changelog_store.py`, auth flow in `web/routes/auth.py`.
- **Medium-read:** `paper/paper_engine.py` (first 100 lines + targeted greps), `live/live_engine.py` (first 120 + NotImplementedError context), `ml/nightly_pipeline.py` (targeted slices), `exchanges/bitget.py` (first 100 lines), `web/metrics.py`.
- **Targeted-grep only:** `strategies/*`, `backtest/*`, most of the paper engine internals, the full Bitget/Kraken/PublicExchange wrappers, most of the `core/*_guard.py` files, `notifications/telegram.py`. Each was sampled for user-scoping + error-handling patterns but not line-by-line.
- **Not opened:** `config/*`, `notebooks/`, `ml/features.py` + `ml/market_regime.py`, `backtest/backtest_engine.py`, `core/clock_monitor.py`. These are in-scope for Reverto's overall codebase review but not security-model.md's multi-tenant concerns.

**Hypothetical vs confirmed.**
- r1-023 (subprocess env leak) is **confirmed by code read** — `env = os.environ.copy()` is literal.
- r1-041 (`_state_mtimes` collision) is **confirmed by code read** — no user_id in the cache key.
- r1-001 API-key stub is **hypothetical exploit** in the sense that exploit feasibility was validated by code, not runtime (the v27 report included a curl reproduction; I did not re-run it on current HEAD, but the code is unchanged since that reproduction).
- r1-012 (BITGET_PASSPHRASE env) is **confirmed by code read** — single env-var read at main_live.py:258.
- Phase-completion percentages are **best-estimate**. Each phase has a loose deliverable count; "complete" means merged to main with its acceptance tests.

**Domains where external review would add value.**
- **Cryptographic review** of the Fernet key rotation flow — I trust the commit-order contract on a read-through but a real key-rotation under load would need a crypto-specialist validation before Phase C ships.
- **Pentest of the signing-service RPC interface** once it exists — Phase G's external security review item is named in security-model.md. Should be scoped to cover service-separation boundaries, mTLS impl, scope-whitelist enforcement.
- **ccxt wrapper behaviour** under adversarial Bitget / Kraken responses — would a malformed error body from the exchange corrupt the idempotency check? Needs fuzz testing against a mock exchange.
- **Postgres migration plan review** by someone with production SaaS experience — Alembic is not trivial; SQLite-era patterns that rely on SQLite's forgiving `WITHOUT ROWID` or triggerless INSERT OR REPLACE semantics won't survive naive translation.

**Explicitly out of scope of this audit.**
- **Frontend UX / Accessibility.** The SPA code in `web/static/app.js` was not read; domains 17-18 covered its size observation but not its patterns.
- **Performance benchmarking under tenant load.** No synthetic multi-tenant workload was run; scalability findings (Part 7) are structural observations, not observed failures.
- **Third-party compliance (GDPR / MiCA / DNB).** Security-model.md Part 6.2a acknowledges these as a separate policy document; this audit does not attempt to cover them.
- **Business continuity + incident-response playbooks.** Mentioned as Phase-G deliverables; not audited because they don't exist yet.
- **Cost / operational economics.** No findings on what running the SaaS would cost, what VPS / DB / Redis / observability spend to expect.

---

_Audit complete. Written against HEAD `4c2efc3`. Next revision: at start of Phase C (post-Phase-B auth-hardening merge), or earlier upon discovery of un-modelled threats._
