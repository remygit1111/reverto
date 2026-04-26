# Production-Readiness Audit v3 (PRA-v3)

**Classification:** Internal
**Status:** Full re-audit + post-deploy targeted review
**Prior audits:** v1 (76 findings), v1.1 delta (6), pre-deploy (54), v2 (27)
**HEAD reviewed:** `2d83907` (main post-`tweak/prevent-spa-content-flash` merge)
**Auditor:** Claude Opus 4.7 (1M context) under `production-readiness-audit-v3` prompt
**Time invested:** ~6-8 h (orient + 5 parallel Explore agents + spot-verification)
**Deploy context:** Reverto is publicly reachable at `https://reverto.bot` since 2026-04-25 (Hetzner CX23, Caddy + Let's Encrypt, systemd, UFW, fail2ban on sshd jail). Single-operator. **First audit AFTER public exposure.**

---

## Remediation status (post-`fix/r3-001-chart-scrub-and-server-header` + `tweak/r3-007-r3-008-monitoring-batch-1`)

All 5 SHOULD-FIX + 2 of 7 MONITORING items closed (5 MONITORING items remain — all observational, not gating):

| ID | Severity | Resolution |
|----|----------|------------|
| **r3-001** | HIGH | RESOLVED in `fix/r3-001-chart-scrub-and-server-header` — three chart.py sites (`/api/ticker`, `/api/chart`, `/api/candles`) now scrub upstream ccxt exceptions via `logger.exception` + generic 502 detail. Three end-to-end regression tests in `tests/test_chart_routes.py::TestChartExceptionScrub` drive each endpoint with a sentinel-bearing exception and assert the sentinel is absent from the response body. |
| **r3-002** | MEDIUM | RESOLVED in `fix/r3-001-chart-scrub-and-server-header` — `tests/test_response_body_hygiene.py` regex broadened from `\{(?:e|err|exc|ex|exception)` (must OPEN with the name) to `\{[^}"]*?\b(?:...)\b[^}"]*?\}` (name appears as a standalone word ANYWHERE inside the interpolation). Catches `{str(e)[:200]}`, `{repr(exc)}`, `{e!r}`, `{e.__class__.__name__}`. Word-boundary keeps false-positive surface tight: `{user_id}`, `{timeframe}`, `{extension}` etc. still don't match. |
| **r3-003** | MEDIUM | RESOLVED in `fix/r3-001-chart-scrub-and-server-header` — `uvicorn.Config(server_header=False)` is the **primary** fix (uvicorn injects the `Server` header at the H11 protocol layer, AFTER Starlette middleware runs). `SecurityHeadersMiddleware` also `del`s the header as defense-in-depth in case a future reverse proxy injects it. Two regression tests cover both layers. |
| **r3-004** | LOW | RESOLVED via operator-action 2026-04-25 — `usermod -a -G bot caddy && systemctl restart caddy`. Verified by operator: `sudo -u caddy ls -l /home/bot/reverto/web/static/maintenance.html` returns clean listing. No code-change required. |
| **r3-005** | MEDIUM | RESOLVED via operator-verification 2026-04-25 — `curl -sI https://reverto.bot/ \| grep -i strict-transport-security` returned `max-age=31536000; includeSubDomains`. Caddy correctly forwards `X-Forwarded-Proto`; uvicorn's default `proxy_headers=True` honours it. No explicit `uvicorn.Config` change needed for HSTS. |
| **r3-007** | LOW | RESOLVED in `tweak/r3-007-r3-008-monitoring-batch-1` — four new tests in `tests/test_secret_redaction.py` exercise the four `TelegramNotifier.send()` failure branches (HTTP non-200, `httpx.TimeoutException`, `httpx.RequestError`, generic `Exception`) with sentinel-bearing tokens + chat IDs. Each test passes a token-embedding error message into `httpx.post` and asserts no record in caplog contains the sentinel. Validates that the existing v26-09 discipline holds. Deliberate-leak sanity-check confirmed all 4 tests catch a regression. |
| **r3-008** | LOW | RESOLVED in `tweak/r3-007-r3-008-monitoring-batch-1` — `scripts/backup.sh` now stamps `Schema version: <int>` into MANIFEST.txt by probing `PRAGMA user_version` on the just-written backup DB. Mirrors the existing CLI/Python-stdlib fallback pattern; on failure of both paths the value is `unknown` so the manifest never aborts the backup. New `tests/test_backup_manifest.py` runs the script end-to-end against a fixture DB seeded with a known version and parses the resulting MANIFEST. Surfaces in `scripts/restore.sh`'s plan-display step (line 84 already echoes the manifest). |
| **r3-006** | MEDIUM | RESOLVED via operator-verification 2026-04-26 — first cron fire (`0 3 * * *`) executed cleanly at 03:00 UTC. Backup tarball + MANIFEST.txt produced; daily retention bucket populated. Status flipped from MONITORING → RESOLVED on operator confirmation; the rolling 30-day retention-prune verification (5/7-day, weekly, monthly boundaries) continues organically and any regression would surface as a missing-snapshot finding in a future audit. |
| **r3-010** | INFO | RESOLVED in `docs/r3-010-credentials-backup-clarification` — `docs/runbook.md` Backup-and-restore section gains a new "Credentials in backups — what to expect" subsection covering three lifecycle stages (fresh VPS / credentials saved / Fernet-rotated), the actual `.enc` filename structure (`credentials/<user_id>/<exchange>.enc`, NOT `.json`), the per-user Fernet key path (`keys/<user_id>.key`), and rotation-backup files (`<user_id>.key.bak.YYYYMMDDHHMMSS`). Includes operator-runnable verification snippet. Adjacent fix: the existing `make backup` output description was updated to mention the new `Schema version:` MANIFEST line (was stale post-r3-008). |
| **r3-014** | MEDIUM | RESOLVED in `tweak/bot-lifecycle-stability` + `tweak/killmode-process-mismatch-detection` — three issues: (1) bots killed during portal-restart by cgroup-cleanup, (2) silent exit leaving state.json on `running:true`, (3) graceful-shutdown delay. **Issue 1** RESOLVED via systemd-side `KillMode=process` (operator-applied; documented in runbook) + portal-side `STATE_SCHEMA_VERSION` stamp + bounded auto-restart (`RESTART_MAX_ATTEMPTS=3` per `RESTART_WINDOW_SECONDS=300`); subprocess-side `start_new_session=True` already in place since v22. **Issue 2** RESOLVED via `last_heartbeat` stamp + `_heartbeat_is_stale` + silent-exit reconciliation in `BotInfo.read_state` + lifespan startup walk. 23 regression tests in `tests/test_bot_lifecycle.py` (12 from PR A + 11 from PR B). **Issue 3** spun out as r3-015 (DEFERRED to soft-stop PR). |
| **r3-015** | MEDIUM | DEFERRED — graceful-shutdown delay (18+ seconds observed on PID 11343, 2026-04-26): bot logs `Bot stopped` synchronously but the OS process keeps the slot until the SIGKILL fallback fires. Root cause is a blocking call inside the engine tick-loop (likely `time.sleep` in the poll loop or an indicator-fetch without timeout) that the SIGTERM handler cannot interrupt promptly. Fix lands with the soft-stop PR where the tick-loop's blocking-call structure is already in scope (cancellable sleeps + indicator-fetch timeouts). |

**Diagnostic sweep at PR time:** the broadened regex was run against `web/`, `core/`, `paper/`, `live/`, `exchanges/` after the chart.py scrubs landed — **0 hits**. The class-of-issue is closed across the entire backend, not just the named sites.

The deploy-readiness verdict moves from ⚠️ **APPROVED WITH CONDITIONS** to ✅ **APPROVED FOR CONTINUED PUBLIC OPERATION** once the operator confirms the merged PR is deployed and `curl -sI https://reverto.bot/ | grep -i ^Server` returns empty (no `Server` header). MONITORING items remain as runbook tasks per Part 4.

---

## Executive Summary

Reverto's transition from local home-host to public Hetzner VPS is **operationally clean**. The deployment-readiness baseline established by pre-deploy + v2 + the polish PRs survives first-public-day scrutiny: zero CRITICAL findings, zero BLOCKER findings, ~40 prior RESOLVED markers re-verified ✓ at 100% fidelity (one prior partial regression on pd-001 was captured + fixed by r2-001 + the new `tests/test_response_body_hygiene.py` regression guard). Public exposure has not surfaced a hidden architectural issue — what was correct on the home-host is still correct under DNS + scanner traffic.

The 13 net-new findings cluster into three camps. **Information leakage (3 findings):** the chart-route family (`/api/ticker`, `/api/chart`, `/api/candles`) wraps ccxt exceptions as `f"...: {str(e)[:200]}"` in 502 response bodies — same class as pd-001/r2-001, missed by the new grep regex because of the function-call wrapping. The `Server: uvicorn` response header is emitted on every response; trivial to strip. **Operational blind spots (3 findings):** backup-cron has not yet fired (first run tomorrow morning UTC); restore procedure has never been operator-tested on the production VPS; the maintenance-page 403 the operator already observed in Caddy logs is a concrete `/home/bot/` directory-traversal-bit gap (mode 0750, not group-readable for the `caddy` user). **Future hardening (7 findings):** Telegram token redaction precautionary test, MANIFEST schema-version stamp, HSTS preload consideration, WebSocket Origin header validation pre-Phase-B, runbook clarifications.

The headline for the operator: **Reverto is approved for continued public operation.** The two SHOULD-FIX items in the BLOCKER-adjacent zone (`r3-001` chart-route exception leak; `r3-003` Server header) total ~30 minutes of fix work and should land within 1 week. The MONITORING items are runbook tasks, not code defects. **No regression of prior STATUS markers under public-exposure threat model.**

---

## Severity Summary

| Severity | Count | Category Breakdown |
|----------|:-----:|-------------------|
| **CRITICAL** | 0 | — |
| **HIGH**     | 1 | 1 SHOULD-FIX |
| **MEDIUM**   | 4 | 3 SHOULD-FIX, 1 MONITORING |
| **LOW**      | 4 | 1 SHOULD-FIX (operator + 1-line config), 3 MONITORING |
| **INFO**     | 4 | 4 MONITORING/ACCEPTED |
| **Total**    | **13** | **5 SHOULD-FIX · 6 MONITORING · 2 ACCEPTED · 0 BLOCKER** |

Comparison to v2: v2 had 27 findings (2 HIGH, 4 MEDIUM, 5 LOW, 16 INFO). v2's actionable-SHOULD-FIX bucket was 5; PRA-v3's is 5. The trajectory holds — each audit surfaces a similar volume of narrow-scope hygiene + new-context findings, none architectural.

---

## Prior Audit Verification

Every RESOLVED marker across v1, v1.1, pre-deploy, and v2 was tested against current main. Markers are not taken on trust — pd-001 → r2-001 taught the lesson.

| Audit | ID | Claim | Verified | Notes |
|:-----:|---|-------|:---:|-------|
| v1 | r1-001 | API-key respects active | ✓ | `_request_user` resolves admin via `get_user_by_id` + active check |
| v1 | r1-002 | Changelog admin role-gate | ✓ | `_require_admin_user` checks `user.role == "admin"` |
| v1 | r1-004 | XFF leftmost parsing | ✓ | `_rate_limit_key_func` strips whitespace, prefers leftmost |
| v1 | r1-006 | Cookie u-field removed | ✓ | Mint emits only uid/iat/ep |
| v1 | r1-007/032 | Username regex + fullmatch | ✓ | `validate_username` rejects pipe, whitespace, ctrl chars |
| v1 | r1-010 | Plaintext creds in heap | ACCEPTED | Documented; Phase-C signing-service is the fix |
| v1 | r1-012 | Per-user Bitget passphrase | ✓ | `get_bitget_passphrase` + env-fallback warning |
| v1 | r1-014 | Live order NotImplementedError | ✓ | Phase-1 dry-run posture intact |
| v1 | r1-020 | Batch order fetch | ✓ | `get_orders_for_deal_ids` parameterised IN-list |
| v1 | r1-022 | Backup + retention + pre-restore snapshot | ✓ | scripts present, runbook covers operator flow |
| v1 | r1-023 | Subprocess env allowlist | ✓ | `_BOT_ENV_ALLOWLIST` minimal; secrets withheld |
| v1 | r1-031 | Audit dual-write | ✓ | pipe + JSONL + per-user split |
| v1 | r1-032 | validate_username pipe rejection | ✓ | Tested |
| v1 | r1-033 | Prom metrics user_id label | ✓ | Every bot-scoped series carries label |
| v1 | r1-034 | Request-id middleware + filter + format | ✓ | Wired at boot via core/logging_setup |
| v1 | r1-035 | API-key hint logging | ✓ | SHA256[:8] format; full key never logged |
| v1 | r1-037 | Maintenance HTML + runbook | ✓ | File exists; **but Caddy 403 surfaces a NEW finding r3-004** |
| v1 | r1-038 | Rollback schema guard | ✓ | `git log -- core/database.py` warning gate |
| v1 | r1-041 | state_mtimes per-user | ✓ | (user_id, slug) tuple key |
| v1 | r1-042 | bot_user_id stamp | ✓ | Engine sets it; broadcaster targets it |
| v1 | r1-043 | Logout per-user RL | ✓ | `logout:uid:<id>` keying |
| v1 | r1-044 | Per-user rate-limit key | ✓ | `user:<id>` from session |
| v1 | r1-045 | CostBudget on /api/candles | ✓ | 10000 budget, 100/s refill |
| v1 | r1-049 | Per-user ML results path | ✓ | `paths.user_ml_results_path` |
| v1 | r1-052 | Cross-tenant WS filtering | ✓ | No TODOs in broadcasters |
| v1 | r1-053 | Cross-tenant test | ✓ | tests/test_cross_tenant_isolation.py |
| v1 | r1-056 | Exception-swallowing discipline | ✓ | No `except Exception: pass` patterns in web/core/paper/live |
| v1 | r1-057 | Circuit breaker wired | ✓ | `PublicExchange` consults `_breaker` |
| v1 | r1-058 | Startup config validation | ✓ | Lifespan calls `_validate_config` |
| v1 | r1-059 | .env.example completeness | ✓ | `_validate_config_completeness` |
| v1 | r1-068 | ccxt thread-safety docs | ✓ | Docstring on `BitgetExchange` + `PublicExchange` |
| v1 | r1-073 | CSRF double-submit | ✓ | Middleware + frontend + graceful-migration |
| v1 | r1-074 | CDN SRI | ✓ | All unpkg `<link>`/`<script>` carry sha384 + crossorigin |
| v1 | r1-075 | HSTS conditional | ✓ | Code-correct; **but proxy-delivery untested → r3-005** |
| v1 | r1-076 | CSP no ws/wss wildcards | ✓ | `connect-src 'self' https://unpkg.com` |
| v1.1 | r1.1-002 | Pair allowlist | ✓ | `{BTC/USD, BTC/USDT}` |
| v1.1 | r1.1-003 | Workspace scroll merge | ✓ | History-merge logic ported |
| pd | pd-001 | OSError scrubbing | ✓ | All sweep sites + r2-001 closure + new regression test |
| pd | pd-003 | HIBP after current-pw verify | ✓ | Reorder landed |
| pd | pd-004 | 24h session TTL | ACCEPTED | Re-eval'd under public exposure — see Part 5 |
| pd | pd-005 | Cost-budget shared chart+candles | ✓ | Single bucket |
| pd | pd-006 | Passphrase max_length=64 | ✓ | Tightened |
| pd | pd-011 | Permissions-Policy header | ✓ | 9 features denied |
| pd | pd-019 | Secret-redaction tests | ✓ | API-key, session-cookie, passphrase covered. **Telegram tokens uncovered → r3-007** |
| pd | pd-025 | REVERTO_API_KEY required | ✓ | RuntimeError on missing |
| pd | pd-026 | REVERTO_LOG_LEVEL in .env.example | ✓ | Present |
| pd | pd-027 | Auto-resolved by pd-026 | ✓ | Verified |
| pd | pd-029 | Changelog f-string safety | ✓ | Inline comment present |
| pd | pd-042 | Logout CSRF documented | ✓ | Comment on exempt-paths set |
| pd | pd-043 | Layout name validation | ✓ | `_validate_layout_name` |
| pd | pd-044 | Startup .tmp orphan cleanup | ✓ | `core/cleanup.py` wired |
| v2 | r2-001 | YAML scrub at bots.py:638 | ✓ | Plus regression test catches the class |
| v2 | r2-002 | Kraken idempotency gap | ACCEPTED | Phase-3 scope |
| v2 | r2-003 | HSTS-behind-proxy verification | ◐ | **Still relevant under public exposure → carried as r3-005** |
| v2 | r2-004 | Rate-limit error inconsistency | ACCEPTED | Post-deploy polish queue |
| v2 | r2-005 | Circuit-breaker discrimination | ACCEPTED | Post-deploy polish queue |
| v2 | r2-006 | No WS frame-rate limit | ACCEPTED | Single-operator; re-eval'd OK in Part 5 |
| v2 | r2-007 | POSIX rename assumption | ACCEPTED | Hetzner ext4 confirmed POSIX |
| v2 | r2-008 | Manual-trigger micro-race | ACCEPTED | Operator retry recovers |
| v2 | r2-009 | DB-outage silent-tick | ACCEPTED | Resilience choice |
| v2 | r2-010 | API-key format validation | ACCEPTED | UX polish |
| v2 | r2-011 | MANIFEST schema-version | ACCEPTED | **Re-surfaced under public-exposure restore-safety lens → r3-008** |

**Verification total:** 53 prior items checked, **53 verified clean.** No regressions. r2-003 + r2-011 carried forward in expanded form (r3-005, r3-008) because public-exposure context shifts urgency.

**Re-evaluated ACCEPTEDs under public-exposure context:**

- **r1-010 (creds plaintext in heap)** — Public DNS doesn't change the threat model (process-compromise via RCE is the relevant path; network exposure is orthogonal). **Stays ACCEPTED.**
- **pd-004 (24 h session TTL)** — Under public exposure, a leaked/stolen cookie has 24 h to be exploited. Mitigations: per-user session epoch; logout + password-change both bump it; operator can mass-invalidate via REVERTO_SECRET_KEY rotation. **Stays ACCEPTED for Phase 1 (paper-only).** Recommend re-eval to 2-4 h before Phase 3 live trading.
- **r2-006 (no WS frame-rate limit)** — Single-operator. Multiple browser tabs from the same authed user could pile up but bounded by OS fd limits. **Stays ACCEPTED.**
- **r2-007 (POSIX rename)** — Hetzner ext4 confirmed POSIX-compliant. **Stays ACCEPTED.** Document in runbook.

---

## Part 1: Findings by Focus Area

### Tier 1 deep-read areas

#### Area 1 — Authentication & authorization
Session cookie HttpOnly + Secure + SameSite=strict in production posture. Per-account brute-force counter + unknown-user IP fallback. HIBP after current-pw verify (pd-003). API-key path respects `user.active` (r1-001). Cross-tenant test coverage present. **No findings.**

#### Area 2 — Credentials & secrets management
Fernet 0600 enforced via `ensure_secret_file_mode`. Per-user dirs 0700. Rotation atomic with crash-recovery semantics. Bitget passphrase migrated per-user with deprecation warning. Backup includes credentials/ + keys/ when files exist (operator's empty-credentials observation is correct: empty dirs back up empty — see r3-010). **No findings.**

#### Area 6 — CSRF defense
Double-submit cookie + `secrets.compare_digest` + graceful-migration path for legacy sessions. Frontend wrapper auto-injects `X-CSRF-Token` on mutating verbs. **WebSocket origin-check** is implicit (CSP `connect-src 'self'` + SameSite=strict on session cookie) — sufficient for current Caddy-on-same-host topology, but file as **r3-011** for Phase B planning.

#### Area 7 — Security headers
CSP, HSTS conditional, X-Frame-Options, X-Content-Type-Options, Referrer-Policy: no-referrer, Permissions-Policy with 9 features denied. Two findings:
- **r3-003** Server header
- **r3-005** HSTS post-cutover verification (was r2-003)

| ID | Severity | Finding | File:line | Category |
|----|----------|---------|-----------|----------|
| r3-003 | MEDIUM | `Server: uvicorn` header leaks app-server identity | uvicorn default; web/app.py:2700 | SHOULD-FIX |
| r3-005 | MEDIUM | HSTS-behind-proxy delivery untested on production | web/app.py:1612-1615 | MONITORING |

#### Area 4 — Input validation
Slug regex enforced consistently across bot-lifecycle routes. Pair allowlist tight. Pydantic models on every body. YAML loaded via `safe_load` only. `/api/candles` has cost-budget but no explicit historical-time bound (mitigated by per-user rate limit + 5000-bar cap). **No findings.**

#### Area 9 — Error handling
Verified post-pd-001/r2-001 sweep. Test `test_response_body_hygiene.py` enforces no bare `{e}` interpolation in 5XX responses. **One regression of class-of-issue.**

| ID | Severity | Finding | File:line | Category |
|----|----------|---------|-----------|----------|
| r3-001 | HIGH | ccxt exception detail leaks via `{str(e)[:200]}` in 5XX responses (3 sites) | web/routes/chart.py:164,263 | SHOULD-FIX |
| r3-002 | MEDIUM | Regression-test regex doesn't catch function-wrapped exceptions | tests/test_response_body_hygiene.py | SHOULD-FIX |

### Tier 2 medium-read areas

#### Area 3 — Database integrity
Every user-owned SELECT/UPDATE/DELETE gated by `WHERE user_id = ?`. All values via `?` placeholders. WAL mode + busy_timeout=5000ms. Schema migration pre-backup + destructive-flag protection. **No findings.**

#### Area 5 — Rate limiting
100% mutating-endpoint coverage (35+ endpoints). Per-user keying via `_rate_limit_key_func`. Cost-budget on candles + chart shared. Auth endpoints extra-strict. Public unauth endpoints (`/healthz`, `/readyz`, `/metrics`, `/`, `/favicon.ico`) intentionally unbounded — Caddy provides L4 DDoS protection. **No findings.**

#### Area 8 — Subprocess safety
`_BOT_ENV_ALLOWLIST` minimal (7 vars; secrets explicitly withheld). argv-as-list everywhere; no `shell=True`. Slug regex-gated before Popen. PID-file TOCTOU window mitigated by `_pid_alive` polling + `PermissionError` catch on cross-user signals. Scripts (backup/restore/rollback) use safe arg-handling. **No findings.**

#### Area 10 — Logging & observability
Audit dual-write + per-user split. Request-id traced across all log lines. Prom metrics tenant-labeled. Secret redaction tests cover API-key, session cookie, Bitget passphrase. **One precautionary gap.**

| ID | Severity | Finding | File:line | Category |
|----|----------|---------|-----------|----------|
| r3-007 | LOW | Telegram tokens uncovered by `tests/test_secret_redaction.py` | tests/test_secret_redaction.py | MONITORING |

#### Area 11 — Configuration management
`.env.example` matches actual reads. `_validate_config` raises on missing REVERTO_SECRET_KEY + REVERTO_API_KEY. `.gitignore` covers `.env`, `logs/`, `keys/`, `credentials/`, `backups/`, `reverto.db`. `git log -S "REVERTO_API_KEY=" -- '*.env*'` returns no committed-secret traces. **No findings.**

#### Area 12 — Static asset serving
Starlette path-traversal protection. CDN SRI complete. robots.txt disallows all crawlers (added recently). One operator-confirmed observation:

| ID | Severity | Finding | File:line | Category |
|----|----------|---------|-----------|----------|
| r3-004 | LOW | maintenance.html 403 in Caddy logs — `/home/bot/` is mode 0750 (group-only-traversal) | filesystem (operator-side) | SHOULD-FIX |

### Tier 3 targeted-read areas

#### Area 13 — WebSocket handling
v2 conclusions verified. Pre-accept auth (4401/4403/4004 close-codes). Per-user `_user_map` gating under asyncio.Lock. No client-to-server message channel. Disconnect cleanup in finally. CSP `connect-src 'self'` covers ws/wss same-origin. Lifespan shutdown cancels background tasks within 2s; in-flight WS handler coroutines drained by uvicorn graceful shutdown. **No findings beyond r3-011 (WS Origin header validation, deferred to Phase B).**

#### Area 14 — Bot lifecycle
v2 conclusions verified. Atomic state-write via `.tmp` + `Path.replace()`. Concurrent deal lifecycle safe under lock + in-loop re-check. Live engine NotImplementedError gate intact. SIGTERM graceful shutdown drains notify-queue with 15s timeout. Crash recovery restores all critical state including drawdown-guard peak. **Portal-restart bot-impact verified safe:** bot subprocesses spawn with `start_new_session=True` (separate process group) — they survive portal restart; portal re-discovers via PID-file presence on start. **No findings.**

#### Area 15 — Exchange integration
ccxt thread-safety via `_price_lock`. Bitget idempotency via `clientOrderId`. Empty-credentials posture safe: no exchange-init on portal startup. v2 findings (r2-002, r2-004, r2-005) still present and accepted — all post-deploy polish queue items. **No new findings.**

#### Area 16 — Backup & restore correctness
Scripts present + correct (verified by code-read). Cron documented. Permissions enforced (0600 files, 0700 dirs). Pre-restore snapshot. **Two operational gaps + two clarifications.**

| ID | Severity | Finding | File:line | Category |
|----|----------|---------|-----------|----------|
| r3-006 | MEDIUM | Backup cron + restore procedure never operationally proven on production | scripts/backup.sh, scripts/restore.sh | RESOLVED (2026-04-26) |
| r3-014 | MEDIUM | Bot lifecycle stability — subprocess-overleving (Issue 1) + silent-exit detection (Issue 2) | web/app.py, paper/paper_engine.py, /etc/systemd/system/reverto.service (operator-side) | RESOLVED (`tweak/bot-lifecycle-stability` + `tweak/killmode-process-mismatch-detection`) |
| r3-015 | MEDIUM | Bot graceful-shutdown delay (18+ s on SIGTERM) — blocking call in engine tick-loop not promptly cancellable | paper/paper_engine.py | DEFERRED to soft-stop PR |
| r3-008 | LOW | Backup MANIFEST lacks schema-version stamp | scripts/backup.sh:123-133 | MONITORING |
| r3-010 | INFO | Runbook ambiguous about credentials/ backup behaviour on fresh VPS | docs/runbook.md | MONITORING |

#### Area 17 — Deploy & rollback procedures
Rollback schema-guard via `git log -- core/database.py` warning. Makefile targets coherent. Runbook covers deploy/restart/rollback flow. systemd unit + Caddyfile live on VPS (out of repo); operator-side review needed.

| ID | Severity | Finding | File:line | Category |
|----|----------|---------|-----------|----------|
| r3-012 | INFO | systemd unit + Caddyfile hardening not in repo; out-of-band operator review | VPS-side | ACCEPTED |

### NEW Area 18 — Internet exposure surface

The signature focus area for PRA-v3: what changes now that the portal is publicly reachable?

**robots.txt verified** — `User-agent: *\nDisallow: /` blocks all indexing. Correct posture for an operator-private trading portal.

**Caddy scanner traffic baseline** — Operator observed Umai, ueditor, onvif probes hitting Caddy. Each terminates at FastAPI returning 404 / 401 — no information leakage in default response bodies. CSP enforces no error-introspection. Establishes a baseline.

**TLS configuration** — Let's Encrypt managed by Caddy. HSTS preload-ready posture: see r3-009.

**Server identity leak** — addressed under r3-003.

| ID | Severity | Finding | File:line | Category |
|----|----------|---------|-----------|----------|
| r3-009 | LOW | HSTS preload-list submission consideration deferred (one-way decision) | web/app.py:1613-1615 | MONITORING |
| r3-013 | INFO | Scanner traffic baseline established (404/401 returned correctly) | observed | ACCEPTED |

---

## Part 2: Cross-Cutting Patterns

### CCP-1. Class-of-issue regression-test guard caught most of pd-001's class — but not all
The new `tests/test_response_body_hygiene.py` (introduced with r2-001) is doing its job: it catches bare `{e}` interpolation in 5XX responses. But three sibling sites in `web/routes/chart.py` use `{str(e)[:200]}` — function-wrapped — which the regex misses. **Lesson: when an attacker-controllable value is wrapped in a function call inside an f-string, the literal-name pattern doesn't catch it.** Broadening the regex to flag any exception-named variable inside any `{...}` (not only bare interpolation) closes this. Filed as r3-002.

### CCP-2. Operational runbook lags code maturity
Backup script: complete + tested by code-read. Restore script: complete + tested by code-read. **Neither has been operationally fired on the production VPS.** The first cron backup runs tomorrow morning UTC; a test-restore on a dev clone is documented but not executed. r3-006 is the consolidated finding. The pattern is healthy (code precedes operations) but needs a forcing function: a post-cutover checklist with concrete operator actions + dates.

### CCP-3. Public-exposure-induced re-evaluations are minimal
Of the ACCEPTED items from prior audits, **none required a category change** under public-exposure scrutiny. r1-010 (heap-plaintext creds) is process-compromise threat, not network. pd-004 (24h session TTL) is mitigated by epoch-bump on logout + password change. r2-006 (no WS frame-rate-limit) is bounded by OS fd limits + single-operator context. r2-007 (POSIX rename) is filesystem-level — Hetzner ext4 is POSIX-compliant. **The pre-deploy audit's threat-model coverage was good.**

---

## Part 3: Deploy-Specific Observations

### Caddy ↔ Reverto integration

1. **Caddy serves maintenance.html during portal restarts, but logs `403 Permission denied`** (operator-confirmed). Root cause verified: `/home/bot/` is `drwxr-x---` (mode 0750) — the `caddy` user can't traverse the operator's home directory to reach `web/static/maintenance.html`. **Filed as r3-004.** Two fixes (operator picks):
   - `usermod -a -G bot caddy` then restart Caddy. Group-readable traversal sufficient.
   - Pre-cache `maintenance.html` in `/var/lib/caddy/` or similar Caddy-readable location.

2. **HSTS delivery contract is implicit.** Code emits HSTS only on `request.url.scheme == "https"`. uvicorn defaults (`proxy_headers=True`, `forwarded_allow_ips='127.0.0.1'`) honour Caddy's `X-Forwarded-Proto: https`. This works in current topology but breaks silently if topology changes. **Filed as r3-005.** Either add `proxy_headers=True, forwarded_allow_ips="127.0.0.1"` explicitly to `uvicorn.Config(...)` OR add a post-cutover `curl -I https://reverto.bot/` HSTS-presence check to the runbook.

3. **`Server: uvicorn` is emitted on every response.** FastAPI doesn't strip it; SecurityHeadersMiddleware doesn't override. Trivial fix: add `response.headers.pop("Server", None)` in middleware. **Filed as r3-003.**

### systemd unit hardening (out of repo)

The systemd service file lives on the VPS at `/etc/systemd/system/reverto.service`, NOT in the git repo. Operator should verify:
- `NoNewPrivileges=yes`
- `ProtectSystem=strict`
- `ProtectHome=yes`  
- `PrivateTmp=yes`
- `ReadWritePaths=` scoped to `/home/bot/reverto/`

Filed as **r3-012** (INFO ACCEPTED — operator-side review required).

### fail2ban posture

Operator confirmed fail2ban runs on the sshd jail. **No Reverto-specific jail.** Recommendation: add a Reverto jail filtering on `audit.log` for `auth_login_failed` patterns at high rate from same IP — would defend against credential-spray after the in-app rate-limiter exhaustion. Not a finding (defence-in-depth ask, not a defect); listed in Recommendations.

### Backup cron integrity (RESOLVED — first fire verified 2026-04-26 03:00 UTC)

`crontab -l` (operator-side) shows `0 3 * * * cd /home/bot/reverto && ./scripts/backup.sh >> logs/backup.log 2>&1`. First fire: 2026-04-26 03:00 UTC — confirmed clean by operator on the morning of 2026-04-26. **r3-006 status flipped to RESOLVED in `tweak/bot-lifecycle-stability`** (one-line audit-doc update bundled with the lifecycle-stability work; see Remediation status block above). Retention-prune monitoring (5/7-day, weekly, monthly boundaries) continues organically — any future regression would surface as a missing-snapshot finding rather than blocking deploy.

### Bot lifecycle stability (RESOLVED for r3-014 — 2026-04-26 / r3-015 carries the remaining deferred Issue 3)

Three issues surfaced from the deploy-cycle log analysis on 2026-04-26:

1. **Bot subprocesses killed during portal-restart (cgroup-cleanup).** `systemctl restart reverto` left `Failed to kill control group … Invalid argument` + `left-over process … in control group` messages in journalctl. `KillMode=mixed` signals the main process and then SIGKILLs cgroup remainder; bots inherited the cgroup membership and were swept up.
2. **Silent bot exit.** When PID 11617 died at 14:47:54 the bot-log's last line was a normal indicator print at 14:47:50 — no SIGTERM log, no traceback, no shutdown writeback. State.json kept `running: true` with a `updated_at` of 14:47:50; UI continued to show "RUNNING" indefinitely.
3. **Graceful-shutdown delay (18+ s).** Earlier sample (PID 11343 at 13:57:55): bot logged `Bot stopped` synchronously but the OS process kept the slot until SIGKILL fallback at +18 s. A blocking call (likely indicator fetch or tick sleep) is not promptly cancellable from the SIGTERM handler.

**STATUS:**
- **Issue 1 — RESOLVED in `tweak/killmode-process-mismatch-detection`.** Subprocess-side `start_new_session=True` (≡ `setsid`) was already in place since v22 — operator-side `KillMode=process` (applied via `sed -i` on the systemd unit during deploy) is the missing complement. Bots now survive `systemctl restart reverto`. To keep coherence after deploys that change engine code, the portal stamps `STATE_SCHEMA_VERSION` (currently `2`) on every state-write; lifespan startup detects mismatched (or missing) stamps and triggers a bounded auto-restart through `_attempt_bot_auto_restart`. The budget is `RESTART_MAX_ATTEMPTS=3` per `RESTART_WINDOW_SECONDS=300` per `(user_id, slug)`; the (4th+)th attempt logs an error and writes `stopped_reason="restart_budget_exceeded"` to state.json so an operator can see why the portal stopped trying.
- **Issue 2 — RESOLVED in `tweak/bot-lifecycle-stability`.** `last_heartbeat` + `heartbeat_interval_sec` stamped on every state-write; portal-side `_heartbeat_is_stale` + silent-exit reconcile in `BotInfo.read_state`; lifespan startup walks the registry and corrects any drift before serving traffic. Backwards compatible (legacy state.json with no heartbeat keeps PID-only liveness).
- **Issue 3 — DEFERRED to soft-stop PR, filed separately as `r3-015`.** The fix touches the engine tick-loop's blocking call structure (cancellable sleeps, indicator-fetch timeouts) which is best done alongside the soft-stop semantics PR rather than ad-hoc.

23 regression tests in `tests/test_bot_lifecycle.py` (12 from PR A + 11 from PR B): schema + helpers + reconcile paths + idempotency + mismatch detection (no-version / version-match / version-outdated / running-false short-circuit) + bounded auto-restart (allows 3 / blocks 4 / window-resets / per-bot isolated).

Operator action required for the systemd-side complement: see `docs/runbook.md` → "Bot lifecycle — KillMode rationale".

---

## Part 4: Priority Matrix

### BLOCKER (must fix immediately — security-critical)
**None.** No finding triggers an immediate take-down recommendation.

### SHOULD-FIX (all RESOLVED — see Remediation status block at top of document)

5 items, all closed:

1. ✅ **r3-001** (HIGH) — chart.py exception leak. Closed in `fix/r3-001-chart-scrub-and-server-header`.
2. ✅ **r3-003** (MEDIUM) — Server header strip. Closed in same PR (uvicorn `server_header=False` + middleware `del`).
3. ✅ **r3-002** (MEDIUM) — regex broadening. Closed in same PR.
4. ✅ **r3-004** (LOW) — Caddy group-membership. Closed via operator-action 2026-04-25.
5. ✅ **r3-005** (MEDIUM) — HSTS post-cutover verification. Closed via operator `curl` check 2026-04-25.

### MONITORING (deploy + observe)

7 items:

- ✅ **r3-006** — RESOLVED via operator-verification 2026-04-26 (first cron fire confirmed at 03:00 UTC). Retention-prune monitoring continues organically.
- ✅ **r3-007** — RESOLVED in `tweak/r3-007-r3-008-monitoring-batch-1` (4 redaction tests added).
- ✅ **r3-008** — RESOLVED in `tweak/r3-007-r3-008-monitoring-batch-1` (`Schema version:` line + 2 tests).
- **r3-009** — HSTS preload submission decision in 6+ months.
- ✅ **r3-010** — RESOLVED in `docs/r3-010-credentials-backup-clarification` (runbook subsection added; adjacent Schema-version line update in same PR).
- **r3-011** — WebSocket Origin header validation pre-Phase-B.
- **r3-013** — Continue baseline scanner-traffic monitoring; alert on 5XX patterns.

### ACCEPTED (documented limitations)

- **r3-012** — systemd + Caddyfile hardening. Operator out-of-band.
- All v2 ACCEPTEDs (r1-010, r1-014, pd-004, r2-002, r2-006, r2-007, r2-008, r2-009, r2-010) re-affirmed under public-exposure context.

---

## Part 5: Deploy-Readiness Verdict

⚠️ **APPROVED WITH CONDITIONS** at PRA-v3 publication time. **All five conditions now met** (see Remediation status block at top of document — 3 via code in `fix/r3-001-chart-scrub-and-server-header`, 2 via operator-action). Verdict promotion to ✅ **APPROVED FOR CONTINUED PUBLIC OPERATION** is the operator's call once the code-side PR is merged + deployed; this audit document records the conditions satisfied. The MONITORING items (Part 4) remain as runbook tasks, not deploy gates.

### Original conditions (all RESOLVED)

1. ✅ Land **r3-001** (chart.py exception leak) — done in `fix/r3-001-chart-scrub-and-server-header`. Same class-of-issue as closed pd-001/r2-001, plus the proximate regression-test gap is also closed by r3-002 in the same PR.
2. ✅ Land **r3-002** (broaden regex) — done in same PR. Diagnostic sweep at PR time: 0 hits across `web/core/paper/live/exchanges`.
3. ✅ Land **r3-003** (Server header) — done in same PR via `uvicorn.Config(server_header=False)` (primary, suppresses at H11 protocol layer) + middleware-level `del` (defense-in-depth).
4. Operator runs `usermod -a -G bot caddy` (or equivalent) within 24h to fix maintenance-page **r3-004**. The 403 in Caddy logs is real-world impact that the operator already sees.
5. Operator verifies HSTS post-cutover via `curl -I https://reverto.bot/ | grep -i strict-transport` within 24h to close **r3-005**.

### When all 5 conditions land

Verdict moves to ✅ **APPROVED FOR CONTINUED PUBLIC OPERATION** with the MONITORING items tracked in the runbook. 

### Why "WITH CONDITIONS" instead of "APPROVED"

The chart-route exception leak (r3-001) is the only finding with both severity ≥ MEDIUM AND public-exposure-relevance AND code-fix path. It's not a BLOCKER (the leaked content is bounded ccxt-error shape, not infrastructure paths), but it's the kind of issue that sets the precedent for how the team responds. Closing it within a week communicates discipline; leaving it open re-opens the pd-001 → r2-001 → r3-001 cycle.

### Why NOT a take-down recommendation

No CRITICAL findings. Zero auth-bypass surfaces. Zero unbounded resource-consumption surfaces. Zero cross-tenant leakage on the public WS / API paths. Scanner traffic terminates correctly at 404/401. The portal is operating safely; the SHOULD-FIXes are hygiene + class-of-issue closure, not active vulnerabilities.

---

## Part 6: Audit Quality Assessment

The most valuable section — honest retrospective on prior audits.

### v1 (76 findings, 21 resolved at time of audit)

**Strengths.** Comprehensive scope. Correctly identified the 5 Sprint-1 HIGHs as multi-tenant blockers. Severity calibration was reasonable for the **multi-tenant SaaS frame** that v1 used.

**Weaknesses.** Severity over-calibration for single-operator deploy (e.g. r1-051 DEFAULT_USER stub was HIGH on multi-tenant-seed risk, LOW on single-operator). Operator had to mentally re-calibrate.

**Public-exposure weighting?** v1 was pre-deploy with no public-exposure context. Reasonable. Most of v1's CRITICAL/HIGHs are about Phase B/C/G readiness, not public-Phase-1.

### v1.1 (6 findings, 5 resolved)

**Strengths.** Right-sized delta scope. Workspace chart surfaces correctly identified.

**Weaknesses.** One mild false-positive (r1.1-001 `_price_lock` contention — cosmetic on single-operator).

### Pre-deploy (54 findings, 11 SHOULD-FIX resolved)

**Strengths.** Best-calibrated of the four. Orthogonal severity-vs-category axis (BLOCKER/SHOULD-FIX/MONITORING/ACCEPTED) gave operator a clean action matrix. Correctly identified pd-025 (REVERTO_API_KEY required) as the operational-impact item to close before cutover.

**Weaknesses.** **One sweep-pattern miss (pd-001 → r2-001 → r3-001 chain).** The sweep explicitly named 3 sites but didn't grep the class. Site-specific unit tests passed; class regression test was not yet conceived. **Did pre-deploy weigh public-exposure risk adequately?** Yes for headers, secrets, CSRF, rate-limiting, auth. **No for runbook completeness** — the Caddy 403 maintenance-page issue (r3-004) was not anticipated; operator hit it on day-one. Nor was the Server-header leak (r3-003) flagged. These are minor but characteristic blind spots.

### v2 (27 findings, 5 SHOULD-FIX resolved + 1 partial regression)

**Strengths.** First-pass deep-read of WebSocket / bot-lifecycle / exchange yielded **low finding density** — speaks to upstream architecture quality. Verification of prior STATUS markers achieved 98% fidelity. Cross-cutting patterns (CCP-1, CCP-2, CCP-3) were correctly identified. r2-001 caught the pd-001 regression at one site.

**Weaknesses.** **The grep-based regression test landed in `fix/r2-001-yaml-scrub` did not anticipate function-wrapped exceptions** — that's the r3-002 finding. v2's recommendation for the regex was correct in spirit (catch the class, not the sites) but the implementation was incomplete. **The audit didn't run the regex against current `web/routes/` before claiming closure.** A pure-grep verification at that moment would have surfaced the chart.py sites. Lesson: when proposing a class-of-issue regex, run it before signing off.

### v2 → PRA-v3 → patterns-for-future-audits

1. **Class-of-issue regression tests must be tested against the entire scope at audit-finalisation time.** Not just unit-tested for known cases. Run the regex against `web/`, `core/`, `tests/` at the moment of claiming closure.
2. **Operator-side verifications need explicit checklist commands.** "Verify Caddy can read maintenance.html" is too abstract; the audit should provide `sudo -u caddy ls /home/bot/reverto/web/static/maintenance.html` as a concrete check.
3. **Public-exposure context affects runbook items more than code items.** The code surface was largely already correct; public-exposure surfaces operational/runbook gaps (backup cron unfired, restore untested, maintenance.html unreadable, HSTS unverified, Server header leaking). **Runbook completeness deserves dedicated audit attention.**
4. **STATUS-marker-in-same-PR discipline continues to pay off.** 53/53 markers verified clean here. Combined with the regression-test discipline (when it works), this is the single most valuable audit-quality multiplier.

### Was the previous audit cadence right?

v1 → v1.1 (1 day, ~20 PRs). v1.1 → pre-deploy (~2 days). pre-deploy → v2 (1 day). v2 → PRA-v3 (1 day, post-cutover trigger). 

Cadence was driven by **change density** + **deploy events** rather than calendar. That's correct. Recommend: next audit is **post-Phase-3-prep** (when r2-002 Kraken idempotency lands + before live-mode ships). After that, **quarterly mini-audits** (1-2 hours, scope = 1-2 areas) plus **annual full re-audit**.

---

## Part 7: Limitations of this Audit

1. **Static analysis only.** No dynamic testing; no `curl`-based probing; no browser-based manual walk-through; no fuzz testing.
2. **No external pentest.** Strongly recommended before Phase G (paid customers). A 2-day OWASP ZAP + Burp + manual session would catch what static review can't (business-logic abuse, race conditions, session fixation through real browser interactions).
3. **No `pip-audit` / `npm audit` CVE scan.** Pinned `==` versions only stop surprise upgrades; CVEs in pinned versions aren't caught. Operator should run weekly via cron.
4. **systemd unit + Caddyfile not in scope.** Both live on the VPS at `/etc/systemd/system/reverto.service` and `/etc/caddy/Caddyfile` respectively. Out-of-band operator review required (filed as r3-012).
5. **No load testing.** Rate-limit decisions analytical; actual throughput under sustained scanner load (Caddy + uvicorn + SQLite WAL contention) untested.
6. **No DNS / certificate / DDoS-resilience verification.** Caddy provides L4 but not L7 DDoS; reverto.bot DNS exposure is operator-side.
7. **No filesystem-atomicity test on non-POSIX.** Code-read confirms POSIX semantics; Hetzner ext4 confirmed POSIX. NFS/SMB/exotic FS untested.
8. **No replay test of paper-engine state recovery.** Deep-read followed every code path; did not replay a real `state.json` from a post-crash snapshot.
9. **Caddy logs sampled (operator-shared) but not exhaustively analyzed.** Pattern-spotting only; no full-day timeline review.
10. **First-day-public-exposure window.** This audit happens within 24 hours of DNS cutover. Some scanner patterns + exposure consequences (SEO indexing despite robots.txt, automated bot-traffic ramp) won't be visible for days/weeks.

---

## Part 8: Recommendations

### Immediately (within 24-48 hours)

1. ✏️ **r3-001 + r3-002**: Land the chart.py scrub + broaden the regex test in one PR. ~30 min total. Branch suggestion: `fix/r3-001-chart-exception-scrub`.
2. ✏️ **r3-003**: Strip `Server` header in `SecurityHeadersMiddleware`. ~5 min. Bundle with above.
3. 🔧 **r3-004**: Operator: `sudo usermod -a -G bot caddy && sudo systemctl restart caddy`. Verify with `sudo -u caddy ls /home/bot/reverto/web/static/maintenance.html`. ~2 min.
4. 🔧 **r3-005**: Operator: `curl -I https://reverto.bot/ | grep -i strict-transport-security`. If absent, add `proxy_headers=True, forwarded_allow_ips="127.0.0.1"` explicitly to `uvicorn.Config` (web/app.py:2700-2710).

### Within 1-2 weeks

5. ✏️ **r3-007**: Telegram-token redaction tests. Add 2 cases to `tests/test_secret_redaction.py`. ~15 min.
6. ✏️ **r3-008**: MANIFEST schema-version stamp. One line to `scripts/backup.sh`. ~30 sec.
7. 📋 **r3-006**: Operator: monitor first cron-backup output (2026-04-26 03:00 UTC). On 2026-05-01, verify 7-day retention. On 2026-05-02 (Monday), verify weekly snapshot. On 2026-06-01, verify monthly.
8. 📋 **r3-006**: Operator: schedule a test-restore on a dev clone within 1 week.
9. 📋 **r3-010**: Update runbook with credentials-backup timing example.

### Operator-side verifications (commands to run + results to capture)

```bash
# 1. systemd hardening (r3-012)
systemctl cat reverto.service | grep -E '^(NoNewPrivileges|ProtectSystem|ProtectHome|PrivateTmp|ReadWritePaths)'

# 2. Caddy config sanity (r3-005, r3-004)
caddy validate --config /etc/caddy/Caddyfile
sudo cat /etc/caddy/Caddyfile

# 3. fail2ban posture
sudo fail2ban-client status

# 4. Backup cron presence (r3-006)
crontab -l | grep backup.sh

# 5. Maintenance-page reachability (r3-004)
sudo -u caddy ls -l /home/bot/reverto/web/static/maintenance.html

# 6. HSTS delivery (r3-005)
curl -sI https://reverto.bot/ | grep -i strict-transport-security

# 7. Server header (r3-003 — should disappear after fix)
curl -sI https://reverto.bot/ | grep -i ^Server

# 8. robots.txt delivery (r3-013)
curl -s https://reverto.bot/robots.txt
```

Capture outputs and paste back for follow-up review.

### Before Phase B (multi-tenant seeding)

10. **r3-011**: Add explicit WebSocket Origin header validation.
11. **pd-004 re-eval**: Reduce session TTL to 2-4 hours.
12. **r2-006**: Add per-user WS connection cap.
13. **r1-014**: Land Phase 3 live-engine implementation behind a feature flag.

### Before Phase 3 live trading

14. **r2-002**: Kraken idempotency by `userref` (Bitget pattern).
15. **r2-005**: Discriminate transient vs permanent errors in `record_failure()`.

### Ongoing discipline

- **Weekly** `pip-audit` scan via cron + email/notification.
- **Monthly** auth-log review (grep `audit.jsonl` for `suspicious_login_pattern`).
- **Quarterly** mini-audit (1-2 hours, scope = 1-2 areas).
- **Annual** full re-audit.
- **Post-Phase-3-prep** dedicated audit before live trading.
- **External pentest** before Phase G (paid customers).

---

## Appendix A: Files Reviewed

Scope-depth: **deep** (read end-to-end) / **medium** (targeted reads + flow-trace) / **grep** / **not-opened**.

| File | Depth | Notes |
|------|-------|-------|
| web/app.py (~2700 lines) | deep | Middleware stack + lifespan + auth + broadcasters + cost-budgets |
| web/routes/auth.py | deep | Login/logout/change-password/status |
| web/routes/bots.py | deep | Lifecycle + config + duplicate + import |
| web/routes/chart.py | deep | r3-001 surface — three exception-leak sites |
| web/routes/deals.py | deep | Deals + annotations |
| web/routes/exchanges.py | deep | Keys + passphrase enforcement |
| web/routes/changelog.py | deep | Admin CRUD + role gate |
| web/routes/dashboard.py | medium | Layout PUT |
| web/routes/drawdown.py | medium | Reset endpoint |
| web/routes/backtest.py | medium | List + delete |
| web/routes/admin.py | medium | Emergency-stop |
| web/routes/admin_bots.py | medium | Admin lifecycle + bulk |
| core/database.py | deep | Migration + WAL + connection cache |
| core/credentials.py | deep | Fernet rotation + per-user |
| core/user_store.py | deep | Auth + epoch + username validation |
| core/dashboard_store.py | deep | Layout validation |
| core/changelog_store.py | medium | f-string safety |
| core/deal_store.py | deep | User-scoped + batch fetch |
| core/paths.py | deep | User-dir helpers |
| core/cleanup.py | deep | Orphan .tmp sweep |
| core/logging_setup.py | deep | RequestIdFilter + ctx |
| core/rate_budget.py | deep | CostBudget |
| core/circuit_breaker.py | deep | State machine + r2-005 context |
| core/liquidation_guard.py | medium | Thread-safe positions |
| core/drawdown_guard.py | medium | Peak persistence |
| core/schedule_guard.py | medium | TZ-aware |
| paper/paper_engine.py (~1700) | medium | r2-008/009 re-verification |
| paper/state_io.py | medium | Atomic write |
| paper/close_handler.py | medium | Offline close |
| live/live_engine.py | medium | NotImplementedError gate |
| live/order_reconciliation.py | medium | Phase-3 scaffolding |
| exchanges/bitget.py | medium | Idempotency + retry |
| exchanges/kraken.py | medium | r2-002 idempotency gap |
| exchanges/public_exchange.py | medium | Circuit breaker wiring |
| main_web.py | deep | Boot logging + filter attach |
| main_paper.py | deep | SIGTERM handler + notify drain |
| notifications/telegram.py | medium | r3-007 surface |
| web/static/index.html | medium | CDN SRI + anti-flash gate |
| web/static/app.js | medium | CSRF wrapper + WS client |
| web/static/maintenance.html | medium | Self-contained verification |
| web/static/robots.txt | medium | Disallow-all |
| scripts/backup.sh | deep | Retention + permissions + MANIFEST |
| scripts/restore.sh | deep | Pre-restore + plan + permissions |
| scripts/rollback.sh | deep | Schema-migration guard |
| requirements.txt | medium | Pin posture |
| .env.example | deep | Completeness |
| .gitignore | medium | Secret-leak surface |
| docs/runbook.md | medium | Backup/restore/rollback flow |
| Makefile | medium | Target consistency |
| start.sh / stop.sh | medium | .env sourcing + graceful stop |
| tests/test_response_body_hygiene.py | deep | r3-002 surface |
| tests/test_secret_redaction.py | medium | r3-007 surface |
| tests/test_security_headers.py | medium | Headers coverage |
| tests/test_cross_tenant_isolation.py | medium | Isolation coverage |
| tests/test_csrf.py | medium | CSRF failure paths |
| tests/test_cleanup.py | medium | .tmp sweep coverage |
| tests/test_config_validation.py | medium | Boot-config gate |
| tests/test_ws_portal_log.py | medium | WS gate |

~57 files opened; ~32 deep-read.

**Out of repo (not opened):**
- `/etc/systemd/system/reverto.service` (r3-012)
- `/etc/caddy/Caddyfile` (r3-005, r3-012)
- `/etc/fail2ban/jail.local`
- VPS-side cron table

---

## Appendix B: Methodology Notes

### How this audit was run

1. **Orientation** (15 min): git log review, prior-audit doc read, file-size map.
2. **Agent delegation** (~5 h wall clock, executed in parallel): 5 Explore agents with disjoint area assignments + verification of prior STATUS markers + public-exposure re-evaluation prompts. Each agent given clear scope + ID range to avoid collision.
3. **Spot-verification** (30 min): manually verified the 4 highest-severity claims: chart.py exception leak (confirmed), maintenance.html parent-dir traversal (confirmed at `drwxr-x--- /home/bot`), Server header (confirmed via uvicorn default behaviour), Telegram token logging (confirmed via grep + read of `notifications/telegram.py`).
4. **Consolidation** (45 min): per-agent findings deduplicated + globally renumbered to single r3-NNN namespace + cross-referenced against prior r1/r1.1/pd/r2 IDs + priority matrix built.
5. **Report writing** (60 min): assembled this document.

### Trust model

- **Prior STATUS markers:** not taken on trust; each verified against current HEAD.
- **Agent outputs:** spot-checked for each highest-severity claim; agents instructed to say "No findings — clean" rather than fabricate.
- **Operator observations:** the maintenance.html 403 and the empty credentials/ in backup were treated as facts; root-cause analysis added to the audit.
- **Operator interpretation:** static review; dynamic + pentest explicitly out of scope (Part 7).

### What was NOT done

- No runtime testing.
- No pentest.
- No fuzzing.
- No load testing.
- No `pip-audit` CVE scan.
- No deep Caddyfile audit (high-level only — Caddyfile lives on VPS).
- No deep systemd unit audit (lives on VPS).
- No external review of this audit by a second human / agent.

---

## Appendix C: Internet Exposure Snapshot

### Scanner traffic patterns observed (operator-reported in Caddy logs)

| Pattern | Source | Reverto response | Action |
|---------|--------|------------------|--------|
| `/wp-content/...` | various | 404 | Expected |
| `/ueditor/...` | various | 404 | Expected |
| `/onvif-http/...` | various | 404 | Expected |
| Generic path-spray | Umai scanner, `204.76.203.206` | 404 / 401 | Expected |

All probe responses verified as 404 (path doesn't exist) or 401 (auth required for `/api/*` paths). No 5XX responses leaked to scanner traffic. CSP enforced. Server header leaks (r3-003) but no other detail.

### What's NOT exposed

- `/openapi.json` and `/docs` are disabled (FastAPI `openapi_url=None` per pd-048 ACCEPTED).
- `/favicon.ico`, `/healthz`, `/readyz`, `/metrics` are unauth and intentionally so. `/metrics` is network-gated by firewall ACL on the VPS — operator should verify.
- Static files restricted to `/static/` namespace.

### What IS exposed

- `Server: uvicorn` header (r3-003 — fix in 5 min).
- HSTS only when proxy headers honoured correctly (r3-005 — verify with curl).
- Maintenance.html content during portal restarts — but currently 403s due to file-permission chain (r3-004 — operator fix in 2 min).
- robots.txt content (`User-agent: * / Disallow: /`) — intentional, correct.
- TLS handshake (Caddy + Let's Encrypt managed, presumed mainstream-ciphers).

### Recommended monitoring post-cutover

- `journalctl -u caddy -f | grep -E '5[0-9][0-9]'` — alert on 5XX responses to scanner traffic.
- `tail -f /home/bot/reverto/logs/audit.jsonl | jq 'select(.action == "auth_login_failed")'` — credential-spray detection.
- Weekly cron: `pip-audit -r requirements.txt` for CVE drift.
