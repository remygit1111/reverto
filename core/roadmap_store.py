"""DB-backed roadmap-phase CRUD.

Foundation for the public ``/roadmap`` page + admin SPA tab.
Schema v10 introduces the ``roadmap_phases`` table; see
``core/database.py`` for the exact columns.

Design mirror of ``core.changelog_store``: synchronous SQLite
helpers, no ORM, thin dict adapters. Callers that need richer
shapes can wrap the dicts themselves.

Module contract:
  * status is validated against ``VALID_STATUSES``; anything
    outside the whitelist raises ``ValueError`` so the API layer
    can return a 400 without leaking column-level detail.
  * ``phase_key`` is the immutable machine identifier (e.g.
    ``"phase-3a"``). It is set on ``create``, validated against
    ``[a-z0-9-]+``, and enforced UNIQUE at the DB layer; ``update``
    never accepts a new key.
  * ``publish`` sets ``published_at`` to the current time only on
    the first flip to published; re-publishing an already-published
    phase is idempotent and does NOT re-stamp the timestamp.
    Editors can rely on ``published_at`` as "first time this went
    public". ``unpublish`` does NOT clear ``published_at`` — a
    later re-publish keeps the original first-publish date so the
    audit trail is preserved.
  * ``list_published`` and ``list_all`` order by ``sort_order ASC``
    so the timeline reads top-to-bottom in the operator's drag-
    and-drop order. ``id ASC`` is the tiebreak for phases that
    accidentally end up at the same sort position.
  * ``reorder`` assigns ``sort_order`` values as multiples of 10
    (10, 20, 30, …). The gaps let a future operator drag a single
    phase between two others without renumbering every row in the
    table.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from core.database import get_db

__all__ = [
    "VALID_STATUSES",
    "MAX_PHASE_KEY_LEN",
    "MAX_DISPLAY_NAME_LEN",
    "MAX_SUMMARY_LEN",
    "MAX_BODY_LEN",
    "MAX_EFFORT_LEN",
    "MAX_NOTE_LEN",
    "MAX_AUDIT_LEN",
    "RoadmapPhaseKeyConflict",
    "create_phase",
    "get_phase",
    "get_phase_by_key",
    "update_phase",
    "delete_phase",
    "publish_phase",
    "unpublish_phase",
    "list_published",
    "list_all",
    "reorder_phases",
]

VALID_STATUSES: frozenset = frozenset({"pending", "active", "done"})

# Soft caps applied at the application layer. SQLite TEXT columns
# stay unconstrained at the schema level so a future bump doesn't
# require a destructive migration. The admin form mirrors these
# values via ``maxlength`` attributes.
MAX_PHASE_KEY_LEN = 50
MAX_DISPLAY_NAME_LEN = 200
MAX_SUMMARY_LEN = 500
MAX_BODY_LEN = 20_000
MAX_EFFORT_LEN = 200
MAX_NOTE_LEN = 500
MAX_AUDIT_LEN = 500

# Reorder step: assign sort_order as multiples of 10 so future
# drag-inserts between two phases don't require a full
# renumbering of the table.
_REORDER_STEP = 10

_PHASE_KEY_RE = re.compile(r"^[a-z0-9-]+$")


class RoadmapPhaseKeyConflict(ValueError):
    """Raised when a ``create_phase`` call collides with an
    existing ``phase_key`` UNIQUE constraint. Subclass of
    ``ValueError`` so route handlers that catch the broader class
    keep working; the more specific class lets callers distinguish
    "duplicate key" from "field too long" / "invalid status" if
    they want different 4xx detail strings.
    """


def _validate_status(status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid roadmap status {status!r}; expected one of "
            f"{', '.join(sorted(VALID_STATUSES))}"
        )


def _validate_phase_key(phase_key: str) -> None:
    if not phase_key:
        raise ValueError("phase_key must not be empty")
    if len(phase_key) > MAX_PHASE_KEY_LEN:
        raise ValueError(
            f"phase_key exceeds {MAX_PHASE_KEY_LEN} characters "
            f"({len(phase_key)} given)"
        )
    if not _PHASE_KEY_RE.match(phase_key):
        raise ValueError(
            "phase_key must match [a-z0-9-]+ (lowercase letters, "
            "digits, dashes only)"
        )


def _validate_required_text(value: str, field: str) -> str:
    """Strip + ensure non-empty + return the trimmed value. Used
    for ``display_name`` and ``summary`` which the schema declares
    NOT NULL with no DEFAULT — empty values are not meaningful."""
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _validate_length(value: str, max_len: int, field: str) -> None:
    if len(value) > max_len:
        raise ValueError(
            f"{field} exceeds {max_len} characters ({len(value)} given)"
        )


def _row_to_dict(row) -> dict:
    return {
        "id": int(row["id"]),
        "phase_key": str(row["phase_key"]),
        "display_name": str(row["display_name"]),
        "summary": str(row["summary"]),
        "status": str(row["status"]),
        "sort_order": int(row["sort_order"]),
        "body_md": str(row["body_md"]),
        "effort_estimate": str(row["effort_estimate"]),
        "in_progress_note": str(row["in_progress_note"]),
        "audit_checkpoint": str(row["audit_checkpoint"]),
        "is_published": bool(row["is_published"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "published_at": row["published_at"] if row["published_at"] else None,
    }


# ── Create ────────────────────────────────────────────────────────────────


def create_phase(
    *,
    phase_key: str,
    display_name: str,
    summary: str,
    status: str = "pending",
    sort_order: int = 0,
    body_md: str = "",
    effort_estimate: str = "",
    in_progress_note: str = "",
    audit_checkpoint: str = "",
) -> int:
    """Insert a new draft phase and return its id.

    The row is created with ``is_published = 0`` and
    ``published_at = NULL``. Use ``publish_phase(id)`` to flip it
    live.
    """
    phase_key = phase_key.strip()
    _validate_phase_key(phase_key)
    display_name = _validate_required_text(display_name, "display_name")
    _validate_length(display_name, MAX_DISPLAY_NAME_LEN, "display_name")
    summary = _validate_required_text(summary, "summary")
    _validate_length(summary, MAX_SUMMARY_LEN, "summary")
    _validate_status(status)
    body_md = body_md or ""
    _validate_length(body_md, MAX_BODY_LEN, "body_md")
    effort_estimate = (effort_estimate or "").strip()
    _validate_length(effort_estimate, MAX_EFFORT_LEN, "effort_estimate")
    in_progress_note = (in_progress_note or "").strip()
    _validate_length(in_progress_note, MAX_NOTE_LEN, "in_progress_note")
    audit_checkpoint = (audit_checkpoint or "").strip()
    _validate_length(audit_checkpoint, MAX_AUDIT_LEN, "audit_checkpoint")

    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO roadmap_phases ("
                "phase_key, display_name, summary, status, sort_order, "
                "body_md, effort_estimate, in_progress_note, "
                "audit_checkpoint"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    phase_key, display_name, summary, status,
                    int(sort_order), body_md, effort_estimate,
                    in_progress_note, audit_checkpoint,
                ),
            )
    except sqlite3.IntegrityError as e:
        # Translate the UNIQUE-constraint violation on phase_key
        # into a domain-meaningful exception so route handlers
        # don't have to inspect SQLite error strings.
        if "phase_key" in str(e):
            raise RoadmapPhaseKeyConflict(
                f"phase_key {phase_key!r} already exists"
            ) from e
        raise
    return int(cur.lastrowid)


# ── Read ──────────────────────────────────────────────────────────────────


_SELECT_COLS = (
    "id, phase_key, display_name, summary, status, sort_order, "
    "body_md, effort_estimate, in_progress_note, audit_checkpoint, "
    "is_published, created_at, updated_at, published_at"
)


def get_phase(phase_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM roadmap_phases WHERE id = ?",
        (phase_id,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def get_phase_by_key(phase_key: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM roadmap_phases WHERE phase_key = ?",
        (phase_key,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_published(limit: int = 50) -> list[dict]:
    """Return published phases ordered by ``sort_order ASC``. The
    public ``/api/roadmap`` consumer reads exactly this list."""
    if limit <= 0:
        return []
    conn = get_db()
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM roadmap_phases "
        "WHERE is_published = 1 "
        "ORDER BY sort_order ASC, id ASC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_all(*, include_unpublished: bool = True) -> list[dict]:
    """Admin-side listing. Drafts included by default; pass
    ``include_unpublished=False`` to mirror ``list_published``
    without the LIMIT clause."""
    conn = get_db()
    if include_unpublished:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM roadmap_phases "
            "ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM roadmap_phases "
            "WHERE is_published = 1 "
            "ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── Update / publish / unpublish / delete ────────────────────────────────


def update_phase(phase_id: int, payload: dict) -> bool:
    """Partial update. Only fields present in ``payload`` (and not
    ``None``) are updated. ``phase_key``, ``id``, and timestamps
    are NEVER editable via this path; callers passing those keys
    are silently ignored so a future SPA cannot trigger a
    schema-rotation by setting ``id`` in the body.

    Bumps ``updated_at`` whenever any column actually changes.
    Returns True if a row matched (regardless of whether columns
    changed), False when ``phase_id`` does not resolve.
    """
    fields: list[str] = []
    values: list = []

    if "display_name" in payload and payload["display_name"] is not None:
        v = _validate_required_text(payload["display_name"], "display_name")
        _validate_length(v, MAX_DISPLAY_NAME_LEN, "display_name")
        fields.append("display_name = ?")
        values.append(v)
    if "summary" in payload and payload["summary"] is not None:
        v = _validate_required_text(payload["summary"], "summary")
        _validate_length(v, MAX_SUMMARY_LEN, "summary")
        fields.append("summary = ?")
        values.append(v)
    if "status" in payload and payload["status"] is not None:
        _validate_status(payload["status"])
        fields.append("status = ?")
        values.append(payload["status"])
    if "sort_order" in payload and payload["sort_order"] is not None:
        fields.append("sort_order = ?")
        values.append(int(payload["sort_order"]))
    if "body_md" in payload and payload["body_md"] is not None:
        v = payload["body_md"]
        _validate_length(v, MAX_BODY_LEN, "body_md")
        fields.append("body_md = ?")
        values.append(v)
    if "effort_estimate" in payload and payload["effort_estimate"] is not None:
        v = (payload["effort_estimate"] or "").strip()
        _validate_length(v, MAX_EFFORT_LEN, "effort_estimate")
        fields.append("effort_estimate = ?")
        values.append(v)
    if "in_progress_note" in payload and payload["in_progress_note"] is not None:
        v = (payload["in_progress_note"] or "").strip()
        _validate_length(v, MAX_NOTE_LEN, "in_progress_note")
        fields.append("in_progress_note = ?")
        values.append(v)
    if "audit_checkpoint" in payload and payload["audit_checkpoint"] is not None:
        v = (payload["audit_checkpoint"] or "").strip()
        _validate_length(v, MAX_AUDIT_LEN, "audit_checkpoint")
        fields.append("audit_checkpoint = ?")
        values.append(v)

    if not fields:
        # Nothing to update — return whether the row exists so the
        # caller's UI can distinguish "no-op edit" from "row gone".
        return get_phase(phase_id) is not None

    fields.append("updated_at = datetime('now')")
    values.append(phase_id)
    conn = get_db()
    with conn:
        # Security note: ``fields`` only holds hardcoded
        # column-assignment string literals built from the
        # if-branches above. No user input lands in the SQL
        # template; user values bind to ? placeholders only.
        cur = conn.execute(
            f"UPDATE roadmap_phases SET {', '.join(fields)} WHERE id = ?",
            values,
        )
    return cur.rowcount > 0


def publish_phase(phase_id: int) -> bool:
    """Flip a phase to published. Idempotent — re-publishing an
    already-published phase leaves ``published_at`` untouched so
    the timestamp reflects the first publish, not the most recent
    edit. ``updated_at`` IS bumped so the admin list shows the
    publish action moved the row."""
    conn = get_db()
    with conn:
        cur = conn.execute(
            "UPDATE roadmap_phases SET "
            "is_published = 1, "
            "published_at = COALESCE(published_at, datetime('now')), "
            "updated_at = datetime('now') "
            "WHERE id = ?",
            (phase_id,),
        )
    return cur.rowcount > 0


def unpublish_phase(phase_id: int) -> bool:
    """Revert a phase to draft. Preserves ``published_at`` so a
    later re-publish carries the original first-publish date; if
    a fresh timestamp is wanted, delete + recreate the phase."""
    conn = get_db()
    with conn:
        cur = conn.execute(
            "UPDATE roadmap_phases SET "
            "is_published = 0, "
            "updated_at = datetime('now') "
            "WHERE id = ?",
            (phase_id,),
        )
    return cur.rowcount > 0


def delete_phase(phase_id: int) -> bool:
    conn = get_db()
    with conn:
        cur = conn.execute(
            "DELETE FROM roadmap_phases WHERE id = ?", (phase_id,),
        )
    return cur.rowcount > 0


def reorder_phases(ordered_ids: list[int]) -> None:
    """Atomically reassign ``sort_order`` for the given ids in the
    order provided. ``ordered_ids[0]`` gets sort_order 10,
    ``ordered_ids[1]`` gets 20, etc. (multiples of 10 so a
    subsequent drag-insert between two phases doesn't require
    renumbering the table).

    Silently skips ids that don't resolve to a row — callers that
    need strict "every id must exist" semantics can pre-validate
    via ``get_phase``.
    """
    if not ordered_ids:
        return
    conn = get_db()
    with conn:
        for index, phase_id in enumerate(ordered_ids, start=1):
            conn.execute(
                "UPDATE roadmap_phases SET "
                "sort_order = ?, "
                "updated_at = datetime('now') "
                "WHERE id = ?",
                (index * _REORDER_STEP, int(phase_id)),
            )
