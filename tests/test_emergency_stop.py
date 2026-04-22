"""Tests for POST /api/emergency-stop and /api/bots/{slug}/drawdown/reset.

We reuse the API-KEY header path the rest of test_web_routes.py uses,
which keeps us off the session-cookie path entirely — any stateful
session fixture that mutates the shared DB leaks across test files
and breaks unrelated API-key auth tests.
"""

import json
import logging
import os
import sys

os.environ["REVERTO_API_KEY"] = "testkey-for-pytest"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web import app as webapp  # noqa: E402
from web.routes import admin as _admin_routes  # noqa: E402


CLIENT = TestClient(webapp.app)
AUTH = {"X-API-Key": "testkey-for-pytest"}


@pytest.fixture(autouse=True)
def _reset_slowapi_limits():
    """Clear the process-wide slowapi limiter between tests.

    /api/emergency-stop carries a 5/minute cap. With the cookie-auth
    tests added for v26-02, this module now issues more than five
    POSTs in a single pytest process — without this reset the last
    tests in the run hit 429 instead of the behaviour they're
    asserting on. The existing TestEmergencyStop suite happened to
    stay under the cap by luck; codifying the reset keeps both old
    and new tests independent of each other's ordering.
    """
    try:
        webapp.limiter.reset()
    except Exception:
        # slowapi's in-memory storage exposes reset(); any other
        # backend (redis in prod) would ignore this fixture, which
        # is what we want — tests only ever run against in-memory.
        pass
    yield


class _FakeBotInfo:
    """Minimal BotInfo stand-in for the emergency-stop tests. Mirrors
    the attributes ``api_emergency_stop`` reads: ``user_id``, ``slug``,
    and the ``running`` property. Shaped as a class so
    ``bot.user_id`` / ``bot.slug`` / ``bot.running`` look like the
    real BotInfo (see ``web/app.py:423``).
    """

    def __init__(self, user_id: int, slug: str, running: bool = True):
        self.user_id = user_id
        self.slug = slug
        self.running = running


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

    def test_calls_stop_bot_for_each_registered_bot(self, monkeypatch):
        """Audit v26 v26-15h regression guard: exercise the loop body,
        not just the empty-registry branch. Pre-fix this endpoint called
        ``stop_bot(bot.slug)`` with a missing ``user_id`` argument —
        the TypeError was swallowed by the bare ``except`` and every
        bot landed in ``failed`` without a SIGTERM. This test pins
        that ``stop_bot`` is invoked once per running bot with the
        ``(int, str)`` signature.
        """
        bots = [
            _FakeBotInfo(user_id=1, slug="alpha"),
            _FakeBotInfo(user_id=1, slug="beta"),
        ]

        async def _fake_all():
            return bots

        calls: list[tuple] = []

        async def _fake_stop_bot(user_id, slug):
            calls.append((user_id, slug))
            return {"ok": True}

        monkeypatch.setattr(webapp.registry, "all", _fake_all)
        monkeypatch.setattr(_admin_routes, "stop_bot", _fake_stop_bot)

        r = CLIENT.post("/api/emergency-stop", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert sorted(body["stopped_bots"]) == ["alpha", "beta"]
        assert body["failed"] == []

        # Two bots → two calls, each with (int user_id, str slug).
        assert len(calls) == 2
        for uid, slug in calls:
            assert isinstance(uid, int)
            assert uid > 0
            assert isinstance(slug, str)
            assert slug in ("alpha", "beta")

    def test_skips_bots_that_are_not_running(self, monkeypatch):
        """Only the ``running`` bots reach stop_bot. A stale pid-file
        (running=False) is a registry row but not a stop target."""
        bots = [
            _FakeBotInfo(user_id=1, slug="alive", running=True),
            _FakeBotInfo(user_id=1, slug="dead", running=False),
        ]

        async def _fake_all():
            return bots

        calls: list[tuple] = []

        async def _fake_stop_bot(user_id, slug):
            calls.append((user_id, slug))
            return {"ok": True}

        monkeypatch.setattr(webapp.registry, "all", _fake_all)
        monkeypatch.setattr(_admin_routes, "stop_bot", _fake_stop_bot)

        r = CLIENT.post("/api/emergency-stop", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["stopped_bots"] == ["alive"]
        assert calls == [(1, "alive")]

    def test_type_error_surfaces_in_logs(self, monkeypatch, caplog):
        """Audit v26 v26-15h defence-in-depth: if stop_bot ever raises
        a TypeError again (signature drift, refactor mistake), the
        error must land in the portal log with a stacktrace — the
        pre-fix bare ``except`` swallowed it into the JSON response
        only, where it was invisible to operators.
        """
        bots = [_FakeBotInfo(user_id=1, slug="broken")]

        async def _fake_all():
            return bots

        async def _broken_stop_bot(user_id, slug):
            raise TypeError("simulated signature mismatch")

        monkeypatch.setattr(webapp.registry, "all", _fake_all)
        monkeypatch.setattr(_admin_routes, "stop_bot", _broken_stop_bot)

        with caplog.at_level(logging.ERROR, logger="web.routes.admin"):
            r = CLIENT.post("/api/emergency-stop", headers=AUTH)

        # Endpoint still returns 200 with the broken bot in `failed` —
        # matches the contract for any per-bot error.
        assert r.status_code == 200
        body = r.json()
        assert body["stopped_bots"] == []
        assert len(body["failed"]) == 1
        assert body["failed"][0]["slug"] == "broken"
        # The error string survives in the JSON payload (trimmed to 200).
        assert "simulated signature mismatch" in body["failed"][0]["error"]

        # And logger.exception captured the stacktrace in portal.log.
        messages = [rec.message for rec in caplog.records]
        assert any("emergency_stop" in m for m in messages), (
            f"expected an emergency_stop error record, got {messages}"
        )
        # exc_info was attached by logger.exception.
        assert any(
            rec.exc_info is not None
            for rec in caplog.records
            if "emergency_stop" in rec.message
        ), "logger.exception should attach exc_info for stacktrace visibility"


class TestEmergencyStopRoleGate:
    """Audit v26-02 role-gate on /api/emergency-stop.

    Covers the cookie-auth path — existing TestEmergencyStop uses the
    API-key header, which resolves to the admin stub (see
    ``_request_user`` fallback for ``X-API-Key``) and therefore never
    exercises the role-check code path. These tests seed a real
    non-admin user in the DB, mint a session cookie for them, and
    verify the 403 branch + audit-log behaviour.
    """

    _COOKIE_NAME = "reverto_session"

    def _seed_user(self, username: str, role: str) -> int:
        """Insert a users row directly and return its id.

        We bypass the normal password-set flow because these tests
        don't go through /api/auth/login — the session cookie is
        minted in-process via webapp._create_session_cookie, which
        only needs the row to exist (active=1) for
        ``user_store.get_user_by_id`` to resolve it.
        """
        from core.database import get_db

        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO users (username, active, role) "
            "VALUES (?, 1, ?)",
            (username, role),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,),
        ).fetchone()
        return int(row["id"])

    def _cookie_for(self, username: str, role: str) -> str:
        from core import user_store

        self._seed_user(username, role)
        user = user_store.get_user_by_username(username)
        assert user is not None
        assert user.role == role
        return webapp._create_session_cookie(user)

    def test_emergency_stop_admin_succeeds(self, monkeypatch):
        """Admin session + empty registry → 200 with empty stopped
        list. Confirms the role-check doesn't reject the happy path.
        """
        async def _fake_all():
            return []

        monkeypatch.setattr(webapp.registry, "all", _fake_all)

        client = TestClient(webapp.app)
        client.cookies.set(
            self._COOKIE_NAME,
            self._cookie_for("admin", "admin"),
        )
        r = client.post("/api/emergency-stop")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["triggered_by"] == "admin"

    def test_emergency_stop_non_admin_returns_403(self, monkeypatch):
        """Non-admin session → 403 with an 'admin role' message. The
        role-check runs BEFORE the registry is touched, so we don't
        need to stub registry.all here — if the gate were missing the
        response would hit the un-stubbed all() and shape would
        differ.
        """
        client = TestClient(webapp.app)
        client.cookies.set(
            self._COOKIE_NAME,
            self._cookie_for("pytest_user", "user"),
        )
        r = client.post("/api/emergency-stop")
        assert r.status_code == 403, r.text
        assert "admin role" in r.json()["detail"].lower()

    def test_emergency_stop_logs_forbidden_attempt(self, caplog):
        """A forbidden attempt must leave a WARNING line with the
        username + role so operators can see failed attempts without
        having to replay the traffic.
        """
        client = TestClient(webapp.app)
        client.cookies.set(
            self._COOKIE_NAME,
            self._cookie_for("pytest_user", "user"),
        )

        with caplog.at_level(logging.WARNING, logger="web.routes.admin"):
            r = client.post("/api/emergency-stop")

        assert r.status_code == 403
        relevant = [
            rec for rec in caplog.records
            if "non-admin" in rec.getMessage()
        ]
        assert relevant, (
            "expected a non-admin emergency-stop warning, got "
            f"{[rec.getMessage() for rec in caplog.records]}"
        )
        msg = relevant[0].getMessage()
        assert "pytest_user" in msg
        assert "role=user" in msg

    def test_emergency_stop_no_session_returns_401(self):
        """No session cookie → 401 from AuthMiddleware before the
        route-level role-check runs. Guards against a regression where
        the role-check might be reached on an unauthenticated request
        (which would be a gate mis-ordering).
        """
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.post("/api/emergency-stop")
        assert r.status_code == 401


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
