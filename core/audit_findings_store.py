"""DB-backed audit/pentest findings tracker.

Foundation for the /admin/findings dashboard. The markdown audit docs
in ``docs/audits/`` and ``docs/pentests/`` remain authoritative for
the *narrative* of each finding (what it is, why it matters, full
remediation rationale). This table tracks the *mutable* operator
state — status, notes, resolution-ref — so the admin UI can roll up
"how many open findings" without grepping eight markdown files every
time.

Module contract mirrors ``core.changelog_store`` and
``core.dashboard_store``:

  * Synchronous SQLite helpers, no ORM.
  * Severity / status are validated against tight whitelists; out-of-
    range values raise ``ValueError`` so the API layer can return a
    400 without leaking column-level detail.
  * ``upsert_finding`` is the seed-import entry point — insert if the
    ``finding_id`` is new, no-op otherwise. The seed never overwrites
    operator edits to ``status`` / ``notes`` / ``resolution_ref``.
  * ``update_finding`` is the operator-edit path — partial update of
    the three mutable fields; immutable fields (severity, source_doc,
    title, description) come straight from the seed.
"""

from __future__ import annotations

from typing import Optional

from core.database import get_db

__all__ = [
    "VALID_SEVERITIES",
    "VALID_STATUSES",
    "MAX_TITLE_LEN",
    "MAX_NOTES_LEN",
    "MAX_RESOLUTION_REF_LEN",
    "list_findings",
    "get_finding",
    "upsert_finding",
    "update_finding",
    "count_total",
    "count_by_status",
]

VALID_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"})
VALID_STATUSES = frozenset(
    {"open", "in_progress", "resolved", "accepted", "deferred"}
)

MAX_TITLE_LEN = 200
MAX_NOTES_LEN = 8000
MAX_RESOLUTION_REF_LEN = 200


def _row_to_dict(row) -> dict:
    """Serialise a sqlite3.Row to the API-shape dict. The store layer
    owns the column names so call sites do not have to know them."""
    return {
        "finding_id": row["finding_id"],
        "source_doc": row["source_doc"],
        "severity": row["severity"],
        "status": row["status"],
        "title": row["title"],
        "description": row["description"],
        "resolution_ref": row["resolution_ref"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_findings(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    source_doc: Optional[str] = None,
) -> list[dict]:
    """Return all findings matching the optional filter triple.

    Filters compose with AND. Each is independently validated against
    the schema enums so a malformed query parameter from the API layer
    never reaches the SQL string. Sort order is (severity-rank, then
    finding_id ASC) so HIGH/CRITICAL items float to the top of the
    admin table without the UI needing to know severity weights.
    """
    sql = "SELECT * FROM audit_findings WHERE 1=1"
    params: list = []
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        sql += " AND status = ?"
        params.append(status)
    if severity is not None:
        if severity not in VALID_SEVERITIES:
            raise ValueError(f"unknown severity: {severity!r}")
        sql += " AND severity = ?"
        params.append(severity)
    if source_doc is not None:
        sql += " AND source_doc = ?"
        params.append(source_doc)
    # Severity rank: CRITICAL=0, HIGH=1, MEDIUM=2, LOW=3, INFO=4 →
    # ASC sort puts the most-severe first. SQLite CASE-WHEN keeps the
    # ordering pinned to the schema enum without a separate join.
    sql += (
        " ORDER BY CASE severity"
        "   WHEN 'CRITICAL' THEN 0"
        "   WHEN 'HIGH'     THEN 1"
        "   WHEN 'MEDIUM'   THEN 2"
        "   WHEN 'LOW'      THEN 3"
        "   WHEN 'INFO'     THEN 4"
        "   ELSE 5"
        " END ASC, finding_id ASC"
    )
    rows = get_db().execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_finding(finding_id: str) -> Optional[dict]:
    """Look up a single finding by its public ID. Returns None when
    the ID does not exist; callers raise 404."""
    row = get_db().execute(
        "SELECT * FROM audit_findings WHERE finding_id = ?",
        (finding_id,),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def upsert_finding(
    *,
    finding_id: str,
    source_doc: str,
    severity: str,
    status: str,
    title: str,
    description: str = "",
    resolution_ref: Optional[str] = None,
    notes: str = "",
) -> bool:
    """Insert a finding if its ``finding_id`` is unseen, else no-op.

    Returns True on insert, False on no-op. The seed-import path is
    the only caller; idempotency means an operator can re-run the
    seed without clobbering manual edits made through the admin UI.

    Validates inputs on the way in: severity + status whitelists,
    title length cap, mandatory non-empty title and source_doc.
    """
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"unknown severity: {severity!r}")
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    if not finding_id:
        raise ValueError("finding_id must be non-empty")
    if not source_doc:
        raise ValueError("source_doc must be non-empty")
    if not title:
        raise ValueError("title must be non-empty")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(
            f"title exceeds {MAX_TITLE_LEN} chars: {len(title)}"
        )
    if resolution_ref is not None and len(resolution_ref) > MAX_RESOLUTION_REF_LEN:
        raise ValueError(
            f"resolution_ref exceeds {MAX_RESOLUTION_REF_LEN} chars"
        )
    if len(notes) > MAX_NOTES_LEN:
        raise ValueError(f"notes exceed {MAX_NOTES_LEN} chars")

    conn = get_db()
    cur = conn.execute(
        "INSERT OR IGNORE INTO audit_findings "
        "(finding_id, source_doc, severity, status, title, description, "
        " resolution_ref, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            finding_id, source_doc, severity, status, title, description,
            resolution_ref, notes,
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def update_finding(
    finding_id: str,
    *,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    resolution_ref: Optional[str] = None,
) -> bool:
    """Operator-driven partial update of the three mutable fields.

    Returns True on update, False if the finding_id does not exist.
    ``None`` means "do not touch this column" so a PATCH that only
    flips status leaves notes intact. ``updated_at`` always advances
    when at least one field changes.

    Invariants the API layer relies on:
      * Status is enum-validated; an unknown value raises ValueError.
      * Notes/resolution_ref length caps protect the schema from
        unbounded operator input.
      * If all three params are None this is a no-op (returns False)
        rather than spending a round-trip on a stamping-only update.
    """
    if status is None and notes is None and resolution_ref is None:
        return False
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    if notes is not None and len(notes) > MAX_NOTES_LEN:
        raise ValueError(f"notes exceed {MAX_NOTES_LEN} chars")
    if resolution_ref is not None and len(resolution_ref) > MAX_RESOLUTION_REF_LEN:
        raise ValueError(
            f"resolution_ref exceeds {MAX_RESOLUTION_REF_LEN} chars"
        )

    sets: list[str] = []
    params: list = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes)
    if resolution_ref is not None:
        sets.append("resolution_ref = ?")
        params.append(resolution_ref)
    sets.append("updated_at = datetime('now')")
    params.append(finding_id)

    conn = get_db()
    cur = conn.execute(
        f"UPDATE audit_findings SET {', '.join(sets)} WHERE finding_id = ?",
        params,
    )
    conn.commit()
    return cur.rowcount > 0


def count_total() -> int:
    """Total row count. Used by the admin UI's stats strip + the
    seed-bootstrap idempotency check (skip seed if table non-empty)."""
    return get_db().execute(
        "SELECT COUNT(*) AS n FROM audit_findings"
    ).fetchone()["n"]


def count_by_status() -> dict[str, int]:
    """Distribution of findings across the five statuses. Returns a
    dict with all five keys present (zero-padded) so the UI does not
    need to defensively check key existence."""
    rows = get_db().execute(
        "SELECT status, COUNT(*) AS n FROM audit_findings GROUP BY status"
    ).fetchall()
    out = {s: 0 for s in VALID_STATUSES}
    for r in rows:
        out[r["status"]] = r["n"]
    return out
