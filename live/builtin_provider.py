"""BuiltinLiveProvider — temporary in-tree LiveProvider scaffold.

⚠️ THIS FILE IS TEMPORARY SCAFFOLDING ⚠️

Phase 3.7 of the plugin-split migration plan deletes this entire
file when the real `reverto-live` pip package becomes available.
See docs/plugin_split_migration.md §3.1 (Task 2.4) and Phase 3.7
for the migration plan.

## Purpose

This class implements the LiveProvider Protocol (core/live_provider.py)
by wrapping the still-in-tree `live/` package. It exists during the
2-week interim window between:
- Phase 2 (TradingEngine extract + plugin seam introduced)
- Phase 3 (live/ physically moves to reverto-live repo)

During this window the framework needs a working LiveProvider so
Tasks 2.5-2.7 can refactor web/ routes against a real provider.

## Why delegate / snapshot from web/ ?

- ``start_bot_dry_run`` *delegates* to ``web.app.start_bot_dry_run``
  (imported inside the method so there is no circular import at
  module load). The spec sketched a "duplicate the validation then
  call the original" pattern, but the real
  ``web.app.start_bot_dry_run`` already performs every validation
  AND calls ``registry.begin_start()`` itself — running the
  validation twice would invoke ``begin_start()`` a second time and
  always fail with "Bot is already starting". Pure delegation is
  behaviourally identical to the production path and side-effect
  safe.
- ``list_live_slugs`` is a verbatim snapshot of the *current*
  ``web/routes/portfolio.py:_live_bot_slugs`` (the real logic reads
  a nested ``bot:`` block, case-insensitively — not a flat
  top-level ``mode`` key). It is copied so Tasks 2.5-2.7 can rewire
  web/ to call this provider without a behaviour change; circular
  imports prevent importing it from web/ once that rewire happens.

The snapshot is frozen at Task 2.4 time and is NOT maintained as
web/ changes — Phase 3.7 deletes this file entirely.

## What this is NOT

- NOT a production live plugin (that's the future `reverto-live`)
- NOT a long-term abstraction (it's deleted in 2-6 weeks)
- NOT a model for plugin implementations (the real plugin will
  live in a separate repo and have different concerns)
"""

from __future__ import annotations

import logging

import yaml

from config.models import BotConfig, Mode
from core import paths
from core.live_provider import SUPPORTED_INTERFACE_VERSION


logger = logging.getLogger(__name__)


class BuiltinLiveProvider:
    """In-tree LiveProvider implementation.

    Temporary scaffolding — see module docstring.

    Implements the LiveProvider Protocol by wrapping the still-in-tree
    `live/` package. All four Protocol methods are implemented;
    `on_breaker_permanent_open` is a no-op until Task 2.8 refactors
    the circuit-breaker callback wiring.
    """

    interface_version: int = SUPPORTED_INTERFACE_VERSION

    # ── Bot lifecycle ─────────────────────────────────────────────

    async def start_bot_dry_run(
        self, user_id: int, slug: str,
    ) -> dict:
        """Spawn a LIVE-mode bot in dry-run via main_live.py.

        Delegates to web.app.start_bot_dry_run (the production code
        path) rather than duplicating its ~70 lines of validation +
        subprocess spawn. The import is performed inside the method
        so there is no circular import at module load time
        (core.plugin_loader imports this module).

        Args:
            user_id: tenant owning the bot
            slug: bot slug (filesystem-safe identifier)

        Returns:
            {"ok": True, "message": str} on success
            {"ok": False, "error": str} on failure
        """
        from web.app import start_bot_dry_run as _web_start_bot_dry_run

        return await _web_start_bot_dry_run(user_id, slug)

    async def is_live_config(self, config: BotConfig) -> bool:
        """Determine whether a config will boot under LiveEngine.

        For the in-tree scaffold this is a simple mode check.
        The real plugin may apply additional criteria.
        """
        return config.mode == Mode.LIVE

    # ── Portal data hooks ─────────────────────────────────────────

    async def list_live_slugs(self, user_id: int) -> set[str]:
        """Return slugs of live-mode bots for the user.

        Verbatim snapshot of web/routes/portfolio.py:_live_bot_slugs
        as of Task 2.4 of the plugin-split migration. The real helper
        reads a nested ``bot:`` block (case-insensitive ``mode``),
        which this copy mirrors exactly so a future Task 2.7 rewire
        is behaviour-preserving.
        """
        # ── COPY of web/routes/portfolio.py:_live_bot_slugs logic ──
        # (frozen snapshot; not maintained as web/ changes — deleted
        # in Phase 3.7).
        user_dir = paths.user_bots_dir(user_id)
        if not user_dir.exists():
            return set()
        live: set[str] = set()
        for yaml_path in user_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            block = data.get("bot") if isinstance(data, dict) else None
            if not isinstance(block, dict):
                continue
            mode = block.get("mode")
            if isinstance(mode, str) and mode.lower() == "live":
                live.add(yaml_path.stem)
        return live

    # ── Optional callbacks ────────────────────────────────────────

    def on_breaker_permanent_open(
        self, breaker_name: str, reason: str,
    ) -> None:
        """No-op for BuiltinLiveProvider.

        Task 2.8 will rewire the circuit-breaker callback through
        the LiveProvider Protocol. Until then, the existing
        in-process telegram fan-out in core/circuit_breaker.py
        remains the active path. This method exists to satisfy
        the Protocol; it does not yet receive calls.
        """
        logger.debug(
            "BuiltinLiveProvider.on_breaker_permanent_open called: "
            "breaker=%s reason=%s (no-op until Task 2.8)",
            breaker_name,
            reason,
        )


# Module-level provider singleton — used by core/plugin_loader's
# fallback when the reverto_live package is not installed.
provider = BuiltinLiveProvider()
