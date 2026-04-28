"""Phase B PR 3 — TOTP login-flow integration tests.

Anchors three pre-deploy contracts the operator's recovery plan
relies on:

  1. Users without TOTP enabled keep the historical password-only
     login path. Zero behaviour change for them — the response
     shape grows ``requires_totp: false`` but the cookie set is
     identical.
  2. Users with TOTP enabled get a 2-step flow:
       - /auth/login → 200 + ``requires_totp: true`` + pending
         cookie, NO session cookie yet.
       - /auth/login/totp → verifies code, sets session cookie, and
         clears the pending cookie.
  3. The lockout-recovery story (operator manually NULLs
     totp_seed_encrypted via SQL) puts the user back into path
     #1 — re-login is password-only. Pinned by
     ``test_recovery_via_seed_null_returns_to_password_only_path``.

Every endpoint test runs through TestClient against the full FastAPI
app so AuthMiddleware, CSRFMiddleware, rate-limiter and Pydantic
validation all participate.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyotp  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import totp, user_store  # noqa: E402
from web import app as webapp  # noqa: E402


_KNOWN_PW = "pytest-login-totp-pw-654321"


@pytest.fixture(autouse=True)
def _reset_slowapi_between_tests():
    """The /auth/login rate-limit is 5/min and these tests routinely
    fire multiple login attempts inside one test. Without a reset
    between tests the slowapi bucket bleeds across tests in this
    file and we'd see 429s instead of the actual auth-flow status
    codes. Same pattern as test_web_routes.py::TestLoginSecurityHardening."""
    try:
        webapp.limiter.reset()
    except Exception:
        pass
    yield
    try:
        webapp.limiter.reset()
    except Exception:
        pass


@pytest.fixture
def base_client():
    """Plain TestClient with cookie-flag overrides matching the
    auth_client fixture used elsewhere — Secure=False because
    TestClient runs over plain HTTP, SameSite=lax because Python
    3.13 + httpx drops strict cookies on follow-up requests with
    no Origin header (audit v26-22)."""
    user_store.set_password(1, _KNOWN_PW)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def admin_with_totp():
    """Enrol the seeded admin in TOTP and yield (client, secret).

    secret is the plaintext base32 — the test uses it to derive a
    valid current code via ``pyotp.TOTP.now()``. The server only
    ever sees the encrypted form in users.totp_seed_encrypted."""
    user_store.set_password(1, _KNOWN_PW)
    secret = totp.generate_secret()
    encrypted = totp.encrypt_seed_for_user(user_id=1, secret=secret)
    user_store.update_user_totp_seed(user_id=1, encrypted_seed=encrypted)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    try:
        yield client, secret
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


def _current_code(secret: str) -> str:
    return pyotp.TOTP(
        secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS,
    ).now()


# ── 1. Users WITHOUT TOTP: zero behaviour change ──────────────────────────


class TestLoginNoTotp:

    def test_login_sets_session_cookie_when_no_totp(self, base_client):
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Phase B PR 3 grew the response shape with a non-secret
        # ``requires_totp`` flag — for no-TOTP users it is False,
        # the SPA reloads into the authed shell on that signal.
        assert body["ok"] is True
        assert body["requires_totp"] is False
        assert "csrf_token" in body
        # Real session cookie present — login is complete.
        cookies = r.cookies
        assert "reverto_session" in cookies
        # NO pending-login-TOTP cookie minted; the no-TOTP path skips it.
        assert "reverto_login_totp_pending" not in cookies

    def test_login_failure_returns_401_no_pending_cookie(self, base_client):
        r = base_client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong-pw"},
        )
        assert r.status_code == 401
        assert "reverto_login_totp_pending" not in r.cookies
        assert "reverto_session" not in r.cookies


# ── 2. Users WITH TOTP enabled: 2-step flow ───────────────────────────────


class TestLoginWithTotp:

    def test_correct_password_returns_requires_totp(self, admin_with_totp):
        client, _ = admin_with_totp
        r = client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["requires_totp"] is True
        # No csrf_token yet — that comes with the session.
        assert "csrf_token" not in body
        # Pending cookie minted.
        set_cookies = r.headers.get_list("set-cookie")
        assert any(
            "reverto_login_totp_pending=" in raw for raw in set_cookies
        )
        # Crucially: NO session cookie yet. The user is still
        # halfway through login.
        assert "reverto_session" not in r.cookies

    def test_wrong_password_no_pending_cookie_set(self, admin_with_totp):
        client, _ = admin_with_totp
        r = client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong-pw"},
        )
        assert r.status_code == 401
        assert "reverto_login_totp_pending" not in r.cookies


# ── 3. /auth/login/totp — second step ─────────────────────────────────────


class TestLoginTotpVerify:

    def test_no_pending_cookie_returns_400(self, base_client):
        r = base_client.post("/auth/login/totp", json={"code": "123456"})
        assert r.status_code == 400
        assert "no login in progress" in r.json()["detail"].lower()

    def test_valid_code_completes_login(self, admin_with_totp):
        client, secret = admin_with_totp
        # Step 1: password.
        r1 = client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r1.status_code == 200
        assert r1.json()["requires_totp"] is True
        # TestClient auto-stores set-cookies for follow-up requests.
        # Step 2: TOTP.
        r2 = client.post(
            "/auth/login/totp",
            json={"code": _current_code(secret)},
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["ok"] is True
        assert body["requires_totp"] is False
        assert "csrf_token" in body
        # Real session cookie now present.
        assert "reverto_session" in r2.cookies
        # Pending cookie cleared (Set-Cookie header carries Max-Age=0).
        cleared = any(
            "reverto_login_totp_pending=" in raw and "Max-Age=0" in raw
            for raw in r2.headers.get_list("set-cookie")
        )
        assert cleared

    def test_wrong_code_returns_401_pending_preserved(self, admin_with_totp):
        client, _ = admin_with_totp
        # Step 1.
        client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        # Step 2 with a deliberately-wrong code. 000000 has ~1-in-1M
        # odds of accidentally matching the live code at any moment;
        # acceptable test-flake floor for this assertion.
        r = client.post("/auth/login/totp", json={"code": "000000"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid TOTP code"
        # No session cookie minted on failure.
        assert "reverto_session" not in r.cookies
        # Pending cookie NOT cleared — the user can retry.
        clears = [
            raw for raw in r.headers.get_list("set-cookie")
            if "reverto_login_totp_pending=" in raw and "Max-Age=0" in raw
        ]
        assert clears == []

    def test_pydantic_rejects_non_six_digit_code(self, admin_with_totp):
        client, _ = admin_with_totp
        client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        r = client.post("/auth/login/totp", json={"code": "abcdef"})
        assert r.status_code == 422
        r2 = client.post("/auth/login/totp", json={"code": "12345"})
        assert r2.status_code == 422

    def test_disabled_mid_flow_clears_pending_and_returns_400(
        self, admin_with_totp,
    ):
        """Race: user disables TOTP via /auth/totp/disable in another
        tab between the password step and the verify step. The
        verify must refuse the cookie and prompt the user to log in
        again — their password alone now suffices."""
        client, _ = admin_with_totp
        # Step 1 stages the pending cookie.
        client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        # Simulate the disable that happened in another tab.
        user_store.update_user_totp_seed(user_id=1, encrypted_seed=None)
        r = client.post("/auth/login/totp", json={"code": "123456"})
        assert r.status_code == 400
        assert "no longer enabled" in r.json()["detail"].lower()
        # Pending cookie cleared.
        assert any(
            "reverto_login_totp_pending=" in raw and "Max-Age=0" in raw
            for raw in r.headers.get_list("set-cookie")
        )

    def test_decrypt_failure_returns_500(self, admin_with_totp, monkeypatch):
        """Forced decrypt-failure surfaces as 500 with a denied
        audit row. Operator alert path — this is what fires when a
        Fernet-key rotation went wrong or the keyfile vanished."""
        client, _ = admin_with_totp
        client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )

        from cryptography.fernet import InvalidToken

        def _boom(_uid, _enc):
            raise InvalidToken("simulated key-rotation failure")

        monkeypatch.setattr(
            "core.totp.decrypt_seed_for_user", _boom,
        )
        r = client.post("/auth/login/totp", json={"code": "123456"})
        assert r.status_code == 500
        assert "temporarily unavailable" in r.json()["detail"].lower()


# ── 4. Pending-login-TOTP cookie helpers ──────────────────────────────────


class TestPendingLoginTotpCookieHelpers:

    def test_round_trip_returns_uid(self):
        from fastapi import Response
        from starlette.requests import Request as StarletteRequest

        response = Response()
        webapp._set_pending_login_totp_cookie(response, user_id=42)
        # Extract the cookie value.
        token = (
            response.headers["set-cookie"]
            .split("reverto_login_totp_pending=", 1)[1]
            .split(";", 1)[0]
        )
        scope = {
            "type": "http", "method": "POST",
            "headers": [
                (
                    b"cookie",
                    f"reverto_login_totp_pending={token}".encode(),
                ),
            ],
        }
        request = StarletteRequest(scope)
        assert webapp._read_pending_login_totp_cookie(request) == 42

    def test_separate_salt_from_enrollment_pending(self):
        """Distinct itsdangerous salt — an enrollment-pending cookie
        cannot be replayed as a login-pending cookie. Confused-deputy
        regression: if a future refactor accidentally collapsed the
        two serializers, a holder of an enrollment cookie could fake
        a login-completion."""
        from fastapi import Response
        from starlette.requests import Request as StarletteRequest

        response = Response()
        # Mint an ENROLLMENT-pending cookie (salt = .totp_pending.v1).
        webapp._set_pending_totp_cookie(
            response, secret="DUMMYBASE32", user_id=1,
        )
        token = (
            response.headers["set-cookie"]
            .split("reverto_totp_pending=", 1)[1]
            .split(";", 1)[0]
        )
        # Try to read it as a LOGIN-pending cookie (different salt).
        scope = {
            "type": "http", "method": "POST",
            "headers": [
                (
                    b"cookie",
                    f"reverto_login_totp_pending={token}".encode(),
                ),
            ],
        }
        request = StarletteRequest(scope)
        # Distinct salts → MAC fails → None.
        assert webapp._read_pending_login_totp_cookie(request) is None

    def test_missing_cookie_returns_none(self):
        from starlette.requests import Request as StarletteRequest
        scope = {"type": "http", "method": "POST", "headers": []}
        request = StarletteRequest(scope)
        assert webapp._read_pending_login_totp_cookie(request) is None

    def test_tampered_cookie_returns_none(self):
        from starlette.requests import Request as StarletteRequest
        scope = {
            "type": "http", "method": "POST",
            "headers": [
                (b"cookie", b"reverto_login_totp_pending=garbage.payload.sig"),
            ],
        }
        request = StarletteRequest(scope)
        assert webapp._read_pending_login_totp_cookie(request) is None


# ── 5. Operator recovery path (SQL fallback) ──────────────────────────────


class TestOperatorRecoveryFallback:
    """Operator-tested 2026-04-28: if a user is locked out (lost
    authenticator app + no recovery codes yet — Phase B PR 3 scope),
    the recovery procedure is to SSH in and run

        UPDATE users SET totp_seed_encrypted = NULL WHERE id = ?;

    These tests pin the contract that flipping that column to NULL
    actually returns the user to the password-only login path. A
    future refactor that introduces a separate ``totp_required``
    flag (or any other gate) without updating the recovery
    procedure would break the operator's tested fallback. Catching
    that here is cheaper than catching it in production."""

    def test_recovery_via_seed_null_returns_to_password_only_path(
        self, admin_with_totp,
    ):
        client, _ = admin_with_totp
        # Pre-recovery: TOTP is the gate.
        r1 = client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r1.json()["requires_totp"] is True

        # Operator runs the recovery SQL.
        user_store.update_user_totp_seed(user_id=1, encrypted_seed=None)

        # Fresh login attempt: clear cookies first to mimic a new
        # browser session post-recovery.
        client.cookies.clear()
        r2 = client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["requires_totp"] is False
        assert "csrf_token" in body
        # Session cookie set on the same request — single round-trip
        # password login restored.
        assert "reverto_session" in r2.cookies


# ── 6. Frontend assets ────────────────────────────────────────────────────


_STATIC = Path(__file__).resolve().parent.parent / "web" / "static"


class TestFrontendLoginTotpAssets:

    def test_login_totp_form_present(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        assert 'id="login-totp-form"' in html
        assert 'id="login-totp-code"' in html
        assert 'id="login-totp-cancel"' in html

    def test_login_handler_supports_two_step(self):
        js = (_STATIC / "app.js").read_text(encoding="utf-8")
        assert "requires_totp" in js
        assert "_showLoginTotpForm" in js
        assert "handleLoginTotpSubmit" in js
        assert "_resetLoginToPasswordStep" in js

    def test_cache_busters_bumped_for_pr3(self):
        import re
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        css_match = re.search(r"style\.css\?v=(\d+)", html)
        js_match = re.search(r"app\.js\?v=(\d+)", html)
        assert css_match and int(css_match.group(1)) >= 96
        assert js_match and int(js_match.group(1)) >= 217

    def test_login_totp_css_class_present(self):
        css = (_STATIC / "style.css").read_text(encoding="utf-8")
        assert ".login-totp-prompt" in css
        assert ".login-totp-cancel" in css
        assert "#login-totp-code" in css
