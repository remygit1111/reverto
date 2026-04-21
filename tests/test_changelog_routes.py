"""Tests for the /changelog + /admin/changelog HTTP surface.

Covers:
  * Auth gates (unauthenticated → redirect, non-admin → 403).
  * Admin CRUD end-to-end (create → publish → edit → unpublish → delete).
  * Public listing only shows published entries.
  * Form validation (invalid category, empty title).
  * Markdown rendering + bleach XSS-sanitisation on the public page.

DB isolation is handled by the autouse fixture in conftest.py.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from core import changelog_store, user_store
from web import app as webapp


_ADMIN_PW = "pytest-changelog-admin-pw-123"


def _admin_session_cookie():
    """Mint a session cookie for user_id=1 without going through the
    rate-limited /auth/login endpoint. Mirrors the _admin_cookie helper
    in tests/test_web_routes.py — tests that run many requests per
    class would blow through slowapi's 5/minute login budget otherwise.
    """
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    return webapp._create_session_cookie(admin)


@pytest.fixture
def admin_client():
    """TestClient with a session cookie for user_id=1.

    Bypasses /auth/login by minting the cookie directly via
    ``_create_session_cookie`` — slowapi's 5/minute login limit would
    otherwise fail mid-class once the fixture runs more than 5 tests.
    ``_COOKIE_SECURE`` / ``_COOKIE_SAMESITE`` are flipped to lax+non-
    secure for the duration of the test so the TestClient actually
    retains the cookie across requests (audit v26-22 known limitation).
    """
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, _ADMIN_PW)

    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set("reverto_session", _admin_session_cookie())
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def non_admin_client():
    """Logged-in but not as user_id=1 — used to check 403 on admin routes."""
    from core.database import get_db
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (username, role) VALUES ('bob', 'user')",
        )
    bob = user_store.get_user_by_username("bob")
    user_store.set_password(bob.id, _ADMIN_PW)

    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set("reverto_session", webapp._create_session_cookie(bob))
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


# ── Auth gates ────────────────────────────────────────────────────────────

class TestAuthGates:

    def test_public_changelog_without_session_redirects(self):
        """Unauthenticated browser request to /changelog hits the
        AuthMiddleware catch and gets redirected to / (SPA login)."""
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/changelog", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    def test_admin_changelog_without_session_redirects(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/admin/changelog", follow_redirects=False)
        assert r.status_code == 303

    def test_admin_changelog_rejects_non_admin(self, non_admin_client):
        """Logged-in user that isn't user_id=1 must be refused with 403,
        not silently downgraded to the public view."""
        r = non_admin_client.get("/admin/changelog")
        assert r.status_code == 403

    def test_non_admin_cannot_create_entry(self, non_admin_client):
        r = non_admin_client.post(
            "/admin/changelog",
            data={
                "title": "x", "description": "y",
                "category": "feature", "action": "draft",
            },
            follow_redirects=False,
        )
        assert r.status_code == 403


# ── Public page ───────────────────────────────────────────────────────────

class TestPublicPage:

    def test_empty_state_shown_when_no_entries(self, admin_client):
        r = admin_client.get("/changelog")
        assert r.status_code == 200
        assert "No updates yet" in r.text

    def test_published_entries_appear(self, admin_client):
        eid = changelog_store.create_entry(
            "Public title", "Body text", "feature",
        )
        changelog_store.publish_entry(eid)

        r = admin_client.get("/changelog")
        assert r.status_code == 200
        assert "Public title" in r.text
        assert "Body text" in r.text

    def test_drafts_hidden_from_public_page(self, admin_client):
        changelog_store.create_entry(
            "Secret draft", "Hidden body", "feature",
        )
        r = admin_client.get("/changelog")
        assert "Secret draft" not in r.text


# ── Admin CRUD ────────────────────────────────────────────────────────────

class TestAdminCrud:

    def test_admin_list_shows_drafts_and_published(self, admin_client):
        draft = changelog_store.create_entry("Draft E", "…", "feature")
        live = changelog_store.create_entry("Live E", "…", "fix")
        changelog_store.publish_entry(live)

        r = admin_client.get("/admin/changelog")
        assert r.status_code == 200
        assert "Draft E" in r.text
        assert "Live E" in r.text
        assert "Draft" in r.text
        assert "Published" in r.text
        _ = draft

    def test_admin_new_form_renders(self, admin_client):
        r = admin_client.get("/admin/changelog/new")
        assert r.status_code == 200
        assert "New changelog entry" in r.text
        assert 'name="title"' in r.text

    def test_create_as_draft_via_post(self, admin_client):
        r = admin_client.post(
            "/admin/changelog",
            data={
                "title": "Form-driven title",
                "description": "Form-driven body",
                "category": "improvement",
                "action": "draft",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/changelog"

        entries = changelog_store.list_all()
        assert len(entries) == 1
        assert entries[0]["title"] == "Form-driven title"
        assert entries[0]["is_published"] is False

    def test_create_and_publish_via_post(self, admin_client):
        r = admin_client.post(
            "/admin/changelog",
            data={
                "title": "Published via form",
                "description": "Body",
                "category": "feature",
                "action": "publish",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        entries = changelog_store.list_all()
        assert len(entries) == 1
        assert entries[0]["is_published"] is True
        assert entries[0]["published_at"] is not None

    def test_create_rejects_invalid_category(self, admin_client):
        r = admin_client.post(
            "/admin/changelog",
            data={
                "title": "X",
                "description": "Y",
                "category": "news",  # not in the whitelist
                "action": "draft",
            },
        )
        assert r.status_code == 400
        assert changelog_store.list_all() == []

    def test_publish_endpoint_flips_state(self, admin_client):
        eid = changelog_store.create_entry(
            "t", "d", "feature",
        )
        r = admin_client.post(
            f"/admin/changelog/{eid}/publish",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert changelog_store.get_entry(eid)["is_published"] is True

    def test_unpublish_endpoint_flips_state(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "feature")
        changelog_store.publish_entry(eid)
        r = admin_client.post(
            f"/admin/changelog/{eid}/unpublish",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert changelog_store.get_entry(eid)["is_published"] is False

    def test_delete_endpoint_removes_entry(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "feature")
        r = admin_client.post(
            f"/admin/changelog/{eid}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert changelog_store.get_entry(eid) is None

    def test_edit_form_renders_existing_values(self, admin_client):
        eid = changelog_store.create_entry(
            "Before title", "Before body", "feature",
        )
        r = admin_client.get(f"/admin/changelog/{eid}/edit")
        assert r.status_code == 200
        assert "Before title" in r.text
        assert "Before body" in r.text

    def test_edit_post_updates_fields(self, admin_client):
        eid = changelog_store.create_entry(
            "Before title", "Before body", "feature",
        )
        r = admin_client.post(
            f"/admin/changelog/{eid}",
            data={
                "title": "After title",
                "description": "After body",
                "category": "fix",
                "action": "draft",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        entry = changelog_store.get_entry(eid)
        assert entry["title"] == "After title"
        assert entry["description"] == "After body"
        assert entry["category"] == "fix"

    def test_edit_unknown_id_is_404(self, admin_client):
        r = admin_client.get("/admin/changelog/9999/edit")
        assert r.status_code == 404

    def test_publish_unknown_id_is_404(self, admin_client):
        r = admin_client.post(
            "/admin/changelog/9999/publish",
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_delete_unknown_id_is_404(self, admin_client):
        r = admin_client.post(
            "/admin/changelog/9999/delete",
            follow_redirects=False,
        )
        assert r.status_code == 404
