"""Regression guard for audit r1-073 — CSRF double-submit-cookie.

Conftest auto-seeds a matching CSRF cookie + header on every
TestClient instance so the ordinary mutating test paths pass
through the middleware. This module specifically exercises the
rejection paths: missing-cookie, missing-header, mismatch,
exempt-path, and GET-pass-through.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import user_store  # noqa: E402
from web import app as webapp  # noqa: E402


@pytest.fixture
def _authenticated_client():
    """A TestClient with the admin session cookie manually set.
    Conftest seeds the CSRF pair; tests below strip pieces to
    exercise the failure paths."""
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set(
        "reverto_session", webapp._create_session_cookie(admin),
    )
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


def test_post_with_matching_cookie_and_header_passes(
    _authenticated_client,
):
    # Conftest already seeded both. Any mutating endpoint the SPA
    # hits routinely should land past the CSRF check — /api/bots
    # POST validates body shape so we expect 400/422, not 403.
    r = _authenticated_client.post("/api/bots", json={})
    assert r.status_code != 403, (
        f"CSRF should not reject with matching pair; got {r.status_code}: {r.text}"
    )


def test_post_missing_header_returns_403(_authenticated_client):
    # Strip the X-CSRF-Token header — cookie still present.
    _authenticated_client.headers.pop("X-CSRF-Token", None)
    r = _authenticated_client.post("/api/bots", json={})
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_post_missing_cookie_triggers_graceful_migration(
    _authenticated_client,
):
    """Hotfix: an authenticated request without the CSRF cookie is
    a legacy-session migration. The middleware lets it through +
    mints a cookie on the response so subsequent requests use
    normal enforcement."""
    _authenticated_client.cookies.delete("reverto_csrf")
    r = _authenticated_client.post("/api/bots", json={})
    assert r.status_code != 403, (
        "graceful migration should pass the request through instead "
        "of 403ing; got CSRF rejection"
    )
    # The response must carry a fresh reverto_csrf cookie.
    set_cookie = r.headers.get("set-cookie", "")
    assert "reverto_csrf=" in set_cookie, (
        f"graceful migration did not attach CSRF cookie; "
        f"set-cookie={set_cookie!r}"
    )


def test_post_with_mismatched_tokens_returns_403(_authenticated_client):
    _authenticated_client.headers.update({"X-CSRF-Token": "definitely-wrong"})
    r = _authenticated_client.post("/api/bots", json={})
    assert r.status_code == 403
    assert "mismatch" in r.json()["detail"].lower()


def test_get_passes_without_csrf_tokens():
    # A clean TestClient (no session cookie) on a GET endpoint.
    # CSRF middleware only fires on mutating methods AND when a
    # session cookie is present — so GET /health is a pass.
    client = TestClient(webapp.app)
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    r = client.get("/healthz")
    assert r.status_code == 200


def test_login_exempt_from_csrf():
    """POST /auth/login is exempt — the client has no token yet."""
    client = TestClient(webapp.app)
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    r = client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    # Accept any response other than 403 CSRF — 401/429/etc all fine.
    assert r.status_code != 403
    if r.status_code == 403:
        assert "CSRF" not in r.json().get("detail", "")


# ── Hotfix: graceful migration for pre-VPS-1 sessions ──────────────────────


def test_get_with_session_but_no_csrf_mints_cookie(_authenticated_client):
    """A legacy-session GET must hand the SPA a CSRF cookie so the
    next mutating request can echo it. Otherwise the SPA would never
    be able to mint a token without re-login."""
    _authenticated_client.cookies.delete("reverto_csrf")
    _authenticated_client.headers.pop("X-CSRF-Token", None)
    r = _authenticated_client.get("/healthz")
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "reverto_csrf=" in set_cookie


def test_migration_only_grants_first_request(_authenticated_client):
    """After the migration grant, the SPA has a cookie. A subsequent
    mutating request WITHOUT the header must still 403 — one-shot
    grant, not a permanent bypass."""
    _authenticated_client.cookies.delete("reverto_csrf")
    # First request — migration grant.
    r1 = _authenticated_client.post("/api/bots", json={})
    assert r1.status_code != 403

    # The response set a cookie; httpx will persist it automatically.
    # Now strip just the header (keep the cookie) — second mutating
    # request should 403 because it has cookie but not header.
    _authenticated_client.headers.pop("X-CSRF-Token", None)
    r2 = _authenticated_client.post("/api/bots", json={})
    assert r2.status_code == 403
    assert "CSRF" in r2.json()["detail"]


def test_login_uses_shared_csrf_cookie_helper():
    """DRY guard: both the login mint path and the middleware
    migration path call ``_set_csrf_cookie_on_response``. If a
    future refactor breaks the helper, this test surfaces it."""
    from fastapi.responses import JSONResponse

    resp = JSONResponse({"ok": True})
    webapp._set_csrf_cookie_on_response(resp, "test-token-value")
    cookies = resp.headers.get("set-cookie", "")
    assert "reverto_csrf=test-token-value" in cookies
    # httponly=False so the SPA can read it.
    assert "HttpOnly" not in cookies
