# tests/test_trading_engine.py
import sys, os, pytest
from unittest.mock import MagicMock
from datetime import datetime, UTC

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    cfg.dca.taker_fee = 0.0006
    cfg.entry.indicators = []
    cfg.schedule.trading_windows = []; cfg.schedule.blackout_dates = []
    cfg.schedule.timezone = "Europe/Amsterdam"
    cfg.telegram.notify_on = []; cfg.ml.enabled = False
    return PaperEngine(config=cfg, exchange=MagicMock(),
                       notifier=_notifier(), initial_balance_btc=0.1)


class TestTakeProfit:
    def test_tp_fires_at_target(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_tp(d, 80000.0 * 1.03)
        assert d.id not in e.state.open_deals
        assert e.state.closed_deals[0].close_reason == "tp"

    def test_tp_no_fire_below_target(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_tp(d, 80000.0 * 1.02)
        assert d.id in e.state.open_deals

    def test_tp_pnl_positive(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_tp(d, 80000.0 * 1.03)
        assert e.state.closed_deals[0].pnl_btc > 0

    def test_tp_notifier_called(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        e.state.open_deal(d)
        e._check_tp(d, 80000.0 * 1.03)
        e._notify_queue.join()
        e.notifier.notify_take_profit.assert_called_once()

    def test_tp_uses_avg_entry(self):
        e = _engine(tp_pct=3.0); d = _deal(80000.0)
        d.orders.append(_order(78000.0, 0.002, "dca", 2))
        e.state.open_deal(d)
        target = d.avg_entry_price * 1.03
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
        sl = 90000.0 * (1 - 0.06)                      # = 84600
        e._check_sl(d, sl + 1)
        assert d.id in e.state.open_deals               # nog open
        e._check_sl(d, sl)
        assert d.id not in e.state.open_deals           # gesloten


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
        price = 80000.0 * 1.03
        e._check_tp(d, price)
        assert len(e.state.closed_deals) == 1
        assert e.state.closed_deals[0].close_reason == "tp"


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
