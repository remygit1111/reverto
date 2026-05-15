"""Single source of truth for Reverto's version.

This module is imported by:
- pyproject.toml (via dynamic version reading, if configured)
- main_*.py entry points (for --version flag)
- Anywhere else that needs the version string

When releasing a new version:
1. Update __version__ here
2. Run `make release VERSION=<new>` to update LICENSE
3. Tag the release: git tag v<new> && git push origin v<new>
4. Add an entry to docs/RELEASES.md

See docs/plugin_split_decisions.md O5 for versioning strategy.
"""

__version__ = "0.5.0"
