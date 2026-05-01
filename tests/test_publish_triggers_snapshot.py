"""Tests that the snapshot-write hooks added in PR 2 actually fire
on every roadmap + changelog mutation that can change the public
state, AND that a snapshot-write failure does NOT propagate up to
fail the calling endpoint.

The hooks live in ``web.routes.roadmap`` /
``web.routes.changelog`` and call
``marketing_export.write_roadmap_snapshot`` /
``write_changelog_snapshot``. We patch those and verify the call
counts.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from core import changelog_store, roadmap_store, user_store
from web import app as webapp


_ADMIN_PW = "pytest-publish-trigger-admin-pw"


@pytest.fixture
def admin_client():
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


def _create_roadmap_phase() -> int:
    return roadmap_store.create_phase(
        phase_key="phase-trigger",
        display_name="Trigger test",
        summary="ok",
    )


def _create_changelog_entry() -> int:
    return changelog_store.create_entry(
        title="Trigger test",
        description="body",
        category="feature",
    )


# ── Roadmap mutation hooks ───────────────────────────────────────────────


class TestRoadmapHooks:

    def test_publish_invokes_snapshot(self, admin_client):
        pid = _create_roadmap_phase()
        with patch(
            "web.routes.roadmap.marketing_export.write_roadmap_snapshot",
        ) as mock:
            r = admin_client.post(f"/api/admin/roadmap/{pid}/publish")
        assert r.status_code == 200
        assert mock.call_count == 1

    def test_unpublish_invokes_snapshot(self, admin_client):
        pid = _create_roadmap_phase()
        roadmap_store.publish_phase(pid)
        with patch(
            "web.routes.roadmap.marketing_export.write_roadmap_snapshot",
        ) as mock:
            r = admin_client.post(f"/api/admin/roadmap/{pid}/unpublish")
        assert r.status_code == 200
        assert mock.call_count == 1

    def test_patch_invokes_snapshot(self, admin_client):
        pid = _create_roadmap_phase()
        with patch(
            "web.routes.roadmap.marketing_export.write_roadmap_snapshot",
        ) as mock:
            r = admin_client.patch(
                f"/api/admin/roadmap/{pid}",
                json={"display_name": "Updated"},
            )
        assert r.status_code == 200
        assert mock.call_count == 1

    def test_delete_invokes_snapshot(self, admin_client):
        pid = _create_roadmap_phase()
        with patch(
            "web.routes.roadmap.marketing_export.write_roadmap_snapshot",
        ) as mock:
            r = admin_client.delete(f"/api/admin/roadmap/{pid}")
        assert r.status_code == 204
        assert mock.call_count == 1

    def test_reorder_invokes_snapshot(self, admin_client):
        pid_a = _create_roadmap_phase()
        pid_b = roadmap_store.create_phase(
            phase_key="phase-trigger-b",
            display_name="b",
            summary="ok",
        )
        with patch(
            "web.routes.roadmap.marketing_export.write_roadmap_snapshot",
        ) as mock:
            r = admin_client.post(
                "/api/admin/roadmap/reorder",
                json={"ids": [pid_b, pid_a]},
            )
        assert r.status_code == 200
        assert mock.call_count == 1

    def test_create_does_not_invoke_snapshot(self, admin_client):
        # Create makes a draft — no public effect, so the hook
        # should NOT fire on create.
        with patch(
            "web.routes.roadmap.marketing_export.write_roadmap_snapshot",
        ) as mock:
            r = admin_client.post(
                "/api/admin/roadmap",
                json={
                    "phase_key": "phase-create-only",
                    "display_name": "Created",
                    "summary": "draft",
                },
            )
        assert r.status_code == 201
        assert mock.call_count == 0


# ── Changelog mutation hooks ─────────────────────────────────────────────


class TestChangelogHooks:

    def test_publish_invokes_snapshot(self, admin_client):
        eid = _create_changelog_entry()
        with patch(
            "web.routes.changelog.marketing_export.write_changelog_snapshot",
        ) as mock:
            r = admin_client.post(f"/api/admin/changelog/{eid}/publish")
        assert r.status_code == 200
        assert mock.call_count == 1

    def test_unpublish_invokes_snapshot(self, admin_client):
        eid = _create_changelog_entry()
        changelog_store.publish_entry(eid)
        with patch(
            "web.routes.changelog.marketing_export.write_changelog_snapshot",
        ) as mock:
            r = admin_client.post(f"/api/admin/changelog/{eid}/unpublish")
        assert r.status_code == 200
        assert mock.call_count == 1

    def test_patch_invokes_snapshot(self, admin_client):
        eid = _create_changelog_entry()
        with patch(
            "web.routes.changelog.marketing_export.write_changelog_snapshot",
        ) as mock:
            r = admin_client.patch(
                f"/api/admin/changelog/{eid}",
                json={"title": "Updated"},
            )
        assert r.status_code == 200
        assert mock.call_count == 1

    def test_delete_invokes_snapshot(self, admin_client):
        eid = _create_changelog_entry()
        with patch(
            "web.routes.changelog.marketing_export.write_changelog_snapshot",
        ) as mock:
            r = admin_client.delete(f"/api/admin/changelog/{eid}")
        assert r.status_code == 204
        assert mock.call_count == 1

    def test_create_does_not_invoke_snapshot(self, admin_client):
        with patch(
            "web.routes.changelog.marketing_export.write_changelog_snapshot",
        ) as mock:
            r = admin_client.post(
                "/api/admin/changelog",
                json={
                    "title": "Draft only",
                    "description": "body",
                    "category": "feature",
                },
            )
        assert r.status_code == 201
        assert mock.call_count == 0


# ── Defense-in-depth: snapshot failure does NOT fail the endpoint ────────


class TestEndpointSurvivesSnapshotFailure:
    """The wrapper hook in routes catches ANY exception from the
    snapshot writer (including unexpected ones the writer's own
    try/except did not catch — e.g. a future NameError or
    ImportError regression). Verify a publish still 200s when the
    writer raises."""

    def test_roadmap_publish_returns_200_even_when_snapshot_raises(
        self, admin_client,
    ):
        pid = _create_roadmap_phase()
        with patch(
            "web.routes.roadmap.marketing_export.write_roadmap_snapshot",
            side_effect=RuntimeError("synthetic regression"),
        ):
            r = admin_client.post(f"/api/admin/roadmap/{pid}/publish")
        assert r.status_code == 200, r.text

    def test_changelog_publish_returns_200_even_when_snapshot_raises(
        self, admin_client,
    ):
        eid = _create_changelog_entry()
        with patch(
            "web.routes.changelog.marketing_export.write_changelog_snapshot",
            side_effect=RuntimeError("synthetic regression"),
        ):
            r = admin_client.post(f"/api/admin/changelog/{eid}/publish")
        assert r.status_code == 200, r.text
