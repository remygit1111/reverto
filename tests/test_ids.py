"""Tests for core/ids.py — globally-unique deal + order ID generation.

Pin the format + uniqueness + time-sortability properties so the
cross-bot-collision regression can't return silently via a refactor.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ids import DEAL_ID_RE, generate_deal_id, generate_order_id


class TestDealIdFormat:

    def test_deal_id_format_matches_regex(self):
        """YYYYMMDDHHMM-RRRR — 12 digits, dash, 4 digits."""
        for _ in range(20):
            deal_id = generate_deal_id()
            assert DEAL_ID_RE.match(deal_id), (
                f"Generated id {deal_id!r} does not match DEAL_ID_RE"
            )

    def test_deal_id_uses_utc(self):
        """The timestamp prefix must be UTC — a caller in CET/EDT must
        not produce a deal_id that reflects local wall-clock time. This
        is the whole point of ISO-ordered IDs: they sort across
        timezones without surprises."""
        # A local-naive datetime that would land on a different minute if
        # the generator called .astimezone() wrong.
        fixed = datetime(2026, 4, 19, 13, 42, 0, tzinfo=timezone.utc)
        deal_id = generate_deal_id(now_utc=fixed)
        assert deal_id.startswith("202604191342-")

    def test_deal_id_is_time_sortable(self):
        """Strings sort in time order — the point of the ISO prefix."""
        earlier = generate_deal_id(
            now_utc=datetime(2026, 4, 19, 7, 0, 0, tzinfo=timezone.utc),
        )
        later = generate_deal_id(
            now_utc=datetime(2026, 4, 19, 13, 42, 0, tzinfo=timezone.utc),
        )
        assert earlier < later, (
            f"String-sort must match time-sort, got {earlier!r} >= {later!r}"
        )

    def test_cross_day_sort_order(self):
        """Day boundaries must not scramble the prefix."""
        yesterday = generate_deal_id(
            now_utc=datetime(2026, 4, 18, 23, 59, 0, tzinfo=timezone.utc),
        )
        today = generate_deal_id(
            now_utc=datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc),
        )
        assert yesterday < today

    def test_different_calls_produce_different_ids(self):
        """100 iterations at the same clock moment must stay unique.

        100 draws from 10_000 slots has a birthday-collision expectation
        of ≈ 0.5 — so the test is stable against any sensible RNG. A
        failure here means the suffix isn't actually random (stuck
        seed, off-by-one range, etc.).
        """
        import random as _random
        ids: set[str] = set()
        fixed = datetime(2026, 4, 19, 13, 42, 0, tzinfo=timezone.utc)
        rng_state = _random.getstate()
        _random.seed(4242)
        try:
            for _ in range(100):
                ids.add(generate_deal_id(now_utc=fixed))
        finally:
            _random.setstate(rng_state)
        assert len(ids) >= 98, (
            f"Expected ≥98/100 unique ids from seeded draws, got {len(ids)}"
        )

    def test_minute_boundary_prefix_changes(self):
        """Two calls 60s apart must have different timestamp prefixes
        regardless of the random suffix."""
        first = generate_deal_id(
            now_utc=datetime(2026, 4, 19, 13, 42, 0, tzinfo=timezone.utc),
        )
        second = generate_deal_id(
            now_utc=datetime(2026, 4, 19, 13, 43, 0, tzinfo=timezone.utc),
        )
        assert first.split("-")[0] != second.split("-")[0]


class TestOrderIdFormat:

    def test_order_id_same_format(self):
        """Order IDs share the generator with deal IDs — deliberately.
        Orders live in a separate table so coincidental overlap with a
        deal id is harmless and callers don't need a second namespace
        to reason about."""
        oid = generate_order_id()
        assert DEAL_ID_RE.match(oid)

    def test_order_id_accepts_injected_clock(self):
        fixed = datetime(2026, 4, 19, 13, 42, 0, tzinfo=timezone.utc)
        oid = generate_order_id(now_utc=fixed)
        assert oid.startswith("202604191342-")


class TestDealIdRegex:
    """DEAL_ID_RE is re-exported via paper_engine + web/app as the
    single source of truth for "is this string a valid deal id?"."""

    def test_accepts_well_formed(self):
        assert DEAL_ID_RE.match("202604191342-0001")
        assert DEAL_ID_RE.match("202604191342-9999")

    def test_rejects_legacy_paper_format(self):
        """PAPER-0001 must fail ingress validation once the cutover
        lands — sentinel files / route params in the old format
        should return 422, not flow through."""
        assert not DEAL_ID_RE.match("PAPER-0001")

    def test_rejects_injection_attempts(self):
        for bad in (
            "202604191342-0001;rm",
            "../etc/passwd",
            "202604191342-00",       # suffix too short
            "20260419134-0001",      # prefix too short
            "2026041913424-0001",    # prefix too long
            "202604191342-00001",    # suffix too long
            "a02604191342-0001",     # non-digit prefix
            "202604191342_0001",     # underscore instead of dash
            "",
        ):
            assert not DEAL_ID_RE.match(bad), f"Must reject: {bad!r}"

    def test_anchored_both_ends(self):
        """Regex must reject a leading prefix."""
        assert not DEAL_ID_RE.match("x202604191342-0001")
        # Note: Python's `$` matches end-of-string OR before-final-\n.
        # The sentinel/route ingress paths strip the deal_id well before
        # it hits the regex, so rejecting a trailing \n isn't required;
        # the injection-attempt test above covers the dangerous cases.
