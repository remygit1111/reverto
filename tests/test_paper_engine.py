"""Engine-level tests for paper/paper_engine.py.

Covers the module-level serialisation helpers (``_deal_to_dict`` /
``_dict_to_deal``) and a smoke-init path for the full PaperEngine
class with mocked exchange + notifier. Detailed trading behaviour
still lives in tests/test_trading_engine.py — this file only pins
the engine wiring that previously had no dedicated fixture.
"""

import sys
from datetime import datetime, UTC
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

from config.models import (  # noqa: E402
    BotConfig,
    DCAConfig,
    Exchange,
    Mode,
    TakeProfitConfig,
)
from paper.paper_engine import PaperEngine, _deal_to_dict, _dict_to_deal  # noqa: E402
from paper.paper_state import PaperDeal, PaperOrder  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def minimal_bot_config():
    """Minimum viable BotConfig — mirrors the indi_group_test YAML layout
    but strips everything the engine does not require for __init__."""
    return BotConfig(
        name="TestBot",
        mode=Mode.PAPER,
        exchange=Exchange.BITGET,
        pair="BTC/USD",
        dca=DCAConfig(
            enabled=True,
            base_order_size=0.001,
            max_orders=3,
            order_spacing_pct=1.5,
            multiplier=1.0,
        ),
        take_profit=TakeProfitConfig(enabled=True, target_pct=3.0),
    )


@pytest.fixture
def mock_exchange():
    """Exchange stub — returns stable ticker + 100 candles of identical data.

    Engine __init__ does not call the exchange (it only does that from
    _tick). Having the stub ready keeps the fixture usable for any
    later tick-level tests without having to re-mock."""
    mock = MagicMock()
    mock.get_ticker.return_value = MagicMock(mark_price=50000.0, last=50000.0)
    mock.get_ohlcv.return_value = [
        [1_000_000 + i * 60_000, 50000.0, 50100.0, 49900.0, 50050.0, 1.0]
        for i in range(100)
    ]
    return mock


@pytest.fixture
def mock_notifier():
    """TelegramNotifier stub — every notify_* method is a no-op MagicMock."""
    n = MagicMock()
    for m in [
        "notify_startup", "notify_shutdown", "notify_entry",
        "notify_dca", "notify_take_profit", "notify_stop_loss",
        "notify_error", "notify_stop", "notify_restart",
    ]:
        setattr(n, m, MagicMock())
    return n


@pytest.fixture
def engine(minimal_bot_config, mock_exchange, mock_notifier, tmp_path):
    """Fully wired engine with a tmp state file. The daemon notify
    thread is torn down via the sentinel on fixture teardown so we
    don't leak it between tests."""
    state_file = tmp_path / "bot.state.json"
    eng = PaperEngine(
        config=minimal_bot_config,
        exchange=mock_exchange,
        notifier=mock_notifier,
        initial_balance_btc=0.1,
        poll_interval=1,
        state_file=str(state_file),
        slug="testbot",
    )
    yield eng
    # Teardown — stop the notify worker without running the full stop().
    eng._notify_queue.put(None)
    eng._notify_thread.join(timeout=5)


# ── Module-level helpers: _deal_to_dict / _dict_to_deal ─────────────────────

class TestDealDictRoundtrip:
    """The state file persists deals as dicts; a round-trip must preserve
    every field the engine uses to resume trading after a restart."""

    def _sample_deal(self):
        orders = [
            PaperOrder(order_number=1, price=80_000.0, size=0.001,
                       timestamp=datetime(2026, 4, 1, tzinfo=UTC), order_type="base"),
            PaperOrder(order_number=2, price=79_500.0, size=0.0012,
                       timestamp=datetime(2026, 4, 1, 1, tzinfo=UTC), order_type="dca"),
        ]
        deal = PaperDeal(
            id="PAPER-0042", bot_name="testbot", symbol="BTC/USD",
            side="long", leverage=1, orders=orders,
        )
        deal._peak_price = 80_500.0
        deal.entry_trigger = {"group_id": 1, "group_name": "G1", "indicators": ["RSI"]}
        return deal

    def test_round_trip_preserves_core_fields(self):
        original = self._sample_deal()
        as_dict = _deal_to_dict(original, current_price=80_250.0)
        restored = _dict_to_deal(as_dict)

        assert restored.id == original.id
        assert restored.bot_name == original.bot_name
        assert restored.symbol == original.symbol
        assert restored.side == original.side
        assert restored.leverage == original.leverage
        assert len(restored.orders) == len(original.orders)
        assert restored.orders[0].order_type == "base"
        assert restored.orders[1].order_type == "dca"

    def test_round_trip_preserves_peak_price(self):
        """_peak_price drives trailing-stop logic — must survive restarts."""
        original = self._sample_deal()
        restored = _dict_to_deal(_deal_to_dict(original, current_price=81_000.0))
        assert restored._peak_price == 80_500.0

    def test_round_trip_preserves_entry_trigger(self):
        original = self._sample_deal()
        restored = _dict_to_deal(_deal_to_dict(original, current_price=80_000.0))
        assert restored.entry_trigger == {
            "group_id": 1, "group_name": "G1", "indicators": ["RSI"],
        }

    def test_closed_deal_uses_stored_pnl(self):
        """For closed deals _deal_to_dict must not recompute pnl against
        a stale current_price — the realised pnl is already stamped."""
        deal = self._sample_deal()
        deal.is_open = False
        deal.pnl_btc = 0.005
        deal.pnl_pct = 5.0
        out = _deal_to_dict(deal, current_price=99_999.0)
        assert out["pnl_btc"] == 0.005
        assert out["pnl_pct"] == 5.0


# ── PaperEngine init ────────────────────────────────────────────────────────

class TestPaperEngineInit:

    def test_engine_initialises_cleanly(self, engine, minimal_bot_config):
        """Smoke test: a fresh engine binds its slug, wires the state
        object, and starts the notify worker without raising."""
        assert engine.config is minimal_bot_config
        assert engine._bot_slug == "testbot"
        assert engine.state is not None
        assert engine.state.initial_balance_btc == 0.1
        assert engine._notify_thread.is_alive()

    def test_engine_slug_falls_back_to_state_file_stem(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path
    ):
        """Without an explicit slug the engine must derive one from the
        state-file stem — strip the '.state' suffix if present."""
        state_file = tmp_path / "some_bot.state.json"
        eng = PaperEngine(
            config=minimal_bot_config,
            exchange=mock_exchange,
            notifier=mock_notifier,
            state_file=str(state_file),
        )
        try:
            assert eng._bot_slug == "some_bot"
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_engine_resumes_from_state_file(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path
    ):
        """An existing state.json with a closed deal must rehydrate into
        state.closed_deals so historical stats survive a restart."""
        import json

        state_file = tmp_path / "bot.state.json"
        state_file.write_text(json.dumps({
            "bot_name": "testbot",
            "balance_btc": 0.1,
            "initial_balance_btc": 0.1,
            "fees_paid_btc": 0.0,
            "_deal_counter": 1,
            "open_deals": [],
            "closed_deals": [{
                "id": "PAPER-0001",
                "bot_name": "testbot",
                "symbol": "BTC/USD",
                "side": "long",
                "leverage": 1,
                "is_open": False,
                "opened_at": "2026-04-01T00:00:00+00:00",
                "closed_at": "2026-04-01T01:00:00+00:00",
                "close_price": 80_500.0,
                "close_reason": "tp",
                "pnl_btc": 0.0001,
                "pnl_pct": 1.0,
                "orders": [{
                    "order_number": 1, "price": 80_000.0, "size": 0.001,
                    "timestamp": "2026-04-01T00:00:00+00:00", "order_type": "base",
                }],
            }],
        }))

        eng = PaperEngine(
            config=minimal_bot_config,
            exchange=mock_exchange,
            notifier=mock_notifier,
            state_file=str(state_file),
            slug="testbot",
        )
        try:
            closed = eng.state.get_closed_deals_snapshot()
            assert len(closed) == 1
            assert closed[0].id == "PAPER-0001"
            assert closed[0].close_reason == "tp"
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)


# ── Balance guard + side + DCA caps (v20 P0 fixes) ──────────────────────────

class TestBalanceGuard:
    """The `_deduct_balance` helper refuses to let balance_btc go
    negative; on paper the effect is cosmetic (balance just doesn't
    go down), but for live trading this becomes the insufficient-funds
    gate that blocks a crash-cascade."""

    def test_deduct_with_sufficient_balance(self, engine):
        engine.state.balance_btc = 0.001
        assert engine._deduct_balance(0.0005, "test") is True
        assert engine.state.balance_btc == pytest.approx(0.0005)

    def test_deduct_rejects_when_insufficient(self, engine):
        engine.state.balance_btc = 0.0001
        assert engine._deduct_balance(0.001, "big-fee") is False
        # Balance unchanged on refusal — no partial deducts.
        assert engine.state.balance_btc == pytest.approx(0.0001)
        # Notifier was asked to surface the error.
        # The notify call is queued async; give the daemon a moment.
        engine._notify_queue.join()
        engine.notifier.notify_error.assert_called()


class TestSideFromDirection:
    """_open_deal used to hardcode side='long'. Now it honours
    BotConfig.direction so short-bots actually open short positions."""

    def test_long_direction_opens_long(self, engine):
        engine.config.direction = "long"
        engine._open_deal(50_000.0)
        deals = engine.state.get_open_deals_snapshot()
        assert len(deals) == 1
        assert list(deals.values())[0].side == "long"

    def test_short_direction_opens_short(self, engine):
        engine.config.direction = "short"
        engine._open_deal(50_000.0)
        deals = engine.state.get_open_deals_snapshot()
        assert len(deals) == 1
        assert list(deals.values())[0].side == "short"


class TestDrawdownGuardPersistedToState:
    """v20 HIGH-LIVE fix: guard state must survive engine restart via
    state.json. Simulate a run that triggers the guard, dump state,
    rehydrate a fresh engine, confirm the guard is still triggered."""

    def test_drawdown_persisted_across_restart(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        from core.drawdown_guard import DrawdownGuardConfig

        state_file = tmp_path / "dd.state.json"
        minimal_bot_config.drawdown_guard = DrawdownGuardConfig(
            enabled=True, max_drawdown_pct=5.0, metric="balance",
        )

        eng1 = PaperEngine(
            config=minimal_bot_config, exchange=mock_exchange,
            notifier=mock_notifier, initial_balance_btc=0.1,
            state_file=str(state_file), slug="ddbot",
        )
        try:
            # Drive the guard to triggered via balance-metric updates.
            eng1.drawdown_guard.update(0.1)   # peak
            eng1.drawdown_guard.update(0.09)  # >10% from 0.1 ... actually 10%, trigger
            assert eng1.drawdown_guard.is_triggered
            eng1._paused_by_drawdown = True
            eng1._write_state(50_000.0, is_open=True)
        finally:
            eng1._notify_queue.put(None)
            eng1._notify_thread.join(timeout=5)

        eng2 = PaperEngine(
            config=minimal_bot_config, exchange=mock_exchange,
            notifier=mock_notifier, initial_balance_btc=0.1,
            state_file=str(state_file), slug="ddbot",
        )
        try:
            assert eng2.drawdown_guard.is_triggered is True
            assert eng2.drawdown_guard.peak_value == pytest.approx(0.1)
            assert eng2._paused_by_drawdown is True
        finally:
            eng2._notify_queue.put(None)
            eng2._notify_thread.join(timeout=5)


# ── Indicator log line filtering ────────────────────────────────────────────


class TestIndicatorLogFiltering:
    """The per-tick "Indicators —" line must mirror the bot's configured
    indicators. Regression: a RSI-only bot used to see EMA9/EMA21/MACD
    values in every log line even though those weren't part of the
    entry strategy, which made logs noisy and misleading."""

    def _attach_indicator_types(self, config, types):
        """Plant one entry.indicator_groups group with the requested
        types. All IndicatorConfig fields besides ``type`` are Optional,
        so we can construct the minimum viable stub without wiring
        per-indicator defaults."""
        from config.models import IndicatorConfig, IndicatorGroup
        config.entry.indicator_groups = [
            IndicatorGroup(
                id=1, name="t",
                indicators=[IndicatorConfig(type=t) for t in types],
            )
        ]

    def _snapshot(self):
        """Full snapshot as get_indicator_snapshot would produce it.
        Individual tests drive the filter by swapping the config, not
        by clipping the snapshot, so the snapshot stays complete."""
        return {
            "rsi_14": 35.17,
            "ema_9": 76072.8,
            "ema_21": 76340.94,
            "macd": 12.5,
            "macd_signal": 170.5,
            "macd_histogram": -157.9944,
        }

    def test_rsi_only_suppresses_ema_and_macd(
        self, engine, minimal_bot_config,
    ):
        self._attach_indicator_types(minimal_bot_config, ["RSI"])
        engine._active_indicator_types = {"RSI"}
        engine._last_snapshot = self._snapshot()

        line = engine._format_indicator_log()
        assert line is not None
        assert line == "Indicators — RSI: 35.17"
        assert "EMA" not in line
        assert "MACD" not in line

    def test_rsi_and_macd_shows_both(self, engine, minimal_bot_config):
        self._attach_indicator_types(minimal_bot_config, ["RSI", "MACD"])
        engine._active_indicator_types = {"RSI", "MACD"}
        engine._last_snapshot = self._snapshot()

        line = engine._format_indicator_log()
        assert line is not None
        assert "RSI: 35.17" in line
        assert "MACD hist: -157.9944" in line
        assert "EMA" not in line

    def test_no_configured_indicators_skips_line(self, engine):
        """Bot without any configured indicators (ASAP-only or empty
        entry) — the log line is suppressed entirely rather than
        printing an "Indicators —" with nothing after it."""
        engine._active_indicator_types = set()
        engine._last_snapshot = self._snapshot()
        assert engine._format_indicator_log() is None

    def test_unknown_only_indicators_skips_line(self, engine):
        """Bot configured exclusively with indicators whose values the
        snapshot doesn't carry (e.g. BOLLINGER, PARABOLIC_SAR). The
        filter MUST NOT fall back to printing EMA/MACD/RSI just because
        those happen to be in the snapshot."""
        engine._active_indicator_types = {"BOLLINGER", "PARABOLIC_SAR"}
        engine._last_snapshot = self._snapshot()
        assert engine._format_indicator_log() is None

    def test_empty_snapshot_skips_line(self, engine):
        """First ticks before enough candle history has been fetched —
        snapshot is an empty dict. Better to stay silent than to log
        placeholder '?' values for every configured indicator."""
        engine._active_indicator_types = {"RSI", "MACD"}
        engine._last_snapshot = {}
        assert engine._format_indicator_log() is None

    def test_collect_types_picks_up_entry_and_tp_groups(
        self, minimal_bot_config,
    ):
        """The collector must merge entry + TP indicator groups so a bot
        that uses MACD purely as a TP confirmation still gets MACD hist
        in its log line."""
        from config.models import IndicatorConfig, IndicatorGroup
        from paper.paper_engine import _collect_active_indicator_types

        minimal_bot_config.entry.indicator_groups = [
            IndicatorGroup(id=1, indicators=[IndicatorConfig(type="RSI")])
        ]
        minimal_bot_config.take_profit.indicator_groups = [
            IndicatorGroup(id=1, indicators=[IndicatorConfig(type="macd")])
        ]
        types = _collect_active_indicator_types(minimal_bot_config)
        assert types == {"RSI", "MACD"}

    def test_collect_types_reads_legacy_flat_indicators(
        self, minimal_bot_config,
    ):
        """Older configs still use the flat entry.indicators list
        instead of indicator_groups — the collector must honour both."""
        from config.models import IndicatorConfig
        from paper.paper_engine import _collect_active_indicator_types

        minimal_bot_config.entry.indicators = [IndicatorConfig(type="RSI")]
        minimal_bot_config.entry.indicator_groups = []
        types = _collect_active_indicator_types(minimal_bot_config)
        assert "RSI" in types
