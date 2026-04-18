"""Tests for main_live.py — slug validation, DRY_RUN env parsing.

Full integration tests that spawn a subprocess + simulate stdin are
out of scope for the unit test suite; instead we import the module
directly and exercise the helper functions + regex.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

import main_live  # noqa: E402


class TestBotSlugRegex:
    """The path-traversal fix relies on a strict regex applied BEFORE
    Path() construction. Confirm common attack inputs are rejected and
    legitimate slugs accepted."""

    def test_accepts_plain_slug(self):
        assert main_live._BOT_SLUG_RE.match("btc_bot") is not None

    def test_accepts_alphanumeric_mix(self):
        assert main_live._BOT_SLUG_RE.match("BtcBot_123") is not None

    def test_accepts_hyphen(self):
        assert main_live._BOT_SLUG_RE.match("my-bot") is not None

    def test_rejects_parent_dir(self):
        assert main_live._BOT_SLUG_RE.match("../../etc/passwd") is None

    def test_rejects_absolute_path(self):
        assert main_live._BOT_SLUG_RE.match("/etc/passwd") is None

    def test_rejects_spaces(self):
        assert main_live._BOT_SLUG_RE.match("bot name") is None

    def test_rejects_empty(self):
        assert main_live._BOT_SLUG_RE.match("") is None

    def test_rejects_shell_metachars(self):
        assert main_live._BOT_SLUG_RE.match("bot;rm") is None
        assert main_live._BOT_SLUG_RE.match("bot$name") is None


class TestDryRunEnvParsing:
    """DRY_RUN env var used to be strict `== "1"` which broke CI setups
    that set DRY_RUN=true. Now case-insensitive across several common
    truthy strings."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "y", "on"])
    def test_truthy_values(self, value, monkeypatch):
        monkeypatch.setenv("DRY_RUN", value)
        assert main_live._env_is_truthy("DRY_RUN") is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "   "])
    def test_falsy_values(self, value, monkeypatch):
        monkeypatch.setenv("DRY_RUN", value)
        assert main_live._env_is_truthy("DRY_RUN") is False

    def test_unset_is_falsy(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        assert main_live._env_is_truthy("DRY_RUN") is False


class TestAuthenticatedExchange:
    """When live mode is requested without saved exchange credentials
    the helper must return None so the caller exits cleanly rather
    than attempting real orders against a read-only client."""

    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.setattr("core.credentials.get_keys", lambda _name, user_id=1: None)
        assert main_live._authenticated_exchange("bitget") is None

    def test_bitget_requires_passphrase_env(self, monkeypatch):
        """Even with saved keys, BITGET_PASSPHRASE must be set for live."""
        monkeypatch.setattr(
            "core.credentials.get_keys",
            lambda _name, user_id=1: {"api_key": "a", "api_secret": "b"},
        )
        monkeypatch.delenv("BITGET_PASSPHRASE", raising=False)
        assert main_live._authenticated_exchange("bitget") is None

    def test_unknown_exchange_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "core.credentials.get_keys",
            lambda _name, user_id=1: {"api_key": "a", "api_secret": "b"},
        )
        assert main_live._authenticated_exchange("ftx") is None
