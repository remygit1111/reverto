"""Regression tests for web/routes/chart.py.

Focus: the /api/price fallback path must only surface state from the
REQUESTING user's bots. Before the audit-v24 cleanup commit the
fallback iterated every bot in the registry regardless of owner —
Phase-1 harmless (one user) but a cross-user leak once Phase-3
sessions land.

Strategy: monkeypatch ``webapp.registry.all`` to return a test-
controlled list of fake BotInfo objects per user_id, and
monkeypatch the Bitget ticker. This isolates the test from the
real filesystem + live parity bots (rsi_paper_test /
rsi_real_test) which are NOT touched.
"""

from __future__ import annotations

import os
import sys

os.environ["REVERTO_API_KEY"] = "testkey-for-pytest"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from core.user import User
from web import app as webapp


# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeBot:
    """Minimal stand-in for BotInfo. Only ``read_state`` is consulted
    by the /api/price fallback path."""

    def __init__(self, slug: str, user_id: int, current_price: float | None):
        self.slug = slug
        self.user_id = user_id
        self._price = current_price

    def read_state(self) -> dict:
        if self._price is None:
            return {}
        return {"current_price": self._price}


def _install_registry(monkeypatch, bots_by_user: dict[int, list[_FakeBot]]) -> None:
    """Replace webapp.registry.all with an async lambda that returns
    the fake bots for a given user_id."""
    async def _fake_all(user_id=None):
        if user_id is None:
            # Phase-2 registry returns flat list when user_id is None,
            # but /api/price always passes user_id so this branch is
            # dead; assert to catch regressions that drop the filter.
            raise AssertionError(
                "registry.all() called without user_id — "
                "cross-user leak regression",
            )
        return list(bots_by_user.get(int(user_id), []))
    monkeypatch.setattr(webapp.registry, "all", _fake_all)


@pytest.fixture
def client():
    return TestClient(webapp.app)


def _admin_cookie() -> str:
    """Test helper: session cookie for the seeded admin user.
    Audit v26-05 tightened ``_create_session_cookie`` to accept only
    ``User`` instances; this helper centralises the admin lookup."""
    from core import user_store
    admin = user_store.get_user_by_username("admin")
    assert admin is not None, "admin seed missing — check init_db"
    return webapp._create_session_cookie(admin)


@pytest.fixture
def session(client):
    """Logged-in session cookie — /api/price is behind the auth
    middleware so without this the TestClient gets 401 before we
    even reach the ticker fallback."""
    client.cookies.set("reverto_session", _admin_cookie())
    return client


@pytest.fixture
def ticker_fails(monkeypatch):
    """Force the Bitget ticker path to raise so /api/price lands in
    the fallback branch — which is the branch under test."""
    def _boom(*a, **kw):
        raise RuntimeError("bitget ticker unavailable in test")
    monkeypatch.setattr(
        webapp._bitget_client, "fetch_ticker", _boom,
    )


# ── The isolation invariant ────────────────────────────────────────────────


class TestPriceFallbackUserScoping:

    def test_user_1_cannot_see_user_2_state(
        self, session, ticker_fails, monkeypatch,
    ):
        """User 2 has a bot with a distinctive price; user 1 has no
        bots. Bitget ticker fails. User 1's fallback must NOT reach
        user 2's state — 99999.99 must not appear anywhere in the
        response body."""
        _install_registry(monkeypatch, {
            1: [],  # user 1 has no bots
            2: [_FakeBot("other_bot", user_id=2, current_price=99999.99)],
        })

        r = session.get("/api/price")
        # 503 because user 1 has no bots + ticker is down.
        assert r.status_code == 503, (
            f"expected 503, got {r.status_code}: {r.text}"
        )
        assert "99999.99" not in r.text
        assert "99999" not in r.text

    def test_user_1_sees_own_state(
        self, session, ticker_fails, monkeypatch,
    ):
        """Positive path: user 1 has a bot with current_price=50000.
        Bitget fails. The fallback finds user 1's bot and returns
        its price with source=bot."""
        _install_registry(monkeypatch, {
            1: [_FakeBot("my_bot", user_id=1, current_price=50_000.0)],
        })

        r = session.get("/api/price")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["price"] == 50_000.0
        assert body["source"] == "bot"
        assert body["pair"] == "BTC/USD"

    def test_user_1_fallback_picks_own_even_when_user_2_exists(
        self, session, ticker_fails, monkeypatch,
    ):
        """Both users have bot-state with a price. User 1 must see
        HIS OWN price back, not user 2's. Guards against a future
        refactor that accidentally widens registry.all's scope."""
        _install_registry(monkeypatch, {
            1: [_FakeBot("my_bot", user_id=1, current_price=50_000.0)],
            2: [_FakeBot("their_bot", user_id=2, current_price=99_999.99)],
        })

        r = session.get("/api/price")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["price"] == 50_000.0
        assert body["price"] != 99_999.99


class TestPriceFallbackWhenUser2Requests:
    """Mirror of the above from the other side: a user 2 session
    must see THEIR OWN fallback, not user 1's. Phase-1 only has
    user 1 in the DB so we simulate user 2 via dependency override."""

    def test_user_2_fallback_scoped_to_user_2(
        self, client, ticker_fails, monkeypatch,
    ):
        _install_registry(monkeypatch, {
            1: [_FakeBot("u1_bot", user_id=1, current_price=50_000.0)],
            2: [_FakeBot("u2_bot", user_id=2, current_price=99_999.99)],
        })

        client.cookies.set("reverto_session", _admin_cookie())
        # Pretend the resolved session is user 2 without needing the
        # DB to carry that row — the /api/price handler only reads
        # user.id off the injected User.
        webapp.app.dependency_overrides[webapp._request_user] = (
            lambda: User(id=2, username="bob")
        )
        try:
            r = client.get("/api/price")
        finally:
            webapp.app.dependency_overrides.clear()

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["price"] == 99_999.99
        assert body["price"] != 50_000.0


# ── 503 fallback contract ──────────────────────────────────────────────────


class TestPriceFallback503:

    def test_503_when_no_ticker_and_no_bots(
        self, session, ticker_fails, monkeypatch,
    ):
        """Caller has no bots AND ticker is down — 503 not 200 with
        a bogus price=0.0 (the pre-cleanup behaviour)."""
        _install_registry(monkeypatch, {1: []})
        r = session.get("/api/price")
        assert r.status_code == 503
        assert "price unavailable" in r.text.lower()

    def test_503_when_bots_have_no_current_price(
        self, session, ticker_fails, monkeypatch,
    ):
        """Bots exist but their state.json has no current_price
        (fresh-booted bot, first tick not yet landed). Still 503 —
        we refuse to fabricate a number out of thin air."""
        _install_registry(monkeypatch, {
            1: [_FakeBot("fresh", user_id=1, current_price=None)],
        })
        r = session.get("/api/price")
        assert r.status_code == 503


# ── Ticker happy path still works ──────────────────────────────────────────


class TestPriceTickerPath:

    def test_ticker_succeeds_returns_bitget_source(
        self, session, monkeypatch,
    ):
        """When Bitget responds we never touch the registry at all —
        the scoping change must not regress the happy path."""
        monkeypatch.setattr(
            webapp._bitget_client, "fetch_ticker",
            lambda *a, **kw: {"last": 71_234.5, "close": 71_234.5},
        )
        # Also install a tripwire registry so a regression that makes
        # the happy path walk registry.all() would explode loudly.
        async def _should_not_be_called(user_id=None):
            raise AssertionError(
                "registry.all must not be touched when ticker works",
            )
        monkeypatch.setattr(
            webapp.registry, "all", _should_not_be_called,
        )

        r = session.get("/api/price")
        assert r.status_code == 200
        body = r.json()
        assert body["price"] == 71_234.5
        assert body["source"] == "bitget"


# ── /api/ticker/{pair} — info-sidebar payload ─────────────────────────────


class TestTickerEndpoint:
    """Regression for the PR 5a workspace chart-panel info-sidebar.

    Covers shape conformance, 10 s cache behaviour, upstream failure
    → 502, and the auth gate. Monkeypatches ``_bitget_client.fetch_ticker``
    so the real Bitget endpoint is never hit during tests.
    """

    @pytest.fixture(autouse=True)
    def _clear_ticker_cache(self):
        """The ticker cache is process-global; tests running after
        each other would otherwise see cached values from neighbours.
        Resets before AND after so both entry and exit are clean."""
        webapp._ticker_cache.clear()
        yield
        webapp._ticker_cache.clear()

    def test_ticker_returns_shape(self, session, monkeypatch):
        """fetch_ticker returns the standard ccxt dict; the endpoint
        maps it onto the sidebar-facing shape."""
        monkeypatch.setattr(
            webapp._bitget_client, "fetch_ticker",
            lambda *a, **kw: {
                "last": 78_077.9,
                "close": 78_077.9,
                "change": -143.9,
                "percentage": -0.18,
                "baseVolume": 12_543.67,
                "high": 79_500.0,
                "low": 75_750.0,
                "bid": 78_067.7,
                "ask": 78_078.2,
                "timestamp": 1_714_053_600_000,
            },
        )
        r = session.get("/api/ticker/BTCUSD")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pair"] == "BTC/USD"
        assert body["price"] == 78_077.9
        assert body["change_24h"] == -143.9
        assert body["change_pct_24h"] == -0.18
        assert body["volume_24h"] == 12_543.67
        assert body["high_24h"] == 79_500.0
        assert body["low_24h"] == 75_750.0
        assert body["bid"] == 78_067.7
        assert body["ask"] == 78_078.2
        assert body["timestamp"] == 1_714_053_600_000

    def test_ticker_caches_within_ttl(self, session, monkeypatch):
        """Two calls within the 10 s TTL should only hit Bitget once."""
        call_count = {"n": 0}

        def _fake_fetch(*a, **kw):
            call_count["n"] += 1
            return {"last": 1.0, "close": 1.0, "change": 0.0,
                    "percentage": 0.0, "baseVolume": 0.0,
                    "high": 0.0, "low": 0.0, "bid": 0.0, "ask": 0.0,
                    "timestamp": 1}

        monkeypatch.setattr(
            webapp._bitget_client, "fetch_ticker", _fake_fetch,
        )
        r1 = session.get("/api/ticker/BTCUSD")
        r2 = session.get("/api/ticker/BTCUSD")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert call_count["n"] == 1, (
            "second call within TTL should hit cache, not Bitget"
        )
        assert r1.json() == r2.json()

    def test_ticker_upstream_failure_returns_502(
        self, session, monkeypatch,
    ):
        """A ccxt-level exception maps to 502 — the SPA surfaces a
        sidebar em-dash without a user-visible 5xx alarm."""
        def _boom(*a, **kw):
            raise RuntimeError("exchange down")
        monkeypatch.setattr(
            webapp._bitget_client, "fetch_ticker", _boom,
        )
        r = session.get("/api/ticker/BTCUSD")
        assert r.status_code == 502, r.text

    def test_ticker_requires_auth(self, client, monkeypatch):
        """Auth middleware gates /api/ticker just like every other
        /api/* route — no session cookie, no ticker."""
        r = client.get("/api/ticker/BTCUSD")
        assert r.status_code == 401

    def test_ticker_coerces_missing_fields_to_none(
        self, session, monkeypatch,
    ):
        """ccxt may emit a sparse ticker (e.g. an exchange without
        bid/ask in its response shape). Missing keys become None in
        the JSON payload so the SPA renders em-dashes without crashing."""
        monkeypatch.setattr(
            webapp._bitget_client, "fetch_ticker",
            lambda *a, **kw: {"last": 100.0, "close": 100.0},
        )
        r = session.get("/api/ticker/BTCUSD")
        assert r.status_code == 200
        body = r.json()
        assert body["price"] == 100.0
        # Every absent key collapses to None, not KeyError or 0.
        for key in (
            "change_24h", "change_pct_24h", "volume_24h",
            "high_24h", "low_24h", "bid", "ask", "timestamp",
        ):
            assert body[key] is None, f"{key} should be None, got {body[key]!r}"

    def test_ticker_rejects_unlisted_pair(self, session, monkeypatch):
        """Audit r1.1-002: unlisted symbols must 400 BEFORE the LRU
        cache or the upstream fetch_ticker call. Guards against
        cache-pollution / eviction-spam by hostile authenticated
        clients.

        Monkeypatch fetch_ticker to crash loudly if it's ever called
        on this path — the reject happens pre-cache-pre-fetch."""
        def _should_not_run(*a, **kw):
            raise AssertionError(
                "fetch_ticker invoked despite pair rejection",
            )
        monkeypatch.setattr(
            webapp._bitget_client, "fetch_ticker", _should_not_run,
        )
        r = session.get("/api/ticker/ZZZXYZ")
        assert r.status_code == 400, r.text
        assert "must be one of" in r.json()["detail"]

    def test_ticker_rejects_injection_shape(self, session, monkeypatch):
        """Path-component that looks like an attempted script injection
        must land on the 400 branch, not anywhere near the cache or
        the exchange client."""
        def _should_not_run(*a, **kw):
            raise AssertionError("fetch_ticker should not be called")
        monkeypatch.setattr(
            webapp._bitget_client, "fetch_ticker", _should_not_run,
        )
        r = session.get("/api/ticker/BTC$USD")
        assert r.status_code == 400

    def test_ticker_accepts_btc_usdt_pair(self, session, monkeypatch):
        """BTC/USDT is in the allowlist for forward-compat; the actual
        upstream call is mocked since Reverto trades BTC/USD inverse
        perp today."""
        monkeypatch.setattr(
            webapp._bitget_client, "fetch_ticker",
            lambda *a, **kw: {
                "last": 1.0, "close": 1.0, "change": 0.0,
                "percentage": 0.0, "baseVolume": 0.0,
                "high": 0.0, "low": 0.0, "bid": 0.0, "ask": 0.0,
                "timestamp": 1,
            },
        )
        r = session.get("/api/ticker/BTCUSDT")
        assert r.status_code == 200
        assert r.json()["pair"] == "BTC/USDT"
