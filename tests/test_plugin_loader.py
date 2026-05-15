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

    def test_plugin_not_installed_returns_none(self):
        """When reverto_live is not installed, loader returns None."""
        # Ensure reverto_live is not in sys.modules
        sys.modules.pop("reverto_live", None)

        result = plugin_loader.load_live_provider()

        assert result is None

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

    def test_loader_caches_none_result(self):
        """When plugin not installed, subsequent calls also return
        None without re-importing.
        """
        sys.modules.pop("reverto_live", None)

        first = plugin_loader.load_live_provider()
        second = plugin_loader.load_live_provider()

        assert first is None
        assert second is None

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

        # With reset, loader re-imports and finds no plugin
        plugin_loader.reset_cache()
        after_reset = plugin_loader.load_live_provider()
        assert after_reset is None


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
