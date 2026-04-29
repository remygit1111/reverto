"""Regression guards for audit r1-012 — per-user Bitget passphrase.

Before the fix ``BITGET_PASSPHRASE`` was a process-wide env-var, which
meant every tenant's live bot would share one passphrase. The fix
moves the passphrase into the per-user encrypted credentials store
(alongside api_key + api_secret) and introduces
``core.credentials.get_bitget_passphrase`` as the consumer-facing
helper with a legacy env-var fallback + deprecation warning.

Coverage:
  * round-trip: save_keys(..., passphrase=...) → get_keys includes it
  * get_bitget_passphrase preferred source = credentials store
  * get_bitget_passphrase falls back to env with a warning
  * get_bitget_passphrase raises when neither source yields
  * exchange-endpoint accepts + requires passphrase for Bitget
  * exchange-endpoint allows Kraken without passphrase
  * rotate_fernet_key preserves the passphrase field
"""

from __future__ import annotations

import logging
import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import credentials, paths, user_store  # noqa: E402
from web import app as webapp  # noqa: E402


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Redirect the per-user key + .enc tree into tmp_path so tests
    run in a sandbox without touching the repo's real credentials dir.
    Mirrors the ``tmp_store`` fixture in tests/test_credentials.py."""
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "_BASE_DIR", tmp_path)
    return tmp_path


# ── save_keys + get_keys round-trip ───────────────────────────────────────


class TestSaveKeysWithPassphrase:
    def test_round_trip_preserves_passphrase(self, tmp_store):
        credentials.save_keys(
            "bitget", "api-k", "api-s", user_id=1, passphrase="my-pass",
            _skip_format_validation=True,
        )
        got = credentials.get_keys("bitget", user_id=1)
        assert got == {
            "api_key": "api-k",
            "api_secret": "api-s",
            "passphrase": "my-pass",
        }

    def test_save_without_passphrase_returns_empty_field(self, tmp_store):
        # Kraken-style: no passphrase. Storage + readback must still
        # produce the stable three-key shape with passphrase = "".
        credentials.save_keys("kraken", "k-k", "k-s", user_id=1, _skip_format_validation=True)
        got = credentials.get_keys("kraken", user_id=1)
        assert got == {"api_key": "k-k", "api_secret": "k-s", "passphrase": ""}

    def test_passphrase_is_actually_encrypted(self, tmp_store):
        credentials.save_keys(
            "bitget", "ak", "sc", user_id=1,
            passphrase="plaintext-passphrase-must-not-leak",
            _skip_format_validation=True,
        )
        raw = paths.exchange_creds_path(1, "bitget").read_bytes()
        assert b"plaintext-passphrase-must-not-leak" not in raw


# ── get_bitget_passphrase consumer-facing helper ──────────────────────────


class TestGetBitgetPassphrase:
    def test_prefers_credentials_store(self, tmp_store, caplog):
        credentials.save_keys(
            "bitget", "ak", "sc", user_id=1, passphrase="store-value",
            _skip_format_validation=True,
        )
        # Env set to something different — helper must ignore it when
        # the store has a value (no deprecation-warning in this path).
        os.environ["BITGET_PASSPHRASE"] = "env-value"
        try:
            with caplog.at_level(logging.WARNING, logger="core.credentials"):
                got = credentials.get_bitget_passphrase(user_id=1)
            assert got == "store-value"
            assert not any(
                "deprecated" in rec.message.lower()
                for rec in caplog.records
            ), "no warning expected when store wins"
        finally:
            os.environ.pop("BITGET_PASSPHRASE", None)

    def test_falls_back_to_env_with_warning(self, tmp_store, caplog):
        # No credentials stored → falls through to env with
        # a deprecation-warning log (audit r1-012 migration signal).
        os.environ["BITGET_PASSPHRASE"] = "env-fallback"
        try:
            with caplog.at_level(logging.WARNING, logger="core.credentials"):
                got = credentials.get_bitget_passphrase(user_id=1)
            assert got == "env-fallback"
            msgs = " | ".join(rec.message for rec in caplog.records)
            assert "deprecated" in msgs.lower()
            assert "r1-012" in msgs
        finally:
            os.environ.pop("BITGET_PASSPHRASE", None)

    def test_raises_when_no_source(self, tmp_store):
        os.environ.pop("BITGET_PASSPHRASE", None)
        with pytest.raises(ValueError) as excinfo:
            credentials.get_bitget_passphrase(user_id=1)
        # Error message must point the operator at the fix path.
        assert "bitget" in str(excinfo.value).lower()
        assert "/api/exchanges" in str(excinfo.value)

    def test_empty_store_passphrase_falls_back(self, tmp_store):
        # Credentials saved but without a passphrase field → store
        # returns "" → helper treats it as absent and falls through.
        credentials.save_keys("bitget", "ak", "sc", user_id=1, _skip_format_validation=True)
        os.environ["BITGET_PASSPHRASE"] = "env-wins"
        try:
            assert credentials.get_bitget_passphrase(user_id=1) == "env-wins"
        finally:
            os.environ.pop("BITGET_PASSPHRASE", None)


# ── /api/exchanges/{name}/keys endpoint ───────────────────────────────────


_ADMIN_PW = "pytest-bitget-passphrase-pw-123"


@pytest.fixture
def admin_client():
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, _ADMIN_PW)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set(
        "reverto_session", webapp._create_session_cookie(admin),
    )
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


class TestExchangeEndpointAcceptsPassphrase:
    def test_bitget_with_passphrase_saved(self, tmp_store, admin_client):
        # r2-010: realistic-length alphanumerics so the format
        # validator on the public endpoint accepts the body. Pre-
        # r2-010 fixtures used "ak-bitget" / "sc-bitget" which the
        # heuristic now (correctly) rejects as malformed.
        BITGET_KEY = "ak" + "0" * 30  # 32 alphanumeric chars
        BITGET_SECRET = "sc" + "0" * 62  # 64 alphanumeric chars
        r = admin_client.post(
            "/api/exchanges/bitget/keys",
            json={
                "api_key": BITGET_KEY,
                "api_secret": BITGET_SECRET,
                "passphrase": "endpoint-pass",
            },
        )
        assert r.status_code == 200, r.text
        # Read back through the credentials layer — passphrase must
        # survive the encrypted round-trip from request body to .enc.
        admin = user_store.get_user_by_username("admin")
        stored = credentials.get_keys("bitget", user_id=admin.id)
        assert stored == {
            "api_key": BITGET_KEY,
            "api_secret": BITGET_SECRET,
            "passphrase": "endpoint-pass",
        }

    def test_bitget_without_passphrase_returns_400(self, tmp_store, admin_client):
        # Empty request body for passphrase → 400 with a pointer to
        # the audit finding. Prevents a silent credential file that
        # would fail later at exchange init.
        r = admin_client.post(
            "/api/exchanges/bitget/keys",
            json={"api_key": "ak", "api_secret": "sc"},
        )
        assert r.status_code == 400
        assert "passphrase" in r.json()["detail"].lower()

    def test_kraken_without_passphrase_succeeds(self, tmp_store, admin_client):
        # Kraken has no passphrase; endpoint must accept body without
        # the field rather than insist on one.
        # r2-010: realistic-length base64 placeholders so the format
        # validator on the public endpoint accepts the body.
        KRAKEN_KEY = "K" + "x" * 55  # 56 base64 chars
        KRAKEN_SECRET = "S" + "x" * 87  # 88 base64 chars
        r = admin_client.post(
            "/api/exchanges/kraken/keys",
            json={"api_key": KRAKEN_KEY, "api_secret": KRAKEN_SECRET},
        )
        assert r.status_code == 200, r.text
        admin = user_store.get_user_by_username("admin")
        stored = credentials.get_keys("kraken", user_id=admin.id)
        assert stored == {
            "api_key": KRAKEN_KEY,
            "api_secret": KRAKEN_SECRET,
            "passphrase": "",
        }

    def test_passphrase_over_max_length_rejected(self, tmp_store, admin_client):
        # pd-006: Pydantic max_length on passphrase is 64. A 65-char
        # paste must 422, not silently truncate.
        r = admin_client.post(
            "/api/exchanges/bitget/keys",
            json={
                "api_key": "ak",
                "api_secret": "sc",
                "passphrase": "p" * 65,
            },
        )
        assert r.status_code == 422


# ── Rotation preserves passphrase ─────────────────────────────────────────


class TestRotationPreservesPassphrase:
    def test_passphrase_survives_fernet_rotation(self, tmp_store):
        # Save a Bitget credential WITH a passphrase, rotate the
        # user's Fernet key, and verify the passphrase decrypts
        # cleanly under the new key.
        credentials.save_keys(
            "bitget", "ak", "sc", user_id=1, passphrase="rotate-me",
            _skip_format_validation=True,
        )
        credentials.rotate_fernet_key(user_id=1)
        stored = credentials.get_keys("bitget", user_id=1)
        assert stored == {
            "api_key": "ak",
            "api_secret": "sc",
            "passphrase": "rotate-me",
        }
