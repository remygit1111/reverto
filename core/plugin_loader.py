"""Plugin loader for the live trading plugin.

Lazily imports `reverto_live` (the closed-source plugin package)
and exposes its provider object. When the plugin is not installed,
returns None — framework callers must handle this gracefully.

Loading is cached: subsequent calls return the same provider
instance (or the same None). Use `reset_cache()` for tests.

## Loading sequence

1. Try `importlib.import_module("reverto_live")`.
2. If ImportError: plugin not installed → return None.
3. If module has no `provider` attribute: malformed plugin →
   log error, return None.
4. If provider's `interface_version` != SUPPORTED_INTERFACE_VERSION:
   incompatible → log error with version info, return None.
5. Otherwise: cache and return the provider.

## Error handling

The loader never raises. All failure modes return None with a
descriptive log line at ERROR level. Callers can distinguish:
- Plugin not installed → INFO level log (expected in framework-
  only deployments)
- Plugin malformed or wrong version → ERROR level log (operator
  needs to investigate)
"""

from __future__ import annotations

import importlib
import logging
from typing import Optional

from core.live_provider import (
    LiveProvider,
    SUPPORTED_INTERFACE_VERSION,
)

logger = logging.getLogger(__name__)


# Module-level cache. None has two meanings:
# - Not loaded yet (initial state)
# - Loaded and plugin is not installed / not usable
# Use `_loaded` flag to distinguish.
_provider: Optional[LiveProvider] = None
_loaded: bool = False


def load_live_provider() -> Optional[LiveProvider]:
    """Load the live trading plugin, if installed.

    Returns the plugin's provider object on success, or None if the
    plugin is not installed or is incompatible. Result is cached;
    subsequent calls return the same value without re-importing.

    Returns:
        LiveProvider instance if plugin is loaded successfully.
        None if plugin is not installed, malformed, or incompatible.
    """
    global _provider, _loaded

    if _loaded:
        return _provider

    _loaded = True

    try:
        module = importlib.import_module("reverto_live")
    except ImportError:
        logger.info(
            "Live plugin (reverto_live) not installed — "
            "framework runs in paper-only mode"
        )
        _provider = None
        return None

    if not hasattr(module, "provider"):
        logger.error(
            "Live plugin (reverto_live) is installed but exposes no "
            "`provider` attribute — plugin is malformed; refusing to "
            "use it"
        )
        _provider = None
        return None

    provider = module.provider

    if not hasattr(provider, "interface_version"):
        logger.error(
            "Live plugin provider has no `interface_version` "
            "attribute; framework cannot verify compatibility — "
            "refusing to use it"
        )
        _provider = None
        return None

    if provider.interface_version != SUPPORTED_INTERFACE_VERSION:
        logger.error(
            "Live plugin interface_version=%d does not match "
            "framework SUPPORTED_INTERFACE_VERSION=%d — refusing "
            "to use incompatible plugin",
            provider.interface_version,
            SUPPORTED_INTERFACE_VERSION,
        )
        _provider = None
        return None

    logger.info(
        "Live plugin loaded successfully (interface_version=%d)",
        provider.interface_version,
    )
    _provider = provider
    return provider


def reset_cache() -> None:
    """Reset the loader cache. Test-only — production code should
    not call this. Useful for tests that want to verify behavior
    across different plugin states.
    """
    global _provider, _loaded
    _provider = None
    _loaded = False
