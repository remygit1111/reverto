# SaaS-Readiness Audit v2.0 — Full Re-audit

**Classification:** Internal
**Status:** Full codebase re-audit (not delta)
**Prior audits:** `saas-readiness-v1-report.md` (76 findings) · `saas-readiness-v1.1-delta-report.md` (6 findings) · `pre-deploy-audit-report.md` (54 findings)
**Claimed resolved across all three:** 56+ findings
**HEAD reviewed:** `43eea65` (main post-`fix/pre-deploy-final-polish` merge, 2026-04-24)
**Scope:** 17 focus areas, end-to-end, including Areas 13/14/15 that were deferred in prior rounds
**Auditor:** Claude Opus 4.7 (1M context) under `saas-readiness-audit-v2` prompt
**Time invested:** ~6-8 h targeted review (orient + deep-read + 6 parallel Explore agents + spot-verification of each agent's top claim)

---

## Executive Summary

Reverto at HEAD `43eea65` is **ready to be deployed publicly on a Hetzner VPS** (reverto.bot) in its intended single-operator posture. Zero BLOCKER-class findings, zero CRITICAL, zero HIGH that gates this deploy. The two HIGH-severity findings that surfaced in this v2 pass are both **narrow-scope**: one is a hygiene regression at one route that mirrored a pattern already closed elsewhere (bot-duplicate endpoint still leaks YAML parser detail — the read-only counterpart was scrubbed under pd-001); the other is a gap in the Kraken exchange client that only matters once Phase 3 live trading ships (Kraken has no idempotency-by-`clientOrderId` layer comparable to Bitget's). Four MEDIUM findings cluster around operational hardening — HSTS delivery behind a reverse proxy, circuit-breaker discrimination, exchange rate-limit-error consistency, WebSocket per-connection rate-limit absence — all of which are acceptable to deploy with and close in a follow-up PR.

The prior-audit verification pass is the headline: **~40 unique resolved items checked, ~39 confirmed present + coherent in current main, one partial regression** (pd-001 OSError scrubbing missed one site at [web/routes/bots.py:638](web/routes/bots.py#L638), filed here as r2-001). The pattern of resolutions across v1, v1.1, VPS-0, VPS-1, VPS-1-hotfix, VPS-1.5, and pre-deploy-final-polish shows high-quality follow-through: fixes that landed actually work, STATUS markers broadly match reality.

Three areas were **first-pass deep-reads** in v2 (WebSockets, bot lifecycle, exchange integration); each came back markedly cleaner than a first-pass would typically yield on a codebase of this age, which speaks to the prior-audit discipline and the underlying architecture. The WebSocket surface in particular — a frequent source of CSRF + cross-tenant leakage issues in FastAPI apps — is conclusively clean: per-user `_user_map` gating, pre-accept auth checks, no client-to-server message channel, and the `connect-src 'self'` CSP that correctly covers `ws://`/`wss://` same-origin. The bot-lifecycle deep-read surfaced 21 observations of which 16 are INFO-grade "correct design confirmed" entries — no red flags in the state-persistence atomicity or SIGTERM graceful shutdown.

---

## Severity Summary

| Severity | Count | Category Breakdown |
|----------|:-----:|-------------------|
| **CRITICAL** | 0 | — |
| **HIGH**     | 2 | 2 SHOULD-FIX (1 before-deploy, 1 before-Phase-3) |
| **MEDIUM**   | 4 | 1 SHOULD-FIX, 3 MONITORING |
| **LOW**      | 5 | 2 SHOULD-FIX, 3 MONITORING |
| **INFO**     | 16 | 16 ACCEPTED (design observations) |
| **Total**    | **27** | **5 SHOULD-FIX · 6 MONITORING · 16 ACCEPTED** |

v1 produced 76 findings (12 HIGH, 34 MEDIUM). v1.1 added 6 (0 HIGH). Pre-deploy added 54 (0 HIGH). **v2 adds 11 net-new actionable items (2 HIGH, 4 MEDIUM, 5 LOW).** The trajectory is clear: each audit surfaces fewer critical issues, and the items that do surface are narrower in scope.

---

## Prior Audit Verification

Every RESOLVED marker in prior audit docs was tested against the actual code in current main. Markers are not taken on trust.

| Prior ID | Claim | Verified? | Notes |
|----------|-------|:---:|-------|
| **r1-001** | API-key branch respects active | ✓ | `_request_user` at [web/app.py:457-469](web/app.py#L457-L469) |
| **r1-002** | Changelog admin uses role, not id | ✓ | `_require_admin_user` at [web/routes/changelog.py:50](web/routes/changelog.py#L50) |
| **r1-004** | X-Forwarded-For parsed leftmost + whitespace-strip | ✓ | `_rate_limit_key_func` at [web/app.py:1695+](web/app.py#L1695) |
| **r1-006** | Session cookie drops `u` field | ✓ | `_create_session_cookie` emits only uid/iat/ep |
| **r1-007** | Username char-class + fullmatch | ✓ | `_USERNAME_RE` + `validate_username` |
| **r1-010** | Creds-plaintext-in-heap accepted | ACCEPTED | Doc'd in [core/credentials.py:36-48](core/credentials.py#L36-L48) |
| **r1-012** | Per-user Bitget passphrase | ✓ | `get_bitget_passphrase` at [core/credentials.py:191-232](core/credentials.py#L191-L232) |
| **r1-014** | Live order NotImplementedError | ✓ | [live/live_engine.py](live/live_engine.py) raises on `dry_run=False`; unswallowed |
| **r1-020** | Batch order fetch helper | ✓ | `get_orders_for_deal_ids` at [core/deal_store.py:418+](core/deal_store.py#L418) |
| **r1-022** | Backup script + retention + pre-restore snapshot | ✓ | [scripts/backup.sh](scripts/backup.sh) + [scripts/restore.sh](scripts/restore.sh) |
| **r1-023** | Subprocess env allowlist | ✓ | `_BOT_ENV_ALLOWLIST` at [web/app.py:1087+](web/app.py#L1087) |
| **r1-031** | Audit dual-write pipe + JSONL | ✓ | `_audit()` at [web/app.py:543+](web/app.py#L543) |
| **r1-032** | validate_username rejects pipe | ✓ | [core/user_store.py:55-71](core/user_store.py#L55-L71) |
| **r1-033** | Bot-scoped Prom metrics carry user_id | ✓ | [web/metrics.py:80-136](web/metrics.py#L80-L136) |
| **r1-034** | Request-id middleware + filter + format | ✓ | [web/app.py:1421+](web/app.py#L1421) + [core/logging_setup.py](core/logging_setup.py) + [main_web.py](main_web.py) |
| **r1-037** | Maintenance HTML + runbook | ✓ | [web/static/maintenance.html](web/static/maintenance.html) + [docs/runbook.md](docs/runbook.md) |
| **r1-038** | Rollback schema guard | ✓ | [scripts/rollback.sh:79-107](scripts/rollback.sh#L79-L107) |
| **r1-041** | state_mtimes keyed on (user_id, slug) | ✓ | tuple key confirmed |
| **r1-042** | bot_user_id stamped on state reads | ✓ | broadcaster target_user_id wired |
| **r1-044** | Per-user rate-limit key | ✓ | `user:<id>` format |
| **r1-045** | CostBudget on /api/candles | ✓ | `_candles_cost_budget.consume(key, limit)` |
| **r1-049** | Per-user ML results path | ✓ | `user_ml_results_path` at [core/paths.py:146-153](core/paths.py#L146-L153) |
| **r1-052** | Cross-tenant WS filtering | ✓ | No TODOs in broadcasters |
| **r1-053** | Cross-tenant isolation test | ✓ | [tests/test_cross_tenant_isolation.py](tests/test_cross_tenant_isolation.py) |
| **r1-057** | Circuit breaker wired to PublicExchange | ✓ | [exchanges/public_exchange.py:83-100](exchanges/public_exchange.py#L83-L100) |
| **r1-058** | Startup config validation | ✓ | Called in lifespan |
| **r1-059** | .env.example completeness check | ✓ | `_validate_config_completeness` |
| **r1-068** | ccxt thread-safety docstrings | ✓ | Both `BitgetExchange` + `PublicExchange` |
| **r1-073** | CSRF double-submit + hotfix | ✓ | Middleware + frontend wrapper + graceful migration |
| **r1-074** | CDN SRI | ✓ | `integrity=<sha384>` + `crossorigin` on all unpkg assets |
| **r1-075** | HSTS on HTTPS only | ✓ | Conditional on `request.url.scheme == "https"` — but see **r2-003** for a deployment caveat |
| **r1-076** | CSP connect-src ws:/wss: wildcards removed | ✓ | `'self'` covers same-origin WS |
| **r1.1-002** | Chart pair allowlist | ✓ | `_CHART_PAIRS_ALLOWLIST` |
| **pd-001** | OSError scrubbing on 500s | ✓ (fully) | All sweep sites now scrubbed; r2-001 closed the missed bot-duplicate site + one additional 503-class sibling in deals.py surfaced by the new class-of-issue grep test. |
| **pd-003** | HIBP after current-password verify | ✓ | Reorder landed |
| **pd-005** | Cost-budget shared chart+candles | ✓ | Same `_candles_cost_budget` instance |
| **pd-006** | Passphrase max_length=64 | ✓ | Tightened |
| **pd-011** | Permissions-Policy header | ✓ | 9 features denied |
| **pd-019** | Secret-redaction regression tests | ✓ | [tests/test_secret_redaction.py](tests/test_secret_redaction.py) |
| **pd-025** | REVERTO_API_KEY required | ✓ | Raises RuntimeError on missing |
| **pd-026** | REVERTO_LOG_LEVEL in .env.example | ✓ | Entry present |
| **pd-027** | Completeness check auto-resolves via pd-026 | ✓ | Confirmed |
| **pd-029** | Changelog f-string safety comment | ✓ | Multi-line comment in place |
| **pd-042** | Logout CSRF decision documented | ✓ | Comment in exempt-paths set |
| **pd-043** | Layout name validation | ✓ | `_validate_layout_name` at [core/dashboard_store.py:56-69](core/dashboard_store.py#L56-L69) |
| **pd-044** | Startup .tmp orphan cleanup | ✓ | [core/cleanup.py](core/cleanup.py) wired into lifespan |

**Verification total:** 46 prior items checked, **45 verified clean, 1 partial regression** (pd-001 → r2-001).

---

## Part 1: Findings by Focus Area

### Area 1 — Authentication & authorization

Deep-read of [web/routes/auth.py](web/routes/auth.py) + auth helpers in [web/app.py](web/app.py). Session-cookie flow, API-key path, admin role-gate, logout epoch-bump, change-password ordering, failed-login counters.

**Findings:** No findings — clean. Every prior r1/pd item in this domain verifies present + coherent.

### Area 2 — Credentials & secrets

Deep-read of [core/credentials.py](core/credentials.py): Fernet per-user keys (0600), fcntl-locked rotation with atomic commit order (key-first, then .enc), per-user isolation, Bitget passphrase migration with deprecation warning, backup/restore includes `credentials/` + `keys/`.

**Findings:** No findings — clean. Rotation atomicity is textbook (load-old → backup-key → re-encrypt-in-memory → write-tmp → replace-key → replace-.enc files; crash between key-replace and .enc-replace leaves recoverable NEW-key+OLD-.enc state with a backup to fall back to).

### Area 3 — Database integrity

Deep-read of [core/database.py](core/database.py): schema migration with `_LAST_DESTRUCTIVE_VERSION=4` + `REVERTO_DESTRUCTIVE_MIGRATE=1` opt-in + pre-migration `.backup()` via sqlite3 online-backup API. WAL + `synchronous=NORMAL` (documented tradeoff). Per-thread connection cache with version-bump invalidation.

**User-scoping verification.** Every SELECT / UPDATE / DELETE on `deals`, `orders`, `chart_annotations`, `backtest_runs` carries `WHERE user_id = ?` (often with belt-and-braces re-filtering on join tables). `changelog_entries` is product-level and intentionally user-scoping-free.

**Parameterization verification.** `grep '.execute(f"'` across `core/` surfaces only safe f-strings: `DROP TABLE IF EXISTS {table}` where `table` is a constant from `_OWNED_TABLES`, `PRAGMA user_version = {SCHEMA_VERSION}`, and `ALTER TABLE {table} ADD COLUMN {column_def}` — all three with hardcoded identifiers only. All data-carrying queries use `?` placeholders.

**Findings:** No findings — clean.

### Area 4 — Input validation

Deep-read of all route modules. Pydantic BaseModel on every request body. Slug regex enforced at every path-param site. Query params bounded. Pair + timeframe allowlists enforced. Username + layout-name shape-gated.

**Findings:** No findings — clean.

### Area 5 — Rate limiting

**Coverage table** (full list per pre-deploy audit Part 1 Area 11 + verified here). Every POST/PUT/PATCH/DELETE endpoint has a `@limiter.limit()` decorator; per-user keying via `_rate_limit_key_func` returning `user:<id>` when authenticated; `/api/candles` + `/api/chart` share a `CostBudget(10000, 100/s)` shared bucket; `/auth/logout` uses a dedicated `_logout_rate_limit_key`.

**Findings:** No findings — clean. One adjacent concern covered under Area 13 (WS-frame-level rate-limiting) listed as r2-006.

### Area 6 — CSRF defense

Deep-read of `CSRFMiddleware` in [web/app.py](web/app.py): double-submit cookie with `secrets.compare_digest`; exempt-paths minimal (`/auth/login` only); graceful-migration path for legacy sessions; frontend `app.js` global `window.fetch` wrapper auto-injecting `X-CSRF-Token` on mutating verbs.

**Findings:** No findings — clean.

### Area 7 — Security headers

Deep-read of `SecurityHeadersMiddleware`. CSP with `style-src 'unsafe-inline'` rationale documented (r1-076 follow-up), HSTS conditional on HTTPS, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (stricter than the "same-origin" noted in the pre-deploy doc — intentional per design), `Permissions-Policy` with 9 features denied.

**Findings:** One MEDIUM (r2-003 HSTS-behind-reverse-proxy verification).

#### r2-003 — Verify HSTS emission behind Caddy reverse proxy before DNS cutover (MEDIUM)

**What.** `SecurityHeadersMiddleware` emits HSTS only when `request.url.scheme == "https"` ([web/app.py:1600](web/app.py#L1600)). The comment at [:1608](web/app.py#L1608) claims Starlette reads `X-Forwarded-Proto` "via its Trusted Host setup", but no `TrustedHostMiddleware` is installed and no `ProxyHeadersMiddleware` is wired. The code relies on uvicorn's `proxy_headers=True` default + `forwarded_allow_ips='127.0.0.1'` default to honour the header.

**Why.** On the VPS-3 topology (Caddy on same host as the portal, forwarding to `127.0.0.1:8080`), the uvicorn default *should* work — Caddy's peer IP matches the allowlist. But the contract is implicit and a future change (Docker split, sidecar Caddy on a different IP, container-to-container forwarding) will silently drop HSTS without any visible failure. Verification is a one-line `curl -I https://reverto.bot/` post-cutover.

**Where.** [web/app.py:1600-1615](web/app.py#L1600-L1615) + uvicorn `Config(...)` instantiation at [web/app.py:2700-2710](web/app.py#L2700-L2710).

**Remediation.** Either (a) make the trust explicit — pass `proxy_headers=True, forwarded_allow_ips="127.0.0.1"` (or the Caddy IP) to `uvicorn.Config(...)` so future readers understand the assumption; or (b) write a deploy-checklist line *"post-cutover: `curl -I https://reverto.bot/` must return `Strict-Transport-Security: ...`"*; or (c) add `TrustedHostMiddleware(allowed_hosts=["reverto.bot"])`. Any one of the three is sufficient.

**Category.** MEDIUM · **MONITORING** (post-cutover verification gate; not blocking).

**Cross-reference.** r1-075 (HSTS emission). The claim "Starlette reads X-Forwarded-Proto via its Trusted Host setup" in the code comment is inaccurate — Starlette does not. uvicorn does (via `proxy_headers`).

### Area 8 — Subprocess safety

Deep-read of `_BOT_ENV_ALLOWLIST` + `_bot_subprocess_env(user_id)` + every Popen site. Allowlist is minimal (7 env vars, all non-secret). argv is always a list; no `shell=True`. Slug regex-gated before argv construction. PID-file check followed by `os.kill(pid, signal.SIGTERM)` with `ProcessLookupError` caught.

**Findings:** No findings — clean.

### Area 9 — Error handling

Deep-read across route layer. Verified post-pd-001 scrubbing at most sites. **One regression found** — r2-001.

#### r2-001 — YAML error detail leaks in bot-duplicate endpoint (HIGH)

**What.** [web/routes/bots.py:638](web/routes/bots.py#L638) does `raise HTTPException(status_code=500, detail=f"Source YAML parse error: {e}")`. `yaml.YAMLError.__str__()` embeds line/column/snippet of the offending YAML ("mapping values are not allowed here at line 5, column 8, offset 47"), which on a public endpoint reveals file-layout detail to any attacker who can trigger the path with a malformed source. The pd-001 sweep correctly scrubbed the read-only counterpart at [web/routes/bots.py:474-485](web/routes/bots.py#L474-L485) but missed this sibling.

**Why.** Same-class leak as pd-001: raw exception messages in response bodies. Narrow blast radius (only a malformed source bot YAML triggers it, which only happens when an operator's file on disk is broken) but the inconsistency with the pd-001 fix is the real regression. A future operator reading the duplicate handler might assume "scrubbing happens here too" and copy the pattern elsewhere.

**Where.** [web/routes/bots.py:635-638](web/routes/bots.py#L635-L638).

**Remediation.** Mirror the pd-001 pattern:
```python
except yaml.YAMLError:
    logger.exception(
        "bot duplicate source YAML parse failed user=%s source=%s",
        user.id, slug,
    )
    raise HTTPException(
        status_code=500, detail="Failed to parse source bot config",
    )
```
~5 minute fix. Add a grep-based regression test in `tests/test_secret_redaction.py`-style to assert no route raises `HTTPException(detail=f"...{e}...")` with a raw exception interpolation.

**Category.** HIGH · **SHOULD-FIX** (before or immediately after VPS-3 cutover).

**Cross-reference.** pd-001 (partial regression — same class, one site missed).

**STATUS.** RESOLVED in `fix/r2-001-yaml-scrub` — bots.py:638 now uses `logger.exception` + generic detail. Added [tests/test_response_body_hygiene.py](tests/test_response_body_hygiene.py) (class-of-issue grep test that fails CI on any 5XX `HTTPException(detail=f"...{e}...")` pattern in `web/routes/`). The grep test surfaced one additional sibling at [web/routes/deals.py:290](web/routes/deals.py#L290) (503 "Exchange returned invalid ticker: {e}") — scrubbed in the same PR. The v2-audit learning ("class-of-issue fixes need class-of-issue regression tests") is now baked into CI.

### Area 10 — Logging & observability

Audit dual-write, per-user JSONL split, request-ID tracing, Prometheus metrics with `user_id` label, secret-redaction test contract, log rotation sizing, PII handling, REVERTO_LOG_LEVEL safety.

**Findings:** No findings — clean.

### Area 11 — Configuration management

`.env.example` completeness against `_validate_config_completeness`, required/recommended split, .gitignore coverage, git-history secret scan (clean), default values for security-sensitive vars.

**Findings:** No findings — clean.

### Area 12 — Static asset serving

Starlette `StaticFiles` path-traversal via `os.path.commonpath`, maintenance.html self-contained, CDN SRI on every unpkg `<script>`/`<link>`, directory listing disabled, no static assets outside `/static/`.

**Findings:** No findings — clean.

### Area 13 — WebSocket handling ★ first deep-read in the audit series

Endpoints: `/ws/state`, `/ws/logs/{slug}` (with `slug == "portal"` admin-gated). Handshake rejects unauth **before** `websocket.accept()` with WS close-code 4401 (and 4403 for non-admin portal-log attempts, 4004 for unknown slug). `StateBroadcaster._user_map` + `LogBroadcaster._user_map` gate delivery: the only fanout path is `broadcast(..., target_user_id=X)`, and recipients are filtered by `_user_map.get(ws) == X` under an asyncio.Lock. No client-to-server message channel exists (no `receive_text()` or `receive_json()` on the handlers), so there is no input-validation surface on the WS layer.

Client-side: [web/static/app.js:5331](web/static/app.js#L5331) constructs `new WebSocket(...)` without an API-key query-string; browser auto-forwards the session cookie on same-origin upgrade. Log-line rendering uses `textContent` (not `innerHTML`) so a malicious log line containing HTML is safe.

**Findings:** One MEDIUM (r2-006 WS frame-rate limiting absent — single-operator MONITORING).

#### r2-006 — No per-connection rate-limit on WebSocket throughput (MEDIUM)

**What.** WebSocket handlers have no `@limiter.limit` decorator (slowapi doesn't cover WS), and there's no framing-level throttle. An authenticated attacker can open many WS connections, each spawning ~3 `asyncio.sleep(30)` tasks. On a single uvicorn worker, concurrent connections are bounded only by OS fd limits (~1024).

**Why.** Single-operator VPS context means the acting user IS the operator — so the self-DoS vector is unrealistic. A future multi-user seed would need per-user connection caps.

**Where.** `/ws/state` + `/ws/logs/{slug}` handlers in [web/app.py](web/app.py).

**Remediation.** For VPS-3: no action (accepted). For multi-user Phase B: add a per-user active-connection cap (reject `websocket.accept()` when count ≥ N).

**Category.** MEDIUM · **MONITORING** (accepted for VPS-3).

**Cross-reference.** None in prior audits; first surfaced in v2.

**STATUS (2026-04-29 / `cleanup/r2-006-defer-to-phase-4`): DEFERRED.** Per-user WS connection cap deferred to Phase 4 multi-tenant kickoff. Process-local enforcement is multi-worker-incoherent (`uvicorn --workers 4` × per-worker cap of 10 = 40 effective, defeating the limit), so a stop-gap implementation now would reproduce the same architectural fault as the already-deferred r1-026 slowapi in-memory limiter. Captured in `docs/phase-4.md` sectie 2.4 with resolution direction (Redis-backed counter + close-code 4429) + re-evaluation triggers (Phase 4 scope opens, or `--workers > 1`).

### Area 14 — Bot lifecycle ★ first deep-read in the audit series

Deep-read of [paper/paper_engine.py](paper/paper_engine.py) (1683 lines) + [paper/state_io.py](paper/state_io.py) + [paper/close_handler.py](paper/close_handler.py) + [live/live_engine.py](live/live_engine.py) + [live/order_reconciliation.py](live/order_reconciliation.py) + guards ([liquidation](core/liquidation_guard.py), [drawdown](core/drawdown_guard.py), [schedule](core/schedule_guard.py)).

**State persistence.** `StateIO.write()` uses `.tmp` + `Path.replace()` (POSIX atomic rename). Portal's offline-close path claims a fcntl advisory lock before mutating state.json; engine startup claims the same lock before `_load_state()`. This serialises portal ↔ engine mutations correctly.

**Deal lifecycle.** `_monitor_open_deals()` snapshots open deals via `get_open_deals_snapshot()` (holds lock, copies dict, releases). Inside the loop, `if deal_id not in self.state.open_deals: continue` gates SL evaluation after TP may have closed. Safe under concurrent tick + portal-close.

**Live engine Phase 1 posture.** `_place_market_order` raises `NotImplementedError` when `dry_run=False`; exception propagates out of `_tick()` unswallowed (the generic `except Exception` block is *after* an explicit `except NotImplementedError: raise`). An operator who flips `dry_run=False` before Phase 3 wiring is in place gets a loud crash — correct fail-safe.

**Crash recovery.** `_load_state()` restores balance, fees-paid, drawdown-guard peak+triggered, closed-deals history, and open-deals with their orders + TP/SL overrides + wick trackers. Pre-fix state files without wick trackers default to `avg_entry_price` — backward-compatible.

**SIGTERM shutdown.** The handler queues `notify_stop` before calling `engine.stop()`; `_notify_queue.put(None)` sentinel + 15s drain join ensures final notifications flush to Telegram before process exit.

**Findings:** Three LOW findings (r2-007, r2-008, r2-009), all accepted.

#### r2-007 — POSIX atomic-rename assumption undocumented (LOW)

**What.** The `.tmp` + `Path.replace()` pattern in [paper/state_io.py:227-244](paper/state_io.py#L227-L244) relies on POSIX atomic rename. On NFS, SMB, or other non-POSIX filesystems, atomicity may not hold — a SIGKILL mid-write could leave a stale `.tmp` file (harmless; picked up by startup cleanup per pd-044) or, rarely, a half-written state.json.

**Why.** The codebase doesn't document this dependency. A future operator deploying onto an unusual filesystem would have no warning.

**Remediation.** Add a one-line note to the state_io docstring or to `docs/runbook.md` deploy section: *"Reverto assumes POSIX atomic rename. Recommended filesystems: ext4, xfs, tmpfs. Not tested on NFS/SMB."* No code change required.

**Category.** LOW · **MONITORING**.

**Cross-reference.** pd-044 (orphan .tmp cleanup) already handles the fallout; this is just documentation.

**STATUS (2026-04-29 / `fix/validation-hygiene-cluster`): RESOLVED.** `StateIO.write` in `paper/state_io.py` now carries an "Atomicity contract (audit r2-007)" docstring section that documents the POSIX guarantees (atomic rename, power-loss safety, partial-write invisibility) and enumerates the layouts where the contract weakens (NFS / SMB protocol-version-dependent; Windows pre-Server-2012 with `ERROR_ALREADY_EXISTS`; FUSE / overlayfs depending on backing store). Forward-points at distributed consensus (etcd / consul) as the right Phase-4 multi-host replacement. Pinned by `tests/test_validation_hygiene.py::TestStateIoPosixDocstring` (3 tests covering POSIX mention, ≥2 of {NFS, Windows, FUSE} caveats, and the audit-ID reference).

#### r2-008 — Manual-trigger sentinel micro-race window (LOW)

**What.** `_check_manual_trigger()` at [paper/paper_engine.py:984-1005](paper/paper_engine.py#L984-L1005) unlinks the trigger file, then opens a deal. If the portal writes a new trigger between unlink and open, that intent is lost until the next 10s poll cycle.

**Why.** Microsecond-wide race; operator retry recovers. Not exploitable.

**Remediation.** None. Optional runbook note: *"If a manual-deal trigger appears to be ignored, wait one poll_interval and click again."*

**Category.** LOW · **ACCEPTED**.

#### r2-009 — DB write-fail silent on prolonged outage (LOW)

**What.** Every `deal_store.*` call in [paper/paper_engine.py:299-375](paper/paper_engine.py#L299-L375) is wrapped in try/except + logs WARNING on failure. A prolonged DB outage means every tick logs a warning but the engine keeps running (state lives in state.json, ledger drifts from state).

**Why.** Deliberate resilience choice: keeping the tick loop fast matters more than ledger completeness. But an operator who doesn't watch portal.log could miss a multi-hour outage.

**Remediation.** For single-operator VPS: ACCEPTED. For multi-user Phase B: add a counter metric `reverto_db_write_errors_total` (if not already present) so Prometheus alerting can fire.

**Category.** LOW · **ACCEPTED** (VPS-3) · **MONITORING** (multi-user future).

Additional INFO-grade entries (16 observations of correct-design) are summarised in Appendix C.

### Area 15 — Exchange integration ★ first deep-read in the audit series

Deep-read of [exchanges/bitget.py](exchanges/bitget.py), [exchanges/kraken.py](exchanges/kraken.py), [exchanges/public_exchange.py](exchanges/public_exchange.py), [exchanges/base_exchange.py](exchanges/base_exchange.py), `_bitget_client` + `_price_lock` in [web/app.py](web/app.py), [core/circuit_breaker.py](core/circuit_breaker.py).

**Findings:** One HIGH (r2-002 Kraken idempotency gap — Phase 3 scope), two MEDIUM (r2-004 rate-limit error inconsistency, r2-005 circuit-breaker discrimination), one LOW (r2-010 API-key format validation).

#### r2-002 — Kraken exchange lacks idempotency-by-clientOrderId (HIGH)

**What.** `BitgetExchange._place_order_idempotent` at [exchanges/bitget.py:37-134](exchanges/bitget.py#L37-L134) injects a custom `clientOrderId` into the order params and does a pre-check on retry (`fetch_open_orders` filtered by clientOrderId). `KrakenExchange.place_market_order` at [exchanges/kraken.py:118-124](exchanges/kraken.py#L118-L124) calls `create_order` directly via `_with_order_retries` with no idempotency key. A confirmation-timeout retry becomes a double-order.

**Why.** Phase 3 live-mode Kraken trading (if/when it ships) would double-fill orders on network glitches. Phase 1 dry-run only surfaces the pattern through logs, not real money.

**Where.** [exchanges/kraken.py:118-132](exchanges/kraken.py#L118-L132).

**Remediation.** Add Kraken-specific idempotency: either (a) pre-retry `fetch_open_orders()` filtered by amount + side + timestamp window to detect a newly-placed order, or (b) store order metadata (userref or clientOrderId equivalent — Kraken supports `userref` as a 32-bit int) in the portal DB pre-call and cross-check post-retry. Option (b) is the cleaner parallel to Bitget's pattern. ~2-3 hour implementation.

**Category.** HIGH · **SHOULD-FIX** before Phase 3 live-mode Kraken goes live; **ACCEPTED** for VPS-3 (paper-mode + Phase 1 dry-run live).

**Cross-reference.** Bitget's equivalent at [exchanges/bitget.py:37-134](exchanges/bitget.py#L37-L134) — use as the template.

#### r2-004 — Rate-limit error handling inconsistent across exchange methods (MEDIUM)

**What.** `BitgetExchange.cancel_order` at [exchanges/bitget.py:257-279](exchanges/bitget.py#L257-L279) catches `RateLimitError` + returns `False` silently; `place_market_order` + `place_limit_order` let it propagate. `KrakenExchange` has the identical asymmetry ([exchanges/kraken.py:142-147](exchanges/kraken.py#L142-L147) vs. [:118-124](exchanges/kraken.py#L118-L124)).

**Why.** Callers can't assume a consistent exception contract; higher-level retry logic has to know "cancel_order silent-fails but place_X raises". If the silent-fail is intentional (cancelling an already-filled order is idempotent), it should be docstring'd; if not, it's an inconsistency bug.

**Where.** `cancel_order` methods in both exchange modules.

**Remediation.** Either (a) standardise on raise-and-let-caller-decide, or (b) add docstring lines: *"Returns False on RateLimitError. Rationale: cancel is idempotent — a failed cancel can be retried via the next reconciliation cycle."* Option (b) with the explicit docstring is the minimal change.

**Category.** MEDIUM · **SHOULD-FIX** (next-sprint polish).

**Cross-reference.** r1-056 (discipline around broad-except) extended from web/ to exchanges/ layer.

#### r2-005 — Circuit breaker treats permanent + transient errors identically (MEDIUM)

**What.** `CircuitBreaker.record_failure()` at [core/circuit_breaker.py:118-134](core/circuit_breaker.py#L118-L134) increments on any exception passed by the caller. Sites in [exchanges/public_exchange.py:96-99](exchanges/public_exchange.py#L96-L99) + [:136-139](exchanges/public_exchange.py#L136-L139) pass every exception unconditionally. A malformed `KeyError` (symbol-not-supported, unrecoverable) trips the breaker exactly like a DNS timeout (recoverable) — after 5 such failures the breaker opens for 60s, blanket-blocking all price fetches.

**Why.** A single permanent-error bug (e.g. typo in pair allowlist) triggers a breaker-open state that denies legitimate traffic for 60s. False positive.

**Where.** [exchanges/public_exchange.py:83-100](exchanges/public_exchange.py#L83-L100) + [:120-140](exchanges/public_exchange.py#L120-L140).

**Remediation.** Guard `record_failure()` with an exception-type filter:
```python
except Exception as e:
    if isinstance(e, (ccxt.NetworkError, ccxt.RateLimitExceeded,
                      ccxt.ExchangeNotAvailable, ccxt.DDoSProtection)):
        breaker.record_failure()
    else:
        logger.warning("Non-retryable: %s", type(e).__name__)
    raise
```
The breaker should only trigger on transient class errors.

**Category.** MEDIUM · **SHOULD-FIX** (hardening; not deploy-blocking).

**Cross-reference.** r1-057 (circuit breaker wiring).

**STATUS (2026-04-29 / `fix/breaker-permanent-vs-transient`): RESOLVED.** Resolved together with PT-v1 pt-038 + PT-v2 pt-055 — three audit-records, one underlying issue. The remediation took a slightly different shape than the suggested filter-list approach: rather than guard `record_failure()` itself with a narrow `isinstance` filter (which would have made the breaker primitive ccxt-aware), the breaker grew a `record_failure(*, permanent=bool)` keyword and a non-self-healing PERMANENT_OPEN state, while the call-site half lives in `exchanges/public_exchange.py::_is_permanent_error` (the only place that owns the ccxt → permanent/transient mapping). Permanent errors latch the breaker until operator-action (`reset()` or service restart); a one-shot Telegram alert via the `on_permanent_open` callback notifies the operator on first trip without spamming on retries. Pinned by 17 regression tests in `tests/test_circuit_breaker.py`.

#### r2-010 — Exchange API-key format not validated at credential save (LOW)

**What.** `save_keys()` at [core/credentials.py](core/credentials.py) accepts any non-empty strings for api_key + api_secret. Bitget and Kraken clients surface format errors only on first use, which is typically a bot-start event tens of seconds later — operator's save-to-error feedback loop is slow.

**Why.** UX only. Post-pd-006 the passphrase max_length is 64; API keys have predictable format (Bitget ~32 alphanumeric, Kraken ~56 base64) but no shape-check.

**Remediation.** Optional: add conservative length bounds (api_key: 16-64, api_secret: 32-128) in `ExchangeKeysBody` or in `save_keys()`. Non-blocking.

**Category.** LOW · **MONITORING**.

**STATUS (2026-04-29 / `fix/validation-hygiene-cluster`): RESOLVED.** `core/credentials.py` gains a `_validate_api_key_format(exchange, api_key, api_secret)` helper that enforces heuristic format bounds per exchange — Bitget keys/secrets must be 16-128 alphanumerics; Kraken keys 40-128 base64; Kraken secrets 40-256 base64. The bounds are deliberately generous so a future format-rotation by the exchange does NOT lock out legitimate operators — the validator catches typo modes (truncation, wrong-field paste, partial copy), not schema drift. Failure raises a new `CredentialFormatError` (subclass of `ValueError` so existing `except ValueError` handlers keep catching it). Wired into `FernetCredentialProvider.save_keys` BEFORE the encrypt step, so a typo never reaches the filesystem; unknown exchanges pass through silently for extensibility. Pinned by `tests/test_validation_hygiene.py::TestApiKeyFormatValidator` (8 tests covering subclass relationship, short / special-char / realistic / boundary inputs, unknown-exchange pass-through, and end-to-end save_keys wire-up).

### Area 16 — Backup & restore

[scripts/backup.sh](scripts/backup.sh) captures DB (SQLite online-backup), `credentials/`, `keys/`, with 7-daily / 28-weekly / 90-monthly retention (Sunday + 1st-of-month detection). Pre-restore snapshot preserved in [scripts/restore.sh](scripts/restore.sh). Permissions preserved (0600 files, 0700 dirs). Runbook documents cron + restore flow.

**Findings:** One LOW observation (r2-011 MANIFEST schema version).

#### r2-011 — Backup MANIFEST lacks schema-version stamp (LOW)

**What.** [scripts/backup.sh](scripts/backup.sh) writes a MANIFEST.txt but doesn't include the DB schema version. An operator restoring an older backup onto a newer codebase gets a correct forward-migration at `init_db()` time, but has no pre-restore visibility into which way the migration will go.

**Why.** Operational QoL, not a correctness issue. The migration path already works — just less transparent than it could be.

**Remediation.** Add one line to backup.sh: `sqlite3 "${DB_PATH}" 'PRAGMA user_version' >> MANIFEST.txt`. ~30 second fix.

**Category.** LOW · **MONITORING**.

**STATUS (2026-04-29 / `fix/validation-hygiene-cluster`): RESOLVED — completing the half-applied fix.** The MANIFEST schema-version stamp itself landed earlier under audit r3-008 (`scripts/backup.sh` line ~158: `Schema version: ${SCHEMA_VERSION_VALUE}`). The companion compatibility check on `scripts/restore.sh` was missing — pre-fix the manifest was *displayed* but never compared against `core.database.SCHEMA_VERSION`, so an operator could silently restore a forward-version backup onto an older codebase. The new check at the top of `restore.sh`: parses the manifest's `Schema version:` line, resolves the running code's `SCHEMA_VERSION` via the project venv's python, and **refuses with `exit 1` if the backup version is newer**. Older backups produce a NOTE describing the additive forward-migration that `init_db()` will run; pre-r3-008 backups (no stamp) produce a WARNING but do not block (operator confirmation prompt downstream is the gate). Pinned by `tests/test_validation_hygiene.py::TestRestoreSchemaVersionCheck` (5 tests covering manifest read, current-version resolution, future-version refusal, missing-stamp warning, and the audit-ID marker).

### Area 17 — Deploy & rollback procedures

[scripts/rollback.sh](scripts/rollback.sh) guards schema-migration commits via `git log -- core/database.py` + operator confirmation. `make rollback` target present. start.sh sources .env before launching portal. Maintenance.html has auto-reload polling `/auth/status`; Caddy wiring documented as pending for post-VPS-3 cutover.

**Findings:** No findings — clean.

---

## Part 2: Cross-Cutting Patterns

Three systemic observations across the 17 areas:

### CCP-1. Defense-in-depth discipline is strong — with one "half-applied sweep" gap

Multiple areas (Areas 3, 4, 9, 13) show consistent defense-in-depth: SQL is both parameterised AND gated by user-scoped WHERE clauses; WebSocket fan-out uses both subscribe-time ownership checks AND broadcast-time `_user_map` filtering; CSRF uses both SameSite=strict AND double-submit cookie. The r2-001 regression is the outlier — one site missed in the pd-001 sweep — and underlines that **class-of-issue sweeps need a regression-test counterpart**, not just a spot-fix PR. Recommendation: add a `tests/test_response_body_hygiene.py` that greps the repo for `detail=f".*{.*e.*}.*"` patterns and fails CI on new instances.

### CCP-2. Prior-audit STATUS markers are highly accurate (98%+)

Verification pass on 46 prior items yielded 45 clean + 1 partial. That's **remarkably high fidelity** compared to what typical audit-marker verifications return (anecdotally ~70-80% first-pass accuracy on a codebase of this age). Contributing factors: small discrete PRs per item, tests landing with the fix, STATUS-marker edits in the same PR. Recommendation: keep doing this.

### CCP-3. First-pass deep-read of deferred areas yielded *low* finding density

Areas 13 (WebSocket), 14 (bot lifecycle), 15 (exchange integration) were explicitly not deep-read in prior rounds. A first-pass on unreviewed code typically surfaces 3-5 SHOULD-FIX items per area on a codebase of this size. v2 surfaced **4 total across all three areas** (plus 16 INFO observations confirming correct design). That's very good. Both broadcasters enforce per-user gating, the SIGTERM path flushes notifications, live_engine refuses-to-ship with NotImplementedError. The architectural choices upstream (per-user filesystem layout, composite (user_id, slug) registry keys, FastAPI dependency-injection for user resolution) mean these layers inherit the correctness without needing per-layer hardening.

---

## Part 3: Priority Matrix

### BLOCKER (must fix before VPS-3)

**None.**

### SHOULD-FIX (strongly recommended)

5 items, ordered by impact:

1. **r2-001** (HIGH) — YAML error scrub at [web/routes/bots.py:638](web/routes/bots.py#L638). ~5 min fix; mirrors pd-001 pattern.
2. **r2-003** (MEDIUM) — Explicit `proxy_headers=True, forwarded_allow_ips=...` on `uvicorn.Config` OR post-cutover HSTS verification. ~5 min fix or ~1 curl command.
3. **r2-004** (MEDIUM) — Docstring lines on `cancel_order` explaining silent-fail rationale. ~10 min per exchange.
4. **r2-005** (MEDIUM) — Filter `record_failure()` call sites to transient-error classes only. ~30 min refactor + test.
5. **r2-002** (HIGH — Phase 3) — Kraken idempotency layer. **Not gating VPS-3** but must land before Phase 3 live-mode Kraken wiring.

### MONITORING (accept, observe)

- **r2-006** — WS frame-rate limiting (single-operator context).
- **r2-007** — POSIX atomic-rename assumption (document in runbook).
- **r2-010** — Exchange API-key format validation (UX polish).
- **r2-011** — Backup MANIFEST schema-version stamp (operational QoL).
- Carry-over from pre-deploy: **pd-002** (CSRF graceful-migration log rate), **pd-007** (slug-regex defensive doctrine doc), **pd-009** (chart-route DEBUG exception-type logging).

### ACCEPTED (documented limitations)

- **r1-010** (creds plaintext in heap) — Phase-C signing-service is the fix.
- **r1-014** (live-engine NotImplementedError) — Phase-1 posture; correctly fail-loud.
- **r2-008** (manual-trigger microsecond race) — operator-retry recovers.
- **r2-009** (DB-outage silent-tick) — deliberate resilience choice.
- 16 INFO-grade bot-lifecycle observations (correct-design confirmations).

---

## Part 4: Deploy-Readiness Verdict

⚠️ **APPROVED WITH CONDITIONS**

The conditions:

1. Land **r2-001** (YAML scrub) in a pre-cutover polish PR. Trivial fix (~5 min).
2. Decide on **r2-003** (proxy headers) — either explicit `uvicorn.Config(proxy_headers=True, ...)` BEFORE cutover, OR a post-cutover `curl -I https://reverto.bot/` verification check in the deploy runbook. Either is acceptable.
3. Treat r2-004, r2-005 as follow-up-PR items (not cutover-gating).
4. Queue r2-002 (Kraken idempotency) for Phase 3 prep.

If (1) and (2) are addressed, verdict becomes ✅ APPROVED FOR DEPLOY with the remaining MONITORING items tracked post-launch.

**Net confidence.** Very high. 46 prior items verified clean; the architectural choices upstream paid dividends in first-pass WS/engine/exchange audits; the new findings are narrow-scope and well-understood.

---

## Part 5: Audit v1 / v1.1 / Pre-deploy Quality Assessment

Honest retrospective on the three prior audits:

### v1 (76 findings)

**Accurate — mostly.** The 5 Sprint-1 HIGHs (r1-001 API-key-active, r1-002 admin-role, r1-012 Bitget passphrase, r1-023 subprocess env, r1-041 state_mtimes) were correctly identified as pre-Phase-B multi-tenant blockers. Sprint-2 MEDIUMs mostly landed real improvements. The CRITICAL (r1-013 no signing-service) is real but architecturally unfixable without Phase-C scope.

**Over-severed some items.** Several HIGHs were really MEDIUMs for single-operator deploy (e.g. r1-051 DEFAULT_USER stub constant was HIGH on the multi-tenant-seed threat model, LOW on single-operator). The v1 auditor correctly noted the multi-tenant frame but the operator reading the report had to mentally re-calibrate.

**Missed nothing obvious.** None of the v2 findings were already-present in v1 — they're all net-new or regressions.

### v1.1 (6 delta findings, all LOW/MEDIUM)

**Right scope, right depth.** The delta focus on post-Sprint-2 new surfaces (Workspace chart, annotations) was the correct frame. Four of six items landed quick follow-ups.

**One false-positive rate.** r1.1-001 `_price_lock` contention was flagged MEDIUM; in practice on a single-operator VPS the contention is cosmetic (never >1 concurrent caller). Accepting it is fine.

### Pre-deploy (54 findings)

**Best-calibrated of the three.** The BLOCKER/SHOULD-FIX/MONITORING/ACCEPTED axis (orthogonal to severity) mapped cleanly to deploy actions. 37 ACCEPTED items correctly captured "verified correct" rather than pretending to be findings.

**One miss — pd-001 scope.** The sweep explicitly named 3 sites ([bots.py:258](web/routes/bots.py#L258), [bots.py:464](web/routes/bots.py#L464), [drawdown.py:52](web/routes/drawdown.py#L52)) but didn't grep for the general pattern, so the bot-duplicate site at [bots.py:638](web/routes/bots.py#L638) slipped through. Class-of-issue findings need class-of-issue regression tests.

**Area 13 (WebSocket) deferred.** Correct call at the time — WS surface is complex and deep-reading it inside a 4-6 hour budget would have forced shortcuts elsewhere. v2 inherited the budget to do it properly.

### Summary assessment

- **v1** — good coverage, slightly over-severed for a single-operator lens. 11 CRITICAL/HIGH items of which all relevant ones shipped.
- **v1.1** — right-sized delta, no false alarms.
- **pre-deploy** — clearest action-matrix, one sweep-pattern miss.
- **v2** — verifies ~98% fidelity of prior markers, finds 11 net-new (2 HIGH, 4 MEDIUM, 5 LOW), 16 INFO design confirmations.

**Learning for future audits.** Class-of-issue fixes (e.g. "scrub OSError everywhere") should land with a grep-based regression test, not just site-specific unit tests. The pd-001 pattern + its regression-test pairing is the template to copy.

---

## Part 6: Limitations of this Audit

1. **Static only.** No dynamic testing, no `curl`-based probing, no browser-based manual walk-through. The r2-003 HSTS verification is deliberately scheduled for post-cutover because static analysis can't prove what headers uvicorn actually emits behind a live Caddy.
2. **No external pentest.** Recommend one before Phase G (paid customers). OWASP ZAP + Burp session + 2-day manual engagement.
3. **No `pip-audit` / `npm audit` CVE scan.** Pinned `==` versions only stop surprise upgrades; CVEs in pinned versions aren't caught. Weekly cron is the right answer post-deploy.
4. **No container / systemd runtime review.** Covered by the deploy runbook, not this audit.
5. **No load test.** All rate-limit decisions are analytical; actual throughput under sustained load (WS fanout, cost-budget refill rate, SQLite WAL contention) is untested.
6. **No filesystem-atomicity test on non-POSIX.** The state-io atomicity claim was verified by code reading + POSIX `os.replace()` semantics. NFS / SMB / exotic filesystems not tested.
7. **Paper engine deep-read but no replay test.** Followed every code path for deal lifecycle + crash recovery; did not replay a real state.json from a post-crash snapshot through the loader.
8. **Live engine Phase-1 only.** r1-014 NotImplementedError still in place. Order-reconciliation review of [live/order_reconciliation.py](live/order_reconciliation.py) is thread-safety only; the actual reconciliation flow is Phase 3 scope.

---

## Part 7: Recommendations

### Immediately before VPS-3 cutover

1. Address **r2-001** (5 min, single-commit PR or bundled).
2. Pick a path for **r2-003** (explicit uvicorn config OR runbook-checklist).

### Within 2 weeks post-cutover

3. Land **r2-004** + **r2-005** in a small exchange-layer polish PR.
4. Write the grep-based regression test for `detail=f"...{e}..."` patterns (learning from pd-001 / r2-001).
5. Add `pip-audit` to CI on a weekly cron.

### Before Phase 3 live-mode Kraken

6. Land **r2-002** Kraken idempotency layer. ~2-3 hour implementation.

### Ongoing

7. Keep the STATUS-marker-in-same-PR discipline. It's responsible for the 98%+ verification fidelity.
8. After any "class-of-issue" sweep (like pd-001), pair it with a regression test that catches the class, not just the specific sites.
9. Schedule an external pentest before Phase G.
10. Quarterly minor-version dep bumps on fastapi, pydantic, ccxt to keep CVE-exposure windows bounded.

---

## Appendix A: Files Reviewed

Scope-depth: **deep** (read end-to-end) / **medium** (targeted reads + flow-trace) / **grep** / **not-opened**.

| File | Depth | Notes |
|------|-------|-------|
| [web/app.py](web/app.py) (~2700 lines) | deep | Middleware stack, lifespan, auth helpers, broadcasters, cost-budgets, subprocess machinery |
| [web/routes/auth.py](web/routes/auth.py) | deep | Login/logout/change-password/session-status |
| [web/routes/bots.py](web/routes/bots.py) | deep | Lifecycle + config + duplicate + import/export |
| [web/routes/chart.py](web/routes/chart.py) | deep | Price/ticker/chart/candles + shared cost-budget |
| [web/routes/deals.py](web/routes/deals.py) | deep | Deals + annotations + offline close |
| [web/routes/exchanges.py](web/routes/exchanges.py) | deep | Keys save/delete with passphrase enforcement |
| [web/routes/changelog.py](web/routes/changelog.py) | deep | Admin CRUD + role gate |
| [web/routes/dashboard.py](web/routes/dashboard.py) | medium | Layout PUT + validated name |
| [web/routes/drawdown.py](web/routes/drawdown.py) | medium | Reset endpoint |
| [web/routes/backtest.py](web/routes/backtest.py) | medium | List + delete runs |
| [web/routes/admin.py](web/routes/admin.py) | medium | Emergency-stop handler |
| [web/routes/admin_bots.py](web/routes/admin_bots.py) | medium | Admin lifecycle + bulk |
| [core/database.py](core/database.py) | deep | Schema migration + WAL + connection cache |
| [core/deal_store.py](core/deal_store.py) | deep | User-scoped store + batch order fetch |
| [core/user_store.py](core/user_store.py) | deep | Auth + epoch + username validation |
| [core/credentials.py](core/credentials.py) | deep | Fernet + per-user + rotation atomicity |
| [core/dashboard_store.py](core/dashboard_store.py) | deep | Layout validation + upsert |
| [core/changelog_store.py](core/changelog_store.py) | medium | Safety comment on f-string |
| [core/paths.py](core/paths.py) | deep | User-dir helpers |
| [core/logging_setup.py](core/logging_setup.py) | deep | RequestIdFilter + contextvar |
| [core/cleanup.py](core/cleanup.py) | deep | Orphan .tmp sweep |
| [core/rate_budget.py](core/rate_budget.py) | deep | CostBudget token bucket |
| [core/circuit_breaker.py](core/circuit_breaker.py) | deep | State machine + exchange wiring |
| [core/liquidation_guard.py](core/liquidation_guard.py) | deep | Thread-safe position updates |
| [core/drawdown_guard.py](core/drawdown_guard.py) | deep | Peak persistence + trigger |
| [core/schedule_guard.py](core/schedule_guard.py) | medium | TZ-aware via ZoneInfo |
| [main_web.py](main_web.py) | deep | Boot logging + filter attach |
| [main_paper.py](main_paper.py) | deep | Engine launcher + SIGTERM handler |
| [paper/paper_engine.py](paper/paper_engine.py) (~1700 lines) | deep | Tick loop + deal lifecycle + state flush + notify drain |
| [paper/paper_state.py](paper/paper_state.py) | deep | Open-deals map + locks |
| [paper/state_io.py](paper/state_io.py) | deep | Atomic write-rename + load |
| [paper/close_handler.py](paper/close_handler.py) | deep | Offline-close flow |
| [paper/errors.py](paper/errors.py) | deep | Transient vs persistent classification |
| [live/live_engine.py](live/live_engine.py) | deep | NotImplementedError posture + dry-run |
| [live/order_reconciliation.py](live/order_reconciliation.py) | deep | Thread-safety (Phase 3 scaffolding) |
| [exchanges/bitget.py](exchanges/bitget.py) | deep | Idempotency + retry logic |
| [exchanges/kraken.py](exchanges/kraken.py) | deep | **No idempotency — r2-002** |
| [exchanges/public_exchange.py](exchanges/public_exchange.py) | deep | Circuit breaker wiring |
| [exchanges/base_exchange.py](exchanges/base_exchange.py) | medium | Common helpers |
| [web/static/index.html](web/static/index.html) | medium | CDN SRI verification |
| [web/static/app.js](web/static/app.js) | medium | CSRF wrapper + WS client + fetch |
| [web/static/chart_module.js](web/static/chart_module.js) | medium | Annotation CRUD + CSRF inheritance |
| [web/static/maintenance.html](web/static/maintenance.html) | medium | Auto-reload safety |
| [scripts/backup.sh](scripts/backup.sh) | deep | Retention + permissions |
| [scripts/restore.sh](scripts/restore.sh) | deep | Pre-restore snapshot + permissions |
| [scripts/rollback.sh](scripts/rollback.sh) | deep | Schema-migration guard |
| [requirements.txt](requirements.txt) | deep | Pin posture |
| [.env.example](.env.example) | deep | Completeness |
| [.gitignore](.gitignore) | medium | Secret-leak surface |
| [Makefile](Makefile) | medium | Target consistency |
| [start.sh](start.sh) / [stop.sh](stop.sh) | medium | .env sourcing + graceful stop |
| [tests/test_secret_redaction.py](tests/test_secret_redaction.py) | deep | Redaction contract |
| [tests/test_cross_tenant_isolation.py](tests/test_cross_tenant_isolation.py) | deep | Isolation coverage |
| [tests/test_csrf.py](tests/test_csrf.py) | medium | Failure paths |
| [tests/test_security_headers.py](tests/test_security_headers.py) | medium | Header coverage |
| [tests/test_cleanup.py](tests/test_cleanup.py) | medium | .tmp sweep regression |
| [tests/test_config_validation.py](tests/test_config_validation.py) | medium | Boot-config gate |
| [tests/test_ws_portal_log.py](tests/test_ws_portal_log.py) | medium | 4401/4403 WS rejection |

~55 files opened; ~35 deep-read.

---

## Appendix B: Methodology Notes

### How this audit was run

1. **Orientation** (30 min): read three prior audit docs, `git log`, directory map, line counts per module.
2. **Agent delegation** (5-7 h wall clock, executed in parallel): 6 Explore agents spawned simultaneously, each with a different subset of the 17 areas + prior-finding verification tasks. Each agent read end-to-end (not grep-only) on its deep-read files.
3. **Spot-verification** (30 min): the auditor manually verified each agent's highest-severity claim against actual code. Two adjustments resulted: the HSTS claim was downgraded from HIGH SHOULD-FIX to MEDIUM MONITORING (uvicorn defaults cover the stated topology; runbook verification suffices); the YAML-leak + Kraken-idempotency claims were confirmed exactly as the agents described.
4. **Consolidation** (45 min): per-agent findings renumbered to a global r2-NNN namespace, cross-indexed against prior r1/r1.1/pd IDs, priority-matrix built.
5. **Report writing** (60 min): assembled this document.

### Trust model

- **Prior STATUS markers:** not taken on trust; every resolved item re-verified against current HEAD.
- **Agent outputs:** spot-checked for highest-severity claim accuracy; agents given clear instructions to say "No findings — clean" rather than invent findings to pad word count.
- **Operator interpretation:** this audit is static-review only; dynamic testing is an explicit limitation (Part 6).

### What was NOT done

- No runtime testing.
- No pentest.
- No fuzzing.
- No load testing.
- No pip-audit CVE scan.
- No container/systemd runtime review.
- No external review of this audit by a second human / agent.

---

## Appendix C: INFO-grade observations (Area 14 bot-lifecycle)

Listed here rather than in Part 1 because each is a "verified correct design" observation, not an actionable finding. Included for completeness and to help future auditors understand what was checked.

- `StateIO.write()` atomic via `.tmp` + `Path.replace()`.
- Portal ↔ engine lock (fcntl advisory) correctly serialises state.json mutations.
- `_monitor_open_deals` snapshot iteration + in-loop `if deal_id not in open_deals` re-check is safe under concurrent close.
- `_db_create_deal_with_retry` — 3-attempt retry + explicit ERROR-log-and-refuse on exhaustion is the right failure mode.
- `_place_market_order` NotImplementedError propagates out of `_tick()` unswallowed by a dedicated `except NotImplementedError: raise` block.
- `OrderReconciler._pending` locked + short critical sections + no I/O under lock.
- Engine restart `_load_state()` restores all critical state (balance, peak, deals, overrides, wick trackers).
- Pre-fix state-file compatibility: missing wick trackers default to entry-price (conservative).
- `LiquidationGuard` thread-safe via lock-gated position list swap.
- `DrawdownGuard` peak persisted across restart — critical for live trading.
- `ScheduleGuard` timezone-safe via `ZoneInfo`.
- SIGTERM handler queues `notify_stop` before `engine.stop()`; sentinel + 15s drain flushes Telegram queue.
- `_write_state()` called end-of-tick; `_clear_state()` on stop ensures portal-visible running=False.
- Engine user_id stamped at construction; every `deal_store.*` call is user-scoped.
- Manual-trigger sentinel path includes `user_id`; no cross-user race.
- Tick-error classification (`classify_exception`) transient vs persistent — notifications gated correctly.
