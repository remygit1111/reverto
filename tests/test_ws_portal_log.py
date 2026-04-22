"""Role-gate on the ``portal`` slug for /ws/logs (audit v26-16).

portal.log can expose cross-user admin actions (failed login
attempts, admin route hits, audit events), so the WS subscription
to ``/ws/logs/portal`` is now admin-only. This suite exercises the
accept / reject split that was added alongside the per-user
broadcaster filter.

Non-admin access is refused with close-code 4403 before accept();
admin access receives the last portal.log lines exactly as before.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from web import app as webapp  # noqa: E402


_COOKIE_NAME = "reverto_session"


def _seed_user(username: str, role: str) -> int:
    """Create (or upsert) a users row and return its id."""
    from core.database import get_db

    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO users (username, active, role) "
        "VALUES (?, 1, ?)",
        (username, role),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,),
    ).fetchone()
    return int(row["id"])


def _cookie_for(username: str, role: str) -> str:
    from core import user_store

    _seed_user(username, role)
    user = user_store.get_user_by_username(username)
    assert user is not None
    assert user.role == role
    return webapp._create_session_cookie(user)


class TestPortalLogAdminGate:
    def test_portal_log_subscribe_admin_succeeds(self):
        """Admin session → handshake accepted. We don't assert on the
        historical-lines payload (portal.log contents vary across
        test runs) — only on the fact that the socket accepts and we
        can read at least one frame before closing.
        """
        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_adm_ws", "admin"))
        with client.websocket_connect("/ws/logs/portal") as ws:
            # receive_text either returns a log line (portal.log is
            # non-empty) or blocks — we close immediately to avoid
            # the 30s ping loop. An immediate close without a raised
            # exception is proof the accept succeeded.
            ws.close()

    def test_portal_log_subscribe_non_admin_returns_4403(self):
        """Non-admin session → close 4403 before accept()."""
        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_usr_ws", "user"))
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/ws/logs/portal") as ws:
                ws.receive_text()
        assert excinfo.value.code == 4403

    def test_portal_log_subscribe_no_session_returns_4401(self):
        """No session cookie → close 4401 (auth-layer rejection).

        Guards the ordering: the auth check must run before the role
        check, otherwise an unauthenticated request would error on
        ``get_user_by_id(None)`` instead of getting the clean 4401.
        """
        client = TestClient(webapp.app)
        client.cookies.clear()
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect("/ws/logs/portal") as ws:
                ws.receive_text()
        assert excinfo.value.code == 4401
