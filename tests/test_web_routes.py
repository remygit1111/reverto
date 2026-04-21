# tests/test_web_routes.py
# Smoke tests voor de web portal routes. Vangt regressies waarbij POST
# en GET op hetzelfde pad (/api/bots) per ongeluk conflicteren of een
# route niet geregistreerd is.
#
# Bijzonder belangrijk voor /api/bots: GET (lijst) en POST (create)
# leven op hetzelfde pad en moeten beide beschikbaar zijn.

import os
import sys

os.environ["REVERTO_API_KEY"] = "testkey-for-pytest"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from web.app import app

CLIENT = TestClient(app)
AUTH = {"X-API-Key": "testkey-for-pytest"}
JSON = {**AUTH, "Content-Type": "application/json"}

# Use a slug that could never collide with a real bot.
# Phase-2 layout: create_bot writes to config/bots/<user_id>/<slug>.yaml
# so every test path lives under user 1.
_TEST_SLUG = "pytest_route_check"
_TEST_USER_ID = 1
_TEST_YAML = f"config/bots/{_TEST_USER_ID}/{_TEST_SLUG}.yaml"
_TEST_STATE = f"logs/{_TEST_USER_ID}/{_TEST_SLUG}.state.json"
_TEST_PID = f"logs/{_TEST_USER_ID}/pids/{_TEST_SLUG}.pid"
_TEST_LOG = f"logs/{_TEST_USER_ID}/{_TEST_SLUG}.log"


@pytest.fixture(autouse=True)
def _cleanup_yaml():
    """Ensure test artefacts are gone before AND after every test.

    The web.app TestClient was bound at import time with the real
    BASE_DIR, so monkey-patching core.paths.BASE_DIR mid-test doesn't
    redirect create_bot's output. Instead we let the tests write to
    the Phase-2 location under config/bots/1/ and then sweep each
    test's artefacts + any credentials side-files so the production
    tree stays clean between runs.
    """
    def _sweep():
        for path in (_TEST_YAML, _TEST_STATE, _TEST_PID, _TEST_LOG):
            if os.path.exists(path):
                os.remove(path)
        # logs/credentials.json no longer exists in Phase 2, but clear
        # it if a stray pre-MT test created one.
        legacy = "logs/credentials.json"
        if os.path.exists(legacy):
            os.remove(legacy)

    _sweep()
    yield
    _sweep()


def _make_payload(name: str = "Pytest Route Check") -> dict:
    return {
        "bot": {
            "name": name,
            "mode": "paper",
            "exchange": "bitget",
            "pair": "BTC/USD",
            "contract_type": "inverse_perpetual",
            "leverage": {"enabled": False, "size": 1},
            "dca": {
                "base_order_size": 0.001,
                "max_orders": 5,
                "order_spacing_pct": 2.5,
                "multiplier": 1.5,
            },
            "entry": {"indicators": []},
            "take_profit": {"target_pct": 3.0},
            "stop_loss": {"type": "fixed", "pct": 5.0},
        }
    }


class TestBotsRouteRegistration:
    """GET en POST /api/bots moeten allebei geregistreerd zijn."""

    def test_get_bots_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/bots" and "GET" in getattr(r, "methods", set())
        ]
        assert len(routes) == 1, "GET /api/bots must be registered exactly once"

    def test_post_bots_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/bots" and "POST" in getattr(r, "methods", set())
        ]
        assert len(routes) == 1, "POST /api/bots must be registered exactly once"

    def test_both_methods_share_path(self):
        methods = set()
        for r in app.routes:
            if getattr(r, "path", "") == "/api/bots":
                methods.update(getattr(r, "methods", set()))
        assert {"GET", "POST"} <= methods, (
            f"/api/bots must accept both GET and POST, got {methods}"
        )


class TestPostBotsSmoke:
    """End-to-end smoke tests tegen POST /api/bots."""

    def test_post_without_auth_is_401(self):
        r = CLIENT.post("/api/bots", json=_make_payload())
        assert r.status_code == 401

    def test_post_without_body_is_400_not_405(self):
        # 400 is correct: the handler reads the body manually now
        # (audit v25 #5 body-size cap lifted the `body: dict` param),
        # so an empty body fails JSON-parse with 400. 405 would mean
        # the POST route is not registered — exactly the regression
        # we guard against.
        r = CLIENT.post("/api/bots", headers=AUTH)
        assert r.status_code != 405
        assert r.status_code == 400

    def test_post_with_valid_payload_creates_bot(self):
        r = CLIENT.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert data.get("ok") is True
        assert data.get("slug") == _TEST_SLUG
        assert os.path.exists(_TEST_YAML)

    def test_duplicate_returns_409(self):
        # First create succeeds
        r1 = CLIENT.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r1.status_code == 200
        # Second create on same slug returns 409 Conflict
        r2 = CLIENT.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r2.status_code == 409
        assert "already exists" in r2.json().get("detail", "")

    def test_post_does_not_break_get(self):
        # After a POST the GET listing should still return 200, not 405
        CLIENT.post("/api/bots", json=_make_payload(), headers=JSON)
        # AuthMiddleware now gates /api/*, so pass the API key — the
        # middleware treats a valid X-API-Key as an authenticated principal.
        r = CLIENT.get("/api/bots", headers=AUTH)
        assert r.status_code == 200
        assert "bots" in r.json()


class TestGetBotSlug:
    """Audit v26-17: GET /api/bots/{slug} returnt 404 bij onbekende
    slug (pre-fix: 200 met ``{"error": ...}`` body, inconsistent met
    andere /api/bots/{slug}/... endpoints die al 404 raisen)."""

    def test_get_unknown_bot_slug_returns_404(self, monkeypatch):
        async def _fake_get(user_id, slug):
            return None

        monkeypatch.setattr(webapp.registry, "get", _fake_get)

        r = CLIENT.get("/api/bots/no_such_bot", headers=AUTH)
        assert r.status_code == 404
        body = r.json()
        # FastAPI's standard error-envelope is {"detail": "..."}.
        assert "detail" in body
        assert "Unknown bot" in body["detail"]
        assert "no_such_bot" in body["detail"]


class TestInvalidPayload:
    def test_missing_required_fields_is_400(self):
        bad = {"bot": {"name": "x", "mode": "paper", "exchange": "bitget"}}
        r = CLIENT.post("/api/bots", json=bad, headers=JSON)
        # Pydantic validation → our endpoint wraps into 400, not 422/500
        assert r.status_code == 400
        assert "Invalid config" in r.json().get("detail", "")

    def test_empty_name_after_slugify_is_400(self):
        # Name is all punctuation → slugify() raises → endpoint returns 400
        bad = _make_payload(name="@@@@")
        r = CLIENT.post("/api/bots", json=bad, headers=JSON)
        # Pydantic BotConfig.name validator rejects non-alnum first
        assert r.status_code == 400


# ── Auth tests ────────────────────────────────────────────────────────────────
# These exercise the session-cookie login flow, the gating middleware, and
# the change-password endpoint. They use a dedicated TestClient without the
# API key cookie so we're testing the session path, not the legacy API-key
# bypass.

from web import app as webapp  # noqa: E402

_KNOWN_PW = "pytest-known-password-123"


def _admin_cookie() -> str:
    """Test helper: mint a session cookie for the seeded admin user.

    Audit v26-05 removed the username-string fallback from
    ``_create_session_cookie``; it now accepts only ``User``
    instances. Centralising the admin-User lookup in one helper
    keeps every test-site to a single readable call.
    """
    from core import user_store
    admin = user_store.get_user_by_username("admin")
    assert admin is not None, "admin seed missing — check init_db"
    return webapp._create_session_cookie(admin)


@pytest.fixture(autouse=True)
def _disable_hibp_by_default(monkeypatch):
    """Patch ``web.routes.auth.is_password_pwned`` to an async no-op
    that returns False for every test in this module.

    Rationale: ``/api/auth/change-password`` now calls HIBP after the
    length-check. Without this patch, every auth-client test that
    exercises the success path — or any failure path past the length
    check — would fire a real network request at
    ``api.pwnedpasswords.com``. That would make the suite slow, add
    flakiness under CI network hiccups, and violate the "tests run
    offline" contract the rest of the suite already follows. Tests
    that specifically want to exercise the pwned-path override this
    fixture with their own monkeypatch.
    """
    async def _always_clean(_plaintext):
        return False

    monkeypatch.setattr(
        "web.routes.auth.is_password_pwned", _always_clean,
    )


@pytest.fixture
def auth_client():
    """TestClient met admin-user die een password heeft gezet via
    user_store.set_password(). De DB is al ge-isoleerd via autouse
    fixture ``_isolate_reverto_db`` in conftest.py, dus elke test
    start met een verse admin-row.

    Forces _COOKIE_SECURE=False for the duration of the test because
    TestClient serves over plain http:// and a browser-equivalent
    silently drops Secure cookies on insecure transports — without
    this override the post-login cookie would never reach the next
    request and every authed assertion would 401.
    """
    from core import user_store
    admin = user_store.get_user_by_username("admin")
    assert admin is not None, "admin seed missing — check init_db"
    user_store.set_password(admin.id, _KNOWN_PW)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    # httpx/TestClient in CI (Ubuntu, Python 3.13) drops strict-samesite
    # cookies on follow-up requests that lack an Origin header — and
    # TestClient doesn't synthesise one. Flip to 'lax' for the duration
    # of the test so the post-login cookie actually reaches /api/bots.
    # Production stays on 'strict' (real browsers always carry an
    # Origin/Referer so the strict policy fires as intended there).
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


class TestAuth:
    def test_status_unauthenticated_without_cookie(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/auth/status")
        assert r.status_code == 200
        # user_id is part of the response shape since the changelog
        # PR — the SPA uses it to gate admin-only nav items.
        assert r.json() == {
            "authenticated": False,
            "username": None,
            "user_id": None,
        }

    def test_login_bad_credentials_returns_401(self, auth_client):
        r = auth_client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid credentials"

    def test_login_success_sets_cookie(self, auth_client):
        r = auth_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # Set-Cookie header must carry the session cookie.
        set_cookie = r.headers.get("set-cookie", "")
        assert "reverto_session=" in set_cookie

    def test_gated_endpoint_requires_session(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/api/bots")
        assert r.status_code == 401

    def test_gated_endpoint_with_session_cookie_works(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/bots")
        assert r.status_code == 200
        assert "bots" in r.json()

    def test_change_password_rejects_short(self, auth_client):
        """Audit v26-03: minimum length is PASSWORD_MIN_LENGTH (12).
        The 11-char value would have passed the pre-fix <8 check; it
        must now be rejected by the centralised policy."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": _KNOWN_PW, "new_password": "elevenchars"},
        )
        assert r.status_code == 400
        assert "12 characters" in r.json().get("detail", "")

    def test_change_password_rejects_wrong_current(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": "not-it", "new_password": "longenough12"},
        )
        assert r.status_code == 401

    def test_change_password_rejects_pwned_password(
        self, auth_client, monkeypatch,
    ):
        """A password that HIBP flags as pwned must be refused with
        400 + the human-readable breach message — even if the current-
        password field would otherwise verify correctly."""
        async def _pwned(_plaintext):
            return True
        monkeypatch.setattr(
            "web.routes.auth.is_password_pwned", _pwned,
        )
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/auth/change-password",
            json={
                "current_password": _KNOWN_PW,
                "new_password": "correcthorsebatterystaple",
            },
        )
        assert r.status_code == 400
        assert "data breaches" in r.json().get("detail", "")

    def test_change_password_accepts_clean_password(
        self, auth_client, monkeypatch,
    ):
        """HIBP returns False → password change succeeds end-to-end,
        and the new password actually lands in the DB (verify_password
        accepts it post-change)."""
        async def _clean(_plaintext):
            return False
        monkeypatch.setattr(
            "web.routes.auth.is_password_pwned", _clean,
        )

        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        new_password = "fresh-clean-password-value-42"
        r = auth_client.post(
            "/api/auth/change-password",
            json={
                "current_password": _KNOWN_PW,
                "new_password": new_password,
            },
        )
        assert r.status_code == 200

        # Confirm the DB now accepts the new password (the store
        # layer, not just the route's response, actually changed).
        from core import user_store
        assert user_store.verify_password("admin", new_password) is not None

    def test_change_password_length_checked_before_hibp(
        self, auth_client, monkeypatch,
    ):
        """If the length check rejects the new password first, HIBP
        must not be called at all. Keeps the cheap sync check ahead
        of the expensive network hop — both as a latency property and
        so an 11-char attempt doesn't needlessly hit a public API."""
        calls: list[str] = []

        async def _tracking(plaintext):
            # We don't care about the plaintext value itself — just
            # record that the function was invoked. We explicitly do
            # NOT store the plaintext anywhere.
            calls.append("called")
            return False

        monkeypatch.setattr(
            "web.routes.auth.is_password_pwned", _tracking,
        )

        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/auth/change-password",
            json={
                "current_password": _KNOWN_PW,
                "new_password": "shortpw",  # 7 chars — below 12
            },
        )
        assert r.status_code == 400
        assert "12 characters" in r.json().get("detail", "")
        assert calls == [], (
            "HIBP check must not fire when the length gate already rejected "
            "the password"
        )

    def test_logout_rate_limited(self, auth_client):
        """Audit v26-04: /auth/logout had no @limiter.limit decorator,
        which meant an attacker with a valid session could flood
        logout to force session-termination noise. Now 10/minute.
        After 10 successful logouts in rapid succession, the 11th
        must return 429 Too Many Requests.
        """
        # Reset the slowapi bucket so this test is hermetic against
        # other rate-limited calls that may have happened earlier in
        # the suite.
        try:
            webapp.limiter.reset()
        except Exception:
            pass

        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        try:
            for i in range(10):
                r = auth_client.post("/auth/logout")
                # Each of the first 10 must succeed (200) or, after
                # the first logout bumps the epoch, silently
                # idempotent (still 200 per the endpoint contract).
                assert r.status_code == 200, (
                    f"request {i+1}/10 unexpectedly got {r.status_code}"
                )

            # The 11th request in the same minute must be rate-limited.
            r = auth_client.post("/auth/logout")
            assert r.status_code == 429
        finally:
            # Drain the consumed bucket so subsequent tests in the
            # same class / session don't inherit a full /auth/logout
            # limiter slot.
            try:
                webapp.limiter.reset()
            except Exception:
                pass


class TestPerUserSessionEpoch:
    """Phase-3a: session_epoch moved from a single .auth.json integer
    to a per-user DB column. Logout and password-change bump ONLY the
    caller's row; other users' cookies survive. The fresh-login-after-
    logout test runs unconditionally now — pre-Phase-3a it was
    @pytest.mark.skipif on CI because the file-based epoch invited
    races between WSL2 and Ubuntu runners. DB-backed UPDATE ... SET
    removes that class of flakiness entirely.
    """

    def test_logout_invalidates_existing_cookie(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        # Cookie works before logout.
        assert auth_client.get("/api/bots").status_code == 200
        # Logout bumps THIS user's epoch.
        assert auth_client.post("/auth/logout").status_code == 200
        # Same cookie value, but the server now rejects it because the
        # embedded epoch no longer matches the DB epoch for uid=1.
        auth_client.cookies.set("reverto_session", token)
        assert auth_client.get("/api/bots").status_code == 401

    def test_logout_bumps_only_callers_epoch(self, auth_client):
        """Insert a second user, bump admin's epoch, confirm the second
        user's epoch is untouched. Pre-Phase-3a this test would have
        been meaningless — epoch was global."""
        from core import user_store
        from core.database import get_db
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO users (username, role) VALUES ('bob', 'user')",
            )
        bob = user_store.get_user_by_username("bob")
        admin_before = user_store.get_session_epoch(1)
        bob_before = user_store.get_session_epoch(bob.id)

        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        auth_client.post("/auth/logout")

        assert user_store.get_session_epoch(1) == admin_before + 1
        assert user_store.get_session_epoch(bob.id) == bob_before

    def test_password_change_invalidates_existing_cookie(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        # Successful change → epoch bump → cookie no longer valid.
        r = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": _KNOWN_PW, "new_password": "newpassword42"},
        )
        assert r.status_code == 200
        auth_client.cookies.set("reverto_session", token)
        assert auth_client.get("/api/bots").status_code == 401

    def test_fresh_login_after_logout_works(self, auth_client):
        """Used to be @pytest.mark.skipif(CI) — pre-Phase-3a the
        .auth.json file-based epoch had a race window between WSL2 and
        CI filesystems that flaked on Ubuntu runners. Post-Phase-3a
        this is a straight SQLite UPDATE followed by a SELECT. The
        separate samesite='lax' override in the ``auth_client`` fixture
        covers a TestClient quirk where httpx drops strict-samesite
        cookies on follow-up requests that lack an Origin header."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        auth_client.post("/auth/logout")
        auth_client.cookies.clear()
        r = auth_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        assert auth_client.get("/api/bots").status_code == 200


class TestInactiveUserRejected:
    """Audit v26-01: ``_require_session`` must reject a user whose
    row has been flipped to ``active = 0`` even when their cookie
    still passes signature + TTL + session_epoch checks.
    ``_request_user`` already enforced this; the two helpers now
    share the same gate.

    Covers both auth-dependency paths:
      * ``_require_session`` → ``/api/auth/change-password``
      * ``_request_user``    → ``/api/bots``
    """

    def _deactivate_admin(self):
        """Flip users.active to 0 for the seeded admin row. Using raw
        SQL rather than a dedicated helper — there isn't one, and
        spawning an admin-freezer helper just for this test would be
        over-abstraction for a one-column UPDATE."""
        from core.database import get_db
        conn = get_db()
        with conn:
            conn.execute("UPDATE users SET active = 0 WHERE id = 1")

    def test_require_session_rejects_inactive_user(self, auth_client):
        """Pre-fix: cookie signature + epoch both passed, the endpoint
        served the deactivated user. Post-fix: 401 before the handler
        body runs. The 'User not found' detail matches what
        ``_request_user`` already returns for the same state, so the
        SPA's generic 401 handler fires regardless of which
        dependency the endpoint uses."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        self._deactivate_admin()

        r = auth_client.post(
            "/api/auth/change-password",
            json={
                "current_password": _KNOWN_PW,
                "new_password": "newpassword-42-long",
            },
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "User not found"

    def test_request_user_rejects_inactive_user(self, auth_client):
        """Parity coverage for ``_request_user``. If a future refactor
        drops the active-check from this helper, the audit v26-01
        parity bug would come back — pin it with an assertion."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        self._deactivate_admin()

        r = auth_client.get("/api/bots")
        assert r.status_code == 401
        assert r.json()["detail"] == "User not found"


class TestDbAnnotationsRoutes:
    """Regression coverage for the /api/db/annotations routes — a past
    report of a 404 on GET turned out to be a 401 from the auth
    middleware, but the routes themselves must stay registered and
    return 200 with a valid session cookie."""

    def test_get_annotations_registered(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/db/annotations?bot_slug=nope&timeframe=1h")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_annotations_without_timeframe(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/db/annotations?bot_slug=nope")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_annotations_missing_bot_slug_is_422_not_404(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/db/annotations")
        # Missing required query param is a validation error, not a
        # missing route — explicitly assert the non-404 so a future
        # route-removal regression trips this test.
        assert r.status_code == 422


class TestWsStateSmoke:
    """Smoke test for /ws/state — the new bot-state push channel.

    We only assert the handshake succeeds with a valid session cookie
    and that we receive the initial summary frame. File-change pushing
    is covered implicitly: connect() unconditionally emits a summary.
    """

    def test_ws_state_accepts_session_cookie(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        with auth_client.websocket_connect("/ws/state") as ws:
            # We should receive at least one frame — either a bot_state
            # snapshot or the trailing summary frame, depending on how
            # many bots are configured in the test environment.
            import json as _json
            raw = ws.receive_text()
            msg = _json.loads(raw)
            assert msg.get("type") in ("bot_state", "summary")


class TestCandlesRange:
    """Smoke tests for /api/candles/{pair}/{timeframe} — the range
    endpoint backing the client-side backtester. Only input validation
    is asserted here; the success path would require hitting the live
    Bitget exchange which is out of scope for a unit test."""

    def test_candles_route_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/candles/{pair}/{timeframe}"
        ]
        assert len(routes) == 1, "/api/candles must be registered exactly once"

    def test_invalid_timeframe_is_400(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/99h",
            params={"start": "2025-01-01", "end": "2025-01-02"},
        )
        assert r.status_code == 400
        assert "timeframe" in r.json()["detail"]

    def test_start_after_end_is_400(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/1h",
            params={"start": "2025-02-01", "end": "2025-01-01"},
        )
        assert r.status_code == 400

    def test_malformed_timestamp_is_400(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/1h",
            params={"start": "not-a-date", "end": "2025-01-02"},
        )
        assert r.status_code == 400


class TestCandlePagination:
    """Direct unit test for _fetch_ohlcv_range — walks the page cursor
    until two empty pages in a row or end_ms is reached. We mock the
    per-page retry helper so we never touch Bitget, and assert that
    the stitched output has no gaps across page boundaries."""

    def _page(self, since_ms, count, tf_ms, step=1):
        """Build a contiguous page of ccxt-shape candles starting at since_ms."""
        return [
            [since_ms + i * tf_ms, 100.0, 101.0, 99.0, 100.5, 10.0]
            for i in range(count * step)
            if i % step == 0
        ][:count]

    def test_pagination_no_gaps_across_pages(self, monkeypatch):
        """Three 200-candle pages stitched together cover the full range
        without dropping any candles between page boundaries."""
        import asyncio
        from web import app as webapp

        tf_ms = webapp._TF_SECONDS["1h"] * 1000
        start_ms = 1_700_000_000_000
        # 3 pages of 200 bars = 600 bars total
        pages = [
            self._page(start_ms + i * 200 * tf_ms, 200, tf_ms)
            for i in range(3)
        ]
        end_ms = start_ms + 600 * tf_ms

        call_log = []

        async def fake_page(client, symbol, timeframe, since_ms_arg, limit):
            call_log.append(since_ms_arg)
            for page in pages:
                if page and page[0][0] == since_ms_arg:
                    return page
            return []

        monkeypatch.setattr(webapp, "_fetch_ohlcv_page_with_retry", fake_page)

        bars = asyncio.run(webapp._fetch_ohlcv_range(
            client=object(), symbol="BTC/USD", timeframe="1h",
            start_ms=start_ms, end_ms=end_ms,
        ))

        # 600 contiguous bars, no duplicates, no gaps larger than tf_ms
        assert len(bars) == 600
        for i in range(1, len(bars)):
            delta_ms = bars[i][0] - bars[i - 1][0]
            assert delta_ms == tf_ms, (
                f"Gap at index {i}: {delta_ms}ms vs expected {tf_ms}ms"
            )
        # Cursor walked forward page-by-page (each since > previous)
        assert call_log == sorted(set(call_log))
        assert call_log[0] == start_ms

    def test_pagination_stops_on_two_empty_pages(self, monkeypatch):
        """Two consecutive empty pages terminate the walk, so we don't
        spin forever on a timeframe whose history ends mid-range."""
        import asyncio
        from web import app as webapp

        tf_ms = webapp._TF_SECONDS["1h"] * 1000
        start_ms = 1_700_000_000_000
        end_ms = start_ms + 10_000 * tf_ms  # Asking for a huge range

        empties = [0]

        async def fake_page(client, symbol, timeframe, since_ms_arg, limit):
            empties[0] += 1
            return []  # Always empty

        monkeypatch.setattr(webapp, "_fetch_ohlcv_page_with_retry", fake_page)

        bars = asyncio.run(webapp._fetch_ohlcv_range(
            client=object(), symbol="BTC/USD", timeframe="1h",
            start_ms=start_ms, end_ms=end_ms,
        ))
        assert bars == []
        # Should have given up after exactly 2 empty pages (not spun to max_pages)
        assert empties[0] == 2


# ── Deal management endpoints ─────────────────────────────────────────────────

class TestDealEndpoints:
    """PATCH and DELETE /api/bots/{slug}/deals/{deal_id} — validates deal_id
    format and writes sentinel files for valid requests."""

    def test_patch_invalid_deal_id_is_422(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        # Post-collision-fix format is YYYYMMDDHHMM-RRRR. Legacy
        # PAPER-NNNN IDs now also fail validation (intentional — old
        # IDs should never reappear from operator tooling).
        for bad_id in [
            "evil-inject", "paper-001", "202604191342-0001;rm",
            "X" * 20, "PAPER-0001",
        ]:
            r = auth_client.patch(
                f"/api/bots/test/deals/{bad_id}",
                json={"tp_enabled": True},
            )
            assert r.status_code == 422, f"Expected 422 for deal_id={bad_id!r}, got {r.status_code}"

    def test_patch_valid_deal_id_is_200(self, auth_client):
        from core import paths
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.patch(
            "/api/bots/test/deals/202604191342-0001",
            json={"tp_enabled": True, "tp_target_pct": 3.5},
        )
        assert r.status_code == 200
        assert r.json().get("ok") is True
        # Clean up sentinel — Phase-2 layout puts it under logs/<user>/.
        sentinel = paths.user_logs_dir(1) / "test.deal_edit_202604191342-0001"
        if sentinel.exists():
            sentinel.unlink()

    def test_delete_cancel_valid(self, auth_client):
        from core import paths
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete(
            "/api/bots/test/deals/202604191342-0002",
            params={"action": "cancel"},
        )
        assert r.status_code == 200
        assert r.json().get("action") == "cancel"
        sentinel = paths.user_logs_dir(1) / "test.deal_cancel_202604191342-0002"
        if sentinel.exists():
            sentinel.unlink()

    def test_delete_close_valid(self, auth_client):
        from core import paths
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete(
            "/api/bots/test/deals/202604191342-0003",
            params={"action": "close"},
        )
        assert r.status_code == 200
        assert r.json().get("action") == "close"
        sentinel = paths.user_logs_dir(1) / "test.deal_close_202604191342-0003"
        if sentinel.exists():
            sentinel.unlink()

    def test_delete_invalid_deal_id_is_422(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete(
            "/api/bots/test/deals/evil-inject",
            params={"action": "close"},
        )
        assert r.status_code == 422

    def test_delete_invalid_action_is_400(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete(
            "/api/bots/test/deals/202604191342-0001",
            params={"action": "nuke"},
        )
        assert r.status_code == 400


# ── Annotation POST endpoint ─────────────────────────────────────────────────

class TestAnnotationPost:
    def test_save_annotation_valid(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post("/api/db/annotations", json={
            "bot_slug": "test", "type": "hline", "timeframe": "1h",
            "x1": 1700000000, "y1": 80000.0,
        })
        assert r.status_code == 200
        assert "id" in r.json()

    def test_save_annotation_missing_slug_is_422(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post("/api/db/annotations", json={
            "type": "hline", "timeframe": "1h", "x1": 1700000000,
        })
        assert r.status_code == 422

    def test_save_annotation_x1_out_of_range(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post("/api/db/annotations", json={
            "bot_slug": "test", "type": "hline", "timeframe": "1h",
            "x1": 3_000_000_000, "y1": 80000.0,
        })
        assert r.status_code == 422


# ── Delete backtest runs endpoint ─────────────────────────────────────────────

class TestDeleteBacktestRuns:
    def test_delete_valid_run(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        # Create a run first
        r = auth_client.post("/api/backtest/save", json={
            "slug": "test_del", "name": "Del Test",
            "params": {"start_date": "2025-01-01", "end_date": "2025-06-01",
                       "timeframe": "1h", "initial_balance_btc": 0.1},
            "summary": {"total_pnl_btc": 0.001, "total_deals": 5},
        })
        assert r.status_code == 200
        run_id = r.json()["id"]
        # Delete it
        r2 = auth_client.delete(f"/api/backtest/runs/{run_id}")
        assert r2.status_code == 200
        assert r2.json().get("ok") is True

    def test_delete_nonexistent_run_is_404(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete("/api/backtest/runs/999999")
        assert r.status_code == 404

    def test_delete_without_auth_is_401(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.delete("/api/backtest/runs/1")
        assert r.status_code == 401


# ── Start dry-run (Phase-1 live launcher) ─────────────────────────────────────

class TestStartDryRun:
    """Portal-side launcher for live-mode bots under Phase 1 dry-run.

    These tests monkeypatch subprocess.Popen so no real bot is ever
    spawned; the assertions target the command shape + env + the
    mode-guard (paper bots must be refused)."""

    def _live_payload(self) -> dict:
        """Use the default name from _make_payload so the resulting slug
        lines up with _TEST_SLUG — the autouse cleanup fixture only
        knows that single path."""
        p = _make_payload()
        p["bot"]["mode"] = "live"
        return p

    def test_route_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/bots/{slug}/start-dry-run"
            and "POST" in getattr(r, "methods", set())
        ]
        assert len(routes) == 1, (
            "POST /api/bots/{slug}/start-dry-run must be registered exactly once"
        )

    def test_unauthenticated_is_401(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.post("/api/bots/whatever/start-dry-run")
        assert r.status_code == 401

    def test_paper_mode_bot_is_refused(self, auth_client, monkeypatch):
        """start_bot_dry_run must return ok=False for a paper-mode bot
        rather than letting main_live.py's hard-mode check swallow it
        silently as an exit-1 subprocess."""
        # Refuse at the helper level — nothing should ever spawn.
        called = {"popen": 0}

        def _fake_popen(*a, **kw):
            called["popen"] += 1
            raise AssertionError("Popen must not run for paper-mode bots")
        monkeypatch.setattr("web.app.subprocess.Popen", _fake_popen)

        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        # Seed a paper-mode YAML via the normal create endpoint.
        create = auth_client.post("/api/bots", json=_make_payload())
        assert create.status_code == 200
        try:
            r = auth_client.post(f"/api/bots/{_TEST_SLUG}/start-dry-run")
            assert r.status_code == 200
            body = r.json()
            assert body.get("ok") is False
            assert "live-mode" in body.get("error", "")
            assert called["popen"] == 0
        finally:
            # Clean up — the autouse fixture removes the YAML, but we
            # also want registry state consistent for the next test.
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")

    def test_live_mode_bot_spawns_main_live_with_dry_run(
        self, auth_client, monkeypatch,
    ):
        """Happy path: live YAML + Popen captured. Verifies argv shape
        (main_live.py --bot <slug> --dry-run) and that DRY_RUN=1 is in
        the child env so the confirmation prompt never blocks a
        non-TTY portal subprocess."""
        captured: dict = {}

        class _FakeProc:
            pid = 4242

        def _fake_popen(cmd, *a, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env", {})
            captured["start_new_session"] = kw.get("start_new_session")
            # Drop a fake PID file so the post-spawn wait loop exits.
            # Phase-2: pid file lives under logs/<user>/pids/.
            from core import paths as _paths
            pid_path = _paths.bot_pid_path(_TEST_USER_ID, _TEST_SLUG)
            pid_path.write_text(str(_FakeProc.pid))
            return _FakeProc()

        monkeypatch.setattr("web.app.subprocess.Popen", _fake_popen)

        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        assert auth_client.post(
            "/api/bots", json=self._live_payload(),
        ).status_code == 200
        try:
            r = auth_client.post(f"/api/bots/{_TEST_SLUG}/start-dry-run")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("ok") is True, body
            assert "DRY-RUN" in body.get("message", "")

            cmd = captured["cmd"]
            assert cmd[1].endswith("main_live.py"), cmd
            assert "--bot" in cmd and _TEST_SLUG in cmd
            assert "--dry-run" in cmd
            assert captured["env"].get("DRY_RUN") == "1"
            assert captured["start_new_session"] is True
        finally:
            # Remove fake PID + YAML so subsequent tests see a clean slate.
            from core import paths as _paths
            pid_file = _paths.bot_pid_path(_TEST_USER_ID, _TEST_SLUG)
            if pid_file.exists():
                pid_file.unlink()
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")

    def test_unknown_slug_is_refused(self, auth_client, monkeypatch):
        def _fake_popen(*a, **kw):
            raise AssertionError("Popen must not run for unknown bot")
        monkeypatch.setattr("web.app.subprocess.Popen", _fake_popen)

        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post("/api/bots/does_not_exist_anywhere/start-dry-run")
        # Helper returns {"ok": False, ...} rather than raising — the
        # endpoint surfaces that as 200 with the ok flag.
        assert r.status_code == 200
        assert r.json().get("ok") is False

    def test_invalid_slug_shape_is_400(self, auth_client, monkeypatch):
        """Slug with shell-metacharacters (or anything outside the
        [A-Za-z0-9_-]+ regex) must fail fast with 400, never reaching
        registry.get or subprocess.Popen. URL-encoded slashes get
        normalised by httpx/Starlette into a different route, so the
        realistic attack payload is something the regex rejects but
        FastAPI keeps as the {slug} path param."""
        def _fake_popen(*a, **kw):
            raise AssertionError("Popen must not run for invalid slug")
        monkeypatch.setattr("web.app.subprocess.Popen", _fake_popen)

        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        # Semicolon + dots aren't in _BOT_SLUG_RE but are kept intact
        # as path params by FastAPI's URL router.
        r = auth_client.post(
            "/api/bots/bot;rm.-rf/start-dry-run",
        )
        assert r.status_code == 400, r.text
        assert "Invalid slug" in r.json().get("detail", "")

    def test_helper_rejects_invalid_slug_directly(self):
        """start_bot_dry_run helper itself rejects bad slugs — belt-
        and-braces so non-route callers (scripts, internal tooling)
        can't accidentally invoke subprocess.Popen with a traversal."""
        import asyncio
        from web.app import start_bot_dry_run
        result = asyncio.run(start_bot_dry_run(1, "../../etc/passwd"))
        assert result["ok"] is False
        assert "Invalid bot slug" in result.get("error", "")


# ── API contract: bot.mode must mirror the YAML ───────────────────────────────

class TestApiBotsReturnsMode:
    """Pins the GET /api/bots contract: the authoritative mode lives in
    the YAML, not in logs/<slug>.state.json. A live-mode bot that has
    never started MUST still surface as mode=live so the overview UI
    can render the orange "Start dry-run" button instead of the green
    paper one. Regression test for the bug where _default_state()
    hardcoded mode=paper for never-started bots."""

    def test_live_yaml_without_state_returns_mode_live(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        # Seed a live-mode YAML via the create endpoint. No bot
        # subprocess runs, so logs/<slug>.state.json never appears —
        # this is the exact scenario the bug report hit.
        payload = _make_payload()
        payload["bot"]["mode"] = "live"
        assert auth_client.post("/api/bots", json=payload).status_code == 200

        # Defensive: make sure no stale state file is laying around
        # from a previous test run (autouse fixture removes the YAML
        # but not the state file).
        state_file = _TEST_STATE
        if os.path.exists(state_file):
            os.remove(state_file)

        try:
            r = auth_client.get("/api/bots")
            assert r.status_code == 200
            bots = {b["slug"]: b for b in r.json()["bots"]}
            assert _TEST_SLUG in bots, f"bot not listed: {list(bots)}"
            assert bots[_TEST_SLUG]["mode"] == "live", (
                f"mode must reflect YAML, got {bots[_TEST_SLUG]['mode']!r}"
            )
        finally:
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")

    def test_paper_yaml_returns_mode_paper(self, auth_client):
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        assert auth_client.post(
            "/api/bots", json=_make_payload(),
        ).status_code == 200

        try:
            r = auth_client.get("/api/bots")
            assert r.status_code == 200
            bots = {b["slug"]: b for b in r.json()["bots"]}
            assert bots[_TEST_SLUG]["mode"] == "paper"
        finally:
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")

    def test_yaml_mode_wins_over_state_file(self, auth_client):
        """If the YAML was edited from paper to live but the engine has
        not yet re-written state.json, the UI must already see 'live'."""
        import json as _json
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        # Create a live YAML.
        payload = _make_payload()
        payload["bot"]["mode"] = "live"
        assert auth_client.post("/api/bots", json=payload).status_code == 200

        # Plant a stale state.json that still says mode=paper.
        os.makedirs(os.path.dirname(_TEST_STATE), exist_ok=True)
        state_file = _TEST_STATE
        with open(state_file, "w") as fh:
            _json.dump({
                "bot_name": "Pytest Route Check",
                "mode": "paper",
                "exchange": "bitget",
                "pair": "BTC/USD",
            }, fh)

        try:
            r = auth_client.get("/api/bots")
            assert r.status_code == 200
            bots = {b["slug"]: b for b in r.json()["bots"]}
            assert bots[_TEST_SLUG]["mode"] == "live", (
                "YAML mode must win over lagging state.json"
            )
        finally:
            if os.path.exists(state_file):
                os.remove(state_file)
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")


# ── /api/bots/validate-config (advisory warnings, no enforcement) ─────────────

class TestValidateConfigEndpoint:
    """Pins the advisory-warnings contract that replaced the
    LiveEngine preflight caps. Every test here verifies that the
    endpoint RETURNS information rather than blocking — a user
    saving a dangerous config still gets a 200 with warnings, the
    POST /api/bots path + start path keep accepting it."""

    def test_route_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/bots/validate-config"
            and "POST" in getattr(r, "methods", set())
        ]
        assert len(routes) == 1

    def test_unauthenticated_is_401(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.post("/api/bots/validate-config", json=_make_payload())
        assert r.status_code == 401

    def test_explosive_dca_emits_high_warnings(self, auth_client):
        """mult=2.0 × 10 orders → worst 512× base, cumulative 1023×
        base. Pre-v25 this was refused at LiveEngine.__init__; now it
        must parse successfully AND surface at least one high-level
        warning so the wizard flags the risk."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        payload = _make_payload()
        payload["bot"]["mode"] = "live"
        payload["bot"]["dca"]["base_order_size"] = 0.001
        payload["bot"]["dca"]["multiplier"] = 2.0
        payload["bot"]["dca"]["max_orders"] = 10

        r = auth_client.post("/api/bots/validate-config", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        warnings = data["warnings"]
        assert any(w["level"] == "high" for w in warnings), warnings
        s = data["summary"]
        assert s["mode"] == "live"
        assert s["worst_case_multiple"] > 50
        assert s["cumulative_multiple"] > 150

    def test_conservative_config_has_no_high_warnings(self, auth_client):
        """Default _make_payload (mult=1.5, max_orders=5) → worst 5.06×
        base, cumulative 7.59× base. Both well under the high-warning
        thresholds (50× / 150×) so no high flag should fire. Also
        under the medium thresholds (20× / 100×) so cumulative is
        clean; the endpoint may still return an empty warnings list."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        r = auth_client.post("/api/bots/validate-config", json=_make_payload())
        assert r.status_code == 200, r.text
        data = r.json()
        assert not any(w["level"] == "high" for w in data["warnings"]), (
            data["warnings"]
        )
        s = data["summary"]
        assert s["worst_case_multiple"] == pytest.approx(1.5 ** 4)
        assert s["cumulative_multiple"] == pytest.approx(
            sum(1.5 ** i for i in range(5))
        )

    def test_live_mode_large_base_warns(self, auth_client):
        """Live bots with base > 0.001 BTC pick up a specific warning
        pointing at dca.base_order_size."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        payload = _make_payload()
        payload["bot"]["mode"] = "live"
        payload["bot"]["dca"]["base_order_size"] = 0.01

        r = auth_client.post("/api/bots/validate-config", json=payload)
        assert r.status_code == 200
        fields = [w["field"] for w in r.json()["warnings"]]
        assert "dca.base_order_size" in fields

    def test_malformed_config_is_400(self, auth_client):
        """Missing required field → BotConfig validation → 400."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        # name is required; drop it.
        bad = {"mode": "paper", "exchange": "bitget", "pair": "BTC/USD"}
        r = auth_client.post("/api/bots/validate-config", json=bad)
        assert r.status_code == 400
        assert "Invalid config" in r.json().get("detail", "")

    def test_endpoint_is_side_effect_free(self, auth_client):
        """validate-config must NOT write any YAML, touch the registry,
        or create state files. Repeat calls with a paper config + verify
        no file lands in config/bots/."""
        import pathlib
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        payload = _make_payload()
        for _ in range(3):
            r = auth_client.post("/api/bots/validate-config", json=payload)
            assert r.status_code == 200
        # Autouse fixture handles the cleanup, but assert that nothing
        # was persisted in the first place.
        assert not pathlib.Path(_TEST_YAML).exists()

    def test_rejects_oversized_body(self, auth_client):
        """Content-Length > MAX_CONFIG_BODY_BYTES → 413, body never
        parsed. Guards against authenticated DoS via huge JSON."""
        from web.routes.bots import MAX_CONFIG_BODY_BYTES
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        # Pad a valid payload past the cap. Using a giant filler key
        # keeps the JSON syntactically parseable — the 413 must fire
        # BEFORE the JSON parser runs.
        payload = _make_payload()
        payload["bot"]["_bloat"] = "x" * (MAX_CONFIG_BODY_BYTES + 1000)
        r = auth_client.post("/api/bots/validate-config", json=payload)
        assert r.status_code == 413
        assert "too large" in r.json().get("detail", "").lower()

    def test_rejects_invalid_content_length(self, auth_client):
        """Malformed Content-Length header → 400, not 500."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        # httpx auto-populates Content-Length. Override it to garbage
        # so the handler's int(header) raises and maps to 400.
        r = auth_client.post(
            "/api/bots/validate-config",
            content=b'{"name": "x"}',
            headers={
                "Content-Type": "application/json",
                "Content-Length": "not-a-number",
            },
        )
        assert r.status_code == 400
        assert "Content-Length" in r.json().get("detail", "")

    def test_chunked_body_within_cap_is_accepted(self, auth_client):
        """Clients without Content-Length (chunked transfer) still work
        as long as the streamed body stays under the cap. The handler
        reads lazily and only aborts once the limit is crossed."""
        import json as _json
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        payload_bytes = _json.dumps(_make_payload()).encode("utf-8")

        def _stream():
            yield payload_bytes

        r = auth_client.post(
            "/api/bots/validate-config",
            content=_stream(),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        assert r.status_code == 200, r.text
        assert "warnings" in r.json()

    def test_chunked_body_over_cap_rejected(self, auth_client):
        """Chunked client streams > cap → 413 during streaming, no
        OOM. Pin the streaming-path guard specifically."""
        from web.routes.bots import MAX_CONFIG_BODY_BYTES
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        # Emit chunks totalling > cap.
        bloat = b"x" * (MAX_CONFIG_BODY_BYTES // 2 + 100)

        def _stream():
            yield b'{"bot": {"filler": "'
            yield bloat
            yield bloat
            yield b'"}}'

        r = auth_client.post(
            "/api/bots/validate-config",
            content=_stream(),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        assert r.status_code == 413


# ── Favicon ──────────────────────────────────────────────────────────────────


class TestFavicon:
    """Regression guard: GET /favicon.ico used to 404 because no route
    was registered even though AuthMiddleware whitelisted the path.
    Browsers hit it on every page-load — a persistent 404 in the
    devtools console is noisy and masks real errors."""

    def test_favicon_root_returns_200_unauthenticated(self):
        """Browsers request /favicon.ico BEFORE the session cookie
        is set — the route must succeed without any auth. Serves the
        multi-resolution ICO shipped in web/static/."""
        r = CLIENT.get("/favicon.ico")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")
        # Real ICO: 5-6 KB. Assert "non-empty binary" to catch a
        # stripped or symlinked-to-empty file.
        assert len(r.content) > 100
        # ICO magic bytes: 00 00 01 00.
        assert r.content[:4] == b"\x00\x00\x01\x00"

    def test_favicon_svg_served_via_static_mount(self):
        """The /static mount picks up the SVG + apple-touch PNG that
        index.html references via <link rel="icon">. Smoke-test all
        three paths so a missing asset surfaces in CI instead of as
        a console error."""
        for path, prefix in (
            ("/static/favicon.svg", b"<"),            # XML/SVG
            ("/static/apple-touch-icon.png", b"\x89PNG"),
        ):
            r = CLIENT.get(path)
            assert r.status_code == 200, f"{path} → {r.status_code}"
            assert r.content.startswith(prefix), (
                f"{path} served something other than the expected asset"
            )

    def test_index_html_references_favicon(self):
        """Protects against someone stripping the <link rel="icon">
        tags from index.html during a rewrite — the route would still
        work but the SVG / apple-touch paths wouldn't be discovered."""
        r = CLIENT.get("/")
        assert r.status_code == 200
        body = r.text
        assert 'rel="icon"' in body
        assert "/favicon.ico" in body
        assert "/static/favicon.svg" in body
        assert "/static/apple-touch-icon.png" in body


# ── Body-size cap on POST /api/bots + PUT /api/bots/{slug}/config ───────────


class TestBotEndpointBodySizeCap:
    """Audit v25 #5 — the validate-config body cap existed since
    v23, but POST /api/bots (create) and PUT /api/bots/{slug}/config
    (update) still accepted arbitrary JSON via FastAPI's auto-parse.
    Both endpoints now route through ``_read_body_with_cap`` with
    the same 64 KB limit. These tests pin both the refusal paths
    (Content-Length header + chunked streaming) and the happy-path
    so a future refactor can't silently re-introduce the DoS surface.
    """

    @pytest.fixture(autouse=True)
    def _sweep_bodycap_bots(self):
        """Bot YAMLs created by these tests don't match the module-
        level ``_cleanup_yaml`` pattern (which targets one specific
        slug). Sweep every ``pytest_bodycap_*`` YAML + sidecars
        before and after each test so state never leaks between
        runs or into production config."""
        import glob as _glob
        import pathlib as _pl

        def _sweep():
            for pattern in (
                f"config/bots/{_TEST_USER_ID}/pytest_bodycap_*.yaml",
                f"logs/{_TEST_USER_ID}/pytest_bodycap_*.state.json",
                f"logs/{_TEST_USER_ID}/pids/pytest_bodycap_*.pid",
                f"logs/{_TEST_USER_ID}/pytest_bodycap_*.log",
            ):
                for p in _glob.glob(pattern):
                    try:
                        _pl.Path(p).unlink()
                    except OSError:
                        pass
        _sweep()
        yield
        _sweep()

    # ── POST /api/bots ──────────────────────────────────────────────

    def test_post_bots_rejects_oversized_content_length(self, auth_client):
        """Padded body above MAX_CONFIG_BODY_BYTES → 413 before any
        JSON parsing touches the payload."""
        from web.routes.bots import MAX_CONFIG_BODY_BYTES
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        payload = _make_payload(name="Pytest Bodycap Post A")
        payload["bot"]["_bloat"] = "x" * (MAX_CONFIG_BODY_BYTES + 1000)

        r = auth_client.post("/api/bots", json=payload)
        assert r.status_code == 413
        assert "too large" in r.json().get("detail", "").lower()

    def test_post_bots_rejects_streamed_oversized_body(self, auth_client):
        """Chunked client (no Content-Length) still bounded — the
        streaming path must abort mid-read."""
        from web.routes.bots import MAX_CONFIG_BODY_BYTES
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        bloat = b"x" * (MAX_CONFIG_BODY_BYTES // 2 + 100)

        def _stream():
            yield b'{"bot": {"filler": "'
            yield bloat
            yield bloat
            yield b'"}}'

        r = auth_client.post(
            "/api/bots",
            content=_stream(),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        assert r.status_code == 413

    def test_post_bots_happy_path_still_creates(self, auth_client):
        """Sanity guard — the cap refactor must not break normal
        create flow. Without this, a bug that rejected every POST
        would still pass the refusal tests above."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/bots",
            json=_make_payload(name="Pytest Bodycap Happy Path"),
        )
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

    def test_post_bots_rejects_invalid_content_length(self, auth_client):
        """Malformed Content-Length → 400, same contract as
        validate-config so clients get one consistent error path
        across the three config-ingesting endpoints."""
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/bots",
            content=b'{"bot": {"name": "x"}}',
            headers={
                "Content-Type": "application/json",
                "Content-Length": "not-a-number",
            },
        )
        assert r.status_code == 400
        assert "Content-Length" in r.json().get("detail", "")

    # ── PUT /api/bots/{slug}/config ─────────────────────────────────

    def test_put_config_rejects_oversized_content_length(self, auth_client):
        """Update path: same 413 refusal on an over-cap Content-Length."""
        from web.routes.bots import MAX_CONFIG_BODY_BYTES
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        # Create a bot first so the PUT has a target — otherwise the
        # 404 "Bot not found" check would fire before the body-cap
        # helper runs (handler-order detail we must not mask).
        create = auth_client.post(
            "/api/bots",
            json=_make_payload(name="Pytest Bodycap Put"),
        )
        assert create.status_code == 200
        slug = create.json()["slug"]

        payload = _make_payload(name="Pytest Bodycap Put")
        payload["bot"]["_bloat"] = "x" * (MAX_CONFIG_BODY_BYTES + 1000)

        r = auth_client.put(f"/api/bots/{slug}/config", json=payload)
        assert r.status_code == 413
        assert "too large" in r.json().get("detail", "").lower()

    def test_put_config_rejects_streamed_oversized_body(self, auth_client):
        """Update path: chunked variant."""
        from web.routes.bots import MAX_CONFIG_BODY_BYTES
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)

        create = auth_client.post(
            "/api/bots",
            json=_make_payload(name="Pytest Bodycap Put Stream"),
        )
        assert create.status_code == 200
        slug = create.json()["slug"]

        bloat = b"x" * (MAX_CONFIG_BODY_BYTES // 2 + 100)

        def _stream():
            yield b'{"bot": {"filler": "'
            yield bloat
            yield bloat
            yield b'"}}'

        r = auth_client.put(
            f"/api/bots/{slug}/config",
            content=_stream(),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        assert r.status_code == 413


class TestBotImportExportDuplicate:
    """Export → YAML download with metadata; duplicate → server-side
    copy; import → YAML body validated via Pydantic before touching
    disk. Every path in all three endpoints has a happy-path case plus
    the realistic failure modes (404/409/400/413).
    """

    @pytest.fixture(autouse=True)
    def _sweep_iex_bots(self):
        """Sweep all pytest_iex_* YAMLs + sidecars before and after
        each test. Matches the isolation pattern used by
        TestBotEndpointBodySizeCap.

        Also resets the slowapi rate-limiter bucket — these endpoints
        have 10/min caps and the full suite accumulates POST traffic
        that would otherwise push this class past 429 when run after
        TestPostBotsSmoke + TestBotEndpointBodySizeCap.
        """
        import glob as _glob
        import pathlib as _pl

        def _sweep():
            for pattern in (
                f"config/bots/{_TEST_USER_ID}/pytest_iex_*.yaml",
                f"logs/{_TEST_USER_ID}/pytest_iex_*.state.json",
                f"logs/{_TEST_USER_ID}/pids/pytest_iex_*.pid",
                f"logs/{_TEST_USER_ID}/pytest_iex_*.log",
            ):
                for p in _glob.glob(pattern):
                    try:
                        _pl.Path(p).unlink()
                    except OSError:
                        pass
        _sweep()
        from web.app import limiter as _limiter
        try: _limiter.reset()
        except Exception: pass
        yield
        _sweep()

    def _create_source_bot(self, slug: str = "pytest_iex_src") -> str:
        """Create a bot to act as the source for export/duplicate
        tests. Returns the slug."""
        payload = _make_payload(name=slug.replace("_", " ").title())
        r = CLIENT.post("/api/bots", json=payload, headers=JSON)
        assert r.status_code == 200, r.text
        return r.json()["slug"]

    # ── GET /api/bots/{slug}/export ─────────────────────────────────

    def test_export_returns_yaml_with_header_metadata(self):
        slug = self._create_source_bot()
        r = CLIENT.get(f"/api/bots/{slug}/export", headers=AUTH)
        assert r.status_code == 200
        body = r.text
        assert body.startswith("# Reverto bot export\n")
        assert f"# Original slug: {slug}" in body
        assert "# Exported:" in body
        assert "# Reverto version:" in body
        # Content-Disposition triggers the browser download with a
        # filename matching the slug.
        cd = r.headers.get("content-disposition", "")
        assert f'filename="{slug}.yaml"' in cd
        # Everything below the header block must still parse as valid
        # YAML — a broken export would be silently unusable otherwise.
        import yaml as _yaml
        parsed = _yaml.safe_load(body)
        assert isinstance(parsed, dict)
        assert "bot" in parsed

    def test_export_404_on_unknown_bot(self):
        r = CLIENT.get("/api/bots/pytest_iex_does_not_exist/export", headers=AUTH)
        assert r.status_code == 404

    # ── POST /api/bots/{slug}/duplicate ─────────────────────────────

    def test_duplicate_creates_copy_with_new_slug(self):
        src = self._create_source_bot()
        r = CLIENT.post(
            f"/api/bots/{src}/duplicate",
            json={"new_slug": "pytest_iex_dup"},
            headers=JSON,
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "slug": "pytest_iex_dup"}
        assert os.path.exists(
            f"config/bots/{_TEST_USER_ID}/pytest_iex_dup.yaml"
        )
        # The duplicate must not carry the source slug's state/history.
        # Because we only copy the YAML, there is simply no state.json
        # for the new slug — the engine rebuilds it on first start.
        assert not os.path.exists(
            f"logs/{_TEST_USER_ID}/pytest_iex_dup.state.json"
        )

    def test_duplicate_409_on_existing_target_slug(self):
        src = self._create_source_bot()
        # Pre-create target to force the conflict.
        self._create_source_bot(slug="pytest_iex_dup_conflict")
        # _make_payload's slugify turns the name we used into a slug —
        # the source + target below are distinct YAML paths.
        r = CLIENT.post(
            f"/api/bots/{src}/duplicate",
            json={"new_slug": "pytest_iex_dup_conflict"},
            headers=JSON,
        )
        assert r.status_code == 409
        assert "already exists" in r.json().get("detail", "")

    def test_duplicate_400_on_invalid_slug_shape(self):
        src = self._create_source_bot()
        r = CLIENT.post(
            f"/api/bots/{src}/duplicate",
            json={"new_slug": "has spaces"},
            headers=JSON,
        )
        assert r.status_code == 400
        assert "Invalid slug" in r.json().get("detail", "")

    def test_duplicate_404_on_unknown_source(self):
        r = CLIENT.post(
            "/api/bots/pytest_iex_nosrc/duplicate",
            json={"new_slug": "pytest_iex_newdup"},
            headers=JSON,
        )
        assert r.status_code == 404

    # ── POST /api/bots/import ───────────────────────────────────────

    def _valid_import_yaml(self) -> str:
        """Realistic YAML body mirroring the export format — a bot
        block nested under {"bot": ...}."""
        import yaml as _yaml
        payload = _make_payload(name="Imported Bot")
        return _yaml.safe_dump(payload, sort_keys=False)

    def test_import_creates_bot_from_valid_yaml(self):
        yaml_body = self._valid_import_yaml()
        r = CLIENT.post(
            "/api/bots/import?slug=pytest_iex_imp",
            content=yaml_body,
            headers={**AUTH, "Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "slug": "pytest_iex_imp"}
        assert os.path.exists(
            f"config/bots/{_TEST_USER_ID}/pytest_iex_imp.yaml"
        )

    def test_import_400_on_invalid_yaml(self):
        r = CLIENT.post(
            "/api/bots/import?slug=pytest_iex_bad",
            content=b"name: [ unterminated",
            headers={**AUTH, "Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 400
        assert "Invalid YAML" in r.json().get("detail", "")

    def test_import_400_on_schema_validation_failure(self):
        # Missing required sub-blocks — BotConfig rejects it.
        r = CLIENT.post(
            "/api/bots/import?slug=pytest_iex_bad",
            content=b"bot:\n  name: x\n  mode: paper\n  exchange: bitget\n",
            headers={**AUTH, "Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 400
        assert "Schema validation failed" in r.json().get("detail", "")

    def test_import_409_on_existing_target_slug(self):
        self._create_source_bot(slug="pytest_iex_existing")
        r = CLIENT.post(
            "/api/bots/import?slug=pytest_iex_existing",
            content=self._valid_import_yaml(),
            headers={**AUTH, "Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 409

    def test_import_400_on_invalid_slug_param(self):
        r = CLIENT.post(
            "/api/bots/import?slug=not%20valid",
            content=self._valid_import_yaml(),
            headers={**AUTH, "Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 400

    def test_import_body_size_cap_enforced(self, auth_client):
        """Oversized YAML → 413 before any parsing. Uses auth_client
        to exercise the same session-cookie path that real browser
        uploads take."""
        from web.routes.bots import MAX_CONFIG_BODY_BYTES
        token = _admin_cookie()
        auth_client.cookies.set("reverto_session", token)
        bloat = b"x" * (MAX_CONFIG_BODY_BYTES + 1000)
        r = auth_client.post(
            "/api/bots/import?slug=pytest_iex_big",
            content=bloat,
            headers={"Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 413
