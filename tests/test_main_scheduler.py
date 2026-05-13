"""Tests for main_scheduler.run_tick + signal/shutdown surface.

The scheduler is a long-running process in production, but
``run_tick`` is exposed at module scope so the test suite can drive
exactly one iteration deterministically. The authenticated exchange
client + price feed are monkey-patched so no test ever does I/O.
"""

from __future__ import annotations

import os
import signal as _signal
import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import main_scheduler  # noqa: E402
from core import user_store  # noqa: E402
from core.database import get_db  # noqa: E402


def _seed_account(
    user_id: int, account_id: int, alias: str = "main",
) -> int:
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


def _stub_creds():
    return {"api_key": "k", "api_secret": "s", "passphrase": "p"}


def _stub_client(balance: float = 0.001, currency: str = "BTC"):
    client = MagicMock()
    client.get_balance.return_value = balance
    client.balance_currency = currency
    return client


class TestRunTick:

    def test_no_users_no_rows(self, monkeypatch):
        # Pristine DB: user "admin" exists from init_db() seed but
        # has no exchange accounts → no snapshot attempts.
        succ, attempted = main_scheduler.run_tick()
        assert succ == 0
        assert attempted == 0

    def test_one_account_happy_path(self, monkeypatch):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11)
        monkeypatch.setattr(
            main_scheduler.exchange_account_store,
            "get_account_credentials",
            lambda acct_id: _stub_creds(),
        )
        monkeypatch.setattr(
            main_scheduler, "build_authenticated_exchange",
            lambda *a, **k: _stub_client(balance=0.001, currency="BTC"),
        )
        monkeypatch.setattr(
            main_scheduler.price_feed, "get_usd_rate",
            lambda c: (65000.0, "test"),
        )
        succ, attempted = main_scheduler.run_tick()
        assert succ == 1
        assert attempted == 1
        # Row landed with the expected USD conversion.
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["balance_native"] == pytest.approx(0.001)
        assert rows[0]["balance_usd"] == pytest.approx(65.0)
        assert rows[0]["rate_source"] == "test"
        assert rows[0]["source"] == "auto"

    def test_one_failing_account_does_not_kill_loop(self, monkeypatch):
        # Two accounts — first fails on client construction, second
        # succeeds. Expect 1/2 successes and no exception bubbling up.
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11, alias="bad")
        _seed_account(admin.id, 12, alias="good")

        def _build(exchange_type, market_type, creds):
            # Identify "bad" via the side-effect of the get_balance
            # mock; we use a separate flag here.
            if creds.get("which") == "bad":
                raise RuntimeError("creds rejected")
            return _stub_client()

        def _creds_for(acct_id):
            return {"which": "bad" if acct_id == 11 else "good",
                    "api_key": "k", "api_secret": "s", "passphrase": "p"}

        monkeypatch.setattr(
            main_scheduler.exchange_account_store,
            "get_account_credentials",
            _creds_for,
        )
        monkeypatch.setattr(
            main_scheduler, "build_authenticated_exchange", _build,
        )
        monkeypatch.setattr(
            main_scheduler.price_feed, "get_usd_rate",
            lambda c: (65000.0, "test"),
        )
        succ, attempted = main_scheduler.run_tick()
        assert succ == 1
        assert attempted == 2

    def test_pricefeed_error_skips_account(self, monkeypatch):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11)
        monkeypatch.setattr(
            main_scheduler.exchange_account_store,
            "get_account_credentials",
            lambda acct_id: _stub_creds(),
        )
        monkeypatch.setattr(
            main_scheduler, "build_authenticated_exchange",
            lambda *a, **k: _stub_client(),
        )

        def _raise(_c):
            raise main_scheduler.price_feed.PriceFeedError("both down")

        monkeypatch.setattr(
            main_scheduler.price_feed, "get_usd_rate", _raise,
        )
        succ, attempted = main_scheduler.run_tick()
        assert succ == 0
        assert attempted == 1
        # No row landed in the DB.
        conn = get_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM portfolio_snapshots",
        ).fetchone()
        assert row[0] == 0

    def test_source_passthrough(self, monkeypatch):
        admin = user_store.get_user_by_username("admin")
        _seed_account(admin.id, 11)
        monkeypatch.setattr(
            main_scheduler.exchange_account_store,
            "get_account_credentials",
            lambda acct_id: _stub_creds(),
        )
        monkeypatch.setattr(
            main_scheduler, "build_authenticated_exchange",
            lambda *a, **k: _stub_client(),
        )
        monkeypatch.setattr(
            main_scheduler.price_feed, "get_usd_rate",
            lambda c: (65000.0, "test"),
        )
        main_scheduler.run_tick(source="manual")
        conn = get_db()
        row = conn.execute(
            "SELECT source FROM portfolio_snapshots LIMIT 1",
        ).fetchone()
        assert row["source"] == "manual"


class TestShutdownFlag:

    def test_sigterm_handler_flips_flag(self, monkeypatch):
        # Reset module-level state before each call.
        monkeypatch.setattr(main_scheduler, "_shutdown_requested", False)
        # The handler doesn't return — it just sets the flag.
        main_scheduler._on_sigterm(_signal.SIGTERM, None)
        assert main_scheduler._shutdown_requested is True


class TestNextHourMath:

    def test_seconds_until_next_hour_at_top_of_hour(self):
        # At HH:00:00 exactly the next hour is 60 minutes away.
        t = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
        sec = main_scheduler._seconds_until_next_hour(t)
        assert sec == pytest.approx(3600.0, abs=0.001)

    def test_seconds_until_next_hour_mid_hour(self):
        # At HH:30:00 the next hour is 30 minutes away.
        t = datetime(2026, 5, 13, 12, 30, 0, tzinfo=timezone.utc)
        sec = main_scheduler._seconds_until_next_hour(t)
        assert sec == pytest.approx(1800.0, abs=0.001)

    def test_seconds_until_next_hour_always_positive(self):
        # Exactly at a top-of-hour with microseconds — the loop must
        # not return 0 (or it would tick immediately twice).
        t = datetime(2026, 5, 13, 12, 0, 0, 999999, tzinfo=timezone.utc)
        sec = main_scheduler._seconds_until_next_hour(t)
        assert sec >= 1.0
