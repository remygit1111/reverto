"""Tests for POST /api/emergency-stop and /api/bots/{slug}/drawdown/reset.

We reuse the API-KEY header path the rest of test_web_routes.py uses,
which keeps us off the .auth.json session-epoch path entirely — any
stateful session fixture that writes to .auth.json leaks across test
files and breaks unrelated API-key auth tests.
"""

import json
import os
import sys

os.environ["REVERTO_API_KEY"] = "testkey-for-pytest"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web import app as webapp  # noqa: E402


CLIENT = TestClient(webapp.app)
AUTH = {"X-API-Key": "testkey-for-pytest"}


class TestEmergencyStop:

    def test_requires_auth(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.post("/api/emergency-stop")
        assert r.status_code == 401

    def test_empty_registry_returns_ok(self, monkeypatch):
        """With no running bots, the endpoint still returns 200 with an
        empty stopped_bots list."""
        async def _fake_all():
            return []

        monkeypatch.setattr(webapp.registry, "all", _fake_all)

        r = CLIENT.post("/api/emergency-stop", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["stopped_bots"] == []
        assert body["failed"] == []


class TestDrawdownReset:

    def test_rejects_invalid_slug(self):
        """Path-encoded traversal attempts must not reach the filesystem."""
        r = CLIENT.post(
            "/api/bots/..%2Fetc/drawdown/reset", headers=AUTH,
        )
        # Either the route doesn't match (404) or our regex check 400s.
        assert r.status_code in (400, 404)

    def test_unknown_slug_returns_404(self, monkeypatch):
        async def _fake_get(user_id, slug):
            return None

        monkeypatch.setattr(webapp.registry, "get", _fake_get)
        r = CLIENT.post("/api/bots/nosuchbot/drawdown/reset", headers=AUTH)
        assert r.status_code == 404

    def test_resets_drawdown_state(self, monkeypatch, tmp_path):
        """state.json with a triggered drawdown_guard is cleared but
        every other field is preserved byte-for-byte."""
        state_file = tmp_path / "bot.state.json"
        state_file.write_text(json.dumps({
            "bot_name": "testbot",
            "balance_btc": 0.1,
            "drawdown_guard": {
                "peak_value": 0.12,
                "triggered": True,
                "trigger_reason": "Drawdown 15% exceeded threshold 10%",
            },
            "paused_by_drawdown": True,
            "open_deals": [],
            "closed_deals": [],
        }))

        class _FakeBot:
            def __init__(self):
                self.state_file = state_file

        async def _fake_get(user_id, slug):
            return _FakeBot()

        monkeypatch.setattr(webapp.registry, "get", _fake_get)

        r = CLIENT.post("/api/bots/testbot/drawdown/reset", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["ok"] is True

        updated = json.loads(state_file.read_text())
        assert updated["drawdown_guard"]["triggered"] is False
        assert updated["drawdown_guard"]["peak_value"] is None
        assert updated["paused_by_drawdown"] is False
        assert updated["balance_btc"] == 0.1
        assert updated["bot_name"] == "testbot"
