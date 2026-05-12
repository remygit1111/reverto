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
        exchange_account_id=1,
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
        "notify_error", "notify_error_persistent",
        "notify_stop", "notify_restart",
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
        exchange_type="bitget",
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
            "bb_pct_b": 0.42,
            "psar": 51200.00,
            "psar_trend": "bull",
            "supertrend": 52100.50,
            "supertrend_dir": "up",
            "sr_support": 51000.0,
            "sr_resistance": 52500.0,
            "qfl_base": 51500.00,
            "market_structure": "HH",
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
        """Bot configured exclusively with an unknown / unsupported
        indicator type. The filter must NOT fall back to printing
        RSI/EMA/MACD just because those happen to be in the snapshot.

        Historically this used BOLLINGER/PARABOLIC_SAR as the "unknown"
        examples; those are now first-class snapshot values so a made-
        up type name is used instead."""
        engine._active_indicator_types = {"NOT_A_REAL_INDICATOR"}
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

    # ── Per-indicator formatting ────────────────────────────────────

    def test_bollinger_only_logs_pct_b(self, engine):
        engine._active_indicator_types = {"BOLLINGER"}
        engine._last_snapshot = self._snapshot()
        line = engine._format_indicator_log()
        assert line == "Indicators — BB %B: 0.42"

    def test_psar_only_logs_value_and_trend(self, engine):
        engine._active_indicator_types = {"PARABOLIC_SAR"}
        engine._last_snapshot = self._snapshot()
        line = engine._format_indicator_log()
        assert line == "Indicators — PSAR: 51200.00 (bull)"

    def test_supertrend_only_logs_value_and_direction(self, engine):
        engine._active_indicator_types = {"SUPERTREND"}
        engine._last_snapshot = self._snapshot()
        line = engine._format_indicator_log()
        assert line == "Indicators — ST: 52100.50 (up)"

    def test_sr_only_logs_support_and_resistance(self, engine):
        engine._active_indicator_types = {"SUPPORT_RESISTANCE"}
        engine._last_snapshot = self._snapshot()
        line = engine._format_indicator_log()
        assert line == "Indicators — S&R: S@51000 R@52500"

    def test_sr_one_sided_renders_placeholder(self, engine):
        """Only one of support/resistance is active — the other side
        gets an em-dash placeholder rather than being dropped."""
        engine._active_indicator_types = {"SUPPORT_RESISTANCE"}
        snap = self._snapshot()
        snap.pop("sr_resistance")
        engine._last_snapshot = snap
        line = engine._format_indicator_log()
        assert line == "Indicators — S&R: S@51000 R@—"

    def test_qfl_only_logs_base(self, engine):
        engine._active_indicator_types = {"QFL"}
        engine._last_snapshot = self._snapshot()
        line = engine._format_indicator_log()
        assert line == "Indicators — QFL base: 51500.00"

    def test_market_structure_only_logs_pattern(self, engine):
        engine._active_indicator_types = {"MARKET_STRUCTURE"}
        engine._last_snapshot = self._snapshot()
        line = engine._format_indicator_log()
        assert line == "Indicators — MS: HH"

    def test_combined_indicators_log_all(self, engine):
        """RSI + BB + PSAR → log shows all three in declaration order."""
        engine._active_indicator_types = {"RSI", "BOLLINGER", "PARABOLIC_SAR"}
        engine._last_snapshot = self._snapshot()
        line = engine._format_indicator_log()
        assert line == (
            "Indicators — RSI: 35.17 | BB %B: 0.42 "
            "| PSAR: 51200.00 (bull)"
        )


# ── Graceful shutdown timing contract ───────────────────────────────────────


class TestGracefulShutdownTiming:
    """Portal's stop_bot must wait at least NOTIFY_DRAIN_TIMEOUT_S
    before SIGKILL, else the engine's notify-worker drain (which
    blocks stop() returning until Telegram messages flush or the
    budget expires) gets cut off mid-send.

    Audit finding (2026-04-19 portal.log): 12/12 bot stops over
    24h escalated to SIGKILL because the portal-wait was hardcoded
    to 10s while NOTIFY_DRAIN_TIMEOUT_S was 15s. A previous commit
    bumped the portal-wait from 5s to 10s with the motivation
    "15s notify-drain budget" but forgot to finish the arithmetic.
    The fix couples the two constants via import; this test pins
    the coupling so an accidental uncoupling trips CI before the
    WARNING storm returns to portal.log.
    """

    def test_portal_wait_strictly_exceeds_notify_drain_budget(self):
        """The portal's total wait (drain-budget + margin) must be
        STRICTLY greater than the engine's drain-budget alone, so
        the engine always gets at least its full drain period plus
        teardown overhead before SIGKILL."""
        from paper.paper_engine import NOTIFY_DRAIN_TIMEOUT_S
        from web.app import _STOP_SAFETY_MARGIN_S

        assert _STOP_SAFETY_MARGIN_S > 0, (
            f"Safety margin must be > 0 so portal-wait "
            f"(drain + margin) strictly exceeds drain alone. "
            f"Got {_STOP_SAFETY_MARGIN_S}"
        )
        # Redundant with the >0 check given today's formula, but
        # explicit about the invariant the formula is meant to
        # satisfy: if someone later changes the formula, this
        # assertion is what catches it.
        portal_wait = NOTIFY_DRAIN_TIMEOUT_S + _STOP_SAFETY_MARGIN_S
        assert portal_wait > NOTIFY_DRAIN_TIMEOUT_S

    def test_constants_are_importable_from_expected_modules(self):
        """The fix depends on the two modules being able to see
        each other's constant. If paper.paper_engine ever loses
        NOTIFY_DRAIN_TIMEOUT_S from its public surface, or
        web.app stops importing it, stop_bot quietly regresses
        to a hardcoded value."""
        import paper.paper_engine as paper_engine
        import web.app as webapp
        assert hasattr(paper_engine, "NOTIFY_DRAIN_TIMEOUT_S")
        assert hasattr(webapp, "NOTIFY_DRAIN_TIMEOUT_S"), (
            "web.app must re-import NOTIFY_DRAIN_TIMEOUT_S so "
            "stop_bot's deadline stays coupled to the engine budget"
        )
        assert hasattr(webapp, "_STOP_SAFETY_MARGIN_S")


class TestDealCreationContract:
    """Audit v25 Finding #13. The contract between
    ``_db_create_deal_with_retry`` and ``_open_deal`` is subtle:

      * On IntegrityError the retry MUTATES ``deal.id`` in place.
        ``_open_deal`` holds a reference to the same deal object and
        later calls ``self.state.open_deal(deal)`` — that call must
        observe the rewritten id, not the original.
      * The helper only returns True after a successful DB persist.
        Only on True may the caller mutate ``self.state``; on False
        the open is refused entirely so in-memory state never diverges
        from the DB.

    The tests below pin both halves so a future refactor that reorders
    state-mutation vs. DB-persist, or that drops the in-place deal.id
    mutation for a copy, fails loudly.
    """

    def _make_deal(self) -> PaperDeal:
        """Fresh deal shaped like what ``_open_deal`` constructs."""
        return PaperDeal(
            id="202604201400-0001",
            bot_name="TestBot",
            symbol="BTC/USD",
            side="long",
            leverage=1,
            orders=[
                PaperOrder(
                    order_number=1,
                    price=50_000.0,
                    size=0.001,
                    timestamp=datetime.now(UTC),
                    order_type="base",
                ),
            ],
        )

    def test_successful_create_keeps_deal_id_stable(
        self, engine, monkeypatch,
    ):
        """First-try success → no id mutation, helper returns True."""
        import core.deal_store as _ds
        calls = {"n": 0}

        def _create_ok(*args, **kwargs):
            calls["n"] += 1
            return None  # deal_store.create_deal returns None on success

        monkeypatch.setattr(_ds, "create_deal", _create_ok)

        deal = self._make_deal()
        original_id = deal.id
        ok = engine._db_create_deal_with_retry(deal)
        assert ok is True
        assert deal.id == original_id
        assert calls["n"] == 1

    def test_collision_retry_mutates_deal_id_in_place(
        self, engine, monkeypatch,
    ):
        """IntegrityError on the first attempt must rewrite ``deal.id``
        so the subsequent ``_open_deal`` state mutation references the
        retry id. Without the in-place assignment the engine would
        happily persist the deal under NEW_ID but track it in memory
        under OLD_ID — every later DCA/TP/SL write would fail to find
        its DB row."""
        import sqlite3 as _sqlite3
        import core.deal_store as _ds

        calls = {"n": 0}

        def _create_collide_once(deal, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _sqlite3.IntegrityError("UNIQUE constraint failed: deals.id")
            return None  # second attempt succeeds

        monkeypatch.setattr(_ds, "create_deal", _create_collide_once)

        deal = self._make_deal()
        original_id = deal.id
        ok = engine._db_create_deal_with_retry(deal)
        assert ok is True
        assert calls["n"] == 2, "expected exactly one retry"
        assert deal.id != original_id, (
            "deal.id must be rewritten in place on IntegrityError so the "
            "caller's state.open_deal(deal) records the retry id"
        )

    def test_exhausted_retries_returns_false_so_caller_skips_state_mutation(
        self, engine, monkeypatch,
    ):
        """All 3 attempts collide → False return → _open_deal MUST NOT
        add the deal to state.open_deals. We exercise the full
        _open_deal path and assert that state stays empty on exhaustion,
        documenting the "refuse the open entirely" contract.
        """
        import sqlite3 as _sqlite3
        import core.deal_store as _ds

        def _always_collide(*args, **kwargs):
            raise _sqlite3.IntegrityError("UNIQUE constraint failed: deals.id")

        monkeypatch.setattr(_ds, "create_deal", _always_collide)

        # Directly test the helper too so the contract is pinned at the
        # function boundary, not just indirectly through _open_deal.
        deal = self._make_deal()
        assert engine._db_create_deal_with_retry(deal, max_attempts=3) is False

        # And confirm _open_deal honours the False by skipping
        # state.open_deal. Pre-call state should be empty.
        assert engine.state.get_open_deals_snapshot() == {}
        engine._open_deal(50_000.0)
        assert engine.state.get_open_deals_snapshot() == {}, (
            "refused DB create must not leave an in-memory phantom deal"
        )


# ── Tick-level structured error logging ─────────────────────────────────────


class _FakeRateLimit(Exception):
    """Stand-in for ccxt.RateLimitExceeded — class name matches the ccxt
    MRO entry that paper.errors treats as transient, so classify_exception
    resolves it to status=429 / is_transient=True."""


_FakeRateLimit.__name__ = "RateLimitExceeded"


class _FakeAuthError(Exception):
    pass


_FakeAuthError.__name__ = "AuthenticationError"


class TestTickFailureStructuredLogging:
    """A raising exchange must produce a single-line structured log with
    bot/exchange/endpoint/symbol/status/class/retry/transient/message
    fields. Operators grep portal.log for these keys when a bot goes
    quiet."""

    def test_transient_failure_logs_structured_fields(self, engine, caplog):
        engine.exchange.get_ticker.side_effect = _FakeRateLimit(
            "bitget 429 Too Many Requests"
        )
        with caplog.at_level("ERROR", logger="paper.paper_engine"):
            engine._tick()

        matches = [r for r in caplog.records if "Tick failure" in r.message]
        assert len(matches) == 1, f"expected 1 tick-failure log, got {matches}"
        line = matches[0].getMessage()
        assert "bot=testbot" in line
        assert "exchange=bitget" in line
        assert "endpoint=tick" in line
        assert "symbol=BTC/USD" in line
        assert "status=429" in line
        assert "class=RateLimitExceeded" in line
        assert "retry=1/5" in line
        assert "transient=yes" in line

    def test_persistent_failure_renders_transient_no(self, engine, caplog):
        """An AuthenticationError must classify as non-transient. The
        structured log must show transient=no so operators can split
        auth/config bugs from rate-limit noise with a single grep."""
        engine.exchange.get_ticker.side_effect = _FakeAuthError(
            "invalid API key"
        )
        with caplog.at_level("ERROR", logger="paper.paper_engine"):
            engine._tick()

        matches = [r for r in caplog.records if "Tick failure" in r.message]
        assert len(matches) == 1
        line = matches[0].getMessage()
        assert "class=AuthenticationError" in line
        assert "status=401" in line
        assert "transient=no" in line

    def test_consecutive_errors_increment_retry_counter(self, engine, caplog):
        """The retry=N/5 field tracks the current streak. Two ticks that
        both fail must render retry=1/5 then retry=2/5 — operators use
        this to distinguish a single flake from a sustained outage."""
        engine.exchange.get_ticker.side_effect = _FakeRateLimit("429")
        with caplog.at_level("ERROR", logger="paper.paper_engine"):
            engine._tick()
            engine._tick()

        lines = [
            r.getMessage() for r in caplog.records
            if "Tick failure" in r.message
        ]
        assert len(lines) == 2
        assert "retry=1/5" in lines[0]
        assert "retry=2/5" in lines[1]

    def test_consecutive_counter_resets_on_successful_tick(
        self, engine, caplog,
    ):
        """After a recovery tick the retry counter restarts at 1 on the
        next failure — without this, a long-running bot would accumulate
        a misleading retry=N even for unrelated flakes."""
        engine.exchange.get_ticker.side_effect = [
            _FakeRateLimit("429"),  # fail
            MagicMock(mark_price=50000.0, last=50000.0),  # recover
            _FakeRateLimit("429"),  # fail again — fresh streak
        ]
        with caplog.at_level("ERROR", logger="paper.paper_engine"):
            engine._tick()
            engine._tick()
            engine._tick()

        lines = [
            r.getMessage() for r in caplog.records
            if "Tick failure" in r.message
        ]
        assert len(lines) == 2
        assert "retry=1/5" in lines[0]
        assert "retry=1/5" in lines[1], (
            "expected counter reset after recovery, got "
            f"{lines[1]}"
        )


# ── Tick-level transient/persistent notification gate ───────────────────────


class TestTickFailureNotificationGate:
    """The Telegram-notification gate suppresses transient errors while
    the retry window still has budget, and fires exactly one persistent
    notification when the streak hits the threshold. Non-transient
    errors skip the retry window and notify on first occurrence."""

    def test_transient_single_failure_does_not_notify(self, engine):
        """One 429 should never reach Telegram — the engine retries on
        the next tick and the flake recovers silently."""
        engine.exchange.get_ticker.side_effect = _FakeRateLimit("429")
        engine._tick()

        engine._notify_queue.join()
        engine.notifier.notify_error_persistent.assert_not_called()

    def test_transient_below_threshold_does_not_notify(self, engine):
        """Four consecutive transient failures still below the 5/5
        threshold — no persistent notification yet."""
        engine.exchange.get_ticker.side_effect = _FakeRateLimit("429")
        for _ in range(4):
            engine._tick()

        engine._notify_queue.join()
        engine.notifier.notify_error_persistent.assert_not_called()

    def test_transient_threshold_fires_persistent_notification(self, engine):
        """The 5th consecutive transient failure crosses the threshold
        and fires the persistent Telegram notification exactly once."""
        engine.exchange.get_ticker.side_effect = _FakeRateLimit(
            "bitget 429 Too Many Requests"
        )
        for _ in range(5):
            engine._tick()

        engine._notify_queue.join()
        assert engine.notifier.notify_error_persistent.call_count == 1
        bot_name, err = engine.notifier.notify_error_persistent.call_args[0]
        assert bot_name == engine.config.name
        assert err.is_transient is True
        assert err.error_class == "RateLimitExceeded"
        assert err.retry_attempt == 5

    def test_persistent_latch_caps_one_notification_per_streak(self, engine):
        """After the persistent-notify fires, further ticks in the same
        streak must not repeat the message — otherwise a prolonged
        outage would spam Telegram."""
        engine.exchange.get_ticker.side_effect = _FakeRateLimit("429")
        for _ in range(10):
            engine._tick()

        engine._notify_queue.join()
        assert engine.notifier.notify_error_persistent.call_count == 1

    def test_non_transient_notifies_on_first_occurrence(self, engine):
        """Auth errors and bugs-in-our-code don't recover via retry —
        notify immediately, don't wait for the streak to reach 5."""
        engine.exchange.get_ticker.side_effect = _FakeAuthError(
            "invalid API key"
        )
        engine._tick()

        engine._notify_queue.join()
        assert engine.notifier.notify_error_persistent.call_count == 1
        _, err = engine.notifier.notify_error_persistent.call_args[0]
        assert err.is_transient is False
        assert err.error_class == "AuthenticationError"
        assert err.retry_attempt == 1

    def test_latch_resets_on_recovery_tick(self, engine):
        """A recovery tick between two failure-streaks clears the
        persistent-notify latch so the second streak can fire a fresh
        notification."""
        engine.exchange.get_ticker.side_effect = (
            [_FakeRateLimit("429")] * 5            # first streak hits threshold
            + [MagicMock(mark_price=50000.0, last=50000.0)]  # recovery
            + [_FakeRateLimit("429")] * 5          # second streak hits threshold
        )
        for _ in range(11):
            engine._tick()

        engine._notify_queue.join()
        assert engine.notifier.notify_error_persistent.call_count == 2, (
            "expected one persistent-notify per streak, got "
            f"{engine.notifier.notify_error_persistent.call_count}"
        )

    def test_transient_errors_do_not_call_legacy_notify_error(self, engine):
        """The legacy free-form notify_error stays reserved for non-tick
        paths (balance guard, reconciler). Tick-level failures go via
        notify_error_persistent only so the old "❌ Error Bot: X" format
        disappears from the tick path."""
        engine.exchange.get_ticker.side_effect = _FakeRateLimit("429")
        for _ in range(5):
            engine._tick()

        engine._notify_queue.join()
        engine.notifier.notify_error.assert_not_called()
