"""Tests for main_live.py — slug validation, DRY_RUN env parsing,
and the authenticated-exchange helper.

Subprocess + stdin integration tests are out of scope for the unit
suite; instead we import the module directly and exercise the helper
functions + regex.
"""

import sys

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

import main_live  # noqa: E402


class TestBotSlugRegex:
    """The path-traversal fix relies on a strict regex applied BEFORE
    Path() construction."""

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
    """DRY_RUN env-var is case-insensitive across several common
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
    """Live boot must refuse to start when the exchange account has
    no decryptable credentials or when the exchange-type is unknown."""

    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "core.exchange_account_store.get_account_credentials",
            lambda _aid: None,
        )
        assert main_live._authenticated_exchange(
            "bitget", "coin_m", exchange_account_id=42, user_id=1,
        ) is None

    def test_bitget_requires_passphrase(self, monkeypatch):
        """A Bitget account with creds but no passphrase must refuse to
        boot rather than build an unauthenticated client downstream."""
        monkeypatch.setattr(
            "core.exchange_account_store.get_account_credentials",
            lambda _aid: {
                "api_key": "a", "api_secret": "b", "passphrase": "",
            },
        )
        assert main_live._authenticated_exchange(
            "bitget", "coin_m", exchange_account_id=42, user_id=1,
        ) is None

    def test_unknown_exchange_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "core.exchange_account_store.get_account_credentials",
            lambda _aid: {
                "api_key": "a", "api_secret": "b", "passphrase": "",
            },
        )
        assert main_live._authenticated_exchange(
            "ftx", "spot", exchange_account_id=42, user_id=1,
        ) is None
