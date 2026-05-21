"""Admin endpoints that drive the static marketing site at
``https://reverto.bot``.

Surface:
  POST /api/admin/marketing/regenerate — admin-only, force-rewrite
  both ``roadmap.json`` and ``changelog.json`` to
  ``/var/www/reverto-marketing/data/``. Used as the manual recovery
  path when a snapshot drifts from the DB (e.g. the publish/
  unpublish hook was non-fatal-skipped because of a transient
  filesystem error).

Auto-export hooks live on ``web.routes.roadmap`` /
``web.routes.changelog`` directly — every publish/unpublish/edit/
delete/reorder triggers a best-effort snapshot rewrite there, so
this regenerate endpoint should rarely be needed.

Admin gate mirrors ``web/routes/changelog.py::_require_admin_user``
byte-for-byte; consolidating the gate is a future refactor.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from core import marketing_export
from core.user import User
from web.app import _audit, _request_actor, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["marketing"])


def _require_admin_user(
    user: User = Depends(_request_user),
) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.post("/api/admin/marketing/regenerate")
@limiter.limit("10/minute")
async def api_marketing_regenerate(
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_require_admin_user),
):
    """Force-rewrite both marketing snapshots.

    Returns:
      * 200 OK with ``{"status": "ok", "results": {...}}`` when
        both snapshots wrote successfully.
      * 207 Multi-Status with ``{"status": "partial", ...}`` when
        exactly one snapshot wrote and the other failed.
      * 500 with ``{"status": "failed", ...}`` when both writes
        failed (typically a permissions issue on the data dir —
        see logs).

    The two writes are independent — the response body always
    includes the per-snapshot result so the operator UI can
    surface a precise error.
    """
    results = marketing_export.write_all_snapshots()
    # PT-v4-AZ-001: canonical _audit shape is (action, target_id,
    # actor, user_id=...). marketing_regenerate is a fleet action
    # with no per-row target, so slug="-" matches the emergency_stop
    # pattern (web/routes/admin.py:126). The per-snapshot result
    # detail used to live in the key_hint position (overriding
    # actor); operators reading the snapshot results should consult
    # the portal log + the response body, both of which still carry
    # the full per-snapshot breakdown.
    _audit(
        "marketing_regenerate", "-", actor,
        user_id=user.id, request=request,
        result="ok" if all(results.values()) else "error",
    )
    if all(results.values()):
        return {"status": "ok", "results": results}
    if any(results.values()):
        return JSONResponse(
            status_code=207,
            content={"status": "partial", "results": results},
        )
    return JSONResponse(
        status_code=500,
        content={"status": "failed", "results": results},
    )
