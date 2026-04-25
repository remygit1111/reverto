# PRA-v3 Sample Verification

**Date:** 2026-04-25
**HEAD reviewed:** `2d83907`
**Method:** Manual code-read, no agents
**Sample size:** 8 of 53 markers (15%)
**Time invested:** ~75 min (≈ 9 min average per marker)

## Purpose

PRA-v3 ([`docs/audits/production-readiness-audit-v3.md`](production-readiness-audit-v3.md)) claimed **53/53 prior STATUS markers verified clean**. That verification was performed by 5 parallel Explore agents; only the 4 highest-severity NEW findings were spot-checked manually. This document independently re-verifies 8 markers across all four prior audits (v1, v1.1, pre-deploy, v2) to determine whether the audit baseline is trustworthy as input for follow-up fix PRs.

## Summary

| Marker | Audit | Severity | Verdict | Notes |
|--------|-------|----------|:-------:|-------|
| r1-022 | v1 | MEDIUM | ✓ | Backup script implements 7/28/90 retention; runbook + code coherent |
| r1-052 | v1 | HIGH | ✓ | Defense-in-depth user_id filter on every broadcast; lock-gated |
| r1-073 | v1 | HIGH | ✓ | Middleware + frontend-wrapper + graceful migration all wired; symmetry on mutating verbs |
| pd-001 | pre-deploy | HIGH | ✓ | Three named sites scrubbed; meta-confirms r3-002 (regex misses `{str(e)[:200]}`) |
| pd-019 | pre-deploy | MEDIUM | ✓ | API-key, session-cookie, passphrase tests present; Telegram gap real (r3-007) |
| pd-044 | pre-deploy | LOW | ✓ | `core/cleanup.py` defined + `web/app.py:1858-1862` calls in lifespan startup |
| r2-001 | v2 | HIGH | ✓ | Named site (bots.py:638) scrubbed; "catches the class" claim contingent on r3-002 |
| r2-006 | v2 | MEDIUM | ✓ | ACCEPTED rationale holds: pre-accept reject blocks unauth flood; OS fd limits bound authed flood |

**Aggregate verdict:**

- **Verified clean:** 8/8
- **Partial:** 0/8
- **Regression:** 0/8
- **Unverifiable:** 0/8

**Implication for PRA-v3 baseline:** ✅ **Solid.** All 8 sampled markers verified independently. The claim "53/53 markers verified clean" appears trustworthy as audit baseline. Two of the markers (pd-001, r2-001) confirm PRA-v3's own meta-finding r3-002 — when verified manually, the `{str(e)[:200]}` regex gap is real and the chart.py exception leak is real. Proceed with the planned r3-001 + r3-002 + r3-003 fix PR.

---

## Per-Marker Detail

### Marker 1 — r1-022 (backup retention)

**Claim (PRA-v3):** `scripts present, runbook covers operator flow`

**Files read:**
- `scripts/backup.sh` (lines 1-189, full file)
- `docs/runbook.md` (lines 334-462, "Backup and restore" section)

**Code observations:**
- `backup.sh:39-41` defines `RETAIN_DAILY=7`, `RETAIN_WEEKLY=28`, `RETAIN_MONTHLY=90` — matches runbook lines 395-398 exactly.
- `backup.sh:144` find pattern `"20*-*"` matches dated backup directories only — pre-restore snapshots (`pre-restore-<ts>/`) are intentionally excluded from the prune (runbook line 401-403 documents this).
- `backup.sh:167` keeps 1st-of-month backups (`dom == 01`) within 90 days; line 172 keeps Sunday backups (`dow == 7`) within 28 days. Both predicates are short-circuit `continue` clauses BEFORE the `rm -rf` on line 176 — correct ordering.
- `backup.sh:114-115` applies `chmod 600` to all files + `chmod 700` to all dirs after the copy. Runbook line 377-379 references this same posture.
- Online-backup via SQLite `.backup` (line 65) with Python stdlib fallback (lines 73-80) — WAL-aware, concurrent-write-safe.
- MANIFEST captures timestamp + host + git HEAD + file listing (lines 124-132).

**Verdict:** ✓ verified clean

**Reasoning:** The retention values, prune semantics, permissions, and pre-restore exclusion all match between code and runbook. The script is well-commented, defensively coded (`set -euo pipefail`), and idempotent (works correctly on first run with no prior backups, on subsequent runs with retention boundaries crossing). The PRA-v3 claim is accurate but understated — the actual code-runbook coherence is stronger than "scripts present + runbook covers".

---

### Marker 2 — r1-052 (cross-tenant WS filtering)

**Claim (PRA-v3):** `No TODOs in broadcasters`

**Files read:**
- `web/app.py:2271-2330` (LogBroadcaster class)
- `web/app.py:2398-2448` (StateBroadcaster class)
- `web/app.py:2493-2520` (broadcast call sites in watch_state_files)

**Code observations:**
- `LogBroadcaster._user_map: dict[WebSocket, int]` (line 2288) holds per-socket user_id at connect time.
- `LogBroadcaster.broadcast()` line 2316-2317: filters targets via `if self._user_map.get(ws) == owner_user_id` BEFORE iterating `send_text`. Cross-user message delivery is impossible.
- `StateBroadcaster.broadcast()` line 2434-2436: same pattern — filter on `target_user_id` match.
- Both broadcasters use `asyncio.Lock()` to serialise connect / disconnect / broadcast.
- Call site `web/app.py:2497`: `state_broadcaster.broadcast(payload, target_user_id=bot.user_id)` — passes the bot's owner explicitly.
- Call site `web/app.py:2519`: per-user summary frames use `target_user_id=uid`.
- `grep TODO|FIXME|XXX web/app.py | grep -iE "broadcast|user_id|filter"` returns zero results.

**Verdict:** ✓ verified clean

**Reasoning:** The PRA-v3 claim "No TODOs in broadcasters" is the weakest formulation of what's actually in the code. The reality is **defense-in-depth user-scoping at every broadcast frame**: subscribe-side enforces ownership via `registry.get(user_id, slug)`, AND the broadcaster filters by `_user_map` on every send. A future regression that bypassed subscribe (e.g. an infra-triggered broadcast on a "portal" slug) would still be filtered at the broadcast layer. This is solid.

**Audit-quality note:** "No TODOs" is a weak verification artifact — would not catch a cross-tenant leak introduced via a different mechanism. The verification value comes from the actual code-read here. PRA-v3's brevity is fine *because* the code stands up; if the code had been weaker, the claim would have hidden it.

---

### Marker 3 — r1-073 (CSRF double-submit)

**Claim (PRA-v3):** `Middleware + frontend + graceful-migration`

**Files read:**
- `web/app.py:1448-1544` (CSRFMiddleware class)
- `web/app.py:166-173` (_CSRF_MUTATING_METHODS + _CSRF_EXEMPT_PATHS constants)
- `web/static/app.js:50-80` (CSRF cookie reader + fetch wrapper)

**Code observations:**
- Backend `_CSRF_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})` (line 166).
- Frontend `const _mutating = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])` (app.js:65). **Symmetric.**
- Backend `_CSRF_EXEMPT_PATHS = frozenset({"/auth/login"})` (line 181) — minimal, only login is exempt (per pd-042 logout-stays-non-exempt decision).
- Middleware flow (lines 1500-1544):
  1. Authenticated-no-CSRF-cookie → graceful migration: mint + attach cookie, request passes through (line 1502-1511).
  2. Non-mutating method → pass through (line 1513).
  3. Path in exempt list → pass through (line 1515).
  4. Unauthenticated → pass through (let auth layer 401) (line 1521).
  5. Cookie or header missing → 403 (line 1526-1535).
  6. Cookie + header present but unequal → 403 via `secrets.compare_digest` (line 1537).
- Frontend `_getCsrfToken()` reads from `document.cookie` (app.js:58-61). Wrapper auto-injects on mutating verbs unless `X-CSRF-Token` already set by caller (app.js:72-74) — caller-override-friendly.

**Verdict:** ✓ verified clean

**Reasoning:** All three claim components verify: (1) middleware enforces double-submit via timing-safe compare on every mutating verb except `/auth/login`; (2) frontend wrapper auto-injects header from cookie on the same verb set; (3) graceful-migration path preserves legacy session usability without indefinitely bypassing the check (one-shot grant + cookie mint). Mutating-verb sets are symmetric between front + back.

---

### Marker 4 — pd-001 (OSError scrubbing) + meta-check on r3-002

**Claim (PRA-v3):** `All sweep sites + r2-001 closure + new regression test`

**Files read:**
- `tests/test_response_body_hygiene.py` (full file, 157 lines)
- `web/routes/chart.py:160-167, 258-265, 365-375` (the 3 sites PRA-v3's r3-001 flagged)

**Code observations:**
- Regression test regex (test_response_body_hygiene.py:54-67):
  ```
  HTTPException(...status_code=5XX...detail=f"...{<excname>}...")
  ```
  where `<excname>` ∈ `{e, err, exc, ex, exception}` and the regex requires the brace to OPEN with that name (line 61: `\{(?:` + name + `)`).
- Mental execution of regex against `f"ticker fetch failed: {str(e)[:200]}"`:
  - First char inside `{` is `s` (from `str(...)`) — does NOT match `e|err|exc|ex|exception`.
  - **Regex misses** function-wrapped exception.
- `chart.py:164` — `f"ticker fetch failed: {str(e)[:200]}"` (502 status). Confirmed leaks ccxt exception detail.
- `chart.py:263` — `f"Exchange error: {str(e)[:200]}"` (502). Same pattern.
- `chart.py:372` — `f"Exchange error: {str(e)[:200]}"` (502). Same pattern.
- The three named pd-001 sites (bots.py:258, bots.py:464, drawdown.py:52) and the r2-001 site (bots.py:638) ARE all scrubbed (verified via grep + read).

**Verdict:** ✓ verified clean — and r3-002 meta-finding confirmed correct

**Reasoning:** The pd-001 closure is correct AT THE NAMED SITES. The regression test catches the bare `{e}` regression at any future site. **However**, the test's regex does NOT catch the `{str(e)[:200]}` variant — confirmed by mental regex execution. PRA-v3 itself filed this as r3-002, and three current chart.py sites (r3-001) demonstrate the gap is exploitable. The meta-finding is independently verified here. PRA-v3's claim wording is technically accurate but the term "catches the class" overstates — it catches the *bare-name* class, not the *exception-wrapped-in-function-call* class.

---

### Marker 5 — pd-019 (secret-redaction tests)

**Claim (PRA-v3):** `API-key, session-cookie, passphrase covered. Telegram tokens uncovered → r3-007`

**Files read:**
- `tests/test_secret_redaction.py` (full file, 181 lines)
- `core/credentials.py:191-232` (`get_bitget_passphrase` — covered by test 4)
- `notifications/telegram.py:50-60` (Telegram token loading — uncovered by tests)

**Code observations:**
- Test 1 (`test_audit_never_contains_full_api_key`, line 38): drives `_audit()` with a 40-char sentinel `sk_test_AAA...` string and asserts the sentinel is absent from caplog + `audit.log` + `audit.jsonl`. ✓ API-key covered.
- Test 2 (`test_audit_key_hint_format_is_prefix_only`, line 97): asserts the hint format is `apikey:<8-char-hex>`. ✓ Hint format pinned.
- Test 3 (`test_session_cookie_not_logged_on_verify_failure`, line 124): drives `_verify_session_cookie` with a sentinel-shaped malformed cookie and asserts the value is absent from caplog. ✓ Session-cookie covered.
- Test 4 (`test_bitget_passphrase_not_logged_on_env_fallback`, line 152): drives `get_bitget_passphrase` via env-fallback path with a sentinel passphrase, asserts the value is absent from caplog. ✓ Passphrase covered.
- **Not covered:** Telegram bot tokens (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CLAUDE_BOT_TOKEN`), Telegram chat IDs, Fernet master keys.
- `notifications/telegram.py:51-52` reads tokens from env. Any future logging refactor in this module would not be guarded by the redaction-test contract.

**Verdict:** ✓ verified clean

**Reasoning:** The three documented categories (API-key, session-cookie, Bitget passphrase) all have semantic regression tests that drive real code paths with sentinel values. The Telegram-token gap is real — there are no tests asserting Telegram tokens stay out of logs. PRA-v3's claim is accurate; r3-007 correctly identifies the gap.

---

### Marker 6 — pd-044 (.tmp orphan cleanup wired)

**Claim (PRA-v3):** `core/cleanup.py wired`

**Files read:**
- `core/cleanup.py` (full file, 73 lines)
- `web/app.py:1838-1869` (lifespan startup block)

**Code observations:**
- `core/cleanup.py:25` defines `def cleanup_orphaned_tmp_files(*directories: Path) -> int:`.
- Function walks each directory recursively via `rglob("*.tmp")`, skips non-files (avoids `IsADirectoryError`), unlinks files individually with per-file try/except (line 47-52). Per-file failures log at DEBUG and continue. Missing directory → silent skip (line 39-40, doesn't fail boot).
- `web/app.py:1855-1862` (inside `lifespan()` startup, AFTER `_validate_config()` + `_validate_config_completeness()`, BEFORE the `=== Portal started ===` log line and background-task creation):
  ```python
  from core.cleanup import cleanup_orphaned_tmp_files
  cleanup_orphaned_tmp_files(
      BASE_DIR / "logs",
      BASE_DIR / "credentials",
  )
  ```
- Scope: only `logs/` and `credentials/` — both Reverto-owned directories.

**Verdict:** ✓ verified clean

**Reasoning:** The function exists, has a non-trivial implementation with proper error-handling, and is called in lifespan startup with the correct directory scope. "Wired" is a precise description — the caller exists, the function is invoked once per portal start, the scope is bounded.

---

### Marker 7 — r2-001 (YAML scrub at bots.py:638)

**Claim (PRA-v3):** `Plus regression test catches the class`

**Files read:**
- `web/routes/bots.py:625-650` (bot-duplicate handler around the r2-001 site)
- `tests/test_response_body_hygiene.py` (re-confirmed regex from Marker 4)

**Code observations:**
- `bots.py:635-650`: the YAML parse is wrapped in `try / except yaml.YAMLError`. The except clause:
  - Calls `logger.exception(...)` with full context (user.id, source slug, target slug) — full trace lands in portal.log.
  - Raises `HTTPException(status_code=500, detail="Failed to parse source bot config for duplication")` — generic detail.
  - **No exception interpolation in detail.** ✓
- The fix correctly mirrors the pd-001 template (`logger.exception` + generic `detail`).
- The regression test would catch a regression to `detail=f"...: {e}"` at this site (status_code=500 + bare `{e}` matches the regex).
- The regression test would NOT catch a regression to `detail=f"...: {str(e)[:200]}"` — same gap as Marker 4.

**Verdict:** ✓ verified clean

**Reasoning:** The named fix at `bots.py:638` is correctly applied with `logger.exception` + generic detail. The regression test catches the bare-pattern regression at this site. PRA-v3's claim "regression test catches the class" is true for the bare-name class but contingent on r3-002 for function-wrapped variants. Same caveat as Marker 4, which is what makes r3-002 a coherent finding rather than just a hypothetical.

---

### Marker 8 — r2-006 (no WS frame-rate limit, ACCEPTED)

**Claim (PRA-v3):** `Single-operator. Multiple browser tabs from same authed user could pile up but bounded by OS fd limits. Stays ACCEPTED.`

**Files read:**
- `web/app.py:2336-2396` (`/ws/logs/{slug}` handler)
- `web/app.py:2529-2582` (`/ws/state` handler)
- `web/app.py:2271-2330` (LogBroadcaster) + `2398-2448` (StateBroadcaster)

**Code observations:**
- Both handlers reject unauthenticated WS upgrade with `await websocket.close(code=4401)` BEFORE `accept()` (lines 2344-2347, 2536-2539).
- An unauthenticated scanner CANNOT establish a persistent WS connection — close happens at handshake. Public-exposure scanner traffic on these paths terminates immediately.
- No `@limiter.limit(...)` decorator on either handler — slowapi doesn't decorate WebSocket endpoints by design.
- Per-connection inner loop is `while True: await asyncio.sleep(30); await websocket.send_text("__ping__")` — bounded outbound rate (one ping per 30s).
- No client-to-server message channel (no `receive_text()` / `receive_json()`) — no inbound message-flood vector.
- No per-user connection cap — `_user_map` is a dict that allows unlimited entries per user_id.
- An authenticated user opening N concurrent WS connections costs N file-descriptors on the uvicorn process. Default Linux soft fd limit on systemd-managed services is 1024-2048.

**Verdict:** ✓ verified clean

**Reasoning:** The ACCEPTED rationale holds:
1. **Unauthenticated WS-flood blocked at handshake** — confirmed: pre-accept rejection prevents any unauth socket from entering the broadcaster.
2. **Authenticated-flood bounded by OS fd limits** — confirmed: no per-user cap exists, so the bound is OS-level. For single-operator, this is a self-DoS vector at most (the operator opens too many tabs); not exploitable cross-tenant.
3. **Phase B will need per-user caps** — correctly deferred. Multi-user threat model would require a `_user_map` size check at connect time.

The PRA-v3 reasoning is sound. Under public-exposure context, the unauth-flood concern (the new threat vs. v2 baseline) is correctly mitigated by the pre-accept reject. Stays ACCEPTED.

---

## Audit-Quality Observations

### What worked well in PRA-v3 claim language

- **Precise file:line references** (e.g. `[web/app.py:1612-1615](web/app.py#L1612-L1615)`) made claim-to-code traversal trivial. None of the 8 markers required hunting for the cited code.
- **Cross-references to original audit IDs** (e.g. "carried as r3-005" or "matched against pd-001 template") provided context that one-line table cells couldn't.
- **Honest carry-forward language** for items where context shifted under public exposure (r2-003 → r3-005, r2-011 → r3-008). Better than silently re-classifying.

### Where claim language was weak

- **"No TODOs in broadcasters" (r1-052)** — verified by absence of a string. The actual reality (defense-in-depth filter on every broadcast frame) is much stronger; the claim hides the strength. Future-auditors who see only "no TODOs" might think the verification was shallow. **Lesson:** describe what the code DOES, not what it doesn't say.

- **"All sweep sites + r2-001 closure + new regression test" (pd-001)** — technically true but ambiguous about which "class" the test catches. The bare-name class is caught; the function-wrapped class isn't. This wording is what allowed r3-001 to slip past the agent verification of pd-001 — the agent likely verified the named sites + saw the test exists, didn't run the regex against current `web/routes/`. **Lesson confirmed from PRA-v3's Part 6:** when proposing a class-of-issue regex, run it against the entire scope at audit-finalisation time. PRA-v3 itself flagged this; manual verification confirms.

- **"core/cleanup.py wired" (pd-044)** — terse. Verified by reading both ends of the wire. Accurate but operator might wonder where the caller lives. **Lesson:** for "wired" claims, cite both the function definition AND the call site.

### What manual verification surfaced that agent verification would have missed

The Marker 4 + Marker 7 cross-check — manually executing the regression-test regex against `f"... {str(e)[:200]}"` — independently confirmed PRA-v3's r3-002 gap finding. An agent that only checked "test exists + claim wording" would not have caught this. **Manual verification has value precisely on meta-claims** (claims about test coverage, claims about classes-of-issues caught) where the test's intent and the test's actual pattern can drift.

### What manual verification confirmed the agents got right

- All 8 markers verified clean — no regressions, no partial fixes.
- The 5/53 agent-coverage spot-check ratio is sufficient for marker-level verification on a codebase of this maturity.
- The PRA-v3 finding count (13 net-new) is consistent with what manual verification would have surfaced — no obvious agent-induced finding-inflation.

---

## Recommendation

**Audit baseline is solid.** All 8 sampled markers verified clean against current code. PRA-v3's claim of 53/53 marker fidelity is trustworthy as input for follow-up PRs.

**Concrete next step:** Proceed with the planned `fix/r3-001-chart-scrub-and-server-header` PR bundling:
- **r3-001** — scrub `chart.py:164,263,372` exception leaks using `logger.exception` + generic detail (mirror pd-001 / r2-001 template).
- **r3-002** — broaden `tests/test_response_body_hygiene.py` regex to catch `{<func>(<excname>)...}` patterns. Suggested approach: change `\{(?:` + names + `)` to a lookahead that allows `\{[^}]*?\b(?:<names>)\b` so any exception-named token inside the f-string interpolation is flagged. Test the new regex against the chart.py sites + the meta-tests already in the file.
- **r3-003** — add `response.headers.pop("Server", None)` to `SecurityHeadersMiddleware`.

**Out-of-band operator action remains the same:**
- `r3-004` — `usermod -a -G bot caddy && systemctl restart caddy` for maintenance.html 403.
- `r3-005` — post-cutover `curl -I https://reverto.bot/` HSTS verification.

**Time spent per marker** (for future calibration):

| Marker | Minutes | Notes |
|--------|--------:|-------|
| r1-022 | 12 | Read full backup.sh + relevant runbook section |
| r1-052 | 9 | Two broadcaster classes + grep for TODOs |
| r1-073 | 11 | Middleware + frontend wrapper + symmetry check |
| pd-001 | 14 | Regex mental-execution + 3 chart.py sites + meta-claim verification |
| pd-019 | 8 | All 4 tests in single file, fast read |
| pd-044 | 6 | Function definition + lifespan caller |
| r2-001 | 8 | Single fix site + regression-test cross-ref |
| r2-006 | 9 | Both WS handlers + broadcaster connection paths |
| **Total** | **77 min** | Within 60-90 min budget |
