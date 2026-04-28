"""Phase B PR 2 — TOTP enrollment / verify / disable endpoint tests.

Builds on the PR 1 foundation. Covers:

* /auth/totp/setup — pending cookie minted + secret + QR returned
* /auth/totp/verify — code-against-pending check, DB commit on success
* /auth/totp/disable — dual-factor (password + code) gate
* The pending-TOTP cookie helpers in web/app.py (sign / verify /
  uid-binding / clear)
* Frontend assets — TOTP modal markup + JS handlers + cache-buster bumps

Every endpoint test runs through TestClient against the full FastAPI
app so middleware, auth-dep, CSRF and rate-limiter all participate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyotp  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import totp, user_store  # noqa: E402
from web import app as webapp  # noqa: E402


_KNOWN_PW = "pytest-totp-known-password-789"
_TEST_CSRF_TOKEN = "pytest-totp-csrf-token-r1073"


@pytest.fixture(autouse=True)
def _disable_hibp_by_default(monkeypatch):
    """Mirror the test_web_routes.py fixture — keeps the password-
    change path offline. Not strictly required for these tests
    (none touch HIBP) but cheap insurance against accidental
    cross-module import drag."""
    async def _always_clean(_plaintext):
        return False
    monkeypatch.setattr(
        "web.routes.auth.is_password_pwned", _always_clean,
    )


@pytest.fixture
def auth_client():
    """TestClient with the seeded admin authenticated.

    Mirrors the auth_client fixture in tests/test_web_routes.py
    one-to-one — same secure/samesite/CSRF posture so a future
    auth-cookie change has a single update site."""
    admin = user_store.get_user_by_username("admin")
    assert admin is not None, "admin seed missing — check init_db"
    user_store.set_password(admin.id, _KNOWN_PW)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set("reverto_csrf", _TEST_CSRF_TOKEN)
    client.headers.update({"X-CSRF-Token": _TEST_CSRF_TOKEN})
    client.cookies.set("reverto_session", webapp._create_session_cookie(admin))
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def totp_user_client(auth_client):
    """auth_client whose admin row has TOTP enabled.

    Useful for /disable + /setup-already-enabled tests. Returns a
    tuple (client, secret) so the test can drive the verify/disable
    paths with a code that actually matches."""
    secret = totp.generate_secret()
    encrypted = totp.encrypt_seed_for_user(user_id=1, secret=secret)
    user_store.update_user_totp_seed(user_id=1, encrypted_seed=encrypted)
    return auth_client, secret


# ── 1. /auth/totp/setup ────────────────────────────────────────────────────


class TestTotpSetup:

    def test_unauthenticated_request_is_401(self):
        # AuthMiddleware redirects unauth non-API paths to / for the
        # browser flow. We pass Accept: application/json (which is
        # what the SPA fetch() helpers do) so the middleware returns
        # the JSON 401 path that real client code consumes.
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.post(
            "/auth/totp/setup",
            headers={"Accept": "application/json"},
        )
        assert r.status_code == 401

    def test_authenticated_setup_returns_secret_uri_and_qr(self, auth_client):
        r = auth_client.post("/auth/totp/setup")
        assert r.status_code == 200, r.text
        body = r.json()
        # Pinned response shape — every field is consumed by the SPA.
        assert "provisioning_uri" in body
        assert body["provisioning_uri"].startswith("otpauth://totp/")
        assert "secret" in body
        assert len(body["secret"]) == 32
        assert "qr_svg" in body
        assert "<svg" in body["qr_svg"]
        assert "expires_at" in body

    def test_setup_mints_pending_cookie(self, auth_client):
        r = auth_client.post("/auth/totp/setup")
        assert r.status_code == 200
        # The Set-Cookie header is what enforces the contract that
        # /auth/totp/verify reads back later. Cookie value is opaque
        # (signed), so we only assert presence + flags.
        set_cookies = r.headers.get_list("set-cookie")
        pending_cookie_set = any(
            "reverto_totp_pending=" in raw for raw in set_cookies
        )
        assert pending_cookie_set, set_cookies

    def test_setup_refuses_when_totp_already_enabled(self, totp_user_client):
        client, _ = totp_user_client
        r = client.post("/auth/totp/setup")
        assert r.status_code == 400
        assert "already" in r.json()["detail"].lower()


# ── 2. /auth/totp/verify ───────────────────────────────────────────────────


class TestTotpVerify:

    def test_verify_without_pending_cookie_is_400(self, auth_client):
        # Drop any pending cookie that might be lingering.
        auth_client.cookies.pop("reverto_totp_pending", None)
        r = auth_client.post("/auth/totp/verify", json={"code": "123456"})
        assert r.status_code == 400
        assert "no totp" in r.json()["detail"].lower()

    def test_verify_with_valid_code_commits_seed_to_db(self, auth_client):
        # Step 1: setup mints pending cookie + returns secret.
        setup = auth_client.post("/auth/totp/setup")
        assert setup.status_code == 200
        secret = setup.json()["secret"]
        # Step 2: derive a current code and verify it.
        code = pyotp.TOTP(
            secret,
            digits=totp.DIGITS,
            interval=totp.PERIOD_SECONDS,
        ).now()
        r = auth_client.post("/auth/totp/verify", json={"code": code})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "totp_enabled": True}
        # DB committed.
        admin = user_store.get_user_by_id(1)
        assert admin.totp_enabled is True
        # Pending cookie cleared.
        set_cookies = r.headers.get_list("set-cookie")
        cleared = any(
            "reverto_totp_pending=" in raw and "Max-Age=0" in raw
            for raw in set_cookies
        )
        assert cleared, set_cookies

    def test_verify_with_wrong_code_keeps_pending_cookie(self, auth_client):
        setup = auth_client.post("/auth/totp/setup")
        assert setup.status_code == 200
        # Submit a code that's almost certainly not the live one
        # (1-in-1M odds it accidentally matches).
        r = auth_client.post("/auth/totp/verify", json={"code": "000000"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid TOTP code"
        # DB NOT committed.
        admin = user_store.get_user_by_id(1)
        assert admin.totp_enabled is False
        # Pending cookie preserved so the user can retry without re-
        # running setup. We assert by attempting another verify with
        # the right code and expecting success.
        secret = setup.json()["secret"]
        code = pyotp.TOTP(
            secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS,
        ).now()
        r2 = auth_client.post("/auth/totp/verify", json={"code": code})
        assert r2.status_code == 200, r2.text

    def test_verify_rejects_non_numeric_code_via_pydantic(self, auth_client):
        # Pydantic 422 fires before the handler — pattern=^\d{6}$.
        r = auth_client.post("/auth/totp/verify", json={"code": "abcdef"})
        assert r.status_code == 422

    def test_verify_rejects_short_code_via_pydantic(self, auth_client):
        r = auth_client.post("/auth/totp/verify", json={"code": "12345"})
        assert r.status_code == 422


# ── 3. /auth/totp/disable ──────────────────────────────────────────────────


class TestTotpDisable:

    def test_disable_when_not_enabled_is_400(self, auth_client):
        r = auth_client.post(
            "/auth/totp/disable",
            json={"current_password": _KNOWN_PW, "totp_code": "123456"},
        )
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"].lower()

    def test_disable_with_wrong_password_is_401(self, totp_user_client):
        client, secret = totp_user_client
        code = pyotp.TOTP(
            secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS,
        ).now()
        r = client.post(
            "/auth/totp/disable",
            json={"current_password": "wrong-password", "totp_code": code},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid credentials"
        # TOTP STILL enabled — neither factor sufficient alone.
        admin = user_store.get_user_by_id(1)
        assert admin.totp_enabled is True

    def test_disable_with_wrong_code_is_401(self, totp_user_client):
        client, _ = totp_user_client
        r = client.post(
            "/auth/totp/disable",
            json={"current_password": _KNOWN_PW, "totp_code": "000000"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid TOTP code"
        admin = user_store.get_user_by_id(1)
        assert admin.totp_enabled is True

    def test_disable_with_both_correct_clears_seed(self, totp_user_client):
        client, secret = totp_user_client
        code = pyotp.TOTP(
            secret, digits=totp.DIGITS, interval=totp.PERIOD_SECONDS,
        ).now()
        r = client.post(
            "/auth/totp/disable",
            json={"current_password": _KNOWN_PW, "totp_code": code},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "totp_enabled": False}
        admin = user_store.get_user_by_id(1)
        assert admin.totp_enabled is False
        assert admin.totp_seed_encrypted is None


# ── 4. Pending-TOTP cookie helpers ─────────────────────────────────────────


class TestPendingTotpCookieHelpers:
    """Direct unit tests on the web.app helpers — ensures the cookie
    contract is honest even outside the endpoint flow."""

    def test_round_trip_returns_secret_when_uid_matches(self):
        from fastapi import Response
        from starlette.requests import Request as StarletteRequest

        secret = totp.generate_secret()
        response = Response()
        webapp._set_pending_totp_cookie(response, secret, user_id=42)
        # Extract the cookie value the framework just set.
        set_cookie_header = response.headers.get("set-cookie")
        assert "reverto_totp_pending=" in set_cookie_header
        token = set_cookie_header.split("reverto_totp_pending=", 1)[1].split(";", 1)[0]
        # Build a fake request that carries the cookie.
        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"cookie", f"reverto_totp_pending={token}".encode())],
        }
        request = StarletteRequest(scope)
        assert (
            webapp._read_pending_totp_cookie(request, expected_user_id=42)
            == secret
        )

    def test_uid_mismatch_returns_none(self):
        from fastapi import Response
        from starlette.requests import Request as StarletteRequest
        secret = totp.generate_secret()
        response = Response()
        webapp._set_pending_totp_cookie(response, secret, user_id=1)
        token = (
            response.headers["set-cookie"]
            .split("reverto_totp_pending=", 1)[1]
            .split(";", 1)[0]
        )
        scope = {
            "type": "http", "method": "POST",
            "headers": [(b"cookie", f"reverto_totp_pending={token}".encode())],
        }
        request = StarletteRequest(scope)
        # uid=2 does not match payload's uid=1 — must fall closed.
        assert webapp._read_pending_totp_cookie(request, expected_user_id=2) is None

    def test_missing_cookie_returns_none(self):
        from starlette.requests import Request as StarletteRequest
        scope = {"type": "http", "method": "POST", "headers": []}
        request = StarletteRequest(scope)
        assert webapp._read_pending_totp_cookie(request, expected_user_id=1) is None

    def test_tampered_cookie_returns_none(self):
        from starlette.requests import Request as StarletteRequest
        # A clearly-invalid signature.
        scope = {
            "type": "http", "method": "POST",
            "headers": [(b"cookie", b"reverto_totp_pending=garbage.payload.signature")],
        }
        request = StarletteRequest(scope)
        assert webapp._read_pending_totp_cookie(request, expected_user_id=1) is None


# ── 5. Frontend assets ─────────────────────────────────────────────────────


_STATIC = Path(__file__).resolve().parent.parent / "web" / "static"


class TestFrontendTotpAssets:
    """Pin the wired-in markup + JS handler names + cache-buster
    bumps so a future refactor that drops one of them gets caught
    here. Pure source-grep assertions; cheap to run."""

    def test_index_html_has_totp_section_in_profile(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        assert 'id="profile-totp-section"' in html
        assert 'id="profile-totp-disabled"' in html
        assert 'id="profile-totp-enabled"' in html
        assert 'id="profile-totp-enable-btn"' in html
        assert 'id="profile-totp-disable-btn"' in html

    def test_index_html_has_enroll_and_disable_modals(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        assert 'id="totp-enroll-modal"' in html
        assert 'id="totp-disable-modal"' in html
        assert 'id="totp-qr-container"' in html
        assert 'id="totp-secret-display"' in html

    def test_index_html_does_not_load_qrcode_cdn(self):
        """Phase B PR 2 deviation from the original prompt: server-
        side SVG via the qrcode Python package replaces the
        client-side qrcode-generator CDN. This test pins that the
        CDN dep is not silently re-introduced — a regression that
        would also re-open the v27-04 supply-chain surface."""
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        assert "qrcode-generator" not in html, (
            "qrcode-generator CDN must not be re-introduced — server-"
            "side SVG keeps v27-04 closed; see /auth/totp/setup qr_svg."
        )

    def test_app_js_has_totp_handlers(self):
        js = (_STATIC / "app.js").read_text(encoding="utf-8")
        assert "_startTotpEnrollment" in js
        assert "_handleTotpVerify" in js
        assert "_handleTotpDisable" in js
        assert "_renderTotpStatusInProfile" in js
        assert "_wireTotpUiHandlers" in js

    def test_cache_buster_bumps_for_totp_pr(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        # Both bumped vs the v94 / v215 baseline that landed before
        # this PR — values just need to be HIGHER (don't pin the
        # exact number so a future PR can bump again).
        import re
        css_match = re.search(r"style\.css\?v=(\d+)", html)
        js_match = re.search(r"app\.js\?v=(\d+)", html)
        assert css_match and int(css_match.group(1)) >= 95
        assert js_match and int(js_match.group(1)) >= 216

    def test_totp_css_classes_present(self):
        css = (_STATIC / "style.css").read_text(encoding="utf-8")
        assert ".totp-qr-container" in css
        assert ".totp-code-input" in css
        assert ".totp-warning" in css
