"""Tests for the Workspace backend — dashboard layout storage +
GET / PUT /api/dashboard/layout endpoints.

PR 1 delivers the persistence layer only; frontend + panel-type
logic land in later PRs. The tests below cover three layers:

  * Store-level (``core.dashboard_store``): round-trip, overwrite,
    size cap, non-serialisable payload, corrupt-row handling,
    delete semantics.
  * Route-level: auth requirement, empty-user read, put-then-get,
    oversize body, user scoping, audit-log output, corrupt-row
    fallthrough to null.
  * Migration: the v6 → v7 additive bump creates the
    ``dashboard_layouts`` table + index and advances
    ``PRAGMA user_version``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import dashboard_store  # noqa: E402
from core import database as _database  # noqa: E402
from web import app as webapp  # noqa: E402


_COOKIE_NAME = "reverto_session"


@pytest.fixture(autouse=True)
def _reset_slowapi_limits():
    """Clear process-wide rate-limit buckets between tests.

    GET + PUT /api/dashboard/layout share a 30/minute cap. Without
    a reset, a suite that fires >30 requests would hit 429 for
    assertions meant to exercise handler behaviour.
    """
    try:
        webapp.limiter.reset()
    except Exception:
        pass
    yield


def _seed_user(username: str, role: str = "user") -> int:
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


def _cookie_for(username: str, role: str = "user") -> str:
    from core import user_store

    _seed_user(username, role)
    user = user_store.get_user_by_username(username)
    assert user is not None
    return webapp._create_session_cookie(user)


# ── Store-level ────────────────────────────────────────────────────────────


class TestDashboardStore:
    def test_get_layout_returns_none_when_unset(self):
        uid = _seed_user("pytest_ds_unset")
        assert dashboard_store.get_layout(uid) is None

    def test_put_layout_inserts_new_row(self):
        uid = _seed_user("pytest_ds_insert")
        payload = {"panels": [{"type": "chart", "id": "p1"}]}
        dashboard_store.put_layout(uid, payload)
        assert dashboard_store.get_layout(uid) == payload

    def test_put_layout_overwrites_existing_row(self):
        """Two puts in succession — the second wins. The UNIQUE
        constraint on (user_id, name) + the ON CONFLICT clause is
        what makes this work without the caller doing their own
        delete-then-insert dance.
        """
        uid = _seed_user("pytest_ds_overwrite")
        dashboard_store.put_layout(uid, {"v": 1})
        dashboard_store.put_layout(uid, {"v": 2})
        assert dashboard_store.get_layout(uid) == {"v": 2}

        # Verify exactly one row exists — overwrite, not append.
        conn = _database.get_db()
        rows = conn.execute(
            "SELECT COUNT(*) FROM dashboard_layouts WHERE user_id = ?",
            (uid,),
        ).fetchone()
        assert int(rows[0]) == 1

    def test_put_layout_rejects_oversized(self):
        uid = _seed_user("pytest_ds_oversize")
        # 17 KB of JSON — well past the 16 KB cap. Using a string
        # value keeps the shape simple while crossing the limit.
        big_payload = {"blob": "x" * (dashboard_store.MAX_LAYOUT_SIZE_BYTES + 1)}
        with pytest.raises(ValueError, match="max size"):
            dashboard_store.put_layout(uid, big_payload)
        # Nothing should have been written.
        assert dashboard_store.get_layout(uid) is None

    def test_put_layout_rejects_non_serialisable(self):
        uid = _seed_user("pytest_ds_nonserial")
        payload = {"bad": object()}  # object() has no JSON encoder
        with pytest.raises(ValueError, match="not JSON-serialisable"):
            dashboard_store.put_layout(uid, payload)

    def test_layout_name_rejects_invalid_chars(self):
        """Audit pd-043 — defensive validation on layout names.
        Space, slash, and traversal patterns must all raise so a
        future code path that branches on the name string (cache
        key, export file-path) can't be surprised."""
        uid = _seed_user("pytest_ds_name_bad")
        for bad in ("bad name", "bad/path", "../etc", "", "a" * 65):
            with pytest.raises(ValueError, match="Invalid layout name"):
                dashboard_store.put_layout(uid, {"v": 1}, name=bad)

    def test_layout_name_accepts_safe_shapes(self):
        """Alphanumeric + underscore + dash up to 64 chars passes."""
        uid = _seed_user("pytest_ds_name_ok")
        for good in ("default", "my_layout", "dash-style", "L1", "a" * 64):
            dashboard_store.put_layout(uid, {"shape": good}, name=good)
            assert dashboard_store.get_layout(uid, name=good) == {"shape": good}

    def test_get_layout_handles_corrupt_json(self):
        """If the stored JSON becomes corrupt (disk glitch, rogue
        edit) ``get_layout`` must surface ValueError so the route
        layer can fall through to an empty-state response instead
        of 500-ing on json.JSONDecodeError.
        """
        uid = _seed_user("pytest_ds_corrupt")
        conn = _database.get_db()
        conn.execute(
            "INSERT INTO dashboard_layouts "
            "(user_id, name, layout_json, updated_at) "
            "VALUES (?, 'default', ?, datetime('now'))",
            (uid, "{not valid json"),
        )
        conn.commit()
        with pytest.raises(ValueError, match="Corrupt"):
            dashboard_store.get_layout(uid)

    def test_delete_layout_returns_true_on_hit_false_on_miss(self):
        uid = _seed_user("pytest_ds_delete")
        assert dashboard_store.delete_layout(uid) is False
        dashboard_store.put_layout(uid, {"panels": []})
        assert dashboard_store.delete_layout(uid) is True
        # Second delete is a miss again.
        assert dashboard_store.delete_layout(uid) is False

    def test_put_and_delete_use_default_name(self):
        """Regression guard: ``put_layout`` and ``delete_layout``
        both default to ``DEFAULT_LAYOUT_NAME``, so a put with no
        name arg must be reachable via delete with no name arg.
        """
        uid = _seed_user("pytest_ds_defname")
        dashboard_store.put_layout(uid, {"x": 1})
        assert dashboard_store.delete_layout(uid) is True


# ── Route-level ────────────────────────────────────────────────────────────


class TestDashboardRoutes:
    _GET = "/api/dashboard/layout"
    _PUT = "/api/dashboard/layout"

    def test_get_returns_null_for_fresh_user(self):
        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_rt_fresh"))
        r = client.get(self._GET)
        assert r.status_code == 200, r.text
        assert r.json() == {"layout": None}

    def test_put_then_get_roundtrip(self):
        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_rt_round"))
        payload = {"panels": [
            {"id": "a", "type": "chart", "x": 0, "y": 0, "w": 6, "h": 4},
            {"id": "b", "type": "deals", "x": 6, "y": 0, "w": 6, "h": 4},
        ]}
        put = client.put(self._PUT, json={"layout": payload})
        assert put.status_code == 200, put.text
        assert put.json()["ok"] is True

        got = client.get(self._GET)
        assert got.status_code == 200, got.text
        assert got.json() == {"layout": payload}

    def test_put_returns_400_on_oversize(self):
        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_rt_oversize"))
        big = {"blob": "x" * (dashboard_store.MAX_LAYOUT_SIZE_BYTES + 1)}
        r = client.put(self._PUT, json={"layout": big})
        assert r.status_code == 400, r.text
        assert "max size" in r.json()["detail"].lower()

    def test_put_requires_auth(self):
        """No session cookie → 401 from the auth middleware before
        the handler runs.
        """
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.put(self._PUT, json={"layout": {"panels": []}})
        assert r.status_code == 401

    def test_put_audits_action(self):
        """Successful PUT must emit an ``dashboard_layout_put``
        entry on the ``reverto.audit`` logger. The audit logger
        has ``propagate=False``, so we attach a capture-handler
        for the duration of the request.
        """
        captured: list[str] = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        audit_logger = logging.getLogger("reverto.audit")
        handler = _CaptureHandler(level=logging.INFO)
        audit_logger.addHandler(handler)
        try:
            client = TestClient(webapp.app)
            client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_rt_audit"))
            r = client.put(self._PUT, json={"layout": {"p": 1}})
            assert r.status_code == 200, r.text
        finally:
            audit_logger.removeHandler(handler)

        assert any("dashboard_layout_put" in m for m in captured), (
            f"expected dashboard_layout_put audit entry, got {captured}"
        )

    def test_layouts_are_user_scoped(self):
        """User A writes a layout; user B's GET must still return
        null. Cross-user bleed here would defeat the point of
        per-user layouts entirely.
        """
        client_a = TestClient(webapp.app)
        client_a.cookies.set(_COOKIE_NAME, _cookie_for("pytest_rt_scope_a"))
        client_b = TestClient(webapp.app)
        client_b.cookies.set(_COOKIE_NAME, _cookie_for("pytest_rt_scope_b"))

        put = client_a.put(
            self._PUT, json={"layout": {"owner": "a"}},
        )
        assert put.status_code == 200, put.text

        got_a = client_a.get(self._GET)
        got_b = client_b.get(self._GET)
        assert got_a.json() == {"layout": {"owner": "a"}}
        assert got_b.json() == {"layout": None}

    def test_corrupt_layout_returns_null_not_crash(self, monkeypatch):
        """When ``dashboard_store.get_layout`` raises ValueError
        (corrupt stored JSON) the route must serve ``{"layout":
        null}`` instead of surfacing a 500. The frontend treats
        null as "render empty-state", which is the graceful
        recovery path.
        """
        def _boom(user_id, name="default"):
            raise ValueError("simulated corrupt row")

        monkeypatch.setattr(dashboard_store, "get_layout", _boom)

        client = TestClient(webapp.app)
        client.cookies.set(_COOKIE_NAME, _cookie_for("pytest_rt_corrupt"))
        r = client.get(self._GET)
        assert r.status_code == 200, r.text
        assert r.json() == {"layout": None}


# ── Migration (v6 → v7) ────────────────────────────────────────────────────


class TestSchemaMigrationV7:
    """``PRAGMA user_version`` goes from 6 to 7 and the
    ``dashboard_layouts`` table + its index land on an existing
    DB without touching any other data.
    """

    def test_migration_creates_table_index_and_bumps_version(
        self, tmp_path,
    ):
        # Build a minimal v6-shaped DB in a tmp file so we don't
        # touch the test-session's ledger. _SCHEMA_STATEMENTS now
        # declares the v7 shape directly, so the "pre-migration"
        # DB has to be constructed by hand: users + user_version
        # pinned to 6, no dashboard_layouts.
        db_path = tmp_path / "legacy_v6.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "username TEXT NOT NULL UNIQUE, "
                "password_hash TEXT, "
                "role TEXT NOT NULL DEFAULT 'user', "
                "session_epoch INTEGER NOT NULL DEFAULT 0, "
                "active INTEGER NOT NULL DEFAULT 1, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "failed_login_count INTEGER NOT NULL DEFAULT 0, "
                "last_failed_login_at TEXT"
                ")",
            )
            conn.execute("PRAGMA user_version = 6")
            conn.commit()

        # Point the global DB singleton at the legacy file, run
        # init_db, and then assert the v7 shape. The autouse
        # ``_isolate_reverto_db`` fixture in conftest.py has
        # already pre-migrated its own tmp DB for this test
        # session, so we re-point deliberately and restore on
        # teardown.
        _database.set_db_path(db_path)
        try:
            _database.init_db()

            conn = _database.get_db()
            user_version = conn.execute(
                "PRAGMA user_version",
            ).fetchone()[0]
            assert user_version == 7

            # Table + index must exist.
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='dashboard_layouts'",
            ).fetchone()
            assert tbl is not None

            idx = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_dashboard_layouts_user'",
            ).fetchone()
            assert idx is not None

            # Table is functional: a put-then-get round-trip on the
            # migrated DB must succeed.
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, role) "
                "VALUES (99, 'migration_probe', 'user')",
            )
            conn.commit()
            dashboard_store.put_layout(99, {"panels": []})
            assert dashboard_store.get_layout(99) == {"panels": []}
        finally:
            # Let the autouse fixture restore the per-test tmp DB
            # for subsequent tests. _isolate_reverto_db re-points
            # on every test, so this teardown isn't strictly
            # required — but being explicit keeps the pairing
            # obvious.
            _database.close_db()

    def test_fresh_install_schema_declares_v7_shape(self):
        """Fresh installs must land directly on v7 without taking
        the migration path. The autouse fixture in conftest.py
        gives us exactly this state: a tmp-DB that was just
        created by ``init_db`` at the current SCHEMA_VERSION.
        """
        conn = _database.get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 7
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='dashboard_layouts'",
        ).fetchone()
        assert tbl is not None
