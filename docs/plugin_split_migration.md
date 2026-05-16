# Plugin Split: Phase 1 Migration Plan

> **Status.** Phase 1 deliverable (planning only, no code changes). Companion documents: [plugin_split_audit.md](plugin_split_audit.md), [plugin_split_design.md](plugin_split_design.md).
>
> **Purpose.** Translate the design into a task-level work plan for Phases 2–7, with effort estimates grounded in the audit. Captures the risks the operator must be aware of and the questions that need answering before Phase 2 starts.

---

## 3.1 Phase-by-phase implementation

The work breaks into six phases (Phase 1 is this trio of documents). Each phase is sized small enough that the operator can ship-or-revert it without holding open a long-running branch.

### Phase 2: Framework refactor (in this repo)

**Goal.** Extract `TradingEngine` base, introduce `LiveProvider` interface, remove inline `Mode.LIVE` branches from the framework. `live/` still lives in the repo at the end of Phase 2 (it physically relocates in Phase 3). At the end of Phase 2, framework can boot without ever calling into `live/` at the Python level.

**Estimated total:** **18–24 hours** of focused work, spread across ~8 PRs.

| # | Task | Files touched | Est. (h) | Risk | Test point |
|---|---|---|---:|---|---|
| 2.1 | Extract `TradingEngine` ABC into `core/trading_engine.py`. Move all INFRA + TRADING methods from `PaperEngine`. Leave `PaperEngine` as a thin `TradingEngine` subclass with `_deduct_balance`, `_place_market_order`, `_get_current_price`. | `paper/paper_engine.py`, new `core/trading_engine.py`, `tests/test_paper_engine.py` (import paths) | 4 | **High.** Touches the 1913-LoC engine. | Full `make test` after each commit; expect 2093 pass to hold. |
| 2.2 | Add `_pre_tick_hook` / `_post_tick_hook` to `TradingEngine._tick`; move `LiveEngine._tick` clock-skew + reconciler logic into those hooks. | `core/trading_engine.py`, `live/live_engine.py` | 2 | Medium. `_tick` is hot path; subtle reordering can break sentinel timing. | `tests/test_live_engine.py` + `tests/test_paper_engine.py`. |
| 2.3 | Define `LiveProvider` Protocol in `core/live_provider.py` and `core/plugin_loader.py` with the loader. | new `core/live_provider.py`, `core/plugin_loader.py` | 2 | Low. Pure new code. | Unit tests for loader (importable, version check, cache). |
| 2.4 | Stub a framework-internal `_BuiltinLiveProvider` that wraps the still-in-tree `live/` package so the seam is exercised end-to-end before the physical relocation. | new `live/builtin_provider.py`, `core/plugin_loader.py` | 2 | Low. Temporary scaffolding. | `tests/test_plugin_loader.py`. |
| 2.5 | Refactor `web/app.py:start_bot_dry_run` to delegate to `live_provider.start_bot_dry_run()`. | `web/app.py` | 2 | Medium. Restart dispatch path is operator-facing. | `tests/test_web_routes.py` adds a mocked-provider case. |
| 2.6 | Refactor `web/app.py:restart_bot` to call `live_provider.is_live_config()` instead of `cfg.mode == Mode.LIVE`. | `web/app.py` | 1 | Low. | Same as 2.5. |
| 2.7 | Refactor `web/routes/portfolio.py:_live_bot_slugs` to call `live_provider.list_live_slugs()`. | `web/routes/portfolio.py` | 1 | Low. | `tests/test_portfolio_routes.py`. |
| 2.8 | Refactor `core/circuit_breaker.py` and `exchanges/public_exchange.py` to use injected `on_permanent_open` callback. Register the Telegram fan-out via plugin loader at framework boot. | `core/circuit_breaker.py`, `exchanges/public_exchange.py`, `web/app.py` (boot wiring) | 3 | **High.** The breaker is mission-critical safety infra. Mis-wired callback can swallow alerts in prod. | `tests/test_circuit_breaker.py` runs with mocked callback; manual smoke on VPS verifies the real Telegram path fires once. |
| 2.9 | Add `tests/conftest.py` fixture `mock_live_provider` and convert any `Mode.LIVE`-touching tests to use it. | `tests/conftest.py`, ~3 test files | 1 | Low. | Full `make test`. |
| 2.10 | Documentation pass: update `docs/architecture.md` and `CLAUDE.md` to reference the new TradingEngine / LiveProvider. | `docs/architecture.md`, `CLAUDE.md` | 1 | Low. | Doc lint. |

**Phase 2 ship gate.** All of:

- `make test` passes (2093 expected; some test counts may shift slightly with conftest changes, which must be explained).
- `make lint` clean.
- A standalone smoke run: uninstall the in-tree `live/` package's imports (e.g. rename to `_live_disabled/` temporarily) → portal boots → paper bots start/stop → restart flows hit the framework path.

### Phase 3: Plugin as a pip package (new repo `reverto-live`)

**Goal.** Move `live/`, `main_live.py`, `exchanges/bitget.py`, `exchanges/kraken.py`, and the 5 plugin-only tests into a sibling repo `reverto-live/`. Framework remains 100% functional without it.

**Estimated total:** **8–12 hours**.

| # | Task | Est. (h) | Risk |
|---|---|---:|---|
| 3.1 | Create `reverto-live` repo skeleton (pyproject.toml, README, CI). Pin `reverto>=0.X` from a git URL until framework hits PyPI. | 2 | Low. |
| 3.2 | Move `live/`, `main_live.py`, `exchanges/bitget.py`, `exchanges/kraken.py` to plugin repo. Update import paths from `live.*` to `reverto_live.*`. Implement `LiveProvider` Protocol in `reverto_live/__init__.py`. | 3 | Medium. Many import path renames. |
| 3.3 | Move the 5 plugin-only tests to `reverto-live/tests/`. Drop them from framework `tests/`. | 1 | Low. |
| 3.4 | Wire plugin CI (GitHub Actions): runs plugin tests against framework installed from git pin. | 2 | Medium. Multi-repo CI complexity. |
| 3.5 | Wire integration CI: separate job installs both, runs full framework + plugin suites together. | 2 | Medium. |
| 3.6 | Update operator-facing docs: `docs/INSTALL.md` adds the plugin-install step for live mode. | 1 | Low. |
| 3.7 | Delete the temporary `live/builtin_provider.py` scaffolding added in Phase 2. | 0.5 | Low. |

**Phase 3 ship gate.** All of:

- Framework CI green (115 framework tests, 0 plugin tests).
- Plugin CI green (5 plugin tests).
- Integration CI green (115 + 5 + the new integration-only tests).
- Manual VPS smoke: pip-install both packages on a clean VM, register a live-mode bot, dry-run-start it, verify dashboard updates.

### Phase 4: Binary obfuscation + distribution

**Goal.** Ship the plugin as a wheel rather than source. Decide on obfuscation level. Set up a private distribution channel.

**Estimated total:** **6–10 hours** (depending on the obfuscation choice).

| # | Task | Est. (h) | Risk |
|---|---|---:|---|
| 4.1 | Decide obfuscation level (open question O7 below). Options: source-only wheel, `pyarmor`-obfuscated wheel, Cython-compiled `.so`. | 0 (decision only) | n/a |
| 4.2 | Build pipeline: produce wheel on tag push. | 2 | Low. |
| 4.3 | (If obfuscating) Test the obfuscated wheel against the integration suite. | 3 | **High.** Obfuscators sometimes break reflection / dataclasses. |
| 4.4 | Set up private PyPI index or signed download URL with short-lived tokens. | 3 | Medium. Operational ongoing cost. |
| 4.5 | Document install + upgrade flow for paying operators. | 2 | Low. |

### Phase 5: License validation

**Goal.** Plugin refuses to boot without a valid license token. Token validates against a remote license server with offline grace period.

**Estimated total:** **8–14 hours**.

| # | Task | Est. (h) | Risk |
|---|---|---:|---|
| 5.1 | Decide license model (per-machine? per-user? perpetual vs subscription?). Open question. | 0 | n/a |
| 5.2 | License server (smallest viable: a single FastAPI endpoint backed by a customers table). | 4 | Medium. |
| 5.3 | Plugin-side license check: reads `REVERTO_LIVE_LICENSE_KEY` from env, POSTs to license server on first run, caches signed response with 7-day offline grace. | 3 | **High.** Getting offline grace wrong locks paying users out. |
| 5.4 | License-expired UX: dashboard banner + dry-run-only mode after expiry. Never blocks paper bots. | 2 | Medium. |
| 5.5 | Tests: mock license server, verify offline grace + expiry path. | 2 | Low. |

### Phase 6: Payment integration

**Goal.** Operator can purchase a license without manual intervention. Integrates with the licensing server.

**Estimated total:** **8–12 hours**.

| # | Task | Est. (h) | Risk |
|---|---|---:|---|
| 6.1 | Pick payment provider (Stripe? Mollie? Crypto-only? Open question.) | 0 | n/a |
| 6.2 | Checkout flow on marketing site (`reverto.bot`). | 4 | Medium. |
| 6.3 | Webhook handler that provisions a license server entry + emails the key to the operator. | 3 | **High.** Fraud / chargeback risk. |
| 6.4 | EU VAT compliance + invoicing (KOR/OSS scheme, see `docs/COMMERCIAL_BOUNDARIES.md`). | 2 | **High.** Tax compliance. |
| 6.5 | Refund + dispute flow. | 1 | Medium. |

### Phase 7: Documentation + migration for existing operators

**Goal.** Existing self-hosters using the all-in-one repo get a smooth path to "framework + plugin". One-shot migration script.

**Estimated total:** **4–6 hours**.

| # | Task | Est. (h) | Risk |
|---|---|---:|---|
| 7.1 | `scripts/migrate_to_plugin_split.py`: detects existing in-tree `live/`, prompts for license key, pip-installs the plugin from a tarball. | 2 | Medium. |
| 7.2 | Migration guide in `docs/MIGRATION_PLUGIN_SPLIT.md` (in framework repo) covering: backup → install plugin → verify → cut-over. | 1 | Low. |
| 7.3 | Changelog entry + dashboard banner one-week-warning before the first version that strips in-tree `live/`. | 1 | Low. |
| 7.4 | Slack/Telegram support for early-adopter operators during the cut-over week. | 1–2 | Low (mostly time, not risk). |

### Grand total estimate

| Phase | Estimate (hours) |
|---|---:|
| 2: Framework refactor | 18–24 |
| 3: Plugin pip package | 8–12 |
| 4: Binary obfuscation + distribution | 6–10 |
| 5: License validation | 8–14 |
| 6: Payment integration | 8–12 |
| 7: Documentation + migration | 4–6 |
| **Total** | **52–78 hours** |

This is **lower than the operator's initial 60–86 estimate**, primarily because the audit found the framework code is less coupled than expected (8 runtime checks rather than the ~20 the operator may have assumed from file sizes). Phase 2 is the dominant phase by hours and risk; the commercial phases (4–6) are short on engineering but carry significant operational/legal weight.

---

## 3.2 Risk register

Risks are sorted by exposure (probability × impact) and tagged with the phase that owns mitigation.

| # | Risk | Phase | Probability | Impact | Mitigation |
|---|---|---|---|---|---|
| R1 | `PaperEngine` extract-base-class breaks paper trading in subtle ways (e.g. sentinel timing, state-write order). | 2 | Medium | **High.** Paper is the only thing keeping prod operators running today | Comprehensive test suite already exists. Refactor in small commits (one method bucket at a time). VPS smoke after each merged PR. Keep `live/` in-tree throughout Phase 2 so a rollback restores the working state. |
| R2 | Circuit-breaker callback refactor swallows production alerts. | 2 | Medium | **High.** Operator wouldn't know exchange is down | Manual smoke test: deliberately trip the breaker on staging (e.g. `iptables` block ccxt host); verify Telegram alert fires. Ship the refactor behind a feature flag `BREAKER_CALLBACK_VIA_PLUGIN=1` for the first week. |
| R3 | Plugin interface becomes too rigid; can't evolve without breaking compat. | 2-3 | Low | Medium | Use `Protocol` (not ABC) so optional methods are `hasattr`-detected. Bump `interface_version` only for required-method additions. Keep `LiveProvider` surface minimal (4 methods today). |
| R4 | Framework tests become hard to run because they need a plugin mock. | 2 | Low | Medium | Provide `mock_live_provider` conftest fixture (see design §2.9). Only 3-ish tests touch this surface. |
| R5 | Plugin obfuscation breaks reflection / dataclasses / Pydantic. | 4 | Medium | **High.** Late-stage discovery would block the launch | Pilot `pyarmor` on a stub `reverto_live` plugin in Phase 4.1 *before* the real plugin is built. Decide go/no-go on obfuscation level early. |
| R6 | License-server outage locks paying users out. | 5 | Medium | **High.** Reputation-critical | 7-day offline grace by default. Plugin caches the signed license response. License server has its own uptime SLO (host on Hetzner or similar, not the same VM as the portal demo). |
| R7 | EU VAT / MiCA exposure from selling licenses. | 6 | Medium | **High.** Legal/regulatory | Already covered in `docs/COMMERCIAL_BOUNDARIES.md`: staying in software-distribution lane (MiCA-safe). Register for KOR/OSS VAT scheme before first sale. Get a 1-hour fintech-law consult before Phase 6 ships. |
| R8 | Operators on existing all-in-one repo don't migrate; framework support window grows indefinitely. | 7 | High | Medium | One-shot migration script + 4-week migration window. After that, frozen `live-bundled` branch tagged and unsupported. |
| R9 | Plugin imports framework internals (e.g. `core/_private_util`), creating de-facto API coupling. | 2-3 | Medium | Medium | Document the supported framework surface explicitly in `docs/PLUGIN_API.md`. Use `_underscore` prefix on framework-internal names. Plugin CI lints for imports of private names. |
| R10 | Naming collision between framework `reverto` package and operator's custom code. | 3 | Low | Low | Reserve `reverto` on PyPI early. Use sentinel module `reverto.__version__` for compat checks. |
| R11 | Plugin and framework versions drift; operator pip-installs an incompatible combo. | 3 | Medium | Medium | `interface_version` integer check at plugin load. `install_requires` upper-pin in plugin's `pyproject.toml`. Clear error message: "Plugin v2.1.0 requires framework >=1.5,<2.0; you have 1.4.0". |
| R12 | Plugin's own tests can't be published (would reveal closed-source code paths). | 3 | Medium | Low | Plugin CI is on a private GitHub repo. Plugin docs ship public, plugin tests stay private. |
| R13 | `start_bot_dry_run` subprocess spawn becomes async-misaligned after delegation. | 2 | Low | Medium | The existing subprocess uses `asyncio.sleep` polling; the delegated provider must preserve that. Integration test exercises the spawn path with a real plugin install. |
| R14 | After Phase 3, the framework repo's `git log` no longer matches the running plugin's `git log`. Forensic post-mortems become harder. | 3 | Low | Medium | Plugin keeps a `provider.framework_compat_version` constant and the plugin CHANGELOG cross-links framework releases it was tested against. |

---

## 3.3 Open questions for operator input

These must be answered before Phase 2 begins. Listed in rough priority order.

| # | Question | Why it matters | Recommendation |
|---|---|---|---|
| O1 | **Same-PR or staged extract?** Should the `TradingEngine` extraction (task 2.1) and the `live/` relocation (Phase 3) happen as one atomic PR, or with Phase 2 fully shipped before Phase 3 starts? | Atomic = cleaner history, riskier; staged = safer, leaves a temporary `live/builtin_provider.py` in framework for ~2 weeks. | **Staged.** The audit-found low coupling means staged costs little extra, and the safety margin during the extraction is worth it. |
| O2 | **Framework license: AGPL, BSL, MIT?** | Determines whether forks can offer commercial live-trading services that bypass our plugin. MIT lets them; AGPL/BSL deters it. | **BSL** (Business Source License, MariaDB/Sentry pattern). Converts to Apache after N years, blocks commercial competitor forks meanwhile. Already discussed in `docs/COMMERCIAL_BOUNDARIES.md`. |
| O3 | **Plugin package distribution name.** `reverto-live` or `reverto_live`? PyPI hyphen or underscore? | Distribution name vs import name confusion. | **`reverto-live`** for distribution, **`reverto_live`** for import. Standard Python convention. |
| O4 | **Pricing model.** Perpetual one-time license, annual subscription, or hybrid (perpetual + paid major upgrades)? | Drives the license server and payment integration design (Phase 5 + 6). | **Perpetual one-time** for v1.x with paid major-version upgrades. Simplest legally; matches the "software not service" MiCA stance. |
| O5 | **Framework versioning at v1.0.** Defer the 1.0 stamp until after the split is shipped and stabilised? | A `0.x` framework gives us freedom to break interfaces; a `1.0` ties us down. | **Defer 1.0 until end of Phase 3.** Mark the plugin as v0.x too until then. |
| O6 | **Plugin-private CI runner.** Does the plugin repo CI run on GitHub-hosted runners (cheap but plugin source goes through Microsoft) or on self-hosted (operational cost)? | Closed-source code on cloud runners is a risk if the obfuscation isn't applied yet. | **GitHub-hosted** for Phase 3 (no real obfuscation yet anyway); revisit if Phase 4 obfuscation requires native build. |
| O7 | **Obfuscation level.** Source-only wheel, `pyarmor`-encoded wheel, or Cython-compiled `.so`? | Trades effort for protection. None is bullet-proof. | **Pyarmor-encoded wheel for Phase 4 launch.** Cython is too invasive (breaks reflection, hard to debug). Source-only invites trivial bypass via the source-available `LiveProvider` interface. |
| O8 | **License-server host.** Same VM as portal demo (cheap) or separate (safer)? | Outage co-correlation. | **Separate, smallest Hetzner CX11 instance.** A €5/mo VM dedicated to license validation is cheap insurance against the portal-demo blast radius. |
| O9 | **Payment provider.** Stripe, Mollie, or crypto-only? | Different fee structures, fraud surfaces, EU compliance levels. | **Mollie.** EU-native, lower fees than Stripe for SEPA, good Dutch tax docs. |
| O10 | **Existing operator migration window.** 2 weeks, 4 weeks, or "indefinite support"? | Trades support burden against operator goodwill. | **4 weeks.** Long enough that operators see at least one weekly notification cycle, short enough that the support burden doesn't drag on. |

---

## 3.4 Success criteria

Phase 1 (this document set) is **complete** when:

1. All three documents exist in `docs/` and have been reviewed by the operator.
2. The operator has answered all 10 open questions in §3.3 (or explicitly deferred specific ones to a later phase).
3. The operator has signed off on the Phase 2 task list in §3.1 (specifically: the 10 tasks 2.1–2.10).
4. There are no open architecture disagreements between the design and the operator's mental model.

Phase 2 is **complete** when:

1. Framework boots with the in-tree `live/` package symlink-renamed-away and all paper-only flows work end-to-end on the staging VPS for 48 hours.
2. `make test` passes with `2093 +/- N` tests where N is documented (e.g. +3 if mock-provider tests added).
3. `make lint` clean.
4. `LiveProvider` Protocol + `plugin_loader.load_live_provider()` exist and are exercised by the temporary `live/builtin_provider.py` shim.
5. The 8 runtime coupling points from audit §1.4 are all replaced with `live_provider.X()` calls.

Phase 3 is **complete** when:

1. Plugin repo (`reverto-live`) is a separate git repo, pip-installable.
2. Framework repo no longer contains `live/`, `main_live.py`, `exchanges/bitget.py`, `exchanges/kraken.py`, or the 5 plugin tests.
3. Framework CI runs without plugin and is green.
4. Plugin CI runs against framework (git pin) and is green.
5. Integration CI runs the full combined suite and is green.
6. Manual VPS smoke: pip-install both packages on a clean VM, register a live-mode bot, dry-run-start, verify the dashboard updates.

Phases 4–7 success criteria are commercial / operational rather than engineering and are scoped at each phase's kickoff.

### Hard "no-go" signals after Phase 2

If any of the following happens during or right after Phase 2, **stop and re-plan** instead of pressing into Phase 3:

- Paper bot regressions discovered in production (any deal logic divergence vs pre-refactor).
- More than 5% test-count drift that can't be explained per-test.
- The `LiveProvider` interface needs more than 8 methods (signals the boundary is wrong).
- The circuit-breaker callback refactor results in a single dropped alert in production.

---

## 3.5 What this plan does NOT cover

- **Web frontend changes.** The 14k-LoC `web/static/app.js` does not need plugin-aware code changes; the existing mode-aware DOM degrades gracefully when `/api/portfolio/per-bot` returns empty or when no live bots exist. Visual polish (e.g. a "Live plugin not installed" badge in the bot wizard) is deferred to Phase 7 docs work.
- **ML pipeline split.** `ml/` is framework-only per the audit. If/when ML becomes a separately-licensed product, it gets its own plan.
- **`scripts/parity_compare.py`.** This 775-LoC tool reads both paper and live deal ledgers to verify they diverge as expected. Stays in framework (reads ledgers via `core/deal_store.py`, no live engine import). Operator-facing only.
- **Backwards-compat support for the current `live-bundled` operators after Phase 7.** Once the migration window closes, the bundled repo gets a final tag and goes into "security patches only" mode. A separate doc will describe that policy when Phase 7 ships.
