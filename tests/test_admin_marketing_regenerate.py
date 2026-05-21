"""Tests for ``POST /api/admin/marketing/regenerate``.

Covers auth gating (401 / 403), success (200), partial success
(207 Multi-Status), and total failure (500). The endpoint is
admin-only and rate-limited at 10/minute via slowapi.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from core import marketing_export, user_store
from web import app as webapp


_ADMIN_PW = "pytest-marketing-regen-admin-pw-789"


@pytest.fixture
def admin_client():
    """TestClient with a session cookie for the seeded admin
    (user_id=1)."""
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, _ADMIN_PW)
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set(
        "reverto_session", webapp._create_session_cookie(admin),
    )
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def non_admin_client():
    """Authenticated-but-not-admin user — covers 403."""
    from core.database import get_db
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (username, role) VALUES ('bob', 'user')",
        )
    bob = user_store.get_user_by_username("bob")
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set(
        "reverto_session", webapp._create_session_cookie(bob),
    )
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        webapp._COOKIE_SAMESITE = prev_samesite


# ── Auth gates ───────────────────────────────────────────────────────────


class TestAuthGates:

    def test_no_session_returns_401(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 401

    def test_non_admin_returns_403(self, non_admin_client):
        r = non_admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 403


# ── Happy path ──────────────────────────────────────────────────────────


class TestHappyPath:

    def test_200_when_both_succeed(self, admin_client):
        r = admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["results"] == {"roadmap": True, "changelog": True}


# ── Partial / failure paths ─────────────────────────────────────────────


class TestPartialAndFailure:

    def test_207_when_only_roadmap_fails(self, admin_client):
        with patch.object(
            marketing_export, "write_roadmap_snapshot", return_value=False,
        ):
            r = admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 207, r.text
        body = r.json()
        assert body["status"] == "partial"
        assert body["results"] == {"roadmap": False, "changelog": True}

    def test_207_when_only_changelog_fails(self, admin_client):
        with patch.object(
            marketing_export, "write_changelog_snapshot", return_value=False,
        ):
            r = admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 207
        body = r.json()
        assert body["status"] == "partial"
        assert body["results"] == {"roadmap": True, "changelog": False}

    def test_500_when_both_fail(self, admin_client):
        with patch.object(
            marketing_export, "write_roadmap_snapshot", return_value=False,
        ), patch.object(
            marketing_export, "write_changelog_snapshot", return_value=False,
        ):
            r = admin_client.post("/api/admin/marketing/regenerate")
        assert r.status_code == 500
        body = r.json()
        assert body["status"] == "failed"
        assert body["results"] == {"roadmap": False, "changelog": False}


# ── PT-v4-MK-004: concurrency lock around regenerate handler ────────────


class TestPTv4MK004RegenerateLock:
    """Class-of-issue regression for PT-v4-MK-004 (INFO).

    Pre-fix the handler had only a slowapi rate-limit. Two
    concurrent regenerate calls (deliberate parallel invocation
    from multiple admin sessions on the same worker) could
    interleave write_roadmap_snapshot + write_changelog_snapshot
    for last-write-wins semantics.

    Post-fix a module-level ``asyncio.Lock()`` wraps the handler
    body. The serial-execution guarantee depends on (a) the lock
    object's existence and (b) it being entered around the
    snapshot writes. A deterministic two-concurrent-call test
    against the real handler is flaky (rate-limit + auth + DB
    state all interact); the light sanity test below pins the
    lock construct so a future refactor that drops it gets caught.
    """

    def test_regenerate_lock_exists_and_is_asyncio_lock(self):
        import asyncio
        from web.routes import marketing as marketing_routes

        assert hasattr(marketing_routes, "_regenerate_lock"), (
            "PT-v4-MK-004 regression: module-level _regenerate_lock "
            "is missing — concurrent regenerate calls can race."
        )
        assert isinstance(
            marketing_routes._regenerate_lock, asyncio.Lock,
        ), (
            "_regenerate_lock must be an asyncio.Lock (not a "
            "threading.Lock or other primitive — handler is async)."
        )

    def test_regenerate_lock_serialises_two_coroutines(self):
        """Functional check: when two coroutines contend on
        _regenerate_lock, the second one is blocked while the
        first holds it. Tests the lock-construct directly (no
        TestClient — the starlette TestClient is not safe for
        parallel calls, so a handler-level concurrent test would
        only verify TestClient's threading model, not ours)."""
        import asyncio
        from web.routes import marketing as marketing_routes

        in_flight = 0
        max_concurrent = 0

        async def _holder(delay):
            nonlocal in_flight, max_concurrent
            async with marketing_routes._regenerate_lock:
                in_flight += 1
                max_concurrent = max(max_concurrent, in_flight)
                await asyncio.sleep(delay)
                in_flight -= 1

        async def _race():
            await asyncio.gather(_holder(0.02), _holder(0.02))

        asyncio.run(_race())
        assert max_concurrent == 1, (
            f"PT-v4-MK-004 regression: {max_concurrent} concurrent "
            "holders observed inside the lock. asyncio.Lock must "
            "serialise the two coroutines."
        )
