"""Roadmap JSON-API routes — powers the /roadmap public page +
admin SPA tab.

Surface:

  GET    /api/roadmap                          — PUBLIC, no auth
  GET    /api/admin/roadmap                    — admin list incl. drafts
  POST   /api/admin/roadmap                    — admin create
  GET    /api/admin/roadmap/{phase_id}         — admin read single
  PATCH  /api/admin/roadmap/{phase_id}         — admin partial update
  POST   /api/admin/roadmap/{phase_id}/publish — admin publish
  POST   /api/admin/roadmap/{phase_id}/unpublish — admin unpublish
  DELETE /api/admin/roadmap/{phase_id}         — admin delete
  POST   /api/admin/roadmap/reorder            — admin drag-and-drop reorder

Mirrors web/routes/changelog.py shape, with two deliberate
deviations:

* ``/api/roadmap`` is publicly accessible (logged-out users can
  see the timeline). It is registered in ``web.app._PUBLIC_PATHS``
  so the auth middleware lets the request through; the route
  handler itself takes no auth dependency. Rate-limited at
  30/minute via slowapi to mitigate scraping if the page goes
  viral. The other endpoints retain ``_require_admin_user``.

* The model carries more fields than changelog (status,
  sort_order, body_md, effort_estimate, in_progress_note,
  audit_checkpoint). The admin response includes everything; the
  public response strips ``id``, ``is_published``, ``created_at``,
  ``updated_at`` because those are admin-bookkeeping rather than
  user-visible.

Admin gate: ``_require_admin_user`` checks ``user.role == 'admin'``
— same pattern as ``web/routes/changelog.py`` and emergency-stop in
``web/routes/admin.py`` (audit v26-02).
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import marketing_export, roadmap_store
from core.markdown_render import render_markdown
from core.user import User
from web.app import _audit, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["roadmap"])


# ── Marketing-snapshot best-effort hook ────────────────────────────────────
# Called after every mutation that can change the public timeline
# (publish / unpublish / patch / delete / reorder). The export
# function already swallows every failure internally and returns
# False; the outer try/except is defense-in-depth so a regression
# in marketing_export (NameError, ImportError, etc.) cannot
# bubble up and turn a 200 DB-mutation into a 500 because of a
# best-effort side channel.
#
# Audit PT-v4-MK-003 — snapshot failures used to be log-only;
# emitting an audit event gives operators a queryable trail for
# "did the marketing snapshot pipeline ever silently fail in the
# last week" without grepping portal logs. Two failure signals:
# (1) write_*_snapshot returned False (caught internally — file
# IO error, JSON serialise error, etc.); (2) unexpected exception
# bubbled through the try/except.
def _snapshot_marketing_roadmap() -> None:
    try:
        ok = marketing_export.write_roadmap_snapshot()
    except Exception as e:
        logger.exception(
            "Marketing roadmap snapshot raised (non-fatal — DB "
            "mutation already committed)"
        )
        _audit(
            "marketing_snapshot_save_failed",
            "roadmap",
            type(e).__name__,
            result="error",
        )
        return
    if not ok:
        _audit(
            "marketing_snapshot_save_failed",
            "roadmap",
            "write_returned_false",
            result="error",
        )


# ── Admin gate ─────────────────────────────────────────────────────────────


def _require_admin_user(
    user: User = Depends(_request_user),
) -> User:
    """Admin-only dependency. Mirrors
    ``web/routes/changelog.py::_require_admin_user`` byte-for-byte
    so a future refactor that consolidates the gate has a single
    target."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Request bodies ─────────────────────────────────────────────────────────


class _RoadmapCreateBody(BaseModel):
    """Create payload — required fields are ``phase_key``,
    ``display_name``, ``summary``. Optional fields default to
    sensible empty / pending values; the admin form fills them
    after the row exists.

    Length caps mirror the soft caps in ``core.roadmap_store`` so
    an oversized input fails fast with 422 before it hits the DB
    layer. ``status`` uses ``Literal`` so Pydantic v2 rejects
    invalid values at parse-time with a clear error message,
    saving a round-trip to the store's ``ValueError`` path."""

    phase_key: str = Field(
        min_length=1,
        max_length=roadmap_store.MAX_PHASE_KEY_LEN,
    )
    display_name: str = Field(
        min_length=1,
        max_length=roadmap_store.MAX_DISPLAY_NAME_LEN,
    )
    summary: str = Field(
        min_length=1,
        max_length=roadmap_store.MAX_SUMMARY_LEN,
    )
    status: Literal["pending", "active", "done"] = "pending"
    sort_order: int = 0
    body_md: str = Field(
        default="",
        max_length=roadmap_store.MAX_BODY_LEN,
    )
    effort_estimate: str = Field(
        default="",
        max_length=roadmap_store.MAX_EFFORT_LEN,
    )
    in_progress_note: str = Field(
        default="",
        max_length=roadmap_store.MAX_NOTE_LEN,
    )
    audit_checkpoint: str = Field(
        default="",
        max_length=roadmap_store.MAX_AUDIT_LEN,
    )


class _RoadmapPatchBody(BaseModel):
    """Partial update — every field optional. ``None`` means
    "don't touch this column"; the store layer handles the
    partial UPDATE. ``phase_key`` is intentionally absent: it is
    immutable post-create."""

    display_name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=roadmap_store.MAX_DISPLAY_NAME_LEN,
    )
    summary: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=roadmap_store.MAX_SUMMARY_LEN,
    )
    status: Optional[Literal["pending", "active", "done"]] = None
    sort_order: Optional[int] = None
    body_md: Optional[str] = Field(
        default=None,
        max_length=roadmap_store.MAX_BODY_LEN,
    )
    effort_estimate: Optional[str] = Field(
        default=None,
        max_length=roadmap_store.MAX_EFFORT_LEN,
    )
    in_progress_note: Optional[str] = Field(
        default=None,
        max_length=roadmap_store.MAX_NOTE_LEN,
    )
    audit_checkpoint: Optional[str] = Field(
        default=None,
        max_length=roadmap_store.MAX_AUDIT_LEN,
    )


class _RoadmapReorderBody(BaseModel):
    """Drag-and-drop reorder payload — list of phase ids in the
    new top-to-bottom order. The store assigns ``sort_order`` as
    multiples of 10 so a subsequent drag-insert between two
    phases doesn't require renumbering the table."""

    ids: list[int] = Field(min_length=1, max_length=200)


# ── Response shaping ───────────────────────────────────────────────────────


def _phase_to_public_json(phase: dict) -> dict:
    """Public ``/api/roadmap`` shape: drops draft-only fields and
    adds ``body_html`` with the markdown rendered through the
    bleach sanitiser. The SPA drops this straight into the DOM via
    ``innerHTML`` — no client-side sanitisation needed.

    Fields the public endpoint deliberately does NOT expose:
    ``id`` (admin-side handle, not stable public reference; use
    ``phase_key`` instead), ``is_published`` (drafts aren't even
    in the result set), ``created_at``, ``updated_at`` (admin
    bookkeeping)."""
    return {
        "phase_key": phase["phase_key"],
        "display_name": phase["display_name"],
        "summary": phase["summary"],
        "status": phase["status"],
        "sort_order": phase["sort_order"],
        "body_md": phase["body_md"],
        "body_html": render_markdown(phase["body_md"]) if phase["body_md"] else "",
        "effort_estimate": phase["effort_estimate"],
        "in_progress_note": phase["in_progress_note"],
        "audit_checkpoint": phase["audit_checkpoint"],
        "published_at": phase["published_at"],
    }


def _phase_to_admin_json(phase: dict) -> dict:
    """Admin shape: carries the raw markdown in ``body_md`` so the
    edit form can round-trip it, plus the pre-rendered
    ``body_html`` for preview. Admin-only bookkeeping fields
    (``is_published``, ``created_at``, ``updated_at``) are
    included."""
    return {
        "id": phase["id"],
        "phase_key": phase["phase_key"],
        "display_name": phase["display_name"],
        "summary": phase["summary"],
        "status": phase["status"],
        "sort_order": phase["sort_order"],
        "body_md": phase["body_md"],
        "body_html": render_markdown(phase["body_md"]) if phase["body_md"] else "",
        "effort_estimate": phase["effort_estimate"],
        "in_progress_note": phase["in_progress_note"],
        "audit_checkpoint": phase["audit_checkpoint"],
        "is_published": phase["is_published"],
        "created_at": phase["created_at"],
        "updated_at": phase["updated_at"],
        "published_at": phase["published_at"],
    }


# ── Public endpoint ────────────────────────────────────────────────────────


@router.get("/api/roadmap")
@limiter.limit("30/minute")
async def api_roadmap_public(
    request: Request,
    user: User = Depends(_request_user),
):
    """Roadmap timeline for the in-app SPA (logged-in only).

    Used to be in ``web.app._PUBLIC_PATHS`` for the public-shell
    PR; PR 3 of the marketing-app split removed that and re-
    added the session-cookie requirement. Logged-out visitors
    now read the roadmap on the static marketing site at
    https://reverto.bot, which is fed by snapshot writes in
    ``core.marketing_export`` rather than by this endpoint.

    The response shape still strips admin-only fields via
    ``_phase_to_public_json`` so a future re-opening of the
    endpoint to anonymous callers (unlikely but possible) would
    not regress the admin/public boundary.
    """
    phases = roadmap_store.list_published(limit=100)
    return {"phases": [_phase_to_public_json(p) for p in phases]}


# ── Admin endpoints ────────────────────────────────────────────────────────


@router.get("/api/admin/roadmap")
@limiter.limit("120/minute")
async def api_admin_roadmap_list(
    request: Request,
    user: User = Depends(_require_admin_user),
):
    phases = roadmap_store.list_all(include_unpublished=True)
    return {"phases": [_phase_to_admin_json(p) for p in phases]}


@router.post("/api/admin/roadmap", status_code=201)
@limiter.limit("30/minute")
async def api_admin_roadmap_create(
    body: _RoadmapCreateBody,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    try:
        phase_id = roadmap_store.create_phase(
            phase_key=body.phase_key,
            display_name=body.display_name,
            summary=body.summary,
            status=body.status,
            sort_order=body.sort_order,
            body_md=body.body_md,
            effort_estimate=body.effort_estimate,
            in_progress_note=body.in_progress_note,
            audit_checkpoint=body.audit_checkpoint,
        )
    except roadmap_store.RoadmapPhaseKeyConflict as e:
        # 409 Conflict for duplicate key — distinct from the
        # generic 400 Bad Request for other validation failures so
        # the admin UI can render a targeted "this phase_key is
        # already taken" message.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit(
        "roadmap_api_create", user.username, f"id={phase_id}",
        user_id=user.id,
    )
    phase = roadmap_store.get_phase(phase_id)
    return _phase_to_admin_json(phase)


@router.get("/api/admin/roadmap/{phase_id}")
@limiter.limit("120/minute")
async def api_admin_roadmap_read(
    phase_id: int,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    phase = roadmap_store.get_phase(phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail="Phase not found")
    return _phase_to_admin_json(phase)


@router.patch("/api/admin/roadmap/{phase_id}")
@limiter.limit("30/minute")
async def api_admin_roadmap_update(
    phase_id: int,
    body: _RoadmapPatchBody,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    if roadmap_store.get_phase(phase_id) is None:
        raise HTTPException(status_code=404, detail="Phase not found")
    # ``model_dump(exclude_unset=True)`` would also work but the
    # store layer's contract is "None means don't touch", so we
    # forward the dict as-is and let the store filter.
    payload = body.model_dump(exclude_unset=False)
    try:
        roadmap_store.update_phase(phase_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit(
        "roadmap_api_update", user.username, f"id={phase_id}",
        user_id=user.id,
    )
    _snapshot_marketing_roadmap()
    phase = roadmap_store.get_phase(phase_id)
    return _phase_to_admin_json(phase)


@router.post("/api/admin/roadmap/reorder")
@limiter.limit("30/minute")
async def api_admin_roadmap_reorder(
    body: _RoadmapReorderBody,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    """Drag-and-drop reorder. Body: ``{"ids": [int, int, ...]}``
    in the new top-to-bottom order. Atomic: the store wraps the
    UPDATE batch in a single transaction so a partial failure
    rolls back to the prior order."""
    # Validate every id exists first — a typo'd id past the
    # validation gate would silently leave that phase at its
    # current sort_order while every other phase moved, producing
    # a confusing partial reorder. Pre-checking is cheap.
    missing: list[int] = []
    for phase_id in body.ids:
        if roadmap_store.get_phase(phase_id) is None:
            missing.append(phase_id)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown phase ids: {missing}",
        )
    roadmap_store.reorder_phases(body.ids)
    _audit(
        "roadmap_api_reorder", user.username,
        f"count={len(body.ids)}", user_id=user.id,
    )
    _snapshot_marketing_roadmap()
    return {"ok": True, "count": len(body.ids)}


@router.post("/api/admin/roadmap/{phase_id}/publish")
@limiter.limit("30/minute")
async def api_admin_roadmap_publish(
    phase_id: int,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    if not roadmap_store.publish_phase(phase_id):
        raise HTTPException(status_code=404, detail="Phase not found")
    _audit(
        "roadmap_api_publish", user.username, f"id={phase_id}",
        user_id=user.id,
    )
    _snapshot_marketing_roadmap()
    phase = roadmap_store.get_phase(phase_id)
    return _phase_to_admin_json(phase)


@router.post("/api/admin/roadmap/{phase_id}/unpublish")
@limiter.limit("30/minute")
async def api_admin_roadmap_unpublish(
    phase_id: int,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    if not roadmap_store.unpublish_phase(phase_id):
        raise HTTPException(status_code=404, detail="Phase not found")
    _audit(
        "roadmap_api_unpublish", user.username, f"id={phase_id}",
        user_id=user.id,
    )
    _snapshot_marketing_roadmap()
    phase = roadmap_store.get_phase(phase_id)
    return _phase_to_admin_json(phase)


@router.delete("/api/admin/roadmap/{phase_id}", status_code=204)
@limiter.limit("30/minute")
async def api_admin_roadmap_delete(
    phase_id: int,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    if not roadmap_store.delete_phase(phase_id):
        raise HTTPException(status_code=404, detail="Phase not found")
    _audit(
        "roadmap_api_delete", user.username, f"id={phase_id}",
        user_id=user.id,
    )
    _snapshot_marketing_roadmap()
    return JSONResponse(content=None, status_code=204)
