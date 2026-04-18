"""Tests for live/live_engine.py — Phase 1 scaffolding.

Covers the pre-flight safety rail (oversized base order rejected),
the dry-run order path (no ccxt call, synthetic fill returned) and
the Phase-3 refusal path (dry_run=False → NotImplementedError).
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
    TakeProfitConfig,
)
from live.live_engine import LiveEngine  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def minimal_bot_config():
    return BotConfig(
        name="LiveBot",
        mode=Mode.LIVE,
        exchange=Exchange.BITGET,
        pair="BTC/USD",
        dca=DCAConfig(
            enabled=True,
            base_order_size=0.0005,
            max_orders=3,
            order_spacing_pct=1.5,
            multiplier=1.0,
        ),
        take_profit=TakeProfitConfig(enabled=True, target_pct=3.0),
    )


@pytest.fixture
def mock_exchange():
    mock = MagicMock()
    mock.get_ticker.return_value = MagicMock(mark_price=50000.0, last=50000.0)
    mock.get_ohlcv.return_value = [
        [1_000_000 + i * 60_000, 50000.0, 50100.0, 49900.0, 50050.0, 1.0]
        for i in range(100)
    ]
    return mock


@pytest.fixture
def mock_notifier():
    n = MagicMock()
    for m in [
        "notify_startup", "notify_shutdown", "notify_entry",
        "notify_dca", "notify_take_profit", "notify_stop_loss",
        "notify_error", "notify_stop", "notify_restart",
    ]:
        setattr(n, m, MagicMock())
    return n


def _make_engine(config, exchange, notifier, tmp_path, **kwargs):
    """Helper — builds a LiveEngine with a tmp state file and cleans up
    the notify worker thread on context exit. Used everywhere instead
    of a pytest fixture because several tests tweak constructor kwargs."""
    state_file = tmp_path / "live.state.json"
    eng = LiveEngine(
        config=config,
        exchange=exchange,
        notifier=notifier,
        initial_balance_btc=0.1,
        poll_interval=1,
        state_file=str(state_file),
        slug="livebot",
        **kwargs,
    )
    return eng


# ── Preflight checks ────────────────────────────────────────────────────────

class TestLiveEnginePreflights:

    def test_rejects_oversized_base_order(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """base_order_size > max → ValueError before the engine ever
        touches the state file or starts a notify thread."""
        minimal_bot_config.dca.base_order_size = 0.1
        with pytest.raises(ValueError, match="exceeds max"):
            _make_engine(
                minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
                max_base_order_size=0.001,
            )

    def test_accepts_valid_size(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """base_order_size <= max → engine boots."""
        minimal_bot_config.dca.base_order_size = 0.0005
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
            max_base_order_size=0.001,
        )
        try:
            assert eng is not None
            assert eng.is_dry_run is True
            assert eng._max_base_order_size == 0.001
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_custom_max_allows_larger_size(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """Operator can raise the cap for well-funded accounts."""
        minimal_bot_config.dca.base_order_size = 0.01
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
            max_base_order_size=0.05,
        )
        try:
            assert eng is not None
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_rejects_dca_multiplier_explosion(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """multiplier=2.0 + max_orders=10 makes the last DCA order
        0.001 × 2^9 = 0.512 BTC — 512× the base-order cap. The preflight
        must refuse this even when base_order_size itself is tiny."""
        minimal_bot_config.dca.base_order_size = 0.001
        minimal_bot_config.dca.multiplier = 2.0
        minimal_bot_config.dca.max_orders = 10
        with pytest.raises(ValueError, match="Worst-case DCA"):
            _make_engine(
                minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
                max_base_order_size=0.001,
            )

    def test_rejects_cumulative_explosion(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """Per-order worst-case under the ceiling, but the SUM of base
        + every DCA order exceeds max_cumulative_size → refused."""
        minimal_bot_config.dca.base_order_size = 0.001
        minimal_bot_config.dca.multiplier = 1.5
        minimal_bot_config.dca.max_orders = 6  # worst DCA ≈ 0.0076 (7.6x)
        # Tight explicit cap — cumulative with 6 orders exceeds 0.005.
        minimal_bot_config.dca.max_cumulative_size = 0.005
        with pytest.raises(ValueError, match="Cumulative DCA"):
            _make_engine(
                minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
                max_base_order_size=0.01,
            )


# ── Dry-run order execution ─────────────────────────────────────────────────

class TestDryRunOrderExecution:

    def test_dry_run_logs_no_real_order(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """Dry-run: _place_market_order returns a synthetic fill, logs
        to the live_order_log, and does NOT call the exchange client's
        place_market_order method."""
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
            dry_run=True,
        )
        try:
            result = eng._place_market_order("buy", 0.0005, price=50_000.0)

            assert result["status"] == "filled"
            assert result["filled"] == 0.0005
            assert result["dry_run"] is True
            assert result["id"].startswith("DRYRUN-")

            log = eng.get_live_order_log()
            assert len(log) == 1
            assert log[0]["side"] == "buy"
            assert log[0]["size"] == 0.0005
            assert log[0]["dry_run"] is True

            # Exchange was never asked to place an order.
            mock_exchange.place_market_order.assert_not_called()
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_non_dry_run_raises_not_implemented(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """dry_run=False → NotImplementedError until Phase 3 wires the
        real ccxt call. The order log still records the intent so an
        operator can inspect what was attempted."""
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
            dry_run=False,
        )
        try:
            with pytest.raises(NotImplementedError, match="Phase 1"):
                eng._place_market_order("buy", 0.0005, price=50_000.0)

            # The log still captured the attempt — useful post-mortem.
            log = eng.get_live_order_log()
            assert len(log) == 1
            assert log[0]["dry_run"] is False
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_order_log_returns_shallow_copy(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """Mutating the returned list must not poke the engine's internal
        state."""
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
            dry_run=True,
        )
        try:
            eng._place_market_order("buy", 0.0005, price=50_000.0)
            log = eng.get_live_order_log()
            log.append({"tainted": True})
            assert len(eng.get_live_order_log()) == 1
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)


# ── Inheritance / bookkeeping ───────────────────────────────────────────────

class TestDryRunImmutability:

    def test_dry_run_property_read_only(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """Re-assignment via the property surface must fail. An AttributeError
        is the acceptable outcome — anything that lets Phase-3 real orders
        flow through a mutated flag is not."""
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
            dry_run=True,
        )
        try:
            with pytest.raises(AttributeError):
                eng.dry_run = False  # type: ignore[misc]
            assert eng.dry_run is True
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)


class TestLiveEngineInheritsPaperBehaviour:

    def test_engine_has_paper_attributes(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """LiveEngine must expose the same state/indicator/guard handles
        PaperEngine callers rely on — no inherited attribute should be
        clobbered."""
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
        )
        try:
            assert eng.state is not None
            assert eng.indicator_engine is not None
            assert eng.liq_guard is not None
            assert eng.drawdown_guard is not None
            assert eng._bot_slug == "livebot"
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)


class TestLiveEngineReconcilerWiring:
    """OrderReconciler must be instantiated, invoked every
    RECONCILE_EVERY_N_TICKS, and the timeout branch must surface as an
    operator notification — all without blocking the tick loop."""

    def test_reconciler_attached(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
        )
        try:
            assert eng.order_reconciler is not None
            # Tick counter starts fresh.
            assert eng._reconcile_tick_counter == 0
            # Reconciler is backed by the engine's exchange client.
            assert eng.order_reconciler.exchange is mock_exchange
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_run_reconciliation_called_every_n_ticks(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """After RECONCILE_EVERY_N_TICKS ticks, _run_reconciliation
        fires exactly once and the counter resets to 0."""
        from live.live_engine import RECONCILE_EVERY_N_TICKS

        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
        )
        try:
            calls = {"n": 0}

            def spy():
                calls["n"] += 1
            eng._run_reconciliation = spy

            for _ in range(RECONCILE_EVERY_N_TICKS):
                eng._tick()

            assert calls["n"] == 1, (
                f"expected 1 reconciler call after {RECONCILE_EVERY_N_TICKS}"
                f" ticks, got {calls['n']}"
            )
            assert eng._reconcile_tick_counter == 0
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_timeout_order_triggers_notification(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """A PendingOrder older than max_age_seconds must surface as a
        notify_error call through the engine's notify queue."""
        import time as _time
        from live.order_reconciliation import PendingOrder

        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
        )
        try:
            # Seed a pending order well past the 60s timeout budget.
            eng.order_reconciler.track_order(PendingOrder(
                client_order_id="stale-coid",
                deal_id="PAPER-0001",
                side="buy",
                size=0.0005,
                placed_at=_time.time() - 120.0,
            ))

            notified = []
            eng._notify = lambda fn, name, msg: notified.append(msg)

            eng._run_reconciliation()

            assert any(
                "timeout" in n.lower() or "stale-coid" in n
                for n in notified
            ), f"no timeout notification, got {notified}"
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)

    def test_reconciliation_exception_does_not_crash_tick(
        self, minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
    ):
        """If the reconciler raises, _run_reconciliation must swallow +
        log rather than letting the exception propagate into the tick."""
        eng = _make_engine(
            minimal_bot_config, mock_exchange, mock_notifier, tmp_path,
        )
        try:
            def _boom(*a, **kw):
                raise RuntimeError("simulated reconciler failure")
            eng.order_reconciler.reconcile = _boom

            # Must not raise.
            eng._run_reconciliation()
        finally:
            eng._notify_queue.put(None)
            eng._notify_thread.join(timeout=5)
