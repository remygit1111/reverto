"""Tests for core/rate_budget.CostBudget (audit r1-045).

Pure-Python token-bucket; no network, no clock-wall dependencies
beyond time.time (which we monkeypatch for determinism).
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.rate_budget import CostBudget  # noqa: E402


def test_fresh_bucket_allows_up_to_budget():
    b = CostBudget(budget=1000, refill_per_second=10)
    assert b.consume("u1", 500) is True
    assert b.consume("u1", 500) is True
    assert b.consume("u1", 1) is False  # exhausted


def test_per_key_isolation():
    b = CostBudget(budget=500, refill_per_second=10)
    assert b.consume("user:1", 500) is True
    # User 2 has a fresh bucket — exhaustion on user:1 doesn't
    # bleed into it.
    assert b.consume("user:2", 500) is True


def test_refill_replenishes_over_time(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr("core.rate_budget.time.time", lambda: now["t"])
    b = CostBudget(budget=1000, refill_per_second=100)

    # Spend everything, then advance 5 s — should refill 500 points.
    assert b.consume("u", 1000) is True
    assert b.consume("u", 1) is False
    now["t"] = 1005.0
    assert b.consume("u", 400) is True  # within refilled 500
    # peek should now show ~100 remaining.
    assert 90 < b.peek("u") < 110


def test_cost_zero_is_always_allowed():
    # Safety-hatch for callers that clamp cost to 0 dynamically.
    b = CostBudget(budget=10, refill_per_second=1)
    # Exhaust the bucket first.
    b.consume("u", 10)
    # Zero-cost should still succeed.
    assert b.consume("u", 0) is True


def test_refill_caps_at_budget(monkeypatch):
    now = {"t": 2000.0}
    monkeypatch.setattr("core.rate_budget.time.time", lambda: now["t"])
    b = CostBudget(budget=1000, refill_per_second=100)

    # Fresh bucket, idle for a long time — peek should still be
    # capped at the budget, not refilled past it.
    now["t"] = 2999.0
    assert b.peek("u") == 1000


def test_reset_clears_key_state(monkeypatch):
    b = CostBudget(budget=100, refill_per_second=1)
    b.consume("u", 100)
    assert b.consume("u", 1) is False
    b.reset("u")
    # After reset the key returns to a fresh-full bucket.
    assert b.consume("u", 100) is True
