"""Tests for core/logging_setup.parse_log_level_env.

Pins the REVERTO_LOG_LEVEL env-var contract: case-insensitive
mapping onto logging.{DEBUG,INFO,WARNING,ERROR,CRITICAL}, empty/
unset falls back silently, invalid values fall back with a
stderr warning.

The parsing is extracted from main_paper/main_live precisely so we
can test it without importing those modules (both have import-time
side effects — basicConfig, subprocess spawning, etc.).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logging_setup import parse_log_level_env  # noqa: E402


class TestValidLevels:
    """Every documented level name resolves to its logging.* constant."""

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("DEBUG",    logging.DEBUG),
            ("INFO",     logging.INFO),
            ("WARNING",  logging.WARNING),
            ("ERROR",    logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ],
    )
    def test_named_level_resolves(self, monkeypatch, name, expected):
        monkeypatch.setenv("REVERTO_LOG_LEVEL", name)
        assert parse_log_level_env() == expected

    def test_case_insensitive(self, monkeypatch):
        """Operator typo protection — 'debug' + 'Debug' + 'DEBUG'
        should all work so the env-var isn't a sharp edge."""
        for variant in ("debug", "Debug", "DEBUG", "dEbUg"):
            monkeypatch.setenv("REVERTO_LOG_LEVEL", variant)
            assert parse_log_level_env() == logging.DEBUG

    def test_whitespace_trimmed(self, monkeypatch):
        """Shells that accidentally prepend a space (common copy-paste
        artefact) must still resolve."""
        monkeypatch.setenv("REVERTO_LOG_LEVEL", "  DEBUG  ")
        assert parse_log_level_env() == logging.DEBUG


class TestFallback:
    """Missing / empty / invalid values fall back to the default."""

    def test_unset_env_var_returns_default(self, monkeypatch):
        monkeypatch.delenv("REVERTO_LOG_LEVEL", raising=False)
        assert parse_log_level_env() == logging.INFO

    def test_empty_string_returns_default(self, monkeypatch):
        monkeypatch.setenv("REVERTO_LOG_LEVEL", "")
        assert parse_log_level_env() == logging.INFO

    def test_explicit_default_level_is_respected(self, monkeypatch):
        """Callers can override the fallback, e.g. a tool that defaults
        to WARNING when the env var is unset."""
        monkeypatch.delenv("REVERTO_LOG_LEVEL", raising=False)
        assert parse_log_level_env(default_level=logging.WARNING) == logging.WARNING

    def test_invalid_value_falls_back_to_info(self, monkeypatch):
        """Invalid spellings (NONSENSE, TRACE, 12) must not crash —
        they fall back to INFO so a typo never stops the portal from
        booting."""
        for bad in ("NONSENSE", "TRACE", "12", "!@#$"):
            monkeypatch.setenv("REVERTO_LOG_LEVEL", bad)
            assert parse_log_level_env() == logging.INFO, (
                f"invalid value {bad!r} should fall back to INFO"
            )

    def test_invalid_value_writes_stderr_warning(self, monkeypatch, capsys):
        """Invalid value must be visible — not a silent fallback. The
        warning goes to stderr (not logger.warning, since logging isn't
        configured yet when this helper runs at module-import time)."""
        monkeypatch.setenv("REVERTO_LOG_LEVEL", "NONSENSE")
        parse_log_level_env()
        captured = capsys.readouterr()
        assert "NONSENSE" in captured.err
        assert "Falling back" in captured.err
        # And nothing on stdout — this is an error-path message.
        assert captured.out == ""

    def test_valid_value_writes_no_warning(self, monkeypatch, capsys):
        """Happy path must be silent — no stderr noise on normal
        boot when the operator set a valid level."""
        monkeypatch.setenv("REVERTO_LOG_LEVEL", "DEBUG")
        parse_log_level_env()
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""


class TestCustomEnvName:
    """The env-var name is parameterised so the helper can be reused
    for other tools (e.g. a future ml-pipeline runner) without
    colliding with the portal's REVERTO_LOG_LEVEL."""

    def test_custom_env_name(self, monkeypatch):
        monkeypatch.setenv("MY_TOOL_LOG_LEVEL", "WARNING")
        monkeypatch.delenv("REVERTO_LOG_LEVEL", raising=False)
        assert parse_log_level_env("MY_TOOL_LOG_LEVEL") == logging.WARNING

    def test_custom_env_name_fallback(self, monkeypatch):
        monkeypatch.delenv("MY_TOOL_LOG_LEVEL", raising=False)
        monkeypatch.setenv("REVERTO_LOG_LEVEL", "DEBUG")
        # The custom helper must NOT accidentally read the portal's
        # var — that would be a cross-tool leak.
        assert parse_log_level_env("MY_TOOL_LOG_LEVEL") == logging.INFO


# ── Integration: env-var actually propagates into basicConfig ──────────────


def test_parse_plugs_into_basic_config_cleanly(monkeypatch):
    """Sanity: the helper returns an int that logging.basicConfig
    accepts directly. Catches a regression where someone accidentally
    returns the string name instead of the int."""
    monkeypatch.setenv("REVERTO_LOG_LEVEL", "DEBUG")
    level = parse_log_level_env()
    # Must be a plain int — logging.basicConfig rejects strings on
    # its ``level`` kwarg.
    assert isinstance(level, int)
    assert level == logging.DEBUG
