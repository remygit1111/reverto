"""Changelog JSON-API routes — powers the /changelog and /admin SPA tabs.

Surface:
  GET    /api/changelog                        — public (logged-in) list
  GET    /api/admin/changelog                  — admin list incl. drafts
  POST   /api/admin/changelog                  — admin create
  GET    /api/admin/changelog/{id}             — admin read single
  PATCH  /api/admin/changelog/{id}             — admin partial update
  POST   /api/admin/changelog/{id}/publish     — admin publish
  POST   /api/admin/changelog/{id}/unpublish   — admin unpublish
  DELETE /api/admin/changelog/{id}             — admin delete

Admin gate: ``_require_admin_user`` checks ``user.role == 'admin'`` —
same pattern as emergency-stop in ``web/routes/admin.py`` (v26-02).

The server-rendered HTML variants (``/changelog``, ``/admin``,
``/admin/changelog``, POST form endpoints) used to live here. They
were removed once the SPA-integrated tabs replaced them; see
``refactor/changelog-spa-integration``.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import changelog_store
from core.markdown_render import render_markdown
from core.user import User
from web.app import _audit, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["changelog"])


# ── Admin gate ─────────────────────────────────────────────────────────────

def _require_admin_user(
    user: User = Depends(_request_user),
) -> User:
    """Admin-only dependency. Gates on ``user.role == 'admin'`` — matches
    the emergency-stop pattern in ``web/routes/admin.py`` (audit v26-02)
    and accepts any admin, not just the seeded id=1.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Request bodies ─────────────────────────────────────────────────────────

class _ChangelogCreateBody(BaseModel):
    """Create payload — every field required. Length caps mirror the
    soft caps enforced by ``core.changelog_store`` so an oversized
    input fails fast with 422 before it hits the DB layer."""
    title: str = Field(min_length=1, max_length=changelog_store.MAX_TITLE_LEN)
    description: str = Field(
        min_length=1, max_length=changelog_store.MAX_DESCRIPTION_LEN,
    )
    category: str = Field(min_length=1, max_length=32)


class _ChangelogPatchBody(BaseModel):
    """Partial update — every field optional. ``None`` means "don't
    touch this column"; the store layer handles the partial UPDATE
    accordingly."""
    title: Optional[str] = Field(
        default=None, min_length=1,
        max_length=changelog_store.MAX_TITLE_LEN,
    )
    description: Optional[str] = Field(
        default=None, min_length=1,
        max_length=changelog_store.MAX_DESCRIPTION_LEN,
    )
    category: Optional[str] = Field(
        default=None, min_length=1, max_length=32,
    )


# ── Response shaping ───────────────────────────────────────────────────────

def _entry_to_public_json(entry: dict) -> dict:
    """Public /api/changelog shape: drops draft-only fields and adds
    ``description_html`` with the markdown already rendered through
    the bleach sanitiser. The SPA drops this straight into the DOM
    via ``innerHTML`` — no client-side sanitisation needed."""
    return {
        "id": entry["id"],
        "title": entry["title"],
        "category": entry["category"],
        "published_at": entry["published_at"],
        "description_html": render_markdown(entry["description"]),
    }


def _entry_to_admin_json(entry: dict) -> dict:
    """Admin /api/admin/changelog shape: carries the raw markdown in
    ``description`` so the edit form can round-trip it, plus the
    pre-rendered ``description_html`` for preview and list display.
    ``is_published`` + ``created_at`` are admin-only bookkeeping
    that the public endpoint strips."""
    return {
        "id": entry["id"],
        "title": entry["title"],
        "category": entry["category"],
        "description": entry["description"],
        "description_html": render_markdown(entry["description"]),
        "is_published": entry["is_published"],
        "created_at": entry["created_at"],
        "published_at": entry["published_at"],
        "source_commit_sha": entry["source_commit_sha"],
    }


# ── Public endpoints ───────────────────────────────────────────────────────

@router.get("/api/changelog")
@limiter.limit("120/minute")
async def api_changelog_public(
    request: Request, user: User = Depends(_request_user),
):
    entries = changelog_store.list_published(limit=50)
    return {"entries": [_entry_to_public_json(e) for e in entries]}


# ── Admin endpoints ────────────────────────────────────────────────────────

@router.get("/api/admin/changelog")
@limiter.limit("120/minute")
async def api_admin_changelog_list(
    request: Request, user: User = Depends(_require_admin_user),
):
    entries = changelog_store.list_all(include_unpublished=True)
    return {"entries": [_entry_to_admin_json(e) for e in entries]}


@router.post("/api/admin/changelog", status_code=201)
@limiter.limit("30/minute")
async def api_admin_changelog_create(
    body: _ChangelogCreateBody,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    try:
        entry_id = changelog_store.create_entry(
            title=body.title,
            description=body.description,
            category=body.category,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit("changelog_api_create", user.username, f"id={entry_id}")
    entry = changelog_store.get_entry(entry_id)
    return _entry_to_admin_json(entry)


@router.get("/api/admin/changelog/{entry_id}")
@limiter.limit("120/minute")
async def api_admin_changelog_read(
    entry_id: int,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    entry = changelog_store.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return _entry_to_admin_json(entry)


@router.patch("/api/admin/changelog/{entry_id}")
@limiter.limit("30/minute")
async def api_admin_changelog_update(
    entry_id: int,
    body: _ChangelogPatchBody,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    if changelog_store.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    try:
        changelog_store.update_entry(
            entry_id,
            title=body.title,
            description=body.description,
            category=body.category,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit("changelog_api_update", user.username, f"id={entry_id}")
    entry = changelog_store.get_entry(entry_id)
    return _entry_to_admin_json(entry)


@router.post("/api/admin/changelog/{entry_id}/publish")
@limiter.limit("30/minute")
async def api_admin_changelog_publish(
    entry_id: int,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    if not changelog_store.publish_entry(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    _audit("changelog_api_publish", user.username, f"id={entry_id}")
    entry = changelog_store.get_entry(entry_id)
    return _entry_to_admin_json(entry)


@router.post("/api/admin/changelog/{entry_id}/unpublish")
@limiter.limit("30/minute")
async def api_admin_changelog_unpublish(
    entry_id: int,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    if not changelog_store.unpublish_entry(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    _audit("changelog_api_unpublish", user.username, f"id={entry_id}")
    entry = changelog_store.get_entry(entry_id)
    return _entry_to_admin_json(entry)


@router.delete("/api/admin/changelog/{entry_id}", status_code=204)
@limiter.limit("30/minute")
async def api_admin_changelog_delete(
    entry_id: int,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    if not changelog_store.delete_entry(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    _audit("changelog_api_delete", user.username, f"id={entry_id}")
    return JSONResponse(content=None, status_code=204)
