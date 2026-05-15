"""LiveProvider Protocol — framework-side seam for the live trading
plugin.

This is the interface that the closed-source `reverto-live` plugin
implements. Framework code calls methods on a LiveProvider instance
loaded lazily via core.plugin_loader.load_live_provider().

The plugin lives in a separate package and the framework never
imports it directly. Protocols (PEP 544) let the plugin satisfy
this interface structurally without inheriting from a framework
base class — avoiding circular imports between the framework and
its plugin.

When the plugin is not installed, the loader returns None and
callers degrade gracefully (typically: refuse live-only operations
with a clear error message; framework remains fully functional for
paper trading and backtesting).

## Interface versioning

The `interface_version` class attribute is a runtime integer
contract. When a plugin advertises a different version from
`SUPPORTED_INTERFACE_VERSION`, the loader refuses to use it.

Bump SUPPORTED_INTERFACE_VERSION when:
- Adding a NEW required method to LiveProvider
- Changing an existing method's signature
- Changing return-value semantics

Do NOT bump when:
- Adding a new OPTIONAL method (callers must check hasattr first)
- Fixing documentation
- Refactoring the plugin's internal implementation

See `docs/plugin_split_decisions.md` for the broader rationale.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from config.models import BotConfig


# The interface version the framework supports. Plugins must match
# this exact value or the loader refuses to use them. Bump when
# adding required methods or changing existing signatures.
SUPPORTED_INTERFACE_VERSION = 1


@runtime_checkable
class LiveProvider(Protocol):
    """Interface for the live trading plugin.

    Framework code calls this; the plugin (in the separate
    `reverto-live` package) provides the implementation.

    Loaded lazily via core.plugin_loader.load_live_provider().
    Returns None if the plugin is not installed; callers degrade
    gracefully (typically: refuse live operations, fully functional
    paper trading + backtesting).

    Interface version 1 (the current). Plugins must match exactly
    via the `interface_version` attribute.
    """

    interface_version: int  # = SUPPORTED_INTERFACE_VERSION

    # ── Bot lifecycle ──────────────────────────────────────────────

    async def start_bot_dry_run(
        self, user_id: int, slug: str,
    ) -> dict:
        """Spawn the live runner subprocess for a given bot slug in
        dry-run mode.

        Args:
            user_id: tenant owning the bot.
            slug: bot slug (filesystem-safe identifier).

        Returns:
            dict matching framework's start_bot shape:
            - {"ok": True, "message": str} on success
            - {"ok": False, "error": str} on failure
        """
        ...

    async def is_live_config(self, config: BotConfig) -> bool:
        """Determine whether a config will boot under LiveEngine.

        Lets framework dispatch without importing Mode enum or
        checking config.mode directly. Plugins may apply additional
        criteria (license check, plugin-specific config fields).

        Args:
            config: a fully-loaded BotConfig.

        Returns:
            True iff this config would run under LiveEngine.
        """
        ...

    # ── Portal data hooks ─────────────────────────────────────────

    async def list_live_slugs(self, user_id: int) -> set[str]:
        """Return slugs of live-mode bots owned by the given user.

        Replaces the inline YAML scan in
        web/routes/portfolio.py:_live_bot_slugs. Plugin may consult
        the YAML config files, an internal cache, or both.

        Args:
            user_id: tenant whose bots to enumerate.

        Returns:
            Set of bot slugs currently configured as live-mode.
        """
        ...

    # ── Optional callbacks ────────────────────────────────────────

    def on_breaker_permanent_open(
        self, breaker_name: str, reason: str,
    ) -> None:
        """Called when a circuit breaker latches permanently open.

        The plugin typically fans this out to Telegram for every
        connected user (the broadcast logic lives in the plugin so
        framework doesn't need Telegram credentials).

        Framework's default (when no plugin is installed) is to
        log + stop the affected bot locally; this callback adds
        the broadcast fan-out.

        Args:
            breaker_name: identifier of the breaker that latched.
            reason: human-readable explanation for the latch.
        """
        ...
