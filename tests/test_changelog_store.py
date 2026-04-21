"""Tests for core.changelog_store — DB-backed changelog CRUD.

The autouse ``_isolate_reverto_db`` fixture from conftest.py gives
every test a fresh SQLite file with schema v5 applied, so the
``changelog_entries`` table exists and is empty on each test entry.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import changelog_store
from core.database import get_db


_TITLE = "Added dark-mode toggle"
_DESCRIPTION = "You can now **toggle** dark mode from Settings."
_CATEGORY = "feature"


class TestCreateAndRead:

    def test_create_returns_positive_int(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        assert isinstance(eid, int)
        assert eid > 0

    def test_create_get_roundtrip(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        entry = changelog_store.get_entry(eid)
        assert entry is not None
        assert entry["title"] == _TITLE
        assert entry["description"] == _DESCRIPTION
        assert entry["category"] == _CATEGORY
        # Drafts on create.
        assert entry["is_published"] is False
        assert entry["published_at"] is None
        assert entry["source_commit_sha"] is None

    def test_create_strips_leading_trailing_whitespace(self):
        eid = changelog_store.create_entry(
            "   padded title   ", "\n\n  body  \n", _CATEGORY,
        )
        entry = changelog_store.get_entry(eid)
        assert entry["title"] == "padded title"
        assert entry["description"] == "body"

    def test_get_returns_none_for_unknown_id(self):
        assert changelog_store.get_entry(9999) is None

    def test_create_with_source_commit_sha(self):
        """source_commit_sha is nullable today but already persisted —
        the auto-generation PR (PR 2) will fill it in from git."""
        eid = changelog_store.create_entry(
            _TITLE, _DESCRIPTION, _CATEGORY,
            source_commit_sha="deadbeef1234",
        )
        entry = changelog_store.get_entry(eid)
        assert entry["source_commit_sha"] == "deadbeef1234"


class TestCategoryValidation:

    @pytest.mark.parametrize("cat", ["feature", "fix", "improvement", "security"])
    def test_allows_whitelisted_categories(self, cat):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, cat)
        assert changelog_store.get_entry(eid)["category"] == cat

    @pytest.mark.parametrize("cat", ["", "unknown", "FEATURE", "news", "bug"])
    def test_rejects_other_categories(self, cat):
        with pytest.raises(ValueError):
            changelog_store.create_entry(_TITLE, _DESCRIPTION, cat)

    def test_update_rejects_invalid_category(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, "feature")
        with pytest.raises(ValueError):
            changelog_store.update_entry(eid, category="spam")


class TestUpdate:

    def test_update_partial_only_touches_passed_fields(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        assert changelog_store.update_entry(eid, title="new title")
        entry = changelog_store.get_entry(eid)
        assert entry["title"] == "new title"
        # Description + category must survive the partial update.
        assert entry["description"] == _DESCRIPTION
        assert entry["category"] == _CATEGORY

    def test_update_all_fields(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        assert changelog_store.update_entry(
            eid, title="t2", description="d2", category="fix",
        )
        entry = changelog_store.get_entry(eid)
        assert entry["title"] == "t2"
        assert entry["description"] == "d2"
        assert entry["category"] == "fix"

    def test_update_with_no_fields_is_noop_but_truthful(self):
        """An update that passes None for every field shouldn't fall
        through to a ``UPDATE SET WHERE id=?`` with no columns (SQLite
        would raise). Report rowcount honestly: True if the row still
        exists, False if gone."""
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        assert changelog_store.update_entry(eid) is True
        assert changelog_store.update_entry(9999) is False

    def test_update_unknown_id_returns_false(self):
        assert changelog_store.update_entry(9999, title="x") is False

    def test_update_empty_title_raises(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        with pytest.raises(ValueError):
            changelog_store.update_entry(eid, title="   ")

    def test_update_does_not_touch_publish_state(self):
        """Editing content on a published entry must not silently
        unpublish it — state transitions go through publish_entry /
        unpublish_entry explicitly."""
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        changelog_store.publish_entry(eid)
        published_at_before = changelog_store.get_entry(eid)["published_at"]

        changelog_store.update_entry(eid, title="edited")
        entry = changelog_store.get_entry(eid)
        assert entry["is_published"] is True
        assert entry["published_at"] == published_at_before


class TestPublishCycle:

    def test_publish_sets_published_at(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        assert changelog_store.publish_entry(eid)
        entry = changelog_store.get_entry(eid)
        assert entry["is_published"] is True
        assert entry["published_at"] is not None

    def test_publish_is_idempotent_on_timestamp(self):
        """Re-publishing a published entry must not re-stamp the
        timestamp — consumers rely on ``published_at`` reflecting
        first publication, not last edit."""
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        changelog_store.publish_entry(eid)
        first_ts = changelog_store.get_entry(eid)["published_at"]
        changelog_store.publish_entry(eid)
        assert changelog_store.get_entry(eid)["published_at"] == first_ts

    def test_unpublish_preserves_published_at(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        changelog_store.publish_entry(eid)
        ts = changelog_store.get_entry(eid)["published_at"]
        changelog_store.unpublish_entry(eid)
        entry = changelog_store.get_entry(eid)
        assert entry["is_published"] is False
        # Timestamp sticks so a re-publish carries the original date.
        assert entry["published_at"] == ts

    def test_publish_unknown_id_returns_false(self):
        assert changelog_store.publish_entry(9999) is False
        assert changelog_store.unpublish_entry(9999) is False


class TestDelete:

    def test_delete_removes_row(self):
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        assert changelog_store.delete_entry(eid)
        assert changelog_store.get_entry(eid) is None

    def test_delete_unknown_id_returns_false(self):
        assert changelog_store.delete_entry(9999) is False


class TestListing:

    def test_list_published_filters_drafts_out(self):
        a = changelog_store.create_entry("draft", _DESCRIPTION, "feature")
        b = changelog_store.create_entry("live", _DESCRIPTION, "fix")
        changelog_store.publish_entry(b)

        published_ids = [e["id"] for e in changelog_store.list_published()]
        assert b in published_ids
        assert a not in published_ids

    def test_list_all_includes_drafts_by_default(self):
        a = changelog_store.create_entry("draft", _DESCRIPTION, "feature")
        b = changelog_store.create_entry("live", _DESCRIPTION, "fix")
        changelog_store.publish_entry(b)

        ids = [e["id"] for e in changelog_store.list_all()]
        assert set(ids) == {a, b}

    def test_list_published_respects_limit(self):
        for i in range(5):
            eid = changelog_store.create_entry(
                f"entry {i}", _DESCRIPTION, _CATEGORY,
            )
            changelog_store.publish_entry(eid)
        assert len(changelog_store.list_published(limit=3)) == 3

    def test_list_published_zero_limit_returns_empty(self):
        """Defensive: a limit of 0 must not execute the SELECT and
        must not trip on SQLite's LIMIT=0 behaviour."""
        eid = changelog_store.create_entry(_TITLE, _DESCRIPTION, _CATEGORY)
        changelog_store.publish_entry(eid)
        assert changelog_store.list_published(limit=0) == []

    def test_list_published_newest_first(self):
        # Use raw SQL to set published_at so the test doesn't sleep.
        conn = get_db()
        ids = []
        for i in range(3):
            eid = changelog_store.create_entry(
                f"e{i}", _DESCRIPTION, _CATEGORY,
            )
            ids.append(eid)
            with conn:
                conn.execute(
                    "UPDATE changelog_entries SET is_published=1, "
                    "published_at=? WHERE id=?",
                    (f"2026-01-0{i + 1} 00:00:00", eid),
                )
        result = [e["id"] for e in changelog_store.list_published()]
        assert result == list(reversed(ids))
