"""End-to-end cross-tenant isolation (audit r1-053).

Two authenticated users, each seeded with their own deals and
annotations. Every route that touches user-owned data must scope
strictly to the authenticated user. Unit-tests cover helpers;
these catch route-level drift by driving the full request-flow.

Seeds use direct DB inserts to side-step the bot-spawn path — the
goal is to pin authorisation, not to exercise the engine.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
os.environ.setdefault("REVERTO_SECRET_KEY", "testkey-for-pytest-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import database, deal_store, user_store  # noqa: E402
from paper.paper_state import PaperDeal, PaperOrder  # noqa: E402
from web import app as webapp  # noqa: E402


_FIXED_MINUTE = datetime(2026, 4, 24, 13, 42, 0, tzinfo=timezone.utc)


def _seed_user(username: str, role: str = "user") -> int:
    conn = database.get_db()
    conn.execute(
        "INSERT INTO users (username, role, active) VALUES (?, ?, 1)",
        (username, role),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,),
    ).fetchone()
    return int(row["id"])


def _client_for(uid: int) -> TestClient:
    user = user_store.get_user_by_id(uid)
    assert user is not None
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    c = TestClient(webapp.app)
    c.cookies.set(
        "reverto_session", webapp._create_session_cookie(user),
    )
    # Restore on client close — TestClient's context manager runs
    # teardown on ``.close()`` which pytest invokes at test-end via
    # the fixture's scope. Snapshot the originals on the instance so
    # teardown can reach them without another module-level read.
    c._reverto_prev_secure = prev_secure
    c._reverto_prev_samesite = prev_samesite
    return c


@pytest.fixture
def two_users():
    """Seed two non-admin users (role='user') and yield TestClients
    for each. The autouse ``_isolate_reverto_db`` fixture from
    conftest already provides a fresh DB per test."""
    uid_a = _seed_user("alice")
    uid_b = _seed_user("bob")
    client_a = _client_for(uid_a)
    client_b = _client_for(uid_b)
    try:
        yield {"a": (uid_a, client_a), "b": (uid_b, client_b)}
    finally:
        webapp._COOKIE_SECURE = client_a._reverto_prev_secure
        webapp._COOKIE_SAMESITE = client_a._reverto_prev_samesite


def _seed_deal(user_id: int, bot_slug: str, deal_id: str) -> None:
    deal = PaperDeal(
        id=deal_id, bot_name=bot_slug, symbol="BTC/USD",
        side="long", leverage=1,
        orders=[PaperOrder(
            order_number=1, price=80000.0, size=0.001,
            timestamp=_FIXED_MINUTE, order_type="base",
        )],
    )
    deal_store.save_deal(deal, bot_slug, bot_slug.upper(), user_id=user_id)


def _seed_annotation(user_id: int, bot_slug: str) -> int:
    """Insert a chart annotation directly; return its rowid."""
    conn = database.get_db()
    cur = conn.execute(
        "INSERT INTO chart_annotations "
        "(user_id, bot_slug, timeframe, type, x1, y1, color) "
        "VALUES (?, ?, '1h', 'hline', 1700000000, 80000.0, '#ffb347')",
        (user_id, bot_slug),
    )
    conn.commit()
    return int(cur.lastrowid)


# ── /api/db/deals ─────────────────────────────────────────────────────────


class TestDealListingIsScoped:
    def test_user_b_does_not_see_user_a_deals(self, two_users):
        uid_a, client_a = two_users["a"]
        uid_b, client_b = two_users["b"]
        _seed_deal(uid_a, "bot_a", "202604241342-1111")

        # User A sees the deal.
        r_a = client_a.get("/api/db/deals?limit=100")
        assert r_a.status_code == 200
        ids_a = {row["deal"]["id"] for row in r_a.json()}
        assert "202604241342-1111" in ids_a

        # User B does not.
        r_b = client_b.get("/api/db/deals?limit=100")
        assert r_b.status_code == 200
        ids_b = {row["deal"]["id"] for row in r_b.json()}
        assert "202604241342-1111" not in ids_b


# ── /api/db/annotations/{ann_id} DELETE ───────────────────────────────────


class TestAnnotationDeletionIsScoped:
    def test_user_b_cannot_delete_user_a_annotation(self, two_users):
        uid_a, client_a = two_users["a"]
        uid_b, client_b = two_users["b"]
        ann_id = _seed_annotation(uid_a, "bot_a")

        # User B tries to delete — expect 404 (not 200/204).
        r_b = client_b.delete(f"/api/db/annotations/{ann_id}")
        assert r_b.status_code == 404, r_b.text

        # User A can still see + delete their own annotation.
        r_a_list = client_a.get(
            "/api/db/annotations?bot_slug=bot_a&timeframe=1h",
        )
        assert r_a_list.status_code == 200
        returned_ids = [a.get("id") for a in r_a_list.json()]
        assert ann_id in returned_ids

        r_a_del = client_a.delete(f"/api/db/annotations/{ann_id}")
        assert r_a_del.status_code == 200


# ── /api/db/annotations list is scoped ───────────────────────────────────


class TestAnnotationListIsScoped:
    def test_user_b_does_not_see_user_a_annotations(self, two_users):
        uid_a, client_a = two_users["a"]
        uid_b, client_b = two_users["b"]
        _seed_annotation(uid_a, "bot_a")

        r_a = client_a.get(
            "/api/db/annotations?bot_slug=bot_a&timeframe=1h",
        )
        assert r_a.status_code == 200
        assert len(r_a.json()) == 1

        # User B queries the same slug+timeframe — must come back empty.
        r_b = client_b.get(
            "/api/db/annotations?bot_slug=bot_a&timeframe=1h",
        )
        assert r_b.status_code == 200
        assert r_b.json() == []


# Not-in-scope for this sweep: end-to-end /api/bots listing isolation.
# That test would need to invalidate the registry's TTL cache plus
# sandbox the real ``config/bots/<user_id>/`` directory tree — the
# registry's path resolution happens at module import via BASE_DIR so
# the DB-isolated test env still hits the real filesystem. The unit-
# level guards in tests/test_admin_bots_routes.py already verify that
# registry.get(user_id, slug) and /api/bots/{slug}/... respect the
# user_id composite key. Revisit with a dedicated FS-sandbox fixture
# in a follow-up PR.
