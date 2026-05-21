"""Tests for the /api/roadmap + /api/admin/roadmap JSON surface.

Mirrors tests/test_changelog_api.py conventions for fixtures.

KRITIEK: ``/api/roadmap`` is publicly accessible (no auth gate)
— a logged-out client must reach the endpoint cleanly. This is
the one route in the file that bypasses the AuthMiddleware via
``web.app._PUBLIC_PATHS``. The admin counterparts at
``/api/admin/roadmap/*`` retain the standard middleware gate +
``_require_admin_user`` dependency.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from core import roadmap_store, user_store
from web import app as webapp


_ADMIN_PW = "pytest-roadmap-admin-pw-456"
_KEY = "phase-1"


@pytest.fixture
def admin_client():
    """TestClient with a session cookie for the seeded admin
    (user_id=1).

    Flake-hardening (intermittent 401 investigation): TestClient is
    context-managed so its anyio portal thread + that thread's
    thread-local SQLite connection are torn down per test. An
    unclosed TestClient leaks the portal thread; across the full
    suite a leaked thread from an epoch-bumping test can lazily
    resolve core.database._DB_PATH onto a *later* test's
    freshly-seeded DB and flip admin's session_epoch 0 -> 1,
    making this cookie fail the per-user epoch check with a
    spurious 401 (signature: cookie_uid=1, cookie_epoch=0,
    server_epoch=1). The pre-yield assertion fails fast with a
    clear message instead of a confusing downstream 401.
    """
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, _ADMIN_PW)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    try:
        with TestClient(webapp.app) as client:
            client.cookies.set(
                "reverto_session", webapp._create_session_cookie(admin),
            )
            assert webapp._verify_session_cookie(
                client.cookies.get("reverto_session"),
            ) is not None, (
                "admin_client minted a session cookie that does not "
                "validate — admin session_epoch is inconsistent "
                "(test-isolation leak); see fixture docstring"
            )
            yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def non_admin_client():
    """Authenticated-but-not-admin user. Covers the 403 path on
    every /api/admin/* endpoint."""
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
    # Context-managed so the portal thread + its SQLite connection
    # are torn down per test (same leak-hardening as admin_client).
    try:
        with TestClient(webapp.app) as client:
            client.cookies.set(
                "reverto_session", webapp._create_session_cookie(bob),
            )
            yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


def _create_payload(**overrides):
    payload = {
        "phase_key": _KEY,
        "display_name": "Foundation",
        "summary": "Multi-bot architecture, paper engine.",
        "status": "pending",
        "sort_order": 10,
    }
    payload.update(overrides)
    return payload


# ── Public-shape /api/roadmap (session-required after PR 3) ──────────────


class TestPublicEndpoint:
    """KRITIEK: /api/roadmap is session-required (PR 3 of the
    marketing-app split removed it from ``_PUBLIC_PATHS``). The
    response shape still strips admin-only fields so a future
    re-opening to anonymous callers would not regress the
    admin/public boundary."""

    def test_anonymous_returns_401(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/api/roadmap")
        # PR 3 re-gated this — was 200 during the public-shell
        # phase. The marketing site at reverto.bot reads the
        # /data/roadmap.json snapshot instead.
        assert r.status_code == 401, r.text

    def test_returns_empty_phases_on_fresh_install(self, admin_client):
        r = admin_client.get("/api/roadmap")
        assert r.status_code == 200
        assert r.json() == {"phases": []}

    def test_drafts_omitted(self, admin_client):
        roadmap_store.create_phase(
            phase_key="draft-1",
            display_name="Draft phase",
            summary="Should not surface publicly",
        )
        r = admin_client.get("/api/roadmap")
        assert r.status_code == 200
        assert r.json()["phases"] == []

    def test_admin_only_fields_omitted(self, admin_client):
        pid = roadmap_store.create_phase(
            phase_key="phase-published",
            display_name="Published",
            summary="Visible to public.",
        )
        roadmap_store.publish_phase(pid)
        r = admin_client.get("/api/roadmap")
        assert r.status_code == 200
        phase = r.json()["phases"][0]
        # Admin-only fields stripped from public shape (the
        # admin-side endpoint at /api/admin/roadmap returns the
        # full row with id / is_published / created_at /
        # updated_at).
        assert "id" not in phase
        assert "is_published" not in phase
        assert "created_at" not in phase
        assert "updated_at" not in phase
        # Public fields present.
        assert phase["phase_key"] == "phase-published"
        assert phase["display_name"] == "Published"
        assert phase["status"] == "pending"

    def test_body_md_rendered_to_html(self, admin_client):
        pid = roadmap_store.create_phase(
            phase_key="phase-md",
            display_name="With markdown",
            summary="Body has **bold** and a list.",
            body_md="**bold** then\n\n- item one\n- item two",
        )
        roadmap_store.publish_phase(pid)
        r = admin_client.get("/api/roadmap")
        phase = r.json()["phases"][0]
        # Pre-rendered HTML for safe innerHTML drop on the SPA.
        assert "<strong>bold</strong>" in phase["body_html"]
        assert "<li>item one</li>" in phase["body_html"]
        # PT-v4-MK-001: ``body_md`` is no longer in the public
        # shape; only the rendered ``body_html`` is. Round-tripping
        # the raw markdown for an edit form is the ADMIN endpoint's
        # job (``/api/admin/roadmap/{id}`` via
        # ``_phase_to_admin_json``), not this public surface.
        assert "body_md" not in phase


# ── Auth gates ────────────────────────────────────────────────────────────


class TestAuthGates:

    def test_admin_list_requires_session(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/api/admin/roadmap")
        assert r.status_code == 401

    def test_admin_list_refuses_non_admin(self, non_admin_client):
        r = non_admin_client.get("/api/admin/roadmap")
        assert r.status_code == 403

    def test_admin_create_refuses_non_admin(self, non_admin_client):
        r = non_admin_client.post(
            "/api/admin/roadmap", json=_create_payload(),
        )
        assert r.status_code == 403

    def test_admin_delete_refuses_non_admin(self, non_admin_client):
        r = non_admin_client.delete("/api/admin/roadmap/1")
        assert r.status_code == 403

    def test_reorder_refuses_non_admin(self, non_admin_client):
        r = non_admin_client.post(
            "/api/admin/roadmap/reorder", json={"ids": [1]},
        )
        assert r.status_code == 403


# ── Admin CRUD ────────────────────────────────────────────────────────────


class TestAdminCrud:

    def test_create_returns_201_with_admin_shape(self, admin_client):
        r = admin_client.post(
            "/api/admin/roadmap", json=_create_payload(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # Admin shape carries id + admin-only fields.
        assert isinstance(body["id"], int)
        assert body["phase_key"] == _KEY
        assert body["is_published"] is False
        assert "created_at" in body
        assert "body_html" in body  # pre-rendered for preview

    def test_create_duplicate_key_returns_409(self, admin_client):
        admin_client.post("/api/admin/roadmap", json=_create_payload())
        r = admin_client.post("/api/admin/roadmap", json=_create_payload())
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"].lower()

    def test_create_invalid_status_rejected_at_pydantic(
        self, admin_client,
    ):
        # Literal validator catches this at parse-time → 422.
        r = admin_client.post(
            "/api/admin/roadmap",
            json=_create_payload(status="halfway"),
        )
        assert r.status_code == 422

    def test_create_invalid_phase_key_returns_400(self, admin_client):
        # Pydantic length / regex doesn't validate the regex
        # (the store does), so this comes back as 400.
        r = admin_client.post(
            "/api/admin/roadmap",
            json=_create_payload(phase_key="Phase With Spaces"),
        )
        assert r.status_code == 400
        assert "[a-z0-9-]" in r.json()["detail"]

    def test_read_returns_admin_shape(self, admin_client):
        c = admin_client.post(
            "/api/admin/roadmap", json=_create_payload(),
        )
        pid = c.json()["id"]
        r = admin_client.get(f"/api/admin/roadmap/{pid}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == pid
        assert body["phase_key"] == _KEY

    def test_read_unknown_id_returns_404(self, admin_client):
        r = admin_client.get("/api/admin/roadmap/9999")
        assert r.status_code == 404

    def test_patch_partial_update(self, admin_client):
        c = admin_client.post(
            "/api/admin/roadmap", json=_create_payload(),
        )
        pid = c.json()["id"]
        r = admin_client.patch(
            f"/api/admin/roadmap/{pid}",
            json={"status": "active", "in_progress_note": "doing it"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "active"
        assert body["in_progress_note"] == "doing it"
        # Unspecified fields untouched.
        assert body["display_name"] == "Foundation"

    def test_publish_unpublish_roundtrip(self, admin_client):
        c = admin_client.post(
            "/api/admin/roadmap", json=_create_payload(),
        )
        pid = c.json()["id"]
        r = admin_client.post(f"/api/admin/roadmap/{pid}/publish")
        assert r.status_code == 200
        assert r.json()["is_published"] is True
        # Now unpublish.
        r = admin_client.post(f"/api/admin/roadmap/{pid}/unpublish")
        assert r.status_code == 200
        assert r.json()["is_published"] is False
        # published_at preserved (audit-trail contract).
        assert r.json()["published_at"] is not None

    def test_delete_returns_204(self, admin_client):
        c = admin_client.post(
            "/api/admin/roadmap", json=_create_payload(),
        )
        pid = c.json()["id"]
        r = admin_client.delete(f"/api/admin/roadmap/{pid}")
        assert r.status_code == 204
        # Subsequent GET returns 404.
        r2 = admin_client.get(f"/api/admin/roadmap/{pid}")
        assert r2.status_code == 404


# ── Admin reorder ─────────────────────────────────────────────────────────


class TestReorder:

    def test_reorder_updates_sort_order(self, admin_client):
        ids = []
        for k in ("a", "b", "c"):
            c = admin_client.post(
                "/api/admin/roadmap",
                json=_create_payload(
                    phase_key=f"phase-{k}",
                    display_name=f"Phase {k.upper()}",
                    sort_order=999,  # all the same — no implicit order
                ),
            )
            ids.append(c.json()["id"])
        # Reorder to reversed.
        r = admin_client.post(
            "/api/admin/roadmap/reorder",
            json={"ids": list(reversed(ids))},
        )
        assert r.status_code == 200, r.text
        # Verify via list endpoint.
        ladmin = admin_client.get("/api/admin/roadmap").json()
        keys = [p["phase_key"] for p in ladmin["phases"]]
        assert keys == ["phase-c", "phase-b", "phase-a"]

    def test_reorder_unknown_id_returns_400(self, admin_client):
        c = admin_client.post(
            "/api/admin/roadmap", json=_create_payload(),
        )
        pid = c.json()["id"]
        r = admin_client.post(
            "/api/admin/roadmap/reorder",
            json={"ids": [pid, 99999]},
        )
        assert r.status_code == 400
        assert "Unknown phase ids" in r.json()["detail"]
