"""Tests for the public-shell mode (logged-out access to
``/#roadmap`` + ``/#changelog``).

Closes the bug discovered after ``feature/roadmap-spa`` deploy:
a logged-out visitor at ``/#roadmap`` saw the login-form
because ``body.is-login`` was set unconditionally on auth-fail
and the nav was hidden via CSS. The public-shell fix introduces
a third auth-mode state (``body.is-public``) that reveals only
public tabs + a Log-in button for visitors arriving at a public
hash route.

These tests pin the contract at three layers:

* **Backend public-paths** (``_PUBLIC_PATHS`` includes both
  ``/api/roadmap`` AND ``/api/changelog``). The middleware lets
  these requests through without a session cookie.
* **Endpoint behaviour** (anonymous ``GET /api/changelog`` and
  ``GET /api/roadmap`` return 200 + filter drafts + omit admin-
  only fields).
* **Frontend markup** (``data-public`` is set on Roadmap and
  Changelog nav buttons; the ``#nav-login-btn`` element exists
  for the public-shell to reveal).

Frontend behaviour beyond markup (CSS rule application, JS
class-toggle on auth result) is verified by the operator's
post-deploy smoke-test scenarios documented in the PR
description — the test suite has no JS-level harness.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from core import changelog_store
from web import app as webapp


_REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Backend: _PUBLIC_PATHS contains the two routes ────────────────────────


class TestPublicPaths:
    """The auth-middleware gates every request except those in
    ``_PUBLIC_PATHS``. For the public-shell to function, both
    ``/api/roadmap`` (added in feature/roadmap-spa) and
    ``/api/changelog`` (added in this PR) must be in the set."""

    def test_roadmap_endpoint_is_public(self):
        """Pre-existing pin from feature/roadmap-spa — verified
        still passes after the public-shell PR."""
        assert "/api/roadmap" in webapp._PUBLIC_PATHS

    def test_changelog_endpoint_is_public(self):
        """NEW: this PR adds /api/changelog to _PUBLIC_PATHS so
        the SPA tab #changelog has content for logged-out
        visitors. Pre-fix the route required a session cookie
        even though semantically it returns the same data a
        logged-in user would see."""
        assert "/api/changelog" in webapp._PUBLIC_PATHS


# ── Endpoint behaviour: anonymous access to changelog ────────────────────


class TestAnonymousChangelogAccess:
    """Anonymous ``GET /api/changelog`` must:
    1. Return 200 (not 401, not 303 redirect).
    2. Filter drafts — only ``is_published = 1`` entries surface.
    3. Strip admin-only fields from the response shape.
    """

    def _anon_client(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        return client

    def test_anonymous_get_returns_200(self):
        """Pre-fix this returned 401 because the route had a
        ``Depends(_request_user)`` dependency. Post-fix the
        dependency is removed and the path is in
        ``_PUBLIC_PATHS``."""
        client = self._anon_client()
        r = client.get("/api/changelog")
        assert r.status_code == 200, r.text
        assert "entries" in r.json()

    def test_anonymous_does_not_see_drafts(self):
        """Even though the endpoint is now public, drafts MUST
        stay hidden. ``list_published()`` already filters at the
        store layer; this test pins that contract end-to-end."""
        # Create a draft only — never published.
        changelog_store.create_entry(
            "Secret upcoming feature", "body", "feature",
        )
        client = self._anon_client()
        r = client.get("/api/changelog")
        assert r.status_code == 200
        assert r.json()["entries"] == [], (
            "Drafts must NOT leak via the now-public /api/changelog. "
            "list_published() filters at the store layer; this guard "
            "catches a regression that swaps to list_all()."
        )

    def test_anonymous_does_not_see_admin_fields(self):
        """The public response shape (``_entry_to_public_json``)
        strips ``is_published``, ``created_at``, raw markdown
        ``description`` (admin uses ``description_html``-pre-
        rendered + raw round-trip; public sees only the rendered
        HTML). Pin so a future shape-edit doesn't accidentally
        expose admin metadata to anonymous callers."""
        eid = changelog_store.create_entry(
            "Public entry", "body **bold**", "feature",
        )
        changelog_store.publish_entry(eid)
        client = self._anon_client()
        r = client.get("/api/changelog")
        assert r.status_code == 200
        entry = r.json()["entries"][0]
        # Public shape carries: id, title, category, published_at,
        # description_html. NOT is_published, NOT created_at, NOT
        # raw description (markdown), NOT source_commit_sha.
        assert "is_published" not in entry, (
            "is_published is admin-only — must not surface in "
            "the public response shape."
        )
        assert "created_at" not in entry, (
            "created_at is admin-only — must not surface."
        )
        assert "description" not in entry, (
            "Raw markdown description is admin-only — public "
            "callers receive description_html (pre-rendered + "
            "sanitised) instead."
        )
        # Public-visible fields ARE present.
        assert entry["title"] == "Public entry"
        assert "<strong>bold</strong>" in entry["description_html"]


# ── Endpoint behaviour: anonymous access to roadmap (regression) ─────────


class TestAnonymousRoadmapAccess:
    """Pre-existing roadmap public-access tests live in
    ``test_roadmap_routes.py``; these are quick regression
    pins that verify the public-shell PR didn't disturb them
    (e.g. by accidentally adding an auth dependency that worked
    via the middleware bypass)."""

    def test_anonymous_roadmap_returns_200(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/api/roadmap")
        assert r.status_code == 200
        assert "phases" in r.json()


# ── Frontend markup: index.html carries the public-shell hooks ───────────


class TestPublicShellMarkup:
    """The public-shell behaviour is driven by three frontend
    markers in ``web/static/index.html``:

    * ``data-public`` attribute on Roadmap + Changelog nav
      buttons — the CSS rule
      ``body.is-public #main-nav .tab:not([data-public])`` hides
      every other tab.
    * ``#nav-login-btn`` element with the ``hidden`` attribute
      and the ``nav-login-btn`` class — revealed by the CSS rule
      ``body.is-public .nav-login-btn { display: inline-flex }``.
    * ``data-public`` MUST NOT be on the Admin tab — admins
      reach their tools via the standard logged-in flow.

    The CSS rule and the JS class-toggle are verified by the
    operator's smoke-test (incognito → /#roadmap should render
    the public-shell). These tests pin only the markup
    invariants the JS depends on.
    """

    def _read_index(self):
        return (_REPO_ROOT / "web" / "static" / "index.html").read_text(
            encoding="utf-8",
        )

    def test_roadmap_nav_button_has_data_public(self):
        html = self._read_index()
        # Locate the Roadmap nav button line and assert the attr.
        # The exact attribute syntax is ``data-public`` (no value)
        # since the CSS selector uses presence-only matching.
        assert 'id="nav-roadmap-btn" data-public' in html, (
            "Roadmap tab must carry the data-public attribute so "
            "the public-shell CSS rule reveals it for logged-out "
            "visitors."
        )

    def test_changelog_nav_button_has_data_public(self):
        html = self._read_index()
        assert 'id="nav-changelog-btn" data-public' in html, (
            "Changelog tab must carry the data-public attribute "
            "for the public-shell CSS rule."
        )

    def test_admin_nav_button_does_not_have_data_public(self):
        """Defence-in-depth: admins reach their tab via logged-in
        flow. The Admin tab uses ``data-admin-only`` (gated on
        user_id=1), NOT ``data-public``. Confusing the two would
        either leak the admin tab to anonymous visitors (bad) or
        hide it from admins (annoying)."""
        html = self._read_index()
        # Locate the admin button line.
        admin_line = next(
            (line for line in html.splitlines()
             if 'id="nav-admin-btn"' in line),
            None,
        )
        assert admin_line is not None, "Admin nav button missing"
        assert "data-public" not in admin_line, (
            "Admin tab must NOT carry data-public — admins use "
            "data-admin-only + the standard logged-in flow."
        )

    def test_nav_login_button_present_and_hidden(self):
        """The Log-in button must exist in the DOM with the
        ``hidden`` attribute (HTML-level hide). CSS reveals it
        only in body.is-public; the HTML default keeps the
        button invisible for logged-in users + visitors arriving
        at the default route (where the login-form takes the
        screen)."""
        html = self._read_index()
        assert 'id="nav-login-btn"' in html, (
            "Log-in button missing from the DOM — the public-"
            "shell has no way to authenticate visitors."
        )
        # Locate the line — the ``hidden`` attribute must appear
        # on the same element so first-paint hides it before any
        # CSS / JS runs.
        login_line = next(
            (line for line in html.splitlines()
             if 'id="nav-login-btn"' in line),
            None,
        )
        assert login_line is not None
        assert "hidden" in login_line, (
            "nav-login-btn must carry the HTML `hidden` attribute "
            "so the button is invisible at first paint. CSS in "
            "is-public mode overrides via display: inline-flex."
        )
        assert "nav-login-btn" in login_line, (
            "nav-login-btn must carry the .nav-login-btn class — "
            "the body.is-public CSS rule selects on this class to "
            "reveal the button."
        )


# ── Frontend behaviour: app.js carries the public-shell wiring ───────────


class TestPublicShellJsWiring:
    """Source-grep guards on app.js. The public-shell mode lives
    or dies by three JS pieces:

    1. ``PUBLIC_HASH_ROUTES`` set with at least ``#roadmap`` and
       ``#changelog``.
    2. An auth-fail branch that checks for public hash before
       falling through to ``_handle401`` (otherwise logged-out
       visitors at ``/#roadmap`` still see the login-form).
    3. A click handler on ``#nav-login-btn`` that swaps to
       is-login mode.
    """

    def _read_app_js(self):
        return (_REPO_ROOT / "web" / "static" / "app.js").read_text(
            encoding="utf-8",
        )

    def test_public_hash_routes_includes_both(self):
        js = self._read_app_js()
        assert "PUBLIC_HASH_ROUTES" in js, (
            "PUBLIC_HASH_ROUTES set missing — the auth-fail branch "
            "has no way to decide between login-form and public-"
            "shell."
        )
        assert "'#roadmap'" in js and "'#changelog'" in js, (
            "PUBLIC_HASH_ROUTES must include both #roadmap and "
            "#changelog so logged-out visitors at either URL get "
            "the public-shell instead of the login-form."
        )

    def test_auth_fail_branch_uses_public_hash_check(self):
        """The DOMContentLoaded handler's auth-fail path must
        consult ``_isPublicHashRoute()`` (or equivalent) and
        call ``_enterPublicShell()`` for public routes. Without
        this branching, every unauthenticated visitor gets the
        login-form regardless of URL — exactly the regression
        this PR fixes."""
        js = self._read_app_js()
        assert "_isPublicHashRoute" in js, (
            "_isPublicHashRoute helper missing — the auth-fail "
            "branch can't distinguish public from default routes."
        )
        assert "_enterPublicShell" in js, (
            "_enterPublicShell helper missing — public routes "
            "have nowhere to land after auth fails."
        )

    def test_login_button_handler_wired(self):
        """``setupEventListeners`` (or equivalent init) must
        attach a click handler to ``#nav-login-btn`` that
        transitions out of public-shell into the login-form.
        Without the wiring, the Log-in button is a dead
        element."""
        js = self._read_app_js()
        assert "_showLoginFormFromPublic" in js, (
            "Log-in button handler missing — clicking the button "
            "would do nothing."
        )
        assert "nav-login-btn" in js, (
            "nav-login-btn id must appear in app.js so the click "
            "listener can attach to the element."
        )

    def test_public_shell_clears_is_login_class(self):
        """``_enterPublicShell`` must remove ``is-login`` if it
        was previously set (e.g. a stale tab transitioning from
        login-form to public-shell). Otherwise the login-form
        chrome leaks into the public-shell view."""
        js = self._read_app_js()
        # Locate the _enterPublicShell function body.
        start = js.find("function _enterPublicShell(")
        assert start >= 0
        end = js.find("\nfunction ", start + 1)
        body = js[start:end if end > 0 else len(js)]
        assert "is-public" in body and "is-login" in body, (
            "_enterPublicShell must mention both classes — set "
            "is-public and clear is-login. Without the clear, a "
            "transition from login-form to public-shell keeps "
            "the login chrome hidden by the stale class."
        )


# ── style.css carries the body.is-public rules ───────────────────────────


class TestPublicShellCss:

    def _read_style(self):
        return (_REPO_ROOT / "web" / "static" / "style.css").read_text(
            encoding="utf-8",
        )

    def test_is_public_hides_non_public_tabs(self):
        css = self._read_style()
        assert "body.is-public #main-nav .tab:not([data-public])" in css, (
            "body.is-public must hide every nav tab except the "
            "ones marked data-public. Without this rule the full "
            "nav leaks to logged-out visitors."
        )

    def test_is_public_reveals_login_button(self):
        css = self._read_style()
        # Two valid styles for the rule body — using a class
        # selector or descendant. Either is fine; the test
        # asserts the class is referenced.
        assert "body.is-public .nav-login-btn" in css, (
            "body.is-public must reveal the Log-in button via a "
            "rule overriding the HTML `hidden` attribute."
        )

    def test_is_public_hides_profile_button(self):
        """Logged-out visitors have no profile to show; the
        profile-btn must collapse alongside the rest of the
        logged-in chrome in public-shell mode."""
        css = self._read_style()
        assert "body.is-public #profile-btn" in css, (
            "body.is-public must hide #profile-btn — there's no "
            "profile to display for an anonymous visitor."
        )
