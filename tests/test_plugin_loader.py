"""Tests for core.plugin_loader.

Verify:
- Loader returns None when reverto_live is not installed
- Loader returns the provider when reverto_live is installed and
  valid
- Loader rejects plugin with wrong interface_version
- Loader rejects plugin without `provider` attribute
- Loader rejects provider without `interface_version` attribute
- Loader caches results (second call returns same object)
- reset_cache() works
"""

import logging
import sys
from unittest.mock import MagicMock, patch

from core import plugin_loader
from core.live_provider import (
    LiveProvider,
    SUPPORTED_INTERFACE_VERSION,
)


class TestPluginLoader:
    """Verify the live plugin loader handles all states correctly."""

    def setup_method(self):
        """Reset cache before each test for isolation."""
        plugin_loader.reset_cache()

    def teardown_method(self):
        """Clean up sys.modules and cache after each test."""
        sys.modules.pop("reverto_live", None)
        plugin_loader.reset_cache()

    def test_external_plugin_not_installed_uses_builtin(self):
        """When reverto_live is not installed, the loader falls back
        to the in-tree BuiltinLiveProvider scaffold (Task 2.4)."""
        from live.builtin_provider import BuiltinLiveProvider

        # Ensure reverto_live is not in sys.modules
        sys.modules.pop("reverto_live", None)

        result = plugin_loader.load_live_provider()

        assert result is not None
        assert isinstance(result, BuiltinLiveProvider)
        assert result.interface_version == SUPPORTED_INTERFACE_VERSION

    def test_plugin_installed_returns_provider(self):
        """When reverto_live is installed with valid provider,
        loader returns it.
        """
        # Create a mock module with a valid provider
        mock_module = MagicMock()
        mock_provider = MagicMock()
        mock_provider.interface_version = SUPPORTED_INTERFACE_VERSION
        mock_module.provider = mock_provider

        with patch.dict(sys.modules, {"reverto_live": mock_module}):
            result = plugin_loader.load_live_provider()

        assert result is mock_provider

    def test_wrong_interface_version_returns_none(self):
        """When plugin has wrong interface_version, loader returns
        None and logs an error.
        """
        mock_module = MagicMock()
        mock_provider = MagicMock()
        # Deliberately mismatched version
        mock_provider.interface_version = SUPPORTED_INTERFACE_VERSION + 1
        mock_module.provider = mock_provider

        with patch.dict(sys.modules, {"reverto_live": mock_module}):
            result = plugin_loader.load_live_provider()

        assert result is None

    def test_missing_provider_attribute_returns_none(self):
        """When reverto_live has no `provider` attribute, loader
        returns None.
        """
        mock_module = MagicMock(spec=[])  # No attributes

        with patch.dict(sys.modules, {"reverto_live": mock_module}):
            result = plugin_loader.load_live_provider()

        assert result is None

    def test_provider_missing_interface_version_returns_none(self):
        """When provider lacks interface_version, loader returns None."""
        mock_module = MagicMock()
        mock_provider = MagicMock(spec=[])  # No attributes
        mock_module.provider = mock_provider

        with patch.dict(sys.modules, {"reverto_live": mock_module}):
            result = plugin_loader.load_live_provider()

        assert result is None

    def test_loader_caches_result(self):
        """Second call returns the same object as first call."""
        mock_module = MagicMock()
        mock_provider = MagicMock()
        mock_provider.interface_version = SUPPORTED_INTERFACE_VERSION
        mock_module.provider = mock_provider

        with patch.dict(sys.modules, {"reverto_live": mock_module}):
            first = plugin_loader.load_live_provider()
            second = plugin_loader.load_live_provider()

        assert first is second
        assert first is mock_provider

    def test_external_plugin_takes_precedence(self):
        """When a valid external reverto_live IS installed, it is
        returned — the in-tree BuiltinLiveProvider is NOT used.
        """
        from live.builtin_provider import BuiltinLiveProvider

        mock_module = MagicMock()
        mock_provider = MagicMock()
        mock_provider.interface_version = SUPPORTED_INTERFACE_VERSION
        mock_module.provider = mock_provider

        with patch.dict(sys.modules, {"reverto_live": mock_module}):
            result = plugin_loader.load_live_provider()

        assert result is mock_provider
        assert not isinstance(result, BuiltinLiveProvider)

    def test_loader_caches_builtin_fallback(self):
        """When the external plugin is not installed, the builtin
        fallback is cached — subsequent calls return the same
        instance without re-importing.
        """
        from live.builtin_provider import BuiltinLiveProvider

        sys.modules.pop("reverto_live", None)

        first = plugin_loader.load_live_provider()
        second = plugin_loader.load_live_provider()

        assert first is second
        assert isinstance(first, BuiltinLiveProvider)

    def test_reset_cache_allows_reload(self):
        """After reset_cache(), loader re-imports and may return
        different result.
        """
        mock_module = MagicMock()
        mock_provider = MagicMock()
        mock_provider.interface_version = SUPPORTED_INTERFACE_VERSION
        mock_module.provider = mock_provider

        with patch.dict(sys.modules, {"reverto_live": mock_module}):
            first = plugin_loader.load_live_provider()
            assert first is mock_provider

        # Plugin "uninstalled" — pop from sys.modules
        sys.modules.pop("reverto_live", None)

        # Without reset, cached value is returned (even though
        # plugin is gone now)
        cached = plugin_loader.load_live_provider()
        assert cached is mock_provider

        # With reset, loader re-imports, finds no external plugin,
        # and falls back to the in-tree BuiltinLiveProvider scaffold.
        from live.builtin_provider import BuiltinLiveProvider

        plugin_loader.reset_cache()
        after_reset = plugin_loader.load_live_provider()
        assert isinstance(after_reset, BuiltinLiveProvider)


class TestLiveProviderProtocol:
    """Verify the LiveProvider Protocol structure."""

    def test_supported_interface_version_is_int(self):
        """The version constant must be an integer."""
        assert isinstance(SUPPORTED_INTERFACE_VERSION, int)
        assert SUPPORTED_INTERFACE_VERSION >= 1

    def test_protocol_has_required_methods(self):
        """LiveProvider Protocol declares the four expected methods."""
        # Protocols don't have __abstractmethods__, but we can
        # check the annotations / dir() for method names
        expected = {
            "start_bot_dry_run",
            "is_live_config",
            "list_live_slugs",
            "on_breaker_permanent_open",
        }
        actual = {name for name in dir(LiveProvider)
                  if not name.startswith("_")}
        # interface_version is in there too
        actual.discard("interface_version")

        assert expected.issubset(actual), (
            f"Missing methods: {expected - actual}"
        )

    def test_protocol_is_runtime_checkable(self):
        """LiveProvider should be @runtime_checkable for isinstance
        checks.
        """
        # Construct a dict that has all the required attributes
        mock = MagicMock()
        mock.interface_version = SUPPORTED_INTERFACE_VERSION
        mock.start_bot_dry_run = MagicMock()
        mock.is_live_config = MagicMock()
        mock.list_live_slugs = MagicMock()
        mock.on_breaker_permanent_open = MagicMock()

        # This should not raise — runtime_checkable allows isinstance
        assert isinstance(mock, LiveProvider)


class TestBuiltinLiveProvider:
    """Verify the temporary in-tree BuiltinLiveProvider scaffold
    (live/builtin_provider.py — deleted in Phase 3.7)."""

    def test_builtin_provider_implements_protocol(self):
        """The module-level `provider` singleton structurally
        satisfies the LiveProvider Protocol.
        """
        from live.builtin_provider import BuiltinLiveProvider, provider

        assert isinstance(provider, BuiltinLiveProvider)
        assert isinstance(provider, LiveProvider)
        assert provider.interface_version == SUPPORTED_INTERFACE_VERSION

    def test_builtin_is_live_config_returns_true_for_live_mode(self):
        """is_live_config() is True for a live-mode config."""
        import asyncio

        from config.models import Mode
        from live.builtin_provider import BuiltinLiveProvider

        cfg = MagicMock()
        cfg.mode = Mode.LIVE
        result = asyncio.run(BuiltinLiveProvider().is_live_config(cfg))
        assert result is True

    def test_builtin_is_live_config_returns_false_for_paper_mode(self):
        """is_live_config() is False for a non-live config."""
        import asyncio

        from config.models import Mode
        from live.builtin_provider import BuiltinLiveProvider

        cfg = MagicMock()
        cfg.mode = Mode.PAPER
        result = asyncio.run(BuiltinLiveProvider().is_live_config(cfg))
        assert result is False


class TestPUBv1002PluginLoaderHardening:
    """Class-of-issue regression for PUB-v1-002 (LOW).

    load_live_provider() had two compounding gaps pre-fix:

    1. Too-narrow catch — only ``ImportError`` was handled. Any
       other exception at import time (RuntimeError, OSError, …)
       propagated out, breaking the documented never-raises
       contract that web/app.py callers depend on.
    2. Cache poisoning — ``_loaded = True`` was set BEFORE the
       try-block. A propagated exception left ``_loaded=True`` /
       ``_provider=None``, so every later call returned None for
       the whole process lifetime.

    Post-fix: three exception buckets (ModuleNotFoundError split
    by ``exc.name``, other ImportError, any other Exception) all
    fall back to the builtin scaffold, and ``_loaded`` flips only
    after a terminal ``_provider`` assignment.

    These tests patch ``importlib.import_module`` to raise each
    failure mode. (_load_builtin_provider() uses a statement-level
    ``from live.builtin_provider import provider`` — NOT
    importlib.import_module — so the patch does not disturb the
    fallback path.)
    """

    def setup_method(self):
        plugin_loader.reset_cache()

    def teardown_method(self):
        sys.modules.pop("reverto_live", None)
        plugin_loader.reset_cache()

    def test_module_not_found_reverto_live_uses_builtin_and_logs_info(
        self, caplog,
    ):
        """Routine path: reverto_live itself not installed →
        INFO log, builtin fallback, NO error."""
        from live.builtin_provider import BuiltinLiveProvider

        err = ModuleNotFoundError(
            "No module named 'reverto_live'", name="reverto_live",
        )
        with caplog.at_level(logging.INFO, logger="core.plugin_loader"):
            with patch.object(
                plugin_loader.importlib, "import_module",
                side_effect=err,
            ):
                result = plugin_loader.load_live_provider()

        assert isinstance(result, BuiltinLiveProvider)
        assert any(
            "not installed" in r.getMessage() for r in caplog.records
        )
        # The routine path must not log at ERROR — an operator
        # filtering for ERROR should see nothing for "plugin
        # simply unshipped".
        assert not any(
            r.levelno >= logging.ERROR for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_transitive_module_not_found_logs_error(self, caplog):
        """reverto_live IS installed but a transitive dep is
        missing → ERROR log (operator-actionable), builtin
        fallback."""
        from live.builtin_provider import BuiltinLiveProvider

        err = ModuleNotFoundError(
            "No module named 'some_transitive_dep'",
            name="some_transitive_dep",
        )
        with caplog.at_level(logging.ERROR, logger="core.plugin_loader"):
            with patch.object(
                plugin_loader.importlib, "import_module",
                side_effect=err,
            ):
                result = plugin_loader.load_live_provider()

        assert isinstance(result, BuiltinLiveProvider)
        error_records = [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert len(error_records) >= 1
        assert "transitive" in error_records[0].getMessage().lower()
        # The missing-module name is surfaced for the operator.
        assert "some_transitive_dep" in error_records[0].getMessage()

    def test_other_import_error_logs_error(self, caplog):
        """A non-ModuleNotFoundError ImportError (circular import,
        explicit raise in package __init__) → ERROR, builtin."""
        from live.builtin_provider import BuiltinLiveProvider

        with caplog.at_level(logging.ERROR, logger="core.plugin_loader"):
            with patch.object(
                plugin_loader.importlib, "import_module",
                side_effect=ImportError("circular import detected"),
            ):
                result = plugin_loader.load_live_provider()

        assert isinstance(result, BuiltinLiveProvider)
        error_records = [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert len(error_records) >= 1
        assert "ImportError" in error_records[0].getMessage()

    def test_non_import_exception_does_not_propagate(self, caplog):
        """The core contract: load_live_provider NEVER raises.

        Pre-fix only ImportError was caught, so a RuntimeError from
        the plugin's package __init__ propagated straight out and
        crashed the web/app.py caller. This test calls the loader
        OUTSIDE any pytest.raises — if it raises, the test errors.
        """
        from live.builtin_provider import BuiltinLiveProvider

        with caplog.at_level(logging.ERROR, logger="core.plugin_loader"):
            with patch.object(
                plugin_loader.importlib, "import_module",
                side_effect=RuntimeError("plugin __init__ blew up"),
            ):
                result = plugin_loader.load_live_provider()  # must not raise

        assert isinstance(result, BuiltinLiveProvider)
        error_records = [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert len(error_records) >= 1
        assert "non-import" in error_records[0].getMessage().lower()

    def test_cache_not_poisoned_by_transient_exception(self):
        """Critical regression test for the cache-ordering fix.

        Pre-fix: a propagated exception set ``_loaded=True`` with
        ``_provider=None``; every later call returned None for the
        process lifetime — a single transient import failure
        permanently disabled live capability until restart.

        Post-fix: ``_loaded`` flips only after ``_provider`` holds a
        terminal value, so the first (failing) call still produces
        a usable builtin provider and the cached value is that
        provider, never None.
        """
        # First call hits the exception path.
        with patch.object(
            plugin_loader.importlib, "import_module",
            side_effect=RuntimeError("transient import blow-up"),
        ):
            first = plugin_loader.load_live_provider()
        assert first is not None, (
            "PUB-v1-002 regression: an exception left the loader "
            "returning None instead of the builtin fallback"
        )

        # Second call — no patch. The cache must hand back the same
        # non-None provider, NOT a poisoned None.
        second = plugin_loader.load_live_provider()
        assert second is first
        assert plugin_loader._loaded is True
        assert plugin_loader._provider is not None, (
            "PUB-v1-002 regression: cache poisoned — _loaded=True "
            "but _provider=None"
        )

    def test_successful_load_caches_real_provider(self):
        """Happy path: importlib returns a well-formed module whose
        ``provider`` attribute carries the supported interface
        version. The external provider is returned and cached.

        NB: the loader reads ``module.provider`` (an already-
        instantiated attribute), NOT ``module.LiveProvider()`` — the
        mock mirrors that shape.
        """
        from types import SimpleNamespace

        fake_provider = SimpleNamespace(
            interface_version=SUPPORTED_INTERFACE_VERSION,
        )
        fake_module = SimpleNamespace(provider=fake_provider)

        with patch.object(
            plugin_loader.importlib, "import_module",
            return_value=fake_module,
        ):
            result = plugin_loader.load_live_provider()
        assert result is fake_provider

        # Cached — a second call returns the same external provider
        # without re-importing.
        second = plugin_loader.load_live_provider()
        assert second is fake_provider
        assert plugin_loader._loaded is True
