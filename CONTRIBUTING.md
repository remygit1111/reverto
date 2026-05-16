# Contributing to Reverto

Thank you for your interest in Reverto. This document explains how
to engage with the project given its current development stage.

---

## Current Contribution Policy

Reverto is in a stage where **the maintainer is not accepting code
contributions** (pull requests) at this time. Contributor License
Agreement (CLA) infrastructure is not yet in place, and accepting
PRs without one would complicate the BSL 1.1 → Apache 2.0 license
conversion that happens automatically four years after each release.

**What this means in practice:**

- ✅ **Issues are welcome.** Bug reports, feature requests,
  documentation suggestions, and questions about behavior are all
  valuable input.
- ❌ **Pull requests are not being accepted** at this time. PRs
  may be closed without review.
- 🔄 **This policy may change** once CLA infrastructure is in
  place. Updates will be announced here when that happens.

If you find something that needs fixing, please open an issue
rather than a PR. The maintainer will evaluate and address it on
a best-effort basis.

---

## How to Open a Good Issue

Useful issues include:

- **Clear title**: describe the problem, not your reaction to it.
  "Bot crashes when restart_bot called twice in 1s" is better than
  "broken".
- **Reproduction steps**: exact commands, configuration, or
  scenarios that trigger the issue.
- **Expected vs actual behavior**: what did you expect to happen?
  What actually happened?
- **Environment details**: Reverto version (`python main_paper.py
  --version`), Python version, OS, exchange.
- **Logs or error messages**: relevant excerpts, redacted of
  secrets.

For security vulnerabilities, do NOT open a public issue. See
[SECURITY.md](SECURITY.md) for private disclosure instructions.

---

## What Issues Are NOT For

Please **don't** open issues for:

- **Trading strategy advice.** Reverto faithfully executes the
  strategy you configured; the maintainer is not a financial
  advisor and won't comment on strategy choices.
- **Requests to add support for assets other than BTC inverse
  perpetuals.** Reverto is purpose-built for that market. Other
  markets are out of scope.
- **Requests for hosted/managed service offerings.** Reverto is
  self-host software. No hosted service exists or is planned for
  the framework itself.
- **Discussion of the commercial Live Plugin pricing or
  availability.** That information will be published at
  [reverto.bot](https://reverto.bot) when ready. Speculation
  isn't productive in the issue tracker.
- **General Bitcoin or crypto discussion.** Reddit, Twitter/X,
  Stack Exchange. Many better venues exist.

---

## Code of Conduct

By participating in this project (opening issues, commenting on
discussions, etc.), you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

The short version: be respectful, stay on-topic, and remember that
this is a volunteer-maintained project.

---

## Future Direction

If you're interested in contributing code in the future, here's
what you can expect:

1. **CLA setup will need to happen first.** Once a CLA process is
   in place, this policy will be updated to specify how to sign and
   submit one.
2. **Contributions will be expected to follow the existing style.**
   The codebase has conventions around testing, formatting, and
   commit messages that contributors will need to match.
3. **Trading-strategy contributions are unlikely to be accepted.**
   Strategy choices are a matter of personal preference and risk
   tolerance; the maintainer's strategy is their own and won't be
   modified based on outside input.

For now, the best way to contribute is to open thoughtful issues
that help improve the framework for everyone who self-hosts it.

---

**Maintained by**: remy1111 ([@remygit1111](https://github.com/remygit1111))
