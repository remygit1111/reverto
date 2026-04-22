"""Workspace dashboard layout persistence endpoints.

PR 1 of the Workspace feature — backend-only. Frontend integration
arrives in PR 2+.

Scope: one layout per user, named "default". The schema supports
multiple named layouts (see ``dashboard_store.DEFAULT_LAYOUT_NAME``)
but the API surface only speaks to the default row — add a ``?name=``
param + a listing endpoint when the multi-layout UI lands.

Circular-import shape mirrors every other route module: this file is
imported at the BOTTOM of ``web/app.py`` so every name pulled from
``web.app`` is already defined at import time.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import dashboard_store
from core.user import User
from web.app import _audit, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])


class LayoutBody(BaseModel):
    """Request body for PUT /api/dashboard/layout.

    The ``layout`` field is opaque: it's whatever JSON-serialisable
    dict the frontend chooses. Size-cap enforcement lives in
    ``dashboard_store.put_layout`` so a non-HTTP caller (CLI,
    future migration script) still gets the same protection.
    """

    layout: dict[str, Any] = Field(
        ..., description="Panel configuration blob",
    )


@router.get("/api/dashboard/layout")
@limiter.limit("30/minute")
async def get_dashboard_layout(
    request: Request,
    user: User = Depends(_request_user),
):
    """Return the caller's default layout.

    Response shape: ``{"layout": {...}}`` or ``{"layout": null}``
    when no layout is stored yet. The frontend treats ``null`` as
    "show empty-state / first-run" — we deliberately do NOT ship a
    server-side default layout so a future layout-schema change
    stays a frontend-only deploy.

    Corrupt stored JSON also surfaces as ``null`` (the store logs
    a warning) so a malformed blob doesn't break the page; the
    user can rebuild their layout on top of empty-state.
    """
    try:
        layout = dashboard_store.get_layout(user.id)
    except ValueError:
        # Corrupt stored JSON. The store already logged the details;
        # falling through to null lets the frontend reset rather
        # than crash on a 500.
        return {"layout": None}
    return {"layout": layout}


@router.put("/api/dashboard/layout")
@limiter.limit("30/minute")
async def put_dashboard_layout(
    request: Request,
    body: LayoutBody,
    user: User = Depends(_request_user),
):
    """Overwrite the caller's default layout.

    Idempotent: auto-creates the row if absent, overwrites in place
    otherwise (see ``dashboard_store.put_layout`` for the
    INSERT ... ON CONFLICT details). ValueError from the store
    (size-cap or non-serialisable payload) maps to 400 — both are
    client-side mistakes.
    """
    try:
        dashboard_store.put_layout(user.id, body.layout)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    _audit("dashboard_layout_put", str(user.id), user.username)
    return {"ok": True}
