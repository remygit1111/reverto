"""Tests for ``POST /api/admin/marketing/regenerate``.

Covers auth gating (401 / 403), success (200), partial success
(207 Multi-Status), and total failure (500). The endpoint is
admin-only and rate-limited at 10/minute via slowapi.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from core import marketing_export, user_store
from web import app as webapp


_ADMIN_PW = "pytest-marketing-regen-admin-pw-789"


@pytest.fixture
def admin_client():
    """TestClient with a session cookie for the seeded admin
    (user_id=1)."""
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, _ADMIN_PW)
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


@pytest.fixture
def non_admin_client():
    """Authenticated-but-not-admin user — covers 403."""
    from core.database import get_db
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (username, role) VALUES ('bob', 'user')",
        )
    bob = user_store.get_user_by_username("bob")
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set(
        "reverto_session", webapp._create_session_cookie(bob),
    )
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


# ── Auth gates ───────────────────────────────────────────────────────────


class TestAuthGates:

    def test_no_session_returns_401(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 401

    def test_non_admin_returns_403(self, non_admin_client):
        r = non_admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 403


# ── Happy path ──────────────────────────────────────────────────────────


class TestHappyPath:

    def test_200_when_both_succeed(self, admin_client):
        r = admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["results"] == {"roadmap": True, "changelog": True}


# ── Partial / failure paths ─────────────────────────────────────────────


class TestPartialAndFailure:

    def test_207_when_only_roadmap_fails(self, admin_client):
        with patch.object(
            marketing_export, "write_roadmap_snapshot", return_value=False,
        ):
            r = admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 207, r.text
        body = r.json()
        assert body["status"] == "partial"
        assert body["results"] == {"roadmap": False, "changelog": True}

    def test_207_when_only_changelog_fails(self, admin_client):
        with patch.object(
            marketing_export, "write_changelog_snapshot", return_value=False,
        ):
            r = admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 207
        body = r.json()
        assert body["status"] == "partial"
        assert body["results"] == {"roadmap": True, "changelog": False}

    def test_500_when_both_fail(self, admin_client):
        with patch.object(
            marketing_export, "write_roadmap_snapshot", return_value=False,
        ), patch.object(
            marketing_export, "write_changelog_snapshot", return_value=False,
        ):
            r = admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 500
        body = r.json()
        assert body["status"] == "failed"
        assert body["results"] == {"roadmap": False, "changelog": False}
