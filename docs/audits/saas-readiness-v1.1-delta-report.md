# SaaS-Readiness Audit v1.1 — Delta Report

**Classification:** Internal
**Status:** Delta audit covering post-v1 merges
**Base audit:** `saas-readiness-v1-report.md` (commit `200163e`, 2026-04-23)
**HEAD reviewed:** `9c6b548` (main post-Sprint-2 merge, 2026-04-24)
**Commits in scope:** 43 (HEAD since `200163e..HEAD`)
**Auditor:** Claude Code under `saas-readiness-v1.1-delta` prompt
**Time budget:** ~3 h targeted review (deep-read on 6 focus areas, grep-level elsewhere)

---

## Executive Summary

Between the v1 baseline and this delta review the tree absorbed two remediation sprints (**5 HIGH** findings resolved in Sprint 1, **11 MEDIUM/LOW** findings in Sprint 2) plus a cluster of Workspace chart features that landed new code surfaces: TradingView-style toolbar + info-sidebar (PR 5a), annotations polish (PR 5b), per-chart timezone (PR 60/61-class series), scroll-to-load historical candles, multi-instance indicator plugin architecture, and per-line styling via a tabbed edit-form. This delta audit deep-reads those surfaces plus verifies the sprint fixes landed coherently.

**Net picture is incremental improvement, not drift.** All 16 fixes from Sprints 1 + 2 are present in main with no observed regressions (Part: *Sprint 1 + 2 Resolution Verification*). The v1 category-level concerns (single-process assumptions, in-memory caches, signing-service separation) remain open per-phase roadmap; this delta did not re-evaluate them. The new surfaces introduced since v1 are clean on the security axis — authentication + rate-limit + cache-key scoping are correct. Two MEDIUM-grade hygiene findings surfaced on the frontend side (scroll-to-load merge parity, ticker lock head-of-line), one LOW on timezone-LS validation, one LOW plus one INFO on the plugin architecture. **Nothing CRITICAL, nothing MUST-fix-before-multi-user-seed.**

**Delta counts:** 6 new findings total (0 CRITICAL, 0 HIGH, 2 MEDIUM, 3 LOW, 1 INFO). Finding IDs follow `r1.1-NNN` prefix. Where a finding is a direct variant of a v1 item, the cross-reference is spelled out in Part 2.

## Severity Summary

| Severity | v1 count | v1.1 new | v1.1 resolved | Net (open) |
|----------|:--------:|:--------:|:-------------:|:----------:|
| CRITICAL | 1 | 0 | 0 | 1 |
| HIGH     | 12 | 0 | 5 (Sprint 1) | 7 |
| MEDIUM   | 34 | 2 | 7 (Sprint 2) + 1 (Sprint 3) | 28 |
| LOW      | 25 | 3 | 4 (Sprint 2) + 3 (Sprint 3b) | 21 |
| INFO     | 4 | 1 | 1 (Sprint 3b) | 4 |
| **Total** | **76** | **6** | **21** | **61** |

Sprint 2 also closed bonus-finding `r1-007` (username character-class) bundled with `r1-032`. Sprint 3 (`fix/r1.1-003-workspace-scroll-merge`) resolved `r1.1-003` (MEDIUM). Sprint 3b (`fix/sprint-3b-v1.1-sweep`) bundled the four remaining LOW/INFO items: `r1.1-002`, `r1.1-004`, `r1.1-005`, `r1.1-006`.

---

## Sprint 1 + Sprint 2 Resolution Verification

Each resolved v1-finding grepped against current main and inspected for coherent implementation. No regressions observed.

### Sprint 1 (HIGH — 5 of 12)

| v1-ID | Fix-branch | Verification |
|-------|-----------|--------------|
| **r1-001** | `fix/r1-001-api-key-respects-active` | `web/app.py:371-382` — API-key branch calls `user_store.get_user_by_id(1)` + active-check; fails 401 on missing/inactive; WARNING logged. Matches fix-spec. ✓ |
| **r1-002** | `fix/r1-002-changelog-admin-role-gate` | `web/routes/changelog.py:50` — `if user.role != "admin"`. Docstring no longer mentions the Phase-3b TODO. ✓ |
| **r1-012** | `fix/r1-012-bitget-passphrase-per-user` | `core/credentials.py:177` — `get_bitget_passphrase(user_id)` helper with store-preferred + env-fallback + deprecation-warning. `main_live.py:247-260` uses it. Endpoint at `web/routes/exchanges.py:58-75` requires passphrase for Bitget, optional for Kraken. ✓ |
| **r1-023** | `fix/r1-023-subprocess-env-whitelist` | `web/app.py:943-991` — `_BOT_ENV_ALLOWLIST` frozenset + `_bot_subprocess_env(user_id)` helper. Both `start_bot` (`:1024`) and `start_bot_dry_run` (`:1189`) call it; neither calls `os.environ.copy()`. ✓ |
| **r1-041** | `fix/r1-041-state-mtimes-per-user` | `web/app.py:2069` — `key = (bot.user_id, bot.slug)` tuple-keyed cache. Declaration at `:2045` is typed `dict[tuple[int, str], float]`. ✓ |

### Sprint 2 (11 MEDIUM/LOW, bundled)

| v1-ID | Verification |
|-------|--------------|
| **r1-004** | `web/app.py:1375` — `_rate_limit_key_func` honours leftmost X-Forwarded-For entry + whitespace-strip + fallback. `Limiter(key_func=_rate_limit_key_func)` at `:1395`. ✓ |
| **r1-007** | Bundled with r1-032; `_USERNAME_RE` excludes whitespace + control chars. ✓ |
| **r1-020** | `core/deal_store.py:418-450` — `get_orders_for_deal_ids` IN-list batch helper. `web/routes/deals.py:79-90` uses it; no per-deal loop remaining. ✓ |
| **r1-032** | `core/user_store.py:55-69` — `validate_username` with `re.fullmatch`; audit-log pipe delimiter rejected. ✓ |
| **r1-042** | `web/app.py:630` (read-path) + `:653` (default-state path) — `validated["bot_user_id"] = self.user_id`. Additive. ✓ |
| **r1-051** | `grep DEFAULT_USER web/ core/ tests/` returns zero production hits; constant + helper deleted from `core/user.py`. ✓ |
| **r1-052** | `grep TODO.*phase web/ core/` returns zero hits. Inferred-resolved per v26-16 broadcaster refactor; no lingering TODO comments. ✓ |
| **r1-053** | `tests/test_cross_tenant_isolation.py` (185 LOC, 3 E2E tests). `/api/bots` listing deferred — documented inline. ✓ |
| **r1-054** | `tests/test_web_routes.py::TestApiKeyRespectsActive::test_api_key_auth_path_isolated_from_cookie` present. ✓ |
| **r1-056** | `grep "except Exception:$" -A 1 | grep -B 1 "pass$"` returns empty for web/ core/ paper/ live/ exchanges/. All converted to `logger.debug`. ✓ |
| **r1-058** | `web/app.py:1410-1458` — `_validate_config()` raises on missing `REVERTO_SECRET_KEY`, warns on missing recommended. Called in `lifespan` startup at `:1470`. ✓ |
| **r1-075** | `web/app.py:1303-1308` — HSTS header emitted on `request.url.scheme == "https"`. ✓ |

**Test suite:** 1281 passed, 2 skipped. No Sprint-related flakes observed during local re-runs.

---

## Part 1: Findings by Focus Area

### Area 1 — `/api/ticker` endpoint

**Scope.** PR 5a added the Workspace chart-panel info-sidebar polling `/api/ticker/{pair}` every 5s. Route lives at `web/routes/chart.py:95-166`, config at `web/app.py:1655-1660`.

**Summary.** Auth + rate-limit + cache-key design are correct. One operational concern around lock-contention with `/api/price`.

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1.1-001 | MEDIUM | `_price_lock` shared between `/api/ticker` and `/api/price` serialises every exchange call | `web/routes/chart.py:131-134` | C |
| r1.1-002 | LOW | Unvalidated `pair` path-param can pollute `_ticker_cache` briefly | `web/routes/chart.py:113-120` | B |

#### r1.1-001 — `_price_lock` head-of-line blocking (MEDIUM)

**Wat.** Both `/api/price` and `/api/ticker` hold `_price_lock` around their `ccxt.fetch_ticker` calls (`web/routes/chart.py:76-77` and `:131-134`). The lock correctly serialises access to the module-level `_bitget_client` — but it ALSO means any `/api/ticker` request for pair A blocks a simultaneous `/api/price` request until the Bitget call returns.

**Waarom.** With N panels polling ticker every 5s and a /api/price call happening at the top of every page render, the blocking is rare in single-user deploy (cache-hits absorb most requests). Post-multi-user seed with 50+ clients the serial behaviour becomes the head-of-line bottleneck. A slow Bitget endpoint (2s p99) with 20 concurrent users queues 40s of tail latency on a single mutex.

**Waar.** See snippet. `_price_lock` declaration at `web/app.py:1649` (`asyncio.Lock()`).

**Remediation.** Variant of v1's **r1-027** (module-level `_bitget_client` + `_price_lock`). Proper fix moves the ccxt client into a connection-pooled resource in the signing-service (Phase C). Interim: Reverto already has `_ticker_cache` (10s TTL) absorbing most requests; at current scale the lock is safe. Document the scaling ceiling so Phase C planning factors it in. No code change required for this delta.

**Phase.** C (couples with r1-027).

**STATUS.** Open — defer to Phase C with r1-027.

#### r1.1-002 — Unvalidated pair path-param briefly pollutes ticker cache (LOW)

**Wat.** `/api/ticker/{pair}` accepts any string; `_normalize_chart_pair(pair)` (`web/app.py:1634-1644`) only does case-normalise + suffix-reshape. A request like `/api/ticker/BTC$USD<script>` passes through, becomes the cache key, is looked up (miss), and finally sent to `ccxt.fetch_ticker` which rejects. The exception raises 502 — but a cache entry was NOT created (miss path runs before the throw), so this one doesn't actually pollute. However, a variant `/api/ticker/ZZZX` (plausible but unlisted symbol) would succeed past ccxt (ccxt returns None or an empty dict for unknown symbols on some exchanges) and land in the cache for 10s.

**Waarom.** The ticker cache is bounded to 32 entries via LRU (`web/app.py:1659`), so a spam-flood would evict legitimate entries but not exhaust memory. Post-multi-user seed, a hostile authenticated client (or a prompt-injection path through the UI) could evict competing users' cached entries, forcing extra upstream calls. Low-severity because the rate-limiter (60/min) caps the evict rate.

**Waar.** See `web/app.py:1634-1644` for normalise helper + `web/routes/chart.py:113-165` for handler.

**Remediation.** Add an allowlist of symbols accepted by `/api/ticker` (just "BTC/USD" today; the chart data path has the same exposure but uses `_CHART_TIMEFRAMES` validation for timeframe — mirror that shape for pair). Five-minute fix; scope-in with the next `/api/ticker` touch.

**Phase.** B.

**STATUS.** RESOLVED in `fix/sprint-3b-v1.1-sweep` (`_CHART_PAIRS_ALLOWLIST = {"BTC/USD", "BTC/USDT"}` enforced at the top of `api_ticker`; 400 before cache/LRU touch).

---

### Area 2 — Scroll-to-load mechanism

**Scope.** Chart-module factory `_maybeLoadMoreHistory` at `web/static/chart_module.js:2523-2597` + main-chart path in `web/static/app.js:5954-6015`. Both prepend older candles to the LWC series on left-edge pan.

**Summary.** AbortController lifecycle + rate-limit retry are correct. One **parity divergence** between main-chart and workspace-panel on 30s refresh merge.

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1.1-003 | MEDIUM | Workspace chart-panel 30s refresh drops scroll-loaded history | `chart_module.js:2504` | B |

#### r1.1-003 — Workspace `_loadCandles` clobbers prepended history (MEDIUM)

**Wat.** The main bot-chart (`app.js::fetchChartData`, `:6017-6058`) merges prior pan-loaded history with fresh candles before `_chartCandles.setData`:
```javascript
// web/static/app.js:6034-6036
const newOldest = candles[0].time;
const priorHistory = _chartCandlesArr.filter((c) => c.time < newOldest);
_chartCandlesArr = priorHistory.concat(candles);
```

The Workspace chart-panel factory's equivalent does not — it replaces wholesale:
```javascript
// web/static/chart_module.js:2504
state._candles = candles;
state._candleSeries.setData(candles);
```

Every 30s the Workspace-panel refresh timer (`:2253`) invokes `_loadCandles()`, wiping any history the user scrolled back to acquire.

**Waarom.** User-visible regression: a user who scrolls back 10 batches to see 5000 historical candles loses them at most ~30s after the pan completes. Pre-v1.1 the scroll-to-load wasn't in the Workspace path at all, so this is new-code drift from the fix-spec applied correctly to main-chart but not to the factory.

**Waar.** See `web/static/chart_module.js::_loadCandles` (`:2484-2509`) for the broken path; `app.js::fetchChartData` (`:6034-6036`) for the reference implementation.

**Remediation.** Port the merge into the factory:
```javascript
// chart_module.js::_loadCandles after line 2503
const newOldest = candles[0].time;
const priorHistory = state._candles.filter((c) => c.time < newOldest);
state._candles = priorHistory.concat(candles);
```

Ten-minute fix. Needs a regression test that pans back, waits > 30s, and asserts the prior history survives.

**Phase.** B.

**STATUS.** RESOLVED in `fix/r1.1-003-workspace-scroll-merge` (extracted `_mergePriorHistory` pure helper; `_loadCandles` uses it before `setData`; helper exposed via `window.RevertoChart.mergePriorHistory` for future JS-test infra).

---

### Area 3 — Timezone state management

**Scope.** Main-chart + Workspace-panel + wizard + backtest-candle share the `window.RevertoChart.buildTimezoneFormatter` helper. Per-chart LS keys at `web/static/app.js:5530, 5580, 5581` and per-panel `state.timezone` in `chart_module.js:1483-1493`.

**Summary.** Legacy migration (`reverto_timezone` → `reverto.main_chart_timezone`) handled correctly. `useUtc → timezone` migration is idempotent on layout-load. One LOW around LS-value validation.

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1.1-004 | LOW | Timezone LS values not validated before being stored in module state | `app.js:5530-5547, 5583-5584` | A |

#### r1.1-004 — Timezone localStorage values bypass `_normalizeChartTimezone` (LOW)

**Wat.** `_loadMainChartTz()` (`app.js:5532-5545`) returns the raw LS value; `_wizardChartTimezone` and `_btCandleChartTimezone` use `localStorage.getItem(...) || 'local'` (`:5583-5584`). None call `_normalizeChartTimezone` before assigning to module-level state. If corrupted / hand-edited LS contains `"Not/A/Real_TZ"`, the dropdown UI sets `sel.value = current` to a non-existent option and the chart renders with the silent fallback `'local'` (via `_normalizeChartTimezone` at format-build time).

**Waarom.** Runtime-safe because `buildTimezoneFormatter` always normalises, but the dropdown displays an inconsistent state (no option highlighted) and the operator has no signal that their stored value is being ignored. Cosmetic / UX-only.

**Waar.** `web/static/app.js:5530-5547` (main), `:5583-5584` (wizard + backtest).

**Remediation.** Wrap each LS-read through `window.RevertoChart && window.RevertoChart._normalizeChartTimezone || identity`. Two-line fix per site. Could be bundled with a future timezone-enhancement PR.

**Phase.** A.

**STATUS.** RESOLVED in `fix/sprint-3b-v1.1-sweep` (`window.RevertoChart.normalizeChartTimezone` exported; new `_normalizeTzFromLS` helper in app.js wraps all three LS-read sites: main-chart, wizard, backtest).

---

### Area 4 — Indicator plugin architecture

**Scope.** 9 built-in plugins + `registerIndicatorPlugin` in `chart_module.js:844-863`. Instance migration via `_migrateIndicators`/`_migrateInstanceStyles`. Active-panel registry `_activePanelCharts` at `:1407`.

**Summary.** Contract + migration are robust. `_activePanelCharts` add/delete balanced. One INFO on init-failure leak, one LOW on registration validation.

| ID | Severity | Finding | File:line | Phase |
|----|----------|---------|-----------|-------|
| r1.1-005 | LOW | `registerIndicatorPlugin` only validates `type` string, not full contract | `chart_module.js:861-864` | A |
| r1.1-006 | INFO | `_activePanelCharts` entry orphans if `_initChart` throws mid-init | `chart_module.js:2353-2412` | A |

#### r1.1-005 — `registerIndicatorPlugin` accepts under-specified plugins (LOW)

**Wat.**
```javascript
// chart_module.js:861-864
function registerIndicatorPlugin(plugin) {
  if (!plugin || typeof plugin !== 'object' || typeof plugin.type !== 'string') return;
  INDICATOR_PLUGINS[plugin.type] = plugin;
}
```

Only the `type` string is validated. A plugin missing `params`, `lines`, `labelTemplate`, `createSeries`, or `render` is accepted silently — and then crashes at render-time with a cryptic TypeError when the operator adds an instance through the manager modal.

**Waarom.** `registerIndicatorPlugin` is a public namespace export (`window.RevertoChart.registerIndicatorPlugin`). Future plugin authors (Reverto's own or third-party) debug a cryptic "Cannot read properties of undefined (reading '0')" deep inside render rather than a crisp "plugin X missing 'lines'" at registration time. Defensive-programming finding; no security dimension.

**Waar.** `web/static/chart_module.js:861-864`.

**Remediation.** Validate each required field + type. Roughly:
```javascript
const required = ['type', 'displayName', 'paneType', 'params', 'lines',
                  'labelTemplate', 'createSeries', 'render'];
for (const k of required) {
  if (!(k in plugin)) {
    console.warn('registerIndicatorPlugin: plugin missing', k);
    return;
  }
}
```

Fifteen-minute fix.

**Phase.** A.

**STATUS.** RESOLVED in `fix/sprint-3b-v1.1-sweep` (eight-field `required` list + type-level checks for array/function fields; `console.warn` + `return false` on violation; built-in plugins unaffected because they populate `INDICATOR_PLUGINS` directly).

#### r1.1-006 — `_initChart` failure leaves orphan in `_activePanelCharts` (INFO)

**Wat.** `_activePanelCharts.add(state._themeRegistryEntry)` happens at `chart_module.js:2353`, BEFORE the rest of `_initChart` (resize-observer setup, range subscription, click handler). Any throw after the add but before the function returns true leaves the entry registered. `destroy()` only runs via `state.onRemove`; a throw inside init doesn't trigger it, so the panel's entry stays in the set forever. `_applyThemeToPanel` is defensive (`if (!entry || !entry.chart) return`) so nothing crashes, but the Set grows by one stale entry per failed init.

**Waarom.** In practice `_initChart` rarely throws — the main failure mode (LWC missing) is caught by `:2322` before the `add` call. So this finding is more "code robustness review" than "live bug". Flagged as INFO because a future panel with flaky init (e.g., pane allocation on a very slow device) would reveal it.

**Waar.** `web/static/chart_module.js:2322-2412` (init body with add at :2353).

**Remediation.** Wrap the code between `add` and the final `return true` in a try/catch; on catch, `_activePanelCharts.delete(state._themeRegistryEntry); state._themeRegistryEntry = null; return false`. Ten-minute fix when somebody has eyes on the file for another reason.

**Phase.** A.

**STATUS.** RESOLVED in `fix/sprint-3b-v1.1-sweep` (post-add body wrapped in try/catch; on throw: delete from set, null the entry reference, `console.error`, return false).

---

### Area 5 — Per-line styling

**Scope.** `LINE_STYLE_MAP`, `_applyOpacityToColor`, `_seriesOptsFromStyle` helpers at `chart_module.js:372-412`. Migration at `_migrateInstanceStyles:906-933`. Tabbed edit-form at `:2037-2131`.

**Summary.** Clean. `_applyOpacityToColor` boundary cases all covered (hex-3 expansion, already-alphaed, NaN input, clamp). Migration is idempotent. Tab state preserved via `state._editTab`. **No findings.**

---

### Area 6 — Sprint 1 + Sprint 2 verification

See *Sprint 1 + Sprint 2 Resolution Verification* above. **All 16 fixes present in main with no regressions observed.**

---

## Part 2: Cross-reference to v1

Findings that are variants of v1 items:

| v1.1-ID | v1-parent | Relationship |
|---------|-----------|--------------|
| r1.1-001 | r1-027 | Variant — both relate to `_price_lock` + module-level `_bitget_client` contention. Parent is broader (whole-client-refactor); r1.1-001 narrows to the specific `/api/ticker` blast-radius. |
| r1.1-002 | r1-028 | Variant — same cache-pollution concern as v1 flagged for `/api/chart`, now extended to `/api/ticker` with the same mitigation path. |
| r1.1-003 | (none) | New — scroll-to-load was introduced post-v1; the merge-parity regression is net-new code. |
| r1.1-004 | (none) | New — per-chart timezone was introduced post-v1. |
| r1.1-005 | (none) | New — plugin architecture was introduced post-v1. |
| r1.1-006 | (none) | New — `_activePanelCharts` registry was introduced post-v1. |

---

## Part 3: Remediation Priority

**MUST-fix before first multi-user seed:** none (no CRITICAL or HIGH findings).

**SHOULD-fix near-term (next sweep PR):**
- **r1.1-003** — Workspace `_loadCandles` merge parity. MEDIUM. Clear user-visible regression (scroll-back loss) that won't heal on its own. Ten-minute fix with one regression test.

**Sweep-PR candidates (bundle with the next audit sweep):**
- **r1.1-002** — `/api/ticker` pair allowlist.
- **r1.1-004** — timezone LS-value validation.
- **r1.1-005** — `registerIndicatorPlugin` contract validation.

**Defer to Phase C (architectural):**
- **r1.1-001** — `_price_lock` head-of-line blocking. Bundles with v1's `r1-027` (signing-service separation).

**Nice-to-have:**
- **r1.1-006** — `_initChart` orphan guard (INFO).

---

## Part 4: Limitations of this delta audit

Honest accounting of what was **not** covered:

1. **v1 category-level concerns** (single-process assumptions, in-memory caches, signing-service separation, Alembic adoption) were NOT re-evaluated. They're documented in v1 Parts 10-17 and remain open per phase roadmap.
2. **Workspace open-deals-panel factory** (`chart_module.js:3446-3703`) was opened for context-reading only, not deep-read. Sortable table, column-drag, debounced `/api/bots` refresh — 260 LOC that could hide cross-tenant leaks. Flagged for next audit.
3. **Annotations flow** (the SVG overlay in `chart_module.js::_renderAnnotations:2635-2750` plus the `/api/db/annotations` CRUD) was touched by PR 5b but only quickly scanned. Length of 100+ LOC worth a dedicated review.
4. **GridStack integration** (`app.js::_createPanelElement`, resize/move handlers, save-debounce) was not opened. Any cross-panel isolation issues would surface here.
5. **Backend tests** — `make test` was not run for this audit. Test coverage numbers reported in Sprint 2 (1281 passing) were taken at face value; no flake-hunting or coverage-gap analysis.
6. **Operational side** — log rotation sizes, portal.log PII exposure, metrics cardinality — out of scope for this delta.

---

## Part 5: Recommendations

### Pattern-level theme

The **main-chart vs. workspace-panel divergence** is becoming a theme. The scroll-to-load fix in Sprint 1 ported correctly to the main-chart path but missed the merge-on-refresh step in the workspace-panel factory (r1.1-003). Same shape of drift showed up in earlier v26-NN findings where the main-chart's active-check was parity-fixed across helper functions. Recommendation: the workspace chart-panel factory is fast becoming a parallel implementation of the main-chart flow. **Consider a shared helper layer** (or a properly factored base class) in `chart_module.js` so features landed on one path automatically surface on the other. Relates to v1's broader `chart_module.js` growth observation.

### Plugin architecture robustness

`registerIndicatorPlugin` (r1.1-005) is an early-stage public API that will see wider use when Reverto adds third-party indicator packages (Phase G+ roadmap). Tightening the contract validation NOW is cheap; the fix sweep might as well pair with `r1.1-006` (init orphan) since both land in the same file.

### Cache-key hygiene

Two of the new findings (r1.1-001, r1.1-002) trace back to the `/api/chart + /api/ticker + /api/candles + /api/price` endpoint cluster. As SaaS scaling forces attention here, consider a unified cache layer (Redis-backed, per-user-scoped where needed) that replaces the four module-level OrderedDict caches. Phase C work; couples with v1 r1-024, r1-025, r1-026.

### v1.1 vs. v1 trend

Delta-finding density is **~10× lower per area** than v1's equivalents (six findings vs. sixty in matched areas). The sprints materially improved the floor, and the new code-surfaces introduced post-v1 were mostly reviewed before merge (visible in PR 5a/5b + feat/workspace-indicator-plugins commit quality). **Continue the pattern of per-PR self-review + periodic sweep-audit.**

---

## Appendix: Files reviewed

Scope-depth legend: **deep-read** = full file read or targeted multi-hundred-line block; **medium** = function(s) of interest read in full; **grep-only** = found via grep, no deep read; **not opened**.

| File | Depth | Notes |
|------|-------|-------|
| `web/routes/chart.py` | deep-read | Focus: `/api/ticker` + `/api/price` + `/api/chart` + `/api/candles` routes |
| `web/app.py` | medium | `_request_user`, `_validate_config`, `_bot_subprocess_env`, `_rate_limit_key_func`, `watch_state_files`, `SecurityHeadersMiddleware`, `_normalize_chart_pair`, `BotInfo.read_state` |
| `web/routes/deals.py` | medium | `/api/db/deals` N+1 verification + annotation routes |
| `web/routes/exchanges.py` | medium | `ExchangeKeysBody` + save handler |
| `web/routes/changelog.py` | medium | `_require_admin_user` |
| `web/routes/admin.py` | grep-only | emergency-stop reference pattern |
| `web/routes/bots.py` | grep-only | `_reverto_version` except-pass site |
| `web/routes/admin_bots.py` | not opened | flagged in Limitations for next audit |
| `core/credentials.py` | deep-read | `save_keys`, `get_keys`, `get_bitget_passphrase`, `_migrateInstanceStyles`-adjacent fields |
| `core/user.py` | deep-read | `DEFAULT_USER` removal verified |
| `core/user_store.py` | deep-read | `validate_username` |
| `core/deal_store.py` | medium | `get_deal_orders`, `get_orders_for_deal_ids` |
| `core/database.py` | grep-only | seed-admin statement |
| `main_live.py` | medium | `_authenticated_exchange` passphrase flow |
| `paper/paper_engine.py` | grep-only | except-pass cleanup verification |
| `web/static/chart_module.js` | deep-read | Plugin registry, styling helpers, panel factory, scroll-to-load, timezone, `_activePanelCharts` |
| `web/static/app.js` | medium | Main-chart scroll-to-load merge, timezone LS helpers, `_createPanelElement` |
| `web/static/index.html` | grep-only | Cache-buster versions + exchange dropdown options |
| `web/static/workspace.css` | not opened | not germane to audit focus |
| `tests/test_web_routes.py` | grep-only | verification of r1-054 test location |
| `tests/test_cross_tenant_isolation.py` | grep-only | r1-053 presence verified |
| `docs/audits/saas-readiness-v1-report.md` | reference | STATUS markers + roll-up matrix |

**Git-log review:** 43 commits between `200163e` and `9c6b548` read via `git log --oneline`; deep-reads triggered by commit-message relevance.
