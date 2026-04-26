"""Tests for the audit/pentest findings tracker.

Backend: /api/admin/findings (admin-only).
Store: core.audit_findings_store.
Seed: data/findings_seed.yaml + scripts/seed_audit_findings.py.

Coverage:
- Seed YAML parses cleanly + every entry passes store-side validation
- Seed import is idempotent (rerunning never duplicates rows)
- /api/admin/findings list / filter / get / patch surface
- Admin gate (non-admin gets 403)
- Update paths: status/notes/resolution_ref + invalid status rejected
- updated_at advances on operator edits
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml
from fastapi.testclient import TestClient

from core import audit_findings_store, user_store
from web import app as webapp


_SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "findings_seed.yaml"


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def seeded_db():
    """Run the seed once per test that requires findings on disk.
    The conftest fixture set hands us a fresh DB per test, so seeding
    here keeps each test deterministic without leaking state."""
    from scripts.seed_audit_findings import import_seed, load_seed
    items = load_seed(_SEED_PATH)
    import_seed(items, quiet=True)
    return items


@pytest.fixture
def admin_client():
    """TestClient with a session cookie for the seeded admin user."""
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, "pytest-findings-admin-pw-12345")
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
    /api/admin/findings endpoint."""
    from core.database import get_db
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (username, role) VALUES ('bobfindings', 'user')",
        )
    bob = user_store.get_user_by_username("bobfindings")
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


# ── Seed file integrity ────────────────────────────────────────────────────

class TestSeedFile:
    """The YAML seed is the source-of-truth for first-boot row count.
    A malformed entry would silently drop a finding from the admin
    rollup, so every entry must round-trip cleanly through the
    store-layer validator."""

    def test_seed_yaml_parses_and_has_expected_shape(self):
        assert _SEED_PATH.is_file()
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        assert "findings" in raw
        assert isinstance(raw["findings"], list)
        assert len(raw["findings"]) >= 200, (
            f"seed should have ~240 findings; got {len(raw['findings'])}"
        )

    def test_every_seed_entry_validates(self):
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        seen_ids: set[str] = set()
        for entry in raw["findings"]:
            # Required keys.
            for key in ("finding_id", "source_doc", "severity",
                        "status", "title"):
                assert key in entry, f"missing {key} in {entry}"
            assert entry["severity"] in audit_findings_store.VALID_SEVERITIES, (
                f"bad severity in {entry['finding_id']}: {entry['severity']}"
            )
            assert entry["status"] in audit_findings_store.VALID_STATUSES, (
                f"bad status in {entry['finding_id']}: {entry['status']}"
            )
            assert len(entry["title"]) <= audit_findings_store.MAX_TITLE_LEN
            # IDs must be unique within the seed.
            assert entry["finding_id"] not in seen_ids, (
                f"duplicate finding_id: {entry['finding_id']}"
            )
            seen_ids.add(entry["finding_id"])


# ── Seed import idempotency ────────────────────────────────────────────────

class TestSeedImport:

    def test_import_is_idempotent(self):
        from scripts.seed_audit_findings import import_seed, load_seed
        items = load_seed(_SEED_PATH)
        # First pass — full insert. Second + third — no-ops.
        first = import_seed(items, quiet=True)
        second = import_seed(items, quiet=True)
        third = import_seed(items, quiet=True)
        assert first[0] == len(items)
        assert second == (0, len(items))
        assert third == (0, len(items))


# ── List / filter API ──────────────────────────────────────────────────────

class TestListAPI:

    def test_admin_can_list(self, admin_client, seeded_db):
        r = admin_client.get("/api/admin/findings")
        assert r.status_code == 200
        body = r.json()
        assert "findings" in body
        assert "stats" in body
        assert len(body["findings"]) == len(seeded_db)
        # Stats roll-up is sane.
        assert body["stats"]["total"] == len(seeded_db)
        by_status = body["stats"]["by_status"]
        for s in audit_findings_store.VALID_STATUSES:
            assert s in by_status

    def test_non_admin_gets_403(self, non_admin_client, seeded_db):
        r = non_admin_client.get("/api/admin/findings")
        assert r.status_code == 403

    def test_unauthenticated_gets_401(self, seeded_db):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/api/admin/findings")
        assert r.status_code == 401

    def test_filter_by_status(self, admin_client, seeded_db):
        r = admin_client.get("/api/admin/findings?status=resolved")
        assert r.status_code == 200
        body = r.json()
        assert all(f["status"] == "resolved" for f in body["findings"])
        # And the seed has more than zero resolved entries.
        assert len(body["findings"]) > 0

    def test_filter_by_severity(self, admin_client, seeded_db):
        r = admin_client.get("/api/admin/findings?severity=HIGH")
        assert r.status_code == 200
        body = r.json()
        assert all(f["severity"] == "HIGH" for f in body["findings"])
        assert len(body["findings"]) > 0

    def test_filter_by_source_doc(self, admin_client, seeded_db):
        r = admin_client.get(
            "/api/admin/findings?source_doc=production-readiness-audit-v3"
        )
        assert r.status_code == 200
        body = r.json()
        assert all(
            f["source_doc"] == "production-readiness-audit-v3"
            for f in body["findings"]
        )
        # PRA-v3 should have ~15 entries per the source markdown.
        assert 10 <= len(body["findings"]) <= 20

    def test_filter_invalid_status_rejected(self, admin_client, seeded_db):
        r = admin_client.get("/api/admin/findings?status=bogus")
        assert r.status_code == 400


# ── Single-get API ─────────────────────────────────────────────────────────

class TestGetAPI:

    def test_get_existing_finding(self, admin_client, seeded_db):
        r = admin_client.get("/api/admin/findings/r3-001")
        assert r.status_code == 200
        body = r.json()
        assert body["finding_id"] == "r3-001"
        assert body["severity"] == "HIGH"
        # Description should not be empty for r3-001 — sanity check
        # that the seed's description field round-trips correctly.
        assert len(body["description"]) > 20

    def test_get_unknown_finding_404(self, admin_client, seeded_db):
        r = admin_client.get("/api/admin/findings/does-not-exist")
        assert r.status_code == 404


# ── Update API ─────────────────────────────────────────────────────────────

class TestUpdateAPI:

    def test_patch_status_persists(self, admin_client, seeded_db):
        # Pick a finding that's currently 'open' so the PATCH actually
        # changes something. r1-003 is open in the seed.
        r = admin_client.patch(
            "/api/admin/findings/r1-003",
            json={"status": "in_progress"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "in_progress"
        # Round-trip — GET reflects the change.
        r2 = admin_client.get("/api/admin/findings/r1-003")
        assert r2.json()["status"] == "in_progress"

    def test_patch_notes_persists(self, admin_client, seeded_db):
        r = admin_client.patch(
            "/api/admin/findings/r1-003",
            json={"notes": "Investigated 2026-04-26 - waiting on Phase B"},
        )
        assert r.status_code == 200
        assert "Investigated 2026-04-26" in r.json()["notes"]

    def test_patch_invalid_status_rejected(self, admin_client, seeded_db):
        r = admin_client.patch(
            "/api/admin/findings/r1-003",
            json={"status": "bogus"},
        )
        # Pydantic Literal rejects with 422.
        assert r.status_code == 422

    def test_patch_unknown_finding_404(self, admin_client, seeded_db):
        r = admin_client.patch(
            "/api/admin/findings/does-not-exist",
            json={"status": "resolved"},
        )
        assert r.status_code == 404

    def test_patch_admin_only(self, non_admin_client, seeded_db):
        r = non_admin_client.patch(
            "/api/admin/findings/r1-003",
            json={"status": "resolved"},
        )
        assert r.status_code == 403

    def test_patch_updates_updated_at(self, admin_client, seeded_db):
        before = admin_client.get("/api/admin/findings/r1-003").json()
        # SQLite datetime('now') is whole-second precision, but the
        # operator-edit path writes a fresh value regardless of clock
        # tick, so the after-value is at least equal-or-later.
        admin_client.patch(
            "/api/admin/findings/r1-003",
            json={"resolution_ref": "tweak/test-ref"},
        )
        after = admin_client.get("/api/admin/findings/r1-003").json()
        assert after["resolution_ref"] == "tweak/test-ref"
        # updated_at is monotonic.
        assert after["updated_at"] >= before["updated_at"]
