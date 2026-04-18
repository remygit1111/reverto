"""Structural tests for the web/routes/ extraction.

Pin that the route modules import cleanly, expose a ``router``
attribute, and that the routes they register are visible on the
FastAPI app. If a future refactor extracts more routes into
web/routes/, extend the lists below.
"""

import os
import sys

os.environ["REVERTO_API_KEY"] = "testkey-for-pytest"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from web.app import app  # noqa: E402


class TestRouteModulesImportable:
    """Each migrated route module must import without error and
    expose a ``router`` (APIRouter) attribute. Regression pin: if a
    refactor accidentally drops the router, this catches it before
    the routes silently vanish from the app."""

    @pytest.mark.parametrize("modname", ["admin", "drawdown"])
    def test_module_importable_and_has_router(self, modname):
        from fastapi import APIRouter
        module = __import__(f"web.routes.{modname}", fromlist=["router"])
        assert hasattr(module, "router"), f"{modname} missing router"
        assert isinstance(module.router, APIRouter)


class TestRoutesRegistered:
    """The paths that were migrated must remain reachable via the
    FastAPI app. Missing paths would surface as 404s in production
    but silently pass tests that check other routes; this catches
    the regression at import-time."""

    MIGRATED_PATHS: set[str] = {
        "/healthz",
        "/readyz",
        "/metrics",
        "/api/emergency-stop",
        "/api/portal/restart",
        "/api/portal/status",
        "/api/bots/{slug}/drawdown/reset",
    }

    def test_all_migrated_paths_registered(self):
        registered = {r.path for r in app.routes if hasattr(r, "path")}
        missing = self.MIGRATED_PATHS - registered
        assert not missing, f"migrated routes missing from app: {missing}"

    def test_non_migrated_paths_still_present(self):
        """Sanity: a few of the routes that STAYED in web/app.py are
        still present — confirms the extraction didn't accidentally
        delete sibling routes."""
        registered = {r.path for r in app.routes if hasattr(r, "path")}
        for path in ["/api/bots", "/api/price", "/auth/login"]:
            assert path in registered, f"{path} vanished after extraction"
