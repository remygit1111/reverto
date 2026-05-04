"""Tests for bot subprocess log rotation — PT-v4-FS-008.

The bot subprocess (main_paper.py / main_live.py) used to write its
log unbounded — a long-running bot would slowly fill the VPS disk.
Post-fix, ``configure_bot_file_logging`` installs a
``RotatingFileHandler`` on the root logger with a 10 MiB cap and 3
backups, both env-overridable.

These tests pin:
  * The handler installed is a RotatingFileHandler with the
    expected maxBytes / backupCount.
  * Env overrides (``REVERTO_BOT_LOG_MAX_BYTES`` /
    ``REVERTO_BOT_LOG_BACKUP_COUNT``) flow through.
  * Malformed / non-positive overrides fall back to defaults.
  * Default values are 10 MiB and 3.
  * Calling configure_bot_file_logging twice replaces the handler
    (idempotent for restart-in-test scenarios).
  * Real rotation: writing over the cap creates a backup file.
  * Existing root-logger handlers are removed (to avoid double-
    writing through Popen's stdout redirect).
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logging_setup import (  # noqa: E402
    _DEFAULT_BOT_LOG_BACKUP_COUNT,
    _DEFAULT_BOT_LOG_MAX_BYTES,
    _resolve_bot_log_backup_count,
    _resolve_bot_log_max_bytes,
    configure_bot_file_logging,
)


@pytest.fixture
def _restore_root_logger():
    """Snapshot + restore the root logger's handlers so a test that
    calls ``configure_bot_file_logging`` doesn't leak handlers into
    sibling tests."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


# ── Resolver helpers ───────────────────────────────────────────────────────


class TestBotLogResolvers:
    """Direct unit tests for the env-var resolvers."""

    def test_max_bytes_default(self, monkeypatch):
        monkeypatch.delenv("REVERTO_BOT_LOG_MAX_BYTES", raising=False)
        assert _resolve_bot_log_max_bytes() == _DEFAULT_BOT_LOG_MAX_BYTES
        # Pin the actual default so a future bump shows up in review.
        assert _DEFAULT_BOT_LOG_MAX_BYTES == 10 * 1024 * 1024

    def test_max_bytes_override(self, monkeypatch):
        monkeypatch.setenv("REVERTO_BOT_LOG_MAX_BYTES", "1048576")
        assert _resolve_bot_log_max_bytes() == 1048576

    def test_max_bytes_malformed_falls_back(self, monkeypatch):
        monkeypatch.setenv("REVERTO_BOT_LOG_MAX_BYTES", "ten")
        assert _resolve_bot_log_max_bytes() == _DEFAULT_BOT_LOG_MAX_BYTES

    def test_max_bytes_non_positive_falls_back(self, monkeypatch):
        monkeypatch.setenv("REVERTO_BOT_LOG_MAX_BYTES", "0")
        assert _resolve_bot_log_max_bytes() == _DEFAULT_BOT_LOG_MAX_BYTES
        monkeypatch.setenv("REVERTO_BOT_LOG_MAX_BYTES", "-100")
        assert _resolve_bot_log_max_bytes() == _DEFAULT_BOT_LOG_MAX_BYTES

    def test_backup_count_default(self, monkeypatch):
        monkeypatch.delenv("REVERTO_BOT_LOG_BACKUP_COUNT", raising=False)
        assert (
            _resolve_bot_log_backup_count() == _DEFAULT_BOT_LOG_BACKUP_COUNT
        )
        assert _DEFAULT_BOT_LOG_BACKUP_COUNT == 3

    def test_backup_count_zero_is_valid(self, monkeypatch):
        """``backupCount=0`` disables rotation while keeping the
        cap-and-truncate behaviour. Honour the operator's choice."""
        monkeypatch.setenv("REVERTO_BOT_LOG_BACKUP_COUNT", "0")
        assert _resolve_bot_log_backup_count() == 0

    def test_backup_count_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("REVERTO_BOT_LOG_BACKUP_COUNT", "-1")
        assert (
            _resolve_bot_log_backup_count() == _DEFAULT_BOT_LOG_BACKUP_COUNT
        )

    def test_backup_count_malformed_falls_back(self, monkeypatch):
        monkeypatch.setenv("REVERTO_BOT_LOG_BACKUP_COUNT", "three")
        assert (
            _resolve_bot_log_backup_count() == _DEFAULT_BOT_LOG_BACKUP_COUNT
        )


# ── Handler installation ───────────────────────────────────────────────────


class TestConfigureBotFileLogging:
    """The setup helper installs exactly one ``RotatingFileHandler``
    with the resolved knobs and removes pre-existing handlers."""

    def test_installs_rotating_handler(
        self, tmp_path, _restore_root_logger,
    ):
        log_path = tmp_path / "test_bot.log"
        handler = configure_bot_file_logging(log_path)
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == _DEFAULT_BOT_LOG_MAX_BYTES
        assert handler.backupCount == _DEFAULT_BOT_LOG_BACKUP_COUNT
        # Root logger has exactly the one handler we just installed.
        root = logging.getLogger()
        assert root.handlers == [handler]

    def test_existing_handlers_removed(
        self, tmp_path, _restore_root_logger,
    ):
        """basicConfig at module load installs a StreamHandler. We
        must drop it so log lines don't double-write to the same path
        via Popen's stdout redirect."""
        root = logging.getLogger()
        sentinel = logging.StreamHandler()
        root.addHandler(sentinel)
        log_path = tmp_path / "test_bot.log"
        configure_bot_file_logging(log_path)
        # Sentinel is gone; root carries only the new handler.
        assert sentinel not in root.handlers
        assert len(root.handlers) == 1

    def test_env_override_propagates(
        self, tmp_path, monkeypatch, _restore_root_logger,
    ):
        monkeypatch.setenv("REVERTO_BOT_LOG_MAX_BYTES", "2048")
        monkeypatch.setenv("REVERTO_BOT_LOG_BACKUP_COUNT", "5")
        handler = configure_bot_file_logging(tmp_path / "ovr.log")
        assert handler.maxBytes == 2048
        assert handler.backupCount == 5

    def test_idempotent_on_second_call(
        self, tmp_path, _restore_root_logger,
    ):
        """Calling twice replaces the handler — the test exists so a
        future operator-driven rotation reload doesn't accumulate
        handlers."""
        first = configure_bot_file_logging(tmp_path / "a.log")
        second = configure_bot_file_logging(tmp_path / "b.log")
        root = logging.getLogger()
        assert root.handlers == [second]
        assert first not in root.handlers

    def test_creates_parent_directory(
        self, tmp_path, _restore_root_logger,
    ):
        """Bot log paths are nested under ``logs/<uid>/`` — the helper
        must mkdir -p the parent so a fresh tenant directory works
        without manual setup."""
        nested = tmp_path / "logs" / "42" / "bot.log"
        configure_bot_file_logging(nested)
        assert nested.parent.is_dir()


# ── Real rotation behaviour ────────────────────────────────────────────────


class TestRotationActuallyHappens:
    """Integration-style: write enough log bytes to cross the cap and
    verify a backup file appears. Uses a tiny cap (1 KiB) so the test
    completes in milliseconds."""

    def test_rotation_creates_backup_file(
        self, tmp_path, monkeypatch, _restore_root_logger,
    ):
        monkeypatch.setenv("REVERTO_BOT_LOG_MAX_BYTES", "1024")
        monkeypatch.setenv("REVERTO_BOT_LOG_BACKUP_COUNT", "3")
        log_path = tmp_path / "rotate.log"
        configure_bot_file_logging(log_path, level=logging.INFO)

        bot_logger = logging.getLogger("rotate_test")
        # Write ~5 KiB worth of log lines — enough to cross 1 KiB
        # multiple times.
        for i in range(40):
            bot_logger.info("x" * 200 + f" line {i}")

        # Active log exists.
        assert log_path.exists()
        # At least one backup file landed.
        backups = sorted(tmp_path.glob("rotate.log.*"))
        assert backups, (
            f"no rotated backup file found; tmp_path contents: "
            f"{list(tmp_path.iterdir())}"
        )
        # No more than backupCount backup files.
        assert len(backups) <= 3

    def test_active_log_capped_after_rotation(
        self, tmp_path, monkeypatch, _restore_root_logger,
    ):
        """The active ``.log`` file shouldn't grow past maxBytes by
        more than one full record (the line that triggered rotation
        but couldn't be split)."""
        cap = 2048
        monkeypatch.setenv("REVERTO_BOT_LOG_MAX_BYTES", str(cap))
        monkeypatch.setenv("REVERTO_BOT_LOG_BACKUP_COUNT", "2")
        log_path = tmp_path / "rotate2.log"
        configure_bot_file_logging(log_path, level=logging.INFO)

        bot_logger = logging.getLogger("rotate2_test")
        for i in range(50):
            bot_logger.info("y" * 300 + f" line {i}")

        size = log_path.stat().st_size
        # Allow up to one extra full log record above cap (the
        # not-yet-rotated line). One record is ~350 bytes; cap is
        # 2048; budget cap + 500 for safety.
        assert size <= cap + 500, (
            f"active log {log_path} grew to {size}B, "
            f"exceeds cap {cap} by more than one record"
        )
