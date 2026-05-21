"""Marketing-site data export.

Writes JSON snapshots of roadmap and changelog data to
``/var/www/reverto-marketing/data/``. The static marketing site at
``https://reverto.bot`` reads these snapshots; this module is the
only writer.

Snapshots are written synchronously when the operator publishes,
unpublishes, edits, deletes, or reorders entries. Failures are
logged but do NOT block the calling DB mutation — the database
is the source of truth and the snapshot is best-effort. The
admin "Regenerate marketing snapshots" endpoint at
``POST /api/admin/marketing/regenerate`` exists as the manual
recovery path when a snapshot drifts.

Snapshot shape mirrors the public-API endpoints
(``/api/roadmap`` and ``/api/changelog``) so the marketing-site
render code receives the same fields the in-app SPA already
consumes — no dual maintenance when the public shape evolves.

Override the data dir via ``REVERTO_MARKETING_DATA_DIR`` for
dev / test environments where ``/var/www/`` is not writable.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from core import changelog_store, roadmap_store
from core.markdown_render import render_markdown

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path("/var/www/reverto-marketing/data")

# PT-v4-MK-002: every filename reaching ``_write_atomic`` must be a
# single basename ending in ``.json``. All current callers
# (write_roadmap_snapshot, write_changelog_snapshot) pass hardcoded
# literals so no exploit exists today; the guard exists for
# defence-in-depth so a future caller that forwards a request-derived
# string cannot land an attacker-controlled write outside the data
# dir. Sibling finding PUB-v1-001 covers the same class of issue in
# core/paths.py and is tracked separately.
_MARKETING_FILENAME_RE = re.compile(r"[a-z0-9_-]+\.json")


def _data_dir() -> Path:
    override = os.environ.get("REVERTO_MARKETING_DATA_DIR")
    if override:
        return Path(override)
    return _DEFAULT_DATA_DIR


def _phase_to_public(phase: dict) -> dict:
    """Public roadmap shape — mirrors
    ``web.routes.roadmap._phase_to_public_json``. Drops admin-only
    bookkeeping and emits ``body_html`` pre-rendered through the
    bleach sanitiser so the marketing site can drop it straight
    into innerHTML without a client-side renderer.

    PT-v4-MK-001: ``body_md`` (operator-internal markdown source)
    is intentionally NOT in the public shape — the marketing site
    only consumes ``body_html``. The asymmetry with the changelog
    (which never exposed the raw markdown) was a hygiene drift,
    not an exploit; this aligns roadmap + changelog public
    surfaces.
    """
    return {
        "phase_key": phase["phase_key"],
        "display_name": phase["display_name"],
        "summary": phase["summary"],
        "status": phase["status"],
        "sort_order": phase["sort_order"],
        "body_html": render_markdown(phase["body_md"]) if phase["body_md"] else "",
        "effort_estimate": phase["effort_estimate"],
        "in_progress_note": phase["in_progress_note"],
        "audit_checkpoint": phase["audit_checkpoint"],
        "published_at": phase["published_at"],
    }


def _entry_to_public(entry: dict) -> dict:
    """Public changelog shape — mirrors
    ``web.routes.changelog._entry_to_public_json``. Drops raw
    ``description`` markdown, ``is_published``, ``created_at``,
    and ``source_commit_sha`` (admin bookkeeping). Emits
    ``description_html`` pre-rendered via bleach."""
    return {
        "id": entry["id"],
        "title": entry["title"],
        "category": entry["category"],
        "published_at": entry["published_at"],
        "description_html": render_markdown(entry["description"]),
    }


def write_roadmap_snapshot() -> bool:
    """Write the published roadmap phases to ``roadmap.json``.

    Returns True on success, False on failure. Never raises —
    callers must not block their own DB work on a snapshot
    failure.
    """
    try:
        phases = roadmap_store.list_published(limit=200)
        payload = {"phases": [_phase_to_public(p) for p in phases]}
        return _write_atomic("roadmap.json", payload)
    except Exception:
        logger.exception("Failed to write roadmap snapshot")
        return False


def write_changelog_snapshot() -> bool:
    """Write the published changelog entries to ``changelog.json``.

    Returns True on success, False on failure. Never raises.
    """
    try:
        entries = changelog_store.list_published(limit=500)
        payload = {"entries": [_entry_to_public(e) for e in entries]}
        return _write_atomic("changelog.json", payload)
    except Exception:
        logger.exception("Failed to write changelog snapshot")
        return False


def write_all_snapshots() -> dict[str, bool]:
    """Regenerate both snapshots. Powers the admin
    "Regenerate marketing snapshots" button.

    Returns ``{"roadmap": bool, "changelog": bool}``. The two
    writes are independent — one failing does not abort the
    other.
    """
    return {
        "roadmap": write_roadmap_snapshot(),
        "changelog": write_changelog_snapshot(),
    }


def _write_atomic(filename: str, payload: dict[str, Any]) -> bool:
    """Write ``payload`` as JSON to ``_data_dir() / filename``
    atomically: write to ``.tmp``, fsync, then rename. Avoids
    Caddy serving a half-written file mid-update.

    PT-v4-MK-002: ``filename`` is validated against
    ``_MARKETING_FILENAME_RE`` (single lowercase basename, .json
    extension) before touching the filesystem. Raises ``ValueError``
    for path-traversal segments, embedded slashes, absolute paths,
    or non-.json extensions.
    """
    if not _MARKETING_FILENAME_RE.fullmatch(filename):
        raise ValueError(
            f"_write_atomic: filename must match "
            f"{_MARKETING_FILENAME_RE.pattern!r}, got: {filename!r}"
        )
    target = _data_dir() / filename
    tmp = _data_dir() / (filename + ".tmp")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        logger.error(
            "Cannot create marketing data dir %s "
            "(permission denied — verify VPS step 'sudo mkdir + "
            "chown bot:bot' has been run)",
            target.parent,
        )
        return False
    except Exception:
        logger.exception(
            "Failed to ensure marketing data dir %s exists",
            target.parent,
        )
        return False

    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        logger.info("Wrote marketing snapshot: %s", target)
        return True
    except Exception:
        logger.exception("Failed to write marketing snapshot %s", target)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False
