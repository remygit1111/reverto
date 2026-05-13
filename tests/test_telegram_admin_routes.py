"""Tests for the /api/telegram/* admin route surface.

Covers auth gates, happy-path responses, cross-user isolation,
and the rate-limit + 404 behaviour around the 4 endpoints.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import database, telegram_config_store, user_store  # noqa: E402
from web import app as webapp  # noqa: E402


@pytest.fixture
def fs_sandbox(monkeypatch):
    """Reset the per-route rate limiter so each test starts with a
    clean slowapi bucket. Also stubs httpx.post in
    notifications.telegram so a test-message never hits the network.
    """
    webapp.limiter.reset()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "RevertoAlertsBot")
    import notifications.telegram as tg_mod
    sends: list[dict] = []

    def _fake_post(url, json=None, timeout=None, **_):
        sends.append({"url": url, "json": json})
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "{}"
        return resp

    monkeypatch.setattr(tg_mod.httpx, "post", _fake_post)
    try:
        yield {"sends": sends}
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
    client._teardown = (prev_secure, prev_samesite)
    return client


def _teardown_client(client: TestClient) -> None:
    prev_secure, prev_samesite = client._teardown
    webapp._COOKIE_SECURE = prev_secure
    webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def admin_client(fs_sandbox):
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, "pytest-tg-pw-12345")
    client = _make_session_client(admin)
    try:
        yield client
    finally:
        _teardown_client(client)


@pytest.fixture
def bob_client(fs_sandbox):
    conn = database.get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (username, role) VALUES ('bob_tg_adm', 'user')",
        )
    bob = user_store.get_user_by_username("bob_tg_adm")
    client = _make_session_client(bob)
    try:
        yield client
    finally:
        _teardown_client(client)


# ── Auth gate ────────────────────────────────────────────────────────────


class TestAuthGate:

    def test_config_get_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        assert client.get("/api/telegram/config").status_code == 401

    def test_link_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        assert client.post("/api/telegram/link").status_code == 401

    def test_notify_on_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        r = client.patch(
            "/api/telegram/notify-on", json={"events": []},
        )
        assert r.status_code == 401

    def test_test_message_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        assert client.post(
            "/api/telegram/test-message",
        ).status_code == 401

    def test_disconnect_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        assert client.delete("/api/telegram/config").status_code == 401


# ── GET /config ──────────────────────────────────────────────────────────


class TestConfigGet:

    def test_disconnected_envelope(self, admin_client):
        body = admin_client.get("/api/telegram/config").json()
        assert body["connected"] is False
        assert body["chat_id_masked"] is None
        assert body["notify_on"] == []

    def test_connected_envelope_masks_chat_id(self, admin_client):
        admin = user_store.get_user_by_username("admin")
        token = telegram_config_store.create_link_token(admin.id)
        telegram_config_store.consume_link_token(token, "987654321")
        body = admin_client.get("/api/telegram/config").json()
        assert body["connected"] is True
        # Last 4 chars only.
        assert body["chat_id_masked"] == "***4321"
        assert "987654321" not in body["chat_id_masked"]


# ── POST /link ────────────────────────────────────────────────────────────


class TestLinkCreate:

    def test_returns_token_and_url(self, admin_client):
        r = admin_client.post("/api/telegram/link")
        assert r.status_code == 200
        body = r.json()
        assert len(body["token"]) == 32
        assert body["telegram_url"].startswith(
            "https://t.me/RevertoAlertsBot?start=link_",
        )
        assert body["expires_at"] is not None

    def test_500_when_bot_username_unset(self, admin_client, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
        r = admin_client.post("/api/telegram/link")
        assert r.status_code == 500

    def test_creating_drops_prior_unused_token(self, admin_client):
        first = admin_client.post("/api/telegram/link").json()["token"]
        second = admin_client.post("/api/telegram/link").json()["token"]
        assert first != second


# ── PATCH /notify-on ──────────────────────────────────────────────────────


class TestNotifyOnUpdate:

    def test_404_when_not_connected(self, admin_client):
        r = admin_client.patch(
            "/api/telegram/notify-on", json={"events": ["entry"]},
        )
        assert r.status_code == 404

    def test_updates_existing(self, admin_client):
        admin = user_store.get_user_by_username("admin")
        t = telegram_config_store.create_link_token(admin.id)
        telegram_config_store.consume_link_token(t, "999")
        r = admin_client.patch(
            "/api/telegram/notify-on",
            json={"events": ["entry", "tp_hit"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert sorted(body["notify_on"]) == ["entry", "tp_hit"]

    def test_400_on_bogus_event(self, admin_client):
        admin = user_store.get_user_by_username("admin")
        t = telegram_config_store.create_link_token(admin.id)
        telegram_config_store.consume_link_token(t, "999")
        r = admin_client.patch(
            "/api/telegram/notify-on",
            json={"events": ["entry", "definitely_not_a_real_event"]},
        )
        assert r.status_code == 400


# ── POST /test-message ────────────────────────────────────────────────────


class TestTestMessage:

    def test_404_when_not_connected(self, admin_client):
        r = admin_client.post("/api/telegram/test-message")
        assert r.status_code == 404

    def test_happy_path_sends(self, admin_client, fs_sandbox):
        admin = user_store.get_user_by_username("admin")
        t = telegram_config_store.create_link_token(admin.id)
        telegram_config_store.consume_link_token(t, "999")
        r = admin_client.post("/api/telegram/test-message")
        assert r.status_code == 200
        assert fs_sandbox["sends"], "expected an httpx send"
        body_text = fs_sandbox["sends"][-1]["json"]["text"]
        assert "Reverto Alerts test" in body_text


# ── DELETE /config ────────────────────────────────────────────────────────


class TestDisconnect:

    def test_404_when_not_connected(self, admin_client):
        r = admin_client.delete("/api/telegram/config")
        assert r.status_code == 404

    def test_happy_path_removes_row(self, admin_client):
        admin = user_store.get_user_by_username("admin")
        t = telegram_config_store.create_link_token(admin.id)
        telegram_config_store.consume_link_token(t, "999")
        r = admin_client.delete("/api/telegram/config")
        assert r.status_code == 200
        assert telegram_config_store.get_config(admin.id) is None


# ── Cross-user isolation ──────────────────────────────────────────────────


class TestIsolation:

    def test_admin_cannot_see_bobs_config(
        self, admin_client, bob_client,
    ):
        bob = user_store.get_user_by_username("bob_tg_adm")
        t = telegram_config_store.create_link_token(bob.id)
        telegram_config_store.consume_link_token(t, "bob-chat")
        # Bob sees connected.
        body = bob_client.get("/api/telegram/config").json()
        assert body["connected"] is True
        # Admin still sees disconnected.
        body_admin = admin_client.get("/api/telegram/config").json()
        assert body_admin["connected"] is False

    def test_admin_disconnect_does_not_touch_bob(
        self, admin_client, bob_client,
    ):
        admin = user_store.get_user_by_username("admin")
        bob = user_store.get_user_by_username("bob_tg_adm")
        t_admin = telegram_config_store.create_link_token(admin.id)
        telegram_config_store.consume_link_token(t_admin, "admin-chat")
        t_bob = telegram_config_store.create_link_token(bob.id)
        telegram_config_store.consume_link_token(t_bob, "bob-chat")
        admin_client.delete("/api/telegram/config")
        # Bob's row survives.
        assert telegram_config_store.get_config(bob.id) is not None
