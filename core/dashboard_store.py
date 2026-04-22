"""Dashboard layout persistence — one-layout-per-user, JSON-blob storage.

PR 1 of the Workspace feature: this module is the backend-only
persistence layer. Frontend integration + panel-type logic arrive
in later PRs.

Design notes:
- ``layout_json`` is opaque to the backend. We validate it's valid
  JSON and under 16 KB; the frontend owns the panel schema. Keeps
  the backend stable while the panel ecosystem evolves.
- ``get_layout`` returns the default layout or ``None`` when unset.
  The frontend decides what the empty-state looks like — we don't
  ship a server-side default so a layout-schema change doesn't
  require a backend deploy.
- ``put_layout`` is idempotent: INSERT ... ON CONFLICT replaces the
  existing row or inserts fresh. ``updated_at`` bumped automatically
  on every write.
- The schema's ``name`` column is 'default'-out-of-the-box. Later
  PRs can expose named layouts by adding ``name=`` plumbing — the
  storage already supports it via the ``UNIQUE (user_id, name)``
  constraint.

DB connection goes through ``core.database.get_db`` so the per-
thread cached connection + test-isolation via ``set_db_path`` keep
working without this module opening a parallel handle.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.database import get_db

logger = logging.getLogger(__name__)

# 16 KB is generous for tens of panels with sensible metadata. A
# bot-editor config round-trips around 4 KB, so this leaves ~4x
# headroom before the UI would have to compress.
MAX_LAYOUT_SIZE_BYTES = 16 * 1024

# Default name constant — exposed so tests + a future multi-layout
# UI can reuse it without magic strings.
DEFAULT_LAYOUT_NAME = "default"


def get_layout(
    user_id: int, name: str = DEFAULT_LAYOUT_NAME,
) -> Optional[dict]:
    """Return the parsed layout dict for ``(user_id, name)``.

    Returns ``None`` if no row exists. Raises ``ValueError`` when
    the stored JSON is unparseable — the route layer turns that
    into an empty-state response so the frontend resets cleanly
    rather than crashing on a corrupt blob.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT layout_json FROM dashboard_layouts "
        "WHERE user_id = ? AND name = ?",
        (int(user_id), str(name)),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["layout_json"])
    except json.JSONDecodeError as e:
        logger.warning(
            "Corrupt layout_json for user=%s name=%r: %s",
            user_id, name, e,
        )
        raise ValueError(f"Corrupt layout JSON: {e}")


def put_layout(
    user_id: int,
    layout: dict,
    name: str = DEFAULT_LAYOUT_NAME,
) -> None:
    """Upsert a layout for ``(user_id, name)``.

    Serialises ``layout`` to JSON (compact — no padding) and stores
    it. Raises ``ValueError`` when:
      * ``layout`` contains a non-JSON-serialisable value, OR
      * the serialised byte length exceeds
        ``MAX_LAYOUT_SIZE_BYTES``.

    Atomic via ``INSERT ... ON CONFLICT (user_id, name) DO UPDATE``:
    one statement replaces the existing row or inserts fresh.
    ``updated_at`` is re-stamped in both branches — SQLite does not
    rerun a column DEFAULT on UPDATE, so we pass the timestamp
    explicitly in the conflict clause.
    """
    try:
        payload = json.dumps(layout, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"Layout is not JSON-serialisable: {e}") from e

    size = len(payload.encode("utf-8"))
    if size > MAX_LAYOUT_SIZE_BYTES:
        raise ValueError(
            f"Layout exceeds max size of {MAX_LAYOUT_SIZE_BYTES} "
            f"bytes (got {size})",
        )

    conn = get_db()
    with conn:
        conn.execute(
            """
            INSERT INTO dashboard_layouts
                (user_id, name, layout_json, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, name) DO UPDATE SET
                layout_json = excluded.layout_json,
                updated_at  = datetime('now')
            """,
            (int(user_id), str(name), payload),
        )


def delete_layout(
    user_id: int, name: str = DEFAULT_LAYOUT_NAME,
) -> bool:
    """Remove a layout.

    Returns ``True`` if a row was deleted, ``False`` if no matching
    layout existed. Exposed now for test cleanup + the future
    multi-layout UI; not yet wired to an endpoint in PR 1.
    """
    conn = get_db()
    with conn:
        cur = conn.execute(
            "DELETE FROM dashboard_layouts "
            "WHERE user_id = ? AND name = ?",
            (int(user_id), str(name)),
        )
        return cur.rowcount > 0
