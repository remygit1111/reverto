# Audit v27 — Full Codebase Review

**Baseline**: commit `dde037d` (end of v26)
**Current HEAD**: commit `8b2f5c2` (one commit past the `cbd8251` reference
in the prompt; `8b2f5c2` only added `docs/audits/v27-backlog.md` B-03 —
no code delta, so the audit surface is identical)
**Delta**: 26 commits, 33 files, +3641 / -105 lines

**Status**: IN PROGRESS — Phase 1 of 6 complete. Findings-tracker
seed (`data/findings_seed.yaml`) extended to include Phase 1 items.
Per-finding STATUS blocks added below: 11 of 12 RESOLVED (v27-01..06,
v27-08..12), 1 OPEN (v27-07 — `style-src 'unsafe-inline'`
acknowledged-as-necessary, phase-out tracked separately). v27-01
was a pre-analysis correction — the v27 baseline narrative said
"PRE-EXISTING" but grep against HEAD showed the short-term fix
already in code. v27-09 + v27-12 closed by
`fix/v27-09-v27-12-defense-in-depth` (LoginBody character-class
regex + credential-redaction-before-truncation in `paper.errors`).

---

## Executive Summary

_Populated at Phase 6. Intentionally empty at this checkpoint._

---

## Phase 1 — Security & Authentication

### Summary

**12 findings: 0 HIGH, 5 MEDIUM, 4 LOW, 3 INFO.**

The delta since v26 is dominated by two changes with meaningful security
surface: the changelog JSON-API + admin CRUD
(`refactor/changelog-spa-integration`) and the two v26 fixes that closed
parity gaps in `_require_session` and the ML-pipeline config path. The
core auth primitives — cookie signing, session_epoch revocation, the
bcrypt flow, and the `_require_session` / `_request_user` pair — passed
this review cleanly. No HIGH findings surfaced.

The five MEDIUM findings all share a common theme: the codebase carries
a defensible single-tenant security model (one admin user, one shared
API key) into every corner of the auth stack, and multiple pieces of
that model **will need explicit plumbing before the first non-admin
user seed**. Four of the five MEDIUM findings are carry-overs from v26
(`v26-02`, `v26-16`, plus two aspects of the API-key bypass documented
only informally in v26). The fifth is a pragmatic-but-avoidable
supply-chain exposure on a third-party CDN.

LOW + INFO findings cover hygiene items, defense-in-depth gaps, and
carry-overs from v26 that did not regress but also did not close.

**Noticeably absent** (good): SQL-injection holes, YAML-unsafe-load
paths, DOM-XSS in SPA rendering, broken user-scoping on
`/api/bots/*` + `/api/deals/*`, login-timing enumeration. These were
all reviewed and found clean.

### Findings table

| ID | Severity | Title | Location | Classification |
|----|----------|-------|----------|----------------|
| v27-01 | MEDIUM | `_request_user` API-key fallback returns a hard-coded admin stub that bypasses `user.active` | `web/app.py:356-368` | PRE-EXISTING (Phase-3a design, undocumented risk) |
| v27-02 | MEDIUM | `/api/emergency-stop` has no admin role gate (v26-02 carry-over) | `web/routes/admin.py:102-152` | PRE-EXISTING (v26-02 still open) |
| v27-03 | MEDIUM | `StateBroadcaster.broadcast()` pushes cross-user payloads to every connected client (v26-16 carry-over) | `web/app.py:1686-1789` | PRE-EXISTING (v26-16 still open) |
| v27-04 | MEDIUM | Third-party CDN loaded without Subresource Integrity (SRI) | `web/static/index.html:1371`, CSP at `web/app.py:1141` | PRE-EXISTING |
| v27-05 | MEDIUM | CSRF posture relies solely on `SameSite=strict`; no defense-in-depth token or Origin header check | `web/app.py:165-214` (cookie flags), `web/routes/changelog.py` (admin writes) | NEW surface via changelog CRUD (pattern pre-existing) |
| v27-06 | LOW | OSError fallback in API-key bootstrap logs the full ephemeral key | `web/app.py:113-121` | PRE-EXISTING |
| v27-07 | LOW | CSP permits `style-src 'unsafe-inline'` + broad `connect-src ws: wss:` | `web/app.py:1139-1146` | PRE-EXISTING |
| v27-08 | LOW | No `Strict-Transport-Security` header emitted by the portal itself | `web/app.py:1134-1150` | PRE-EXISTING |
| v27-09 | LOW | `LoginBody.username` has no character-class restriction | `web/routes/auth.py:41-43` | PRE-EXISTING |
| v27-10 | INFO | Cookie payload carries `u` (username) that no longer flows into any auth decision | `web/app.py:236-241`, `_verify_session_cookie` | PRE-EXISTING (stale post-Phase-3a) |
| v27-11 | INFO | `audit.log` is a single cross-user file; no per-user segregation | `web/app.py:420-442` | PRE-EXISTING (Phase-3b scoping item) |
| v27-12 | INFO | `TickerError.message` truncation at 200 chars may still leak URL fragments with API-key tails to Telegram | `paper/errors.py:33-90`, `notifications/telegram.py:311-329` | PRE-EXISTING (v26-09 adjacent) |

### Detailed findings

#### v27-01 — API-key fallback returns hard-coded admin stub that bypasses `user.active`

**Severity**: MEDIUM
**Location**: `web/app.py:356-368` (`_request_user`), `core/user.py:51-65` (`DEFAULT_USER` / `get_default_user`)
**Classification**: PRE-EXISTING — documented as design in Phase-3a, but not flagged in v26

**Observation.** `_request_user` contains this branch for requests that
arrive with a valid `X-API-Key` but no session cookie:

```python
# web/app.py:362-367
provided = request.headers.get("X-API-Key")
if provided and secrets.compare_digest(provided, _API_KEY):
    return get_default_user()
raise HTTPException(status_code=401, detail="Not authenticated")
```

`get_default_user()` returns the module-level singleton
`DEFAULT_USER = User(id=1, username="admin", role="admin")` — hardcoded,
never consulted the DB. Two consequences follow:

1. **`user.active` is not enforced for API-key callers.** The post-v26-01
   fix in `_require_session` and the pre-existing check in `_request_user`
   both refuse deactivated users on the cookie path, but the API-key path
   skips straight past the DB lookup. An admin flipped to `active = 0`
   who still holds the shared `REVERTO_API_KEY` retains admin-equivalent
   access forever.
2. **Every admin-gated route is reachable with the API key.**
   `_require_admin_user` (`web/routes/changelog.py:45-57`) delegates to
   `_request_user` and checks `user.id != 1`. API-key callers land with
   `user.id == 1`, so the entire `/api/admin/changelog/*` surface — plus
   any future route wired through `_require_admin_user` — is accessible
   to any script holding the portal's single shared API key.

**Impact.**
- **Single-tenant today**: acceptable by design. `REVERTO_API_KEY` is
  the admin's automation credential; "admin key == admin" is consistent.
- **Multi-tenant (Phase-3b blocker)**: there is one API key for the
  whole portal. There is no per-user API-key concept. Any tenant who
  learns the key (operator slip, CI log capture, shared dev environment)
  gets admin across every tenant's bots, deals, credentials, and the
  changelog CRUD.
- **Key-rotation gap**: `REVERTO_API_KEY` has no per-user epoch-equivalent.
  Rotation requires setting a new env var + restarting the portal, which
  invalidates every script at once.

**Reproduction.** With the portal running:

```bash
# Deactivate admin in the DB.
sqlite3 logs/reverto.db "UPDATE users SET active=0 WHERE id=1"

# Cookie-auth path: blocked (post-v26-01).
curl -s -b reverto_session=$COOKIE http://localhost:8080/api/bots
# → 401 {"detail":"User not found"}

# API-key path: still works.
curl -s -H "X-API-Key: $REVERTO_API_KEY" http://localhost:8080/api/bots
# → 200 with the full bots listing.

# Admin-gated route via API key.
curl -s -H "X-API-Key: $REVERTO_API_KEY" \
     -H "Content-Type: application/json" \
     -X POST http://localhost:8080/api/admin/changelog \
     -d '{"title":"via api","description":"x","category":"feature"}'
# → 201 (admin CRUD happens as user_id=1).
```

**Fix direction.**
- **Short term**: in the API-key branch of `_request_user`, consult the
  DB for `id=1` instead of returning `DEFAULT_USER` unconditionally.
  Refuse when `user.active = 0`. One extra `get_user_by_id(1)` per
  API-key request is cheap.
- **Medium term** (Phase-3b): introduce per-user API keys stored in the
  `users` table (or a `user_api_keys` child table), hashed at rest,
  matched in constant time. Deprecate the module-global `REVERTO_API_KEY`.
- Document the current behaviour explicitly in `docs/security-model.md`
  until (1) lands — today it's implicit in the code comment.

**STATUS — RESOLVED (pre-analysis correction).** The v27 baseline
narrative said "PRE-EXISTING / Phase-3a design" but a grep against
HEAD shows the short-term fix already landed (cross-references
r1-001 / fix/r1-001-api-key-respects-active). `_request_user`'s
API-key branch now does `user_store.get_user_by_id(1)` and refuses
when `admin_user is None or not admin_user.active`
(`web/app.py:631-645`). The medium-term per-user API-key system
(Phase-3b) remains future work — tracked under r1-003 (Single
shared REVERTO_API_KEY, status: open).

---

#### v27-02 — `/api/emergency-stop` has no admin role gate

**Severity**: MEDIUM
**Location**: `web/routes/admin.py:102-152`
**Classification**: PRE-EXISTING — v26-02 still open

**Observation.** The handler has a rate limit and writes to the audit
log, but guards only on `_request_actor` (returns a string identifier
for logging) and does NOT require admin:

```python
# web/routes/admin.py:102-106
@router.post("/api/emergency-stop")
@limiter.limit("5/minute")
async def api_emergency_stop(
    request: Request, actor: str = Depends(_request_actor),
):
```

The body iterates `await registry.all()` (every bot across every user)
and sends SIGTERM to each running one. No `_require_admin_user`
dependency anywhere in the chain.

**Impact.** Any authenticated user can halt every bot across every
tenant. Today single-tenant, so no active exploit surface. The moment
a second (non-admin) user is seeded, that user becomes a kill-switch
for the admin's bots.

**Reproduction.** Seed a second user → log in as them → `curl -b`
with their session cookie → the emergency-stop fires. Covered by the
v26-02 audit narrative.

**Fix direction.** Add `user: User = Depends(_request_user)` +
`if user.id != 1: raise HTTPException(403)` — or, once Phase-3b role-
checks land, the single-line `_require_admin_user` swap.

**STATUS — RESOLVED (carry-over of v26-02).** Closed by the
fix/v26-02 branch — `/api/emergency-stop` now resolves the caller
via `Depends(_request_user)`, refuses non-admin with 403, and
emits a structured audit event with `result="denied"` so failed
attempts surface in `audit.jsonl`. See `docs/audits/v26-report.md`
v26-02 STATUS block for closure detail.

---

#### v27-03 — `StateBroadcaster.broadcast()` pushes cross-user payloads to every connected client

**Severity**: MEDIUM
**Location**: `web/app.py:1686-1789` (`StateBroadcaster` + `watch_state_files`)
**Classification**: PRE-EXISTING — v26-16 still open

**Observation.** `watch_state_files` scans every bot across every user
(`bots = await registry.all()` at line 1753) and broadcasts each
bot-state payload to every connected WS client via
`state_broadcaster.broadcast(payload)`. `StateBroadcaster._clients` is
a flat `set[WebSocket]` with no per-user filtering. The initial snapshot
handed out in `ws_state` IS user-scoped
(`registry.all(user_id=user_id)` at line 1807), but every subsequent
push is not.

Two `TODO(phase-3b, audit v26-16)` comments already flag this inline.

**Impact.** Under single-tenant: no data leak because only one user
exists. Under multi-tenant rollout: every WS client receives every
user's bot-state + summary payloads, including `total_pnl_btc`,
`balance_btc`, `open_deals` (with deal IDs, entry prices, PnL).

**Fix direction.** On `connect()`, store `user_id` from
`_ws_extract_user_id` alongside the WS. In `broadcast()` (and in
`watch_state_files` when it sends per-bot payloads), look up the bot's
owner and skip WS clients whose `user_id` doesn't match. Summary
broadcasts should be computed per-user rather than globally.

**STATUS — RESOLVED (carry-over of v26-16).** Closed by
fix/v26-16-ws-per-user-filter — both `LogBroadcaster` and
`StateBroadcaster` now record the connecting client's `user_id` and
filter at broadcast time. Cross-tenant regression tests assert that
a connection for user B does not receive frames produced by user A.
See `docs/audits/v26-report.md` v26-16 STATUS block.

---

#### v27-04 — Third-party CDN loaded without Subresource Integrity (SRI)

**Severity**: MEDIUM
**Location**: `web/static/index.html:1371`; CSP at `web/app.py:1139-1146`
**Classification**: PRE-EXISTING

**Observation.**

```html
<!-- web/static/index.html:1371 -->
<script src="https://unpkg.com/lightweight-charts@5.1.0/dist/lightweight-charts.standalone.production.js"></script>
```

No `integrity=` attribute, no `crossorigin=`. CSP at `web/app.py:1141`
explicitly whitelists `https://unpkg.com` under `script-src` — any
script served from that host is accepted.

**Impact.** The portal's chart rendering trusts whatever bytes unpkg.com
serves for that URL. Three plausible failure modes:

1. **Supply-chain compromise of lightweight-charts maintainer's npm
   account** → new major-version bump that re-uses the 5.1.0 tag is
   unlikely, but npm has history of maintainer takeovers; unpkg serves
   straight from npm.
2. **unpkg.com CDN compromise** → unpkg is a small-team-operated
   service; a compromised origin pushes malicious JS to every Reverto
   deployment on the next reload.
3. **MITM on a non-HTTPS portal deployment** → the script URL is
   HTTPS-pinned, so this requires a TLS certificate compromise against
   unpkg.com specifically, not the portal host.

Attacker JS in the portal context can read session cookies (they're
`HttpOnly` → can't read from JS, good), but can read + exfiltrate API
keys entered in the profile modal, submit changelog entries (SameSite
allows same-origin POST), and run arbitrary XHR against the portal's
own admin endpoints with the admin user's session.

**Fix direction.**
- Add `integrity="sha384-..."` + `crossorigin="anonymous"` on the
  `<script>` tag. Fetch the hash at the pinned version once, commit it.
- Alternatively: vendor the bundle into `web/static/vendor/` and drop
  the `https://unpkg.com` allowance from CSP. ~250 KB static file —
  negligible for a self-hosted portal.
- Either approach closes the supply-chain surface.

**STATUS — RESOLVED.** Every `<script src="https://unpkg.com/...">`
in `web/static/index.html` now carries `integrity="sha384-..."` +
`crossorigin="anonymous"`. The CSP `script-src` allowance for
unpkg.com is retained but is now SRI-gated — a tampered bytestream
would fail integrity verification before execution.

---

#### v27-05 — CSRF posture relies solely on `SameSite=strict`; no token or Origin check

**Severity**: MEDIUM
**Location**: `web/app.py:165-214` (cookie flags), every mutating
route (changelog CRUD, bots CRUD, deals, emergency-stop)
**Classification**: NEW surface via changelog admin CRUD; cookie
posture pre-existing

**Observation.** The portal has no CSRF middleware, no synchroniser
token, and no explicit Origin / Referer check on mutating endpoints.
The only defense against cross-site request forgery for cookie-auth
callers is `_COOKIE_SAMESITE = "strict"` (line 214) plus the implicit
browser preflight on `application/json` POST / PATCH / DELETE.

This works today but has two characteristics worth flagging:

1. **Single lever.** If `_COOKIE_SAMESITE` ever flips to `lax` (e.g.
   an operator sets it for IdP-redirect compatibility, or a test
   fixture leaks the override into production — the test fixture
   already does this for httpx/TestClient quirks, see v26-22 note),
   every cookie-auth POST endpoint that accepts `application/x-www-form-urlencoded`
   becomes CSRF-exploitable.
2. **New admin write surface.** The changelog refactor added POST /
   PATCH / DELETE routes under `/api/admin/changelog/*` that all
   require admin. These endpoints use `application/json` bodies →
   preflight required → no simple-form attack, assuming the endpoints
   stay JSON-only. But the general pattern of "admin writes protected
   only by SameSite" keeps expanding as new admin features land.

**Impact.**
- Today: low, because SameSite=strict + JSON-only endpoints blocks the
  classic cross-site form attack. The `/api/emergency-stop` endpoint
  is POST with no body → cross-site form submit would succeed IF the
  cookie rode along, but SameSite=strict drops it. That's the single
  backstop.
- Under any configuration drift on `_COOKIE_SAMESITE`: high exploit
  surface.

**Fix direction.**
- **Cheap**: add an explicit Origin header check on every mutating
  endpoint — refuse if missing or not matching the portal's own
  origin. One 5-line middleware; doesn't break curl/script callers
  (they'd set Origin themselves) or break same-origin SPA fetches.
- **Stronger**: double-submit CSRF token (issued on login, stored in
  a non-HttpOnly cookie or returned in `/auth/status`, echoed in an
  `X-CSRF-Token` header on every mutating request). Verified
  server-side. Standard pattern.
- Either makes the CSRF defense two-layered so a single config drift
  (or a future browser SameSite regression) doesn't flatten it.

**STATUS — RESOLVED.** `CSRFMiddleware` (web/app.py) implements the
double-submit pattern: a `reverto_csrf` cookie is minted on login,
and every mutating request (POST/PATCH/DELETE) MUST echo the value
in an `X-CSRF-Token` header. Cookie + header are compared in
constant time. The defence is now two-layered — a SameSite drift
no longer flattens it.

---

#### v27-06 — OSError fallback in API-key bootstrap logs the full ephemeral key

**Severity**: LOW
**Location**: `web/app.py:113-121`
**Classification**: PRE-EXISTING

**Observation.**

```python
# web/app.py:113-121
except OSError as e:
    # Last-resort fallback: if we genuinely can't write the file
    # the operator still needs the key, so log it. Should never
    # hit in practice — logs/ is writable on every supported host.
    logger.error(
        "REVERTO_API_KEY not set and could not write %s (%s). "
        "Ephemeral key (will be lost on restart): %s",
        _EPHEMERAL_API_KEY_FILE, e, _API_KEY,
    )
```

The `%s` at the end puts the full API key into `portal.log`. The
happy-path writes to `logs/.api_key_ephemeral` at mode 0600 and never
logs the key — exactly right. The OSError branch logs it directly into
a file with standard log rotation + a comment ("Should never hit in
practice") justifying the choice.

**Impact.** Log files propagate further than operators often remember
— `docker logs`, journald, cloud log shippers, support-case attachments.
A single OSError on `mkdir`/`write_text` exposes the key to every
downstream log consumer.

**Fix direction.** On OSError fall back to `print(..., file=sys.stderr)`
instead (or `logger.error` with the key replaced by its 8-char SHA-256
prefix for identification, and a stderr-only instruction to retrieve
it from the operator's out-of-band channel). Don't write the key into
a file that has log rotation.

**STATUS — RESOLVED.** The OSError branch in
`web/app.py:322-336` now logs only the sha256[:8] hint of the key
with an instruction to set `REVERTO_API_KEY` in `.env` and
restart. The full key never reaches `portal.log` and therefore
never reaches downstream log shippers.

---

#### v27-07 — CSP permits `style-src 'unsafe-inline'` + broad `connect-src`

**Severity**: LOW
**Location**: `web/app.py:1139-1146`
**Classification**: PRE-EXISTING

**Observation.**

```python
# web/app.py:1139-1146
response.headers["Content-Security-Policy"] = (
    "default-src 'self'; "
    "script-src 'self' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'"
)
```

Three items worth flagging:

1. `style-src 'unsafe-inline'` allows inline `style=` attributes and
   `<style>` blocks. `bleach` strips `style=` from rendered markdown,
   but the portal's own SPA rendering emits inline styles in places
   (e.g. `renderBotCard` templates). Removing the allowance would
   require refactoring those to classes. Mitigation-in-principle, not
   a live bug.
2. `connect-src 'self' ws: wss:` allows WebSocket connections to ANY
   host, not just `self`. An XSS gadget (defense-in-depth scenario
   given v27-04) could exfil via a WebSocket to an attacker host.
3. Missing directives: no `object-src 'none'`, no `base-uri 'none'`,
   no `form-action 'self'`. `default-src 'self'` backstops `object-src`
   implicitly but explicit is better; `base-uri` isn't covered by
   `default-src`.

**Impact.** Defense-in-depth reduction. Not a primary vulnerability.

**Fix direction.** Tighten:
- `connect-src 'self' ws://localhost:* wss://<portal-host>` (or
  document the `ws:`/`wss:` wildcard as an accepted trade-off).
- Add `object-src 'none'; base-uri 'none'; form-action 'self'`.
- Phase out `style-src 'unsafe-inline'` by moving inline styles to
  classes (bigger refactor, separate PR).

**STATUS — OPEN.** `style-src 'unsafe-inline'` remains in the CSP
because the SPA emits inline `style=` attributes in places
(`renderBotCard` templates and similar). An inline comment in
`web/app.py` acknowledges it as necessary; the phase-out is
tracked as a separate refactor — moving inline styles to classes —
not a hardening blocker. The `connect-src` and missing-directive
items remain to be addressed alongside.

---

#### v27-08 — No `Strict-Transport-Security` header emitted by the portal itself

**Severity**: LOW
**Location**: `web/app.py:1134-1150` (SecurityHeadersMiddleware)
**Classification**: PRE-EXISTING

**Observation.** The middleware adds `X-Frame-Options`,
`X-Content-Type-Options`, `Referrer-Policy`, and CSP — but no
`Strict-Transport-Security`. Deployments behind nginx/caddy with
HTTPS may add it at the proxy layer; the portal running directly (as
on Reverto-Dev during development) serves responses without the
header.

**Impact.** Low — a correctly-configured reverse proxy fixes this in
production. Development environments and any future direct-HTTPS
deployment (TLS-terminating uvicorn) would be missing the first-visit
downgrade protection.

**Fix direction.** Add `Strict-Transport-Security: max-age=31536000;
includeSubDomains` to the middleware output, gated on `X-Forwarded-Proto:
https` OR just emitted unconditionally and trusting the browser to
ignore it over plain HTTP. Minimal code change.

**STATUS — RESOLVED (audit r1-075).** `SecurityHeadersMiddleware`
emits `Strict-Transport-Security: max-age=31536000; includeSubDomains`
unconditionally — browsers ignore the header over plain HTTP, so
emitting it always is safer than gating on `X-Forwarded-Proto`
(which the portal can't trust without a per-deploy proxy contract).

---

#### v27-09 — `LoginBody.username` has no character-class restriction

**Severity**: LOW
**Location**: `web/routes/auth.py:41-43`
**Classification**: PRE-EXISTING

**Observation.**

```python
# web/routes/auth.py:41-43
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)
```

`username` accepts any characters — whitespace, control chars,
punctuation, emoji. The DB schema has `username TEXT NOT NULL UNIQUE`
with no character constraint; `verify_password` just does
`get_user_by_username(username)` which is a bind-parameter SQL lookup.

Today only `admin` exists; no user-creation endpoint exists yet. So
no real-world impact. But once user-signup / admin-add-user lands:

1. A username containing `\n` / `\r` / `\t` breaks the audit log's
   pipe-delimited format (`%(asctime)s | %(action)s | %(slug)s | %(key_hint)s`).
2. Username containing control chars can confuse later UI rendering
   (`_cachedUsername` goes into `document.body` via profile-initial
   rendering).
3. Case-sensitivity inconsistency: schema is UNIQUE case-sensitive,
   so `admin` and `Admin` would be two distinct rows — confusing UX.

**Fix direction.** Add a character-class regex + lowercase normalisation
when the signup / add-user endpoint lands. `^[a-z0-9_\-]{3,32}$` is
the safe default. Not urgent today because no write-path creates new
usernames outside the `init_db()` seed.

**STATUS — RESOLVED in `fix/v27-09-v27-12-defense-in-depth`.**
`LoginBody.username` now carries `pattern=r"^[a-zA-Z0-9_.-]+$"` —
the same character class `core.user_store.validate_username` has
enforced at INSERT time since r1-032/r1-007, so every incumbent
row is compatible (pinned by the
`test_existing_users_match_new_pattern` regression). The login
boundary now refuses whitespace, control chars, emoji, ASCII
homoglyphs, and SQL-injection-shaped payloads with a 422 before
the handler runs. `ChangePasswordBody` has no username field — the
endpoint resolves the user from the session cookie via
`_request_user` — so no parallel pattern is needed there.

---

#### v27-10 — Cookie payload carries `u` field that no longer drives auth decisions

**Severity**: INFO
**Location**: `web/app.py:236-241` (`_create_session_cookie`),
`web/app.py:279` (`_verify_session_cookie` — uses only `uid` + `ep`)
**Classification**: PRE-EXISTING — Phase-3a legacy

**Observation.** `_create_session_cookie` writes:

```python
return _session_serializer.dumps({
    "uid": user.id,
    "u": user.username,
    "iat": int(time.time()),
    "ep": user_store.get_session_epoch(user.id),
})
```

`_verify_session_cookie` then uses `uid` to re-lookup the user + epoch,
and the auth decision is made entirely from the DB row (in `_request_user`).
`u` is only echoed back to `/auth/status` and used in `_request_actor`
as a display name for audit lines.

If an admin renames a user (not currently supported, but plausible
post-signup-flow), every existing cookie keeps the stale username
baked in. Audit logs written during the transition will attribute
actions to the old name. Information-level; not an auth bypass.

**Fix direction.** When a rename API lands, either (a) bump the user's
`session_epoch` so cookies force re-issue, or (b) drop `u` from the
cookie payload and look it up per request from the DB. (a) is cheaper.

**STATUS — RESOLVED (audit r1-006).** Option (b) was chosen — `u`
is no longer in the cookie payload (`web/app.py:516-520`), only
`uid` / `iat` / `ep` survive. Every read-path resolves the User
from `uid` via `user_store.get_user_by_id`. Pre-fix cookies that
still carry both `u` and `uid` continue to validate because the
reader only requires `uid`.

---

#### v27-11 — `audit.log` is a single cross-user file

**Severity**: INFO
**Location**: `web/app.py:420-442` (`_audit_logger` bootstrap)
**Classification**: PRE-EXISTING — Phase-3b scoping item

**Observation.** `_audit_logger` writes to `logs/audit.log` with no
user separation. Every tenant's lifecycle events (bot start/stop,
login, password-change, changelog CRUD, emergency-stop) land in one
shared file.

**Impact.** Single-tenant: not a problem. Multi-tenant: tenants
shouldn't be able to see other tenants' event timelines via the audit
log. Assuming the log is operator-only (not exposed over HTTP), this
is an operational segregation question, not a web-security one.

**Fix direction.** When multi-tenant lands, either split per-user log
files (`logs/<user_id>/audit.log`) OR add a `user_id` field to every
audit line and filter at read time. Second option is simpler and
preserves cross-user admin visibility for compliance queries.

**STATUS — RESOLVED (audit r1-031, refined Phase-A wrap-up).**
Both options were taken: the central `logs/audit.log` (legacy pipe
format) and `logs/audit.jsonl` (JSONL with `user_id` / `ip` /
`result` fields, Phase-A) preserve cross-user admin queries; a
per-user split also lands at `logs/<user_id>/audit.jsonl` whenever
the caller passes `user_id`, so per-tenant audit pulls are a
single file read.

---

#### v27-12 — `TickerError.message` truncation at 200 chars may leak URL fragments to Telegram

**Severity**: INFO
**Location**: `paper/errors.py:33-90` (`_MESSAGE_CHAR_CAP = 200`),
`notifications/telegram.py:311-329` (`_resolve_error_reason`)
**Classification**: PRE-EXISTING — v26-09 adjacent

**Observation.** `classify_exception` truncates `str(exc)[:200]` into
`TickerError.message`. `_resolve_error_reason` then puts the first 80
chars into the Telegram "Reason" line for generic errors:

```python
# notifications/telegram.py:328-329
head = err.message[:80].replace("\n", " ")
return f"{cls}: {head}" if head else cls
```

ccxt exceptions have historically embedded the request URL into the
message string. For authenticated endpoints ccxt uses headers, not
query strings, so API keys should not appear. **But** some endpoints
(e.g. `fetchTicker` on public markets) may echo the URL back; if ccxt
ever changes a subset of its error formats to include
`apiKey=...&signature=...` tails, the 200-char truncation is wide
enough that a URL tail could surface on Telegram.

v26-09 addressed the direct `response.text` leak. This is the
exception-message path, which is a different surface.

**Impact.** Defence-in-depth only. No confirmed ccxt behaviour that
leaks keys via exception text at time of review; the risk is that
future ccxt upgrades change format and the truncation isn't
url-pattern aware.

**Fix direction.** In `classify_exception`, strip anything that looks
like a URL (`https?://[^\s]+`) before truncating. Cheap belt-and-braces.
Alternatively: keep only the error class name + status code in the
Telegram Reason line for unknown error classes, and rely on
`portal.log` for the free-form message.

**STATUS — RESOLVED in `fix/v27-09-v27-12-defense-in-depth`.**
`paper.errors._redact_secrets` now runs against `str(exc)` BEFORE
the 200-char truncation. Patterns target three credential shapes:
query-string params (`apiKey=…`, `signature=…`, `secret=…`,
`passphrase=…`, `sign=…`), `Authorization: Bearer …` headers, and
JWT-shape (three base64 segments dot-separated). Param-name is
preserved on the `key=value` shape (`apiKey=[REDACTED]`) so error
context stays useful for debugging while the secret payload is
gone. Critical ordering — redaction-first guarantees that a
credential at position 150+ does not survive into Telegram via the
truncation cap. End-to-end test (`test_classify_exception_strips_
credentials_through_full_path`) drives a NetworkError carrying a
URL with both `apiKey=` and `signature=` through the full
`classify_exception` path and asserts neither value reaches
`TickerError.message`.

---

### Items verified clean (not findings, review coverage)

The following were actively checked and passed review:

- **Login timing-attack enumeration**: `verify_password` returns `None`
  uniformly for every failure mode (missing user, inactive, NULL hash,
  wrong password); `auth_login` adds a 100 ms `asyncio.sleep` before
  the 401 and returns `"Invalid credentials"` identically (`web/routes/auth.py:53-61`).
  Error path is single-branch, no timing signal.
- **SQL injection**: every f-string SQL in `core/*.py` was inspected
  — `core/deal_store.py:475`, `core/deal_store.py:654`,
  `core/changelog_store.py:180`, `core/database.py:358`. All
  interpolate only hardcoded column names or fixed lists built from
  module-level constants (`_BACKTEST_COLS`, `_OWNED_TABLES`); values
  are always `?`-bound.
- **Path traversal in bot slugs**: `_BOT_SLUG_RE = r"^[A-Za-z0-9_\-]+$"`
  enforced on every `{slug}` route (see `web/routes/bots.py:215` and
  callers). Slugs never reach `Path()` without this validation.
- **YAML loading**: 100 % of call sites use `yaml.safe_load`. No
  `yaml.load(..., Loader=FullLoader)` anywhere in the repo tree.
- **Markdown XSS**: `core/markdown_render.py` runs markdown-it with
  `html=False` (raw HTML in markdown is escaped to text) followed by
  bleach with an explicit tag / attribute / protocol allow-list.
  `test_markdown_render.py` covers `<script>`, inline `style=`,
  `<iframe>`, `onerror`, `javascript:`, `data:`, `<form>`.
- **SPA innerHTML writes**: every instance in `web/static/app.js`
  either (a) interpolates hardcoded template strings + user-controlled
  values wrapped in `safeText()`, (b) uses `fmtPnl` / `fmtPrice` which
  coerce via `Number(...)`, or (c) emits server-sanitised
  `description_html` from the changelog (line 2044, commented explicitly).
- **User-scoping on `/api/bots/*`**: every endpoint resolves via
  `registry.get(user.id, slug)` or `registry.all(user_id=user.id)`.
  No cross-user drift in the read-path.
- **WebSocket auth**: `_ws_extract_user_id` is the cookie-only WS
  equivalent of `_request_user`; checks `uid`, DB lookup, `active`.
  No API-key path for WS (correct — headers don't travel on WS
  upgrades).
- **HttpOnly / Secure cookies**: both flags set; `Secure` only drops
  to False when `REVERTO_INSECURE_COOKIES=1` (explicit operator
  opt-in).
- **`itsdangerous.BadSignature` / `SignatureExpired` handling**:
  `_verify_session_cookie` catches both explicitly and uses a broad
  `except Exception` as a defensive backstop — no cookie corruption
  can escape as a 500.
- **Rate-limit coverage**: all auth + admin + changelog mutating
  routes carry `@limiter.limit`. `/auth/logout` at 10/min (v26-04
  fixed); `/auth/login` at 5/min.
- **CSRF on API-key callers**: `X-API-Key` header-only path is
  CSRF-resistant by construction (headers don't auto-attach
  cross-origin without a preflight).

### Notes for later phases

Items surfaced during this sweep that are not security-primary but
should be picked up in Phase 2 (data integrity / concurrency) or
later:

- **Phase 2 candidate**: `_audit_logger` writes to `audit.log` with a
  `RotatingFileHandler(backupCount=3)`. Under multi-tenant load the
  5MB/3-rotation window may rotate away forensic evidence faster than
  an investigator can fetch it. Concurrency is not the issue; retention
  is. Integrity-adjacent but not strictly concurrency.
- **Phase 2 candidate**: `_bitget_client` is a module-level ccxt
  instance (`web/app.py:401`) serialised by `_price_lock` (alluded to
  in v26 report, v26-25). Single-writer-at-a-time means the /api/price
  endpoint is a throughput choke-point but also means a slow Bitget
  response holds the lock and can queue other price requests. Integrity
  OK, availability concern.
- **Phase 2 candidate**: `session_epoch` UPDATE → SELECT was addressed
  in v26-11 via SQLite `RETURNING`. Confirm the whole `bump_session_epoch`
  path still uses the single-statement variant under real-world
  concurrency (two tabs logging out simultaneously).
- **Phase 3-4 candidate**: `v27-03` (WS state broadcaster) and
  `v27-02` (emergency-stop) can be fixed together as part of the
  multi-user readiness sweep. Pair with `v27-01` short-term fix so
  the DB is the single source of truth for user state across every
  auth path.
- **Phase 5 / UX**: `docs/security-model.md` doesn't document the
  "API-key == admin stub" behaviour (v27-01). Worth a paragraph
  addition independent of the code fix.

### Files reviewed

**Auth + middleware (deep)**:
- `web/app.py` (lines 1-450, 1130-1270, 1580-1810 — auth primitives,
  middleware, WS broadcasters)
- `web/routes/auth.py` (all 150 lines)
- `web/routes/admin.py` (all 160 lines, re-reviewed vs v26)
- `web/routes/changelog.py` (all 243 lines — new in delta)

**Backend primitives (targeted)**:
- `core/user.py`
- `core/user_store.py` (re-reviewed — post v26-03 + v26-01 fixes)
- `core/changelog_store.py`
- `core/markdown_render.py`
- `core/database.py` (migration paths + FK / SQL patterns)
- `core/deal_store.py` (f-string SQL audit only)
- `core/paths.py` (user-scoping contract)

**Notifications / error surfacing**:
- `notifications/telegram.py` (secret-leak channels)
- `paper/errors.py` (TickerError + message truncation)
- `paper/paper_engine.py` (error-notification call sites, lines 275-285, 735-800, 960-1030)

**Frontend (targeted on innerHTML + CDN + auth-state)**:
- `web/static/index.html` (header, nav, CDN reference)
- `web/static/app.js` (auth-state caching, innerHTML writes,
  rendering helpers)

**Scripts + config**:
- `scripts/setup_admin.py` (post-v26-03)
- `config/config_loader.py` (YAML loading)

**Test coverage cross-reference**:
- `tests/test_web_routes.py` — auth fixtures + admin-check parity
- `tests/test_changelog_api.py` — CRUD + auth gates
- `tests/test_markdown_render.py` — XSS regression surface
- `tests/test_user_store.py` — active / epoch / bcrypt
- `tests/test_setup_admin.py` — password policy

Not reviewed in Phase 1 (scheduled for later phases):
- `paper/state_io.py`, `paper/paper_state.py` — Phase 2 (concurrency)
- `strategies/`, `ml/` (beyond `nightly_pipeline.py`) — Phase 3-4
- `exchanges/`, `live/` — Phase 3 (trading flow)
- `backtest/` — Phase 4

---

_Phase 2 (Data integrity & concurrency) TBD._
_Phase 3 (Exchange integration + trading) TBD._
_Phase 4 (Engine + strategies) TBD._
_Phase 5 (Operator UX + docs) TBD._
_Phase 6 (Summary + prioritisation) TBD._
