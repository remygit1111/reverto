# tests/test_web_routes.py
# Smoke tests voor de web portal routes. Vangt regressies waarbij POST
# en GET op hetzelfde pad (/api/bots) per ongeluk conflicteren of een
# route niet geregistreerd is.
#
# Bijzonder belangrijk voor /api/bots: GET (lijst) en POST (create)
# leven op hetzelfde pad en moeten beide beschikbaar zijn.

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from web.app import app

CLIENT = TestClient(app)
AUTH = {"X-API-Key": "testkey-for-pytest"}
JSON = {**AUTH, "Content-Type": "application/json"}

# Use a slug that could never collide with a real bot
_TEST_SLUG = "pytest_route_check"
_TEST_YAML = f"config/bots/{_TEST_SLUG}.yaml"


@pytest.fixture(autouse=True)
def _cleanup_yaml():
    """Ensure the test bot YAML is gone before and after every test."""
    if os.path.exists(_TEST_YAML):
        os.remove(_TEST_YAML)
    yield
    if os.path.exists(_TEST_YAML):
        os.remove(_TEST_YAML)
    # credentials files created by the auth path during tests. .auth.json
    # is left in place because the bootstrap runs once at module import —
    # removing it would not regenerate it, and removing .credentials.key
    # would poison decryption on the next run. Only touch credentials.json.
    if os.path.exists("logs/credentials.json"):
        os.remove("logs/credentials.json")


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

    def test_post_without_body_is_422_not_405(self):
        # 422 is correct (missing body). 405 would mean the POST route
        # is not registered at all — exactly the regression we guard against.
        r = CLIENT.post("/api/bots", headers=AUTH)
        assert r.status_code != 405
        assert r.status_code == 422

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

import bcrypt  # noqa: E402

from web import app as webapp  # noqa: E402

_KNOWN_PW = "pytest-known-password-123"


@pytest.fixture
def auth_client():
    """TestClient with the known password provisioned in .auth.json.
    Yields the client and restores the original auth blob afterwards.

    Forces _COOKIE_SECURE=False for the duration of the test because
    TestClient serves over plain http:// and a browser-equivalent
    silently drops Secure cookies on insecure transports — without
    this override the post-login cookie would never reach the next
    request and every authed assertion would 401.
    """
    original = webapp._load_auth()
    webapp._save_auth({
        "username": "admin",
        "password_hash": bcrypt.hashpw(
            _KNOWN_PW.encode("utf-8"), bcrypt.gensalt(rounds=4)
        ).decode("utf-8"),
    })
    prev_secure = webapp._COOKIE_SECURE
    webapp._COOKIE_SECURE = False
    client = TestClient(webapp.app)
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        if original:
            webapp._save_auth(original)


class TestAuth:
    def test_status_unauthenticated_without_cookie(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/auth/status")
        assert r.status_code == 200
        assert r.json() == {"authenticated": False, "username": None}

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
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/bots")
        assert r.status_code == 200
        assert "bots" in r.json()

    def test_change_password_rejects_short(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": _KNOWN_PW, "new_password": "short"},
        )
        assert r.status_code == 400

    def test_change_password_rejects_wrong_current(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": "not-it", "new_password": "longenough1"},
        )
        assert r.status_code == 401


class TestSessionEpochInvalidation:
    """Server-side session invalidation via the .auth.json session_epoch.
    Logout and password change both bump the epoch, so any cookie minted
    under the previous epoch is rejected on the next request."""

    def test_logout_invalidates_existing_cookie(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        # Cookie works before logout.
        assert auth_client.get("/api/bots").status_code == 200
        # Logout bumps the epoch.
        assert auth_client.post("/auth/logout").status_code == 200
        # Same cookie value, but the server now rejects it because the
        # embedded epoch no longer matches the on-disk one.
        auth_client.cookies.set("reverto_session", token)
        assert auth_client.get("/api/bots").status_code == 401

    def test_password_change_invalidates_existing_cookie(self, auth_client):
        token = webapp._create_session_cookie("admin")
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
        # Bump the epoch via logout first.
        auth_client.post("/auth/logout")
        # New login mints a cookie under the new epoch and works.
        r = auth_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        # The TestClient picks up the Set-Cookie automatically and the
        # next request is authenticated.
        assert auth_client.get("/api/bots").status_code == 200


class TestDbAnnotationsRoutes:
    """Regression coverage for the /api/db/annotations routes — a past
    report of a 404 on GET turned out to be a 401 from the auth
    middleware, but the routes themselves must stay registered and
    return 200 with a valid session cookie."""

    def test_get_annotations_registered(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/db/annotations?bot_slug=nope&timeframe=1h")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_annotations_without_timeframe(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/db/annotations?bot_slug=nope")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_annotations_missing_bot_slug_is_422_not_404(self, auth_client):
        token = webapp._create_session_cookie("admin")
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
        token = webapp._create_session_cookie("admin")
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
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/99h",
            params={"start": "2025-01-01", "end": "2025-01-02"},
        )
        assert r.status_code == 400
        assert "timeframe" in r.json()["detail"]

    def test_start_after_end_is_400(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/1h",
            params={"start": "2025-02-01", "end": "2025-01-01"},
        )
        assert r.status_code == 400

    def test_malformed_timestamp_is_400(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/1h",
            params={"start": "not-a-date", "end": "2025-01-02"},
        )
        assert r.status_code == 400
