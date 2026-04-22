"""Unit tests for LogBroadcaster + StateBroadcaster (audit v26-16).

Audit v26-16 added a per-user filter to both WS broadcasters so
multi-tenant rollouts don't leak one user's bot state into another
user's browser. These tests exercise the filter in isolation from
FastAPI / TestClient — a ``_FakeWebSocket`` stands in for real
``starlette.websockets.WebSocket`` objects so we can observe
``.send_text`` calls directly.

The route-level integration (ws_logs portal-slug admin gate, end-to-
end ws_state subscribe with cookie) lives in tests/test_web_routes.py
and tests/test_ws_portal_log.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from web.app import LogBroadcaster, StateBroadcaster  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _FakeWebSocket:
    """Minimal stand-in for starlette.WebSocket.

    Only surfaces the methods the broadcasters touch: ``accept`` (no-
    op) and ``send_text`` (records payloads). ``send_text`` can be
    primed to raise so we can exercise the stale-cleanup branch.
    """

    def __init__(self, fail_on_send: bool = False):
        self.sent: list[str] = []
        self.accepted: bool = False
        self._fail_on_send = fail_on_send

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, payload: str) -> None:
        if self._fail_on_send:
            raise RuntimeError("simulated transport failure")
        self.sent.append(payload)


# ── LogBroadcaster ──────────────────────────────────────────────────────────


class TestLogBroadcasterUserFilter:
    def test_broadcast_delivers_to_matching_user(self):
        bc = LogBroadcaster()
        ws_u1 = _FakeWebSocket()
        ws_u2 = _FakeWebSocket()

        async def _drive():
            await bc.connect(ws_u1, "alpha", user_id=1)
            await bc.connect(ws_u2, "alpha", user_id=2)
            await bc.broadcast("alpha", "owner-1 line", owner_user_id=1)

        _run(_drive())

        assert ws_u1.sent == ["owner-1 line"]
        assert ws_u2.sent == [], (
            "user 2 must not receive frames owned by user 1 even "
            "though both subscribed to the same slug"
        )

    def test_broadcast_skips_when_no_client_matches_owner(self):
        bc = LogBroadcaster()
        ws_u1 = _FakeWebSocket()

        async def _drive():
            await bc.connect(ws_u1, "alpha", user_id=1)
            await bc.broadcast("alpha", "nobody listens", owner_user_id=999)

        _run(_drive())

        assert ws_u1.sent == []

    def test_broadcast_ignores_other_slugs(self):
        bc = LogBroadcaster()
        ws_alpha = _FakeWebSocket()
        ws_beta = _FakeWebSocket()

        async def _drive():
            await bc.connect(ws_alpha, "alpha", user_id=1)
            await bc.connect(ws_beta, "beta", user_id=1)
            await bc.broadcast("alpha", "only alpha", owner_user_id=1)

        _run(_drive())

        assert ws_alpha.sent == ["only alpha"]
        assert ws_beta.sent == []

    def test_disconnect_removes_user_map_entry(self):
        bc = LogBroadcaster()
        ws = _FakeWebSocket()

        async def _drive():
            await bc.connect(ws, "alpha", user_id=1)
            assert ws in bc._user_map
            await bc.disconnect(ws, "alpha")

        _run(_drive())

        assert ws not in bc._user_map
        assert ws not in bc._clients.get("alpha", set())

    def test_send_failure_evicts_client_and_user_map(self):
        bc = LogBroadcaster()
        ws_ok = _FakeWebSocket()
        ws_dead = _FakeWebSocket(fail_on_send=True)

        async def _drive():
            await bc.connect(ws_ok, "alpha", user_id=1)
            await bc.connect(ws_dead, "alpha", user_id=1)
            await bc.broadcast("alpha", "line", owner_user_id=1)

        _run(_drive())

        assert ws_ok.sent == ["line"]
        assert ws_dead not in bc._clients["alpha"], (
            "failing socket should be purged from the slug set"
        )
        assert ws_dead not in bc._user_map, (
            "failing socket's user_map entry must also go away"
        )


# ── StateBroadcaster ────────────────────────────────────────────────────────


class TestStateBroadcasterUserFilter:
    def test_broadcast_delivers_to_matching_user(self):
        sb = StateBroadcaster()
        ws_u1 = _FakeWebSocket()
        ws_u2 = _FakeWebSocket()

        async def _drive():
            await sb.connect(ws_u1, user_id=1)
            await sb.connect(ws_u2, user_id=2)
            await sb.broadcast('{"type":"bot_state","slug":"a"}', target_user_id=1)

        _run(_drive())

        assert ws_u1.sent == ['{"type":"bot_state","slug":"a"}']
        assert ws_u2.sent == []

    def test_summary_per_user_scenario(self):
        """Simulates the watch_state_files summary loop: two users
        each get one summary frame, and cross-user clients never see
        each other's aggregates.
        """
        sb = StateBroadcaster()
        alice = _FakeWebSocket()
        bob = _FakeWebSocket()

        async def _drive():
            await sb.connect(alice, user_id=1)
            await sb.connect(bob, user_id=2)
            # Simulate the per-user summary fan-out that
            # watch_state_files now performs.
            await sb.broadcast('{"type":"summary","data":{"owner":"alice"}}',
                               target_user_id=1)
            await sb.broadcast('{"type":"summary","data":{"owner":"bob"}}',
                               target_user_id=2)

        _run(_drive())

        assert alice.sent == ['{"type":"summary","data":{"owner":"alice"}}']
        assert bob.sent == ['{"type":"summary","data":{"owner":"bob"}}']

    def test_disconnect_removes_user_map_entry(self):
        sb = StateBroadcaster()
        ws = _FakeWebSocket()

        async def _drive():
            await sb.connect(ws, user_id=1)
            assert ws in sb._user_map
            await sb.disconnect(ws)

        _run(_drive())

        assert ws not in sb._user_map
        assert ws not in sb._clients

    def test_send_failure_evicts_client_and_user_map(self):
        sb = StateBroadcaster()
        ws_ok = _FakeWebSocket()
        ws_dead = _FakeWebSocket(fail_on_send=True)

        async def _drive():
            await sb.connect(ws_ok, user_id=1)
            await sb.connect(ws_dead, user_id=1)
            await sb.broadcast('{"x":1}', target_user_id=1)

        _run(_drive())

        assert ws_ok.sent == ['{"x":1}']
        assert ws_dead not in sb._clients
        assert ws_dead not in sb._user_map


# ── get_admin_user_ids ──────────────────────────────────────────────────────


class TestGetAdminUserIds:
    """The new helper the tail_logs portal-fan path relies on."""

    def test_returns_admin_ids_only(self):
        from core import user_store
        from core.database import get_db

        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO users (username, active, role) "
            "VALUES (?, 1, ?)",
            ("pytest_admin_broad", "admin"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO users (username, active, role) "
            "VALUES (?, 1, ?)",
            ("pytest_regular_broad", "user"),
        )
        conn.commit()

        admin_ids = user_store.get_admin_user_ids()
        admin_row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ("pytest_admin_broad",),
        ).fetchone()
        regular_row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ("pytest_regular_broad",),
        ).fetchone()
        assert int(admin_row["id"]) in admin_ids
        assert int(regular_row["id"]) not in admin_ids

    def test_skips_deactivated_admin(self):
        from core import user_store
        from core.database import get_db

        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO users (username, active, role) "
            "VALUES (?, 0, ?)",
            ("pytest_disabled_admin_broad", "admin"),
        )
        conn.commit()
        disabled_id = int(conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ("pytest_disabled_admin_broad",),
        ).fetchone()["id"])

        assert disabled_id not in user_store.get_admin_user_ids()
