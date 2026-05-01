"""Regression guards: the public-shell mode is GONE.

Background: between feature/roadmap-spa and the marketing-app
split, the app supported a third auth-mode state
(``body.is-public``) that revealed Roadmap + Changelog tabs to
logged-out visitors at ``/#roadmap`` and ``/#changelog``. PR 3
of the marketing-app split removed that mode entirely — public
content moved to the static marketing site at
``https://reverto.bot``. The app at ``app.reverto.bot`` is now
session-required end-to-end.

This file inverts the original public-shell pin tests into
"public-shell stays gone" guards. The security-relevant ones
(_PUBLIC_PATHS exclusion + 401 on anonymous) are the load-
bearing assertions; the markup / JS / CSS pins are kept
shallow because their original purpose (revealing public tabs)
no longer exists, but the absence-pins protect against an
accidental re-introduction.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from web import app as webapp


_REPO_ROOT = Path(__file__).resolve().parent.parent


# ── Backend: _PUBLIC_PATHS no longer exposes the JSON endpoints ──────────


class TestPublicPathsExcludeRoadmapChangelog:
    """The auth-middleware gates every request except those in
    ``_PUBLIC_PATHS``. PR 3 removed ``/api/roadmap`` and
    ``/api/changelog`` from the set so anonymous callers get
    401 again. The marketing site at reverto.bot does NOT use
    these endpoints — it reads JSON snapshots written by
    ``core.marketing_export`` to ``/var/www/reverto-marketing/
    data/``."""

    def test_roadmap_endpoint_is_not_public(self):
        assert "/api/roadmap" not in webapp._PUBLIC_PATHS, (
            "/api/roadmap must require auth — the marketing site "
            "reads from /data/roadmap.json (file snapshot), not "
            "the API. Re-adding to _PUBLIC_PATHS would re-open the "
            "anonymous-shell hole that PR 3 closed."
        )

    def test_changelog_endpoint_is_not_public(self):
        assert "/api/changelog" not in webapp._PUBLIC_PATHS, (
            "/api/changelog must require auth — same reasoning as "
            "the roadmap endpoint."
        )


# ── Endpoint behaviour: anonymous returns 401 ────────────────────────────


class TestAnonymousAccessRejected:

    def _anon_client(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        return client

    def test_anonymous_get_changelog_returns_401(self):
        r = self._anon_client().get("/api/changelog")
        assert r.status_code == 401, (
            "Pre-PR-3 this returned 200 because the endpoint was "
            "in _PUBLIC_PATHS. Now session-required."
        )

    def test_anonymous_get_roadmap_returns_401(self):
        r = self._anon_client().get("/api/roadmap")
        assert r.status_code == 401, (
            "Pre-PR-3 this returned 200 because the endpoint was "
            "in _PUBLIC_PATHS. Now session-required."
        )


# ── Frontend markup: public-shell DOM removed ────────────────────────────


class TestPublicShellMarkupRemoved:
    """The ``data-public`` / ``nav-login-btn`` / ``view-roadmap`` /
    ``view-changelog`` markers used to drive the public-shell.
    PR 3 stripped them. These pins catch an accidental
    re-introduction (e.g. someone copy-pasting from an old
    branch)."""

    def _read_index(self):
        return (_REPO_ROOT / "web" / "static" / "index.html").read_text(
            encoding="utf-8",
        )

    def test_no_data_public_attributes(self):
        html = self._read_index()
        assert "data-public" not in html, (
            "data-public was the public-shell tab marker — none "
            "of the in-app tabs should still carry it."
        )

    def test_no_nav_login_button(self):
        html = self._read_index()
        assert 'id="nav-login-btn"' not in html, (
            "The Log-in button only existed for the public-shell "
            "(logged-out visitors transitioning to the login "
            "form). With the public-shell gone, every logged-out "
            "visitor lands directly on #view-login — no button "
            "needed."
        )

    def test_no_public_roadmap_view(self):
        html = self._read_index()
        assert 'id="view-roadmap"' not in html, (
            "view-roadmap was the public timeline render target. "
            "Public roadmap now lives at https://reverto.bot/"
            "roadmap.html."
        )

    def test_no_public_changelog_view(self):
        html = self._read_index()
        assert 'id="view-changelog"' not in html, (
            "view-changelog was the public list render target. "
            "Public changelog now lives at https://reverto.bot/"
            "changelog.html."
        )


# ── Frontend behaviour: app.js no longer carries public-shell helpers ────


class TestPublicShellJsRemoved:

    def _read_app_js(self):
        return (_REPO_ROOT / "web" / "static" / "app.js").read_text(
            encoding="utf-8",
        )

    def test_no_public_hash_routes_set(self):
        js = self._read_app_js()
        assert "PUBLIC_HASH_ROUTES" not in js, (
            "PUBLIC_HASH_ROUTES drove the public-shell branching. "
            "Removed in PR 3 — the auth-fail path now goes "
            "straight to _handle401 / login form."
        )

    def test_no_public_shell_helpers(self):
        js = self._read_app_js()
        for name in ("_isPublicHashRoute", "_enterPublicShell",
                     "_showLoginFormFromPublic"):
            assert name not in js, (
                f"{name} was a public-shell helper, removed in PR 3."
            )


# ── style.css: body.is-public rules removed ──────────────────────────────


class TestPublicShellCssRemoved:

    def _read_style(self):
        return (_REPO_ROOT / "web" / "static" / "style.css").read_text(
            encoding="utf-8",
        )

    def test_no_is_public_rules(self):
        css = self._read_style()
        # Match the actual selectors. The descriptive comment
        # that mentions "body.is-public ... removed in PR 3" is
        # allowed; it's a plain comment, not a selector. To
        # check for actual rules we look for the selector
        # combinator pattern.
        bad_selectors = [
            "body.is-public #",
            "body.is-public .",
            "body.is-public[",
        ]
        for sel in bad_selectors:
            assert sel not in css, (
                f"CSS selector starting with {sel!r} found — "
                "body.is-public rules were removed in PR 3."
            )
