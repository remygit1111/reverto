# Audit v26 Report

**Date:** 2026-04-20
**Scope:** Complete codebase audit, severity-gecategoriseerde findings. Delta-aware ten opzichte van v25 (`audits/audit_v25.md`, 2026-04-19). Branch-HEAD at audit time: `0ef39a7` (main post-Phase-3a merge).
**Methodology:** Category-by-category inspectie met file-referenties. Geen score-percentage; severity-counts only.

## Summary

| Severity | Count |
|----------|-------|
| HIGH     | 1     |
| MEDIUM   | 8     |
| LOW      | 10    |
| INFO     | 7     |

**Most critical findings (top 5):**

1. **HIGH** — `/api/emergency-stop` calls `stop_bot(bot.slug)` with signature mismatch → TypeError caught silently, no bots stopped (`web/routes/admin.py:123`).
2. **MEDIUM** — Schema v4 migration is destructive without an operator-confirmation gate in `init_db()`; a single `make start` after upgrade wipes deals/orders/annotations/backtest_runs (`core/database.py:255-283`).
3. **MEDIUM** — `_require_session` dependency skips the `user.active` check that `_request_user` enforces, so a deactivated user with a still-valid epoch can hit `/api/auth/change-password` (`web/app.py:256-261`).
4. **MEDIUM** — `/api/emergency-stop` performs no role check; any authenticated user can SIGTERM every bot across every tenant (`web/routes/admin.py:102-136`).
5. **MEDIUM** — Post-Phase-3a operator step `make setup-admin` is not documented in `docs/runbook.md` or `README.md`; new operators will hit 401 on first login with no signpost (`docs/runbook.md:84-102`).

**Delta sinds v25:**

- **13 v25 findings RESOLVED** (11 closed in the audit-v25-backlog-sweep + Phase-3a, 2 closed in separate PRs earlier).
- **1 v25 finding STILL OPEN**: v25 #4 DB + `state.json` dual-source (architecturaal; re-filed below as INFO-i1).
- **12 NEW findings** introduced or surfaced post-v25 (Phase-3a delta or previously un-reviewed).
- **7 PRE-EXISTING findings** that v25 did not flag but remain relevant.

See **Appendix A** for the per-v25-finding close-out.

---

## 1. Security & Authentication

**Context.** Session-auth is now fully DB-backed via `core.user_store` (Phase-3a). Cookies carry a signed payload with `uid`, `u`, `iat`, `ep`; verify does a per-user DB epoch-check. Password hashes are bcrypt rounds=12, set via `scripts/setup_admin.py` (provisioning) or `/api/auth/change-password`. API-key header (`X-API-Key`) remains as an admin-equivalent path for scripts. Rate limiting via SlowAPI is per-IP.

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-01 | MEDIUM | `_require_session` does not check `user.active`, breaking parity with `_request_user` | `web/app.py:256-261` | NEW (Phase-3a split of helpers) |
| v26-02 | MEDIUM | `/api/emergency-stop` has no role gate | `web/routes/admin.py:102-136` | PRE-EXISTING (Phase-3 scoping item per `docs/phase-3.md §2`) |
| v26-03 | MEDIUM | Password-length policy inconsistent between setup-admin (10) and change-password (8) | `scripts/setup_admin.py:43`, `web/routes/auth.py:119` | NEW (Phase-3a) |
| v26-04 | LOW | `/auth/logout` has no rate-limit decorator | `web/routes/auth.py:81` | PRE-EXISTING |
| v26-05 | LOW | `_create_session_cookie` username-string fallback mints a `uid=-1` cookie (dead defensive branch — login never reaches it) | `web/app.py:195-206` | NEW (Phase-3a) |
| v26-06 | LOW | `save_encrypted` / `load_encrypted` / `_system_fernet` are dead code post-Phase-3a (no caller) | `core/credentials.py:226-253` | NEW (Phase-3a) |
| v26-07 | LOW | `setup_admin.py` treats `REVERTO_ADMIN_PW=""` as set, skips interactive prompt, and then fails length check | `scripts/setup_admin.py:61-62` | NEW (Phase-3a) |
| v26-08 | INFO | Rate limiter keyed on `get_remote_address` without X-Forwarded-For parsing; behind a reverse proxy all traffic shares one bucket | `web/app.py:1165` | PRE-EXISTING (comment acknowledges) |
| v26-09 | INFO | Telegram error path logs `response.text` which could theoretically echo the bot token if the upstream error format ever changes | `notifications/telegram.py:80` | PRE-EXISTING |

### v26-01 — `_require_session` does not check `user.active`

**Wat.** `_require_session` is the FastAPI dependency used by `/api/auth/change-password` (via `Depends(_require_session)`). It calls `_verify_session_cookie` and returns the payload on success. `_verify_session_cookie` checks signature, TTL, per-user epoch — but does **not** dereference the users row to confirm `active == 1`. `_request_user` (the dependency used by every other route) **does** do that check.

**Waarom.** An admin deactivating a user by flipping `users.active = 0` will not immediately invalidate that user's cookie — the deactivation only takes effect on the next login or if someone bumps their `session_epoch`. Between flag and effect, a deactivated user can still call `/api/auth/change-password`. Scope is narrow because only one endpoint uses `_require_session`, but the inconsistency is the risk: a future endpoint added via `Depends(_require_session)` inherits the same gap.

**Waar.**
```python
# web/app.py:256-261
def _require_session(request: Request) -> dict:
    """FastAPI dependency — reject if the caller has no valid session cookie."""
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    return payload
```

**Remediation.** Fold `_require_session` into `_request_user` (return a `User` instance) or add an `active` check inside `_verify_session_cookie` so both call-paths share the same gate. Deactivation should also call `bump_session_epoch` to cut existing cookies immediately — document that invariant.

**STATUS — RESOLVED (Phase-A wrap-up).** First closed in two passes: an interim active-check was added inside `_require_session` itself (preserving the helper's signature). Phase-A wrap-up then deleted `_require_session` entirely and migrated `/api/auth/change-password` to `Depends(_request_user)`, eliminating the parity surface — there is now exactly one auth dependency that does the active-check, so the recommended remediation is realised in full. Regression: `tests/test_web_routes.py::TestInactiveUserRejected::test_change_password_rejects_inactive_user`.

### v26-02 — `/api/emergency-stop` has no role gate

**Wat.** `docs/phase-3.md §2` explicitly calls out that emergency-stop is intentionally admin-cross-user, and UI-side "je stopt bots van N users" is planned for Phase-3. The current route (`web/routes/admin.py:102`) guards only on `_request_actor` which returns a username string — no role enforcement. Under the single-user deployment this has no exploit surface; under Phase-3b it becomes every tenant's kill-switch on every other tenant's bots.

**Waarom.** Pre-condition for multi-user rollout. Not a launch-blocker today (single tenant) but must land before the first non-admin user is seeded.

**Waar.**
```python
# web/routes/admin.py:102-136
@router.post("/api/emergency-stop")
@limiter.limit("5/minute")
async def api_emergency_stop(
    request: Request, actor: str = Depends(_request_actor),
):
    ...
    for bot in await registry.all():
        ...
```

No `user: User = Depends(_request_user)` and no `if user.role != "admin": raise HTTPException(403)`.

**Remediation.** Add `user = Depends(_request_user)` + role assertion. Consider keeping `registry.all()` unscoped (the behaviour matches the Phase-3 spec), but surface the affected-user count in the response so the frontend can build the "you are about to stop bots from N users" dialog.

**STATUS — RESOLVED.** `/api/emergency-stop` now resolves the caller via `Depends(_request_user)` and refuses with 403 when `user.role != 'admin'`; non-admin attempts are emitted as a `WARNING` portal-log line and (Phase-A wrap-up) as a structured audit event with `result="denied"` so failed attempts are traceable. `registry.all()` remains unscoped per the Phase-3 spec.

### v26-03 — Password-length policy inconsistent

**Wat.** `scripts/setup_admin.py:43` requires `_MIN_PW_LEN = 10`. `web/routes/auth.py:119` requires `len(body.new_password) < 8`. An operator can set a 10-char password via setup_admin, then change it to an 8-char password via the portal.

**Waarom.** Mild — 8 is already above the trivially-weak floor. But an audit policy tightening one path and not the other is the kind of drift that bites once audit requirements harden. And a reader of the code can't tell which number is authoritative.

**Waar.**
```python
# scripts/setup_admin.py:43
_MIN_PW_LEN = 10

# web/routes/auth.py:119
if len(body.new_password) < 8:
    raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
```

**Remediation.** Move `_MIN_PW_LEN = 10` (or a project-wide `MIN_PASSWORD_LENGTH`) into `core.user_store`; both call-sites import it. Update the error message to reference the constant so future bumps propagate.

### v26-04 — `/auth/logout` missing rate limit

**Wat.** Every other auth endpoint carries `@limiter.limit(...)`. Logout does not, likely because it's idempotent. But an authenticated caller can spam it to drive `bump_session_epoch` UPDATEs on their own row.

**Waarom.** DoS surface is small (only the attacker's row is touched) and the endpoint requires a valid session cookie via `_verify_session_cookie` — so this is defense-in-depth, not a live attack. LOW.

**Remediation.** Add `@limiter.limit("10/minute")` mirroring `/api/auth/change-password`.

### v26-05 — Username-string fallback in `_create_session_cookie`

**Wat.** Phase-3a kept the legacy string-shaped caller path so `auth_client` test fixtures don't break. If the username doesn't resolve to a User, the function mints a cookie with `uid=-1`. `_verify_session_cookie` rejects `cookie_uid <= 0`, so the cookie is dead-on-arrival.

**Waarom.** Pure dead code. A real login always goes through `verify_password` which returns a User. Tests call `_create_session_cookie("admin")` on the seeded admin (resolves). The `uid=-1` branch has no observable consumer.

**Waar.**
```python
# web/app.py:194-206
else:
    user = user_store.get_user_by_username(str(username_or_user))
    if user is None:
        return _session_serializer.dumps({
            "uid": -1,  # <-- dead branch
            ...
        })
```

**Remediation.** Drop the branch and raise `ValueError("unknown username")`. Or narrow the signature to only accept `User`.

### v26-06 — Dead Fernet-blob helpers

**Wat.** `core.credentials.save_encrypted` / `load_encrypted` / `_system_fernet` / `_load_or_create_system_key` were the Fernet wrappers for `.auth.json`. Post-Phase-3a, the admin password lives in `users.password_hash` (bcrypt). Nothing production-side calls these helpers; only `tests/test_credentials.py:202-218` still exercises them.

**Waarom.** Code hygiene. Retaining them preserves the ability to write other encrypted-blob files later, but the docstrings still reference `.auth.json` as their active use case — misleading to a reader.

**Remediation.** Either remove (plus the two tests), or rename to `encrypt_system_blob` / `decrypt_system_blob` with a docstring noting they're reserved for future encrypted state files.

### v26-07 — Empty `REVERTO_ADMIN_PW` env-var UX

**Wat.** `os.environ.get("REVERTO_ADMIN_PW")` returns `""` when the var is set to the empty string, passes the `if password is None:` guard, and then falls through the length check with a generic `ERROR: password must be at least 10 characters`. A less-common footgun is `REVERTO_ADMIN_PW=` in a shell script that produces an empty value.

**Remediation.** `if not password:` instead of `if password is None:`, so an empty env-var falls through to the interactive prompt.

### v26-08 — Rate limiter IP-only (INFO)

Already documented inline at `web/app.py:1165`. Behind an nginx/caddy reverse proxy every request looks like it comes from `127.0.0.1`, which collapses all users into a single rate-limit bucket. Phase-3b task to add X-Forwarded-For parsing with a trusted-proxy allowlist.

### v26-09 — Telegram error-log response.text (INFO)

Upstream Telegram's JSON error shape currently doesn't echo the bot token. If they ever add one (e.g. "url parsing failed: https://api.telegram.org/bot<token>/..."), the error line in `logs/portal.log` would capture it. Defense-in-depth: log `response.status_code` only, or redact the URL pattern before emission.

---

## 2. Database & Persistence

**Context.** SQLite at `logs/reverto.db` with WAL mode, busy_timeout=5000, per-thread connection cache. Schema v4 (post-Phase-3a) adds `password_hash` / `role` / `session_epoch` on `users`. Owned tables (`deals`, `orders`, `chart_annotations`, `backtest_runs`) FK-reference `users(id)`. Migration path for `current < SCHEMA_VERSION` is destructive drop-and-recreate.

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-10 | MEDIUM | Schema v4 migration auto-runs destructively on `make start`; only a WARNING log alerts operator | `core/database.py:255-283` | NEW (Phase-3a introduced v4) |
| v26-11 | LOW | `bump_session_epoch` uses UPDATE-then-SELECT instead of `RETURNING`; concurrent bumps can return stale value | `core/user_store.py:131-151` | NEW (Phase-3a) |
| v26-12 | INFO | `_write_lock` serialises all deal/order writes across threads (throughput ceiling, not a correctness issue) | `core/deal_store.py:32` | PRE-EXISTING |

### v26-10 — Schema v4 destructive migration without confirmation gate

**Wat.** `_migrate_schema` inspects `PRAGMA user_version`; if below `SCHEMA_VERSION` it logs a WARNING and then drops every owned table before recreating. `init_db()` is called automatically on portal startup. An operator who pulls main and runs `make start` on an existing v3 DB loses all deals/orders/annotations/backtest_runs. The Phase-3a commit messages document the backup-first SLA but the code itself has no refusal path.

**Waarom.** Operator error is the typical production risk; this path silently destroys history if the operator forgets the backup. MEDIUM because the runbook does document it — but the runbook isn't what runs on boot. v25 already flagged the same pattern for the 0→3 migration as intentional; Phase-3a doubles down on "destructive is fine if you have backups".

**Waar.**
```python
# core/database.py:255-283
if current < SCHEMA_VERSION:
    logger.warning(
        "Schema migration: dropping owned tables from v%d and "
        "recreating at v%d. ...",
        current, SCHEMA_VERSION,
    )
    for table in _OWNED_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
```

**Remediation.** Gate the drop behind an env-var (`REVERTO_ALLOW_DESTRUCTIVE_MIGRATION=1`) or an idempotent sentinel file that `scripts/reset_db.py` creates as a side-effect of its backup step. Without the gate, `_migrate_schema` refuses the drop and exits with a clear error pointing at the runbook.

**STATUS — RESOLVED.** `_migrate_schema` now refuses to drop owned tables unless `REVERTO_DESTRUCTIVE_MIGRATE=1` is set in the environment (`core/database.py:_DESTRUCTIVE_OPT_IN_ENV`), raising `DestructiveMigrationBlocked` with a runbook pointer. The runbook + `scripts/reset_db.py` flip the gate around their backup ceremony; an operator who forgets the backup gets a hard stop instead of a wiped DB. Pre-existing-data probe (`_has_existing_owned_data`) means fresh installs never trigger the guard.

### v26-11 — `bump_session_epoch` non-atomic return

**Wat.** The helper runs `UPDATE ... SET session_epoch = session_epoch + 1`, commits, then runs a separate `SELECT session_epoch FROM users WHERE id = ?`. A concurrent bump from another thread can land between the two, so the returned integer does not necessarily equal "the value I wrote".

**Waarom.** Consumers currently do not rely on the return value beyond logging (`web/routes/auth.py` discards it). If a future caller uses the number for optimistic-locking, the race bites. LOW.

**Remediation.** Use `UPDATE ... RETURNING session_epoch` (SQLite supports it since 3.35). Or merge into one statement and fetch in the same connection+transaction.

### v26-12 — `_write_lock` throughput ceiling (INFO)

One module-level `threading.Lock()` serialises every write across every thread (portal async workers + engine notify threads + test fixtures). WAL mode would allow concurrent writers at the SQLite level, but Python-side we hold the lock before the DB ever sees the transaction. Not a correctness concern — but a parity-test with 20 bots writing DCA updates will bottleneck here before the DB does. Measure before acting.

---

## 3. Exchange Integration & Trading

**Context.** `exchanges/base_exchange.py` defines the abstract surface; `exchanges/bitget.py` and `exchanges/kraken.py` wrap ccxt. Bitget is the Phase-1/live target; Kraken is in maintenance. `_with_order_retries` provides the idempotency-retry pattern (pre-check via `fetch_open_orders` by `clientOrderId`).

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-13 | INFO | ccxt pinned at 4.5.48; no documented upgrade cadence or regression-test for wrapper compatibility | `requirements.txt:12` | PRE-EXISTING |

No findings in this category beyond the informational note. Error handling is tight, credentials do not leak in log paths (no `logger.<fn>` sees `api_key` / `api_secret` / `passphrase`), and the idempotency retry design is sound.

### v26-13 — ccxt upgrade cadence (INFO)

`ccxt==4.5.48` is a specific point release. ccxt publishes weekly; exchange-side API drift surfaces quickly. There is no CI job that smoke-tests the Bitget wrapper against a sandbox, and no documented policy on when to bump. Not a finding per se — just a watch-item for the Phase-3 deployment runbook.

---

## 4. Paper Engine & State

**Context.** `paper/paper_engine.py` (1550 lines) owns the tick loop, DCA/TP/SL logic, deal-ID retry, sentinel handling, and guard wiring. `paper/paper_state.py` is the in-memory state model; `paper/state_io.py` handles atomic `.state.json` writes.

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-14 | INFO | DB + `state.json` dual-source-of-truth remains (v25 #4 carry-over) | `core/deal_store.py` + `paper/paper_engine._write_state` | PRE-EXISTING (v25 #4) |

Phase-3a did not touch this architectural item, and v25 flagged it as Phase-3-scoping work. The audit v25 backlog sweep landed the `_db_create_deal_with_retry` contract test (commit `967c2b2`), which pins the current invariant: in-memory state mutates only after DB persist succeeds. That is the best mitigation available inside this architecture.

### v26-14 — DB + `state.json` dual-source (INFO, carry-over)

No change since v25. Re-filed at INFO rather than MEDIUM because v25 already identified it and the current implementation includes the mitigations needed (retry-on-IntegrityError, contract test). Fix belongs in Phase-3c/d scoping when the engine can migrate to a single source.

---

## 5. Live Engine & Reconciliation

**Context.** `live/live_engine.py` inherits from PaperEngine; real orders still raise NotImplementedError (Phase-3 gate). `live/order_reconciliation.py` has a scaffolded `OrderReconciler` with `fetch_order` branch commented out. `main_live.py` requires `BITGET_PASSPHRASE` env-var; exchange keys come from the encrypted per-user store.

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-15 | INFO | Real-order path still `NotImplementedError`, reconciler `fetch_order` still commented out | `live/live_engine.py:281`, `live/order_reconciliation.py:~120` | PRE-EXISTING |

No new findings. Live-ready gate unchanged since v24/v25.

---

## 6. Web Routes & API

**Context.** Nine route modules under `web/routes/`. Every non-public endpoint pulls through `Depends(_request_user)` or `Depends(_request_actor)` plus a `@limiter.limit` decorator. Public paths whitelisted in `_PUBLIC_PATHS` (`/`, `/favicon.ico`, `/healthz`, `/readyz`, `/metrics`, `/auth/*`).

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-15h | HIGH | `/api/emergency-stop` calls `stop_bot(bot.slug)` with the wrong signature → TypeError, every bot falls through the except clause, no SIGTERM sent | `web/routes/admin.py:123` | PRE-EXISTING (surfaced in v26) |
| v26-16 | MEDIUM | WS `state_broadcaster` + `log_broadcaster` do not filter per-user at broadcast time | `web/app.py:1524`, `web/app.py:1625-1638` | PRE-EXISTING (TODO documented; Phase-3b blocker) |
| v26-17 | LOW | `GET /api/bots/{slug}` returns `{"error": ...}` with HTTP 200 on unknown slug, instead of 404 | `web/routes/bots.py:170-178` | PRE-EXISTING |

### v26-15h — Emergency-stop signature mismatch (HIGH)

**Wat.** `web/routes/admin.py:123` calls `await stop_bot(bot.slug)`. `stop_bot` is defined as `async def stop_bot(user_id: int, slug: str)`. The one-argument call raises `TypeError: stop_bot() missing 1 required positional argument: 'slug'`. The `except Exception` clause five lines below catches it and appends `{"slug": bot.slug, "error": "stop_bot() missing 1 required positional argument: 'slug'"}` to `failed`. Every bot ends up in `failed`; no SIGTERM ever leaves the portal.

**Waarom.** Emergency-stop is the safety-critical "oh-shit button" — it exists because an operator pushes it when something is actively going wrong. Silent no-op in that moment is exactly the failure mode the endpoint should not have. Latent since the Phase-1→2 composite-key migration renamed `stop_bot(slug)` → `stop_bot(user_id, slug)`; never surfaced because the only test (`tests/test_emergency_stop.py::test_empty_registry_returns_ok`) covers the empty-registry branch.

**Waar.**
```python
# web/routes/admin.py:117-136
stopped: list[str] = []
failed: list[dict] = []
for bot in await registry.all():
    if not bot.running:
        continue
    try:
        result = await stop_bot(bot.slug)   # <-- missing user_id
        ...
    except Exception as e:
        failed.append({"slug": bot.slug, "error": str(e)[:200]})

return {"ok": True, "stopped_bots": stopped, "failed": failed, ...}
```

Verified locally: `python -c "import asyncio; from web import app; asyncio.run(app.stop_bot('x'))"` raises the expected TypeError.

**Remediation.** `await stop_bot(bot.user_id, bot.slug)` — `BotInfo` already carries `user_id`. Add a regression test that runs a mock bot through emergency-stop and asserts `stopped_bots == [slug]` (not the current "empty list on empty registry" happy-path).

**STATUS — RESOLVED.** Call site fixed to `await stop_bot(bot.user_id, bot.slug)`. Regression coverage extended in `tests/test_emergency_stop.py` to register a running mock bot and assert its slug lands in `stopped_bots` (closing v26-21 alongside).

### v26-16 — WS broadcaster not per-user filtered

**Wat.** `LogBroadcaster.broadcast(slug, line)` distributes a log line to every WS client subscribed to that slug. `StateBroadcaster.broadcast(payload)` pushes a payload to every connected `/ws/state` client. Neither filters by `user_id`. The `_ws_extract_user_id` check gates *connection* but once connected, a client receives every message for that channel.

For `log`: each slug subscription is user-scoped (a non-owning user can't subscribe to a slug they don't own, because the subscribe code path resolves the slug against `registry.get(user_id=...)`). So a client physically cannot join a channel belonging to another user — this is safe in practice today.

For `state`: `watch_state_files` polls every bot across every user and broadcasts a single flat payload to every client. Under the current single-user deployment this is a no-op (one user, all bots theirs). Under Phase-3b with multiple users it is a cross-tenant data leak.

**Waarom.** Not exploitable today (one user). Becomes a MEDIUM data leak the moment a second user is seeded without this fix landing first. Explicit TODO(phase-3) comment already in both classes.

**Waar.**
```python
# web/app.py:1525-1532  LogBroadcaster
# TODO(phase-3): broadcast() moet per-user filteren ...

# web/app.py:1630-1638  StateBroadcaster
# TODO(phase-3): broadcast() moet per-user filteren ...
```

**Remediation.** Store `user_id` on `connect()`. In `broadcast()`, look up the bot's owner and emit only to matching clients. Add a test that user A's client does not receive user B's bot state.

**STATUS — RESOLVED.** Both `LogBroadcaster` and `StateBroadcaster` now record the connecting client's `user_id` and filter at broadcast time: log lines route on slug-ownership, state payloads are per-user pruned before send. The matching `TODO(phase-3)` comments in `web/app.py` are gone (closing v26-20 alongside). Cross-tenant regression tests assert that a connection for user B does not receive frames produced by user A.

### v26-17 — `GET /api/bots/{slug}` returns 200 on unknown slug

**Wat.** Instead of `HTTPException(404)`, the handler returns `{"error": f"Unknown bot: {slug}"}` with the default 200 status. Frontend parsing of the response then has to branch on the error field instead of the HTTP status.

**Remediation.** `raise HTTPException(status_code=404, detail=f"Unknown bot: {slug}")`. Matches the contract of every other `{slug}` endpoint.

---

## 7. Indicator Engine & Strategies

**Context.** `strategies/indicator_engine.py` dispatches to per-indicator modules under `strategies/indicators/`. v25 audit confirmed indicator log noise reduction (commit `a4b0b6c`); coverage is adequate.

No findings. The Phase-3a delta did not touch strategies.

---

## 8. ML Pipeline

**Context.** `ml/nightly_pipeline.py` is a partial scaffold — the optuna objective returns `0.0` (TODO pending a BacktestEngine integration). Entry-filter + feature-engineering modules are real; not yet used at runtime.

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-18 | MEDIUM | `ml/nightly_pipeline.optimize_parameters` reads `config/bots/{slug}.yaml` (Phase-1 path) instead of `config/bots/<user_id>/{slug}.yaml` | `ml/nightly_pipeline.py:183` | PRE-EXISTING (Phase-2 regression never caught) |

### v26-18 — Non user-scoped config path in ML pipeline

**Wat.**
```python
# ml/nightly_pipeline.py:183
config_path = Path(__file__).parent.parent / "config" / "bots" / f"{bot_slug}.yaml"
```

Under the Phase-2 layout the file lives at `config/bots/<user_id>/{slug}.yaml`. The current call returns "config_missing" on every real install, which is coincidentally why the stub has not visibly broken. The moment Phase-3 un-stubs the optuna harness, every run fails to find its config.

**Waarom.** MEDIUM because the stub returns early; no active crash path today. Will break silently in Phase-3 otherwise.

**Remediation.** Route through `core.paths.bot_yaml_path(user_id, slug)` — accept `user_id` as a required parameter of `optimize_parameters`. Or keep the stub and delete the filesystem lookup entirely until Phase-3 wires it properly.

---

## 9. Notifications & Logging

**Context.** Telegram-only today via `notifications/telegram.py`. The paper engine queues notifications on a daemon thread; shutdown drains with `NOTIFY_DRAIN_TIMEOUT_S`. Portal and bot logs are in `logs/portal.log` and `logs/<user_id>/<slug>.log`. Log level defaults to INFO; `REVERTO_LOG_LEVEL` env-var overrides.

No new findings. v26-09 covered the telegram error-path observation.

---

## 10. Configuration & Operator UX

**Context.** Bot YAMLs validate via `config/models.py` (strict Pydantic). Portal wizard has advisory `/api/bots/validate-config`. Wipe-deals and migration scripts live under `scripts/`. Makefile targets: `start`, `stop-all`, `reset-db`, `migrate-fs`, `wipe-deals`, `setup-admin`.

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-19 | MEDIUM | `make setup-admin` step is not documented in `docs/runbook.md` or `README.md` | `docs/runbook.md:84-102`, `README.md` | NEW (Phase-3a) |
| v26-20 | LOW | Dangling `TODO(phase-3)` comments in `web/app.py` (LogBroadcaster + StateBroadcaster + watch_state_files) | `web/app.py:1525, 1630, 1689` | PRE-EXISTING |

### v26-19 — setup-admin missing from startup docs

**Wat.** Phase-3a commit messages and `docs/phase-3.md` status-note mention `make setup-admin` as a required post-migration step. A fresh operator reading `docs/runbook.md "Startup checklist (fresh machine)"` sees `make start` → `make status` → `curl /healthz`. Hitting the portal login form then yields 401 with no error that points at setup_admin.

**Waarom.** MEDIUM operator-UX. The fix is a 5-line runbook edit, but forgetting it loops every new operator through a debug cycle.

**Waar.**
```
docs/runbook.md:84-102  "Startup checklist (fresh machine)"
README.md:8-13          "Quick start"
```

**Remediation.** Add a step 4 to the runbook's startup checklist:
```
# 4. Provision admin password (required before first login)
REVERTO_ADMIN_PW=<pw> make setup-admin
```
Mirror in `README.md "Quick start"`. Cross-reference from `docs/phase-3.md` status-note.

**STATUS — RESOLVED.** Both `docs/runbook.md` "Startup checklist (fresh machine)" and `README.md` "Quick start" now include the `make setup-admin` step before the first portal login. The `docs/phase-3.md` status-note carries a forward-pointer to the runbook section so an operator landing on the historical doc still finds the current procedure.

### v26-20 — Dangling Phase-3 TODO comments

Three comments in `web/app.py` point at the broadcaster per-user filtering (v26-16). They are accurate descriptions; clean-up can land together with the fix.

---

## 11. Test Coverage & Quality

**Context.** 903 tests, isolated SQLite per test (`tests/conftest.py` autouse fixture). No skipif markers outstanding. CI matrix: Python 3.12 + 3.13. Coverage floor raised to 80% (commit `01887d0`).

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-21 | MEDIUM | `tests/test_emergency_stop.py` only covers the empty-registry branch — the actual stop-every-bot path (where v26-15h's bug lives) is untested | `tests/test_emergency_stop.py:34-47` | PRE-EXISTING (gap surfaced in v26 via code read) |
| v26-22 | LOW — **ACCEPTED** (known limitation, 2026-04-21) | `TestClient` session cookies drop under `SameSite=strict` on CI; `_COOKIE_SAMESITE` test-override stays in place. Explorative fix attempted, Gate 1 NO-GO. | `tests/test_web_routes.py:191-197`, `web/app.py:165-214` | NEW (Phase-3a) |
| v26-23 | INFO | Coverage of `web/routes/admin.py` is adequate for the routes it tests but the emergency-stop happy path is in the uncovered lines (v25 rapporteerde 74%) | `web/routes/admin.py` | PRE-EXISTING |

### v26-21 — Emergency-stop happy path untested

**Wat.** The only test that exercises `/api/emergency-stop` mocks `registry.all` to return `[]` and asserts `stopped_bots == []`. The code branch that iterates running bots and calls `stop_bot` is not exercised anywhere — which is why v26-15h sat latent across multiple commits.

**Remediation.** Add a test that registers a mock BotInfo with a live pid (monkeypatch `_pid_alive` and `os.kill`) and asserts the slug lands in `stopped_bots`. Would have caught v26-15h the moment it was introduced.

### v26-22 — SameSite test-cluster drift (ACCEPTED, 2026-04-21)

**Wat.** Production cookies are `SameSite=strict`. The test fixture flips to `lax` because httpx/TestClient in CI on Python 3.13 + Ubuntu runners dropped the cookie on follow-up requests without an Origin header (DIAG-6 output on diagnose commit `88ce0e3`). The fix landed in commit `5a4d97b` as a `_COOKIE_SAMESITE` constant that tests override to `lax` while production stays on `strict`. That leaves a test-production divergence: the test-suite validates the `lax` cookie-posture, not the real production one.

**Resolution (2026-04-21): ACCEPTED as known limitation.** Branch `fix/audit-v26-22-testclient-samesite` ran the exploratory plan from the prompt. Gate 1 (exploratory research, max 30 min) landed NO-GO:

1. **httpx 0.28.1 does NOT enforce SameSite.** Source read (`httpx/_models.py:11` imports `http.cookiejar.CookieJar` from stdlib; `Cookies` class at line 1079 just wraps the jar) plus the upstream discussion [encode/httpx#2168](https://github.com/encode/httpx/discussions/2168) confirm that SameSite is stored as a nonstandard cookie attribute, never checked on subsequent requests. The "Origin header fixes the drop" hypothesis has no code-path in httpx to engage.

2. **Local repro shows SameSite=strict works on Python 3.12.3.** A standalone `/tmp/test_samesite_fullflow.py` ran the exact failing flow (login → GET → logout → `cookies.clear()` → fresh login → gated GET) with `SameSite=strict` — the cookie is delivered on the final GET in all three variants (no Origin header, matching Origin, cross-site Origin). The workstation has no Python 3.13 binary (only 3.12.3), so the CI-specific 3.13-Ubuntu behaviour cannot be reproduced locally.

Combining 1 + 2: I cannot develop or validate a fix without Python 3.13 reproducibility, and the wrapper hypothesis does not have a code-path in httpx to exploit even if it did repro. Pushing an unvalidated wrapper for CI to judge violates the "tests pass at every commit" rule.

**Consequence.** Auth-tests validate the `lax` cookie-posture only. Manual QA in staging / production covers the real `strict` behaviour.

**Revisit triggers.** The `_COOKIE_SAMESITE` comment in `web/app.py` lists the three conditions under which this acceptance should be reviewed:
- TOTP implementation (Phase B) re-opens the auth-stack for broader rework.
- httpx publishes a TestClient SameSite-aware release.
- Python 3.13 reproducibility becomes available on the workstation.

**Remediation (deferred).** The original proposal — `tests/test_auth_cookie_posture.py::test_login_cookie_has_strict_samesite` that reads `Set-Cookie` when `_COOKIE_SAMESITE` is its default — is still the right safety-net but would need to toggle the constant back to its production value before reading the header, which is the kind of test-fixture contortion this acceptance is trying to avoid. Revisit under one of the triggers above.

**STATUS — RESOLVED in `feat/cookie-posture-regression-test` (Phase B PR 5).** The "test-fixture contortion" the deferral worried about turned out to be a single fixture that flips `_COOKIE_SECURE = True` + `_COOKIE_SAMESITE = "strict"` for the duration of the test, and only inspects the Set-Cookie header on the first response (no follow-up requests rely on the cookie surviving plain-HTTP TestClient transport). Pinned the per-cookie attribute posture for all four production cookies — `reverto_session`, `reverto_csrf`, `reverto_totp_pending` (Phase B PR 2), `reverto_login_totp_pending` (Phase B PR 3) — with an explicit "intentionally NOT HttpOnly" carve-out for the CSRF cookie (double-submit pattern requires JS read access). Plus a defence-in-depth check that no cookie carries a Domain attribute (would broaden scope to subdomains) and that `reverto_session` is NOT minted during the TOTP-pending phase (would bypass the 2FA gate). 12 regression tests; sanity-checked by tampering 3 different attributes (HttpOnly, SameSite, Secure) and observing diagnostic failures on each. The original "revisit triggers" section above is now informational only — Trigger #1 (Phase B re-opens the auth-stack) fired and produced this fix.

---

## 12. Documentation & Runbook

**Context.** `docs/architecture.md`, `docs/runbook.md`, `docs/deployment.md`, `docs/phase-3.md`. v25 findings about doc-drift are closed (commit `e27c25d` added deal-ID format, wipe-deals section, log level override, import/export/duplicate flow).

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-19 | MEDIUM | (see §10) | — | — |
| v26-24 | LOW | `docs/phase-3.md` historical sections still describe `.auth.json` as the auth-state store; the status-note at top flags them as historical, but a reader parsing the body without the preamble sees obsolete claims | `docs/phase-3.md:63-271` | NEW (Phase-3a) |

### v26-24 — Phase-3 doc body is stale

Preface says "secties hieronder die dat proces beschrijven zijn historisch" but §7 still reads as present-tense auth architecture ("password hashes in `.auth.json`"). Prune or clearly strike through the paragraphs overtaken by Phase-3a.

---

## 13. Performance & Efficiency

**Context.** Only casual inspection (per audit brief).

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-25 | INFO | `_bitget_client` is a module-level ccxt instance + `_price_lock` serialises every `/api/price` call | `web/app.py:349-354` | PRE-EXISTING |

No new perf findings. The `_scan_user_dirs` cache-TTL from the v25 backlog sweep is now in place (commit `74af616`); orphan-log dedup likewise.

### v26-25 — /api/price global serialisation (INFO)

Every `/api/price` call takes `_price_lock` because ccxt clients are not thread-safe. Under heavy dashboard polling this becomes a choke point. Not urgent; document as a Phase-3 item if /api/price ever becomes an SLO-critical path.

---

## 14. Dependency & Infrastructure

**Context.** Two requirement files (`requirements.txt` core, `requirements-ml.txt` optional). CI does `pip-audit --strict` on both + a smoke-import check (closes v25 #3). Python matrix 3.12 + 3.13.

| ID | Severity | Finding | File:line | Delta-sinds-v25 |
|----|----------|---------|-----------|-----------------|
| v26-26 | INFO | `requirements-ml.txt` has no `--constraint` pinning against `requirements.txt`; a numpy bump in ML context can diverge from core | `requirements-ml.txt` | PRE-EXISTING |

No CVE hits under `pip-audit --strict` as of the audit date. ccxt + cryptography + fastapi are on recent pins.

### v26-26 — ML/core version divergence risk (INFO)

Installing `requirements-ml.txt` after `requirements.txt` can resolve to a different `numpy` or `scipy` than the core already pins. Usually pip's resolver detects conflicts, but edge cases around transitive deps can silently upgrade. Adding `-c requirements.txt` to the ML file as a constraint would lock divergence out.

---

## Appendix A: Resolved v25 findings

| v25 # | Severity in v25 | Resolution | Commit |
|-------|-----------------|------------|--------|
| #1 | MEDIUM | `_scan_user_dirs` fail-closed cache + orphan dedup | `1ee4737`, `74af616` |
| #2 | MEDIUM | Session-epoch CI skip — root-caused (samesite TestClient quirk) and fixed via Phase-3a + `_COOKIE_SAMESITE` override | `5a4d97b`, Phase-3a series |
| #3 | MEDIUM | CI smoke-import step added; prometheus_client + xgboost now blocked from drifting out of manifests | `2a6862d` |
| #4 | MEDIUM | DB + state.json dual-source — **STILL OPEN**, re-filed as v26-14 INFO (Phase-3 scoping work) | n/a |
| #5 | MEDIUM | Body-size cap on `POST /api/bots` + `PUT /api/bots/{slug}/config` | `382adb8` |
| #6 | LOW | Registry DB-query per 5s → TTL cache | `74af616` |
| #7 | LOW | Orphan warning log-dedup | `1ee4737` |
| #8 | LOW | Deal-ID NTP-backward edge case documented | `9649f34` |
| #9 | LOW | wipe-deals TOCTOU → `fcntl.flock` on sentinel | `8661f64` |
| #10 | LOW | wipe-deals POSIX-only limitation documented | `eec7301` |
| #11 | LOW | CI coverage-floor raised 55% → 80% | `01887d0` |
| #12 | LOW | `docs/architecture.md` + `docs/runbook.md` updated | `e27c25d` |
| #13 | LOW | `_db_create_deal_with_retry` in-place mutation test pinned | `967c2b2` |
| v24 LOW #4 (carry) | LOW | `credentials/` parent tightened to `0o700` | `eacf118` |
| v24 LOW #5 (carry) | LOW | `logs/pids/` rmdir after fs-migration | `8661f64` |

**Net: 13 of 13 v25 findings closed (v25 #4 retained as architectural INFO). Impressive delta.**

---

## Appendix B: Recommended prioritization

### Must fix before multi-user rollout (Phase-3b gate) — ALL CLOSED

All five Phase-3b-gate items are RESOLVED at the time of the Phase-A wrap-up sweep. See the per-finding STATUS blocks for closure detail.

1. ~~**v26-15h (HIGH)**~~ — Fix `stop_bot(bot.slug)` signature mismatch in `web/routes/admin.py:123`. Add test v26-21 alongside. **RESOLVED.**
2. ~~**v26-02 (MEDIUM)**~~ — Add role check to `/api/emergency-stop`. **RESOLVED.**
3. ~~**v26-16 (MEDIUM)**~~ — Per-user filter on WS broadcasters. **RESOLVED.**
4. ~~**v26-01 (MEDIUM)**~~ — Consolidate `_require_session` into `_request_user` or share the `active` check. **RESOLVED** (Phase-A wrap-up consolidation — `_require_session` deleted entirely).
5. ~~**v26-10 (MEDIUM)**~~ — Destructive-migration guard (env-var or sentinel file). **RESOLVED** (env-var `REVERTO_DESTRUCTIVE_MIGRATE`).

### Must fix before next fresh-install deployment

6. ~~**v26-19 (MEDIUM)**~~ — Document `make setup-admin` in runbook + README. **RESOLVED.**
7. **v26-03 (MEDIUM)** — Consolidate password-length constant.
8. **v26-18 (MEDIUM)** — Fix ML pipeline config path to be user-scoped (or delete stub).

### Sweep-PR candidates (audit-v26-sweep)

9. **v26-04** rate-limit `/auth/logout`.
10. **v26-05** drop username-string fallback in `_create_session_cookie`.
11. **v26-06** remove or rename dead Fernet helpers.
12. **v26-07** treat empty `REVERTO_ADMIN_PW` as unset.
13. **v26-11** use `UPDATE ... RETURNING` in `bump_session_epoch`.
14. **v26-17** `GET /api/bots/{slug}` → proper 404.
15. **v26-20** resolve TODO(phase-3) comments together with v26-16.
16. ~~**v26-22** add cookie-posture smoke test.~~ **RESOLVED** in
    `feat/cookie-posture-regression-test` (Phase B PR 5). The
    2026-04-21 ACCEPTED-status was rolled back when the Phase B
    auth-stack re-opening (the explicit revisit-trigger #1 from the
    original resolution note) produced a viable fixture pattern.
    See Part 11 STATUS — RESOLVED block.
17. **v26-24** strike through obsolete `docs/phase-3.md` sections.

### Defer (INFO / observational)

v26-08, v26-09, v26-12, v26-13, v26-14 (carry-over), v26-15, v26-23, v26-25, v26-26 — document once, revisit next audit.

---

_Audit uitgevoerd op 2026-04-20. HEAD: `0ef39a7` (main post-Phase-3a merge). Parity-bots zijn stopped (Phase-3a deploy in progress); `logs/` timestamps bevestigd onaangeroerd sinds de laatste wijziging._
