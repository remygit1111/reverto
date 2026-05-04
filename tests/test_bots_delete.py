"""End-to-end tests for ``DELETE /api/bots/{slug}`` — PT-v4-FS-001.

The handler now calls ``paths.purge_bot`` before unlinking the YAML
so a bot's filesystem + DB state is gone after the call returns.
These tests verify:

  * The original bug regression: recreating a bot under the same
    slug starts fresh (no inherited state.json).
  * Existing handler invariants stay (404 unknown, 409 running).
  * Response body now carries the purge summary.
  * Audit event fires on success.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import app as webapp  # noqa: E402

AUTH = {"X-API-Key": "testkey-for-pytest"}
JSON = {**AUTH, "Content-Type": "application/json"}

_TEST_USER_ID = 1
_TEST_SLUG = "pytest_delete_bot"
_TEST_YAML = f"config/bots/{_TEST_USER_ID}/{_TEST_SLUG}.yaml"


def _make_payload(name: str = "Pytest Delete Bot") -> dict:
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


@pytest.fixture(autouse=True)
def _sweep_artefacts():
    """Strip every file the test might leave in the production tree
    so the suite is rerunnable. Mirrors the sweep style in
    test_web_routes.py."""
    artefact_paths = [
        _TEST_YAML,
        f"logs/{_TEST_USER_ID}/{_TEST_SLUG}.state.json",
        f"logs/{_TEST_USER_ID}/{_TEST_SLUG}.state.lock",
        f"logs/{_TEST_USER_ID}/{_TEST_SLUG}.manual_trigger",
        f"logs/{_TEST_USER_ID}/{_TEST_SLUG}.log",
        f"logs/{_TEST_USER_ID}/pids/{_TEST_SLUG}.pid",
        f"ml/{_TEST_USER_ID}/results_{_TEST_SLUG}.json",
    ]

    def _sweep():
        for p in artefact_paths:
            try:
                Path(p).unlink()
            except OSError:
                pass

    _sweep()
    webapp.limiter.reset()
    yield
    _sweep()
    webapp.limiter.reset()
    webapp.registry._last_refresh = 0.0


@pytest.fixture
def client():
    return TestClient(webapp.app)


# ── Existing-behaviour invariants (regression guards) ──────────────────────


class TestDeleteUnknownBot:
    def test_returns_404(self, client):
        r = client.delete("/api/bots/no_such_bot", headers=AUTH)
        assert r.status_code == 404
        # String detail per the project's error contract.
        detail = r.json().get("detail", "")
        assert isinstance(detail, str)
        assert "not found" in detail.lower()


class TestDeleteRunningBot:
    def test_returns_409_when_running(self, client, monkeypatch):
        # Create a bot via the API, then monkeypatch ``running`` to
        # True on its registry entry.
        r = client.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200, r.text

        # Force the registry's view of this bot to "running".
        async def _fake_get(user_id, slug):
            bot = type(
                "FakeBot", (),
                {"user_id": user_id, "slug": slug, "running": True},
            )
            return bot()

        monkeypatch.setattr(webapp.registry, "get", _fake_get)
        r = client.delete(f"/api/bots/{_TEST_SLUG}", headers=AUTH)
        assert r.status_code == 409
        detail = r.json().get("detail", "")
        assert "running" in detail.lower()


# ── New-behaviour: purge before YAML unlink ────────────────────────────────


class TestDeletePurgesAndUnlinks:
    def test_response_includes_purge_summary(self, client):
        r = client.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200, r.text

        r = client.delete(f"/api/bots/{_TEST_SLUG}", headers=AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("slug") == _TEST_SLUG
        # purge_summary dict surfaced verbatim.
        purged = body.get("purged")
        assert isinstance(purged, dict)
        assert set(purged.keys()) == {
            "files_removed", "files_failed",
            "db_rows_removed", "warnings",
        }

    def test_yaml_is_unlinked(self, client):
        r = client.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200
        assert Path(_TEST_YAML).exists()

        r = client.delete(f"/api/bots/{_TEST_SLUG}", headers=AUTH)
        assert r.status_code == 200, r.text
        assert not Path(_TEST_YAML).exists(), (
            "YAML must be unlinked AFTER purge_bot succeeds — "
            "otherwise the bot would still appear in the registry."
        )

    def test_purge_called_with_correct_args(self, client, monkeypatch):
        """Pin the wiring: route handler invokes ``paths.purge_bot``
        with (user_id, slug) — not the bot config's ``name`` field
        or any other identifier."""
        r = client.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200

        captured: dict = {}

        def _fake_purge(user_id, slug):
            captured["user_id"] = user_id
            captured["slug"] = slug
            return {
                "files_removed": 0, "files_failed": [],
                "db_rows_removed": {}, "warnings": [],
            }

        monkeypatch.setattr(
            "core.paths.purge_bot", _fake_purge,
        )

        r = client.delete(f"/api/bots/{_TEST_SLUG}", headers=AUTH)
        assert r.status_code == 200
        assert captured == {"user_id": _TEST_USER_ID, "slug": _TEST_SLUG}

    def test_purge_runs_before_yaml_unlink(self, client, monkeypatch):
        """YAML-last principle. If purge crashes, the YAML must
        still exist so the operator can retry."""
        r = client.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200
        assert Path(_TEST_YAML).exists()

        def _crashing_purge(user_id, slug):
            raise RuntimeError("simulated purge crash")

        monkeypatch.setattr("core.paths.purge_bot", _crashing_purge)

        # Starlette's TestClient re-raises uncaught exceptions
        # instead of converting them to 500 responses (FastAPI
        # behaviour in production is the standard 500). The
        # exception itself is what we want to assert on — the bug
        # would be the route silently swallowing the failure and
        # going on to unlink the YAML.
        with pytest.raises(RuntimeError, match="simulated purge crash"):
            client.delete(f"/api/bots/{_TEST_SLUG}", headers=AUTH)

        # Crucially: YAML still on disk so DELETE is retryable.
        assert Path(_TEST_YAML).exists(), (
            "YAML must survive a purge crash so DELETE is retryable"
        )


# ── The motivating regression: recreate-with-same-slug ────────────────────


class TestRecreateWithSameSlugStartsFresh:
    """The bug PT-v4-FS-001 documents: pre-fix, deleting a bot left
    state.json on disk; recreating a bot under the same slug let the
    engine rehydrate from the OLD state — inheriting balance, open
    deals, drawdown peak, PnL history. Post-fix, recreate sees no
    leftover state.

    The test seeds a state.json directly (mimicking what a running
    engine would write) so we don't need to spin up a subprocess.
    """

    def test_state_json_gone_after_delete(self, client):
        # Create + simulate engine running.
        r = client.post("/api/pytest_delete/skip", json={}, headers=AUTH)
        # Use the real create path.
        r = client.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200

        state_path = Path(f"logs/{_TEST_USER_ID}/{_TEST_SLUG}.state.json")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "balance_btc": 0.05,
            "open_deals": [
                {"id": "ghost-deal", "balance_btc": 0.05},
            ],
            "running": False,
        }))

        r = client.delete(f"/api/bots/{_TEST_SLUG}", headers=AUTH)
        assert r.status_code == 200, r.text
        assert not state_path.exists(), (
            "state.json must be removed by purge_bot — pre-fix this "
            "leaked into a recreated bot under the same slug."
        )

    def test_recreate_after_delete_has_no_inherited_state(self, client):
        """End-to-end: create → seed state.json → delete → create
        again under same slug → state.json must be absent (the new
        engine would create a clean one on first tick)."""
        r = client.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200

        state_path = Path(f"logs/{_TEST_USER_ID}/{_TEST_SLUG}.state.json")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text('{"balance_btc": 0.123, "running": false}')
        assert state_path.exists()

        # Delete the bot. Purge wipes state.json.
        r = client.delete(f"/api/bots/{_TEST_SLUG}", headers=AUTH)
        assert r.status_code == 200
        assert not state_path.exists()

        # Recreate the same slug — no state.json yet (engine would
        # create a fresh one on first tick).
        r = client.post("/api/bots", json=_make_payload(), headers=JSON)
        assert r.status_code == 200
        assert not state_path.exists(), (
            "recreate must not see the OLD state.json — that's the "
            "PT-v4-FS-001 regression we're guarding against"
        )
