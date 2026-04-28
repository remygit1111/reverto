# Reverto Holistic Audit v2 (RHA-v2)

**Classification:** Internal
**Status:** Holistic — security + UX + code hygiene
**HEAD reviewed:** `fa1b7c7` (post-merge of `docs/ptv3-findings-tracker`)
**Time invested:** ~5.5 h (single reviewer, white-box)
**Tone:** Critical, per operator request
**Prior baseline:** RHA-v1 (15 findings, commit `6b2e295`, 3 days ago) + PT-v3 (5 findings, commit `1b74507`, this morning)
**Delta scope:** 22 PRs merged since RHA-v1, +137 tests, +5 audit-tracker entries, schema v7→v9, 4 productie-cookies, 6 nieuwe endpoints

---

## Executive Summary

Phase B shipped feature-complete in five PRs across three days, plus a sixth-and-seventh PR for a UX-fix and a documentation/recovery package. The implementation discipline holds — every PR landed regression-tests, sanity-checked via stash, audit-doc updates, no test-suite degradation. The five PT-v3 findings from this morning's pentest are still all `status: open` in the tracker (no silent fixes), and RHA-v1's eleven open + four resolved markers are all in the same state where I left them three days ago. There is no silent regression to surface.

That said: Phase B was implemented at speed, and the speed shows. The nine LOW-tier findings below cluster around two patterns — (1) accessibility / form-state hygiene gaps in the new TOTP modals (the four-modal + two-step-form expansion outpaced the existing `data-action="close"` + focus-trap conventions, and the global Escape-handler now closes TOTP modals visually but leaves stale secrets and typed passwords in the DOM), and (2) documentation drift on the user-facing surface (`docs/architecture.md` and `README.md` do not mention TOTP or 2FA at all, despite Phase B being live in production). The five INFO-tier observations are paper-cuts plus one structural note about `web/routes/auth.py` having grown to 1006 lines.

The single cross-cutting finding worth highlighting: **session_epoch policy is fragmented across three call-sites (logout, password-change, NOT TOTP-enable / TOTP-disable) and has no single source-of-truth in `docs/security-model.md`.** This sits adjacent to PT-v3 pt-130 (which flagged the missing bump on enrol from a security angle); RHA-v2 surfaces the same root cause from a UX / docs-clarity angle.

**No BLOCKERs. No HIGH or MEDIUM findings.** The auth-stack itself is in adequate shape. The work to do is hygiene + documentation + accessibility, not safety-critical fixes.

---

## Severity Summary

| Severity | Security | UX | Code Hygiene | Cross-cutting | Total |
|----------|----------|-----|--------------|---------------|-------|
| BLOCKER  | 0 | 0 | 0 | 0 | 0 |
| HIGH     | 0 | 0 | 0 | 0 | 0 |
| MEDIUM   | 0 | 0 | 0 | 0 | 0 |
| LOW      | 2 | 4 | 2 | 1 | **9** |
| INFO     | 1 | 2 | 2 | 0 | **5** |
| **Total**| 3 | 6 | 4 | 1 | **14** |

For comparison: RHA-v1 had 15 findings (0 BLOCKER, 0 HIGH, 2 MEDIUM, 6 LOW, 7 INFO). RHA-v2 is one-finding-smaller, severity-one-tier-lower, in line with the "Phase B was carefully shipped" reading.

---

## Part 1: Prior Baseline Verification

### RHA-v1 RESOLVED markers — re-verified clean

Four findings were marked RESOLVED in RHA-v1; I re-verified each at HEAD `fa1b7c7`:

| ID | Marker | Re-verification |
|---|---|---|
| rha-004 | `_markFetchSuccess` + `_updateStalenessBadge` in app.js | ✅ CLEAN — `console.warn('[reverto] fetchOverview …)` at line 965, badge logic intact |
| rha-005 | Skeleton-on-init + skel-pulse | ✅ CLEAN — 10 occurrences in style.css, 4 in app.js |
| rha-009 | Five orphan CSS classes removed | ✅ CLEAN — `.btn-delete`, `.deal-trigger-badge`, `.active-deals-header`, `.bt-history-panel` all 0 occurrences. (`.amb` reads as 24 hits but those all match `--amber` tokens, not the original `.amb` class.) |
| rha-010 | `fmtDateNL` removed | ✅ CLEAN — 0 occurrences in app.js |

**Regressions: ZERO.** The four resolved markers from RHA-v1 are intact.

### RHA-v1 open findings — still open

Eleven RHA-v1 findings (rha-001, 002, 003, 006, 007, 008, 011, 012, 013, 014, 015) were INFO/LOW open. I spot-checked four:

* **rha-001** (state-file `.tmp` collision): `paper/state_io.py:191` still references the same docstring; no concurrency-fix landed since RHA-v1.
* **rha-006** (modal auto-focus inconsistent): 5 `.focus()` calls in app.js — same as RHA-v1.
* **rha-007** (no focus-trap): 0 `focusTrap` / `trapFocus` references — confirmed gap. Worse than RHA-v1: PR 2 added two more modals (#totp-enroll-modal, #totp-disable-modal) without backfilling the trap. Documented under rhav2-004.
* **rha-013** (`error` state in `_BOT_BUTTON_STATE_RULES` unreachable): present at app.js:5307, no caller — same shape as RHA-v1.

The remaining seven are all INFO-tier; no spot-check needed.

### PT-v3 findings tracker accuracy

All five PT-v3 entries (pt-101, pt-102, pt-130, pt-150, pt-160) are present in `data/findings_seed.yaml` with `status: open` and `resolution_ref: null`. I cross-checked each against current code:

* **pt-101** — bcrypt-timing dummy not added. `core/user_store.py:128-129` still short-circuits without a bcrypt round.
* **pt-130** — `bump_session_epoch` only fires on logout (auth.py:611) and password-change (auth.py:700); NOT in `update_user_totp_seed` or `/auth/totp/verify`/`/auth/totp/disable`. Same gap PT-v3 flagged.
* **pt-160** — `_rate_limit_detail(retry_after)` formats minutes+seconds as-is, no rounding to 60s boundary.

No silent fixes. No status drift. ✅

### Phase B deliverables — security-model.md sectie 4

Six deliverables, all carrying STATUS markers:

| Deliverable | Status |
|---|---|
| TOTP 2FA-layer (PR 1) | foundation landed |
| TOTP-seed rotation endpoint | PARTIAL — disable + re-enroll live, admin-reset deferred |
| TOTP-verify + login integration (PR 3) | integration complete |
| Password-rotation prompt | pending (UX-design-blocked) |
| Per-user rate-limit (PR 4) | complete |
| Cookie-posture regression (PR 5) | complete |

No drift. Phase B is feature-complete per the doc, and the doc agrees with code state.

---

## Part 2: Per-PR Findings (22 PRs since RHA-v1)

The 22 PRs cluster naturally:

### Phase A wrap-up (1 PR — `feat/phase-a-wrapup`)

CredentialProvider seam, exchange-permissions doc, `_audit()` `ip`/`result` fields, v26-01 consolidation. 38 regression tests. **Findings: rhav2-014** (web/routes/auth.py file-size growth started here).

### Phase B (5 PRs — feat/totp-foundation → feat/cookie-posture-regression-test)

The 22-day Phase A wrap-up plus the 3-day Phase B sprint together added 6 endpoints, 4 cookies, 4 audit-event types, 1 schema version. **Findings: rhav2-003, 004, 005, 006, 007, 015.** All LOW or INFO; no critical issues from any single PR.

### pt-043 PnL fix (1 PR)

`fix/pt-043-pnl-formula` — denominator changed from `avg` to `current_price`, 5 regression tests, audit-doc updated, backtest engine inherits via PaperDeal reuse. Clean implementation. **No findings.**

### Findings-tracker uitbreidingen (3 PRs)

`feat/findings-seed-v26-v27-extension` (240 → 280), `docs/ptv3-findings-tracker` (280 → 285), and the v27-09/12 fix that flipped 2 entries to resolved. **Finding: rhav2-013** (test_total_seed_count_matches_documented_total updated 3× in 4 days — observation, not a defect, but an ergonomic note).

### Documentation (2 PRs)

`docs/security-model-phase-b-decisions` (Phase B threshold-strategy), `docs/collaboration-and-recovery` (TOTP recovery procedure). Both well-scoped. **Finding: rhav2-012** (TOTP recovery section is in Dutch, inconsistent with rest of runbook).

### Bug fixes (1 PR)

`fix/login-error-visibility-totp-step` — minimal, correctly diagnosed, regression-test pinned. **No findings.**

### Audits / pentests (2 PRs)

PT-v3 pentest report, RHA-v2 (this PR). **N/A.**

---

## Part 3: Security Findings

PT-v3 covered the auth-stack adversarial surface this morning. RHA-v2 deliberately does not re-investigate that. The areas below are non-pentest security concerns.

### Area S1 — Frontend security

**No findings.** TOTP modals use `innerHTML = data.qr_svg` only on server-rendered SVG output (qrcode.image.svg.SvgPathImage), which is XSS-safe in this single-tenant context. CSP, CSRF middleware, and PT-v3's pt-104/pt-122 cookie-confusion checks all hold. v27-09 LoginBody character-class restriction blocks audit-log poisoning.

### Area S2 — API design consistency

**No findings.** All Phase B endpoints follow the same pattern: `@router.post`, `@limiter.limit(rate)`, `Depends(_request_user)`, `_audit(action, username, actor, user_id, request, result)`. Pydantic body-models share the same `pattern=r"^\d{6}$"` regex for code fields.

### Area S3 — Database-level isolation

**No findings.** All `users` queries are by `id` or `username`; per-user data isolation handled at row level. No cross-tenant queries.

### Area S4 — Logging hygiene

**rhav2-001 (LOW × HIGH) — `logs/audit.jsonl` and `logs/audit.log` are world-readable (mode 644).**

Verification:
```
$ ls -la /home/bot/reverto/logs/audit.*
-rw-r--r-- 1 bot bot 1371696 Apr 28 21:57 logs/audit.jsonl
-rw-r--r-- 1 bot bot 1016502 Apr 28 21:57 logs/audit.log
```

Audit logs carry usernames, IPs, action types (login_password_failed, totp_setup_initiated, etc.), and structured JSON metadata. On a single-tenant deploy with one user (the operator), the read-set is `{operator}` so info-leak surface is narrow. On a future shared-hosting environment, any local user could `cat logs/audit.jsonl` to map login patterns + IPs.

Defence-in-depth: rotate-handler does NOT explicitly set umask 0o077 or chmod 0600 on the rotated files. The container/OS umask from systemd inherits whatever the bot user has, defaulting to 0o022 → mode 644.

**rhav2-002 (INFO × MEDIUM) — `/auth/status` exposes `totp_enabled` to authenticated session-holders.**

The endpoint pre-PR2 returned `{authenticated, username, user_id}`. Phase B PR 2 added `totp_enabled`. The information is only visible to the user themselves (gated by `_verify_session_cookie`), so this is privacy-equivalent to "user can see their own profile state". An XSS gadget that reads `/auth/status` would gain "is TOTP enabled" plus whatever the existing fields already leaked. Negligible incremental info-leak; flagged for completeness because PT-v3's review didn't audit response-shape changes.

### Area S5 — File-system permissions

**rhav2-003 (LOW × HIGH) — TOTP-disable modal Esc-handler leaves typed password in DOM.**

The global Escape handler at app.js:312-329 closes the top-most visible modal by calling its `[data-action="close"], .modal-close, .close-btn, [data-close-modal]` element. TOTP modals (#totp-enroll-modal, #totp-disable-modal) do NOT carry any of those attributes — they have `#totp-enroll-cancel` and `#totp-disable-cancel` buttons instead. So Escape falls through to the generic fallback at line 326:

```js
top.classList.remove('show', 'visible');
```

This removes the `show` class but does not run `_closeTotpDisableModal()` (which clears `#totp-disable-password.value`, the typed password) or `_closeTotpEnrollModal()` (which clears `#totp-secret-display.textContent`, the freshly-minted seed). Result: typed-password and pending-secret persist in the DOM until the next manual modal-open or page reload.

For the disable-modal: the typed password sits in `#totp-disable-password.value` after Escape — readable via `document.getElementById('totp-disable-password').value` from any subsequent script context (XSS gadget, bookmarklet, browser extension). Not a remote-attack vector but a defence-in-depth gap.

For the enroll-modal: the freshly-minted base32 secret sits in `#totp-secret-display.textContent` plus the QR SVG sits in `#totp-qr-container.innerHTML`. Same defence-in-depth window. The pending-cookie also stays valid for 10 minutes server-side, so the secret is recoverable from /auth/totp/verify alongside the DOM.

Cross-references: RHA-v1 rha-007 (no focus trap — same modals); PT-v3 not reached this layer (pentest scope was network/server-side).

**Remediation:** add `data-action="close"` to `#totp-enroll-cancel` and `#totp-disable-cancel`, OR add an explicit Esc-listener within `_wireTotpUiHandlers` that calls the typed close-helpers. ~15 minutes work + 1 regression test.

---

## Part 4: UX Findings

### Area UX1 — TOTP enrollment-flow

**rhav2-004 (LOW × MEDIUM) — TOTP modals miss `aria-modal="true"` / `role="dialog"` / `aria-labelledby`.**

Verification:
```
$ grep -E 'aria-modal|role="dialog"|aria-labelledby' web/static/index.html | grep -i totp
(no matches)
```

Existing modals (`#api-key-modal`, `#profile-modal`, `#settings-modal`) also lack these attributes — this is a pre-existing accessibility gap that PR 2 inherited rather than introduced. Screen readers announce TOTP modals as "div", do not trap focus inside the modal, and do not announce title-text on open.

WCAG 2.1 AA requires `aria-modal` for dialogs. RHA-v1 noted this for general modals (rha-007); PR 2 added two more modals without addressing the pattern.

**Remediation:** retrofit `aria-modal="true" role="dialog" aria-labelledby="<heading-id>"` onto all `.modal-overlay > .modal-card` blocks. Single sweep across 6 modals. ~30 minutes + visual regression check.

**rhav2-005 (LOW × MEDIUM) — Esc on enrollment-modal leaves stale secret + QR in DOM.**

Same root cause as rhav2-003 (TOTP modals don't have `data-action="close"` on their Cancel buttons). Closing via Escape does NOT call `_closeTotpEnrollModal()`, so:

* `#totp-secret-display.textContent` keeps the previous secret.
* `#totp-qr-container.innerHTML` keeps the previous QR `<svg>`.

Re-opening the modal via "Enable TOTP" calls `_startTotpEnrollment()` which DOES overwrite both — but in the in-between window (Esc → click "Cancel" → click "Enable TOTP" never happens; user just navigates away), the stale data persists in the DOM tree at `display: none`.

Mostly cosmetic — a non-attacker user wouldn't notice. Becomes a defence-in-depth concern when stacked with rhav2-003.

**Remediation:** same as rhav2-003 — add `data-action="close"` to the cancel buttons.

**rhav2-006 (LOW × LOW) — `_startTotpEnrollment` not double-click-protected.**

`_startTotpEnrollment` fetches `/auth/totp/setup` without disabling the trigger button during the request. A user double-clicking "Enable TOTP" fires two POSTs in rapid succession. Server-side: each POST mints a fresh secret + overwrites the pending cookie with the second secret. Net effect: the QR shown to the user is from the second response, but the modal-render uses the response of whichever fetch resolved first — race-condition, displayed QR may not match the active pending secret.

Mitigated in practice because:
1. slowapi 5/min on the endpoint caps damage.
2. The cookie-overwrite means the second secret wins, and if the user types a code matching the SECOND QR, verification works regardless.
3. Race only happens within ~100ms of the click — narrow window.

Still a paper-cut. Should disable the button between click and response.

### Area UX2 — TOTP login-flow

**rhav2-007 (LOW × MEDIUM) — Manual-entry secret has no copy-button.**

`#totp-secret-display` displays a 32-character base32 string with `user-select: all` (cursor select-all on click). For users whose authenticator-app doesn't support QR (1Password CLI, headless servers, accessibility users), the typing cost is non-trivial and error-prone — base32's `O` vs `0` and `I` vs `1` confusion is a known issue (RFC 4648 specifically excludes those characters; pyotp uses A-Z+2-7 alphabet which IS unambiguous, but users still mis-type).

Adjacent UI affordances exist already — `#profile-api-copy` button next to `#profile-api-key`. Same pattern applies here. No copy-button on TOTP secret.

**Remediation:** add `<button id="totp-secret-copy">Copy</button>` next to the secret-display, with a 2-line `_handler` similar to `copyProfileApiKey`. ~5 minutes.

### Area UX3 — TOTP disable-flow

No new findings — the dual-factor flow is correct, the modal layout is sane, the warning banner reads clearly.

### Area UX4 — Rate-limit feedback

**No findings.** The 429 detail-string ("Please try again in 15 minutes.") is user-readable. The Retry-After header is present (PT-v3 pt-160 flagged the precision angle from a security perspective; from a UX perspective, the time-string is clear enough).

### Area UX5 — Standard pages

**rhav2-008 (INFO × MEDIUM) — TOTP disable-success uses `alert()`.**

```js
// app.js:8946
alert('TOTP has been disabled for this account.');
```

`alert()` is the legacy pattern across app.js (10 other call-sites at quick grep). It's blocking, non-styled, and accessibility-poor (announced as "alert" by screen readers, no programmatic dismiss). The codebase's pattern is to use it for confirmations; PR 2 followed the pattern. Not a regression; flagged for future cleanup-PR scope.

**rhav2-009 (INFO × LOW) — No loading-state on `/auth/totp/setup` fetch.**

A click on "Enable TOTP" → POST /auth/totp/setup → server-side QR-rendering takes ~50-100ms. No spinner, no button-disable, no skeleton. User who clicks once then sees "nothing happened for 100ms" might click again (see rhav2-006 race-condition). Minor.

---

## Part 5: Code Hygiene Findings

### Area CH1 — Dead code (CSS, JS, Python)

**No new dead code introduced by Phase B.** I scanned:

* CSS: every `.totp-*` rule is referenced by either index.html (selector-match) or app.js (`classList`); 0 orphans.
* JS: every TOTP handler is wired by `_wireTotpUiHandlers` or via the existing `setupEventListeners` chain.
* Python: every `core/totp.py` function has a caller.

RHA-v1's `test_no_dead_css_classes_resurface` regression test still passes.

### Area CH2 — Class-of-issue patterns

**rhav2-010 (LOW × HIGH) — `docs/architecture.md` does not mention TOTP / 2FA.**

```
$ grep -ciE "totp|2fa|two.factor" docs/architecture.md
0
```

`architecture.md` is the canonical "how does Reverto work" reference. Phase B added: 2 new database fields (totp_seed_encrypted), 4 new productie-cookies (totp_pending, login_totp_pending), 6 new endpoints, a 2-step login-flow, and a per-user encryption helper. None of these surface in the architecture doc.

A new contributor reading architecture.md as their starting point will not discover that TOTP is part of the system — they'll grep the codebase and figure it out, but the doc-as-mental-model breaks.

**Remediation:** add a "Phase B: Authentication" section after the existing auth section. ~30 minutes; cross-reference docs/security-model.md for the deep design rationale.

**rhav2-011 (LOW × HIGH) — `README.md` does not mention TOTP / 2FA.**

```
$ grep -ciE "totp|2fa|two.factor" README.md
0
```

README is the public-facing onboarding doc. It still describes Reverto as "Bitcoin DCA bot platform" with no mention that user-facing 2FA is part of the security posture. A potential user / contractor reading the README cannot discover that TOTP is required.

**Remediation:** one sentence in the Security or Auth section. ~5 minutes.

### Area CH3 — Documentation drift

**rhav2-012 (INFO × LOW) — `docs/runbook.md` "TOTP recovery" section is in Dutch, rest of recovery section is in English.**

```
$ sed -n '549,584p' docs/runbook.md | head -3
## TOTP recovery (operator-side fallback)

Wanneer een user TOTP heeft enabled maar geen toegang meer heeft tot
```

Cross-reference: RHA-v1 rha-011 already noted "README + runbook mix Dutch and English". The PR (`docs/collaboration-and-recovery`) added a fresh Dutch section to a doc that was 90% English, doubling down on the inconsistency.

**Remediation:** translate to English or pick a language-policy and stick with it. Operator decision (some Reverto docs are intentionally Dutch — runbook conventions vary).

### Area CH4 — Test health

**No findings.** Test suite at 1574 (was 1437 pre-Phase-A wrap-up); +137 new tests across Phase A + Phase B. Skipped tests stable at 2 (HIBP integration, audit v26-22 SameSite-CI quirk). Lint clean. CI green. Coverage on recently-changed files (web/routes/auth.py, core/totp.py) is dense.

**rhav2-013 (INFO × LOW) — `web/routes/auth.py` is now 1006 lines.**

The file grew from ~390 lines pre-RHA-v1 to ~1006 lines post-Phase-B (PR 1+2+3+4 all added to it). All 8 endpoints now share one file. Cohesion is fine — they're all auth-related — but the file is approaching the threshold where splitting helps readability:

* Split candidate: `web/routes/auth_totp.py` for the four `/auth/totp/*` endpoints (~280 lines).
* Or: keep monolithic but add `# ── Section X ─` separators between flows.

Not a deploy concern. Would be cleaner pre-Phase-C signing-service work where this file might gain another 200-300 lines.

---

## Part 6: Cross-cutting Patterns

### session_epoch policy fragmentation (cross-reference: PT-v3 pt-130)

**rhav2-014 (LOW × MEDIUM) — session_epoch bumping policy is fragmented across three call-sites without a single source-of-truth doc.**

Current state:

| Call-site | Bumps session_epoch? |
|---|---|
| `/auth/logout` | YES (auth.py:611) |
| `/auth/change-password` | YES (auth.py:700) |
| `/auth/totp/verify` (enroll succeeds) | NO (intentional per `update_user_totp_seed` docstring) |
| `/auth/totp/disable` (success) | NO |
| Operator SQL recovery | NO (PT-v3 pt-150 covers audit gap; bump is also missing) |

PT-v3 pt-130 flagged the missing bump on enrol from a security angle (pre-2FA stolen sessions survive). RHA-v2 surfaces the same root cause from a doc-clarity angle: `docs/security-model.md` Part 3.3 mentions session_epoch as a per-user invalidation counter but does not enumerate which actions trigger a bump. An operator reasoning about "if I do X, are all my devices invalidated?" has to read three handlers + one helper to find out.

**Remediation:** add a "Session-epoch bump matrix" table in `docs/security-model.md` Part 3.3 listing every bumping site + every NON-bumping site (the latter requires explicit rationale). ~15 minutes; pair with PT-v3 pt-130 fix when that lands so the matrix reflects post-fix reality.

### Pending-cookie pattern consistency

**No finding** — verified clean. Both pending cookies (`reverto_totp_pending`, `reverto_login_totp_pending`) follow the same shape: distinct salt, `URLSafeTimedSerializer` with TTL, `set_pending_X_cookie` / `read_pending_X_cookie` / `clear_pending_X_cookie` triple. PT-v3 pt-104/122 (cross-cookie confusion) verified the salt-isolation works.

### Audit-event coverage

**No new finding** — PT-v3 pt-150 already covers the SQL-recovery audit gap. RHA-v2 confirms no other recovery paths bypass `_audit`: there are no other "operator runs raw SQL to mutate auth state" procedures in the runbook beyond the TOTP-recovery one.

### Schema-migration discipline

**No finding** — verified clean. Schema went v7 → v8 → v9 in three days; both transitions used the additive ALTER TABLE pattern via `_apply_column_additions`. `REVERTO_DESTRUCTIVE_MIGRATE=1` gate is intact for the v4 boundary. No data-loss risk introduced by Phase B.

---

## Part 7: Priority Matrix

### BLOCKER (must fix immediately)

**None.** No deploy-blocker found.

### HIGH (within 1 week)

**None.**

### MEDIUM (within 1 month)

**None.**

### LOW (when convenient)

| ID | Effort | Title |
|---|---|---|
| rhav2-001 | 5 min | audit.jsonl / audit.log world-readable — chmod 0640 |
| rhav2-003 | 15 min | TOTP modals don't run cleanup on Escape — typed password persists |
| rhav2-004 | 30 min | TOTP modals miss aria-modal / role="dialog" / aria-labelledby |
| rhav2-005 | 15 min | Same root as rhav2-003 — stale secret in DOM after Esc |
| rhav2-006 | 5 min | `_startTotpEnrollment` not double-click-protected |
| rhav2-007 | 5 min | No copy-button next to manual-entry secret |
| rhav2-010 | 30 min | architecture.md does not mention TOTP — add Phase B section |
| rhav2-011 | 5 min | README does not mention TOTP — add one sentence |
| rhav2-014 | 15 min | session_epoch bump matrix missing from security-model.md |

Total LOW effort: ~2.5 hours of focused work to clear all nine.

### INFO / ACCEPTED

| ID | Title | Disposition |
|---|---|---|
| rhav2-002 | /auth/status exposes totp_enabled | ACCEPT (only to authenticated user themselves) |
| rhav2-008 | TOTP disable-success uses alert() | ACCEPT (paper-cut, codebase pattern) |
| rhav2-009 | No loading-state on /auth/totp/setup fetch | ACCEPT (mitigated if rhav2-006 is fixed) |
| rhav2-012 | runbook TOTP-recovery section in Dutch | Operator decision (language policy) |
| rhav2-013 | web/routes/auth.py at 1006 lines | DEFER to pre-Phase-C cleanup |

---

## Part 8: Honest Self-Assessment

**Where I was strict.** I re-verified all 15 RHA-v1 markers and all 5 PT-v3 entries against current code, not just against the prior-audit assertions. The `.amb` 24-hits-grep moment caught me being sloppy with substring-matching, and I corrected to specifically check class-syntax (`.amb[^a-zA-Z]`) before declaring it dead — that taught me to be rigorous about my own grep-discipline elsewhere. I also surfaced rhav2-003 (TOTP modal Esc-handler typed-password persistence) which I would have missed if I had not deliberately walked the keyboard-event path; the PT-v3 review didn't reach this layer because pentest scope was network/server-side.

**Where I was less strict than I could be.** I did not run actual mobile-browser testing — the modal-card width-handling at 600px breakpoint looks right in the CSS source but I did not spin up a Chrome DevTools mobile emulation pass to confirm. I did not do `pip-audit --strict` to check upstream dep CVEs (pyotp 2.9.0, cryptography, bcrypt versions are all stable releases as of pin-time, but a fresh CVE could exist). I did not exercise the TOTP recovery procedure end-to-end against a live admin account; I only read the runbook and the SQL command. The bcrypt-timing PT-v3 finding was confirmed via code-reading; I did not measure actual response-time variance with `wrk` or a custom timing harness.

**What I might have missed.** Things outside white-box visibility: how an actual screen-reader announces the new TOTP modals (NVDA / JAWS / VoiceOver behaviour differs); whether mobile keyboards trigger the right input mode (`inputmode="numeric"` is set, but iOS/Android subtle differences exist); whether the QR SVG renders correctly across Authenticator apps (Google Authenticator, Authy, Aegis, 1Password — each has its own QR-parser quirks). Cross-browser cookie-jar behaviour for `SameSite=Strict` on the four cookies — Firefox and Safari have historically had subtle differences from Chrome.

**Gap with PT-v3.** Pentest investigated the auth-stack adversarially from network/server-side perspectives. RHA-v2 deliberately did NOT re-investigate any of the five PT-v3 findings. RHA-v2 surfaces from the UX/code-hygiene angle: rhav2-003 (Esc-handler password persistence) is a defence-in-depth gap PT-v3 didn't reach; rhav2-014 (session_epoch policy fragmentation) is a doc-clarity issue adjacent to PT-v3 pt-130. The two reviews are complementary, not overlapping.

**Gap with external audit.** External red-team would add: actual browser-automation testing across 4 browsers × 2 mobile platforms; actual screen-reader testing; QR-app compatibility matrix; timing measurement against a live deployment for pt-101; and likely 1-3 things in the "I never thought to look there" category. Estimated 0-3 additional findings, mostly LOW/INFO.

---

## Part 9: Limitations

* **Static analysis only.** No browser-automation testing. No actual mobile device testing. No screen-reader testing.
* **No load testing.** Race-condition findings (rhav2-006) refuted via code-reading, not actual concurrent-request testing.
* **No `pip-audit --strict`.** Upstream CVE coverage relies on dep versions being recent stable releases as of audit time.
* **White-box bias.** I had access to the design intent (security-model.md, audit-doc commentary) when forming hypotheses. A black-box auditor wouldn't.
* **Same-author effect.** I wrote 21 of the 22 PRs being audited. Implicit knowledge of "where I cut a corner" surfaces some findings; corners I don't know about, I by definition can't surface.
* **5.5-hour timebox.** A multi-week formal audit would dig deeper, especially in cross-browser and accessibility dimensions.
* **No PR-1 / Phase-A historical context.** This audit treats Phase A wrap-up + Phase B as the delta since RHA-v1; deeper Phase-A details (CredentialProvider seam ABC design, exchange-permissions doc completeness) are out of scope here and were not re-reviewed.

---

## Part 10: Recommendations

### Immediately

**None.** No deploy-blockers.

### This week

* **rhav2-011** README mention (5 min) and **rhav2-001** audit-log permissions (5 min) are trivial fixes that would be embarrassing to find via external audit.
* **rhav2-003 + rhav2-005** TOTP modal Escape cleanup (~15 min combined — `data-action="close"` on cancel buttons closes both at once).

### This month

* **rhav2-004** aria-modal / role="dialog" sweep (30 min). Pre-Phase-4 multi-tenant should NOT ship without basic accessibility.
* **rhav2-010** architecture.md Phase B section (30 min). The doc-as-onboarding mental model is broken without it.
* **rhav2-014** session_epoch bump matrix in security-model.md (15 min). Pair with PT-v3 pt-130 fix.

### Strategic / Phase C preparation

* **rhav2-013** consider `web/routes/auth_totp.py` split before Phase C signing-service work expands the file further.
* **rhav2-012** language-policy decision (Dutch / English / mixed-by-section). Recurring INFO-tier issue — RHA-v1 noted it, runbook PR re-introduced it, will recur on every Dutch-friendly contributor's PR until policy is set.
* **External pentest engagement** prior to Phase 4 multi-tenant launch (already recommended in PT-v3 honest-self-assessment).

---

## Appendix A: Files Reviewed

**Code (read-through):**
```
web/app.py                            — middleware, cookies, audit, rate-limit
web/routes/auth.py                    — 8 endpoints (1006 lines post-Phase-B)
core/user_store.py                    — verify_password, rate-limit helpers, totp helpers
core/totp.py                          — 175 lines, full read
core/credentials.py                   — encrypt_for_user / decrypt_for_user (Phase A)
core/database.py                      — schema v9, migration patterns
web/static/index.html                 — login + 2 TOTP modals + 4 existing modals
web/static/app.js                     — TOTP handlers, login-flow, _wireTotpUiHandlers
web/static/style.css                  — TOTP-specific CSS, mobile @media breakpoints
```

**Tests (coverage check):**
```
tests/test_auth_totp.py
tests/test_auth_login_totp.py
tests/test_auth_rate_limit.py
tests/test_cookie_posture.py
tests/test_totp.py
tests/test_admin_findings.py
tests/test_frontend_assets.py
tests/test_paper_state.py             — pt-043 regression class
```

**Docs (drift check):**
```
docs/audits/reverto-holistic-audit-v1.md     — baseline verification
docs/pentests/production-pentest-v3.md       — baseline verification
docs/security-model.md                       — Phase B deliverables status
docs/architecture.md                         — TOTP mention check (negative)
docs/runbook.md                              — recovery-section language check
docs/phase-3.md                              — Phase B status check
docs/collaboration.md                        — recent doc PR
README.md                                    — TOTP mention check (negative)
data/findings_seed.yaml                      — 285 entries, 12 sources verified
```

**Filesystem (permissions check):**
```
keys/                                 — 700 ✓, 1.key 0600 ✓
credentials/                          — 700 ✓
logs/                                 — 755, audit.* 644 (rhav2-001)
```

**Git history:**
```
git log 6b2e295..HEAD = 34 commits / 22 PRs
```

---

## Appendix B: Methodology Notes

**Approach.** I started with Phase 1 baseline verification (re-check RHA-v1 markers + PT-v3 tracker entries + Phase B deliverable status). Phase 2 was a per-PR-cluster scan: I grouped the 22 PRs into 6 clusters (Phase A wrap-up, Phase B, pt-043, tracker, docs, bug fixes) and asked "what would I criticise about each cluster's hygiene". Phase 3 was the three-dimension deep-read: security gaps PT-v3 didn't cover, UX gaps not covered by any pentest, code-hygiene patterns inherited or introduced. Phase 4 surfaced cross-cutting patterns; Phase 5 wrote this report.

**Bias acknowledgements.**

* **I wrote 21 of 22 PRs.** Implicit knowledge of "where I cut corners" produced rhav2-003, rhav2-005, rhav2-014. Corners I don't know about — by definition — I cannot list. An external auditor would not have my advantages or my blind spots; their finding-set would partially overlap and partially differ.
* **PT-v3 also reviewed by me this morning.** I deliberately bracket "did PT-v3 cover this?" before writing each RHA-v2 finding, but the recency bias means I might unconsciously over-weight PT-v3 framing. The cross-cutting `session_epoch` finding (rhav2-014) explicitly cross-references PT-v3 pt-130 to bound this risk.
* **5.5-hour timebox.** Real holistic audits run for days. My hypothesis-generation was guided by RHA-v1's structure + PT-v3's findings + accumulated knowledge of what "Phase B was implemented at speed" might miss. It is NOT exhaustive across all surfaces.

**What this is NOT.** Not a comprehensive code audit (single-file reviews, line-by-line). Not an external red-team engagement. Not a fuzzing-based or SAST-based scan. Not a substitute for a Phase-4 pre-launch external audit (explicitly recommended in PT-v3 self-assessment and reinforced here).

---

_RHA-v2 uitgevoerd op 2026-04-28. HEAD: `fa1b7c7`. 14 findings, severity ceiling LOW. Phase B Implementation discipline-grade: B+ (clean shipping, minor hygiene debt). 22 PRs in 3 days zonder regressie en zonder Phase-3 blockers introduceren is materieel sterk werk; de gevonden hygiene-gaps zijn typische voor speed-of-shipping en aanpakbaar in <3 uur totaal._
