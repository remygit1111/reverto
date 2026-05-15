# Reverto Releases

Tracking of Reverto release versions, their Business Source License
parameters, and Change Date conversion to Apache 2.0.

## How BSL versioning works

Each release of Reverto is licensed under BSL 1.1 with a specific
Change Date (4 years from release date). On the Change Date, that
specific version automatically converts to Apache License 2.0.

This is a "rolling" conversion - newer releases remain under BSL,
while older releases (4+ years old) become Apache 2.0. Practical
implications:

- Your latest version is always BSL-protected
- Old versions (4+ years) become Apache 2.0 but are practically
  obsolete by then
- Anyone with a copy from before a license switch keeps the rights
  granted by that copy's license

## Release table

| Version | Released | Change Date | Status | Notes |
|---------|----------|-------------|--------|-------|
| v0.x-dev | 2026-05-15 | 2030-05-15 | BSL 1.1 | Initial BSL release. Previous versions under Apache 2.0. |

## Pre-BSL history

Versions released before 2026-05-15 were under Apache License 2.0.
Those rights remain in effect for anyone who had access to those
versions during their Apache 2.0 distribution period. The license
switch to BSL applies only to versions released on or after
2026-05-15.

## Adding a new release entry

When releasing a new version:

1. Run `make release` (see Makefile target)
2. Or manually update LICENSE file (Licensed Work version + Change
   Date) and add a row to the release table above

## References

- [LICENSE](../LICENSE) - current BSL 1.1 license text
- [docs/plugin_split_decisions.md](plugin_split_decisions.md) O2 -
  rationale for BSL choice
