# Security Policy

Reverto is a self-hosted trading bot that handles exchange API
keys and (in live mode) authorises real trades. Security
vulnerabilities can put real funds at risk, so we take reports
seriously and respond as quickly as a volunteer-maintained
project can.

## Reporting a vulnerability

If you discover a security vulnerability in Reverto, please
report it **privately**.

**Do NOT** open a public GitHub Issue or post the details in any
public discussion before a fix has shipped.

**Do** use one of these channels:

- **Preferred: GitHub Security Advisory.** Visit the repository
  on GitHub, open the **Security** tab, and click **Report a
  vulnerability**. This creates a private advisory only the
  maintainers can read. Direct link:
  <https://github.com/remygit1111/reverto/security/advisories/new>
- **Fallback.** If GitHub Security Advisories are not available
  for the fork you found this in, open a public GitHub Issue
  titled **`[SECURITY] Request private disclosure channel`** and
  ask the maintainer to provide a private channel, without
  including any vulnerability details in the public issue.

We do not currently publish a dedicated security email. If you
prefer email and the maintainer publishes one in their GitHub
profile, you may use that, but the GitHub Security Advisory flow
is the path with the cleanest audit trail.

## What to include

A useful report includes:

- **Description**: what the vulnerability is, in one or two
  sentences.
- **Reproduction steps**: exact commands, requests, or
  configuration that exposes the issue. A minimal reproduction is
  far more useful than a long screencast.
- **Affected versions / commits**: the commit SHA you tested
  against (or the version tag if releases exist by then).
- **Impact**: what can an attacker do? Read another user's
  data? Place trades they shouldn't? Bypass authentication?
- **Suggested fix**: optional. If you know how to close it,
  share what you have. We won't take offence if a maintainer
  writes a different patch.

You don't need to draft a full advisory; we'll iterate with you
on wording before public disclosure.

## Response timeline

This is a volunteer-maintained project. There is no
guaranteed SLA. Best-effort response targets:

- **Acknowledgement**: within 7 days.
- **Initial assessment**: within 14 days, including a
  preliminary severity call and a rough fix ETA.
- **Fix or response**: within 30 days for confirmed
  vulnerabilities. Complex issues may take longer; we'll keep
  you in the loop.

If you don't hear back inside the acknowledgement window, please
nudge. Reports occasionally get buried under other work.

## Supported versions

The latest commit on `main` is the supported version. Older
commits and historical tags are not maintained; once a fix lands
on `main`, that becomes the new baseline.

Self-hosters are responsible for pulling updates regularly. The
project does not push security patches; you must `git pull` to
receive them.

## Disclosure policy

After a fix has shipped on `main`, the vulnerability may be
publicly disclosed, typically 30 to 90 days after the fix,
depending on how widely the unfixed code is in use. The exact
timing is coordinated with the reporter.

Reporters are credited in the public advisory unless they prefer
to remain anonymous. Tell us your preference in the report.

## Out of scope

The following are explicitly **not** considered Reverto
vulnerabilities:

- **Loss of funds due to trading-strategy choices.** Reverto
  faithfully executes the strategy you configured. A bad strategy
  losing money is a strategy issue, not a Reverto issue.
- **Loss of funds due to user-provided API keys being misused.**
  Reverto stores exchange API keys encrypted at rest with a
  Fernet key. If you store keys with permissions broader than
  Reverto needs, an attacker who compromises the host can use
  those broader permissions. See
  [docs/exchange-permissions.md](docs/exchange-permissions.md)
  for the recommended minimum permissions.
- **Issues only reproducible with privileged local access.** An
  attacker who already has shell access on the host has the
  Fernet key on disk and the SQLite DB, both of which are out of
  Reverto's threat model to defend against. Defence-in-depth
  improvements are still welcome, but a "host root can read
  files" report is not a vulnerability.
- **Issues in third-party dependencies.** Report those upstream
  (pypi package, ccxt, FastAPI, etc.). We track dependency CVEs
  via routine updates but don't accept third-party CVEs as
  Reverto reports.
- **Theoretical issues without a concrete attack path.** "X
  could in principle be insecure" without a proof-of-concept or
  a clear scenario isn't actionable. We're happy to discuss
  hardening proposals as regular issues, just not as security
  reports.
- **Self-DoS via misconfiguration.** Setting
  `max_orders=50` and burning through a `max_cumulative_size: null`
  is a foot-gun, not a vulnerability.

When in doubt, report it. We'd rather triage one extra non-issue
than miss a real one.
