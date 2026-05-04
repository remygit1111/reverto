# tests/test_trading_engine.py
import sys, os, pytest
from unittest.mock import MagicMock
from datetime import datetime, UTC

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.inverse_perp_math import (
    compute_sl_target_price,
    compute_tp_target_price,
)
from paper.paper_state import PaperState, PaperDeal, PaperOrder
from paper.paper_engine import PaperEngine

def _order(price, size=0.001, t="base", n=1):
    return PaperOrder(order_number=n, price=price, size=size,
                      timestamp=datetime.now(UTC), order_type=t)

def _deal(price=80000.0, size=0.001, lev=1):
    return PaperDeal(id="T-0001", bot_name="tb", symbol="BTC/USD",
                     side="long", leverage=lev, orders=[_order(price, size)])

def _notifier():
    n = MagicMock()
    for m in ["notify_startup","notify_shutdown","notify_entry","notify_dca",
              "notify_take_profit","notify_stop_loss","notify_error",
              "notify_schedule_open","notify_schedule_close"]:
        setattr(n, m, MagicMock())
    return n

def _engine(sl_type="fixed", sl_pct=6.0, tp_pct=3.0, max_orders=5,
            spacing=2.5, mult=1.5, base_size=0.001):
    cfg = MagicMock()
    cfg.name = "tb"; cfg.pair = "BTC/USD"
    cfg.mode.value = "paper"; cfg.exchange.value = "bitget"
    cfg.leverage.enabled = False; cfg.leverage.size = 1
    cfg.leverage.liquidation_guard.warn_pct = 15.0
    cfg.leverage.liquidation_guard.emergency_close_pct = 5.0
    cfg.take_profit.target_pct = tp_pct
    cfg.take_profit.indicator_confirm = None
    cfg.take_profit.minimum_tp_pct = None
    cfg.stop_loss.type = sl_type; cfg.stop_loss.pct = sl_pct
    cfg.dca.max_orders = max_orders; cfg.dca.order_spacing_pct = spacing
    cfg.dca.multiplier = mult; cfg.dca.base_order_size = base_size
    cfg.dca.taker_fee = 0.0006; cfg.dca.step_scale = 1.0
    cfg.entry.indicators = []
    cfg.schedule.trading_windows = []; cfg.schedule.blackout_dates = []
    cfg.schedule.timezone = "Europe/Amsterdam"
    cfg.telegram.notify_on = []; cfg.ml.enabled = False
    return PaperEngine(config=cfg, exchange=MagicMock(),
                       notifier=_notifier(), initial_balance_btc=0.1)


class TestTakeProfit:
    # pt-041: TP target derivation switched from linear (avg * 1.03)
    # to inverse-perp (avg / 0.97) at tp_pct=3 %. Tests now derive
    # the trigger price via the same helper the engine uses so
    # they stay correct under any future helper revision.

    def test_tp_fires_at_target(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        e.state.open_deal(d)
        target = compute_tp_target_price(80000.0, 3.0, "long")
        e._check_tp(d, target)
        assert d.id not in e.state.open_deals
        assert e.state.closed_deals[0].close_reason == "tp"

    def test_tp_no_fire_below_target(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        e.state.open_deal(d)
        # 80000 * 1.02 = 81600 sits below the post-fix target
        # (~82474) so this path stays valid regardless of the
        # linear-vs-inverse fix; pin literal to keep the test
        # easy to read at a glance.
        e._check_tp(d, 80000.0 * 1.02)
        assert d.id in e.state.open_deals

    def test_tp_pnl_positive(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        e.state.open_deal(d)
        target = compute_tp_target_price(80000.0, 3.0, "long")
        e._check_tp(d, target)
        assert e.state.closed_deals[0].pnl_btc > 0

    def test_tp_notifier_called(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        e.state.open_deal(d)
        target = compute_tp_target_price(80000.0, 3.0, "long")
        e._check_tp(d, target)
        e._notify_queue.join()
        e.notifier.notify_take_profit.assert_called_once()

    def test_tp_uses_avg_entry(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        d.orders.append(_order(78000.0, 0.002, "dca", 2))
        e.state.open_deal(d)
        target = compute_tp_target_price(d.avg_entry_price, 3.0, "long")
        e._check_tp(d, target - 1)
        assert d.id in e.state.open_deals
        e._check_tp(d, target)
        assert d.id not in e.state.open_deals


class TestFixedStopLoss:
    def test_sl_fires(self):
        e = _engine(sl_type="fixed", sl_pct=6.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_sl(d, 80000.0 * 0.94)
        assert d.id not in e.state.open_deals
        assert e.state.closed_deals[0].close_reason == "sl"

    def test_sl_no_fire_above_threshold(self):
        e = _engine(sl_type="fixed", sl_pct=6.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_sl(d, 80000.0 * 0.95)
        assert d.id in e.state.open_deals

    def test_sl_pnl_negative(self):
        e = _engine(sl_type="fixed", sl_pct=6.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_sl(d, 80000.0 * 0.94)
        assert e.state.closed_deals[0].pnl_btc < 0

    def test_sl_notifier_called(self):
        e = _engine(sl_type="fixed", sl_pct=6.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_sl(d, 80000.0 * 0.94)
        e._notify_queue.join()
        e.notifier.notify_stop_loss.assert_called_once()


class TestTrailingStopLoss:
    def test_peak_initialized(self):
        e = _engine(sl_type="trailing", sl_pct=6.0); d = _deal(80000.0)
        d._peak_price = 0.0; e.state.open_deal(d)
        e._check_sl(d, 80000.0)
        assert d._peak_price == 80000.0

    def test_peak_tracks_higher(self):
        e = _engine(sl_type="trailing", sl_pct=6.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_sl(d, 80000.0)
        e._check_sl(d, 85000.0)
        e._check_sl(d, 83000.0)
        assert d._peak_price == 85000.0

    def test_peak_not_updated_lower(self):
        e = _engine(sl_type="trailing", sl_pct=6.0); d = _deal(80000.0)
        d._peak_price = 85000.0; e.state.open_deal(d)
        e._check_sl(d, 82000.0)
        assert d._peak_price == 85000.0

    def test_trailing_fires_from_peak(self):
        e = _engine(sl_type="trailing", sl_pct=6.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_sl(d, 90000.0)                        # peak = 90000
        # pt-041: trailing SL line is now derived inversely from
        # the peak — 90 000 / 1.06 ≈ 84 905.66 instead of the old
        # linear 90 000 × 0.94 = 84 600.
        sl = compute_sl_target_price(90000.0, 6.0, "long")
        e._check_sl(d, sl + 1)
        assert d.id in e.state.open_deals               # nog open
        e._check_sl(d, sl)
        assert d.id not in e.state.open_deals           # gesloten


class TestStopLossNone:
    """SL type 'none' must skip the SL check entirely."""

    def test_sl_none_no_trigger(self):
        e = _engine(sl_type='none', sl_pct=5.0)
        d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_sl(d, 40000.0)
        assert d.id in e.state.open_deals

    def test_sl_none_deal_stays_open(self):
        e = _engine(sl_type='none', sl_pct=0.1)
        d = _deal(80000.0)
        e.state.open_deal(d)
        for price in [79000, 70000, 50000, 10000]:
            e._check_sl(d, float(price))
        assert d.id in e.state.open_deals


class TestDCA:
    def test_dca_triggers(self):
        e = _engine(spacing=2.5, max_orders=5); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.975)
        assert d.dca_count == 1

    def test_dca_no_trigger_above_spacing(self):
        e = _engine(spacing=2.5); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.98)
        assert d.dca_count == 0

    def test_dca_disabled_when_max_orders_zero(self):
        # max_orders=0 must short-circuit the DCA check entirely so a
        # base-order-only bot never adds extra fills.
        e = _engine(spacing=2.5, max_orders=0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.95)
        assert d.dca_count == 0

    def test_dca_disabled_when_max_orders_one(self):
        # max_orders=1 means "base order only" — same disabled behaviour.
        e = _engine(spacing=2.5, max_orders=1); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.90)
        assert d.dca_count == 0

    def test_dca_max_orders_respected(self):
        e = _engine(spacing=2.5, max_orders=5, mult=1.0); d = _deal(80000.0)
        e.state.open_deal(d)
        for _ in range(4):
            e._check_dca(d, d.orders[-1].price * 0.975)
        assert d.dca_count == 4
        e._check_dca(d, d.orders[-1].price * 0.975)
        assert d.dca_count == 4  # geen 5e DCA

    def test_no_cascade_single_call(self):
        e = _engine(spacing=2.5, max_orders=5); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_dca(d, 70000.0)   # grote daling, raakt meerdere niveaus
        assert d.dca_count == 1    # maar slechts 1 order per call

    def test_multiplier_sizing(self):
        e = _engine(spacing=2.5, mult=1.5, base_size=0.001)
        d = _deal(80000.0, 0.001); e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.975)
        assert abs(d.orders[1].size - 0.001) < 1e-8
        e._check_dca(d, d.orders[-1].price * 0.975)
        assert abs(d.orders[2].size - 0.0015) < 1e-8

    def test_reference_is_last_order(self):
        e = _engine(spacing=2.5, max_orders=5, mult=1.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_dca(d, 78000.0)
        assert d.dca_count == 1
        e._check_dca(d, 78000.0 * 0.975)
        assert d.dca_count == 2

    def test_dca_notifier_called(self):
        e = _engine(spacing=2.5); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.975)
        e._notify_queue.join()
        e.notifier.notify_dca.assert_called_once()


class TestNoDoubleClose:
    def test_tp_before_sl(self):
        e = _engine(sl_type="fixed", sl_pct=6.0, tp_pct=3.0)
        d = _deal(80000.0); e.state.open_deal(d)
        price = compute_tp_target_price(80000.0, 3.0, "long")
        e._check_tp(d, price)
        assert len(e.state.closed_deals) == 1
        assert e.state.closed_deals[0].close_reason == "tp"


class TestWickSimulation:
    """Wick simulation (post-per-deal-tracking fix): TP / SL / trailing
    only fire on ticks observed AFTER the deal opened. Pre-existing
    wicks in the same forming candle no longer trigger retroactively.

    Prior to the fix, these tests seeded the forming candle's cached
    wick directly via ``e._wick_candle[tf] = (high, low, close)`` —
    that path is now irrelevant because ``_check_tp`` / ``_check_sl``
    read ``deal._wick_high_since_open`` / ``_wick_low_since_open``
    instead of the candle. The updated tests drive the tracker with
    an explicit ``_update_deal_wick_trackers(price)`` call which
    represents "a tick at this price was observed during the deal's
    lifetime".
    """

    def test_tp_fires_on_wick_high_when_tick_below_target(self):
        """A tick at the wick high was OBSERVED during the deal's
        lifetime; a later sampled tick lands below the target. TP
        must still fire because the tracker retained the peak."""
        e = _engine(tp_pct=3.0); d = _deal(80000.0); e.state.open_deal(d)
        wick_high = 80000.0 * 1.04
        # Simulate: tick loop first observed the price spike to
        # wick_high, then the subsequent sample landed at `tick`.
        e._update_deal_wick_trackers(wick_high)
        tick = 80000.0 * 1.01
        e._check_tp(d, tick)
        assert d.id not in e.state.open_deals
        closed = e.state.closed_deals[0]
        assert closed.close_reason == "tp"
        # Fill capped at the post-fix inverse-perp target, not at
        # the wick high.
        target = compute_tp_target_price(80000.0, 3.0, "long")
        assert abs(closed.close_price - target) < 0.01

    def test_sl_fires_on_current_tick_below_stop(self):
        """SL uses the LIVE TICK only in the post-fix model (no
        tracker.low memory). Renamed from the old
        ``_fires_on_wick_low_when_tick_above_stop`` — that scenario
        is no longer reachable because the monitor loop evaluates
        SL on every tick and closes at the first tick below the
        line; a later tick above the line would never see a deal
        that's still open."""
        e = _engine(sl_type="fixed", sl_pct=5.0); d = _deal(80000.0); e.state.open_deal(d)
        sl_line = 80000.0 * 0.95
        # Tick drops below SL line — SL fires at this tick.
        e._check_sl(d, sl_line - 50)
        assert d.id not in e.state.open_deals
        closed = e.state.closed_deals[0]
        assert closed.close_reason == "sl"

    def test_wick_disabled_falls_back_to_tick_only(self):
        """``use_wick_simulation=False`` forces TP evaluation to use the
        current tick only, ignoring any tracker value the monitor loop
        might have folded in from a prior tick."""
        e = _engine(tp_pct=3.0); d = _deal(80000.0); e.state.open_deal(d)
        e.config.use_wick_simulation = False
        # Tracker says target is reached (a prior tick spike), current
        # tick below. With wick-sim disabled, the tracker must be
        # ignored — only the live tick counts.
        e._update_deal_wick_trackers(80000.0 * 1.05)
        e._check_tp(d, 80000.0 * 1.01)
        assert d.id in e.state.open_deals

    def test_tp_normal_tick_path_unchanged(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0); e.state.open_deal(d)
        # No tracker updates; tick itself crosses target.
        e._check_tp(d, compute_tp_target_price(80000.0, 3.0, "long"))
        assert d.id not in e.state.open_deals

    def test_trailing_peak_updates_via_wick_high(self):
        e = _engine(sl_type="trailing", sl_pct=5.0)
        d = _deal(80000.0); e.state.open_deal(d)
        wick_high = 82000.0
        # Prior tick during lifetime observed the wick_high.
        e._update_deal_wick_trackers(wick_high)
        tick = 81000.0
        e._check_sl(d, tick)
        assert d._peak_price == pytest.approx(wick_high)
        # Deal still open — tracker low is above the new SL line.
        assert d.id in e.state.open_deals


class TestPerDealWickTracking:
    """Regression coverage for the rapid-fire-TP bug.

    Pre-fix: a deal opened mid-candle inherited the candle's full
    forming wick — including ticks that happened BEFORE the deal
    was opened. Post-fix: each deal's wick trackers start at
    ``avg_entry_price`` and only rise (fall) on ticks observed
    during that deal's own lifetime.
    """

    def test_wick_tracker_seeded_from_entry_price_on_open(self):
        """Fresh deal construction seeds both trackers to
        ``avg_entry_price`` via ``PaperDeal.__post_init__``. A later
        tick at that price is a no-op; only a higher (lower) tick
        mutates the tracker."""
        d = _deal(80000.0)
        assert d._wick_high_since_open == pytest.approx(80000.0)
        assert d._wick_low_since_open == pytest.approx(80000.0)

    def test_wick_tracker_updates_on_tick(self):
        e = _engine(tp_pct=3.0); d = _deal(100.0); e.state.open_deal(d)
        e._update_deal_wick_trackers(105.0)
        assert d._wick_high_since_open == pytest.approx(105.0)
        # Low tracker doesn't move on an upward tick.
        assert d._wick_low_since_open == pytest.approx(100.0)

    def test_wick_tracker_updates_low_on_downward_tick(self):
        e = _engine(tp_pct=3.0); d = _deal(100.0); e.state.open_deal(d)
        e._update_deal_wick_trackers(95.0)
        assert d._wick_low_since_open == pytest.approx(95.0)
        assert d._wick_high_since_open == pytest.approx(100.0)

    def test_wick_tracker_ignores_pre_existing_candle_wick(self):
        """THE CORE REGRESSION TEST for the rapid-fire TP bug.

        Scenario observed on the RSI real-test bot: the 15m candle
        already had a wick-high ABOVE the TP target before the new
        deal was opened. Pre-fix, the next tick would read the
        forming candle's high via ``_wick_high_low`` and fire TP
        immediately — hundreds of deals cycled through open → TP
        in minutes, all on the same pre-existing wick. Post-fix,
        the deal's tracker starts at entry and only rises on new
        ticks, so the pre-existing wick does nothing.
        """
        e = _engine(tp_pct=3.0); d = _deal(100.0); e.state.open_deal(d)
        # Populate the old ``_wick_candle`` cache to simulate the
        # forming candle with a pre-existing high above TP. The cache
        # is now unused by ``_check_tp`` — this seed exists purely to
        # prove that the old code path has no effect on behaviour.
        tf = e.config.timeframe
        e._wick_candle[tf] = (110.0, 99.0, 102.0)  # high, low, close
        # No ticks observed since deal-open → tracker stays at 100.
        # The next tick is at 102 — still below TP target of 103.
        e._check_tp(d, 102.0)
        assert d.id in e.state.open_deals, (
            "TP must NOT fire on a pre-existing candle wick — the tracker "
            "only captures ticks observed since the deal was opened."
        )

    def test_tp_hit_on_tick_after_deal_open(self):
        """Direct tick-hit path — the tracker is updated to the tick
        value, which is also at target. TP fires cleanly."""
        e = _engine(tp_pct=3.0); d = _deal(100.0); e.state.open_deal(d)
        target = compute_tp_target_price(100.0, 3.0, "long")
        e._update_deal_wick_trackers(target)
        e._check_tp(d, target)
        assert d.id not in e.state.open_deals
        assert e.state.closed_deals[0].close_reason == "tp"

    def test_tp_hit_on_wick_after_deal_open(self):
        """Wick-hit path — a prior tick landed above target, the
        next sampled tick is below but tracker retained the peak.
        TP fires with fill capped at target."""
        e = _engine(tp_pct=3.0); d = _deal(100.0); e.state.open_deal(d)
        e._update_deal_wick_trackers(105.0)  # spike observed
        e._check_tp(d, 102.0)                # next sample below
        assert d.id not in e.state.open_deals
        closed = e.state.closed_deals[0]
        assert closed.close_reason == "tp"
        # Fill capped at the post-fix inverse-perp target.
        target = compute_tp_target_price(100.0, 3.0, "long")
        assert closed.close_price == pytest.approx(target)

    def test_sl_ignores_pre_fix_tracker_low_memory(self):
        """Post-fix SL decision is tick-only (see ``_check_sl`` comment
        for the anachronism argument). A tracker.low value captured
        from a prior dip must NOT retroactively trigger SL if the
        current tick has recovered — the monitor loop would have
        already closed the deal at the dip tick if the dip actually
        crossed the line."""
        e = _engine(sl_type="fixed", sl_pct=5.0)
        d = _deal(100.0); e.state.open_deal(d)
        # Synthetically push tracker.low below SL line without firing
        # SL (as would happen if the monitor loop somehow skipped a
        # tick — which shouldn't happen in practice, but we pin the
        # behaviour anyway).
        d._wick_low_since_open = 94.0
        # Current tick has recovered above SL line.
        e._check_sl(d, 96.0)
        assert d.id in e.state.open_deals, (
            "SL must not fire on a historical tracker.low — decision "
            "is based on the current tick only."
        )

    def test_trailing_stop_uses_since_open_high(self):
        """Trailing peak must only rise on ticks observed during the
        deal's lifetime — not on the forming candle's full wick."""
        e = _engine(sl_type="trailing", sl_pct=5.0)
        d = _deal(100.0); e.state.open_deal(d)

        # Seed the OLD forming-candle cache to a peak we want the
        # trailing logic to IGNORE. Pre-fix this would have jumped
        # the peak to 120.
        tf = e.config.timeframe
        e._wick_candle[tf] = (120.0, 95.0, 102.0)

        # Tick during lifetime observes 110 — that's the real peak.
        e._update_deal_wick_trackers(110.0)
        e._check_sl(d, 108.0)
        assert d._peak_price == pytest.approx(110.0), (
            "Trailing peak must rise from the since-open tracker "
            "(110) — NOT from the forming-candle cache (120)."
        )

    def test_rapid_fire_scenario_no_double_close(self):
        """End-to-end regression: a pre-existing wick + a fresh deal
        opened on the same candle must produce ZERO spurious closes.
        This is the scenario that burned 26 deals in 30 minutes on
        the RSI real-test bot."""
        e = _engine(tp_pct=1.0); d = _deal(100.0); e.state.open_deal(d)

        # Candle cache suggests a 15% wick from earlier in the
        # candle. Pre-fix, the next tick anywhere near entry would
        # fire TP immediately because the cached wick > target.
        tf = e.config.timeframe
        e._wick_candle[tf] = (115.0, 99.0, 100.5)

        # Simulate a handful of ticks near the entry price.
        for sample in [100.2, 100.5, 100.1, 100.3]:
            e._update_deal_wick_trackers(sample)
            e._check_tp(d, sample)
            e._check_sl(d, sample)

        assert d.id in e.state.open_deals, (
            "Post-fix: no spurious TP or SL on pre-existing wicks. "
            "The deal must still be open after 4 near-entry ticks."
        )


class TestStateLoadBackwardsCompat:
    """Pre-fix state.json files do not carry ``_wick_high_since_open``
    / ``_wick_low_since_open``. The loader must synthesise a sane
    default from ``avg_entry_price`` so an in-flight deal carried
    over a portal restart doesn't immediately trip TP/SL on the
    next tick."""

    def test_dict_to_deal_without_new_fields_uses_avg_entry(self):
        from paper.state_io import dict_to_deal
        state = {
            "id": "T-0001",
            "bot_name": "tb",
            "symbol": "BTC/USD",
            "side": "long",
            "leverage": 1,
            "orders": [
                {
                    "order_number": 1,
                    "price": 80000.0,
                    "size": 0.001,
                    "timestamp": None,
                    "order_type": "base",
                },
            ],
            "is_open": True,
            # No _wick_high_since_open / _wick_low_since_open keys —
            # as would be the case for any state.json written by the
            # pre-fix engine.
        }
        d = dict_to_deal(state)
        assert d._wick_high_since_open == pytest.approx(80000.0)
        assert d._wick_low_since_open == pytest.approx(80000.0)

    def test_dict_to_deal_respects_persisted_trackers(self):
        """When the state file DOES carry the trackers (post-fix
        engine wrote them), the loader must round-trip them
        unchanged so a tracker that already saw ticks isn't reset
        on restart."""
        from paper.state_io import dict_to_deal
        state = {
            "id": "T-0001",
            "bot_name": "tb",
            "symbol": "BTC/USD",
            "side": "long",
            "leverage": 1,
            "orders": [
                {
                    "order_number": 1,
                    "price": 80000.0,
                    "size": 0.001,
                    "timestamp": None,
                    "order_type": "base",
                },
            ],
            "is_open": True,
            "_wick_high_since_open": 81500.0,
            "_wick_low_since_open": 79200.0,
        }
        d = dict_to_deal(state)
        assert d._wick_high_since_open == pytest.approx(81500.0)
        assert d._wick_low_since_open == pytest.approx(79200.0)

    def test_deal_to_dict_persists_trackers(self):
        from paper.state_io import deal_to_dict
        d = _deal(80000.0)
        # Mutate tracker to a distinct value so the roundtrip check
        # can't pass by coincidence.
        d._wick_high_since_open = 82345.0
        d._wick_low_since_open = 78123.0
        out = deal_to_dict(d)
        assert out["_wick_high_since_open"] == pytest.approx(82345.0)
        assert out["_wick_low_since_open"] == pytest.approx(78123.0)


class TestManualTriggerLiquidationGuard:
    """Manual deal trigger refuses to open when the entry would land
    inside the liquidation guard's emergency band. Spot bots are
    always safe regardless of price."""

    def test_spot_bot_always_safe(self):
        e = _engine()
        # Default _engine has leverage.enabled=False, size=1.
        assert e._manual_trigger_liq_safe(80000.0) is True

    def test_leveraged_bot_safe_at_normal_distance(self):
        e = _engine()
        e.config.leverage.enabled = True
        e.config.leverage.size = 5
        e.config.leverage.liquidation_guard.emergency_close_pct = 5.0
        # At 5x leverage liquidation sits ~20% below entry — well clear
        # of the 5% emergency band, so any positive price is safe.
        assert e._manual_trigger_liq_safe(80000.0) is True

    def test_excessive_leverage_refused(self):
        e = _engine()
        e.config.leverage.enabled = True
        e.config.leverage.size = 100
        e.config.leverage.liquidation_guard.emergency_close_pct = 5.0
        # 100x liquidation distance ≈ 1%, well inside the 5% band.
        assert e._manual_trigger_liq_safe(80000.0) is False

    def test_zero_price_refused(self):
        e = _engine()
        e.config.leverage.enabled = True
        e.config.leverage.size = 10
        assert e._manual_trigger_liq_safe(0.0) is False


# ── step_scale != 1.0 ────────────────────────────────────────────────────────

class TestStepScale:
    def test_step_scale_widens_spacing(self):
        """DCA spacing should widen by step_scale^dca_count each order."""
        e = _engine(spacing=2.0, max_orders=5, mult=1.0)
        e.config.dca.step_scale = 1.5
        d = _deal(80000.0); e.state.open_deal(d)

        # DCA 1: spacing = 2.0 * 1.5^0 = 2.0% below 80000 = 78400
        e._check_dca(d, 78400.0)
        assert d.dca_count == 1
        last = d.orders[-1].price
        assert last == 78400.0

        # DCA 2: spacing = 2.0 * 1.5^1 = 3.0% below 78400 = 76048
        trigger = last * (1 - 3.0 / 100)
        e._check_dca(d, trigger)
        assert d.dca_count == 2

        # DCA 3: spacing = 2.0 * 1.5^2 = 4.5% below last order
        last2 = d.orders[-1].price
        trigger3 = last2 * (1 - 4.5 / 100)
        e._check_dca(d, trigger3)
        assert d.dca_count == 3

    def test_step_scale_1_matches_flat(self):
        """step_scale=1.0 should produce constant spacing (sanity)."""
        e = _engine(spacing=2.5, max_orders=3, mult=1.0)
        e.config.dca.step_scale = 1.0
        d = _deal(80000.0); e.state.open_deal(d)
        e._check_dca(d, 80000.0 * 0.975)
        assert d.dca_count == 1
        p2 = d.orders[-1].price * 0.975
        e._check_dca(d, p2)
        assert d.dca_count == 2


# ── Config model validators ───────────────────────────────────────────────────

class TestConfigValidators:
    def test_negative_base_order_size_rejected(self):
        from config.models import DCAConfig
        with pytest.raises(Exception):
            DCAConfig(base_order_size=-1.0)

    def test_zero_target_pct_rejected(self):
        from config.models import TakeProfitConfig
        with pytest.raises(Exception):
            TakeProfitConfig(target_pct=0)

    def test_leverage_over_125_rejected(self):
        from config.models import LeverageConfig
        with pytest.raises(Exception):
            LeverageConfig(enabled=True, size=200)

    def test_sl_pct_over_100_rejected(self):
        from config.models import StopLossConfig
        with pytest.raises(Exception):
            StopLossConfig(type="fixed", pct=101)

    def test_valid_config_accepted(self):
        from config.models import DCAConfig, TakeProfitConfig, StopLossConfig
        DCAConfig(base_order_size=0.001)
        TakeProfitConfig(target_pct=3.0)
        StopLossConfig(type="fixed", pct=5.0)
