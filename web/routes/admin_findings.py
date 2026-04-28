"""Admin-only routes for the audit/pentest findings tracker.

Backend for /admin/findings dashboard. Endpoints:

  GET  /api/admin/findings                  — filtered list
  GET  /api/admin/findings/{finding_id}     — single finding
  PATCH /api/admin/findings/{finding_id}    — partial update of
                                              status / notes / resolution_ref

All endpoints gate on ``user.role == 'admin'`` via the same
``_require_admin_user`` pattern the changelog admin routes use
(audit v26-02 / r1-002). Mutating endpoints inherit the global
CSRFMiddleware protection (cookie + header double-submit) so no
per-route CSRF code lives here.

The markdown audit-docs (docs/audits/, docs/pentests/) remain the
authoritative source for *what* each finding is. This module owns
the operator-mutable bits: status, notes, resolution_ref. Seed
import (scripts/seed_audit_findings.py) populates the table on
first boot; subsequent operator edits flow through PATCH here.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import audit_findings_store
from core.user import User
from web.app import _audit, _request_actor, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-findings"])


# ── Admin gate ─────────────────────────────────────────────────────────────

def _require_admin_user(
    user: User = Depends(_request_user),
) -> User:
    """Admin-only dependency. Mirrors changelog.py's _require_admin_user
    so a future role-system refactor (admin → fine-grained perms) only
    has to update one call site per module rather than spreading new
    decorators across every route."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Request bodies ─────────────────────────────────────────────────────────

class _FindingPatchBody(BaseModel):
    """Partial update for the three operator-mutable columns. All
    fields optional; ``None`` means "do not touch." Length caps mirror
    the soft caps in ``audit_findings_store`` so an oversized input
    returns 422 before reaching the DB."""

    status: Optional[Literal[
        "open", "in_progress", "resolved", "accepted", "deferred",
    ]] = None
    notes: Optional[str] = Field(
        default=None, max_length=audit_findings_store.MAX_NOTES_LEN,
    )
    resolution_ref: Optional[str] = Field(
        default=None,
        max_length=audit_findings_store.MAX_RESOLUTION_REF_LEN,
    )


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/api/admin/findings")
@limiter.limit("60/minute")
async def api_admin_findings_list(
    request: Request,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    source_doc: Optional[str] = None,
    user: User = Depends(_require_admin_user),
):
    """Return all findings matching the optional filter triple, plus
    a stats roll-up so the admin UI can render counts without a second
    round-trip. Sort order (CRITICAL first) is owned by the store.
    """
    try:
        items = audit_findings_store.list_findings(
            status=status, severity=severity, source_doc=source_doc,
        )
    except ValueError as e:
        # Unknown filter value — surface as 400 so the UI's filter
        # dropdown drift gets noticed.
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "findings": items,
        "stats": {
            "total": audit_findings_store.count_total(),
            "by_status": audit_findings_store.count_by_status(),
        },
    }


@router.get("/api/admin/findings/{finding_id}")
@limiter.limit("60/minute")
async def api_admin_findings_get(
    finding_id: str,
    request: Request,
    user: User = Depends(_require_admin_user),
):
    """Single-finding lookup for the detail-modal flow."""
    finding = audit_findings_store.get_finding(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@router.patch("/api/admin/findings/{finding_id}")
@limiter.limit("30/minute")
async def api_admin_findings_update(
    finding_id: str,
    body: _FindingPatchBody,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_require_admin_user),
):
    """Operator-driven partial update of status / notes /
    resolution_ref. Audit-logged so a status flip is traceable to the
    admin who made it."""
    if audit_findings_store.get_finding(finding_id) is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    try:
        changed = audit_findings_store.update_finding(
            finding_id,
            status=body.status,
            notes=body.notes,
            resolution_ref=body.resolution_ref,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if changed:
        # Match the standard ``_audit(action, slug, actor, user_id=N)``
        # shape used by every other mutating route (bot_start,
        # exchange_keys_set, emergency_stop, etc.). Position-2 is the
        # *target* identifier — the finding being edited — and
        # position-3 is the *actor* string ("session:<username>")
        # produced by ``_request_actor``. The pre-fix call had these
        # swapped which produced inconsistent audit-log lines.
        # Change-context (which fields moved) is intentionally not
        # encoded in the audit line — the DB row's ``updated_at``
        # plus the post-update state are the source of truth for
        # *what* changed; the audit log captures *who* / *when*.
        _audit(
            "admin_finding_update",
            finding_id,
            actor,
            user_id=user.id,
            request=request,
        )
    return audit_findings_store.get_finding(finding_id)
