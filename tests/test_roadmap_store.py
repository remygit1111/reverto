"""Tests for core.roadmap_store — DB-backed roadmap-phase CRUD.

The autouse ``_isolate_reverto_db`` fixture from conftest.py gives
every test a fresh SQLite file with schema v10 applied, so the
``roadmap_phases`` table exists and is empty on each test entry.

Mirrors tests/test_changelog_store.py conventions: arrange → act
→ assert per test, no shared mutable state, validation paths
asserted via ``pytest.raises(ValueError, match=...)``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import roadmap_store


_KEY = "phase-1"
_NAME = "Foundation"
_SUMMARY = "Multi-bot architecture, paper engine, exchange abstraction."


def _create(**overrides) -> int:
    payload = {
        "phase_key": _KEY,
        "display_name": _NAME,
        "summary": _SUMMARY,
    }
    payload.update(overrides)
    return roadmap_store.create_phase(**payload)


class TestCreate:

    def test_create_returns_positive_int(self):
        pid = _create()
        assert isinstance(pid, int)
        assert pid > 0

    def test_create_round_trip_defaults_to_draft(self):
        pid = _create()
        phase = roadmap_store.get_phase(pid)
        assert phase is not None
        assert phase["phase_key"] == _KEY
        assert phase["display_name"] == _NAME
        assert phase["summary"] == _SUMMARY
        # Defaults
        assert phase["status"] == "pending"
        assert phase["sort_order"] == 0
        assert phase["body_md"] == ""
        assert phase["is_published"] is False
        assert phase["published_at"] is None
        # Timestamps land on insert.
        assert phase["created_at"]
        assert phase["updated_at"]

    def test_create_strips_leading_trailing_whitespace(self):
        pid = _create(
            display_name="   Padded name   ", summary="\n\n  body  \n",
        )
        phase = roadmap_store.get_phase(pid)
        assert phase["display_name"] == "Padded name"
        assert phase["summary"] == "body"

    def test_create_rejects_invalid_status(self):
        with pytest.raises(ValueError, match="Invalid roadmap status"):
            _create(status="wat")

    def test_create_rejects_uppercase_phase_key(self):
        with pytest.raises(ValueError, match=r"\[a-z0-9-\]"):
            _create(phase_key="Phase-Two")

    def test_create_rejects_phase_key_with_spaces(self):
        with pytest.raises(ValueError, match=r"\[a-z0-9-\]"):
            _create(phase_key="phase two")

    def test_create_rejects_empty_phase_key(self):
        with pytest.raises(ValueError, match="phase_key must not be empty"):
            _create(phase_key="   ")

    def test_create_rejects_empty_display_name(self):
        with pytest.raises(ValueError, match="display_name must not be empty"):
            _create(display_name="   ")

    def test_create_rejects_empty_summary(self):
        with pytest.raises(ValueError, match="summary must not be empty"):
            _create(summary="")

    def test_create_rejects_oversized_display_name(self):
        with pytest.raises(ValueError, match="display_name exceeds"):
            _create(display_name="x" * (roadmap_store.MAX_DISPLAY_NAME_LEN + 1))

    def test_create_rejects_oversized_summary(self):
        with pytest.raises(ValueError, match="summary exceeds"):
            _create(summary="x" * (roadmap_store.MAX_SUMMARY_LEN + 1))

    def test_create_rejects_oversized_body(self):
        with pytest.raises(ValueError, match="body_md exceeds"):
            _create(body_md="x" * (roadmap_store.MAX_BODY_LEN + 1))

    def test_create_rejects_oversized_phase_key(self):
        with pytest.raises(ValueError, match="phase_key exceeds"):
            # All-dashes is technically still in the regex set,
            # but length-cap comes first.
            _create(phase_key="a" * (roadmap_store.MAX_PHASE_KEY_LEN + 1))

    def test_create_rejects_duplicate_phase_key(self):
        _create()
        with pytest.raises(roadmap_store.RoadmapPhaseKeyConflict):
            _create()

    def test_duplicate_conflict_is_value_error_subclass(self):
        """Route handlers that catch ValueError keep working —
        the more specific class is for callers that want to
        distinguish dup-key from other validation failures."""
        assert issubclass(
            roadmap_store.RoadmapPhaseKeyConflict, ValueError,
        )


class TestRead:

    def test_get_returns_none_for_missing_id(self):
        assert roadmap_store.get_phase(999) is None

    def test_get_by_key_returns_none_for_missing(self):
        assert roadmap_store.get_phase_by_key("nope") is None

    def test_get_by_key_round_trip(self):
        _create()
        phase = roadmap_store.get_phase_by_key(_KEY)
        assert phase is not None
        assert phase["phase_key"] == _KEY


class TestList:

    def test_list_published_excludes_drafts(self):
        _create(phase_key="phase-a")
        _create(phase_key="phase-b", display_name="B", summary="B sum")
        # Neither published.
        assert roadmap_store.list_published() == []

    def test_list_published_includes_published(self):
        pid = _create()
        roadmap_store.publish_phase(pid)
        result = roadmap_store.list_published()
        assert len(result) == 1
        assert result[0]["phase_key"] == _KEY

    def test_list_published_orders_by_sort_order(self):
        # Insert in scrambled order; published list should sort
        # by sort_order ASC.
        a = _create(phase_key="phase-a", display_name="A", summary="A", sort_order=30)
        b = _create(phase_key="phase-b", display_name="B", summary="B", sort_order=10)
        c = _create(phase_key="phase-c", display_name="C", summary="C", sort_order=20)
        for pid in (a, b, c):
            roadmap_store.publish_phase(pid)
        result = roadmap_store.list_published()
        assert [p["phase_key"] for p in result] == [
            "phase-b", "phase-c", "phase-a",
        ]

    def test_list_all_includes_drafts(self):
        # ``a`` is intentionally created and never referenced
        # again — the assertion below confirms list_all returns
        # both rows by phase_key, which pins that drafts are
        # NOT filtered out.
        _create(phase_key="phase-a", display_name="A", summary="A")
        b = _create(phase_key="phase-b", display_name="B", summary="B")
        roadmap_store.publish_phase(b)
        result = roadmap_store.list_all()
        keys = {p["phase_key"] for p in result}
        assert keys == {"phase-a", "phase-b"}


class TestUpdate:

    def test_update_partial_only_provided_fields(self):
        pid = _create(status="pending", sort_order=10)
        roadmap_store.update_phase(pid, {"status": "active"})
        phase = roadmap_store.get_phase(pid)
        assert phase["status"] == "active"
        assert phase["sort_order"] == 10  # untouched

    def test_update_returns_false_for_missing_id(self):
        assert roadmap_store.update_phase(999, {"status": "active"}) is False

    def test_update_ignores_phase_key(self):
        """phase_key is immutable post-create — a payload that
        includes it must NOT change the row's key."""
        pid = _create()
        # Even if a buggy caller passes phase_key in payload, the
        # store's allow-list of editable fields means it never
        # reaches the SQL.
        roadmap_store.update_phase(pid, {
            "phase_key": "different-key",
            "display_name": "New name",
        })
        phase = roadmap_store.get_phase(pid)
        assert phase["phase_key"] == _KEY  # original
        assert phase["display_name"] == "New name"

    def test_update_rejects_invalid_status(self):
        pid = _create()
        with pytest.raises(ValueError, match="Invalid roadmap status"):
            roadmap_store.update_phase(pid, {"status": "wat"})

    def test_update_rejects_oversized_field(self):
        pid = _create()
        with pytest.raises(ValueError, match="display_name exceeds"):
            roadmap_store.update_phase(pid, {
                "display_name": "x" * (roadmap_store.MAX_DISPLAY_NAME_LEN + 1),
            })

    def test_update_with_empty_payload_returns_truthy_for_existing(self):
        """An empty / no-op payload must not be conflated with
        "row not found" — the UI distinguishes both states."""
        pid = _create()
        assert roadmap_store.update_phase(pid, {}) is True


class TestPublishUnpublish:

    def test_publish_sets_published_at(self):
        pid = _create()
        before = roadmap_store.get_phase(pid)
        assert before["published_at"] is None
        roadmap_store.publish_phase(pid)
        after = roadmap_store.get_phase(pid)
        assert after["is_published"] is True
        assert after["published_at"] is not None

    def test_publish_idempotent_preserves_published_at(self):
        """Audit-trail contract: re-publishing must NOT re-stamp
        the timestamp. published_at = "first time this went
        public", not "most recent edit"."""
        pid = _create()
        roadmap_store.publish_phase(pid)
        first_ts = roadmap_store.get_phase(pid)["published_at"]
        # Second publish should keep the original timestamp.
        roadmap_store.publish_phase(pid)
        second_ts = roadmap_store.get_phase(pid)["published_at"]
        assert first_ts == second_ts

    def test_unpublish_preserves_published_at(self):
        """Re-publish-after-unpublish must carry the original
        first-publish date forward."""
        pid = _create()
        roadmap_store.publish_phase(pid)
        original_ts = roadmap_store.get_phase(pid)["published_at"]
        roadmap_store.unpublish_phase(pid)
        phase = roadmap_store.get_phase(pid)
        assert phase["is_published"] is False
        assert phase["published_at"] == original_ts

    def test_publish_returns_false_for_missing(self):
        assert roadmap_store.publish_phase(999) is False


class TestDelete:

    def test_delete_removes_row(self):
        pid = _create()
        assert roadmap_store.delete_phase(pid) is True
        assert roadmap_store.get_phase(pid) is None

    def test_delete_returns_false_for_missing(self):
        assert roadmap_store.delete_phase(999) is False


class TestReorder:

    def test_reorder_assigns_multiples_of_10(self):
        a = _create(phase_key="phase-a", display_name="A", summary="A", sort_order=99)
        b = _create(phase_key="phase-b", display_name="B", summary="B", sort_order=42)
        c = _create(phase_key="phase-c", display_name="C", summary="C", sort_order=7)
        roadmap_store.reorder_phases([a, b, c])
        sorts = {
            p["phase_key"]: p["sort_order"]
            for p in roadmap_store.list_all()
        }
        # Multiples of 10 in the order provided.
        assert sorts == {"phase-a": 10, "phase-b": 20, "phase-c": 30}

    def test_reorder_changes_visible_order_in_list_published(self):
        a = _create(phase_key="phase-a", display_name="A", summary="A")
        b = _create(phase_key="phase-b", display_name="B", summary="B")
        c = _create(phase_key="phase-c", display_name="C", summary="C")
        for pid in (a, b, c):
            roadmap_store.publish_phase(pid)
        # Reverse order via reorder.
        roadmap_store.reorder_phases([c, b, a])
        result = roadmap_store.list_published()
        assert [p["phase_key"] for p in result] == [
            "phase-c", "phase-b", "phase-a",
        ]

    def test_reorder_empty_list_is_noop(self):
        _create()
        # Should not raise.
        roadmap_store.reorder_phases([])

    def test_reorder_skips_unknown_ids(self):
        """An id that doesn't exist is silently skipped — callers
        that need strict semantics pre-validate via get_phase."""
        pid = _create()
        roadmap_store.reorder_phases([pid, 9999])
        phase = roadmap_store.get_phase(pid)
        assert phase["sort_order"] == 10
