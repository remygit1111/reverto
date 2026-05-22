"""Plugin loader for the live trading plugin.

Lazily imports `reverto_live` (the closed-source plugin package)
and exposes its provider object. When the plugin is not installed
— or fails to import — the loader falls back to the in-tree
`BuiltinLiveProvider` scaffold so the framework stays live-capable
during the Phase 2-3 interim window.

Loading is cached: subsequent calls return the same provider
instance (or the same None). Use `reset_cache()` for tests.

## Loading sequence

1. Try `importlib.import_module("reverto_live")`.
2. On any import failure, fall back to the builtin scaffold:
   - `ModuleNotFoundError` for `reverto_live` itself → INFO
     log (the routine path while the Phase 3 plugin is
     unshipped), then builtin.
   - any other import-time failure (a missing transitive dep,
     a circular import, or a non-import exception raised by
     the plugin's package `__init__`) → ERROR log with
     traceback, then builtin.
3. If the import succeeded but the module has no `provider`
   attribute: malformed plugin → ERROR log, cached None.
4. If the provider has no `interface_version`, or it !=
   SUPPORTED_INTERFACE_VERSION: incompatible → ERROR log,
   cached None.
5. Otherwise: cache and return the external provider.

## Error handling

The loader never raises (PUB-v1-002) — `web/app.py` callers
(`start_bot_dry_run`, `restart_bot`) rely on this without their
own try/except. Every failure mode is logged and resolves to a
*terminal* cached outcome: a provider (external or builtin) or,
for an installed-but-broken plugin, None.

Callers can distinguish the failure modes by log level:
- Plugin not installed → INFO (expected in framework-only
  deployments).
- Plugin installed-but-broken, or any import-time exception →
  ERROR with traceback (operator needs to investigate).
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

    Used whenever the external reverto_live package fails to import
    — whether it is not installed, has a missing transitive dep, or
    raises any other exception at import time (PUB-v1-002). An
    external plugin that imports cleanly but is malformed or
    version-incompatible never reaches here — those are operator
    errors surfaced as None by load_live_provider().
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
    package is not installed — or fails to import for any other
    reason — falls back to the framework-internal
    BuiltinLiveProvider scaffold (temporary — removed in Phase 3.7).
    Result is cached; subsequent calls return the same value without
    re-importing.

    Never raises (PUB-v1-002): every import-time failure, including
    non-ImportError exceptions, is caught, logged, and resolved to
    the builtin scaffold. The cache flag is set only after a
    terminal outcome, so a transient import failure cannot poison
    the cache into permanently returning None.

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

    # PUB-v1-002: ``module`` stays None when the import fails by any
    # route. The post-import validation block below is then skipped
    # and ``_provider`` — already set to the builtin fallback inside
    # the matching except handler — flows straight to the cached
    # return at the end.
    module = None
    try:
        module = importlib.import_module("reverto_live")
    except ModuleNotFoundError as exc:
        # ModuleNotFoundError subclasses ImportError. Split it:
        # ``exc.name == "reverto_live"`` is the routine path — the
        # Phase 3 plugin is simply unshipped. A DIFFERENT missing
        # module means reverto_live IS installed but one of its
        # transitive deps failed to import: an operator-actionable
        # breakage, not a routine "not installed".
        if exc.name == "reverto_live":
            logger.info(
                "reverto_live not installed; falling back to the "
                "in-tree BuiltinLiveProvider scaffold"
            )
        else:
            logger.error(
                "reverto_live is installed but a transitive import "
                "failed (missing module: %s); falling back to the "
                "builtin scaffold",
                exc.name,
                exc_info=True,
            )
        _provider = _load_builtin_provider()
    except ImportError:
        # A non-ModuleNotFoundError ImportError — a circular import,
        # or an ImportError raised explicitly from reverto_live's
        # package __init__. Always operator-actionable.
        logger.error(
            "reverto_live raised ImportError on load; falling back "
            "to the builtin scaffold",
            exc_info=True,
        )
        _provider = _load_builtin_provider()
    except Exception:
        # PUB-v1-002: anything else raised at module-import time —
        # RuntimeError from a misconfigured C-extension, OSError,
        # AttributeError, etc. Pre-fix this propagated straight out
        # of load_live_provider() and broke the documented
        # never-raises contract that web/app.py callers depend on.
        # Degrade to the builtin scaffold instead of crashing them.
        logger.error(
            "reverto_live failed to load with a non-import "
            "exception; falling back to the builtin scaffold",
            exc_info=True,
        )
        _provider = _load_builtin_provider()

    if module is not None:
        # Import succeeded — validate the plugin's shape. A plugin
        # that imports cleanly but is malformed or version-
        # incompatible is an operator error: surface it as a cached
        # None (re-importing would not fix it) rather than silently
        # masking it with the builtin scaffold.
        if not hasattr(module, "provider"):
            logger.error(
                "Live plugin (reverto_live) is installed but exposes "
                "no `provider` attribute — plugin is malformed; "
                "refusing to use it"
            )
            _provider = None
        else:
            provider = module.provider
            if not hasattr(provider, "interface_version"):
                logger.error(
                    "Live plugin provider has no `interface_version` "
                    "attribute; framework cannot verify compatibility "
                    "— refusing to use it"
                )
                _provider = None
            elif provider.interface_version != SUPPORTED_INTERFACE_VERSION:
                logger.error(
                    "Live plugin interface_version=%d does not match "
                    "framework SUPPORTED_INTERFACE_VERSION=%d — "
                    "refusing to use incompatible plugin",
                    provider.interface_version,
                    SUPPORTED_INTERFACE_VERSION,
                )
                _provider = None
            else:
                logger.info(
                    "Live plugin loaded successfully "
                    "(interface_version=%d)",
                    provider.interface_version,
                )
                _provider = provider

    # PUB-v1-002 (Optie X): ``_loaded`` flips to True ONLY here —
    # after a terminal outcome has been assigned to ``_provider``.
    # Pre-fix it was set before the try-block, so a propagated
    # exception left ``_loaded=True`` with ``_provider=None``,
    # poisoning the cache: every later call returned None for the
    # whole process lifetime. With the assignment last, the
    # invariant ``_loaded is True implies _provider was set by a
    # terminal branch`` always holds.
    _loaded = True
    return _provider


def reset_cache() -> None:
    """Reset the loader cache. Test-only — production code should
    not call this. Useful for tests that want to verify behavior
    across different plugin states.
    """
    global _provider, _loaded
    _provider = None
    _loaded = False
