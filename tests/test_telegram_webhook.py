"""Tests for the POST /api/telegram/webhook/{secret} handler.

The webhook is unique in that its auth surface is the URL-path
secret (Telegram doesn't sign webhook requests). Tests pin every
gate: secret mismatch → 404; unknown token → graceful reply;
valid /start link → telegram_configs upsert + welcome reply.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import telegram_config_store  # noqa: E402
from core.database import get_db  # noqa: E402
from web import app as webapp  # noqa: E402


_SECRET = "abc" * 8 + "ef"


@pytest.fixture
def webhook_env(monkeypatch):
    """Set TELEGRAM_WEBHOOK_SECRET + TELEGRAM_BOT_TOKEN + a captured
    httpx.post stub so the welcome-reply send doesn't hit the network.

    Also resets slowapi so 60/minute doesn't bite under repeated runs.
    """
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    sends: list[dict] = []

    def _fake_post(url, json=None, timeout=None, **_):
        sends.append({"url": url, "json": json})
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "{}"
        return resp

    import notifications.telegram as tg_mod
    monkeypatch.setattr(tg_mod.httpx, "post", _fake_post)
    webapp.limiter.reset()
    yield {"sends": sends}
    webapp.limiter.reset()


@pytest.fixture
def client(webhook_env):
    return TestClient(webapp.app)


def _post(client, secret, body):
    return client.post(f"/api/telegram/webhook/{secret}", json=body)


def _start_message(text: str, chat_id: int = 999) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Remy"},
            "text": text,
        },
    }


# ── Secret gate ───────────────────────────────────────────────────────────


class TestSecretGate:

    def test_wrong_secret_is_404(self, client):
        r = _post(client, "wrong-secret", _start_message("/start"))
        assert r.status_code == 404

    def test_missing_secret_env_blocks_all(self, monkeypatch, client):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
        r = _post(client, _SECRET, _start_message("/start"))
        assert r.status_code == 404

    def test_route_without_secret_path_blocks(self, client):
        # The endpoint requires the secret in the path — there is
        # no handler at the unparameterised URL. The auth middleware
        # responds with 401 to any non-public POST without a session;
        # FastAPI's router would 404 if the middleware were past.
        # Either way it's "unreachable from the open internet".
        r = client.post("/api/telegram/webhook", json=_start_message("/start"))
        assert r.status_code in (401, 404, 405)


# ── /start payloads ───────────────────────────────────────────────────────


class TestStartFlow:

    def test_valid_link_token_consumes_and_replies(
        self, client, webhook_env,
    ):
        token = telegram_config_store.create_link_token(1)
        r = _post(client, _SECRET, _start_message(f"/start link_{token}"))
        assert r.status_code == 200
        # telegram_configs upsert landed.
        cfg = telegram_config_store.get_config(1)
        assert cfg is not None
        assert cfg["chat_id"] == "999"
        # Welcome reply was sent — message body mentions "Connected".
        assert webhook_env["sends"], "expected welcome reply"
        body_text = webhook_env["sends"][-1]["json"]["text"]
        assert "Connected" in body_text

    def test_unknown_token_replies_with_help(self, client, webhook_env):
        r = _post(
            client, _SECRET,
            _start_message("/start link_deadbeef00000000000000000000beef"),
        )
        assert r.status_code == 200
        body_text = webhook_env["sends"][-1]["json"]["text"]
        assert "expired" in body_text.lower() or "used" in body_text.lower()

    def test_already_used_token_replies_with_help(
        self, client, webhook_env,
    ):
        token = telegram_config_store.create_link_token(1)
        telegram_config_store.consume_link_token(token, "888")
        r = _post(client, _SECRET, _start_message(f"/start link_{token}"))
        assert r.status_code == 200
        body_text = webhook_env["sends"][-1]["json"]["text"]
        assert "expired" in body_text.lower() or "used" in body_text.lower()

    def test_expired_token_replies_with_help(self, client, webhook_env):
        token = telegram_config_store.create_link_token(1)
        past = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        conn = get_db()
        with conn:
            conn.execute(
                "UPDATE telegram_link_tokens SET expires_at = ? "
                "WHERE token = ?", (past, token),
            )
        r = _post(client, _SECRET, _start_message(f"/start link_{token}"))
        assert r.status_code == 200
        # No config row was upserted.
        assert telegram_config_store.get_config(1) is None

    def test_start_without_payload_replies_with_help(
        self, client, webhook_env,
    ):
        r = _post(client, _SECRET, _start_message("/start"))
        assert r.status_code == 200
        body_text = webhook_env["sends"][-1]["json"]["text"]
        assert "Welcome" in body_text or "Reverto" in body_text

    def test_unrelated_message_replies_with_help(
        self, client, webhook_env,
    ):
        r = _post(client, _SECRET, _start_message("hello there"))
        assert r.status_code == 200
        body_text = webhook_env["sends"][-1]["json"]["text"]
        assert "reverto" in body_text.lower()

    def test_start_with_non_link_payload_replies_with_help(
        self, client, webhook_env,
    ):
        r = _post(client, _SECRET, _start_message("/start junk"))
        assert r.status_code == 200
        body_text = webhook_env["sends"][-1]["json"]["text"]
        assert "reverto" in body_text.lower() or "portal" in body_text.lower()

    def test_no_message_field_acknowledges_silently(
        self, client, webhook_env,
    ):
        # Telegram sends edits / callbacks too — we acknowledge with
        # 200 but don't reply.
        r = client.post(
            f"/api/telegram/webhook/{_SECRET}",
            json={"update_id": 42},
        )
        assert r.status_code == 200
        assert webhook_env["sends"] == []


# ── Cross-user isolation ──────────────────────────────────────────────────


class TestIsolation:

    def test_user_b_token_does_not_link_user_a(self, client):
        # Seed user 2 + their token.
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO users (id, username, role) "
                "VALUES (2, 'bob_wh', 'user')",
            )
        token = telegram_config_store.create_link_token(2)
        r = _post(client, _SECRET, _start_message(f"/start link_{token}"))
        assert r.status_code == 200
        # Only user 2 has a config; user 1 is untouched.
        assert telegram_config_store.get_config(1) is None
        assert telegram_config_store.get_config(2) is not None
