"""SIGKILL / corrupt-state recovery regression tests.

The engine's _load_state sweeps orphan ``*.state.*.tmp`` files before
touching the main state file so a SIGKILL mid-write can't leave the
directory littered with dead tmp files or confuse the portal. These
tests pin that contract.
"""

import json
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from config.models import (  # noqa: E402
    BotConfig,
    DCAConfig,
    Mode,
    TakeProfitConfig,
)
from paper.paper_engine import PaperEngine  # noqa: E402


@pytest.fixture
def min_config():
    return BotConfig(
        name="RecoveryBot",
        mode=Mode.PAPER,
        exchange_account_id=1,
        pair="BTC/USD",
        dca=DCAConfig(
            enabled=True, base_order_size=0.001,
            max_orders=3, order_spacing_pct=2.5, multiplier=1.0,
        ),
        take_profit=TakeProfitConfig(enabled=True, target_pct=3.0),
    )


def _mock_notifier():
    n = MagicMock()
    for m in [
        "notify_startup", "notify_shutdown", "notify_entry",
        "notify_dca", "notify_take_profit", "notify_stop_loss",
        "notify_error", "notify_stop", "notify_restart",
    ]:
        setattr(n, m, MagicMock())
    return n


class TestOrphanTmpCleanup:

    def test_sigkill_orphan_tmp_is_removed(self, min_config, tmp_path):
        """SIGKILL during state write leaves <state>.tmp behind.
        A fresh engine must sweep it on startup."""
        state_file = tmp_path / "bot.state.json"
        good_state = {
            "bot_name": "RecoveryBot",
            "balance_btc": 0.123,
            "initial_balance_btc": 0.1,
            "fees_paid_btc": 0.0,
            "open_deals": [],
            "closed_deals": [],
        }
        state_file.write_text(json.dumps(good_state))

        # Orphan tmp — partial JSON from a crashed write.
        orphan = tmp_path / "bot.state.json.tmp"
        orphan.write_text('{"balance_btc": 0.999, PARTIAL')

        exchange = MagicMock()
        notifier = _mock_notifier()
        eng = PaperEngine(
            config=min_config, exchange=exchange, notifier=notifier,
            state_file=str(state_file), slug="recovbot",
        )
        try:
            assert not orphan.exists(), "orphan tmp should be swept"
            assert eng.state.balance_btc == pytest.approx(0.123)
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_corrupt_state_starts_clean_with_warning(
        self, min_config, tmp_path, caplog,
    ):
        """A state file that can't be parsed must not crash the engine
        — it boots with a clean PaperState and logs a warning."""
        state_file = tmp_path / "bot.state.json"
        state_file.write_text('{"utterly": "broken"')  # unterminated JSON

        exchange = MagicMock()
        notifier = _mock_notifier()

        with caplog.at_level("WARNING"):
            eng = PaperEngine(
                config=min_config, exchange=exchange, notifier=notifier,
                state_file=str(state_file), slug="recovbot",
            )
        try:
            # Fell back to clean default balance, not the broken file.
            assert eng.state.balance_btc == pytest.approx(0.1)
            assert any(
                "could not be parsed" in r.message for r in caplog.records
            )
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_non_state_tmp_files_are_preserved(self, min_config, tmp_path):
        """The sweep must only touch our own state siblings. Other
        modules (credentials, deal_store) write their own .tmp files
        into sibling dirs — we must not nuke those."""
        state_file = tmp_path / "bot.state.json"
        state_file.write_text("{}")

        # A .tmp file that is NOT ours.
        foreign = tmp_path / "credentials.json.tmp"
        foreign.write_text("foreign data")

        exchange = MagicMock()
        notifier = _mock_notifier()
        eng = PaperEngine(
            config=min_config, exchange=exchange, notifier=notifier,
            state_file=str(state_file), slug="recovbot",
        )
        try:
            assert foreign.exists(), "foreign .tmp files must be preserved"
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)
