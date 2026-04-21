"""Changelog + admin routes.

JSON-API surface (SPA-facing):
  GET    /api/changelog                        — public (logged-in) list
  GET    /api/admin/changelog                  — admin list incl. drafts
  POST   /api/admin/changelog                  — admin create
  GET    /api/admin/changelog/{id}             — admin read single
  PATCH  /api/admin/changelog/{id}             — admin partial update
  POST   /api/admin/changelog/{id}/publish     — admin publish
  POST   /api/admin/changelog/{id}/unpublish   — admin unpublish
  DELETE /api/admin/changelog/{id}             — admin delete

Legacy server-rendered surface (retained for this commit; removed in
the final cleanup phase of the SPA-integration refactor):
  GET  /changelog, /admin, /admin/changelog*, POST form endpoints

Admin gate: Phase-3b role-checks aren't wired yet (audit v26-02 —
emergency-stop has the same hole). For now "admin" == ``user.id == 1``;
``_require_admin_user`` is the one place that decision lives so the
Phase-3b swap is a one-line change.
"""

from __future__ import annotations

import html
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    """Admin-only dependency. Today "admin" is the seeded user_id=1.

    Once Phase-3b role-checks ship this becomes ``user.role == 'admin'``
    and the user-id literal goes away; the swap lives in one place so
    every admin route picks it up automatically. Emergency-stop has the
    same shape in audit v26-02 — follow this helper when that lands too.
    """
    if user.id != 1:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── JSON-API ───────────────────────────────────────────────────────────────
# Shapes + helpers used by the SPA. The legacy server-rendered routes
# further down still exist for this commit; the cleanup phase of the
# refactor removes them once the SPA has fully taken over.


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


@router.get("/api/changelog")
@limiter.limit("120/minute")
async def api_changelog_public(
    request: Request, user: User = Depends(_request_user),
):
    entries = changelog_store.list_published(limit=50)
    return {"entries": [_entry_to_public_json(e) for e in entries]}


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
    # 204 No Content — Starlette treats a JSONResponse with no body
    # as an empty response of the status code set above.
    return JSONResponse(content=None, status_code=204)


# ── Shared HTML helpers ────────────────────────────────────────────────────

_CATEGORY_LABELS = {
    "feature": "Feature",
    "fix": "Fix",
    "improvement": "Improvement",
    "security": "Security",
}


def _category_badge(category: str) -> str:
    label = _CATEGORY_LABELS.get(category, category)
    safe_cat = html.escape(category)
    return (
        f'<span class="cl-badge cl-badge-{safe_cat}">'
        f'{html.escape(label)}</span>'
    )


def _format_timestamp(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    # SQLite's datetime('now') emits "YYYY-MM-DD HH:MM:SS"; take the
    # date half for the user-facing surface (matches "when was this
    # feature added" rather than "exactly when the admin clicked save").
    return html.escape(ts.split(" ")[0])


# Nav items rendered in the portal-consistent header. ``active`` marks
# the row whose href prefix matches the current page so the correct
# tab lights up. The SPA-side tabs (Overview / Bots / Deals / ...)
# land under "Dashboard" because they live inside the SPA route (``/``).
_NAV_ITEMS = (
    ("Dashboard", "/"),
    ("Changelog", "/changelog"),
    ("Admin", "/admin"),
)


def _nav_html(current_path: str, *, show_admin: bool) -> str:
    """Render the portal's main-nav bar for the server-rendered pages.

    ``active`` hits when the current path is the nav target OR sits
    underneath it (``/admin/changelog`` keeps "Admin" highlighted).
    ``show_admin=False`` hides the Admin tab for non-admin users; the
    SPA has the same rule via ``body.is-admin`` + data-admin-only.
    """
    pieces = []
    for label, href in _NAV_ITEMS:
        if label == "Admin" and not show_admin:
            continue
        is_active = current_path == href or (
            href != "/" and current_path.startswith(href)
        )
        if href == "/" and current_path != "/":
            is_active = False
        cls = "tab active" if is_active else "tab"
        pieces.append(f'<a class="{cls}" href="{href}">{html.escape(label)}</a>')
    return f'<nav id="main-nav">{"".join(pieces)}</nav>'


def _page_shell(
    title: str,
    body: str,
    *,
    current_path: str,
    show_admin: bool,
) -> str:
    """Wrap a body fragment in portal-consistent chrome.

    Mirrors the SPA's header shape (logo + subtitle + spacer) + nav
    bar so the server-rendered changelog / admin pages don't feel
    like a different app. Picks up every design token from
    ``style.css`` (font, colours, spacing); ``changelog.css`` holds
    only the category-badge palette which is changelog-specific.
    """
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)} — Reverto</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="stylesheet" href="/static/style.css?v=77">
<link rel="stylesheet" href="/static/changelog.css?v=2">
</head>
<body>
<header>
  <a class="logo" href="/">Reverto</a>
  <span class="logo-sub">{html.escape(title)}</span>
  <div class="header-spacer"></div>
</header>
{_nav_html(current_path, show_admin=show_admin)}
<main class="page active cl-page">
{body}
</main>
</body>
</html>"""


def _render_entry_public(entry: dict) -> str:
    body_html = render_markdown(entry["description"])
    return f"""<article class="card cl-entry">
  <div class="cl-entry-header">
    <h2 class="cl-entry-title">{html.escape(entry['title'])}</h2>
    <div class="cl-entry-meta">
      {_category_badge(entry['category'])}
      <span class="cl-entry-date">{_format_timestamp(entry['published_at'])}</span>
    </div>
  </div>
  <div class="cl-entry-body">{body_html}</div>
</article>"""


def _render_admin_row(entry: dict) -> str:
    eid = int(entry["id"])
    status_cls = "cl-status-published" if entry["is_published"] else "cl-status-draft"
    status_lbl = "Published" if entry["is_published"] else "Draft"
    publish_btn = (
        f'<form method="post" action="/admin/changelog/{eid}/unpublish" '
        f'class="cl-inline-form">'
        f'<button type="submit" class="hbtn hbtn-theme">Unpublish</button>'
        f'</form>'
        if entry["is_published"]
        else
        f'<form method="post" action="/admin/changelog/{eid}/publish" '
        f'class="cl-inline-form">'
        f'<button type="submit" class="hbtn hbtn-theme btn-accent">Publish</button>'
        f'</form>'
    )
    return f"""<tr>
  <td>{html.escape(entry['title'])}</td>
  <td>{_category_badge(entry['category'])}</td>
  <td><span class="cl-status {status_cls}">{status_lbl}</span></td>
  <td>{_format_timestamp(entry['created_at'])}</td>
  <td class="cl-actions">
    <a class="hbtn hbtn-theme" href="/admin/changelog/{eid}/edit">Edit</a>
    {publish_btn}
    <form method="post" action="/admin/changelog/{eid}/delete"
          class="cl-inline-form"
          onsubmit="return confirm('Delete this entry? This cannot be undone.');">
      <button type="submit" class="hbtn hbtn-theme btn-danger">Delete</button>
    </form>
  </td>
</tr>"""


def _render_form(
    *,
    action: str,
    entry: Optional[dict] = None,
    error: Optional[str] = None,
) -> str:
    title = html.escape(entry["title"]) if entry else ""
    description = html.escape(entry["description"]) if entry else ""
    current_cat = entry["category"] if entry else "feature"
    options = "\n".join(
        f'<option value="{cat}"{" selected" if cat == current_cat else ""}>'
        f'{html.escape(label)}</option>'
        for cat, label in _CATEGORY_LABELS.items()
    )
    error_html = (
        f'<div class="cl-form-error">{html.escape(error)}</div>'
        if error else ""
    )
    is_edit = entry is not None
    unpublish_btn = (
        f'<form method="post" action="/admin/changelog/{entry["id"]}/unpublish" '
        f'class="cl-inline-form">'
        f'<button type="submit" class="hbtn hbtn-theme">Unpublish</button>'
        f'</form>'
        if is_edit and entry and entry.get("is_published")
        else ""
    )
    return f"""<form class="card cl-form" method="post" action="{action}">
  {error_html}
  <label class="cl-label" for="cl-title">Title</label>
  <input class="cl-input" id="cl-title" name="title" type="text"
         maxlength="{changelog_store.MAX_TITLE_LEN}" required value="{title}">

  <label class="cl-label" for="cl-category">Category</label>
  <select class="cl-input" id="cl-category" name="category" required>
    {options}
  </select>

  <label class="cl-label" for="cl-description">Description (markdown)</label>
  <textarea class="cl-input cl-textarea" id="cl-description" name="description"
            maxlength="{changelog_store.MAX_DESCRIPTION_LEN}" required
            rows="12">{description}</textarea>

  <div class="cl-form-actions">
    <a class="hbtn hbtn-theme" href="/admin/changelog">Cancel</a>
    <button type="submit" name="action" value="draft" class="hbtn hbtn-theme">
      Save as draft
    </button>
    <button type="submit" name="action" value="publish" class="hbtn hbtn-theme btn-accent">
      Save &amp; publish
    </button>
    {unpublish_btn}
  </div>
</form>"""


_ADMIN_BREADCRUMB = (
    '<div class="cl-breadcrumb">'
    '<a href="/admin">&larr; Back to Admin</a>'
    '</div>'
)


def _is_admin_id(user: User) -> bool:
    """Mirror of ``_require_admin_user``'s decision, used where we
    only want a bool — specifically the nav-rendering path on the
    public /changelog page, which has to know whether to show the
    Admin tab without failing the request."""
    return user.id == 1


# ── Public route ───────────────────────────────────────────────────────────

@router.get("/changelog", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def changelog_public(
    request: Request, user: User = Depends(_request_user),
):
    entries = changelog_store.list_published(limit=50)
    if not entries:
        body = (
            '<h1 class="section-title cl-page-title">What\'s new</h1>'
            '<div class="card cl-empty">No updates yet.</div>'
        )
    else:
        entries_html = "\n".join(_render_entry_public(e) for e in entries)
        body = (
            '<h1 class="section-title cl-page-title">What\'s new</h1>'
            f'<div class="cl-entries">{entries_html}</div>'
        )
    return HTMLResponse(_page_shell(
        "Changelog", body,
        current_path="/changelog",
        show_admin=_is_admin_id(user),
    ))


# ── Admin overview ─────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def admin_index(
    request: Request, user: User = Depends(_require_admin_user),
):
    """Admin-area landing page. Currently lists a single action
    ("Manage Changelog"); this is the extensible slot where future
    admin surfaces (emergency-stop UI, user management, …) plug in.
    """
    body = """
<h1 class="section-title cl-page-title">Admin</h1>
<div class="cl-admin-grid">
  <a class="card cl-admin-card" href="/admin/changelog">
    <div class="cl-admin-card-title">Manage Changelog</div>
    <div class="cl-admin-card-desc">
      Create, edit, publish and delete changelog entries shown on
      /changelog.
    </div>
  </a>
</div>
"""
    return HTMLResponse(_page_shell(
        "Admin", body,
        current_path="/admin",
        show_admin=True,
    ))


# ── Admin changelog routes ─────────────────────────────────────────────────

@router.get("/admin/changelog", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def admin_changelog_list(
    request: Request, user: User = Depends(_require_admin_user),
):
    entries = changelog_store.list_all(include_unpublished=True)
    if not entries:
        rows_html = (
            '<tr><td colspan="5" class="cl-empty-cell">'
            'No entries yet. Click "New entry" to add the first one.'
            '</td></tr>'
        )
    else:
        rows_html = "\n".join(_render_admin_row(e) for e in entries)
    body = f"""
{_ADMIN_BREADCRUMB}
<div class="cl-admin-header">
  <h1 class="section-title cl-page-title">Changelog — admin</h1>
  <a class="hbtn hbtn-theme btn-accent" href="/admin/changelog/new">+ New entry</a>
</div>
<div class="card card-no-pad">
  <table class="cl-admin-table">
    <thead>
      <tr>
        <th>Title</th>
        <th>Category</th>
        <th>Status</th>
        <th>Created</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</div>
"""
    return HTMLResponse(_page_shell(
        "Changelog admin", body,
        current_path="/admin/changelog",
        show_admin=True,
    ))


@router.get("/admin/changelog/new", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def admin_changelog_new_form(
    request: Request, user: User = Depends(_require_admin_user),
):
    body = (
        f'{_ADMIN_BREADCRUMB}'
        '<h1 class="section-title cl-page-title">New changelog entry</h1>'
        + _render_form(action="/admin/changelog")
    )
    return HTMLResponse(_page_shell(
        "New entry", body,
        current_path="/admin/changelog",
        show_admin=True,
    ))


@router.post("/admin/changelog", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def admin_changelog_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    action: str = Form("draft"),
    user: User = Depends(_require_admin_user),
):
    try:
        entry_id = changelog_store.create_entry(
            title=title, description=description, category=category,
        )
    except ValueError as e:
        body = (
            f'{_ADMIN_BREADCRUMB}'
            '<h1 class="section-title cl-page-title">New changelog entry</h1>'
            + _render_form(action="/admin/changelog", error=str(e))
        )
        return HTMLResponse(
            _page_shell(
                "New entry", body,
                current_path="/admin/changelog",
                show_admin=True,
            ),
            status_code=400,
        )
    if action == "publish":
        changelog_store.publish_entry(entry_id)
        _audit("changelog_create_publish", user.username, f"id={entry_id}")
    else:
        _audit("changelog_create_draft", user.username, f"id={entry_id}")
    return RedirectResponse(url="/admin/changelog", status_code=303)


@router.get("/admin/changelog/{entry_id}/edit", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def admin_changelog_edit_form(
    entry_id: int, request: Request,
    user: User = Depends(_require_admin_user),
):
    entry = changelog_store.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    body = (
        f'{_ADMIN_BREADCRUMB}'
        f'<h1 class="section-title cl-page-title">Edit entry #{entry_id}</h1>'
        + _render_form(
            action=f"/admin/changelog/{entry_id}", entry=entry,
        )
    )
    return HTMLResponse(_page_shell(
        "Edit entry", body,
        current_path="/admin/changelog",
        show_admin=True,
    ))


@router.post("/admin/changelog/{entry_id}", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def admin_changelog_update(
    entry_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    action: str = Form("draft"),
    user: User = Depends(_require_admin_user),
):
    existing = changelog_store.get_entry(entry_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    try:
        changelog_store.update_entry(
            entry_id,
            title=title,
            description=description,
            category=category,
        )
    except ValueError as e:
        # Re-render the edit form with the submitted values + error.
        preview = {
            **existing,
            "title": title,
            "description": description,
            "category": category if category in changelog_store.VALID_CATEGORIES
                        else existing["category"],
        }
        body = (
            f'{_ADMIN_BREADCRUMB}'
            f'<h1 class="section-title cl-page-title">Edit entry #{entry_id}</h1>'
            + _render_form(
                action=f"/admin/changelog/{entry_id}",
                entry=preview,
                error=str(e),
            )
        )
        return HTMLResponse(
            _page_shell(
                "Edit entry", body,
                current_path="/admin/changelog",
                show_admin=True,
            ),
            status_code=400,
        )
    if action == "publish":
        changelog_store.publish_entry(entry_id)
        _audit("changelog_update_publish", user.username, f"id={entry_id}")
    else:
        _audit("changelog_update", user.username, f"id={entry_id}")
    return RedirectResponse(url="/admin/changelog", status_code=303)


@router.post("/admin/changelog/{entry_id}/publish")
@limiter.limit("30/minute")
async def admin_changelog_publish(
    entry_id: int, request: Request,
    user: User = Depends(_require_admin_user),
):
    if not changelog_store.publish_entry(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    _audit("changelog_publish", user.username, f"id={entry_id}")
    return RedirectResponse(url="/admin/changelog", status_code=303)


@router.post("/admin/changelog/{entry_id}/unpublish")
@limiter.limit("30/minute")
async def admin_changelog_unpublish(
    entry_id: int, request: Request,
    user: User = Depends(_require_admin_user),
):
    if not changelog_store.unpublish_entry(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    _audit("changelog_unpublish", user.username, f"id={entry_id}")
    return RedirectResponse(url="/admin/changelog", status_code=303)


@router.post("/admin/changelog/{entry_id}/delete")
@limiter.limit("30/minute")
async def admin_changelog_delete(
    entry_id: int, request: Request,
    user: User = Depends(_require_admin_user),
):
    if not changelog_store.delete_entry(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    _audit("changelog_delete", user.username, f"id={entry_id}")
    return RedirectResponse(url="/admin/changelog", status_code=303)
