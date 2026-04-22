"""Tests for the admin cross-user bot-overview routes.

Backend surface introduced by the Fase 1 Admin Bot Overview feature:

  GET  /api/admin/bots
  POST /api/admin/bots/{uid}/{slug}/start
  POST /api/admin/bots/{uid}/{slug}/start-dry-run
  POST /api/admin/bots/{uid}/{slug}/stop
  POST /api/admin/bots/{uid}/{slug}/restart

Every endpoint is admin-only (role-gate mirrors v26-02 on
/api/emergency-stop). Lifecycle endpoints double-log: central
audit.log via ``_audit()`` + the target bot's own log via
``_log_to_bot_log`` so the owner sees an ``[ADMIN]`` line when
tailing their usual log file.
"""

from __future__ import annotations

import logging
import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web import app as webapp  # noqa: E402
from web.routes import admin_bots as _admin_bots_routes  # noqa: E402


_COOKIE_NAME = "reverto_session"


@pytest.fixture(autouse=True)
def _reset_slowapi_limits():
    """Clear the process-wide slowapi limiter between tests.

    Admin lifecycle endpoints are capped at 20/minute; the suite
    below fires more than that in a single pytest process. Resetting
    per-test keeps assertions independent of ordering.
    """
    try:
        webapp.limiter.reset()
    except Exception:
        pass
    yield


def _seed_user(username: str, role: str) -> int:
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


def _cookie_for(username: str, role: str) -> str:
    from core import user_store

    _seed_user(username, role)
    user = user_store.get_user_by_username(username)
    assert user is not None
    assert user.role == role
    return webapp._create_session_cookie(user)


class _FakeBot:
    """Mimics the BotInfo attributes the list endpoint reads.

    Only the ``user_id``, ``slug``, ``running`` and ``read_state``
    surface is touched by ``list_all_bots``; keeping the shim
    minimal makes the tests easy to grow without being dragged
    into the full BotInfo constructor API.
    """

    def __init__(self, user_id: int, slug: str, *,
                 running: bool = False, state: dict | None = None):
        self.user_id = user_id
        self.slug = slug
        self.running = running
        self._state = state or {
            "bot_name": slug.capitalize(),
            "mode": "paper",
            "exchange": "bitget",
            "pair": "BTC/USD",
            "current_price": 0.0,
            "balance_btc": 0.1,
            "total_pnl_btc": 0.0,
            "open_deals_count": 0,
            "closed_deals_count": 0,
            "win_rate": 0.0,
        }

    def read_state(self) -> dict:
        return dict(self._state)


# ── GET /api/admin/bots ────────────────────────────────────────────────────


class TestListAllBots:
    _URL = "/api/admin/bots"

    def test_admin_succeeds(self, monkeypatch):
        async def _fake_all():
            return []

        monkeypatch.setattr(webapp.registry, "all", _fake_all)

        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_admin_bots", "admin"))
        r = client.get(self._URL)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "users" in body
        assert isinstance(body["users"], list)

    def test_non_admin_returns_403(self, monkeypatch):
        async def _fake_all():
            return []

        monkeypatch.setattr(webapp.registry, "all", _fake_all)

        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_user_bots", "user"))
        r = client.get(self._URL)
        assert r.status_code == 403
        assert "admin role" in r.json()["detail"].lower()

    def test_groups_bots_by_user(self, monkeypatch):
        """Two users, three bots — the response must bucket the bots
        under their owners and sort users by id for stable UI order.
        """
        bots = [
            _FakeBot(user_id=2, slug="beta"),
            _FakeBot(user_id=1, slug="alpha"),
            _FakeBot(user_id=1, slug="gamma", running=True),
        ]

        async def _fake_all():
            return bots

        monkeypatch.setattr(webapp.registry, "all", _fake_all)

        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_admin_grp", "admin"))
        r = client.get(self._URL)
        assert r.status_code == 200, r.text
        body = r.json()
        # Sorted by user_id ascending.
        ids_in_order = [u["user_id"] for u in body["users"]]
        assert ids_in_order == sorted(ids_in_order)
        # Each user_id bucket contains the expected slugs.
        by_uid = {u["user_id"]: u for u in body["users"]}
        assert {b["slug"] for b in by_uid[1]["bots"]} == {"alpha", "gamma"}
        assert {b["slug"] for b in by_uid[2]["bots"]} == {"beta"}
        # running flag is surfaced per bot.
        gamma = next(b for b in by_uid[1]["bots"] if b["slug"] == "gamma")
        assert gamma["running"] is True

    def test_includes_username_from_user_store(self, monkeypatch):
        """The response carries each owner's username so the
        frontend can render per-user headers without an N+1 fetch.
        """
        uid_alice = _seed_user("pytest_alice_bots", "user")

        async def _fake_all():
            return [_FakeBot(user_id=uid_alice, slug="solo")]

        monkeypatch.setattr(webapp.registry, "all", _fake_all)

        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_admin_un", "admin"))
        r = client.get(self._URL)
        assert r.status_code == 200, r.text
        by_uid = {u["user_id"]: u for u in r.json()["users"]}
        assert by_uid[uid_alice]["username"] == "pytest_alice_bots"


# ── POST /api/admin/bots/{uid}/{slug}/{action} ─────────────────────────────


class _LifecycleCallTracker:
    """Captures every call the admin routes make into the start_bot /
    stop_bot / restart_bot / start_bot_dry_run helpers so we can
    assert on (user_id, slug) without spinning up real subprocesses.
    """

    def __init__(self):
        self.calls: list[tuple[str, int, str]] = []

    def install(self, monkeypatch):
        async def _start(uid, slug):
            self.calls.append(("start", uid, slug))
            return {"ok": True, "message": f"{slug} started"}

        async def _start_dry_run(uid, slug):
            self.calls.append(("start_dry_run", uid, slug))
            return {"ok": True, "message": f"{slug} dry-run started"}

        async def _stop(uid, slug):
            self.calls.append(("stop", uid, slug))
            return {"ok": True, "message": f"{slug} stopped"}

        async def _restart(uid, slug):
            self.calls.append(("restart", uid, slug))
            return {"ok": True, "message": f"{slug} restarted"}

        monkeypatch.setattr(_admin_bots_routes, "start_bot", _start)
        monkeypatch.setattr(
            _admin_bots_routes, "start_bot_dry_run", _start_dry_run,
        )
        monkeypatch.setattr(_admin_bots_routes, "stop_bot", _stop)
        monkeypatch.setattr(_admin_bots_routes, "restart_bot", _restart)


class TestAdminLifecycleEndpoints:
    _TARGET_UID = 7
    _TARGET_SLUG = "targetbot"

    def _url(self, action: str) -> str:
        return f"/api/admin/bots/{self._TARGET_UID}/{self._TARGET_SLUG}/{action}"

    def test_admin_start_invokes_helper(self, monkeypatch):
        tracker = _LifecycleCallTracker()
        tracker.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_lc1", "admin"),
        )
        r = client.post(self._url("start"))
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert tracker.calls == [
            ("start", self._TARGET_UID, self._TARGET_SLUG)
        ]

    def test_admin_stop_invokes_helper(self, monkeypatch):
        tracker = _LifecycleCallTracker()
        tracker.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_lc2", "admin"),
        )
        r = client.post(self._url("stop"))
        assert r.status_code == 200, r.text
        assert tracker.calls == [
            ("stop", self._TARGET_UID, self._TARGET_SLUG)
        ]

    def test_admin_restart_invokes_helper(self, monkeypatch):
        tracker = _LifecycleCallTracker()
        tracker.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_lc3", "admin"),
        )
        r = client.post(self._url("restart"))
        assert r.status_code == 200, r.text
        assert tracker.calls == [
            ("restart", self._TARGET_UID, self._TARGET_SLUG)
        ]

    def test_admin_start_dry_run_invokes_helper(self, monkeypatch):
        tracker = _LifecycleCallTracker()
        tracker.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_lc4", "admin"),
        )
        r = client.post(self._url("start-dry-run"))
        assert r.status_code == 200, r.text
        assert tracker.calls == [
            ("start_dry_run", self._TARGET_UID, self._TARGET_SLUG)
        ]

    def test_non_admin_returns_403(self, monkeypatch):
        tracker = _LifecycleCallTracker()
        tracker.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_user_lc", "user"),
        )
        r = client.post(self._url("start"))
        assert r.status_code == 403
        # Helper must NOT be reached on the reject path.
        assert tracker.calls == []

    def test_invalid_slug_returns_400(self, monkeypatch):
        """Traversal-shaped slugs are refused by the regex guard
        before we ever invoke start_bot.
        """
        tracker = _LifecycleCallTracker()
        tracker.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_badslug", "admin"),
        )
        # "..%2Fetc" URL-decodes to "../etc" — either the route doesn't
        # match (404) or our regex returns 400. Both are acceptable;
        # what's NOT acceptable is reaching start_bot.
        r = client.post("/api/admin/bots/1/..%2Fetc/start")
        assert r.status_code in (400, 404)
        assert tracker.calls == []


class TestAdminActionAudit:
    _TARGET_UID = 1
    _TARGET_SLUG = "audit_test_bot"

    def test_admin_action_logs_to_bot_log(self, monkeypatch, tmp_path):
        """An admin start call must append an ``[ADMIN]`` line to the
        target bot's log file via ``_log_to_bot_log``. We redirect
        ``paths.user_logs_dir`` at the monkeypatch level so the test
        writes inside tmp_path instead of the repo's real logs tree.
        """
        from core import paths

        def _fake_logs_dir(uid: int):
            d = tmp_path / "logs" / str(uid)
            d.mkdir(parents=True, exist_ok=True)
            return d

        monkeypatch.setattr(webapp.paths, "user_logs_dir", _fake_logs_dir)
        monkeypatch.setattr(paths, "user_logs_dir", _fake_logs_dir)

        tracker = _LifecycleCallTracker()
        tracker.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_audit", "admin"),
        )
        r = client.post(
            f"/api/admin/bots/{self._TARGET_UID}/{self._TARGET_SLUG}/start",
        )
        assert r.status_code == 200, r.text

        bot_log = tmp_path / "logs" / str(self._TARGET_UID) / (
            self._TARGET_SLUG + ".log"
        )
        assert bot_log.exists(), "admin action should have created bot log"
        contents = bot_log.read_text(encoding="utf-8")
        assert "[ADMIN]" in contents
        assert "Bot started by admin" in contents
        assert "pytest_admin_audit" in contents

    def test_admin_action_audited_in_central_log(self, monkeypatch):
        """The central audit stream must still see an entry even when
        the bot-log write is also happening. The ``reverto.audit``
        logger has ``propagate=False`` (it only writes to audit.log),
        so we can't hook it via caplog — attach a dedicated capture
        handler for the duration of the request instead.
        """
        tracker = _LifecycleCallTracker()
        tracker.install(monkeypatch)

        captured: list[str] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        audit_logger = logging.getLogger("reverto.audit")
        capture_handler = _CaptureHandler(level=logging.INFO)
        audit_logger.addHandler(capture_handler)
        try:
            client = TestClient(webapp.app)
            client.cookies.set(
                _COOKIE_NAME, _cookie_for("pytest_admin_aud2", "admin"),
            )
            r = client.post(
                f"/api/admin/bots/{self._TARGET_UID}/{self._TARGET_SLUG}/start",
            )
            assert r.status_code == 200, r.text
        finally:
            audit_logger.removeHandler(capture_handler)

        assert any("admin_bot_start" in m for m in captured), (
            f"expected admin_bot_start in audit log, got {captured}"
        )


# ── Bulk endpoints (Fase 2) ────────────────────────────────────────────────


class _BulkHelperStub:
    """Per-test helper that drives ``stop_bot`` / ``restart_bot``
    results. Accepts a ``fail_slugs`` set so the test can make a
    subset of targets "fail" to validate partial-success shape.
    """

    def __init__(self, *, fail_slugs: set[str] | None = None):
        self.fail_slugs = fail_slugs or set()
        self.calls: list[tuple[str, int, str]] = []

    def install(self, monkeypatch):
        async def _stop(uid, slug):
            self.calls.append(("stop", uid, slug))
            if slug in self.fail_slugs:
                return {"ok": False, "error": f"simulated stop fail for {slug}"}
            return {"ok": True, "message": f"{slug} stopped"}

        async def _restart(uid, slug):
            self.calls.append(("restart", uid, slug))
            if slug in self.fail_slugs:
                return {"ok": False, "error": f"simulated restart fail for {slug}"}
            return {"ok": True, "message": f"{slug} restarted"}

        monkeypatch.setattr(_admin_bots_routes, "stop_bot", _stop)
        monkeypatch.setattr(_admin_bots_routes, "restart_bot", _restart)


def _bulk_body(targets: list[tuple[int, str]]) -> dict:
    return {"bots": [{"user_id": uid, "slug": slug} for uid, slug in targets]}


class TestBulkActions:
    """POST /api/admin/bots/bulk/{stop,restart} — sequential bulk
    lifecycle with partial-success accounting and a hard 20-bot
    cap.
    """

    _STOP_URL = "/api/admin/bots/bulk/stop"
    _RESTART_URL = "/api/admin/bots/bulk/restart"

    def test_bulk_stop_admin_all_succeed(self, monkeypatch):
        helper = _BulkHelperStub()
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_bulk1", "admin"),
        )
        body = _bulk_body([(1, "alpha"), (1, "beta"), (2, "gamma")])
        r = client.post(self._STOP_URL, json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["ok"] is True
        assert payload["total_requested"] == 3
        assert payload["total_succeeded"] == 3
        assert payload["total_failed"] == 0
        assert len(payload["processed"]) == 3
        assert payload["failed"] == []
        # Helper must have been invoked once per target in request order.
        assert [c[0] for c in helper.calls] == ["stop"] * 3
        assert [(c[1], c[2]) for c in helper.calls] == [
            (1, "alpha"), (1, "beta"), (2, "gamma"),
        ]

    def test_bulk_stop_partial_failure(self, monkeypatch):
        """One target returns ok=False — it lands in ``failed`` with
        the error string; the other two stay in ``processed``.
        """
        helper = _BulkHelperStub(fail_slugs={"beta"})
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_bulk2", "admin"),
        )
        body = _bulk_body([(1, "alpha"), (1, "beta"), (2, "gamma")])
        r = client.post(self._STOP_URL, json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["total_requested"] == 3
        assert payload["total_succeeded"] == 2
        assert payload["total_failed"] == 1
        assert {p["slug"] for p in payload["processed"]} == {"alpha", "gamma"}
        assert len(payload["failed"]) == 1
        assert payload["failed"][0]["slug"] == "beta"
        assert "simulated stop fail" in payload["failed"][0]["error"]

    def test_bulk_stop_helper_exception_captured(self, monkeypatch):
        """If the lifecycle helper raises (not just returns ok=False)
        the bulk loop must catch it, record the target as failed, and
        still process the remaining targets.
        """
        calls: list[tuple[int, str]] = []

        async def _stop(uid, slug):
            calls.append((uid, slug))
            if slug == "boom":
                raise RuntimeError("simulated helper crash")
            return {"ok": True}

        monkeypatch.setattr(_admin_bots_routes, "stop_bot", _stop)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_bulk_exc", "admin"),
        )
        body = _bulk_body([(1, "alpha"), (1, "boom"), (1, "gamma")])
        r = client.post(self._STOP_URL, json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["total_succeeded"] == 2
        assert payload["total_failed"] == 1
        boom_failure = payload["failed"][0]
        assert boom_failure["slug"] == "boom"
        assert "simulated helper crash" in boom_failure["error"]
        # All three targets reached the helper — loop did not abort early.
        assert calls == [(1, "alpha"), (1, "boom"), (1, "gamma")]

    def test_bulk_stop_non_admin_returns_403(self, monkeypatch):
        helper = _BulkHelperStub()
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_user_bulk", "user"),
        )
        r = client.post(
            self._STOP_URL, json=_bulk_body([(1, "alpha")]),
        )
        assert r.status_code == 403
        # Reject before any helper invocation.
        assert helper.calls == []

    def test_bulk_stop_invalid_slug_returns_400(self, monkeypatch):
        """Slug that passes Pydantic min/max-length but would break
        the regex guard must be refused with 400 before any helper
        runs. ``..slash`` is URL-safe (no %2F decoding needed) and
        Pydantic won't object, so this exercises our own
        _BOT_SLUG_RE guard in ``_validate_bulk_slugs``.
        """
        helper = _BulkHelperStub()
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_badslug2", "admin"),
        )
        body = {"bots": [
            {"user_id": 1, "slug": "ok_slug"},
            {"user_id": 1, "slug": "../etc"},
        ]}
        r = client.post(self._STOP_URL, json=body)
        assert r.status_code == 400
        assert "invalid slug" in r.json()["detail"].lower()
        assert helper.calls == [], (
            "traversal slug must be rejected before any target is "
            "processed, including the valid one earlier in the list"
        )

    def test_bulk_stop_max_20_enforced(self, monkeypatch):
        """21 targets → 422 from Pydantic max_length validation."""
        helper = _BulkHelperStub()
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_cap", "admin"),
        )
        body = _bulk_body([(1, f"bot{i}") for i in range(21)])
        r = client.post(self._STOP_URL, json=body)
        assert r.status_code == 422, r.text
        assert helper.calls == []

    def test_bulk_stop_empty_list_returns_422(self, monkeypatch):
        """Empty targets list → 422 (Pydantic min_length=1).

        Doc-style: spec asked for 400, Pydantic's validator returns
        422 Unprocessable Entity for schema failures — same "reject
        before handler" semantics, documented here so the caller
        knows which code to catch.
        """
        helper = _BulkHelperStub()
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_empty", "admin"),
        )
        r = client.post(self._STOP_URL, json={"bots": []})
        assert r.status_code == 422
        assert helper.calls == []

    def test_bulk_stop_audits_to_central_log(self, monkeypatch):
        helper = _BulkHelperStub()
        helper.install(monkeypatch)

        captured: list[str] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        audit_logger = logging.getLogger("reverto.audit")
        handler = _CaptureHandler(level=logging.INFO)
        audit_logger.addHandler(handler)
        try:
            client = TestClient(webapp.app)
            client.cookies.set(
                _COOKIE_NAME,
                _cookie_for("pytest_admin_bulk_aud", "admin"),
            )
            r = client.post(
                self._STOP_URL,
                json=_bulk_body([(1, "alpha"), (1, "beta")]),
            )
            assert r.status_code == 200, r.text
        finally:
            audit_logger.removeHandler(handler)

        assert any("admin_bulk_stop" in m for m in captured), (
            f"expected admin_bulk_stop audit entry, got {captured}"
        )
        assert any("count=2" in m for m in captured), (
            "audit entry should encode the target count"
        )

    def test_bulk_stop_logs_admin_line_per_success(
        self, monkeypatch, tmp_path,
    ):
        """Each successful bulk-stop must append an ``[ADMIN]`` line
        to the target bot's own log via ``_log_to_bot_log``.
        Failures do NOT get a courtesy line — the failure is already
        surfaced in the response payload and audit stream.
        """
        from core import paths

        def _fake_logs_dir(uid: int):
            d = tmp_path / "logs" / str(uid)
            d.mkdir(parents=True, exist_ok=True)
            return d

        monkeypatch.setattr(webapp.paths, "user_logs_dir", _fake_logs_dir)
        monkeypatch.setattr(paths, "user_logs_dir", _fake_logs_dir)

        helper = _BulkHelperStub(fail_slugs={"beta"})
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME,
            _cookie_for("pytest_admin_bulk_botlog", "admin"),
        )
        r = client.post(
            self._STOP_URL,
            json=_bulk_body([(1, "alpha"), (1, "beta"), (1, "gamma")]),
        )
        assert r.status_code == 200, r.text

        # alpha + gamma succeeded → each should have an [ADMIN] line.
        for slug in ("alpha", "gamma"):
            log_path = tmp_path / "logs" / "1" / f"{slug}.log"
            assert log_path.exists(), f"expected bot log for {slug}"
            contents = log_path.read_text(encoding="utf-8")
            assert "[ADMIN]" in contents
            assert "bulk" in contents.lower()
            assert "pytest_admin_bulk_botlog" in contents

        # beta failed → no [ADMIN] line written. The log may not
        # exist at all (no writes happened) — both states are fine.
        beta_log = tmp_path / "logs" / "1" / "beta.log"
        if beta_log.exists():
            assert "[ADMIN]" not in beta_log.read_text(encoding="utf-8")

    def test_bulk_restart_admin_all_succeed(self, monkeypatch):
        helper = _BulkHelperStub()
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_bulkr1", "admin"),
        )
        body = _bulk_body([(1, "alpha"), (2, "beta")])
        r = client.post(self._RESTART_URL, json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["total_succeeded"] == 2
        assert payload["total_failed"] == 0
        assert [c[0] for c in helper.calls] == ["restart", "restart"]

    def test_bulk_restart_partial_failure(self, monkeypatch):
        helper = _BulkHelperStub(fail_slugs={"alpha"})
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_admin_bulkr2", "admin"),
        )
        body = _bulk_body([(1, "alpha"), (1, "beta")])
        r = client.post(self._RESTART_URL, json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["total_succeeded"] == 1
        assert payload["total_failed"] == 1
        assert payload["processed"][0]["slug"] == "beta"
        assert payload["failed"][0]["slug"] == "alpha"

    def test_bulk_restart_non_admin_returns_403(self, monkeypatch):
        helper = _BulkHelperStub()
        helper.install(monkeypatch)

        client = TestClient(webapp.app)
        client.cookies.set(
            _COOKIE_NAME, _cookie_for("pytest_user_bulkr", "user"),
        )
        r = client.post(
            self._RESTART_URL, json=_bulk_body([(1, "alpha")]),
        )
        assert r.status_code == 403
        assert helper.calls == []
