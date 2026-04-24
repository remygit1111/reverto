"""Regression guard for audit r1-058 — boot-time config validation.

``_validate_config`` runs inside ``lifespan`` startup. Missing
REQUIRED vars raise RuntimeError so uvicorn fails fast; missing
RECOMMENDED vars emit a WARNING log and continue.
"""

from __future__ import annotations

import logging
import os
import sys

os.environ.setdefault("REVERTO_SECRET_KEY", "testkey-for-pytest-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from web.app import _validate_config, _validate_config_completeness  # noqa: E402
from web import app as webapp  # noqa: E402


def test_validate_config_raises_on_missing_secret_key(monkeypatch):
    monkeypatch.delenv("REVERTO_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="REVERTO_SECRET_KEY"):
        _validate_config()


def test_validate_config_warns_on_missing_recommended(monkeypatch, caplog):
    monkeypatch.setenv("REVERTO_SECRET_KEY", "test")
    for var in ("REVERTO_API_KEY", "BITGET_API_KEY", "BITGET_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with caplog.at_level(logging.WARNING, logger="web.app"):
        _validate_config()
    warnings = " ".join(rec.message for rec in caplog.records)
    assert "REVERTO_API_KEY" in warnings
    assert "BITGET_API_KEY" in warnings
    assert "BITGET_API_SECRET" in warnings


def test_validate_config_passes_with_all_set(monkeypatch, caplog):
    monkeypatch.setenv("REVERTO_SECRET_KEY", "x")
    monkeypatch.setenv("REVERTO_API_KEY", "x")
    monkeypatch.setenv("BITGET_API_KEY", "x")
    monkeypatch.setenv("BITGET_API_SECRET", "x")
    with caplog.at_level(logging.WARNING, logger="web.app"):
        _validate_config()  # no raise
    # No "Missing recommended" line should land when everything's set.
    assert not any(
        "Missing recommended" in rec.message for rec in caplog.records
    ), [rec.message for rec in caplog.records]


# ── r1-059: .env.example completeness ──────────────────────────────────────


def test_validate_config_completeness_warns_on_drift(
    tmp_path, monkeypatch, caplog,
):
    # Write a tiny .env.example to an isolated BASE_DIR and point the
    # validator at it. Missing SOMEVAR must surface as a WARNING.
    example = tmp_path / ".env.example"
    example.write_text("SOMEVAR=\n")
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.delenv("SOMEVAR", raising=False)
    monkeypatch.delenv("_VALIDATE_CONFIG_SUPPRESS_EXAMPLE_CHECK", raising=False)

    with caplog.at_level(logging.WARNING, logger="web.app"):
        _validate_config_completeness()

    assert any(
        "SOMEVAR" in rec.message and ".env.example" in rec.message
        for rec in caplog.records
    ), [rec.message for rec in caplog.records]


def test_validate_config_completeness_silent_when_all_present(
    tmp_path, monkeypatch, caplog,
):
    example = tmp_path / ".env.example"
    example.write_text("PRESENTVAR=\n")
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setenv("PRESENTVAR", "1")
    monkeypatch.delenv("_VALIDATE_CONFIG_SUPPRESS_EXAMPLE_CHECK", raising=False)

    with caplog.at_level(logging.WARNING, logger="web.app"):
        _validate_config_completeness()

    assert not any(
        ".env.example" in rec.message for rec in caplog.records
    ), [rec.message for rec in caplog.records]


def test_validate_config_completeness_ignores_comments_and_blank(
    tmp_path, monkeypatch, caplog,
):
    example = tmp_path / ".env.example"
    example.write_text(
        "# Just a comment\n"
        "\n"
        "   # Leading-space comment\n"
        "REALVAR=\n"
    )
    monkeypatch.setattr(webapp, "BASE_DIR", tmp_path)
    monkeypatch.setenv("REALVAR", "1")
    monkeypatch.delenv("_VALIDATE_CONFIG_SUPPRESS_EXAMPLE_CHECK", raising=False)

    with caplog.at_level(logging.WARNING, logger="web.app"):
        _validate_config_completeness()

    # Only REALVAR should be parsed; it's present → no warning.
    assert not any(
        ".env.example" in rec.message for rec in caplog.records
    )
