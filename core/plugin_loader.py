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


def _load_builtin_provider() -> Optional[LiveProvider]:
    """Fall back to the in-tree BuiltinLiveProvider scaffold.

    ⚠️ TEMPORARY — Phase 3.7 deletes live/builtin_provider.py and this
    fallback together when the real reverto-live pip package ships.

    Used only when the external reverto_live package is *not
    installed* (ImportError). An external plugin that IS installed
    but malformed/incompatible never reaches here — those are
    operator errors surfaced as None by load_live_provider().
    """
    try:
        from live.builtin_provider import provider as _builtin
    except ImportError as e:
        logger.error(
            "BuiltinLiveProvider import failed: %s — framework "
            "live functionality unavailable",
            e,
        )
        return None

    if _builtin.interface_version != SUPPORTED_INTERFACE_VERSION:
        logger.error(
            "BuiltinLiveProvider interface_version=%d does not match "
            "framework SUPPORTED_INTERFACE_VERSION=%d",
            _builtin.interface_version,
            SUPPORTED_INTERFACE_VERSION,
        )
        return None

    logger.info(
        "Using BuiltinLiveProvider (in-tree scaffold) — Phase 3 will "
        "replace it with the reverto_live pip package"
    )
    return _builtin


def load_live_provider() -> Optional[LiveProvider]:
    """Load the live trading plugin.

    Prefers the external `reverto_live` pip package. When that
    package is not installed, falls back to the framework-internal
    BuiltinLiveProvider scaffold (temporary — removed in Phase 3.7).
    Result is cached; subsequent calls return the same value without
    re-importing.

    Returns None only when the external plugin is installed but
    malformed/incompatible, or when even the built-in scaffold fails
    to import (which should never happen in practice).

    Returns:
        LiveProvider instance (external plugin, else built-in
        scaffold) on success. None when the external plugin is
        installed-but-broken or the scaffold itself is unavailable.
    """
    global _provider, _loaded

    if _loaded:
        return _provider

    _loaded = True

    try:
        module = importlib.import_module("reverto_live")
    except ImportError:
        # External plugin not installed — use the in-tree scaffold
        # so the framework stays live-capable during the Phase 2-3
        # interim window.
        _provider = _load_builtin_provider()
        return _provider

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
