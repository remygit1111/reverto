"""Cost-based token-bucket rate limiter (audit r1-045).

Complements the coarse per-endpoint slowapi ``@limiter.limit``
decorators — useful when different requests to the same endpoint
carry very different server-side costs. ``/api/candles`` is the
motivating case: a 100-candle request is cheap, a 5000-candle
request hits the paginated fetch path hard. A flat "20/minute"
lets 20 expensive requests through as easily as 20 trivial ones.

Each key has a budget (max points) that refills at a constant
rate. ``consume(key, cost)`` succeeds when enough budget remains
and debits the cost; returns False otherwise. Synchronous, no
external store — fine for single-process deploys. Multi-worker
uvicorn gets the same caveat as the slowapi in-memory limiter
(see audit r1-026): each worker holds its own state.
"""

from __future__ import annotations

import time
from typing import Optional


class CostBudget:
    """Per-key token bucket with constant refill.

    Parameters
    ----------
    budget:
        Maximum cost-points a key can hold at rest. Bursty callers
        can accumulate up to this much budget while idle, then
        spend it in one larger call.
    refill_per_second:
        Points regenerated per second toward the budget. A sustained
        caller's effective throughput is capped here.

    Default tuning for ``/api/candles`` (budget=10000, refill=100):
      * Sustained: ≈ 100 candles/sec = one 5000-candle request
        every 50 seconds.
      * Burst: up to 10000 candles (two 5000-candle requests
        back-to-back when idle).
    """

    def __init__(
        self,
        budget: int = 10000,
        refill_per_second: int = 100,
    ) -> None:
        self.budget = float(budget)
        self.refill_rate = float(refill_per_second)
        self._state: dict[str, tuple[float, float]] = {}

    def consume(self, key: str, cost: int) -> bool:
        """Try to debit ``cost`` points from ``key``'s bucket.

        Returns True on success (bucket debited), False when the
        refilled budget is still < cost. Failed attempts still
        update the bucket's timestamp so a subsequent call reads
        the freshest elapsed-time window.
        """
        if cost <= 0:
            # Zero-cost pass-throughs should never reject; makes
            # the helper safe to pre-guard a call-site even when
            # cost is computed dynamically and may be clamped low.
            return True
        now = time.time()
        last_ts, remaining = self._state.get(key, (now, self.budget))
        elapsed = max(0.0, now - last_ts)
        remaining = min(self.budget, remaining + elapsed * self.refill_rate)
        if remaining < cost:
            self._state[key] = (now, remaining)
            return False
        self._state[key] = (now, remaining - cost)
        return True

    def peek(self, key: str) -> float:
        """Return current (refilled) budget for ``key`` without
        debiting. Useful for tests + operator metrics."""
        now = time.time()
        last_ts, remaining = self._state.get(key, (now, self.budget))
        elapsed = max(0.0, now - last_ts)
        return min(self.budget, remaining + elapsed * self.refill_rate)

    def reset(self, key: Optional[str] = None) -> None:
        """Reset one key (or the whole bucket when ``key`` is None).
        Test-only helper; production callers should never need it."""
        if key is None:
            self._state.clear()
        else:
            self._state.pop(key, None)
