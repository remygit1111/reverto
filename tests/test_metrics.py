"""Tests for web/metrics.py + /metrics endpoint + /healthz + /readyz.

Counters/gauges are module-globals in prometheus_client so their value
persists between tests. We use `._value.get()` as the portable read
path (available in prometheus_client >= 0.11); if a future release
changes the internal, update here + pin version.
"""

import os
import sys

os.environ["REVERTO_API_KEY"] = "testkey-for-pytest"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ccxt  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from unittest.mock import patch  # noqa: E402

from web import app as webapp  # noqa: E402
from web import metrics  # noqa: E402


CLIENT = TestClient(webapp.app)


# ── classify_error ──────────────────────────────────────────────────────────

class TestClassifyError:
    """Bounded-label mapping. Every case must return one of the
    documented label strings — no falling back to raw classname."""

    def test_rate_limit_mapped(self):
        assert metrics.classify_error(
            ccxt.RateLimitExceeded("slow")
        ) == "rate_limit"

    def test_insufficient_funds_mapped(self):
        assert metrics.classify_error(
            ccxt.InsufficientFunds("broke")
        ) == "insufficient_funds"

    def test_network_error_mapped(self):
        assert metrics.classify_error(
            ccxt.NetworkError("timeout")
        ) == "network"

    def test_not_implemented_mapped(self):
        assert metrics.classify_error(NotImplementedError()) == "not_implemented"

    def test_value_error_mapped(self):
        assert metrics.classify_error(ValueError("x")) == "value_error"

    def test_unknown_falls_back_to_other(self):
        class _Weird(Exception):
            pass
        assert metrics.classify_error(_Weird("?")) == "other"


# ── Counter + Gauge helpers ─────────────────────────────────────────────────

class TestRecordTick:

    def test_record_tick_increments(self):
        """record_tick() increases the per-slug counter by exactly 1."""
        counter = metrics.bot_ticks_total.labels(bot_slug="rt", mode="paper")
        before = counter._value.get()
        metrics.record_tick("rt", "paper")
        assert counter._value.get() == before + 1

    def test_record_tick_error_accepts_exception(self):
        """Passing an exception instance maps via classify_error."""
        c = metrics.bot_tick_errors_total.labels(
            bot_slug="rt_err", kind="rate_limit",
        )
        before = c._value.get()
        metrics.record_tick_error("rt_err", ccxt.RateLimitExceeded("x"))
        assert c._value.get() == before + 1

    def test_record_tick_error_accepts_label_string(self):
        """Legacy caller path: str label passes through unchanged."""
        c = metrics.bot_tick_errors_total.labels(
            bot_slug="rt_str", kind="legacy_label",
        )
        before = c._value.get()
        metrics.record_tick_error("rt_str", "legacy_label")
        assert c._value.get() == before + 1


class TestGaugeSetters:

    def test_set_balance(self):
        metrics.set_balance("gb", 0.12345)
        assert metrics.bot_balance_btc.labels(bot_slug="gb")._value.get() == 0.12345

    def test_set_open_deals(self):
        metrics.set_open_deals("gd", 3)
        assert metrics.bot_open_deals.labels(bot_slug="gd")._value.get() == 3

    def test_set_drawdown_pct(self):
        metrics.set_drawdown_pct("gp", 7.5)
        assert metrics.bot_drawdown_pct.labels(bot_slug="gp")._value.get() == 7.5


# ── /metrics endpoint ───────────────────────────────────────────────────────

class TestMetricsEndpoint:

    def test_metrics_returns_prometheus_text(self):
        r = CLIENT.get("/metrics")
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        # prometheus_client uses `text/plain; version=0.0.4; charset=utf-8`.
        assert "text/plain" in ct
        assert "# HELP" in r.text
        assert "# TYPE" in r.text

    def test_metrics_requires_no_auth(self):
        """Scrape endpoint is network-gated, not application-gated.
        Confirm no 401/403 on an unauthenticated request."""
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/metrics")
        assert r.status_code == 200

    def test_metrics_includes_known_series(self):
        """After we exercise the helpers, the scrape must list them."""
        metrics.record_tick("endpointbot", "paper")
        metrics.set_balance("endpointbot", 0.05)
        body = CLIENT.get("/metrics").text
        assert "reverto_bot_ticks_total" in body
        assert "reverto_bot_balance_btc" in body


# ── /healthz + /readyz ──────────────────────────────────────────────────────

class TestHealthEndpoints:

    def test_healthz_returns_ok(self):
        r = CLIENT.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "timestamp" in body
        assert "pid" in body

    def test_healthz_requires_no_auth(self):
        client = TestClient(webapp.app)
        client.cookies.clear()
        r = client.get("/healthz")
        assert r.status_code == 200

    def test_readyz_ok_with_working_db(self):
        r = CLIENT.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"

    def test_readyz_503_on_db_error(self):
        """Simulate a broken SELECT 1 — readyz must switch to 503."""

        def _boom() -> None:
            raise RuntimeError("DB locked")

        with patch.object(webapp, "_check_db_sync_blocking", _boom):
            r = CLIENT.get("/readyz")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert "DB locked" in body["error"]

    def test_readyz_503_on_timeout(self):
        """A DB call that hangs past 3s must surface as a 503 with a
        timeout error string."""
        import time as _time

        def _hang() -> None:
            _time.sleep(5)

        with patch.object(webapp, "_check_db_sync_blocking", _hang):
            r = CLIENT.get("/readyz")
        assert r.status_code == 503
        assert "timed out" in r.json().get("error", "")
