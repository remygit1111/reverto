"""Tests for the /api/exchange-accounts route surface.

Covers happy-path CRUD, auth + cross-user isolation, and the
test-connection round-trip. The exchange client itself is monkey-
patched so no real ccxt calls go out.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import (  # noqa: E402
    credentials, database, exchange_account_store, paths, user_store,
)
from web import app as webapp  # noqa: E402


_REALISTIC_BITGET_KEY = "ak" + "0" * 30
_REALISTIC_BITGET_SEC = "sc" + "0" * 62
_REALISTIC_KRAKEN_KEY = "K" + "x" * 55
_REALISTIC_KRAKEN_SEC = "S" + "x" * 87


@pytest.fixture
def fs_sandbox(tmp_path, monkeypatch):
    """Redirect credentials/keys to tmp_path so the real repo tree
    stays untouched. Reset the per-user rate limiter at fixture entry
    AND exit so tests that POST repeatedly don't trip the 10/minute
    cap and so a prior test's bucket doesn't leak into this one."""
    webapp.limiter.reset()
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    monkeypatch.setattr(credentials, "_BASE_DIR", tmp_path)
    try:
        yield tmp_path
    finally:
        webapp.limiter.reset()


def _make_session_client(user) -> TestClient:
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set(
        "reverto_session", webapp._create_session_cookie(user),
    )
    client._teardown = (prev_secure, prev_samesite)  # type: ignore[attr-defined]
    return client


def _teardown_client(client: TestClient) -> None:
    prev_secure, prev_samesite = client._teardown  # type: ignore[attr-defined]
    webapp._COOKIE_SECURE = prev_secure
    webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def admin_client(fs_sandbox):
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, "pytest-ex-routes-admin-pw-12345")
    client = _make_session_client(admin)
    try:
        yield client
    finally:
        _teardown_client(client)


@pytest.fixture
def bob_client(fs_sandbox):
    """A second authenticated user, used as the would-be attacker in
    cross-user isolation tests."""
    conn = database.get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (username, role) "
            "VALUES ('bob_ex', 'user')",
        )
    bob = user_store.get_user_by_username("bob_ex")
    client = _make_session_client(bob)
    try:
        yield client
    finally:
        _teardown_client(client)


def _create_via_api(client, exchange_type, alias, **extra):
    body = {
        "exchange_type": exchange_type,
        "alias": alias,
        "api_key": _REALISTIC_BITGET_KEY if exchange_type == "bitget"
                   else _REALISTIC_KRAKEN_KEY,
        "api_secret": _REALISTIC_BITGET_SEC if exchange_type == "bitget"
                      else _REALISTIC_KRAKEN_SEC,
    }
    if exchange_type == "bitget":
        body["passphrase"] = "my-pass"
    body.update(extra)
    return client.post("/api/exchange-accounts", json=body)


# ── Unauthenticated ────────────────────────────────────────────────────────


class TestAuthGate:

    def test_anonymous_list_blocked(self, fs_sandbox):
        client = TestClient(webapp.app)
        r = client.get("/api/exchange-accounts")
        assert r.status_code == 401

    def test_anonymous_create_blocked(self, fs_sandbox):
        client = TestClient(webapp.app)
        r = client.post("/api/exchange-accounts", json={})
        assert r.status_code in (401, 403)


# ── Supported list ────────────────────────────────────────────────────────


class TestSupportedExchanges:

    def test_supported_list(self, admin_client):
        r = admin_client.get("/api/exchanges/supported")
        assert r.status_code == 200
        body = r.json()
        assert "bitget" in body["exchanges"]
        assert "kraken" in body["exchanges"]


# ── Create + validation ───────────────────────────────────────────────────


class TestCreate:

    def test_bitget_with_passphrase(self, admin_client):
        r = _create_via_api(admin_client, "bitget", "main")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["account"]["alias"] == "main"
        assert body["account"]["exchange_type"] == "bitget"

    def test_kraken_without_passphrase(self, admin_client):
        r = _create_via_api(admin_client, "kraken", "main")
        assert r.status_code == 200, r.text

    def test_bitget_without_passphrase_400(self, admin_client):
        r = admin_client.post("/api/exchange-accounts", json={
            "exchange_type": "bitget",
            "alias": "main",
            "api_key": _REALISTIC_BITGET_KEY,
            "api_secret": _REALISTIC_BITGET_SEC,
        })
        assert r.status_code == 400
        assert "passphrase" in r.json()["detail"].lower()

    def test_unknown_exchange_type_400(self, admin_client):
        r = admin_client.post("/api/exchange-accounts", json={
            "exchange_type": "ftx",
            "alias": "main",
            "api_key": "a", "api_secret": "b",
        })
        assert r.status_code == 400

    def test_duplicate_alias_400(self, admin_client):
        _create_via_api(admin_client, "bitget", "main")
        r = _create_via_api(admin_client, "bitget", "main")
        assert r.status_code == 400


# ── List + Get ─────────────────────────────────────────────────────────────


class TestListGet:

    def test_list_returns_only_my_accounts(
        self, admin_client, bob_client,
    ):
        _create_via_api(admin_client, "bitget", "admin-main")
        _create_via_api(bob_client, "kraken", "bob-main")
        admin_resp = admin_client.get("/api/exchange-accounts").json()
        bob_resp = bob_client.get("/api/exchange-accounts").json()
        assert [a["alias"] for a in admin_resp["accounts"]] == ["admin-main"]
        assert [a["alias"] for a in bob_resp["accounts"]] == ["bob-main"]

    def test_get_foreign_account_404(self, admin_client, bob_client):
        # Bob creates one, admin tries to read its id.
        r = _create_via_api(bob_client, "kraken", "secret-bob")
        bob_account_id = r.json()["account"]["id"]
        r = admin_client.get(f"/api/exchange-accounts/{bob_account_id}")
        assert r.status_code == 404

    def test_credentials_never_in_response(self, admin_client):
        r = _create_via_api(admin_client, "bitget", "main")
        body = r.json()
        flat = repr(body)
        assert _REALISTIC_BITGET_KEY not in flat
        assert _REALISTIC_BITGET_SEC not in flat
        assert "my-pass" not in flat

        r = admin_client.get("/api/exchange-accounts").json()
        flat = repr(r)
        assert _REALISTIC_BITGET_KEY not in flat
        assert _REALISTIC_BITGET_SEC not in flat


# ── Patch ─────────────────────────────────────────────────────────────────


class TestPatch:

    def test_rename_alias(self, admin_client):
        r = _create_via_api(admin_client, "bitget", "main")
        aid = r.json()["account"]["id"]
        r = admin_client.patch(
            f"/api/exchange-accounts/{aid}", json={"alias": "renamed"},
        )
        assert r.status_code == 200
        assert r.json()["alias"] == "renamed"

    def test_set_default_flag(self, admin_client):
        r1 = _create_via_api(admin_client, "bitget", "a")
        r2 = _create_via_api(admin_client, "bitget", "b")
        id_a = r1.json()["account"]["id"]
        id_b = r2.json()["account"]["id"]
        admin_client.patch(
            f"/api/exchange-accounts/{id_a}", json={"is_default": True},
        )
        admin_client.patch(
            f"/api/exchange-accounts/{id_b}", json={"is_default": True},
        )
        # b should be default; a should have been auto-unset.
        body_a = admin_client.get(f"/api/exchange-accounts/{id_a}").json()
        body_b = admin_client.get(f"/api/exchange-accounts/{id_b}").json()
        assert body_a["is_default"] is False
        assert body_b["is_default"] is True

    def test_patch_foreign_404(self, admin_client, bob_client):
        r = _create_via_api(bob_client, "kraken", "secret-bob")
        bob_account_id = r.json()["account"]["id"]
        r = admin_client.patch(
            f"/api/exchange-accounts/{bob_account_id}",
            json={"alias": "stolen"},
        )
        assert r.status_code == 404


# ── Delete + blocking-bot 409 ─────────────────────────────────────────────


class TestDelete:

    def test_delete_account(self, admin_client):
        r = _create_via_api(admin_client, "bitget", "main")
        aid = r.json()["account"]["id"]
        r = admin_client.delete(f"/api/exchange-accounts/{aid}")
        assert r.status_code == 200
        # Subsequent reads 404.
        r = admin_client.get(f"/api/exchange-accounts/{aid}")
        assert r.status_code == 404

    def test_delete_with_blocking_bot_409(self, admin_client, tmp_path):
        r = _create_via_api(admin_client, "bitget", "main")
        aid = r.json()["account"]["id"]
        # Write a fake bot YAML that references the account_id.
        admin = user_store.get_user_by_username("admin")
        user_dir = paths.user_bots_dir(admin.id)
        (user_dir / "blocker.yaml").write_text(
            f"bot:\n  name: blocker\n  exchange_account_id: {aid}\n",
            encoding="utf-8",
        )
        try:
            r = admin_client.delete(f"/api/exchange-accounts/{aid}")
            assert r.status_code == 409
            detail = r.json()["detail"]
            assert "blocker" in detail["blocking_bots"]
        finally:
            (user_dir / "blocker.yaml").unlink()

    def test_delete_foreign_404(self, admin_client, bob_client):
        r = _create_via_api(bob_client, "kraken", "main")
        bob_id = r.json()["account"]["id"]
        r = admin_client.delete(f"/api/exchange-accounts/{bob_id}")
        assert r.status_code == 404


# ── Test-connection round-trip ────────────────────────────────────────────


class TestTestConnection:

    def test_success_updates_last_tested_at(self, admin_client, monkeypatch):
        # Stub the BitgetExchange constructor so no ccxt traffic
        # leaves the test runner.
        fake_client = MagicMock()
        fake_client.get_balance.return_value = 0.42

        def _fake_bitget(*args, **kwargs):
            return fake_client
        monkeypatch.setattr(
            "exchanges.bitget.BitgetExchange", _fake_bitget,
        )

        r = _create_via_api(admin_client, "bitget", "main")
        aid = r.json()["account"]["id"]
        # last_tested_at should still be null before the test runs.
        before = admin_client.get(f"/api/exchange-accounts/{aid}").json()
        assert before["last_tested_at"] is None

        r = admin_client.post(
            f"/api/exchange-accounts/{aid}/test-connection",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["balance_btc"] == 0.42

        after = admin_client.get(f"/api/exchange-accounts/{aid}").json()
        assert after["last_tested_at"] is not None

    def test_failure_returns_ok_false(self, admin_client, monkeypatch):
        def _broken_bitget(*args, **kwargs):
            raise RuntimeError("auth failed — bad signature\nlong details here")
        monkeypatch.setattr(
            "exchanges.bitget.BitgetExchange", _broken_bitget,
        )

        r = _create_via_api(admin_client, "bitget", "main")
        aid = r.json()["account"]["id"]
        r = admin_client.post(
            f"/api/exchange-accounts/{aid}/test-connection",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        # Sanitised: no newlines past the first line.
        assert "\n" not in body["error"]

    def test_test_foreign_404(self, admin_client, bob_client):
        r = _create_via_api(bob_client, "bitget", "main")
        bob_id = r.json()["account"]["id"]
        r = admin_client.post(
            f"/api/exchange-accounts/{bob_id}/test-connection",
        )
        assert r.status_code == 404
