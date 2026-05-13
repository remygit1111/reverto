"""Tests for core.portfolio_store — CRUD + manual rate-limit gate.

The autouse ``_isolate_reverto_db`` fixture in conftest.py wires a
fresh SQLite file with the v13 schema for every test, so
``portfolio_snapshots`` already exists when each test starts.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core import portfolio_store  # noqa: E402
from core.database import get_db  # noqa: E402


def _seed_account(
    user_id: int = 1, account_id: int = 11, alias: str | None = None,
) -> int:
    """Insert an ``exchange_accounts`` row so the FK on portfolio
    snapshots is satisfied. UUID is a placeholder — the store never
    decrypts it in these tests. ``alias`` defaults to a per-account
    string so multiple seeds within one test don't trip the UNIQUE
    constraint on (user, exchange_type, market_type, alias).
    """
    alias = alias if alias is not None else f"acct-{account_id}"
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO exchange_accounts "
            "(id, user_id, exchange_type, market_type, alias, "
            " credentials_uuid) "
            "VALUES (?, ?, 'bitget', 'coin_m', ?, 'uuid-stub')",
            (account_id, user_id, alias),
        )
    return account_id


class TestCreateSnapshot:

    def test_happy_path(self):
        _seed_account()
        sid = portfolio_store.create_snapshot(
            user_id=1, exchange_account_id=11,
            balance_native=0.001, currency="BTC",
            balance_usd=65.0, usd_rate=65000.0,
            rate_source="coingecko", source="auto",
        )
        assert isinstance(sid, int)
        assert sid > 0

    def test_rejects_unknown_source(self):
        _seed_account()
        with pytest.raises(ValueError):
            portfolio_store.create_snapshot(
                user_id=1, exchange_account_id=11,
                balance_native=0.001, currency="BTC",
                balance_usd=65.0, usd_rate=65000.0,
                rate_source="coingecko", source="bogus",
            )

    def test_fk_violation_on_missing_account(self):
        # No seeded account — FK should refuse the insert.
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            portfolio_store.create_snapshot(
                user_id=1, exchange_account_id=999,
                balance_native=0.001, currency="BTC",
                balance_usd=65.0, usd_rate=65000.0,
                rate_source="coingecko", source="auto",
            )


class TestLatestPerAccount:

    def test_empty_returns_empty_list(self):
        assert portfolio_store.latest_per_account(1) == []

    def test_picks_most_recent_per_account(self):
        _seed_account(account_id=11)
        _seed_account(account_id=12)
        # Two snapshots for account 11 — make sure we get the newer one.
        portfolio_store.create_snapshot(
            user_id=1, exchange_account_id=11,
            balance_native=0.001, currency="BTC",
            balance_usd=60.0, usd_rate=60000.0,
            rate_source="coingecko", source="auto",
        )
        # Force the second row to a later captured_at so MAX() is
        # deterministic regardless of clock resolution.
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(user_id, exchange_account_id, captured_at, balance_native, "
                " currency, balance_usd, usd_rate, rate_source, source) "
                "VALUES (1, 11, '2030-01-01T00:00:00', 0.002, 'BTC', 130.0, "
                " 65000.0, 'coingecko', 'auto')",
            )
        # And one snapshot for account 12.
        portfolio_store.create_snapshot(
            user_id=1, exchange_account_id=12,
            balance_native=50.0, currency="USDT",
            balance_usd=50.0, usd_rate=1.0,
            rate_source="bitget", source="auto",
        )
        rows = portfolio_store.latest_per_account(1)
        assert len(rows) == 2
        by_acct = {r["exchange_account_id"]: r for r in rows}
        assert by_acct[11]["balance_usd"] == pytest.approx(130.0)
        assert by_acct[12]["currency"] == "USDT"
        # Joined account metadata is on each row.
        assert by_acct[11]["alias"] == "acct-11"
        assert by_acct[11]["exchange_type"] == "bitget"

    def test_cross_user_isolation(self):
        # User 1's account.
        _seed_account(user_id=1, account_id=21)
        # Seed a second user and account.
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO users (id, username, role) "
                "VALUES (2, 'bob', 'user')",
            )
        _seed_account(user_id=2, account_id=22)
        portfolio_store.create_snapshot(
            user_id=1, exchange_account_id=21,
            balance_native=1.0, currency="BTC",
            balance_usd=65000.0, usd_rate=65000.0,
            rate_source="coingecko", source="auto",
        )
        portfolio_store.create_snapshot(
            user_id=2, exchange_account_id=22,
            balance_native=2.0, currency="BTC",
            balance_usd=130000.0, usd_rate=65000.0,
            rate_source="coingecko", source="auto",
        )
        assert len(portfolio_store.latest_per_account(1)) == 1
        assert len(portfolio_store.latest_per_account(2)) == 1
        assert portfolio_store.latest_per_account(1)[0][
            "exchange_account_id"
        ] == 21


class TestHistory:

    def test_returns_rows_in_range(self):
        _seed_account(account_id=11)
        conn = get_db()
        with conn:
            for ts, val in [
                ("2026-05-13T08:00:00", 100.0),
                ("2026-05-13T09:00:00", 110.0),
                ("2026-05-13T10:00:00", 120.0),
            ]:
                conn.execute(
                    "INSERT INTO portfolio_snapshots "
                    "(user_id, exchange_account_id, captured_at, "
                    " balance_native, currency, balance_usd, usd_rate, "
                    " rate_source, source) "
                    "VALUES (1, 11, ?, 0.001, 'BTC', ?, 65000.0, "
                    " 'coingecko', 'auto')",
                    (ts, val),
                )
        since = datetime(2026, 5, 13, 8, 30, tzinfo=timezone.utc)
        until = datetime(2026, 5, 13, 9, 30, tzinfo=timezone.utc)
        rows = portfolio_store.history(1, since, until)
        assert len(rows) == 1
        assert rows[0]["balance_usd"] == pytest.approx(110.0)


class TestManualAllowed:

    def test_no_prior_snapshot_allowed(self):
        allowed, next_at = portfolio_store.manual_allowed(1)
        assert allowed is True
        assert next_at is None

    def test_only_auto_rows_dont_count(self):
        # Spec: "1 manual snapshot per rolling hour" — auto-source
        # rows must not block the operator's manual button.
        _seed_account()
        portfolio_store.create_snapshot(
            user_id=1, exchange_account_id=11,
            balance_native=0.001, currency="BTC",
            balance_usd=65.0, usd_rate=65000.0,
            rate_source="coingecko", source="auto",
        )
        allowed, _ = portfolio_store.manual_allowed(1)
        assert allowed is True

    def test_recent_manual_blocks(self):
        _seed_account()
        portfolio_store.create_snapshot(
            user_id=1, exchange_account_id=11,
            balance_native=0.001, currency="BTC",
            balance_usd=65.0, usd_rate=65000.0,
            rate_source="coingecko", source="manual",
        )
        allowed, next_at = portfolio_store.manual_allowed(1)
        assert allowed is False
        assert next_at is not None
        # next_allowed_at sits within the next 60 minutes of "now".
        delta = next_at - datetime.now(timezone.utc)
        assert timedelta(minutes=0) <= delta <= timedelta(minutes=60)

    def test_manual_older_than_an_hour_allowed(self):
        _seed_account()
        # Insert an old manual row directly so we can backdate
        # captured_at past the rolling window.
        old_ts = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(user_id, exchange_account_id, captured_at, "
                " balance_native, currency, balance_usd, usd_rate, "
                " rate_source, source) "
                "VALUES (1, 11, ?, 0.001, 'BTC', 65.0, 65000.0, "
                " 'coingecko', 'manual')",
                (old_ts,),
            )
        allowed, next_at = portfolio_store.manual_allowed(1)
        assert allowed is True
        assert next_at is None

    def test_cross_user_isolation(self):
        # User 1 used their manual snapshot; user 2 should still be
        # allowed.
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO users (id, username, role) "
                "VALUES (2, 'bob', 'user')",
            )
        _seed_account(user_id=1, account_id=31)
        _seed_account(user_id=2, account_id=32)
        portfolio_store.create_snapshot(
            user_id=1, exchange_account_id=31,
            balance_native=0.001, currency="BTC",
            balance_usd=65.0, usd_rate=65000.0,
            rate_source="coingecko", source="manual",
        )
        assert portfolio_store.manual_allowed(1)[0] is False
        assert portfolio_store.manual_allowed(2)[0] is True
