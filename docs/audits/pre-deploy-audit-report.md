# Pre-Deploy Audit — reverto.bot on Hetzner VPS

**Classification:** Internal
**Status:** Pre-deploy audit for VPS-3 migration
**Base audits:** `saas-readiness-v1-report.md` + `saas-readiness-v1.1-delta-report.md`
**HEAD reviewed:** `81a3975` (main post-`fix/vps-1-hotfix-csrf-migration` merge, 2026-04-24)
**Target deploy:** Hetzner VPS with Caddy + Let's Encrypt serving `https://reverto.bot`
**Auditor:** Claude Opus 4.7 under `pre-deploy-audit` prompt
**Time budget:** ~4-6 h targeted review (15 focus areas, delegated to five parallel Explore agents)

---

## Remediation status (post-VPS-1.5 + pre-deploy final polish)

All 11 SHOULD-FIX items closed in `fix/vps-1.5-polish` (2026-04-24). 3 of 6 MONITORING items closed in `fix/pre-deploy-final-polish` (2026-04-24). The deploy-readiness verdict moves from **⚠️ APPROVED WITH CONDITIONS** to **✅ APPROVED FOR DEPLOY**. Individual finding STATUS lines below are marked `RESOLVED in fix/<branch>`.

| Finding | Branch | Status |
|---|---|---|
| pd-001 (OSError scrub) | `fix/vps-1.5-polish` | RESOLVED |
| pd-003 (HIBP after verify) | `fix/vps-1.5-polish` | RESOLVED |
| pd-005 (/api/chart cost-budget) | `fix/vps-1.5-polish` | RESOLVED |
| pd-006 (passphrase max_length) | `fix/vps-1.5-polish` | RESOLVED |
| pd-011 (Permissions-Policy) | `fix/vps-1.5-polish` | RESOLVED |
| pd-025 (API_KEY required) | `fix/vps-1.5-polish` | RESOLVED |
| pd-026 (LOG_LEVEL in .env.example) | `fix/vps-1.5-polish` | RESOLVED |
| pd-029 (changelog f-string comment) | `fix/vps-1.5-polish` | RESOLVED |
| pd-042 (logout CSRF doc) | `fix/vps-1.5-polish` | RESOLVED |
| pd-043 (layout name validation) | `fix/vps-1.5-polish` | RESOLVED |
| pd-047 (ML pin) | `fix/vps-1.5-polish` | RESOLVED |
| pd-019 (secret-redaction tests) | `fix/pre-deploy-final-polish` | RESOLVED |
| pd-027 (env-example blind spot) | `fix/vps-1.5-polish` (auto via pd-026) | RESOLVED |
| pd-044 (orphan .tmp cleanup) | `fix/pre-deploy-final-polish` | RESOLVED |

Remaining MONITORING items: **pd-002, pd-007, pd-009**. All three are observational-only (CSRF graceful-migration log rate, slug-regex defensive doctrine, broad-except triage logging). Not gating; tracked for post-deploy runbook attention.

---

## Executive Summary

Reverto's tree post-VPS-0 + VPS-1 + hotfix is **deploy-ready with narrow-scope caveats**. The 30 audit items closed across Sprint 1, Sprint 2, Sprint 3/3b, and the three VPS sweeps (0, 1, 1-hotfix) all verify coherent in current main — none have regressed. The public-exposure-specific scan across the 15 focus areas surfaced **no CRITICAL or HIGH findings**: there is no path-traversal, no SQL injection, no shell-injection surface, no unbounded data leak, no missing CSRF on mutating endpoints, no missing rate-limiter on any state-changing route. The weakest links are **operational**, not architectural: `_validate_config()` treats `REVERTO_API_KEY` as *recommended* rather than *required* so a mis-configured deploy will silently fall back to ephemeral keys; the `SecurityHeadersMiddleware` omits `Permissions-Policy`; and three routes leak raw `OSError` strings into 500-response `detail` fields. None are exploitable in a meaningful sense on a trusted-host single-operator VPS, but all are cheap to close before first DNS cutover.

Severity counts: **0 BLOCKER / 11 SHOULD-FIX / 6 MONITORING / 37 ACCEPTED** (out of 54 findings).

## Deploy-Readiness Verdict

✅ **APPROVED FOR DEPLOY** — all 11 SHOULD-FIX items from the initial audit closed in `fix/vps-1.5-polish`. Zero BLOCKER findings remain; zero SHOULD-FIX items remain. The 6 MONITORING items are operational-observation-level and do not gate the cutover.

Earlier verdict (pre-polish) was **⚠️ APPROVED WITH CONDITIONS**; those conditions are now met:
1. ✅ pd-025 (API-key startup validation) — RESOLVED.
2. ✅ pd-011 (Permissions-Policy) — RESOLVED.
3. ✅ pd-001 (OS-error scrubbing on 500s) — RESOLVED.
4. ✅ All other SHOULD-FIX items — RESOLVED.
5. ☐ Monitor portal.log for 24 h after first TLS-cutover for unexpected error patterns (operator gate, not code).

---

## Severity Summary

Post-VPS-1.5 + pre-deploy final polish:

| Severity | Open | Resolved (polish sweeps) | Action |
|----------|:----:|:-----:|--------|
| **BLOCKER**   | **0** | 0 | Must fix before VPS-3 |
| **SHOULD-FIX** | **0** | 11 | All closed in `fix/vps-1.5-polish` |
| **MONITORING** | **3** | 3 | Observational-only; runbook tracking |
| **ACCEPTED**   | **37** | 0 | Verified correct or documented limitation |
| **Total**      | **40 open** | **14** | |

Pre-polish breakdown noted that the only security-primitive SHOULD-FIX items (as opposed to hygiene-level) were **pd-011** (missing `Permissions-Policy`), **pd-001** (`OSError` in response `detail`), and **pd-003** (change-password network-call ordering). All three are now resolved. The final-polish sweep additionally closed **pd-019** (secret-redaction regression tests), **pd-027** (auto-resolved via pd-026), and **pd-044** (startup `.tmp` cleanup hook).

---

## Sprint 1-3 + VPS-0-1 + Hotfix Verification

Every resolved r1-NNN / r1.1-NNN finding was re-grepped against current main. None regressed; every fix is present and coherent with the baseline audit's STATUS-marker claim.

| v1/v1.1 ID | Fix branch | Verification | Status |
|---|---|---|---|
| **r1-001** | `fix/r1-001-api-key-respects-active` | `_request_user` at [web/app.py:447-460](web/app.py#L447-L460) — API-key branch calls `user_store.get_user_by_id(1)` + active-check; fails 401 on missing/inactive; WARNING logged. | ✓ |
| **r1-002** | `fix/r1-002-changelog-admin-role-gate` | [web/routes/changelog.py:50](web/routes/changelog.py#L50) — `if user.role != "admin"` gate on every admin endpoint; no lingering `user.id != 1` hacks. | ✓ |
| **r1-004** | `feat/sprint-2-audit-sweep` | `_rate_limit_key_func` honours leftmost X-Forwarded-For entry + whitespace-strip + fallback; [tests/test_rate_limit_key.py](tests/test_rate_limit_key.py) covers the cases. | ✓ |
| **r1-006** | `fix/vps-0-sweep` | Cookie payload carries only `uid` (int); `u` field removed from both mint + verify. | ✓ |
| **r1-007** | Bundled with r1-032 | `_USERNAME_RE` excludes whitespace + control chars; `re.fullmatch` (not `re.match`) per follow-up bug-fix. | ✓ |
| **r1-012** | `fix/r1-012-bitget-passphrase-per-user` | `get_bitget_passphrase(user_id)` at [core/credentials.py:191-232](core/credentials.py#L191-L232) — store-preferred + env-fallback + deprecation warning. | ✓ |
| **r1-020** | `feat/sprint-2-audit-sweep` | Batch order-fetch helper at [core/deal_store.py:418-451](core/deal_store.py#L418-L451); N+1 removed from `/api/db/deals`. | ✓ |
| **r1-022** | `fix/vps-0-backup` | `scripts/backup.sh` + `scripts/restore.sh` present with 7/28/90-day retention + MANIFEST.txt + pre-restore snapshot. | ✓ |
| **r1-023** | `fix/r1-023-subprocess-env-whitelist` | `_BOT_ENV_ALLOWLIST` + `_bot_subprocess_env(user_id)` used by every Popen site; `os.environ.copy()` removed. | ✓ |
| **r1-031** | VPS-1 + hotfix | `_audit()` dual-writes pipe + JSONL; per-user split under `logs/<uid>/audit.jsonl` now fires (hotfix propagated `user_id=user.id` through every call-site). | ✓ |
| **r1-032** | `feat/sprint-2-audit-sweep` | `validate_username` at [core/user_store.py:55-71](core/user_store.py#L55-L71); audit-log pipe delimiter rejected. | ✓ |
| **r1-033** | VPS-1 | Every bot-scoped Prom counter/gauge/histogram carries `user_id` label; `unknown` bucket for legacy callers. | ✓ |
| **r1-034** | VPS-1 + hotfix | `RequestIdMiddleware` mints+echoes `X-Request-Id`; filter attached at boot via [core/logging_setup.py](core/logging_setup.py); `%(request_id)s` column in [main_web.py](main_web.py) format. | ✓ |
| **r1-035** | `fix/vps-0-sweep` | API-key hint (SHA256[:8]) logged; full key never appears in audit.log. | ✓ |
| **r1-037** | `fix/vps-0-deploy-rollback` | `web/static/maintenance.html` present + runbook Caddy wiring documented. | ✓ |
| **r1-038** | `fix/vps-0-deploy-rollback` | `scripts/rollback.sh` present; schema-migration guard via `git log -- core/database.py`. | ✓ |
| **r1-041** | `fix/r1-041-state-mtimes-per-user` | `_state_mtimes` keyed on `(user_id, slug)` tuple. | ✓ |
| **r1-042** | `feat/sprint-2-audit-sweep` | `bot_user_id` stamped on every state read; no cross-user WS leak. | ✓ |
| **r1-043** | `fix/vps-0-sweep` | `_logout_rate_limit_key` returns `logout:uid:<id>` for authenticated callers; IP fallback only for unauth. | ✓ |
| **r1-044** | VPS-1 | `_rate_limit_key_func` returns `user:<id>` when session-cookie is valid; IP fallback otherwise. | ✓ |
| **r1-045** | VPS-1 | `CostBudget(budget=10000, refill=100/s)` fronts `/api/candles`; cost = candle_limit. | ✓ |
| **r1-047** | post-v26-17 | No bare `{"error":...}` responses remain; every route uses `HTTPException(detail=...)`. | ✓ |
| **r1-048** | `fix/vps-0-sweep` | Docstring inline + `openapi_url=None` set (auto-docs disabled in production). | ✓ |
| **r1-049** | `fix/vps-0-sweep` | `paths.user_ml_results_path` — ML results land in `ml/<uid>/` not the shared dir. | ✓ |
| **r1-051** | `feat/sprint-2-audit-sweep` | `DEFAULT_USER` constant deleted; zero production hits. | ✓ |
| **r1-052** | `feat/sprint-2-audit-sweep` | Zero `TODO.*phase` comments remain in web/ + core/. | ✓ |
| **r1-053** | `feat/sprint-2-audit-sweep` | [tests/test_cross_tenant_isolation.py](tests/test_cross_tenant_isolation.py) — 3 E2E scenarios (deal listing, annotation delete, annotation list). `/api/bots` listing deferred per inline note. | ✓ (partial-documented) |
| **r1-054** | `feat/sprint-2-audit-sweep` | `TestApiKeyRespectsActive::test_api_key_auth_path_isolated_from_cookie` present. | ✓ |
| **r1-056** | `feat/sprint-2-audit-sweep` | No `except Exception: pass` patterns remain in web/ + core/ + paper/ + live/. | ✓ |
| **r1-057** | `fix/vps-0-sweep` | [core/circuit_breaker.py](core/circuit_breaker.py) wired into `PublicExchange`. | ✓ |
| **r1-058** | `feat/sprint-2-audit-sweep` | `_validate_config()` + `_validate_config_completeness()` called in `lifespan()` startup; raises on missing SECRET_KEY. (Partial for API_KEY — see **pd-025**.) | ◐ |
| **r1-059** | `fix/vps-0-sweep` | `_validate_config_completeness` scans `.env.example` + warns on missing. | ✓ |
| **r1-068** | VPS-1 | ccxt thread-safety docstring on `BitgetExchange` + `PublicExchange`; `_price_lock: asyncio.Lock` serialises. | ✓ |
| **r1-073** | VPS-1 + hotfix | `CSRFMiddleware` double-submit cookie + graceful-migration path + shared helper between login mint + migration mint. | ✓ |
| **r1-074** | `fix/vps-0-sweep` | `integrity=<sha384>` + `crossorigin="anonymous"` on every unpkg `<script>` + `<link>` in `index.html`. | ✓ |
| **r1-075** | `feat/sprint-2-audit-sweep` | HSTS emitted only on `request.url.scheme == "https"`; test covers HTTPS-on + HTTP-absent. | ✓ |
| **r1-076** | VPS-1 | CSP `connect-src` has `'self' https://unpkg.com` only; ws:/wss: wildcards removed; test covers. | ✓ |
| **r1-010** | — | Plaintext creds in process heap — documented limitation, Phase-C signing-service. | ACCEPTED |
| **r1-048** | — | OpenAPI exposure — `openapi_url=None` in prod. | ACCEPTED |
| **r1.1-002** | `fix/sprint-3b-v1.1-sweep` | `_CHART_PAIRS_ALLOWLIST` = `{"BTC/USD", "BTC/USDT"}`. | ✓ |
| **r1.1-003** | `fix/r1.1-003-workspace-scroll-merge` | Workspace factory merges `priorHistory` like main-chart. | ✓ |
| **r1.1-004** | `fix/sprint-3b-v1.1-sweep` | Per-chart TZ validation guard. | ✓ |
| **r1.1-005** | `fix/sprint-3b-v1.1-sweep` | Plugin contract doc + `initChart` orphan guard. | ✓ |
| **r1.1-006** | `fix/sprint-3b-v1.1-sweep` | `initChart` orphan guard. | ✓ |

**Partial-resolution notes.** `r1-058` is marked ◐ because the fix landed the validation hook correctly but elevates `REVERTO_API_KEY` to WARNING-level only (not RAISE). In practice on a correctly-configured VPS the var is set and the check passes; the finding is captured as **pd-025** for explicit flag-raising pre-deploy. `r1-053` cross-tenant test passes for the 3 implemented scenarios; the `/api/bots` listing deferral is well-documented inline and acceptable in single-operator context (captured as MONITORING in the operational checklist).

---

## Part 1: Findings by Focus Area

### Area 1 — Public endpoints

**Scope.** Endpoints reachable **without** a session cookie. The public set is: `/` (redirects to login when unauth), `/favicon.ico`, `/healthz`, `/readyz`, `/metrics`, `/auth/status`, `/auth/login`, `/auth/logout`, `/static/*`, and WebSocket `/ws/*` (which are auth-gated inside the handshake). Every endpoint was traced through `AuthMiddleware._PUBLIC_PATHS` + the route handler for (a) input validation, (b) rate-limit setting, (c) error-response shape.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-001 | SHOULD-FIX | `OSError` details leaked into 500 response `detail` | web/routes/bots.py, web/routes/drawdown.py |
| pd-002 | MONITORING | CSRF graceful-migration grants one-shot bypass even on /auth/logout | web/app.py:1490-1501 |

#### pd-001 — `OSError` details leaked into 500 response `detail` (SHOULD-FIX)

**What.** Three routes surface raw `str(e)` from I/O exceptions in the HTTPException `detail` field:
- `POST /api/bots/{slug}/deal/start` — `HTTPException(status_code=500, detail=f"Failed to write trigger: {e}")` at [web/routes/bots.py:258-259](web/routes/bots.py#L258-L259).
- `POST /api/bots/{slug}/drawdown/reset` — `HTTPException(status_code=500, detail=f"State unreadable: {str(e)[:100]}")` at [web/routes/drawdown.py:52-54](web/routes/drawdown.py#L52-L54).
- `GET /api/bots/{slug}/config` — `HTTPException(status_code=500, detail=f"YAML parse error: {e}")` at [web/routes/bots.py:464-465](web/routes/bots.py#L464-L465).

**Why.** On a public VPS an attacker who coaxes one of these paths into failing (e.g. a malformed state.json, a permissions issue after a manual edit) receives the raw `PermissionError: [Errno 13] Permission denied: '/home/bot/reverto/logs/3/rsi_btc.trigger'` in the response body. That leaks the exact on-disk layout.

**Where.** 3 call-sites listed above.

**Remediation.** Replace the f-string detail with a generic message; log the full exception via `logger.exception(...)`:

```python
try:
    trigger.write_text("", encoding="utf-8")
except OSError:
    logger.exception("manual-deal trigger write for %s/%s", user.id, slug)
    raise HTTPException(status_code=500, detail="Failed to write manual trigger")
```

~10 minute fix across the 3 sites.

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — three sites now `logger.exception(...)` + generic `HTTPException(detail=...)`.

#### pd-002 — CSRF graceful-migration grants one-shot bypass on /auth/logout (MONITORING)

**What.** The hotfix graceful-migration path ([web/app.py:1490-1501](web/app.py#L1490-L1501)) lets any authenticated request that lacks the CSRF cookie through once and mints a fresh cookie on the response. That applies to `/auth/logout` as well, since logout is not in `_CSRF_EXEMPT_PATHS`.

**Why.** Logout is idempotent + already triggers an epoch-bump, so a one-shot bypass on it has no exploitation value — the attacker can't actually *log someone in* via logout. The concern is operational: repeated "CSRF graceful-migration" log lines on the same source-IP flag a misconfigured client that should have re-logged in.

**Where.** [web/app.py:1490-1501](web/app.py#L1490-L1501), combined with `/auth/logout` being in `_PUBLIC_PATHS` but NOT in `_CSRF_EXEMPT_PATHS`.

**Remediation.** No code change required. Add one line to the runbook: *"If you see > 50 CSRF graceful-migration log lines per hour from the same IP, that client is stuck in legacy mode — clear cookies or re-login."* The graceful-migration window naturally closes within 24 h (session TTL).

**Category.** MONITORING.

---

### Area 2 — Auth flow end-to-end

**Scope.** Login / logout / change-password / API-key path. Specifically: session-cookie + CSRF-cookie minting, flags, epoch bump on logout, HIBP check ordering, API-key active-check coherence across both `_request_user` and `_require_session`.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-003 | SHOULD-FIX | HIBP network call fires before current-password verification in change-password | web/routes/auth.py:338-368 |
| pd-004 | ACCEPTED | Session TTL fixed at 24 h with no refresh/sliding window | web/app.py:152, 320 |

#### pd-003 — HIBP network call fires before current-password verification (SHOULD-FIX)

**What.** In `POST /api/auth/change-password`, the HIBP (Have-I-Been-Pwned) k-anonymity lookup at [web/routes/auth.py:338-355](web/routes/auth.py#L338-L355) runs **before** the current-password check at [web/routes/auth.py:378](web/routes/auth.py#L378). An unauthenticated-but-session-cookied attacker can spray change-password requests with 100-char payloads and cause the portal to hit `haveibeenpwned.com` on every attempt, even though every one of those attempts will subsequently fail the current-password verify.

**Why.** On a metered-egress VPS this is bandwidth + quota waste. The HIBP SDK fails-open (doesn't block the change), but the network call still fires. Reordering removes the cost.

**Where.** [web/routes/auth.py:338-380](web/routes/auth.py#L338-L380).

**Remediation.** Two-line reorder: move the HIBP check block below the `verify_password` gate so it only runs when the current password is confirmed.

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — HIBP call now runs only after the current-password verify succeeds; regression test asserts the HIBP mock does not fire on a wrong current-password.

#### pd-004 — Session TTL fixed at 24 h with no refresh (ACCEPTED)

**What.** `_SESSION_TTL = 86400` at [web/app.py:152](web/app.py#L152). No sliding-window renewal; after 24 h from login the user is forcibly bounced to the login screen.

**Why.** Single-operator context + typical 8-hour trading sessions means the 24 h ceiling is comfortable. A future multi-user SaaS seed with all-day traders would need a refresh mechanism.

**Where.** [web/app.py:152](web/app.py#L152), `_verify_session_cookie` at :320.

**Remediation.** None for VPS-3. Re-evaluate post-multi-user seed.

**Category.** ACCEPTED.

---

### Area 3 — Input validation on public + authenticated endpoints

**Scope.** Pair names, slugs, timeframes, limit/pagination params, Pydantic body models, path-traversal surface.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-005 | SHOULD-FIX | `/api/chart` 500-candle ceiling lacks cost-budget on public IP-keyed path | web/routes/chart.py:207-218 |
| pd-006 | SHOULD-FIX | `ExchangeKeysBody.passphrase` max_length=512 is overly permissive for Bitget | web/routes/exchanges.py:36-38 |
| pd-007 | MONITORING | Slug regex validated inconsistently — registry.get is the real gate, but belt-and-braces drift-risk | web/routes/bots.py:187-262 |
| pd-008 | ACCEPTED | `_CHART_PAIRS_ALLOWLIST` = {BTC/USD, BTC/USDT} — intentionally conservative | web/app.py:2008 |

#### pd-005 — `/api/chart` limit bounded but no cost-budget (SHOULD-FIX)

**What.** `/api/chart/{pair}/{timeframe}` ([web/routes/chart.py:207-218](web/routes/chart.py#L207-L218)) bounds `limit` to [10, 500] and is rate-limited at 40/min. The endpoint is public (pre-auth); 500 candles × 40/min = 20 000 candles/min/IP. The `/api/candles` endpoint has an additional `CostBudget` (r1-045) precisely to prevent this kind of amortised load; `/api/chart` doesn't.

**Why.** On a VPS behind a shared proxy, one hostile IP can sustain 333 candles/sec of upstream Bitget calls (via the cache) before the slowapi limiter kicks in. Not catastrophic, but inconsistent with the `/api/candles` discipline.

**Where.** [web/routes/chart.py:207-218](web/routes/chart.py#L207-L218).

**Remediation.** Either (a) tighten to 30/min + limit 200, or (b) attach a public-IP-keyed `CostBudget` like `/api/candles` uses.

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — option (b) implemented. `/api/chart` shares the existing `_candles_cost_budget(10000, 100/s)` keyed via `_rate_limit_key_func` (per-user when authenticated, IP fallback otherwise). Cache-hit path untouched so legit dashboard refresh load stays cheap.

#### pd-006 — Passphrase max_length=512 overly permissive (SHOULD-FIX)

**What.** `ExchangeKeysBody.passphrase = Field(min_length=1, max_length=512)` at [web/routes/exchanges.py:36-38](web/routes/exchanges.py#L36-L38). Bitget passphrases are user-chosen during API-key creation and are conventionally 10-20 chars.

**Why.** Low direct risk (Pydantic validates before the handler), but an operator's typo-paste of a 500-char string silently succeeds, which is confusing UX.

**Remediation.** Reduce to 64 (safe headroom). One-line change plus an inline comment.

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — `max_length=64` plus Pydantic-422 regression test.

#### pd-007 — Slug regex inconsistent across bot-lifecycle routes (MONITORING)

**What.** `/api/bots/{slug}/start-dry-run` explicitly validates slug against `_BOT_SLUG_RE` at [web/routes/bots.py:215](web/routes/bots.py#L215); `/api/bots/{slug}/start`, `/stop`, `/restart`, `/deal/start`, `/config` do NOT. They rely on `registry.get(user_id, slug)` returning `None` for malformed slugs.

**Why.** `registry.get` is the real safety gate (it checks the filesystem + composite key). The regex check is belt-and-braces. The inconsistency is a drift-risk: a future refactor might assume all endpoints validate and remove the registry guard on one of them.

**Remediation.** Either uniformly add `if not _BOT_SLUG_RE.match(slug): raise 400` to every endpoint, or add a block-comment at the top of `bots.py` explaining the registry-guard pattern.

**Category.** MONITORING.

**STATUS (2026-04-29 / `fix/validation-hygiene-cluster`): RESOLVED via design-intent documentation.** The asymmetry between `_SLUG_RE` (post-lowercase narrow charset) and `_BOT_SLUG_RE` (URL-path superset) is intentional, not drift — `slugify()` lowercases input first, so the narrow regex matches the post-lowercase result. Widening `_SLUG_RE` to match `_BOT_SLUG_RE` would change the on-disk filesystem layout for every mixed-case input. A new "pd-007 design note" comment block above the two regex declarations in `web/app.py` explains the lifecycle (sanitisation → URL validation) so a future "consistency" PR cannot quietly harmonise them. Pinned by `tests/test_validation_hygiene.py::TestSlugRegexDesignIntent` (4 tests covering the narrow-charset post-lowercase contract, the URL-path superset, the `slugify()` two-stage pipeline, and the design-note presence).

#### pd-008 — Chart pair allowlist conservative (ACCEPTED)

**What.** Hardcoded `{"BTC/USD", "BTC/USDT"}` ([web/app.py:2008](web/app.py#L2008)). Adding a new pair requires a code change + redeploy.

**Why.** Intentional (r1.1-002 resolution). Prevents cache pollution + bounds upstream exchange call surface.

**Category.** ACCEPTED.

---

### Area 4 — Error handling

**Scope.** 500 leakage, 4xx shape consistency, secret redaction in exception logs, FastAPI default handlers.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-009 | MONITORING | Broad `except Exception` in chart routes loses type/message granularity for triage | web/routes/chart.py |
| pd-010 | ACCEPTED | No stack-traces leak; all HTTPException shapes consistent with `{"detail": ...}` | cross-tree |

#### pd-009 — Broad `except Exception` in chart routes (MONITORING)

**What.** `/api/price` silently falls back to bot-state on any exception; `/api/ticker` and `/api/chart` catch `Exception` + surface `str(e)[:200]`. The truncation is good, but the broad catch makes log-triage hard: an upstream Bitget 502 vs. a parsing TypeError look identical in logs.

**Where.** [web/routes/chart.py](web/routes/chart.py) (lines 91, 155, 238, 310, 347).

**Remediation.** Before raising, log the exception type + full message at DEBUG level:
```python
except Exception as e:
    logger.debug("ticker %s: %s: %s", pair, type(e).__name__, e)
    raise HTTPException(status_code=502, detail="ticker fetch failed")
```
Client response stays safe; operator visibility gets richer.

**Category.** MONITORING.

**STATUS (2026-04-29 / `fix/validation-hygiene-cluster`): RESOLVED.** The three upstream-fetch try-blocks in `web/routes/chart.py` (`/api/ticker`, `/api/chart`, `/api/candles`) now classify by ccxt exception class: `ccxt.NetworkError` / `asyncio.TimeoutError` → 503 + `logger.warning` (transient, retry-able); `ccxt.BadSymbol` → 400 + `logger.warning` (caller error, not exchange-side); `ccxt.ExchangeError` → 502 + `logger.exception` (permanent exchange-side, scrubbed wire detail per audit r3-001); other `Exception` → 500 + `logger.exception` (genuinely unexpected, full traceback for postmortem). The `/api/price` `except Exception` at line 98 stays as-is because its fallback-to-bot-state path is the deliberate UX contract — the wire detail there is a 503 only after the fallback fails. Pinned by `tests/test_validation_hygiene.py::TestChartRouteExceptionSplit` (5 tests).

#### pd-010 — No stack-trace leaks; shape consistent (ACCEPTED)

**What.** Every route raises `HTTPException(status_code=..., detail="...")`. FastAPI's default 500 handler returns `{"detail": "Internal Server Error"}` without a traceback in the body. No `repr(e)` or `traceback` patterns found in response bodies.

**Category.** ACCEPTED.

---

### Area 5 — Security headers

**Scope.** `SecurityHeadersMiddleware` at [web/app.py:1542-1594](web/app.py#L1542-L1594). Each header evaluated for justification + completeness.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-011 | SHOULD-FIX | `Permissions-Policy` header absent | web/app.py:1542-1594 |
| pd-012 | ACCEPTED | CSP `style-src 'unsafe-inline'` remains, documented for r1-076 follow-up | web/app.py:1556 |
| pd-013 | ACCEPTED | HSTS missing `preload` directive — intentional for single-operator | web/app.py:1591-1592 |
| pd-014 | ACCEPTED | `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'` — both set | web/app.py:1574-1578 |

#### pd-011 — `Permissions-Policy` header missing (SHOULD-FIX)

**What.** `SecurityHeadersMiddleware.dispatch()` emits CSP, HSTS (on HTTPS), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`. It does **not** emit `Permissions-Policy`.

**Why.** A public portal should deny sensor APIs (camera, microphone, geolocation, payment) explicitly. Absence means the browser default (permissive) applies. Low direct risk for a trading portal (no legit use for those APIs), but the header is industry-standard hardening.

**Remediation.** Two-line addition:
```python
response.headers["Permissions-Policy"] = (
    "camera=(), microphone=(), geolocation=(), "
    "payment=(), usb=(), accelerometer=(), gyroscope=()"
)
```
Plus a regression test line in `tests/test_security_headers.py`.

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — `Permissions-Policy` header emits on every response with `camera=()` / `microphone=()` / `geolocation=()` / `payment=()` / `usb=()` / `bluetooth=()` / motion-sensors denied; regression test asserts presence + key directives.

#### pd-012, pd-013, pd-014 — CSP/HSTS/X-Frame details (ACCEPTED)

- `style-src 'unsafe-inline'` remains pending a full refactor of inline style attributes to CSS classes (r1-076 follow-up, documented at [web/app.py:1549-1555](web/app.py#L1549-L1555)).
- HSTS omits `preload` — appropriate for a single-operator VPS not submitting to the HSTS preload list. `max-age=31536000; includeSubDomains` is correct.
- Clickjacking is defended at two layers: `X-Frame-Options: DENY` (legacy) + CSP `frame-ancestors 'none'` (modern).

**Category.** ACCEPTED (all three).

---

### Area 6 — Static-asset serving

**Scope.** `/static/*` mount, `maintenance.html`, directory listing, any non-`/static` static surfaces.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-015 | ACCEPTED | Starlette `StaticFiles` path-traversal protection via `os.path.commonpath` is sound | web/app.py:1894 |
| pd-016 | ACCEPTED | `maintenance.html` self-contained + harmless if served while portal is up | web/static/maintenance.html |
| pd-017 | ACCEPTED | Directory listing disabled (Starlette default) | — |
| pd-018 | ACCEPTED | `index.html` + `/favicon.ico` served as explicit routes, auth-gated appropriately | web/app.py:1905, 1917 |

**No SHOULD-FIX or BLOCKER findings.** All four ACCEPTED.

---

### Area 7 — Logging practices

**Scope.** Secret redaction in log calls, PII, log rotation, audit event coverage, file permissions, REVERTO_LOG_LEVEL safety.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-019 | MONITORING | API-key hint format logged but no regression test asserts full key never leaks | web/app.py:122 |
| pd-020 | ACCEPTED | No emails / session cookies / passwords in any logger call | cross-tree |
| pd-021 | ACCEPTED | Log rotation 5 MB × 3 backups ≈ 20 MB disk ceiling per log — safe for VPS | main_web.py:42-52 |
| pd-022 | ACCEPTED | Audit event coverage comprehensive (login, bot-lifecycle, exchange keys, changelog, emergency-stop, drawdown-reset) | web/app.py:533-593 |
| pd-023 | ACCEPTED | Log files mode 0644 — single-operator VPS, sole tenant | main_web.py:42 |
| pd-024 | ACCEPTED | `REVERTO_LOG_LEVEL=DEBUG` doesn't leak payloads (no payloads logged at any level) | core/logging_setup.py |

#### pd-019 — API-key-hint regression test gap (MONITORING)

**What.** `web/app.py:122` emits `apikey:<sha256-hint>` on ephemeral-key generation; the full key never appears in audit.log. But no test asserts "audit logs never contain `_API_KEY`". A future refactor could accidentally log the full key.

**Remediation.** Add a test in `tests/test_audit_log.py` that triggers an audit event and greps the audit file for the full API-key string. 5-minute addition.

**Category.** MONITORING.

**STATUS.** RESOLVED in `fix/pre-deploy-final-polish` — new [tests/test_secret_redaction.py](tests/test_secret_redaction.py) adds 4 semantic regression tests covering API-key hint format, session-cookie redaction on `_verify_session_cookie` failure, and Bitget passphrase absence from the r1-012 deprecation-warning path. Each test drives the real code path with a recognisable sentinel secret and asserts the sentinel doesn't appear in caplog / audit.log / audit.jsonl.

#### pd-020 through pd-024 (ACCEPTED)

All logging practices verify clean. Single-operator file-permission posture (0644) is correct for a VPS with only the `bot` user as tenant. On any future multi-user host, `mode=0o600` would apply.

---

### Area 8 — Configuration handling

**Scope.** `.env.example` completeness, startup validation strictness, secrets-in-git, default values for security-sensitive vars.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-025 | SHOULD-FIX | `_validate_config()` treats `REVERTO_API_KEY` as recommended (WARN), not required (RAISE) | web/app.py:1710-1760 |
| pd-026 | SHOULD-FIX | `.env.example` omits `REVERTO_LOG_LEVEL` despite it being allowlisted | .env.example vs. web/app.py:1083 |
| pd-027 | MONITORING | `_validate_config_completeness` has a blind spot until pd-026 is fixed | web/app.py:1763-1804 |
| pd-028 | ACCEPTED | Ephemeral-key fallback is fail-soft by design for dev-friendliness | web/app.py:99-130 |

#### pd-025 — `REVERTO_API_KEY` not elevated to required in startup validation (SHOULD-FIX)

**What.** `_validate_config()` has two buckets: `required` (raises `RuntimeError` if missing) and `recommended` (logs `WARNING` if missing). `REVERTO_SECRET_KEY` is required; `REVERTO_API_KEY` is only recommended. If an operator's `.env` omits the API key, the portal falls back to an ephemeral key written to `logs/.api_key_ephemeral` with `atexit` cleanup — so the key changes on every restart, silently invalidating every CI/script/integration that authenticates with the prior value.

**Why.** An operator restarting the portal to pick up a config change will break their own backup cron, monitoring probes, and any script that holds a cached key, *without any visible signal* unless they're tailing the WARNING log at startup. On a systemd-deployed VPS, startup logs are easy to miss.

**Where.** [web/app.py:1710-1760](web/app.py#L1710-L1760), specifically the `required` vs. `recommended` split at lines 1732-1739.

**Remediation.** Move `REVERTO_API_KEY` from the `recommended` dict to the `required` dict. Startup will RAISE if missing, forcing the operator to set it explicitly. Dev/test paths that rely on the ephemeral fallback already set it via pytest `os.environ.setdefault(...)` so they aren't affected.

(This is the operational-impact BLOCKER candidate in agent output; we classify as SHOULD-FIX because the deploy runbook covers it and the WARNING is emitted, but it is the single most important SHOULD-FIX to land before DNS cutover.)

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — `REVERTO_API_KEY` moved to the `required` dict; missing-var now raises `RuntimeError` at startup with a clear message. Regression test asserts the RuntimeError fires.

#### pd-026 — `.env.example` omits `REVERTO_LOG_LEVEL` (SHOULD-FIX)

**What.** `REVERTO_LOG_LEVEL` is allowlisted for subprocesses at [web/app.py:1083](web/app.py#L1083) and honoured by `core/logging_setup.py`, but does not appear in `.env.example`. An operator copy-pasting the template has no hint that the knob exists.

**Remediation.** Add a block to `.env.example`:
```
# Optional: override log verbosity temporarily. Valid: DEBUG, INFO, WARNING, ERROR.
# Default (unset): INFO. Leave unset in production; flip to DEBUG for incident-debug.
# REVERTO_LOG_LEVEL=INFO
```

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — `REVERTO_LOG_LEVEL=` entry added to `.env.example`.

#### pd-027 — `_validate_config_completeness` blind spot (MONITORING)

**What.** The completeness check reads `.env.example` line-by-line and warns on any var listed there but not set. Since `REVERTO_LOG_LEVEL` isn't listed (pd-026), the check can't warn on it. Resolves automatically once pd-026 lands.

**Category.** MONITORING.

**STATUS.** RESOLVED — auto-resolved by `fix/vps-1.5-polish` (pd-026 added `REVERTO_LOG_LEVEL=` to `.env.example`). Confirmed at HEAD `a02df35` that `_validate_config_completeness` now sees the entry and would warn if the runtime env unsets it. No code change required; tracked here only for audit completeness.

#### pd-028 — Ephemeral-key fail-soft (ACCEPTED)

**What.** Module-import fallback generates an ephemeral key + writes to `logs/.api_key_ephemeral` with atexit cleanup. Dev-friendly; survives a single session.

**Category.** ACCEPTED.

---

### Area 9 — Database-query patterns

**Scope.** User-scoping, parameterized queries, cross-tenant test coverage, transaction boundaries.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-029 | SHOULD-FIX | `changelog_store.update_entry` f-string SQL (safe but unconventional) lacks inline safety comment | core/changelog_store.py:156-183 |
| pd-030 | ACCEPTED | All user-owned SELECT/UPDATE/DELETE include `WHERE user_id = ?` | cross-tree |
| pd-031 | ACCEPTED | No user data interpolated into SQL templates; all values parameterized via `?` | cross-tree |
| pd-032 | ACCEPTED | Cross-tenant test suite covers deal-list, annotation-delete, annotation-list | tests/test_cross_tenant_isolation.py |

#### pd-029 — `update_entry` f-string SQL lacks safety comment (SHOULD-FIX)

**What.** [core/changelog_store.py:156-183](core/changelog_store.py#L156-L183) builds an UPDATE statement via `f"UPDATE changelog_entries SET {', '.join(fields)} WHERE id = ?"` where `fields` is a list of hardcoded column assignments (e.g. `"title = ?"`). The pattern is safe — no user-controlled string lands in the SQL template — but a developer unfamiliar with the codebase might assume this is template injection.

**Remediation.** Three-line block-comment explaining the invariant: `fields` is hardcoded column-assignment strings; user data only lands in `values` via `?`.

**Category.** SHOULD-FIX (documentation hygiene).

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — inline comment now documents the hardcoded-fields invariant and warns against extending the builder with user-supplied field names.

#### pd-030, pd-031, pd-032 (ACCEPTED)

Deep-read of `core/deal_store.py`, `core/dashboard_store.py`, `core/user_store.py`, `core/credentials.py`, `core/changelog_store.py` confirms:
- Every user-owned row is gated by `WHERE user_id = ?` in both read + mutate paths.
- No f-string interpolation of user-controlled values into SQL (the one f-string in `changelog_store.py` joins hardcoded column names, not user input).
- `tests/test_cross_tenant_isolation.py` covers the three highest-value paths. `/api/bots` listing is deferred but documented.

---

### Area 10 — Credentials store integrity

**Scope.** Fernet encrypt/decrypt, key rotation, file/dir permissions, backup inclusion, passphrase per-user migration, key-plaintext-in-heap.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-033 | ACCEPTED | `ensure_secret_file_mode` enforces 0600 on all Fernet key writes | core/credentials.py (multiple) |
| pd-034 | ACCEPTED | Bitget passphrase per-user store + legacy env-fallback with deprecation WARNING | core/credentials.py:191-232 |
| pd-035 | ACCEPTED | Backup includes `credentials/` + `keys/` trees in encrypted form; pre-restore snapshot preserved | scripts/backup.sh, restore.sh |
| pd-036 | ACCEPTED | Key rotation atomic under fcntl advisory lock; crash-safe with recoverable failure modes | core/credentials.py:335-425 |
| pd-037 | ACCEPTED | Plaintext creds in engine heap — documented Phase-C limitation | core/credentials.py:36-48 (r1-010) |

**No SHOULD-FIX or BLOCKER findings.** Credential store is the most-scrutinised area in prior audits; the VPS-3 deploy inherits a mature implementation.

---

### Area 11 — Rate-limiting coverage

**Scope.** Per-endpoint coverage table; per-user keying; cost-budget; auth-specific limits.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-038 | ACCEPTED | 100% of POST/PUT/PATCH/DELETE endpoints carry a `@limiter.limit(...)` decorator | all web/routes/*.py |
| pd-039 | ACCEPTED | Per-user keying via `_rate_limit_key_func` (r1-044) | web/app.py:1661-1700 |
| pd-040 | ACCEPTED | Cost-budget on `/api/candles` (r1-045) | web/routes/chart.py:62-64 |
| pd-041 | ACCEPTED | Auth endpoints: login 5/min, change-password 10/min, logout 10/min per-user | web/routes/auth.py |

**Rate-limit coverage table** — every mutating endpoint + its limiter setting, produced by deep-read of `web/routes/*.py`:

| Endpoint | Method | Limit | Keying |
|----------|:------:|:-----:|:------:|
| /auth/login | POST | 5/min | shared (IP-fallback) |
| /auth/logout | POST | 10/min | **per-user** (r1-043) |
| /api/auth/change-password | POST | 10/min | shared |
| /api/bots | POST | 20/min | shared |
| /api/bots/{slug}/start | POST | 20/min | shared |
| /api/bots/{slug}/start-dry-run | POST | 20/min | shared |
| /api/bots/{slug}/stop | POST | 20/min | shared |
| /api/bots/{slug}/restart | POST | 20/min | shared |
| /api/bots/{slug}/deal/start | POST | 5/min | shared |
| /api/bots/{slug}/config | PUT | 10/min | shared |
| /api/bots/{slug} | DELETE | 10/min | shared |
| /api/bots/{slug}/duplicate | POST | 10/min | shared |
| /api/bots/import | POST | 10/min | shared |
| /api/bots/{slug}/deals/{deal_id} | PATCH | 10/min | shared |
| /api/bots/{slug}/deals/{deal_id} | DELETE | 10/min | shared |
| /api/db/annotations | POST | 30/min | shared |
| /api/db/annotations/{id} | DELETE | 30/min | shared |
| /api/db/annotations/all | DELETE | 10/min | shared |
| /api/exchanges/{name}/keys | POST | 10/min | shared |
| /api/exchanges/{name}/keys | DELETE | 10/min | shared |
| /api/dashboard/layout | PUT | 10/min | shared |
| /api/admin/changelog | POST | 30/min | shared |
| /api/admin/changelog/{id} | PATCH | 30/min | shared |
| /api/admin/changelog/{id}/publish | POST | 30/min | shared |
| /api/admin/changelog/{id}/unpublish | POST | 30/min | shared |
| /api/admin/changelog/{id} | DELETE | 30/min | shared |
| /api/admin/bots/{uid}/{slug}/start | POST | 20/min | shared |
| /api/admin/bots/{uid}/{slug}/start-dry-run | POST | 20/min | shared |
| /api/admin/bots/{uid}/{slug}/stop | POST | 20/min | shared |
| /api/admin/bots/{uid}/{slug}/restart | POST | 20/min | shared |
| /api/admin/bots/bulk/stop | POST | 10/min | shared |
| /api/admin/bots/bulk/restart | POST | 10/min | shared |
| /api/emergency-stop | POST | 5/min | shared |
| /api/backtest/runs/{id} | DELETE | 10/min | shared |
| /api/bots/{slug}/drawdown/reset | POST | 10/min | shared |

Plus ~20 GET endpoints at 30-120/min (read-side). `/api/candles` additionally gated by `CostBudget(10000, 100/s)`.

**No findings.** Coverage is complete.

---

### Area 12 — CSRF / CORS

**Scope.** CSRFMiddleware coverage, exempt-path list justification, graceful-migration bypass window, CORS posture.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-042 | SHOULD-FIX | `/auth/logout` CSRF semantics ambiguous — not exempt, but idempotent | web/app.py CSRFMiddleware + web/routes/auth.py |

#### pd-042 — Logout CSRF requirement non-obvious (SHOULD-FIX)

**What.** `/auth/logout` is in `_PUBLIC_PATHS` (bypasses AuthMiddleware) but NOT in `_CSRF_EXEMPT_PATHS`. With a valid session + CSRF cookie + header, logout succeeds. With a legacy session (no CSRF cookie), graceful-migration lets it through. With a session + missing header, logout 403s.

**Why.** Logout is idempotent — the semantics of requiring CSRF on it is a coin-flip. The SPA currently sends the header, so the live flow works. But the ambiguity invites client/server drift: a future SPA refactor that assumes logout is like login (exempt) will break.

**Remediation.** Option A (recommended): add `"/auth/logout"` to `_CSRF_EXEMPT_PATHS` with an inline comment — logout can't cause a meaningful side-effect, and exempting it aligns with `/auth/login`. Option B: add an explicit docstring line on `auth_logout` reading *"CSRF header is required on this endpoint."*

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — chose a hybrid: logout stays non-exempt (graceful-migration still keeps pre-CSRF sessions working) but the exempt-paths comment now documents the decision + rationale so future maintainers don't guess.

**CORS note.** No `CORSMiddleware` is installed. Same-origin-only is the default, which matches the threat model — the SPA and the API share an origin on reverto.bot. Confirmed clean.

---

### Area 13 — File-system interaction

**Scope.** User-controlled strings flowing into `Path(...)`, symlink-follow guards, tempfile cleanup, bot-import/duplicate.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-043 | SHOULD-FIX | `dashboard_store` layout `name` parameter accepted without regex validation | core/dashboard_store.py:48, 76, 122 |
| pd-044 | MONITORING | Orphaned `.tmp` files in credentials + drawdown write paths on crash | core/credentials.py:143, web/routes/drawdown.py:63 |
| pd-045 | ACCEPTED | Slug regex enforced at every FS-touch site (`paths.bot_yaml_path` + Popen argv) | cross-tree |
| pd-046 | ACCEPTED | User-dir isolation via `str(int(user_id))` — integer-only, no traversal surface | core/paths.py |

#### pd-043 — Dashboard layout name validation (SHOULD-FIX)

**What.** `dashboard_store.get_layout`, `put_layout`, `delete_layout` accept a `name` param (defaulting to `"default"`) coerced via `str(name)` but not validated against a regex. The SQL is parameterized so there's no injection surface; the concern is defensive — a future code path that branches on the name string, or uses it as a cache key, could surprise.

**Remediation.** Add `_LAYOUT_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")` + validate at function entry. ~10 min fix.

**Category.** SHOULD-FIX (defense-in-depth).

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — `_validate_layout_name` helper with regex `^[A-Za-z0-9_\-]{1,64}$` gates all three call-sites; unit tests cover reject + accept shapes.

#### pd-044 — Orphaned `.tmp` files on crash (MONITORING)

**What.** Atomic write-then-replace pattern leaves orphaned `.tmp` files in `credentials/` + per-user `logs/` on ungraceful portal crash. The `.tmp` never gets picked up as live (replace is atomic) but accumulates over time.

**Remediation.** Add a lifespan startup hook that scans for `**/*.tmp` in `credentials/` + `logs/` and removes them with logging. Alternatively, `unlink(missing_ok=True)` after successful `replace()` (no-op on success, cleans only if something weird happened).

**Category.** MONITORING.

**STATUS.** RESOLVED in `fix/pre-deploy-final-polish` — new [core/cleanup.py](core/cleanup.py) exposes `cleanup_orphaned_tmp_files(*directories)`, wired into the lifespan startup just after config validation. Scoped to `logs/` + `credentials/`; missing-dir is silent (boot can't fail over it); per-file unlink errors log at DEBUG + continue so one broken file doesn't halt the sweep. 6 regression tests in [tests/test_cleanup.py](tests/test_cleanup.py).

#### pd-045, pd-046 (ACCEPTED)

Slug validation enforced at every FS-touch site: `_BOT_SLUG_RE` gates before `paths.bot_yaml_path`, before subprocess argv construction. User-dir parents (`config/bots/<user_id>/`, `logs/<user_id>/`, `credentials/<user_id>/`) derive from integer `user_id` only — never a user-supplied string — so there's no traversal surface.

---

### Area 14 — Third-party dependencies

**Scope.** Version-pin posture, known-CVE spot-check, CDN SRI, license posture.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-047 | SHOULD-FIX | Optional ML-stack deps use `>=` floating pins | requirements.txt:70-78 |
| pd-048 | ACCEPTED | Core deps pinned `==` at recent stable versions (fastapi, starlette, pydantic v2, cryptography, ccxt) | requirements.txt:1-60 |
| pd-049 | ACCEPTED | CDN SRI + `crossorigin="anonymous"` on every external script/link (r1-074) | web/static/index.html:1541-1546 |
| pd-050 | ACCEPTED | ccxt thread-safety documented; `_price_lock` serialises (r1-068) | exchanges/public_exchange.py |

#### pd-047 — ML-stack floating pins (SHOULD-FIX)

**What.** Commented-out optional deps in `requirements.txt:70-78` use `>=` (e.g. `optuna>=3.0.0`). ML is not shipped by default, but when activated, the build pulls arbitrary newer versions.

**Remediation.** Replace `>=` with `==` for commented ML block, pinning tested versions.

**Category.** SHOULD-FIX.

**STATUS.** RESOLVED in `fix/vps-1.5-polish` — ML block now pins `optuna==3.6.1`, `xgboost==2.1.3`, `lightgbm==4.5.0`, `scikit-learn==1.5.2`, `jupyter==1.1.1`, `matplotlib==3.9.2`, `seaborn==0.13.2`, `plotly==5.24.1`. `joblib` duplicate removed (already in core block).

---

### Area 15 — Subprocess spawn safety

**Scope.** r1-023 env allowlist, argv-as-list vs. shell-string, PID-file TOCTOU, script argument validation.

**Findings:**

| ID | Severity | Finding | File:line |
|----|----------|---------|-----------|
| pd-051 | ACCEPTED | `_BOT_ENV_ALLOWLIST` minimal + correct; no secrets leak to subprocesses | web/app.py:1077-1084 |
| pd-052 | ACCEPTED | Popen argv is a list; no `shell=True` anywhere; slugs regex-gated before argv | web/app.py:1164, 1331 |
| pd-053 | ACCEPTED | PID TOCTOU window exists but mitigated — target PID is portal-controlled + SIGTERM is graceful | web/app.py:1204-1230 |
| pd-054 | ACCEPTED | `scripts/backup.sh`, `restore.sh`, `rollback.sh` use safe arg-handling; `git rev-parse --verify` guards SHA | scripts/ |

**No SHOULD-FIX or BLOCKER findings.** Subprocess surface is the most hardened layer post-r1-023.

---

## Part 2: Cross-Reference to v1 + v1.1

Findings that extend or vary from prior audit items. Most `pd-NNN` findings are **new** — they surfaced only when the 15 focus areas were tracked against a public-deploy threat model rather than a multi-tenant SaaS threat model.

| pd-ID | Relates to | Relationship |
|-------|-----------|--------------|
| pd-001 | r1-047 | r1-047 closed bare `{"error":...}` responses. pd-001 is a narrower variant: 3 sites that now use HTTPException(detail=...) but still leak raw `str(e)` inside the detail. Not a regression of r1-047; a separate class. |
| pd-003 | r1-006 | Same route (`change-password`); different concern (r1-006 = cookie `u` field, pd-003 = HIBP call ordering). |
| pd-005 | r1-045 | r1-045 added CostBudget to `/api/candles`. pd-005 notes `/api/chart` lacks the same discipline despite a similar threat. |
| pd-007 | r1-007, r1-032 | r1-007 + r1-032 closed username validation. pd-007 is the slug equivalent: inconsistency, not missing-altogether. |
| pd-011 | r1-076 | r1-076 tightened CSP `connect-src`. pd-011 notes `Permissions-Policy` is absent — orthogonal header, adjacent concern. |
| pd-019 | r1-035 | r1-035 added the hint-logging. pd-019 notes the absence of a regression test for that behaviour. |
| pd-025 | r1-058 | r1-058 added `_validate_config()`. pd-025 notes the SECRET_KEY is required but API_KEY is only recommended — partial fix. |
| pd-026 | r1-059 | r1-059 added `.env.example` + completeness-check. pd-026 notes a missing entry (`REVERTO_LOG_LEVEL`). |
| pd-042 | r1-073 | r1-073 + hotfix landed CSRF. pd-042 notes one logout-endpoint semantic gap. |
| pd-043 | r1-053 | r1-053 added cross-tenant test. pd-043 notes a defensive-validation gap orthogonal to tenant scoping. |
| pd-044 | r1-011 | r1-011 landed atomic key rotation. pd-044 notes the generic `.tmp` orphan issue that spans multiple atomic-write sites. |

---

## Part 3: Pre-Deploy Action List

### Must-fix before VPS-3 (BLOCKER)

**None.**

### Strongly recommended (SHOULD-FIX) — landed in `fix/vps-1.5-polish`

All 11 items resolved:

1. ✅ **pd-025** — `REVERTO_API_KEY` elevated to required.
2. ✅ **pd-011** — `Permissions-Policy` header added + test.
3. ✅ **pd-001** — `OSError` details scrubbed from 500 responses in 3 sites.
4. ✅ **pd-042** — `/auth/logout` CSRF posture documented (kept non-exempt).
5. ✅ **pd-003** — HIBP call reordered after current-password verify.
6. ✅ **pd-043** — `dashboard_store` layout `name` param validated.
7. ✅ **pd-006** — `ExchangeKeysBody.passphrase` max_length reduced to 64.
8. ✅ **pd-026** — `REVERTO_LOG_LEVEL` added to `.env.example`.
9. ✅ **pd-029** — Safety comment added to `changelog_store.update_entry` f-string.
10. ✅ **pd-005** — Cost-budget attached to `/api/chart` (shared with `/api/candles`).
11. ✅ **pd-047** — ML-stack deps pinned to tested versions.

### Deploy + monitor (MONITORING)

Remaining open (observational-only; not gating):

- **pd-002** — Watch logs for repeated "CSRF graceful-migration" on same IP (runbook addition).
- **pd-007** — Document slug-regex defensive consistency pattern.
- **pd-009** — Add DEBUG-level exception-type logging in chart routes.

Closed in `fix/pre-deploy-final-polish`:

- ✅ **pd-019** — Secret-redaction regression tests added (4 tests).
- ✅ **pd-027** — Auto-resolved by pd-026 (REVERTO_LOG_LEVEL in .env.example).
- ✅ **pd-044** — Startup `.tmp` orphan cleanup hook wired into lifespan (6 tests).

### Accepted limitations (documented)

See the 37 ACCEPTED findings across the 15 areas. Notable:
- **pd-004** (24 h session TTL) — defer refresh to Phase-2.
- **pd-028** (ephemeral-key fail-soft) — dev-friendly by design.
- **pd-037** (creds plaintext in heap, r1-010) — Phase-C signing-service is the fix.
- **pd-012** (CSP `style-src 'unsafe-inline'`) — full refactor deferred.
- **pd-023** (logs mode 0644) — single-operator VPS.

---

## Part 4: Deploy-Readiness Checklist

Operational checklist for VPS-3 migration to `https://reverto.bot`:

- [x] **All BLOCKER items resolved** (trivially: there are none).
- [x] **Pre-deploy polish PR landed** — `fix/vps-1.5-polish` closed all 11 SHOULD-FIX items.
- [ ] Caddy config prepared: reverse-proxy to `127.0.0.1:8080`, Let's Encrypt auto-TLS for `reverto.bot`.
- [ ] `maintenance.html` placement verified in Caddy config (serve during portal restart).
- [ ] Firewall: only 22 (SSH) + 80 + 443 open. UFW enabled + default-deny.
- [ ] SSH: key-only auth (`PasswordAuthentication no`); no root login.
- [ ] `fail2ban` configured for SSH + optional portal-log jail.
- [ ] Ubuntu unattended-upgrades enabled (security-patches only).
- [ ] DNS: `reverto.bot` A-record prepared **but not yet pointing** to VPS IP.
- [ ] `.env` secrets generated fresh for VPS (do NOT copy dev-server secrets):
  - [ ] `REVERTO_SECRET_KEY` = `python3 -c 'import secrets; print(secrets.token_hex(32))'`
  - [ ] `REVERTO_API_KEY` = same (or stronger)
  - [ ] `REVERTO_INSECURE_COOKIES` **unset** (production = TLS-only cookies).
- [ ] DB + credentials migrated from dev-server backup via `scripts/restore.sh`.
- [ ] First deploy test on VPS (DNS not yet pointing; access via IP + `/etc/hosts` override):
  - [ ] `systemctl status reverto-portal` — green.
  - [ ] `curl http://VPS_IP/healthz` — 200 OK.
  - [ ] `curl http://VPS_IP/readyz` — 200 OK.
  - [ ] Cookie-based login via browser with `127.0.0.1 reverto.bot` in `/etc/hosts`.
- [ ] DNS cutover: `reverto.bot` A-record → VPS IP.
- [ ] Post-cutover sanity sweep (within 10 min):
  - [ ] `https://reverto.bot/healthz` returns 200.
  - [ ] Browser DevTools: HSTS header present; no mixed-content warnings.
  - [ ] `curl -I https://reverto.bot/` shows CSP + X-Frame-Options + Referrer-Policy.
  - [ ] `securityheaders.com` scan: minimum grade B.
  - [ ] Login + logout + change-password + CSRF flow all green.
- [ ] Monitor `portal.log` + `audit.jsonl` for 24 h for unexpected error patterns.
- [ ] Test rollback procedure on VPS (`make rollback` or `scripts/rollback.sh` to prior SHA).
- [ ] Verify backup cron: `scripts/backup.sh` runs daily + produces `backups/YYYY-MM-DD/`.
- [ ] Verify retention prune: old backups removed per 7/28/90-day policy.
- [ ] Document the VPS IP, the systemd unit name, and the backup location in operator runbook.

---

## Part 5: Limitations of this Audit

Honest accounting of what was **not** covered:

1. **No dynamic testing.** This is a static-review audit — no `curl`-based probing, no fuzzing, no manual browser testing of the live portal. Runtime behaviour (e.g. actual CSP enforcement in a real browser) relies on test coverage + static reading.

2. **No pentest.** No OWASP ZAP scan, no Burp session, no fuzzing. Recommend an external pentest before opening `reverto.bot` to paying customers (Phase G).

3. **No dependency CVE scan.** `pip-audit` was not run. Core deps were spot-checked by version for known-bad patterns; nothing obvious flagged.

4. **No container / systemd-unit review.** The VPS-3 operational surface (systemd service file, Caddy config, cron schedule) is out of this audit's scope — covered by the deploy runbook.

5. **No review of ML pipeline internals.** The `ml/` subsystem is a Phase-2 feature not enabled in the default deploy (per requirements.txt:70-78 commented block).

6. **No exchanges backend review.** Bitget client code (`exchanges/bitget_exchange.py`) was spot-checked for thread-safety docs (r1-068 verification) but not deep-read for order-placement correctness — outside this audit's public-exposure scope.

7. **No WebSocket deep-read.** `/ws/*` endpoints were noted as auth-gated in the handshake but not deep-read for message-validation surface. This is a known gap; a WS-specific pass would make sense post-deploy.

8. **No multi-tenant scenarios.** All findings assume single-operator context. Multi-tenant-specific concerns from v1 (e.g. `r1-013` signing-service separation, process-local caches) remain open per the Phase roadmap and are explicitly out of scope here.

---

## Part 6: Recommendations

Pattern-level observations across all 15 areas:

1. **The codebase is in solid shape for a first public deploy.** Thirty audit items closed cleanly across VPS-0, VPS-1, and the hotfix. The new findings surfaced here are polish (documentation, small reorderings, one missing header), not architectural debt.

2. **Operational hygiene, not security, is the dominant theme of the SHOULD-FIX list.** `.env.example` completeness, API-key validation strictness, documentation comments — these are the kinds of items an operator first-time-deploying will trip over, not an attacker exploiting. Spend the 3-hour polish PR to remove that friction.

3. **Schedule an external pentest before paid customers.** This audit confirms no known static-review gaps, but a dynamic/offensive scan (OWASP ZAP + manual proxy session) finds classes of issues that static review can't: business-logic abuses, session fixation through actual browser interactions, race conditions, unintended request-smuggling. Budget this before Phase G (SaaS launch), not before VPS-3 (single-operator deploy).

4. **Add `pip-audit` + `npm audit` (when frontend adds npm) to CI.** Pinned `==` versions only stop surprise upgrades — they don't tell you when a pinned version itself becomes vulnerable. Automated CVE-scanning on a weekly cadence would catch that.

5. **Consider a dependency-upgrade rotation cadence.** Quarterly minor-version bumps on core deps (fastapi, pydantic, ccxt) keep the CVE-exposure window bounded.

6. **Formalise the "defense-in-depth" doctrine.** Multiple findings (pd-007 slug regex, pd-029 f-string SQL, pd-043 layout name) pattern-match to "the real safety gate is elsewhere, but the belt-and-braces check is missing/unclear." Decide the policy: either enforce defensive validation uniformly, or write a doctrine-doc explaining why the primary gate is sufficient. Current code is *correct* but leaves future maintainers guessing.

7. **Post-deploy monitoring surface is strong.** Prom metrics (r1-033), request-id tracing (r1-034), audit-log dual-write (r1-031), per-user split — all in place. VPS-3 operator has the telemetry to catch runtime issues fast.

---

## Appendix: Files Reviewed

Scope-depth legend: **deep** (read end-to-end) / **medium** (targeted reads + grep) / **grep-only** / **not opened**.

| File | Scope | Notes |
|------|-------|-------|
| web/app.py | deep | 2676 lines — middleware stack, helpers, lifespan, config validation |
| web/routes/auth.py | deep | Login / logout / change-password / CSRF mint |
| web/routes/bots.py | deep | Lifecycle + config + duplicate + import + export |
| web/routes/chart.py | deep | Price / ticker / chart / candles + cost-budget |
| web/routes/deals.py | deep | Deals + annotations + offline close |
| web/routes/exchanges.py | deep | Keys save/delete with passphrase enforcement |
| web/routes/changelog.py | deep | Admin CRUD + audit propagation |
| web/routes/dashboard.py | medium | Layout PUT — pd-043 surfaced here |
| web/routes/drawdown.py | medium | Reset endpoint — pd-001 surfaced here |
| web/routes/backtest.py | medium | List + delete runs |
| web/routes/admin.py | medium | Emergency-stop handler |
| web/routes/admin_bots.py | medium | Admin lifecycle + bulk |
| core/database.py | medium | Schema migration + connection handling |
| core/deal_store.py | medium | User-scoped store (r1-020 + r1-042 verification) |
| core/user_store.py | medium | Username validation (r1-007/032) + password + epoch |
| core/credentials.py | deep | Fernet encrypt/rotate/backup — Area 10 |
| core/dashboard_store.py | deep | Area 13 layout validation |
| core/changelog_store.py | medium | pd-029 f-string SQL |
| core/paths.py | medium | User-dir helpers |
| core/logging_setup.py | deep | RequestIdFilter + context-var (hotfix location) |
| core/rate_budget.py | deep | CostBudget implementation |
| core/circuit_breaker.py | grep-only | Wired into PublicExchange |
| main_web.py | deep | Boot logging + RotatingFileHandler + filter attach |
| requirements.txt | deep | Pin posture review |
| .env.example | deep | Completeness check |
| .gitignore | medium | Secret-leak surface |
| web/static/index.html | medium | SRI + crossorigin on CDN resources |
| web/static/maintenance.html | medium | Static-serve safety |
| scripts/backup.sh | medium | Script safety + credential inclusion |
| scripts/restore.sh | medium | Pre-restore snapshot + plan mode |
| scripts/rollback.sh | medium | `git rev-parse --verify` guard |
| exchanges/public_exchange.py | grep-only | ccxt thread-safety docstring (r1-068) |
| exchanges/bitget_exchange.py | grep-only | ccxt thread-safety docstring (r1-068) |
| tests/test_cross_tenant_isolation.py | medium | Cross-tenant coverage |
| tests/test_csrf.py | medium | CSRF fail paths |
| tests/test_security_headers.py | medium | HSTS + CSP coverage |
| tests/test_audit_log.py | medium | Dual-write coverage |
| tests/test_request_id.py | medium | Request-id middleware coverage |
| tests/test_credentials_rotation.py | grep-only | Key rotation round-trip |
| live/live_engine.py | grep-only | Logger patterns — no secret leaks |
| paper/paper_engine.py | not opened | Engine internals outside deploy-readiness scope |
| ml/*.py | not opened | Phase-2 feature, not enabled in default deploy |
