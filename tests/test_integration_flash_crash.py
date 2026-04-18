"""Flash-crash / DCA cascade integration tests.

These drive a real PaperEngine through a crash scenario and assert
the v20 cascade-guards actually kick in: MAX_DCA_PER_TICK keeps only
one DCA from firing per tick, and the cumulative-notional cap blocks
DCA #N once the summed position exceeds the threshold.
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from config.models import (  # noqa: E402
    BotConfig,
    DCAConfig,
    Exchange,
    Mode,
    StopLossConfig,
    TakeProfitConfig,
)
from paper.paper_engine import PaperEngine  # noqa: E402


@pytest.fixture
def crashy_config():
    """Config tuned for a cascade: tight spacing + 10 DCA levels so a
    flash crash would walk through every level if the per-tick cap
    weren't active."""
    return BotConfig(
        name="CrashBot",
        mode=Mode.PAPER,
        exchange=Exchange.BITGET,
        pair="BTC/USD",
        dca=DCAConfig(
            enabled=True,
            base_order_size=0.001,
            max_orders=10,
            order_spacing_pct=1.0,   # 1% per step
            multiplier=1.2,           # gentle geometric growth
            step_scale=1.0,
        ),
        take_profit=TakeProfitConfig(enabled=True, target_pct=3.0),
        # Disable SL so the flash-crash drop doesn't close the deal
        # before we can observe the DCA cap.
        stop_loss=StopLossConfig(type="none", pct=5.0),
    )


@pytest.fixture
def engine(crashy_config, tmp_path):
    notifier = MagicMock()
    for m in [
        "notify_startup", "notify_shutdown", "notify_entry",
        "notify_dca", "notify_take_profit", "notify_stop_loss",
        "notify_error", "notify_stop", "notify_restart",
    ]:
        setattr(notifier, m, MagicMock())
    exchange = MagicMock()
    eng = PaperEngine(
        config=crashy_config,
        exchange=exchange,
        notifier=notifier,
        initial_balance_btc=0.1,
        poll_interval=1,
        state_file=str(tmp_path / "crash.state.json"),
        slug="crashbot",
    )
    yield eng
    eng._notify_queue.put(None)
    eng._notify_thread.join(timeout=5)


class TestDcaCascadeCap:

    def test_per_tick_cap_blocks_multiple_dca(self, engine):
        """Open a deal, simulate a flash-crash price drop of 30%, and
        verify _monitor_open_deals only fires ONE DCA even though
        price is below every DCA-spacing level."""
        engine._open_deal(60_000.0)
        deal = next(iter(engine.state.get_open_deals_snapshot().values()))
        dca_before = deal.dca_count

        # Crash to 42_000 — 30% down. With 1% spacing, every DCA level
        # 1..10 would be "triggered" if not capped.
        engine._monitor_open_deals(42_000.0)

        dca_after = deal.dca_count
        added = dca_after - dca_before
        assert added == 1, f"expected 1 DCA per tick, got {added}"

    def test_multiple_ticks_gradually_add_dca(self, engine):
        """Successive ticks during a sustained crash add one DCA each,
        so the operator can still see the cascade progress in state
        but never gets a single-tick avalanche. DCA spacing is relative
        to the last order, so we need a progressively lower price to
        trip each level."""
        engine._open_deal(60_000.0)
        deal = next(iter(engine.state.get_open_deals_snapshot().values()))

        # Spacing = 1% per level. Drop another 1% (of the previous
        # order's price) each tick.
        engine._monitor_open_deals(59_000.0)   # DCA #1 (base 60k × -1%)
        assert deal.dca_count == 1
        engine._monitor_open_deals(58_000.0)   # DCA #2 (DCA #1 × -1%)
        assert deal.dca_count == 2
        engine._monitor_open_deals(57_000.0)   # DCA #3
        assert deal.dca_count == 3


class TestCumulativeNotionalCap:

    def test_cap_blocks_dca_once_cumulative_exceeds(self, crashy_config, tmp_path):
        """With a deliberately tight max_cumulative_size, DCA stops
        adding orders once `sum(order.size) + next_dca >` cap."""
        crashy_config.dca.max_cumulative_size = 0.003  # 3× base only
        crashy_config.dca.multiplier = 1.0
        crashy_config.dca.max_orders = 10

        notifier = MagicMock()
        for m in [
            "notify_startup", "notify_entry", "notify_dca",
            "notify_take_profit", "notify_stop_loss", "notify_error",
            "notify_stop", "notify_shutdown", "notify_restart",
        ]:
            setattr(notifier, m, MagicMock())
        exchange = MagicMock()

        eng = PaperEngine(
            config=crashy_config,
            exchange=exchange, notifier=notifier,
            initial_balance_btc=0.1,
            state_file=str(tmp_path / "cap.state.json"),
            slug="capbot",
        )
        try:
            eng._open_deal(60_000.0)
            deal = next(iter(eng.state.get_open_deals_snapshot().values()))

            # Drive 5 ticks — enough to hit the 3× cap twice over.
            for _ in range(5):
                eng._monitor_open_deals(40_000.0)

            total_size = sum(o.size for o in deal.orders)
            # Base + ≤2 DCA orders = 0.003; the 3rd DCA would push it
            # above the cap and must be refused.
            assert total_size <= 0.003 + 1e-9
            assert deal.dca_count <= 2
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)
