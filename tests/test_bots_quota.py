"""Tests for the per-user bot creation quota — PT-v4-FS-007.

Pins the cap on every endpoint that creates a new bot:
``POST /api/bots``, ``POST /api/bots/{slug}/duplicate``, and
``POST /api/bots/import``. Without these tests an authenticated user
could fill the disk with thousands of YAMLs + state files and OOM
the host.

The cap defaults to 10 (``_DEFAULT_MAX_BOTS_PER_USER`` in
``web/routes/bots.py``) and is overridable via
``REVERTO_MAX_BOTS_PER_USER``. The override path is exercised
explicitly so a future revision can't silently change the env-var
contract.
"""

from __future__ import annotations

import glob
import os
import pathlib
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web import app as webapp  # noqa: E402

AUTH = {"X-API-Key": "testkey-for-pytest"}
JSON = {**AUTH, "Content-Type": "application/json"}

_TEST_USER_ID = 1


def _make_payload(name: str) -> dict:
    return {
        "bot": {
            "name": name,
            "mode": "paper",
            "exchange_account_id": 1,
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
def _sweep_quota_yaml():
    """Strip every bot YAML this module's tests create. Mirrors the
    sweep style in test_web_routes.py but targets the
    ``pytest_quota_*`` slug prefix so concurrent test runs don't step
    on each other's fixtures."""
    patterns = (
        f"config/bots/{_TEST_USER_ID}/pytest_quota_*.yaml",
        f"logs/{_TEST_USER_ID}/pytest_quota_*.state.json",
        f"logs/{_TEST_USER_ID}/pids/pytest_quota_*.pid",
        f"logs/{_TEST_USER_ID}/pytest_quota_*.log",
    )

    def _sweep():
        for pattern in patterns:
            for path in glob.glob(pattern):
                try:
                    pathlib.Path(path).unlink()
                except OSError:
                    pass

    _sweep()
    yield
    _sweep()


@pytest.fixture
def client(monkeypatch):
    """Per-test TestClient that invalidates the bot registry on
    teardown so a YAML-sweep'd test doesn't leave stale entries
    cached for the next test, and resets the slowapi rate-limiter
    so a 10-create burst inside one test doesn't trip the 20/min
    rule and pollute the next test."""
    webapp.limiter.reset()
    c = TestClient(webapp.app)
    yield c
    webapp.limiter.reset()
    # Drop the cached registry — sweeping the YAMLs alone leaves the
    # in-memory map populated with deleted bots, which would cause
    # the *next* test's first quota check to count phantoms.
    webapp.registry._last_refresh = 0.0


def _create_n_bots(client: TestClient, n: int, prefix: str) -> None:
    """Helper: create ``n`` bots through the public endpoint. Asserts
    each create returned 200 so a quota test failing isn't masked by
    a flaky setup."""
    for i in range(n):
        payload = _make_payload(f"pytest_quota_{prefix}_{i}")
        r = client.post("/api/bots", json=payload, headers=JSON)
        assert r.status_code == 200, (
            f"setup-time create #{i} failed: {r.status_code} {r.text}"
        )


# ── Default cap (10) on POST /api/bots ──────────────────────────────────────


class TestPostBotsQuotaDefault:
    """Default ``REVERTO_MAX_BOTS_PER_USER`` is 10 — the 11th create
    must return 429 with a human-readable string ``detail`` so the
    SPA's ``err.textContent = body.detail`` rendering shows a real
    sentence (not ``[object Object]``)."""

    def test_eleventh_create_returns_429(self, client, monkeypatch):
        # Be explicit: clear any leaked override from a prior test so
        # this case really does exercise the default.
        monkeypatch.delenv("REVERTO_MAX_BOTS_PER_USER", raising=False)
        _create_n_bots(client, 10, prefix="default")

        r = client.post(
            "/api/bots",
            json=_make_payload("pytest_quota_default_overflow"),
            headers=JSON,
        )
        assert r.status_code == 429, r.text
        # Pin the exact wording — if a future PR rewords the message,
        # this test breaks and the change shows up in PR review.
        assert r.json() == {
            "detail": "Bot limit reached. You have 10 bots; the maximum is 10.",
        }

    def test_tenth_create_still_succeeds(self, client, monkeypatch):
        """Boundary: at-cap is the rejection point, the create THAT
        REACHES the cap must succeed."""
        monkeypatch.delenv("REVERTO_MAX_BOTS_PER_USER", raising=False)
        _create_n_bots(client, 9, prefix="boundary")
        r = client.post(
            "/api/bots",
            json=_make_payload("pytest_quota_boundary_tenth"),
            headers=JSON,
        )
        assert r.status_code == 200, r.text


# ── Cap on duplicate + import paths ─────────────────────────────────────────


class TestQuotaOnDuplicateAndImport:
    """Audit pivot: a quota that's only on POST /api/bots leaves the
    duplicate and import endpoints as bypass paths. Both must enforce
    the same cap."""

    def test_duplicate_at_cap_returns_429(self, client, monkeypatch):
        """Fill to cap, then try to duplicate any of the existing bots
        into a new slug. Must 429 — the duplicate would push the total
        above the cap."""
        monkeypatch.delenv("REVERTO_MAX_BOTS_PER_USER", raising=False)
        _create_n_bots(client, 10, prefix="dup")
        # Pick the first bot as the source. Slugify mirrors create_bot:
        # `name.lower()` with spaces preserved → `slugify` strips them.
        source_slug = "pytest_quota_dup_0"
        r = client.post(
            f"/api/bots/{source_slug}/duplicate",
            json={"new_slug": "pytest_quota_dup_overflow"},
            headers=JSON,
        )
        assert r.status_code == 429, r.text
        assert r.json() == {
            "detail": "Bot limit reached. You have 10 bots; the maximum is 10.",
        }

    def test_import_at_cap_returns_429(self, client, monkeypatch):
        """Same shape via the import endpoint. The body is YAML, not
        JSON, but the quota check fires before the body is read so the
        Content-Type doesn't matter."""
        monkeypatch.delenv("REVERTO_MAX_BOTS_PER_USER", raising=False)
        _create_n_bots(client, 10, prefix="imp")

        yaml_body = yaml.safe_dump(_make_payload("pytest_quota_imp_overflow"))
        r = client.post(
            "/api/bots/import?slug=pytest_quota_imp_overflow",
            content=yaml_body,
            headers={**AUTH, "Content-Type": "application/x-yaml"},
        )
        assert r.status_code == 429, r.text
        assert r.json() == {
            "detail": "Bot limit reached. You have 10 bots; the maximum is 10.",
        }


# ── Env-var override ────────────────────────────────────────────────────────


class TestQuotaEnvOverride:
    """``REVERTO_MAX_BOTS_PER_USER`` lets the operator tighten or relax
    the cap without a deploy. Tests pin the override path because a
    silent regression to "default-only" would defeat the configurability
    contract."""

    def test_override_to_three_caps_at_three(self, client, monkeypatch):
        monkeypatch.setenv("REVERTO_MAX_BOTS_PER_USER", "3")
        _create_n_bots(client, 3, prefix="env3")
        r = client.post(
            "/api/bots",
            json=_make_payload("pytest_quota_env3_overflow"),
            headers=JSON,
        )
        assert r.status_code == 429, r.text
        # Both numbers must appear in the message: current count and
        # cap. Pin the exact text so a wording rewrite surfaces in
        # review.
        assert r.json() == {
            "detail": "Bot limit reached. You have 3 bots; the maximum is 3.",
        }

    def test_malformed_override_falls_back_to_default(self, client, monkeypatch):
        """Garbage env-var must not silently disable the cap. The
        helper logs a warning and uses ``_DEFAULT_MAX_BOTS_PER_USER``."""
        monkeypatch.setenv("REVERTO_MAX_BOTS_PER_USER", "not-an-integer")
        # Create 10 bots — these should all succeed under the default.
        _create_n_bots(client, 10, prefix="envbad")
        # The 11th should still be refused by the default cap.
        r = client.post(
            "/api/bots",
            json=_make_payload("pytest_quota_envbad_overflow"),
            headers=JSON,
        )
        assert r.status_code == 429, r.text
        # Default cap (10) wins when the env override is malformed.
        assert r.json() == {
            "detail": "Bot limit reached. You have 10 bots; the maximum is 10.",
        }

    def test_non_positive_override_falls_back_to_default(
        self, client, monkeypatch,
    ):
        """A 0 or negative override would be a foot-gun (effectively
        disables creates). Helper rejects it and falls back to
        default."""
        monkeypatch.setenv("REVERTO_MAX_BOTS_PER_USER", "0")
        _create_n_bots(client, 10, prefix="envzero")
        r = client.post(
            "/api/bots",
            json=_make_payload("pytest_quota_envzero_overflow"),
            headers=JSON,
        )
        assert r.status_code == 429
        assert r.json() == {
            "detail": "Bot limit reached. You have 10 bots; the maximum is 10.",
        }


# ── Helper-level resolver tests ─────────────────────────────────────────────


class TestMaxBotsHelperResolver:
    """Direct unit tests for the env-var resolver. These don't hit the
    FastAPI surface — they just pin the (env var, default, fallback)
    triple so we never have to debug "is the cap 10 or 100?" by
    spelunking middleware ordering."""

    def test_default_when_unset(self, monkeypatch):
        from web.routes.bots import _max_bots_per_user
        monkeypatch.delenv("REVERTO_MAX_BOTS_PER_USER", raising=False)
        assert _max_bots_per_user() == 10

    def test_valid_override(self, monkeypatch):
        from web.routes.bots import _max_bots_per_user
        monkeypatch.setenv("REVERTO_MAX_BOTS_PER_USER", "42")
        assert _max_bots_per_user() == 42

    def test_malformed_override_returns_default(self, monkeypatch):
        from web.routes.bots import _max_bots_per_user
        monkeypatch.setenv("REVERTO_MAX_BOTS_PER_USER", "twelve")
        assert _max_bots_per_user() == 10

    def test_negative_override_returns_default(self, monkeypatch):
        from web.routes.bots import _max_bots_per_user
        monkeypatch.setenv("REVERTO_MAX_BOTS_PER_USER", "-5")
        assert _max_bots_per_user() == 10
