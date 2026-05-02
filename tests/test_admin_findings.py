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
- "Showing X of Y" filter-aware count indicator structurally present
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


# ── v26 / v27 / v27-backlog extension ──────────────────────────────────────


class TestV26V27SeedExtension:
    """Extension PR (feat/findings-seed-v26-v27-extension): the seed
    grew from 240 entries / 8 sources to 280 entries / 11 sources by
    folding in v26-report (25), v27-report (12), and v27-backlog (3).

    Class-of-issue regression: a future PR that drops or renames one
    of these sources, or that breaks the v26 ↔ v27 carry-over linkage,
    would silently degrade the admin tracker without an obvious error.
    These tests pin the contract.
    """

    @staticmethod
    def _by_source(items, source):
        return [f for f in items if f["source_doc"] == source]

    def test_seed_has_v26_v27_and_backlog_sources_with_expected_counts(self):
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        items = raw["findings"]
        assert len(self._by_source(items, "v26-report")) == 25, (
            "v26-report seed should carry 25 detailed-section findings "
            "(v26-23 has no detailed section and is intentionally skipped)"
        )
        assert len(self._by_source(items, "v27-report")) == 12
        assert len(self._by_source(items, "v27-backlog")) == 3

    def test_total_seed_count_matches_documented_total(self):
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        items = raw["findings"]
        assert len(items) == 314, (
            f"seed total drifted from 285: got {len(items)}. Update the "
            "header comment in data/findings_seed.yaml in the same PR."
        )

    def test_v27_carry_overs_share_resolution_ref_with_v26_parent(self):
        """v27-02 carries v26-02; v27-03 carries v26-16. The carry-over
        rows MUST share the parent's resolution_ref so an operator
        scanning the tracker sees consistent closure detail across both
        audits — and a sweep PR that retitles the parent's branch can't
        leave the carry-over pointing at a stale branch name.
        """
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        by_id = {f["finding_id"]: f for f in raw["findings"]}
        assert by_id["v27-02"]["resolution_ref"] == by_id["v26-02"]["resolution_ref"]
        assert by_id["v27-03"]["resolution_ref"] == by_id["v26-16"]["resolution_ref"]

    def test_resolved_findings_have_resolution_ref_filled(self):
        """Every entry with status=resolved MUST carry a non-empty
        resolution_ref pointing at the closing branch / commit. An
        operator looking at a resolved finding without a ref has no
        way to navigate from the tracker back to the closure work.
        Carved out: r1-077 onwards may legitimately be resolved-by-
        deletion (no branch); for the v26+v27+backlog rows we hold
        the stricter contract.
        """
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        new_sources = {"v26-report", "v27-report", "v27-backlog"}
        new_resolved = [
            f
            for f in raw["findings"]
            if f["source_doc"] in new_sources and f["status"] == "resolved"
        ]
        # Floor guards against an empty filter passing vacuously after
        # a future refactor that drops the extension entries.
        assert len(new_resolved) >= 20, (
            "expected ≥ 20 resolved findings across the v26+v27+backlog "
            f"sources; got {len(new_resolved)} — the extension may have "
            "been reverted"
        )
        offenders = [f["finding_id"] for f in new_resolved if not f.get("resolution_ref")]
        assert offenders == [], (
            "v26/v27 seed entries with status=resolved but no "
            f"resolution_ref: {offenders}"
        )

    def test_v27_open_findings_match_grep_verified_set(self):
        """Pre-analysis correction guard. v27-baseline narrative said
        most of v27 was PRE-EXISTING / unfixed, but grep against HEAD
        showed 9 of 12 had already landed. The
        fix/v27-09-v27-12-defense-in-depth PR closed the remaining two
        actionable items, leaving v27-07 (CSP `style-src 'unsafe-inline'`,
        acknowledged-as-necessary while the SPA emits inline styles)
        as the sole open finding. Pin that exact one-element set so a
        future PR that opens or closes any v27 finding without syncing
        the seed fails this assertion.
        """
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        v27 = [f for f in raw["findings"] if f["source_doc"] == "v27-report"]
        open_ids = sorted(f["finding_id"] for f in v27 if f["status"] == "open")
        assert open_ids == ["v27-07"], (
            f"v27 open-set drifted: {open_ids} — re-run the grep "
            "verification or sync data/findings_seed.yaml."
        )


# ── PT-v3 extension (Phase B auth-stack adversarial review) ────────────────


class TestPTv3SeedExtension:
    """PT-v3 produced 5 findings (pt-101, pt-102, pt-130, pt-150,
    pt-160) all status=open at the time of seeding. These tests pin
    the contract so a future PR that fixes one of them MUST sync the
    seed YAML's status / resolution_ref alongside the code change —
    otherwise the open-set assertion below fails."""

    def test_ptv3_findings_present_in_yaml(self):
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        ptv3 = [
            f for f in raw["findings"]
            if f["source_doc"] == "production-pentest-v3"
        ]
        assert len(ptv3) == 5
        assert {f["finding_id"] for f in ptv3} == {
            "pt-101", "pt-102", "pt-130", "pt-150", "pt-160",
        }

    def test_ptv3_findings_status_matches_known_resolution_set(self):
        """At seed-time none of the PT-v3 findings had been fixed.
        ``fix/phase-4-readiness-security-cluster`` (2026-04-29) closed
        pt-101 and pt-160 — both must carry status=resolved + a
        resolution_ref. The remaining three (pt-102, pt-130, pt-150)
        are still open with resolution_ref=None at the time of writing.

        Forces the seed YAML to stay in sync with reality — a future
        PR that fixes pt-130 (e.g. session-epoch bump on TOTP toggle)
        must sync this expected-state map alongside the code change.
        """
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        ptv3 = {
            f["finding_id"]: f
            for f in raw["findings"]
            if f["source_doc"] == "production-pentest-v3"
        }
        assert len(ptv3) == 5

        expected: dict[str, tuple[str, str | None]] = {
            "pt-101": (
                "resolved", "fix/phase-4-readiness-security-cluster",
            ),
            "pt-102": ("open", None),
            "pt-130": (
                "resolved", "fix/pt-130-totp-bumps-epoch",
            ),
            "pt-150": (
                "resolved", "fix/pt-150-totp-admin-reset-wrapper",
            ),
            "pt-160": (
                "resolved", "fix/phase-4-readiness-security-cluster",
            ),
        }
        offenders = [
            (fid, ptv3[fid]["status"], ptv3[fid]["resolution_ref"])
            for fid, (want_status, want_ref) in expected.items()
            if (
                ptv3[fid]["status"] != want_status
                or ptv3[fid]["resolution_ref"] != want_ref
            )
        ]
        assert offenders == [], (
            f"PT-v3 findings drifted from expected status set: "
            f"{offenders}. If you fixed one, update its status to "
            "resolved + fill resolution_ref AND update the expected "
            "map in this test. If you intentionally accepted one, "
            "set status=accepted with a notes field and update the "
            "map."
        )

    def test_ptv3_severity_distribution_matches_pentest_doc(self):
        """The PT-v3 markdown report has a severity table claiming
        1 MEDIUM + 3 LOW + 1 INFO across the 5 findings. Pin that
        distribution so a careless edit to the seed (e.g. promoting
        an INFO to MEDIUM without updating the doc) breaks here."""
        from collections import Counter
        raw = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
        ptv3 = [
            f for f in raw["findings"]
            if f["source_doc"] == "production-pentest-v3"
        ]
        counts = Counter(f["severity"] for f in ptv3)
        assert counts == Counter({"LOW": 3, "MEDIUM": 1, "INFO": 1}), (
            f"PT-v3 severity distribution drifted: {dict(counts)}. "
            "Cross-check with docs/pentests/production-pentest-v3.md "
            "Hypothesis Summary table — both must agree."
        )


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


# ── Audit-log format consistency ──────────────────────────────────────────

class TestAuditLogFormat:
    """The PATCH /api/admin/findings/{id} audit entry must match the
    standard ``_audit(action, slug, actor, user_id=N)`` shape used by
    every other mutating route (bot_start, exchange_keys_set, etc.):

      slug = finding_id          (target identifier)
      user = "session:<username>" (actor — produced by _request_actor)
      user_id = admin user id

    The pre-fix call had ``slug`` and ``user`` swapped: ``slug`` got
    the admin's username and ``user`` got a hand-built "id=X status=Y"
    detail string. The mismatch broke any audit-log grep keyed on
    finding-id and any per-actor rollup keyed on the user field.
    """

    def test_audit_log_uses_finding_id_as_slug_and_session_actor(
        self, admin_client, seeded_db, tmp_path, monkeypatch,
    ):
        from core import paths
        # Redirect LOG_DIR + per-user dir to tmp so we can read the
        # audit.jsonl line this PATCH writes without polluting the
        # operator's logs/ tree. Same fixture shape as
        # tests/test_audit_log.py uses.
        monkeypatch.setattr(webapp, "LOG_DIR", tmp_path)
        monkeypatch.setattr(paths, "BASE_DIR", tmp_path)

        # Force a state change so the audit branch fires (the route
        # only audits when audit_findings_store.update_finding
        # returned ``changed=True``).
        r = admin_client.patch(
            "/api/admin/findings/r3-001",
            json={"notes": "audit-format regression check"},
        )
        assert r.status_code == 200

        global_jsonl = tmp_path / "audit.jsonl"
        assert global_jsonl.exists(), (
            "audit.jsonl never written — PATCH didn't reach _audit()"
        )
        import json as _json
        lines = global_jsonl.read_text(encoding="utf-8").strip().splitlines()
        # Walk backwards for the most recent admin_finding_update
        # entry (other tests in the same suite may have written
        # entries after we set up the redirect).
        entry = None
        for raw in reversed(lines):
            candidate = _json.loads(raw)
            if candidate.get("action") == "admin_finding_update":
                entry = candidate
                break
        assert entry is not None, (
            "no admin_finding_update entry found in audit.jsonl"
        )

        # The actual format-consistency assertions.
        assert entry["slug"] == "r3-001", (
            f"slug must be the finding_id (target), not the actor — "
            f"got {entry['slug']!r}"
        )
        assert entry["user"] == "session:admin", (
            f"user must be the actor 'session:<username>' shape, not "
            f"a status detail string — got {entry['user']!r}"
        )
        # user_id should be the admin's id (1 in the test fixture).
        admin_user = user_store.get_user_by_username("admin")
        assert entry["user_id"] == admin_user.id


# ── "Showing X of Y" filter-aware indicator ───────────────────────────────

class TestShowingIndicator:
    """The findings tracker shows a compact "Showing X of Y" line
    that appears only when at least one filter is active. It sits
    next to the always-visible global stats roll-up.

    These are source-grep tests rather than browser-driven: there's
    no JS test harness in the project (see tests/test_frontend_assets.py
    for the same pattern). A future regression that drops the
    element, the toggle helper, or the no-filter hide-branch fails
    here before it lands in front of an operator.
    """

    def test_showing_indicator_element_present(self):
        index_html = (
            Path(__file__).resolve().parent.parent
            / "web" / "static" / "index.html"
        )
        html = index_html.read_text(encoding="utf-8")
        assert 'id="findings-showing"' in html
        assert 'class="findings-showing' in html
        # Default state must include `hidden` so a fresh page-load
        # without any filter does not flash the indicator before the
        # JS toggles it off.
        assert 'class="findings-showing hidden"' in html

    def test_showing_indicator_logic_present(self):
        app_js = (
            Path(__file__).resolve().parent.parent
            / "web" / "static" / "app.js"
        )
        js = app_js.read_text(encoding="utf-8")
        # Helper must exist and be wired up.
        assert "_updateShowingIndicator" in js
        # The "filter active?" check must compose all three filter
        # selects so a partial composition (e.g. only checking
        # status) does not silently drop coverage when a future
        # source-doc-only filter is set.
        assert "findings-filter-status" in js
        assert "findings-filter-severity" in js
        assert "findings-filter-source" in js
        # No-filter hide branch must exist so the indicator does
        # not stick around stale after the operator clears filters.
        # Carve out the helper body so we don't false-positive on
        # ``classList.add('hidden')`` calls elsewhere in app.js.
        fn_start = js.index("function _updateShowingIndicator(")
        fn_end = js.index("\nfunction ", fn_start + 1)
        fn_body = js[fn_start:fn_end]
        assert "hasActiveFilter" in fn_body
        assert "classList.add('hidden')" in fn_body
        assert "classList.remove('hidden')" in fn_body
        # Output format must follow the operator-spec phrasing.
        assert "Showing" in fn_body
        assert " of " in fn_body
