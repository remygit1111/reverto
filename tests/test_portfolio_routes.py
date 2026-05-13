"""Tests for the /api/portfolio/* endpoints.

Covers:
  * auth gates (unauthenticated requests → 401)
  * happy-path shape of /latest, /history, /per-bot
  * 1-per-hour manual-snapshot gate (200 then 429)
  * cross-user isolation
  * empty-state rendering (no snapshots yet)
  * range param validation on /history
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import (  # noqa: E402
    database, paths, portfolio_store, price_feed, user_store,
)
from core.database import get_db  # noqa: E402
from web import app as webapp  # noqa: E402
from web.routes import portfolio as portfolio_routes  # noqa: E402


@pytest.fixture
def fs_sandbox(tmp_path, monkeypatch):
    """Sandbox FS layout (bots dir, credentials) under tmp_path so
    real config dirs are never touched. Resets the per-route rate
    limiter at entry and exit."""
    webapp.limiter.reset()
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)
    # Don't let any test hit a real price feed — every successful
    # snapshot path stubs it explicitly.
    price_feed._clear_cache()
    try:
        yield tmp_path
    finally:
        webapp.limiter.reset()
        price_feed._clear_cache()


def _make_session_client(user) -> TestClient:
    prev_secure = webapp._COOKIE_SECURE
    prev_samesite = webapp._COOKIE_SAMESITE
    webapp._COOKIE_SECURE = False
    webapp._COOKIE_SAMESITE = "lax"
    client = TestClient(webapp.app)
    client.cookies.set(
        "reverto_session", webapp._create_session_cookie(user),
    )
    client._teardown = (prev_secure, prev_samesite)
    return client


def _teardown_client(client: TestClient) -> None:
    prev_secure, prev_samesite = client._teardown
    webapp._COOKIE_SECURE = prev_secure
    webapp._COOKIE_SAMESITE = prev_samesite


@pytest.fixture
def admin_client(fs_sandbox):
    admin = user_store.get_user_by_username("admin")
    assert admin is not None
    user_store.set_password(admin.id, "pytest-portfolio-admin-pw-12345")
    client = _make_session_client(admin)
    try:
        yield client
    finally:
        _teardown_client(client)


@pytest.fixture
def bob_client(fs_sandbox):
    conn = database.get_db()
    with conn:
        conn.execute(
            "INSERT INTO users (username, role) "
            "VALUES ('bob_pf', 'user')",
        )
    bob = user_store.get_user_by_username("bob_pf")
    client = _make_session_client(bob)
    try:
        yield client
    finally:
        _teardown_client(client)


def _seed_account(
    user_id: int, account_id: int, alias: str = "main",
    exchange: str = "bitget", market: str = "coin_m",
) -> int:
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO exchange_accounts "
            "(id, user_id, exchange_type, market_type, alias, "
            " credentials_uuid) "
            "VALUES (?, ?, ?, ?, ?, 'uuid-stub')",
            (account_id, user_id, exchange, market, alias),
        )
    return account_id


def _seed_snapshot(
    user_id: int, account_id: int, *,
    balance_native: float = 0.001, currency: str = "BTC",
    balance_usd: float = 65.0, usd_rate: float = 65000.0,
    rate_source: str = "coingecko", source: str = "auto",
    captured_at: str | None = None,
) -> int:
    if captured_at is None:
        return portfolio_store.create_snapshot(
            user_id=user_id, exchange_account_id=account_id,
            balance_native=balance_native, currency=currency,
            balance_usd=balance_usd, usd_rate=usd_rate,
            rate_source=rate_source, source=source,
        )
    conn = get_db()
    with conn:
        cur = conn.execute(
            "INSERT INTO portfolio_snapshots "
            "(user_id, exchange_account_id, captured_at, balance_native, "
            " currency, balance_usd, usd_rate, rate_source, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, account_id, captured_at, balance_native, currency,
             balance_usd, usd_rate, rate_source, source),
        )
    return int(cur.lastrowid or 0)


# ── Auth ──────────────────────────────────────────────────────────────────


class TestAuthGate:

    def test_latest_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        assert client.get("/api/portfolio/latest").status_code == 401

    def test_history_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        assert client.get("/api/portfolio/history").status_code == 401

    def test_per_bot_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        assert client.get("/api/portfolio/per-bot").status_code == 401

    def test_manual_requires_auth(self, fs_sandbox):
        client = TestClient(webapp.app)
        assert client.post(
            "/api/portfolio/snapshot/manual",
        ).status_code == 401


# ── /latest ───────────────────────────────────────────────────────────────


class TestLatest:

    def test_empty_state_returns_zero_total(self, admin_client):
        r = admin_client.get("/api/portfolio/latest")
        assert r.status_code == 200
        body = r.json()
        assert body["accounts"] == []
        assert body["totals"]["balance_usd"] == 0.0
        assert body["totals"]["as_of"] is None
        # No prior manual snapshot, so the manual button is enabled.
        assert body["manual_allowed"] is True

    def test_one_account_one_snapshot(self, admin_client):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11)
        _seed_snapshot(admin.id, 11, balance_usd=65.0)
        body = admin_client.get("/api/portfolio/latest").json()
        assert len(body["accounts"]) == 1
        a = body["accounts"][0]
        assert a["account_id"] == 11
        assert a["balance_usd"] == 65.0
        assert a["currency"] == "BTC"
        assert a["market_label"] == "Coin-M Perpetual"
        assert body["totals"]["balance_usd"] == 65.0
        assert body["totals"]["by_currency"]["BTC"] == 0.001

    def test_cross_user_isolation(self, admin_client, bob_client):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11)
        _seed_snapshot(admin.id, 11)
        # Bob has no accounts.
        body = bob_client.get("/api/portfolio/latest").json()
        assert body["accounts"] == []


# ── /history ──────────────────────────────────────────────────────────────


class TestHistory:

    def test_default_range_is_7d(self, admin_client):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11)
        _seed_snapshot(admin.id, 11)
        r = admin_client.get("/api/portfolio/history")
        assert r.status_code == 200
        body = r.json()
        assert body["range"] == "7d"
        assert isinstance(body["points"], list)
        assert len(body["points"]) >= 1

    def test_invalid_range_400(self, admin_client):
        r = admin_client.get("/api/portfolio/history?range=1y")
        assert r.status_code == 400

    def test_per_account_split(self, admin_client):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11, alias="a")
        _seed_account(admin.id, 12, alias="b")
        # Same captured_at — both should land in one aggregated point.
        ts = "2026-05-13T09:00:00"
        _seed_snapshot(
            admin.id, 11, balance_usd=10.0, captured_at=ts,
        )
        _seed_snapshot(
            admin.id, 12, balance_usd=15.0, captured_at=ts,
        )
        body = admin_client.get(
            "/api/portfolio/history?range=all",
        ).json()
        # Find the aggregated point.
        point = next(p for p in body["points"] if p["captured_at"] == ts)
        assert point["total_usd"] == pytest.approx(25.0)
        assert point["per_account"]["11"] == 10.0
        assert point["per_account"]["12"] == 15.0


# ── /per-bot ──────────────────────────────────────────────────────────────


class TestPerBot:

    def test_no_yaml_dir_empty_list(self, admin_client, monkeypatch):
        # No bot YAMLs exist yet — handler returns the empty list
        # without hitting the price feed.
        body = admin_client.get("/api/portfolio/per-bot").json()
        assert body == {"bots": []}

    def test_live_bot_aggregates(self, admin_client, fs_sandbox, monkeypatch):
        admin = user_store.get_user_by_username("admin")
        # Drop a live-mode YAML so the handler counts the matching
        # deals.
        bots_dir = paths.user_bots_dir(admin.id)
        bots_dir.mkdir(parents=True, exist_ok=True)
        (bots_dir / "live_one.yaml").write_text(
            "bot:\n  mode: live\n", encoding="utf-8",
        )
        # Two closed deals + one open deal, all under live_one.
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO deals "
                "(id, user_id, bot_slug, bot_name, side, status, "
                " opened_at, initial_price, total_size, pnl_btc) "
                "VALUES "
                "('d1', ?, 'live_one', 'Live One', 'long', 'closed', "
                " '2026-01-01', 60000.0, 0.001, 0.001)",
                (admin.id,),
            )
            conn.execute(
                "INSERT INTO deals "
                "(id, user_id, bot_slug, bot_name, side, status, "
                " opened_at, initial_price, total_size, pnl_btc) "
                "VALUES "
                "('d2', ?, 'live_one', 'Live One', 'long', 'closed', "
                " '2026-01-02', 61000.0, 0.001, -0.0005)",
                (admin.id,),
            )
            conn.execute(
                "INSERT INTO deals "
                "(id, user_id, bot_slug, bot_name, side, status, "
                " opened_at, initial_price, total_size, pnl_btc) "
                "VALUES "
                "('d3', ?, 'live_one', 'Live One', 'long', 'open', "
                " '2026-01-03', 65000.0, 0.002, 0.0001)",
                (admin.id,),
            )
        # Force a deterministic BTC/USD rate so the assertions don't
        # care about external state.
        monkeypatch.setattr(
            portfolio_routes.price_feed, "get_usd_rate",
            lambda c: (65000.0, "test"),
        )
        body = admin_client.get("/api/portfolio/per-bot").json()
        assert len(body["bots"]) == 1
        b = body["bots"][0]
        assert b["bot_slug"] == "live_one"
        assert b["trade_count"] == 3
        assert b["open_positions_count"] == 1
        # Realized USD: (0.001 + -0.0005) * 65000 = 32.5
        assert b["realized_pnl_usd"] == pytest.approx(32.5)
        # Unrealized USD: 0.0001 * 65000 = 6.5
        assert b["unrealized_pnl_usd"] == pytest.approx(6.5)
        assert b["total_pnl_usd"] == pytest.approx(39.0)
        # Open position value: 65000 * 0.002 = 130
        assert b["open_position_value_usd"] == pytest.approx(130.0)

    def test_paper_bot_excluded(self, admin_client, fs_sandbox, monkeypatch):
        admin = user_store.get_user_by_username("admin")
        bots_dir = paths.user_bots_dir(admin.id)
        bots_dir.mkdir(parents=True, exist_ok=True)
        (bots_dir / "paper_one.yaml").write_text(
            "bot:\n  mode: paper\n", encoding="utf-8",
        )
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO deals "
                "(id, user_id, bot_slug, bot_name, side, status, "
                " opened_at, initial_price, total_size, pnl_btc) "
                "VALUES "
                "('p1', ?, 'paper_one', 'Paper One', 'long', 'closed', "
                " '2026-01-01', 60000.0, 0.001, 0.01)",
                (admin.id,),
            )
        monkeypatch.setattr(
            portfolio_routes.price_feed, "get_usd_rate",
            lambda c: (65000.0, "test"),
        )
        body = admin_client.get("/api/portfolio/per-bot").json()
        # Paper bot intentionally excluded — list stays empty.
        assert body["bots"] == []


# ── /snapshot/manual ──────────────────────────────────────────────────────


class TestManualSnapshot:

    def test_blocked_when_recent_manual_exists(
        self, admin_client, monkeypatch,
    ):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11)
        # Pre-existing manual row inside the rolling hour → 429.
        _seed_snapshot(admin.id, 11, source="manual")
        r = admin_client.post("/api/portfolio/snapshot/manual")
        assert r.status_code == 429
        body = r.json()
        assert "next_allowed_at" in body["detail"]

    def test_happy_path_creates_rows(self, admin_client, monkeypatch):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11)
        # Stub the authenticated client + price feed.
        fake_client = MagicMock()
        fake_client.get_balance.return_value = 0.001
        fake_client.balance_currency = "BTC"
        monkeypatch.setattr(
            portfolio_routes, "build_authenticated_exchange",
            lambda *a, **k: fake_client,
        )
        monkeypatch.setattr(
            portfolio_routes.exchange_account_store,
            "get_account_credentials",
            lambda acct_id: {
                "api_key": "k", "api_secret": "s", "passphrase": "p",
            },
        )
        monkeypatch.setattr(
            portfolio_routes.price_feed, "get_usd_rate",
            lambda c: (65000.0, "test"),
        )
        r = admin_client.post("/api/portfolio/snapshot/manual")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] == 1
        assert len(body["accounts"]) == 1
        # The row landed with source=manual so a second click is
        # blocked.
        r2 = admin_client.post("/api/portfolio/snapshot/manual")
        assert r2.status_code == 429
