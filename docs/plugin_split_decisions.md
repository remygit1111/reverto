# Plugin Split Decisions

Authoritative answers to the 10 open questions raised in
[`docs/plugin_split_migration.md`](plugin_split_migration.md) §3.3.
These decisions guide the Phase 2–7 implementation work.

- **Decision date**: 2026-05-14
- **Operator**: Remy

---

## Summary table

| ID  | Question                  | Decision                                           |
| --- | ------------------------- | -------------------------------------------------- |
| O1  | Staged or atomic refactor? | Staged                                             |
| O2  | Framework license?         | BSL 1.1 with Additional Use Grant Option 3        |
| O3  | Plugin package naming?     | `reverto-live` (dist) / `reverto_live` (import)   |
| O4  | Pricing model?             | Hybrid (perpetual + paid major upgrades)          |
| O5  | Framework v1.0 marker?     | Defer until after Phase 3                          |
| O6  | Plugin CI runner?          | GitHub-hosted in Phase 3, revisit in Phase 4      |
| O7  | Obfuscation level?         | Pyarmor wheel with stub-test in Phase 4           |
| O8  | License-server host?       | Separate Hetzner CX11 (€5/mo)                     |
| O9  | Payment provider?          | Defer until Phase 6                                |
| O10 | Migration window?          | 4 weeks                                            |

---

## O1 — Staged refactor

**Decision**: Staged.

Phase 2 (`TradingEngine` extraction + `LiveProvider` interface) ships
and stabilises before Phase 3 (`live/` relocation to the plugin repo)
begins. During the roughly two-week interim, `live/builtin_provider.py`
acts as a shim in the framework repo so paper trading and live trading
both continue to function.

**Rationale**:

- Rollback per phase is possible.
- Smaller PRs are easier to review.
- Lower risk of breaking working code.
- Aligns with the operator's "veiligheid centraal" principle.

**Implications for Phase 2**:

- The final task in Phase 2 verifies that the `live/builtin_provider.py`
  pattern works end-to-end.
- The framework can run with or without the external plugin during the
  interim window.
- Test suites run with the shim in place.

---

## O2 — Framework license: BSL 1.1 with Additional Use Grant Option 3

**Decision**: Business Source License 1.1 with an Additional Use Grant
that permits non-production use freely and requires either a commercial
licence or the Reverto Live Plugin for production use.

**Specific terms**:

- **Licence**: Business Source License 1.1.
- **Change Date**: 4 years from each release date (rolling).
- **Change License**: Apache License, Version 2.0.
- **Additional Use Grant**: "You may use the Licensed Work for
  non-production purposes including evaluation, paper trading, and
  backtesting. Production use (live trading with real funds) requires
  either a separate commercial licence from the Licensor or use together
  with a separately-licensed Reverto Live Plugin."

**Rationale**:

- Source-available aids transparency — crypto users can audit the code
  that handles their API keys.
- The rolling 4-year window protects the newest work continuously.
- Older versions become Apache 2.0, which is good-citizen behaviour and
  marketing-friendly for the wider Python/crypto ecosystem.
- The operator is mildly concerned about the eventual Apache 2.0
  conversion but accepts that 4-year-old versions have limited practical
  value as a foundation for a competitor.
- Cleanly compatible with the Live Plugin commercial licensing model
  (production use channel goes through the plugin).

**Why not MIT (the Jesse pattern)**:

- MIT offers no protection against commercial competitors using the
  framework as a foundation.
- BSL's rolling protection means newest features are always protected.
- The 4-year delta means competitors permanently lag.

**Implementation requirements**:

- `LICENSE` file in the repo root with BSL 1.1 boilerplate and the
  specific parameters above.
- `docs/RELEASES.md` tracking the version → Change Date mapping.
- `Makefile` target `release` to automate `LICENSE` updates per release.
- `README.md` update explaining the licensing model.
- File-level header updates are not required (BSL covers the Licensed
  Work as a whole).

**Migration from Apache 2.0**:

- The current Apache 2.0 licence stays in place until the switch
  happens.
- The operator decides timing: between Phase 1 and Phase 2, or just
  before the first paid release.
- No retroactive impact on Apache 2.0 versions — anyone holding an old
  version keeps their Apache 2.0 rights for that specific version.

---

## O3 — Plugin package naming

**Decision**: Standard Python conventions apply.

- **Distribution name** (PyPI / `pip install`): `reverto-live`.
- **Import name** (Python module): `reverto_live`.

Example usage:

```bash
pip install reverto-live  # hyphen
```

```python
import reverto_live  # underscore
```

There is no real choice here — this is Python convention. The decision
is documented for completeness.

---

## O4 — Pricing model: Hybrid

**Decision**: Hybrid — perpetual one-time licence per major-version
series, with paid major upgrades.

**Specific structure**:

- v1.x: one-time purchase grants a perpetual licence for the entire
  v1.x series (1.0, 1.1, 1.2, … up to the final v1.x release).
- v2.0: paid upgrade, likely discounted for existing v1.x customers.
- v2.x: perpetual within the v2.x series.
- The pattern repeats for v3.0, v4.0, and so on.

**Rationale**:

- Perpetual licences are easier to sell than subscriptions for crypto
  users, who tend to dislike recurring billing.
- The subscription model fits poorly with the "software not service"
  MiCA positioning.
- Major upgrades provide ongoing revenue without the churn dynamics of a
  subscription.
- Customers pay only when they want new major features.
- This is the operator's preference; pure perpetual and pure
  subscription were both rejected.

**Future considerations**:

- Pricing levels will be determined in Phase 6.
- Upgrade discount strategy is TBD — for example, 50% off for existing
  customers vs full price for new customers.
- The licence server must support per-version validation from day one.

---

## O5 — Framework v1.0 versioning: defer

**Decision**: Defer the framework v1.0 marker until after Phase 3
completes.

**Rationale**:

- During the refactor, interfaces may still need breaking changes.
- v0.x signals "API not stable yet"; operators expect changes.
- v1.0 commits to API stability — premature during active architectural
  change.
- After Phase 3 (plugin successfully separated), the interfaces have
  been validated end-to-end and v1.0 becomes a meaningful promise.

**Practical impact**:

- The current version stays in the v0.x range during Phase 2–3.
- The v1.0 marker happens after successful plugin separation
  validation.
- Phase 4 onward proceeds under a stable v1.0 framework.

---

## O6 — Plugin CI runner: GitHub-hosted in Phase 3, revisit in Phase 4

**Decision**: GitHub-hosted runners for Phase 3 work. Re-evaluate
self-hosted versus Hetzner Cloud runners during Phase 4 when obfuscation
enters the picture.

**Phase 3 reasoning**:

- The plugin is still source-only Python (no Pyarmor yet).
- "Closed source" status in Phase 3 means private repo, not obfuscation.
- GitHub-hosted private-repo minutes: 2000/month free tier.
- Estimated usage: 200–500 min/month — well within the free tier.
- Microsoft hosts ephemeral VMs; trust exposure is minimal for
  source-only code that's about to be checked out by paying customers
  anyway.

**Phase 4 re-evaluation triggers**:

- Pyarmor builds may require native compilation.
- Obfuscated artefacts living briefly on Microsoft infrastructure
  warrants a renewed trust assessment.
- Possible alternative: Hetzner Cloud runners wired into GitHub Actions
  via the self-hosted-runner integration.

**Final decision deferred until Phase 4 begins.**

---

## O7 — Obfuscation: Pyarmor with stub-test

**Decision**: Pyarmor 9 wheel for Live Plugin distribution, with a
mandatory stub-test in Phase 4 before fully committing to it.

**Specific approach**:

1. Phase 4.1: build a stub plugin with representative code (Pydantic
   `BaseModel`, dataclasses, typing patterns drawn from the current
   `live/` package).
2. Pyarmor-obfuscate the stub.
3. Test that the stub functions correctly:
   - Pydantic validation works.
   - Dataclass serialisation works.
   - Typing hints do not break runtime introspection.
   - Reverto-specific patterns work (for example, the chart-theme
     registry-style lookups and the circuit-breaker callbacks).
4. Only after stub validation: apply Pyarmor to the real plugin.

**Rationale**:

- Pyarmor is the industry standard for commercial Python.
- Source-only offers no real protection — trivially bypassable.
- Cython compilation is too invasive — it breaks Pydantic in some
  configurations and complicates debugging.
- Pyarmor combined with the licence server provides "good enough"
  protection.
- The stub-test prevents costly mid-refactor discovery of
  incompatibilities.

**Cost considerations**:

- Pyarmor Pro licence: roughly $150–200 one-time.
- A Pyarmor Group licence is required for offline obfuscation (also
  one-time).
- Rate limits: 100 different devices per 24h — not a constraint for a
  solo-dev release cycle.

**Realistic security expectations**:

- Pyarmor stops casual reverse engineers (~95%).
- Determined attackers can bypass it within hours to days of effort.
- Real security comes from the licence server plus continuous updates.
- Obfuscation is layer 1 of a 3-layer protection model (obfuscation →
  licence server → update cadence).

---

## O8 — License-server host: separate Hetzner CX11

**Decision**: The licence server runs on a dedicated Hetzner CX11 VM,
fully separate from the `app.reverto.bot` production VPS.

**Specifications**:

- Hetzner CX11: roughly €5/month.
- Separate from the production app VPS.
- Dedicated subdomain (`license.reverto.bot` or `licenses.reverto.bot`).
- SQLite database for licence records — simple, recoverable, and
  fits the expected scale comfortably.
- FastAPI backend with admin endpoints.
- HTTPS via Let's Encrypt and Caddy (the same stack as production).

**Rationale**:

- No blast-radius co-correlation: a production outage doesn't affect
  licence validation, and vice versa.
- Independent backup strategy.
- Independent monitoring and alerting.
- Can scale separately if needed.
- Cheap enough to justify the isolation (€60/year).

**Architectural implications**:

- The Live Plugin calls `license.reverto.bot` at startup and
  periodically thereafter.
- A grace period covers licence-server outages — suggested default: 7
  days of cached validation.
- The plugin must fail safe (refuse to start) on the first run if the
  licence server is unreachable; this only blocks initial activation,
  not subsequent normal operation.
- The licence server has its own admin portal for manual licence
  management.

---

## O9 — Payment provider: defer until Phase 6

**Decision**: Defer the payment-provider choice until Phase 6 (payment
integration).

**Candidates under consideration**:

- **Mollie**: EU-native (Amsterdam), low SEPA fees, requires
  self-handling EU VAT via the OSS scheme.
- **Lemon Squeezy**: Merchant of Record (handles VAT), higher fees
  (~5–7%).
- **Paddle**: Merchant of Record, similar profile to Lemon Squeezy.
- **Stripe**: Global default, higher EU SEPA fees, requires
  self-handling VAT.

**Reasons for deferral**:

- Phase 6 is 5–8 months away (after Phases 2–5 complete).
- Market conditions may shift (fees, features, EU regulations).
- The operator will have a clearer revenue projection by then.
- VAT administrative complexity differs significantly between the
  options (self-handled versus Merchant of Record), and the right
  trade-off depends on expected revenue.

**Pre-Phase 6 considerations** (not decisions, just heuristics):

- Mollie tends to win if expected annual revenue exceeds roughly €50k —
  fees become lower than Merchant-of-Record pricing.
- Lemon Squeezy or Paddle suit a solo dev with minimal admin capacity.
- Stripe is useful if non-EU expansion becomes a priority.

---

## O10 — Migration window: 4 weeks

**Decision**: A 4-week migration window for existing operators between
the Phase 7 release and EOL of the pre-plugin architecture.

**Rationale**:

- Long enough for a weekly notification cycle (3–4 reminders).
- Short enough that the operator support burden stays manageable.
- Aligns with industry norms for major version transitions.

**Current relevance**: low — no external operators exist yet. This
decision is for future planning, when Reverto has paying customers
running the older architecture.

**Notification strategy** (when relevant):

- Week 1: announcement email and portal notification.
- Week 2: in-app banner with a countdown.
- Week 3: more prominent banner plus an email reminder.
- Week 4: final-week banner and direct support outreach for stragglers.
- After 4 weeks: support for the old architecture stops.

---

## What this enables

With these 10 decisions locked in, Phase 2 can begin without further
blocking input from the operator. Specifically:

- `TradingEngine` extraction can proceed — the architectural decisions
  in [`plugin_split_design.md`](plugin_split_design.md) remain valid.
- The `LiveProvider` interface design is unblocked.
- Branch strategy and PR cadence follow the staged approach (O1).
- `LICENSE` file changes can be made (O2 — BSL 1.1 with the Option-3
  grant).
- Plugin package structure follows the naming standard (O3).
- CI configuration uses GitHub-hosted runners (O6).

## What remains decided later

Three decisions are explicitly deferred:

- **O5** — v1.0 marker, deferred until after Phase 3.
- **O6** — CI runner re-evaluation, deferred until Phase 4.
- **O9** — Payment provider, deferred until Phase 6.

These deferrals are intentional. Early decisions in those areas would
be premature given current information.

## Risk register update

The following risks from
[`plugin_split_migration.md`](plugin_split_migration.md) §3.2 are
affected by these decisions:

- **R5** (Pyarmor breaks Pydantic/dataclasses): mitigated by the
  stub-test requirement in O7. Status: managed.
- **R6** (licence-server outage locks paying users): mitigated by the
  separate VPS in O8, combined with the 7-day grace period strategy.
  Status: managed.
- **R7** (EU VAT / MiCA exposure): mitigated by the deferred payment
  decision in O9, which allows Phase 6 to be re-evaluated with better
  information. Status: monitored, not yet resolved.

## References

- [`docs/plugin_split_audit.md`](plugin_split_audit.md) — coupling
  analysis.
- [`docs/plugin_split_design.md`](plugin_split_design.md) — proposed
  architecture.
- [`docs/plugin_split_migration.md`](plugin_split_migration.md) —
  phase-by-phase plan.
- This document — operator decisions on the open questions.

## Revision history

- 2026-05-14: initial creation, all 10 questions decided.
