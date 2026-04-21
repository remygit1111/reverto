"""Tests for the /api/changelog + /api/admin/changelog JSON surface.

These endpoints back the SPA-integrated changelog tab. The server-
rendered HTML routes live in tests/test_changelog_routes.py and
will be removed in the final cleanup phase of the refactor.
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


_ADMIN_PW = "pytest-changelog-api-admin-pw-123"


@pytest.fixture
def admin_client():
    """TestClient with a session cookie for user_id=1.

    Mirrors tests/test_changelog_routes.py::admin_client — minted
    cookie directly to avoid slowapi's 5/minute login ceiling across
    large test classes.
    """
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
    """Authenticated-but-not-admin — covers the 403 path on every
    /api/admin/* endpoint."""
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


# ── Auth gates ────────────────────────────────────────────────────────────

class TestAuthGates:
    def test_public_api_requires_session(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/api/changelog")
        # /api/* gets JSON 401, not a redirect.
        assert r.status_code == 401

    def test_admin_api_refuses_non_admin(self, non_admin_client):
        r = non_admin_client.get("/api/admin/changelog")
        assert r.status_code == 403

    def test_admin_create_refuses_non_admin(self, non_admin_client):
        r = non_admin_client.post(
            "/api/admin/changelog",
            json={
                "title": "x", "description": "y", "category": "feature",
            },
        )
        assert r.status_code == 403


# ── Public /api/changelog ─────────────────────────────────────────────────

class TestPublicApi:
    def test_returns_empty_entries_on_fresh_install(self, admin_client):
        r = admin_client.get("/api/changelog")
        assert r.status_code == 200
        assert r.json() == {"entries": []}

    def test_drafts_hidden(self, admin_client):
        changelog_store.create_entry(
            "Secret draft", "body", "feature",
        )
        r = admin_client.get("/api/changelog")
        assert r.status_code == 200
        assert r.json()["entries"] == []

    def test_published_entries_appear_with_html(self, admin_client):
        eid = changelog_store.create_entry(
            "Public title", "body with **bold**", "feature",
        )
        changelog_store.publish_entry(eid)
        r = admin_client.get("/api/changelog")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        e = entries[0]
        assert e["id"] == eid
        assert e["title"] == "Public title"
        assert e["category"] == "feature"
        # Markdown is rendered server-side; the public endpoint only
        # ships the already-sanitised HTML.
        assert "<strong>bold</strong>" in e["description_html"]
        # Raw markdown must NOT leak into the public shape — the SPA
        # has no reason to see it.
        assert "description" not in e

    def test_public_response_strips_draft_metadata(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "fix")
        changelog_store.publish_entry(eid)
        r = admin_client.get("/api/changelog")
        entry = r.json()["entries"][0]
        assert "is_published" not in entry
        assert "created_at" not in entry
        assert "source_commit_sha" not in entry


# ── Admin /api/admin/changelog CRUD ───────────────────────────────────────

class TestAdminList:
    def test_empty_list(self, admin_client):
        r = admin_client.get("/api/admin/changelog")
        assert r.status_code == 200
        assert r.json() == {"entries": []}

    def test_includes_drafts(self, admin_client):
        draft = changelog_store.create_entry("draft", "d", "feature")
        live = changelog_store.create_entry("live", "d", "fix")
        changelog_store.publish_entry(live)
        r = admin_client.get("/api/admin/changelog")
        ids = {e["id"] for e in r.json()["entries"]}
        assert ids == {draft, live}


class TestAdminCreate:
    def test_create_as_draft(self, admin_client):
        r = admin_client.post(
            "/api/admin/changelog",
            json={
                "title": "Via API",
                "description": "Body",
                "category": "improvement",
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["title"] == "Via API"
        assert body["category"] == "improvement"
        assert body["is_published"] is False
        assert "<p>Body</p>" in body["description_html"]

    def test_create_rejects_invalid_category(self, admin_client):
        r = admin_client.post(
            "/api/admin/changelog",
            json={
                "title": "x", "description": "y", "category": "spam",
            },
        )
        assert r.status_code == 400
        # No row created — store sanity.
        assert changelog_store.list_all() == []

    def test_create_rejects_missing_fields(self, admin_client):
        r = admin_client.post(
            "/api/admin/changelog",
            json={"title": "only"},
        )
        # Pydantic-rejected payload → 422 (FastAPI default).
        assert r.status_code == 422

    def test_create_rejects_empty_title(self, admin_client):
        r = admin_client.post(
            "/api/admin/changelog",
            json={"title": "", "description": "y", "category": "fix"},
        )
        assert r.status_code == 422


class TestAdminRead:
    def test_read_single_entry(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "feature")
        r = admin_client.get(f"/api/admin/changelog/{eid}")
        assert r.status_code == 200
        assert r.json()["id"] == eid

    def test_read_unknown_is_404(self, admin_client):
        r = admin_client.get("/api/admin/changelog/9999")
        assert r.status_code == 404


class TestAdminPatch:
    def test_patch_partial_title(self, admin_client):
        eid = changelog_store.create_entry("before", "body", "feature")
        r = admin_client.patch(
            f"/api/admin/changelog/{eid}",
            json={"title": "after"},
        )
        assert r.status_code == 200
        assert r.json()["title"] == "after"
        # Description + category survived the partial patch.
        entry = changelog_store.get_entry(eid)
        assert entry["description"] == "body"
        assert entry["category"] == "feature"

    def test_patch_all_fields(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "feature")
        r = admin_client.patch(
            f"/api/admin/changelog/{eid}",
            json={
                "title": "t2", "description": "d2", "category": "fix",
            },
        )
        assert r.status_code == 200
        entry = changelog_store.get_entry(eid)
        assert entry["title"] == "t2"
        assert entry["description"] == "d2"
        assert entry["category"] == "fix"

    def test_patch_invalid_category_400(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "feature")
        r = admin_client.patch(
            f"/api/admin/changelog/{eid}",
            json={"category": "spam"},
        )
        assert r.status_code == 400

    def test_patch_unknown_id_404(self, admin_client):
        r = admin_client.patch(
            "/api/admin/changelog/9999",
            json={"title": "x"},
        )
        assert r.status_code == 404


class TestAdminPublishCycle:
    def test_publish_returns_updated_entry(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "feature")
        r = admin_client.post(f"/api/admin/changelog/{eid}/publish")
        assert r.status_code == 200
        body = r.json()
        assert body["is_published"] is True
        assert body["published_at"] is not None

    def test_unpublish_returns_updated_entry(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "feature")
        changelog_store.publish_entry(eid)
        r = admin_client.post(f"/api/admin/changelog/{eid}/unpublish")
        assert r.status_code == 200
        assert r.json()["is_published"] is False

    def test_publish_unknown_id_404(self, admin_client):
        r = admin_client.post("/api/admin/changelog/9999/publish")
        assert r.status_code == 404


class TestAdminDelete:
    def test_delete_204(self, admin_client):
        eid = changelog_store.create_entry("t", "d", "feature")
        r = admin_client.delete(f"/api/admin/changelog/{eid}")
        assert r.status_code == 204
        # Body is empty per HTTP spec — r.text may be "" or "null".
        assert changelog_store.get_entry(eid) is None

    def test_delete_unknown_is_404(self, admin_client):
        r = admin_client.delete("/api/admin/changelog/9999")
        assert r.status_code == 404
