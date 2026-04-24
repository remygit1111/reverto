"""Regression guard for audit r1-020 — N+1 fix in /api/db/deals.

``core.deal_store.get_orders_for_deal_ids`` is the batch-fetch
helper that replaces the N+1 pattern in /api/db/deals. These tests
pin its shape + tenant-scoping + empty-list semantics so a future
refactor can't silently reintroduce the per-deal loop.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("REVERTO_API_KEY", "testkey-for-pytest")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core import database, deal_store  # noqa: E402
from paper.paper_state import PaperDeal, PaperOrder  # noqa: E402


_FIXED_MINUTE = datetime(2026, 4, 24, 13, 42, 0, tzinfo=timezone.utc)


def _order(n: int, price: float = 80000.0):
    return PaperOrder(
        order_number=n, price=price, size=0.001,
        timestamp=_FIXED_MINUTE,
        order_type="base" if n == 1 else "dca",
    )


def _deal(deal_id: str, orders: list[PaperOrder]) -> PaperDeal:
    return PaperDeal(
        id=deal_id, bot_name="tb", symbol="BTC/USD",
        side="long", leverage=1, orders=orders,
    )


@pytest.fixture
def fresh_db(tmp_path):
    database.set_db_path(tmp_path / "deals_batch.db")
    database.init_db()
    yield tmp_path
    database.close_db()


def _seed_deal_with_orders(deal_id: str, orders: list[PaperOrder],
                           bot_slug: str, user_id: int) -> None:
    """Save both the deal row and each order row under a bot/user.

    ``save_deal`` persists the deal header only; orders live in the
    separate ``orders`` table and need their own insert. Factored
    out so every test scenario seeds consistently.
    """
    deal_store.save_deal(
        _deal(deal_id, orders), bot_slug, bot_slug.upper(), user_id=user_id,
    )
    for o in orders:
        deal_store.save_order(o, deal_id, bot_slug, user_id=user_id)


class TestGetOrdersForDealIds:
    def test_empty_list_returns_empty_dict(self, fresh_db):
        assert deal_store.get_orders_for_deal_ids([], user_id=1) == {}

    def test_batches_orders_per_deal(self, fresh_db):
        # Seed three deals each with two orders under user 1.
        for suffix in ("0001", "0002", "0003"):
            _seed_deal_with_orders(
                f"202604241342-{suffix}",
                [_order(1), _order(2, 79000.0)],
                bot_slug=f"bot_{suffix}", user_id=1,
            )

        result = deal_store.get_orders_for_deal_ids(
            [
                "202604241342-0001",
                "202604241342-0002",
                "202604241342-0003",
            ],
            user_id=1,
        )

        assert set(result.keys()) == {
            "202604241342-0001",
            "202604241342-0002",
            "202604241342-0003",
        }
        for did, orders in result.items():
            assert len(orders) == 2, f"{did}: {len(orders)}"
            assert [o["order_number"] for o in orders] == [1, 2]

    def test_unknown_deal_ids_get_empty_lists(self, fresh_db):
        # Two known, one ghost. The ghost must land as [] so callers
        # can iterate without a KeyError guard.
        _seed_deal_with_orders(
            "202604241342-0042", [_order(1)],
            bot_slug="bot_a", user_id=1,
        )
        result = deal_store.get_orders_for_deal_ids(
            ["202604241342-0042", "ghost-does-not-exist"], user_id=1,
        )
        assert result["202604241342-0042"][0]["order_number"] == 1
        assert result["ghost-does-not-exist"] == []

    def test_user_scoping_blocks_cross_tenant_leak(self, fresh_db):
        # User 1 owns one deal; user 2 asks for its id — must get
        # back an empty list, not the other tenant's orders.
        _seed_deal_with_orders(
            "202604241342-0077", [_order(1), _order(2)],
            bot_slug="bot_a", user_id=1,
        )

        as_user_2 = deal_store.get_orders_for_deal_ids(
            ["202604241342-0077"], user_id=2,
        )
        assert as_user_2 == {"202604241342-0077": []}

        as_user_1 = deal_store.get_orders_for_deal_ids(
            ["202604241342-0077"], user_id=1,
        )
        assert len(as_user_1["202604241342-0077"]) == 2
