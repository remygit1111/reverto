"""Tests for ``core.marketing_export``.

Covers:
  * Happy-path snapshot writes for both roadmap + changelog.
  * Atomic-write semantics (.tmp → rename, no partial files).
  * Field-stripping: admin-only columns must not leak into the
    public snapshots, raw markdown is replaced with bleach-rendered
    HTML.
  * Failure paths return False instead of raising — callers must
    not block their own DB mutation on a snapshot failure.

The autouse ``_isolate_marketing_export`` fixture in conftest.py
redirects the data-dir at a tmp_path per test, so these tests do
not need their own monkeypatch.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import changelog_store, marketing_export, roadmap_store


def _data_dir() -> Path:
    """Resolve the data dir the autouse fixture set."""
    override = os.environ.get("REVERTO_MARKETING_DATA_DIR")
    assert override, "conftest fixture should have set this"
    return Path(override)


# ── Roadmap snapshot ─────────────────────────────────────────────────────


class TestRoadmapSnapshot:

    def test_writes_published_phase(self):
        pid = roadmap_store.create_phase(
            phase_key="phase-published",
            display_name="Visible",
            summary="Public phase.",
        )
        roadmap_store.publish_phase(pid)

        ok = marketing_export.write_roadmap_snapshot()
        assert ok is True

        path = _data_dir() / "roadmap.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "phases" in data
        assert len(data["phases"]) == 1
        assert data["phases"][0]["phase_key"] == "phase-published"

    def test_drafts_omitted(self):
        # Draft (created but not published).
        roadmap_store.create_phase(
            phase_key="phase-draft",
            display_name="Draft only",
            summary="Should not surface.",
        )
        # Published.
        pid = roadmap_store.create_phase(
            phase_key="phase-live",
            display_name="Live",
            summary="Visible.",
        )
        roadmap_store.publish_phase(pid)

        marketing_export.write_roadmap_snapshot()
        data = json.loads((_data_dir() / "roadmap.json").read_text("utf-8"))
        keys = {p["phase_key"] for p in data["phases"]}
        assert keys == {"phase-live"}

    def test_strips_admin_only_fields(self):
        pid = roadmap_store.create_phase(
            phase_key="phase-strip",
            display_name="Strip test",
            summary="Field-stripping check.",
        )
        roadmap_store.publish_phase(pid)
        marketing_export.write_roadmap_snapshot()

        data = json.loads((_data_dir() / "roadmap.json").read_text("utf-8"))
        phase = data["phases"][0]
        # Admin-only fields must NOT appear publicly.
        for forbidden in ("id", "is_published", "created_at", "updated_at"):
            assert forbidden not in phase, (
                f"{forbidden!r} leaked into the marketing snapshot"
            )
        # Public fields must appear.
        for required in (
            "phase_key", "display_name", "summary", "status",
            "sort_order", "body_md", "body_html", "effort_estimate",
            "in_progress_note", "audit_checkpoint", "published_at",
        ):
            assert required in phase, f"{required!r} missing from snapshot"

    def test_body_html_pre_rendered(self):
        pid = roadmap_store.create_phase(
            phase_key="phase-md",
            display_name="MD test",
            summary="Body has markdown.",
            body_md="**bold** then\n\n- item one\n- item two",
        )
        roadmap_store.publish_phase(pid)
        marketing_export.write_roadmap_snapshot()

        data = json.loads((_data_dir() / "roadmap.json").read_text("utf-8"))
        phase = data["phases"][0]
        assert "<strong>bold</strong>" in phase["body_html"]
        assert "<li>item one</li>" in phase["body_html"]
        assert phase["body_md"].startswith("**bold**")

    def test_empty_when_no_published_phases(self):
        ok = marketing_export.write_roadmap_snapshot()
        assert ok is True
        data = json.loads((_data_dir() / "roadmap.json").read_text("utf-8"))
        assert data == {"phases": []}


# ── Changelog snapshot ───────────────────────────────────────────────────


class TestChangelogSnapshot:

    def test_writes_published_entry(self):
        eid = changelog_store.create_entry(
            title="Visible entry",
            description="Public.",
            category="feature",
        )
        changelog_store.publish_entry(eid)

        ok = marketing_export.write_changelog_snapshot()
        assert ok is True

        path = _data_dir() / "changelog.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "entries" in data
        assert len(data["entries"]) == 1
        assert data["entries"][0]["title"] == "Visible entry"

    def test_filters_drafts(self):
        # Draft.
        changelog_store.create_entry(
            title="Draft entry",
            description="hidden",
            category="fix",
        )
        # Published.
        eid = changelog_store.create_entry(
            title="Published entry",
            description="visible",
            category="fix",
        )
        changelog_store.publish_entry(eid)

        marketing_export.write_changelog_snapshot()
        data = json.loads((_data_dir() / "changelog.json").read_text("utf-8"))
        titles = {e["title"] for e in data["entries"]}
        assert titles == {"Published entry"}

    def test_strips_admin_fields(self):
        eid = changelog_store.create_entry(
            title="Strip check",
            description="hidden raw markdown",
            category="security",
        )
        changelog_store.publish_entry(eid)
        marketing_export.write_changelog_snapshot()

        data = json.loads((_data_dir() / "changelog.json").read_text("utf-8"))
        entry = data["entries"][0]
        # Admin-only / raw-markdown fields must not appear.
        for forbidden in (
            "is_published", "created_at",
            "description", "source_commit_sha",
        ):
            assert forbidden not in entry, (
                f"{forbidden!r} leaked into the marketing snapshot"
            )
        # Public fields must appear.
        for required in (
            "id", "title", "category", "published_at",
            "description_html",
        ):
            assert required in entry, f"{required!r} missing"


# ── Atomic write semantics ──────────────────────────────────────────────


class TestAtomicWrite:

    def test_no_tmp_left_behind_on_success(self):
        eid = changelog_store.create_entry(
            title="atomic-ok", description="x", category="feature",
        )
        changelog_store.publish_entry(eid)
        marketing_export.write_changelog_snapshot()
        # No .tmp residue should exist after success.
        assert not (_data_dir() / "changelog.json.tmp").exists()
        assert (_data_dir() / "changelog.json").exists()

    def test_tmp_cleaned_up_on_replace_failure(self):
        eid = changelog_store.create_entry(
            title="atomic-cleanup", description="x", category="fix",
        )
        changelog_store.publish_entry(eid)

        # Force os.replace to raise — tmp file should still get
        # cleaned up so the directory doesn't accumulate cruft.
        with patch.object(
            marketing_export.os, "replace",
            side_effect=OSError("boom"),
        ):
            ok = marketing_export.write_changelog_snapshot()
        assert ok is False
        assert not (_data_dir() / "changelog.json.tmp").exists()


# ── Failure handling ─────────────────────────────────────────────────────


class TestFailureHandling:

    def test_permission_error_returns_false_no_raise(self, monkeypatch):
        # Point the data dir at a path that cannot be created.
        monkeypatch.setenv(
            "REVERTO_MARKETING_DATA_DIR",
            "/proc/this-cannot-be-mkdir-target/data",
        )
        ok = marketing_export.write_roadmap_snapshot()
        assert ok is False

    def test_store_exception_returns_false_no_raise(self):
        with patch.object(
            roadmap_store, "list_published",
            side_effect=RuntimeError("DB exploded"),
        ):
            ok = marketing_export.write_roadmap_snapshot()
        assert ok is False

    def test_changelog_store_exception_returns_false_no_raise(self):
        with patch.object(
            changelog_store, "list_published",
            side_effect=RuntimeError("DB exploded"),
        ):
            ok = marketing_export.write_changelog_snapshot()
        assert ok is False


# ── write_all_snapshots ──────────────────────────────────────────────────


class TestWriteAllSnapshots:

    def test_returns_dict_with_both_keys(self):
        results = marketing_export.write_all_snapshots()
        assert set(results.keys()) == {"roadmap", "changelog"}
        assert results == {"roadmap": True, "changelog": True}

    def test_independent_failures(self):
        # Roadmap fails; changelog should still attempt + succeed.
        with patch.object(
            roadmap_store, "list_published",
            side_effect=RuntimeError("roadmap broken"),
        ):
            results = marketing_export.write_all_snapshots()
        assert results == {"roadmap": False, "changelog": True}
        # Changelog file written.
        assert (_data_dir() / "changelog.json").exists()
