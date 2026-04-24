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

from web.app import _validate_config  # noqa: E402


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
