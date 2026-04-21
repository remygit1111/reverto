"""DB-backed changelog entry CRUD.

Foundation for the /changelog page + admin CRUD. Manual entries for
now — later PRs will add auto-generation from git commits and a
per-user read-tracking badge. Schema v5 introduces the
``changelog_entries`` table; see ``core/database.py`` for the exact
columns.

Design mirror of ``core.user_store``: synchronous SQLite helpers,
no ORM, thin dict adapters. Callers that need richer shapes can
wrap the dicts themselves.

Module contract:
  * Categories are validated against a whitelist; anything outside
    the whitelist raises ``ValueError`` so the API layer can return a
    400 without leaking column-level detail.
  * ``publish_entry`` sets ``published_at`` to the current time only
    on the first flip to published; re-publishing an already-published
    entry is idempotent and does NOT re-stamp the timestamp. Editors
    can rely on ``published_at`` as "first time this went public".
  * ``list_published`` returns entries ordered by ``published_at``
    desc (newest first). ``list_all`` sorts drafts-with-no-
    ``published_at`` alongside published rows by ``created_at`` desc,
    so the admin list is stable regardless of publish state.
"""

from __future__ import annotations

from typing import Optional

from core.database import get_db

__all__ = [
    "VALID_CATEGORIES",
    "MAX_TITLE_LEN",
    "MAX_DESCRIPTION_LEN",
    "create_entry",
    "get_entry",
    "update_entry",
    "delete_entry",
    "publish_entry",
    "unpublish_entry",
    "list_published",
    "list_all",
]

# Locked whitelist — order matches the UI dropdown so the form's
# default (first entry) is ``feature``.
VALID_CATEGORIES: tuple[str, ...] = ("feature", "fix", "improvement", "security")

# Soft caps applied at the application layer. SQLite doesn't enforce
# them at the column level (deliberately — the schema keeps TEXT
# unconstrained so a future bump doesn't require a destructive
# migration). The admin form echoes the same numbers back via
# ``maxlength`` attributes.
MAX_TITLE_LEN = 200
MAX_DESCRIPTION_LEN = 20_000


def _validate_category(category: str) -> None:
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"Invalid changelog category {category!r}; expected one of "
            f"{', '.join(VALID_CATEGORIES)}"
        )


def _row_to_dict(row) -> dict:
    return {
        "id": int(row["id"]),
        "title": str(row["title"]),
        "description": str(row["description"]),
        "category": str(row["category"]),
        "is_published": bool(row["is_published"]),
        "created_at": str(row["created_at"]),
        "published_at": row["published_at"] if row["published_at"] else None,
        "source_commit_sha": (
            row["source_commit_sha"] if row["source_commit_sha"] else None
        ),
    }


def create_entry(
    title: str,
    description: str,
    category: str,
    source_commit_sha: Optional[str] = None,
) -> int:
    """Insert a new draft entry and return its id.

    The row is created with ``is_published = 0`` and ``published_at
    = NULL``. Use ``publish_entry(id)`` to flip it live.
    """
    title = title.strip()
    description = description.strip()
    if not title:
        raise ValueError("title must not be empty")
    if not description:
        raise ValueError("description must not be empty")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(
            f"title exceeds {MAX_TITLE_LEN} characters ({len(title)} given)"
        )
    if len(description) > MAX_DESCRIPTION_LEN:
        raise ValueError(
            f"description exceeds {MAX_DESCRIPTION_LEN} characters"
        )
    _validate_category(category)
    conn = get_db()
    with conn:
        cur = conn.execute(
            "INSERT INTO changelog_entries "
            "(title, description, category, source_commit_sha) "
            "VALUES (?, ?, ?, ?)",
            (title, description, category, source_commit_sha),
        )
    return int(cur.lastrowid)


def get_entry(entry_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT id, title, description, category, is_published, "
        "created_at, published_at, source_commit_sha "
        "FROM changelog_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def update_entry(
    entry_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    category: Optional[str] = None,
) -> bool:
    """Partial update — only non-None kwargs land in the UPDATE.

    Returns True if the row existed (cur.rowcount >= 1), False when
    no row matched. Does NOT touch ``is_published`` or
    ``published_at``; use ``publish_entry`` / ``unpublish_entry`` for
    state transitions.
    """
    fields: list[str] = []
    values: list = []
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        if len(title) > MAX_TITLE_LEN:
            raise ValueError(
                f"title exceeds {MAX_TITLE_LEN} characters ({len(title)} given)"
            )
        fields.append("title = ?")
        values.append(title)
    if description is not None:
        description = description.strip()
        if not description:
            raise ValueError("description must not be empty")
        if len(description) > MAX_DESCRIPTION_LEN:
            raise ValueError(
                f"description exceeds {MAX_DESCRIPTION_LEN} characters"
            )
        fields.append("description = ?")
        values.append(description)
    if category is not None:
        _validate_category(category)
        fields.append("category = ?")
        values.append(category)
    if not fields:
        # Nothing to update — report truthful rowcount anyway so the
        # caller's UI can distinguish "nothing changed" from "row gone".
        return get_entry(entry_id) is not None
    values.append(entry_id)
    conn = get_db()
    with conn:
        cur = conn.execute(
            f"UPDATE changelog_entries SET {', '.join(fields)} WHERE id = ?",
            values,
        )
    return cur.rowcount > 0


def delete_entry(entry_id: int) -> bool:
    conn = get_db()
    with conn:
        cur = conn.execute(
            "DELETE FROM changelog_entries WHERE id = ?", (entry_id,),
        )
    return cur.rowcount > 0


def publish_entry(entry_id: int) -> bool:
    """Flip an entry to published. Idempotent: re-publishing an
    already-published entry leaves ``published_at`` untouched so the
    timestamp reflects the first publish, not the most recent edit."""
    conn = get_db()
    with conn:
        cur = conn.execute(
            "UPDATE changelog_entries "
            "SET is_published = 1, "
            "    published_at = COALESCE(published_at, datetime('now')) "
            "WHERE id = ?",
            (entry_id,),
        )
    return cur.rowcount > 0


def unpublish_entry(entry_id: int) -> bool:
    """Revert an entry to draft. Preserves ``published_at`` so a
    later re-publish still carries the original first-publish date;
    if you want a fresh timestamp, delete + recreate the entry."""
    conn = get_db()
    with conn:
        cur = conn.execute(
            "UPDATE changelog_entries SET is_published = 0 WHERE id = ?",
            (entry_id,),
        )
    return cur.rowcount > 0


def list_published(limit: int = 50) -> list[dict]:
    """Return published entries newest-first. ``limit`` defaults to
    50 — enough for the user-facing /changelog page without pagination
    for now; the index on ``(is_published, published_at DESC)`` keeps
    the query cheap regardless of table size."""
    if limit <= 0:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, description, category, is_published, "
        "created_at, published_at, source_commit_sha "
        "FROM changelog_entries "
        "WHERE is_published = 1 "
        "ORDER BY published_at DESC, id DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_all(include_unpublished: bool = True) -> list[dict]:
    """Admin-side listing. Drafts are included by default (the admin
    UI shows both); pass ``include_unpublished=False`` to mirror
    ``list_published`` without the ``LIMIT`` clause."""
    conn = get_db()
    if include_unpublished:
        rows = conn.execute(
            "SELECT id, title, description, category, is_published, "
            "created_at, published_at, source_commit_sha "
            "FROM changelog_entries "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, description, category, is_published, "
            "created_at, published_at, source_commit_sha "
            "FROM changelog_entries "
            "WHERE is_published = 1 "
            "ORDER BY published_at DESC, id DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
