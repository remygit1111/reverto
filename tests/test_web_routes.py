# tests/test_web_routes.py
# Smoke tests voor de web portal routes. Vangt regressies waarbij POST
# en GET op hetzelfde pad (/api/bots) per ongeluk conflicteren of een
# route niet geregistreerd is.
#
# Bijzonder belangrijk voor /api/bots: GET (lijst) en POST (create)
# leven op hetzelfde pad en moeten beide beschikbaar zijn.

import os
import sys

os.environ["REVERTO_API_KEY"] = "testkey-for-pytest"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from web.app import app

CLIENT = TestClient(app)
AUTH = {"X-API-Key": "testkey-for-pytest"}
JSON = {**AUTH, "Content-Type": "application/json"}

# Use a slug that could never collide with a real bot
_TEST_SLUG = "pytest_route_check"
_TEST_YAML = f"config/bots/{_TEST_SLUG}.yaml"


@pytest.fixture(autouse=True)
def _cleanup_yaml():
    """Ensure the test bot YAML is gone before and after every test."""
    if os.path.exists(_TEST_YAML):
        os.remove(_TEST_YAML)
    yield
    if os.path.exists(_TEST_YAML):
        os.remove(_TEST_YAML)
    # credentials files created by the auth path during tests. .auth.json
    # is left in place because the bootstrap runs once at module import —
    # removing it would not regenerate it, and removing .credentials.key
    # would poison decryption on the next run. Only touch credentials.json.
    if os.path.exists("logs/credentials.json"):
        os.remove("logs/credentials.json")


def _make_payload(name: str = "Pytest Route Check") -> dict:
    return {
        "bot": {
            "name": name,
            "mode": "paper",
            "exchange": "bitget",
            "pair": "BTC/USD",
            "contract_type": "inverse_perpetual",
            "leverage": {"enabled": False, "size": 1},
            "dca": {
                "base_order_size": 0.001,
                "max_orders": 5,
                "order_spacing_pct": 2.5,
                "multiplier": 1.5,
            },
            "entry": {"indicators": []},
            "take_profit": {"target_pct": 3.0},
            "stop_loss": {"type": "fixed", "pct": 5.0},
        }
    }


class TestBotsRouteRegistration:
    """GET en POST /api/bots moeten allebei geregistreerd zijn."""

    def test_get_bots_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/bots" and "GET" in getattr(r, "methods", set())
        ]
        assert len(routes) == 1, "GET /api/bots must be registered exactly once"

    def test_post_bots_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/bots" and "POST" in getattr(r, "methods", set())
        ]
        assert len(routes) == 1, "POST /api/bots must be registered exactly once"

    def test_both_methods_share_path(self):
        methods = set()
        for r in app.routes:
            if getattr(r, "path", "") == "/api/bots":
                methods.update(getattr(r, "methods", set()))
        assert {"GET", "POST"} <= methods, (
            f"/api/bots must accept both GET and POST, got {methods}"
        )


class TestPostBotsSmoke:
    """End-to-end smoke tests tegen POST /api/bots."""

    def test_post_without_auth_is_401(self):
        r = CLIENT.post("/api/bots", json=_make_payload())
        assert r.status_code == 401

    def test_post_without_body_is_422_not_405(self):
        # 422 is correct (missing body). 405 would mean the POST route
        # is not registered at all — exactly the regression we guard against.
        r = CLIENT.post("/api/bots", headers=AUTH)
        assert r.status_code != 405
        assert r.status_code == 422

    def test_post_with_valid_payload_creates_bot(self):
        r = CLIENT.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert data.get("ok") is True
        assert data.get("slug") == _TEST_SLUG
        assert os.path.exists(_TEST_YAML)

    def test_duplicate_returns_409(self):
        # First create succeeds
        r1 = CLIENT.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r1.status_code == 200
        # Second create on same slug returns 409 Conflict
        r2 = CLIENT.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r2.status_code == 409
        assert "already exists" in r2.json().get("detail", "")

    def test_post_does_not_break_get(self):
        # After a POST the GET listing should still return 200, not 405
        CLIENT.post("/api/bots", json=_make_payload(), headers=JSON)
        # AuthMiddleware now gates /api/*, so pass the API key — the
        # middleware treats a valid X-API-Key as an authenticated principal.
        r = CLIENT.get("/api/bots", headers=AUTH)
        assert r.status_code == 200
        assert "bots" in r.json()


class TestInvalidPayload:
    def test_missing_required_fields_is_400(self):
        bad = {"bot": {"name": "x", "mode": "paper", "exchange": "bitget"}}
        r = CLIENT.post("/api/bots", json=bad, headers=JSON)
        # Pydantic validation → our endpoint wraps into 400, not 422/500
        assert r.status_code == 400
        assert "Invalid config" in r.json().get("detail", "")

    def test_empty_name_after_slugify_is_400(self):
        # Name is all punctuation → slugify() raises → endpoint returns 400
        bad = _make_payload(name="@@@@")
        r = CLIENT.post("/api/bots", json=bad, headers=JSON)
        # Pydantic BotConfig.name validator rejects non-alnum first
        assert r.status_code == 400


# ── Auth tests ────────────────────────────────────────────────────────────────
# These exercise the session-cookie login flow, the gating middleware, and
# the change-password endpoint. They use a dedicated TestClient without the
# API key cookie so we're testing the session path, not the legacy API-key
# bypass.

import bcrypt  # noqa: E402

from web import app as webapp  # noqa: E402

_KNOWN_PW = "pytest-known-password-123"


@pytest.fixture
def auth_client():
    """TestClient with the known password provisioned in .auth.json.
    Yields the client and restores the original auth blob afterwards.

    Forces _COOKIE_SECURE=False for the duration of the test because
    TestClient serves over plain http:// and a browser-equivalent
    silently drops Secure cookies on insecure transports — without
    this override the post-login cookie would never reach the next
    request and every authed assertion would 401.
    """
    original = webapp._load_auth()
    webapp._save_auth({
        "username": "admin",
        "password_hash": bcrypt.hashpw(
            _KNOWN_PW.encode("utf-8"), bcrypt.gensalt(rounds=4)
        ).decode("utf-8"),
    })
    prev_secure = webapp._COOKIE_SECURE
    webapp._COOKIE_SECURE = False
    client = TestClient(webapp.app)
    try:
        yield client
    finally:
        webapp._COOKIE_SECURE = prev_secure
        if original:
            webapp._save_auth(original)


class TestAuth:
    def test_status_unauthenticated_without_cookie(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/auth/status")
        assert r.status_code == 200
        assert r.json() == {"authenticated": False, "username": None}

    def test_login_bad_credentials_returns_401(self, auth_client):
        r = auth_client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid credentials"

    def test_login_success_sets_cookie(self, auth_client):
        r = auth_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # Set-Cookie header must carry the session cookie.
        set_cookie = r.headers.get("set-cookie", "")
        assert "reverto_session=" in set_cookie

    def test_gated_endpoint_requires_session(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/api/bots")
        assert r.status_code == 401

    def test_gated_endpoint_with_session_cookie_works(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/bots")
        assert r.status_code == 200
        assert "bots" in r.json()

    def test_change_password_rejects_short(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": _KNOWN_PW, "new_password": "short"},
        )
        assert r.status_code == 400

    def test_change_password_rejects_wrong_current(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": "not-it", "new_password": "longenough1"},
        )
        assert r.status_code == 401


class TestSessionEpochInvalidation:
    """Server-side session invalidation via the .auth.json session_epoch.
    Logout and password change both bump the epoch, so any cookie minted
    under the previous epoch is rejected on the next request."""

    def test_logout_invalidates_existing_cookie(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        # Cookie works before logout.
        assert auth_client.get("/api/bots").status_code == 200
        # Logout bumps the epoch.
        assert auth_client.post("/auth/logout").status_code == 200
        # Same cookie value, but the server now rejects it because the
        # embedded epoch no longer matches the on-disk one.
        auth_client.cookies.set("reverto_session", token)
        assert auth_client.get("/api/bots").status_code == 401

    def test_password_change_invalidates_existing_cookie(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        # Successful change → epoch bump → cookie no longer valid.
        r = auth_client.post(
            "/api/auth/change-password",
            json={"current_password": _KNOWN_PW, "new_password": "newpassword42"},
        )
        assert r.status_code == 200
        auth_client.cookies.set("reverto_session", token)
        assert auth_client.get("/api/bots").status_code == 401

    def test_fresh_login_after_logout_works(self, auth_client):
        # Bump the epoch via logout first.
        auth_client.post("/auth/logout")
        # New login mints a cookie under the new epoch and works.
        r = auth_client.post(
            "/auth/login",
            json={"username": "admin", "password": _KNOWN_PW},
        )
        assert r.status_code == 200
        # The TestClient picks up the Set-Cookie automatically and the
        # next request is authenticated.
        assert auth_client.get("/api/bots").status_code == 200


class TestDbAnnotationsRoutes:
    """Regression coverage for the /api/db/annotations routes — a past
    report of a 404 on GET turned out to be a 401 from the auth
    middleware, but the routes themselves must stay registered and
    return 200 with a valid session cookie."""

    def test_get_annotations_registered(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/db/annotations?bot_slug=nope&timeframe=1h")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_annotations_without_timeframe(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/db/annotations?bot_slug=nope")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_annotations_missing_bot_slug_is_422_not_404(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get("/api/db/annotations")
        # Missing required query param is a validation error, not a
        # missing route — explicitly assert the non-404 so a future
        # route-removal regression trips this test.
        assert r.status_code == 422


class TestWsStateSmoke:
    """Smoke test for /ws/state — the new bot-state push channel.

    We only assert the handshake succeeds with a valid session cookie
    and that we receive the initial summary frame. File-change pushing
    is covered implicitly: connect() unconditionally emits a summary.
    """

    def test_ws_state_accepts_session_cookie(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        with auth_client.websocket_connect("/ws/state") as ws:
            # We should receive at least one frame — either a bot_state
            # snapshot or the trailing summary frame, depending on how
            # many bots are configured in the test environment.
            import json as _json
            raw = ws.receive_text()
            msg = _json.loads(raw)
            assert msg.get("type") in ("bot_state", "summary")


class TestCandlesRange:
    """Smoke tests for /api/candles/{pair}/{timeframe} — the range
    endpoint backing the client-side backtester. Only input validation
    is asserted here; the success path would require hitting the live
    Bitget exchange which is out of scope for a unit test."""

    def test_candles_route_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/candles/{pair}/{timeframe}"
        ]
        assert len(routes) == 1, "/api/candles must be registered exactly once"

    def test_invalid_timeframe_is_400(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/99h",
            params={"start": "2025-01-01", "end": "2025-01-02"},
        )
        assert r.status_code == 400
        assert "timeframe" in r.json()["detail"]

    def test_start_after_end_is_400(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/1h",
            params={"start": "2025-02-01", "end": "2025-01-01"},
        )
        assert r.status_code == 400

    def test_malformed_timestamp_is_400(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.get(
            "/api/candles/BTCUSD/1h",
            params={"start": "not-a-date", "end": "2025-01-02"},
        )
        assert r.status_code == 400


class TestCandlePagination:
    """Direct unit test for _fetch_ohlcv_range — walks the page cursor
    until two empty pages in a row or end_ms is reached. We mock the
    per-page retry helper so we never touch Bitget, and assert that
    the stitched output has no gaps across page boundaries."""

    def _page(self, since_ms, count, tf_ms, step=1):
        """Build a contiguous page of ccxt-shape candles starting at since_ms."""
        return [
            [since_ms + i * tf_ms, 100.0, 101.0, 99.0, 100.5, 10.0]
            for i in range(count * step)
            if i % step == 0
        ][:count]

    def test_pagination_no_gaps_across_pages(self, monkeypatch):
        """Three 200-candle pages stitched together cover the full range
        without dropping any candles between page boundaries."""
        import asyncio
        from web import app as webapp

        tf_ms = webapp._TF_SECONDS["1h"] * 1000
        start_ms = 1_700_000_000_000
        # 3 pages of 200 bars = 600 bars total
        pages = [
            self._page(start_ms + i * 200 * tf_ms, 200, tf_ms)
            for i in range(3)
        ]
        end_ms = start_ms + 600 * tf_ms

        call_log = []

        async def fake_page(client, symbol, timeframe, since_ms_arg, limit):
            call_log.append(since_ms_arg)
            for page in pages:
                if page and page[0][0] == since_ms_arg:
                    return page
            return []

        monkeypatch.setattr(webapp, "_fetch_ohlcv_page_with_retry", fake_page)

        bars = asyncio.run(webapp._fetch_ohlcv_range(
            client=object(), symbol="BTC/USD", timeframe="1h",
            start_ms=start_ms, end_ms=end_ms,
        ))

        # 600 contiguous bars, no duplicates, no gaps larger than tf_ms
        assert len(bars) == 600
        for i in range(1, len(bars)):
            delta_ms = bars[i][0] - bars[i - 1][0]
            assert delta_ms == tf_ms, (
                f"Gap at index {i}: {delta_ms}ms vs expected {tf_ms}ms"
            )
        # Cursor walked forward page-by-page (each since > previous)
        assert call_log == sorted(set(call_log))
        assert call_log[0] == start_ms

    def test_pagination_stops_on_two_empty_pages(self, monkeypatch):
        """Two consecutive empty pages terminate the walk, so we don't
        spin forever on a timeframe whose history ends mid-range."""
        import asyncio
        from web import app as webapp

        tf_ms = webapp._TF_SECONDS["1h"] * 1000
        start_ms = 1_700_000_000_000
        end_ms = start_ms + 10_000 * tf_ms  # Asking for a huge range

        empties = [0]

        async def fake_page(client, symbol, timeframe, since_ms_arg, limit):
            empties[0] += 1
            return []  # Always empty

        monkeypatch.setattr(webapp, "_fetch_ohlcv_page_with_retry", fake_page)

        bars = asyncio.run(webapp._fetch_ohlcv_range(
            client=object(), symbol="BTC/USD", timeframe="1h",
            start_ms=start_ms, end_ms=end_ms,
        ))
        assert bars == []
        # Should have given up after exactly 2 empty pages (not spun to max_pages)
        assert empties[0] == 2


# ── Deal management endpoints ─────────────────────────────────────────────────

class TestDealEndpoints:
    """PATCH and DELETE /api/bots/{slug}/deals/{deal_id} — validates deal_id
    format and writes sentinel files for valid requests."""

    def test_patch_invalid_deal_id_is_422(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        # Lowercase, spaces, special chars — all must be rejected
        for bad_id in ["evil-inject", "paper-001", "PAPER-0001;rm", "X" * 20]:
            r = auth_client.patch(
                f"/api/bots/test/deals/{bad_id}",
                json={"tp_enabled": True},
            )
            assert r.status_code == 422, f"Expected 422 for deal_id={bad_id!r}, got {r.status_code}"

    def test_patch_valid_deal_id_is_200(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.patch(
            "/api/bots/test/deals/PAPER-0001",
            json={"tp_enabled": True, "tp_target_pct": 3.5},
        )
        assert r.status_code == 200
        assert r.json().get("ok") is True
        # Clean up sentinel
        sentinel = webapp.LOG_DIR / "test.deal_edit_PAPER-0001"
        if sentinel.exists():
            sentinel.unlink()

    def test_delete_cancel_valid(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete(
            "/api/bots/test/deals/PAPER-0002",
            params={"action": "cancel"},
        )
        assert r.status_code == 200
        assert r.json().get("action") == "cancel"
        sentinel = webapp.LOG_DIR / "test.deal_cancel_PAPER-0002"
        if sentinel.exists():
            sentinel.unlink()

    def test_delete_close_valid(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete(
            "/api/bots/test/deals/PAPER-0003",
            params={"action": "close"},
        )
        assert r.status_code == 200
        assert r.json().get("action") == "close"
        sentinel = webapp.LOG_DIR / "test.deal_close_PAPER-0003"
        if sentinel.exists():
            sentinel.unlink()

    def test_delete_invalid_deal_id_is_422(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete(
            "/api/bots/test/deals/evil-inject",
            params={"action": "close"},
        )
        assert r.status_code == 422

    def test_delete_invalid_action_is_400(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete(
            "/api/bots/test/deals/PAPER-0001",
            params={"action": "nuke"},
        )
        assert r.status_code == 400


# ── Annotation POST endpoint ─────────────────────────────────────────────────

class TestAnnotationPost:
    def test_save_annotation_valid(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post("/api/db/annotations", json={
            "bot_slug": "test", "type": "hline", "timeframe": "1h",
            "x1": 1700000000, "y1": 80000.0,
        })
        assert r.status_code == 200
        assert "id" in r.json()

    def test_save_annotation_missing_slug_is_422(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post("/api/db/annotations", json={
            "type": "hline", "timeframe": "1h", "x1": 1700000000,
        })
        assert r.status_code == 422

    def test_save_annotation_x1_out_of_range(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post("/api/db/annotations", json={
            "bot_slug": "test", "type": "hline", "timeframe": "1h",
            "x1": 3_000_000_000, "y1": 80000.0,
        })
        assert r.status_code == 422


# ── Delete backtest runs endpoint ─────────────────────────────────────────────

class TestDeleteBacktestRuns:
    def test_delete_valid_run(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        # Create a run first
        r = auth_client.post("/api/backtest/save", json={
            "slug": "test_del", "name": "Del Test",
            "params": {"start_date": "2025-01-01", "end_date": "2025-06-01",
                       "timeframe": "1h", "initial_balance_btc": 0.1},
            "summary": {"total_pnl_btc": 0.001, "total_deals": 5},
        })
        assert r.status_code == 200
        run_id = r.json()["id"]
        # Delete it
        r2 = auth_client.delete(f"/api/backtest/runs/{run_id}")
        assert r2.status_code == 200
        assert r2.json().get("ok") is True

    def test_delete_nonexistent_run_is_404(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.delete("/api/backtest/runs/999999")
        assert r.status_code == 404

    def test_delete_without_auth_is_401(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.delete("/api/backtest/runs/1")
        assert r.status_code == 401


# ── Start dry-run (Phase-1 live launcher) ─────────────────────────────────────

class TestStartDryRun:
    """Portal-side launcher for live-mode bots under Phase 1 dry-run.

    These tests monkeypatch subprocess.Popen so no real bot is ever
    spawned; the assertions target the command shape + env + the
    mode-guard (paper bots must be refused)."""

    def _live_payload(self) -> dict:
        """Use the default name from _make_payload so the resulting slug
        lines up with _TEST_SLUG — the autouse cleanup fixture only
        knows that single path."""
        p = _make_payload()
        p["bot"]["mode"] = "live"
        return p

    def test_route_registered(self):
        routes = [
            r for r in app.routes
            if getattr(r, "path", "") == "/api/bots/{slug}/start-dry-run"
            and "POST" in getattr(r, "methods", set())
        ]
        assert len(routes) == 1, (
            "POST /api/bots/{slug}/start-dry-run must be registered exactly once"
        )

    def test_unauthenticated_is_401(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.post("/api/bots/whatever/start-dry-run")
        assert r.status_code == 401

    def test_paper_mode_bot_is_refused(self, auth_client, monkeypatch):
        """start_bot_dry_run must return ok=False for a paper-mode bot
        rather than letting main_live.py's hard-mode check swallow it
        silently as an exit-1 subprocess."""
        # Refuse at the helper level — nothing should ever spawn.
        called = {"popen": 0}

        def _fake_popen(*a, **kw):
            called["popen"] += 1
            raise AssertionError("Popen must not run for paper-mode bots")
        monkeypatch.setattr("web.app.subprocess.Popen", _fake_popen)

        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        # Seed a paper-mode YAML via the normal create endpoint.
        create = auth_client.post("/api/bots", json=_make_payload())
        assert create.status_code == 200
        try:
            r = auth_client.post(f"/api/bots/{_TEST_SLUG}/start-dry-run")
            assert r.status_code == 200
            body = r.json()
            assert body.get("ok") is False
            assert "live-mode" in body.get("error", "")
            assert called["popen"] == 0
        finally:
            # Clean up — the autouse fixture removes the YAML, but we
            # also want registry state consistent for the next test.
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")

    def test_live_mode_bot_spawns_main_live_with_dry_run(
        self, auth_client, monkeypatch,
    ):
        """Happy path: live YAML + Popen captured. Verifies argv shape
        (main_live.py --bot <slug> --dry-run) and that DRY_RUN=1 is in
        the child env so the confirmation prompt never blocks a
        non-TTY portal subprocess."""
        captured: dict = {}

        class _FakeProc:
            pid = 4242

        def _fake_popen(cmd, *a, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env", {})
            captured["start_new_session"] = kw.get("start_new_session")
            # Drop a fake PID file so the post-spawn wait loop exits.
            from web.app import PID_DIR
            PID_DIR.mkdir(parents=True, exist_ok=True)
            (PID_DIR / f"{_TEST_SLUG}.pid").write_text(str(_FakeProc.pid))
            return _FakeProc()

        monkeypatch.setattr("web.app.subprocess.Popen", _fake_popen)

        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        assert auth_client.post(
            "/api/bots", json=self._live_payload(),
        ).status_code == 200
        try:
            r = auth_client.post(f"/api/bots/{_TEST_SLUG}/start-dry-run")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("ok") is True, body
            assert "DRY-RUN" in body.get("message", "")

            cmd = captured["cmd"]
            assert cmd[1].endswith("main_live.py"), cmd
            assert "--bot" in cmd and _TEST_SLUG in cmd
            assert "--dry-run" in cmd
            assert captured["env"].get("DRY_RUN") == "1"
            assert captured["start_new_session"] is True
        finally:
            # Remove fake PID + YAML so subsequent tests see a clean slate.
            from web.app import PID_DIR
            pid_file = PID_DIR / f"{_TEST_SLUG}.pid"
            if pid_file.exists():
                pid_file.unlink()
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")

    def test_unknown_slug_is_refused(self, auth_client, monkeypatch):
        def _fake_popen(*a, **kw):
            raise AssertionError("Popen must not run for unknown bot")
        monkeypatch.setattr("web.app.subprocess.Popen", _fake_popen)

        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)
        r = auth_client.post("/api/bots/does_not_exist_anywhere/start-dry-run")
        # Helper returns {"ok": False, ...} rather than raising — the
        # endpoint surfaces that as 200 with the ok flag.
        assert r.status_code == 200
        assert r.json().get("ok") is False


# ── API contract: bot.mode must mirror the YAML ───────────────────────────────

class TestApiBotsReturnsMode:
    """Pins the GET /api/bots contract: the authoritative mode lives in
    the YAML, not in logs/<slug>.state.json. A live-mode bot that has
    never started MUST still surface as mode=live so the overview UI
    can render the orange "Start dry-run" button instead of the green
    paper one. Regression test for the bug where _default_state()
    hardcoded mode=paper for never-started bots."""

    def test_live_yaml_without_state_returns_mode_live(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)

        # Seed a live-mode YAML via the create endpoint. No bot
        # subprocess runs, so logs/<slug>.state.json never appears —
        # this is the exact scenario the bug report hit.
        payload = _make_payload()
        payload["bot"]["mode"] = "live"
        assert auth_client.post("/api/bots", json=payload).status_code == 200

        # Defensive: make sure no stale state file is laying around
        # from a previous test run (autouse fixture removes the YAML
        # but not the state file).
        state_file = f"logs/{_TEST_SLUG}.state.json"
        if os.path.exists(state_file):
            os.remove(state_file)

        try:
            r = auth_client.get("/api/bots")
            assert r.status_code == 200
            bots = {b["slug"]: b for b in r.json()["bots"]}
            assert _TEST_SLUG in bots, f"bot not listed: {list(bots)}"
            assert bots[_TEST_SLUG]["mode"] == "live", (
                f"mode must reflect YAML, got {bots[_TEST_SLUG]['mode']!r}"
            )
        finally:
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")

    def test_paper_yaml_returns_mode_paper(self, auth_client):
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)

        assert auth_client.post(
            "/api/bots", json=_make_payload(),
        ).status_code == 200

        try:
            r = auth_client.get("/api/bots")
            assert r.status_code == 200
            bots = {b["slug"]: b for b in r.json()["bots"]}
            assert bots[_TEST_SLUG]["mode"] == "paper"
        finally:
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")

    def test_yaml_mode_wins_over_state_file(self, auth_client):
        """If the YAML was edited from paper to live but the engine has
        not yet re-written state.json, the UI must already see 'live'."""
        import json as _json
        token = webapp._create_session_cookie("admin")
        auth_client.cookies.set("reverto_session", token)

        # Create a live YAML.
        payload = _make_payload()
        payload["bot"]["mode"] = "live"
        assert auth_client.post("/api/bots", json=payload).status_code == 200

        # Plant a stale state.json that still says mode=paper.
        os.makedirs("logs", exist_ok=True)
        state_file = f"logs/{_TEST_SLUG}.state.json"
        with open(state_file, "w") as fh:
            _json.dump({
                "bot_name": "Pytest Route Check",
                "mode": "paper",
                "exchange": "bitget",
                "pair": "BTC/USD",
            }, fh)

        try:
            r = auth_client.get("/api/bots")
            assert r.status_code == 200
            bots = {b["slug"]: b for b in r.json()["bots"]}
            assert bots[_TEST_SLUG]["mode"] == "live", (
                "YAML mode must win over lagging state.json"
            )
        finally:
            if os.path.exists(state_file):
                os.remove(state_file)
            auth_client.delete(f"/api/bots/{_TEST_SLUG}")
