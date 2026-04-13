# tests/test_backtest.py
# Tests voor de backtest engine en rapport.

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.backtest_engine import BacktestEngine, BacktestCandle
from backtest.backtest_report import BacktestResult


def _candle(ts, o, h, lo, c, v=100.0):
    return BacktestCandle(timestamp=ts, open=o, high=h, low=lo, close=c, volume=v)


def _config(tp_pct=3.0, sl_pct=6.0, sl_type="fixed", spacing=2.5,
            max_orders=5, mult=1.5, base_size=0.001, timeframe="1h"):
    cfg = MagicMock()
    cfg.name = "bt-test"
    cfg.pair = "BTC/USD"
    cfg.timeframe = timeframe
    cfg.leverage.size = 1
    cfg.leverage.enabled = False
    cfg.take_profit.target_pct = tp_pct
    cfg.take_profit.indicator_confirm = None
    cfg.stop_loss.type = sl_type
    cfg.stop_loss.pct = sl_pct
    cfg.dca.max_orders = max_orders
    cfg.dca.order_spacing_pct = spacing
    cfg.dca.multiplier = mult
    cfg.dca.base_order_size = base_size
    cfg.dca.taker_fee = 0.0006
    cfg.entry.indicators = []  # geen indicators → altijd entry
    return cfg


def _engine_with_candles(candles, **kwargs):
    """Maak een engine met gegeven candles en geen indicators (altijd entry).

    Wraps the candle list in a per-timeframe dict since the backtest
    engine now expects candles_per_tf. The default bot timeframe in
    _config() is '1h'.
    """
    cfg = _config(**kwargs)
    return BacktestEngine(
        config=cfg,
        candles_per_tf={cfg.timeframe: candles},
        initial_balance_btc=0.1,
    )


def _make_candles(prices: list[float], base_ts: int = 1_700_000_000_000) -> list[BacktestCandle]:
    """Maak een lijst candles van een lijst sluitprijzen. high=close, low=close."""
    return [
        _candle(base_ts + i * 3_600_000, p, p * 1.001, p * 0.999, p)
        for i, p in enumerate(prices)
    ]


# ── BacktestCandle ────────────────────────────────────────────────────────────

class TestBacktestCandle:
    def test_dt_is_datetime(self):
        c = _candle(1_700_000_000_000, 80000, 81000, 79000, 80500)
        from datetime import datetime
        assert isinstance(c.dt, datetime)

    def test_fields_correct(self):
        c = _candle(1_000, 100, 110, 90, 105, 50)
        assert c.open == 100
        assert c.high == 110
        assert c.low == 90
        assert c.close == 105
        assert c.volume == 50


# ── BacktestEngine: entry logica ──────────────────────────────────────────────

class TestEngineEntry:
    def test_no_entry_without_enough_candles(self):
        """Warmup vereist 78 candles — minder = geen entry."""
        candles = _make_candles([80000.0] * 50)
        e = _engine_with_candles(candles)
        result = e.run()
        assert result.total_deals == 0

    def test_entry_after_warmup(self):
        """Na 78 candles warmup moet een entry plaatsvinden (geen indicators)."""
        candles = _make_candles([80000.0] * 100)
        e = _engine_with_candles(candles)
        result = e.run()
        assert result.total_deals >= 1

    def test_no_double_entry(self):
        """Tweede entry mag niet plaatsvinden terwijl deal open is."""
        candles = _make_candles([80000.0] * 200)
        e = _engine_with_candles(candles)
        result = e.run()
        # Zonder TP/SL trigger blijft de deal open — maximaal 1 deal
        assert result.total_deals <= 1


# ── BacktestEngine: Take Profit ───────────────────────────────────────────────

class TestEngineTP:
    def test_tp_closes_deal(self):
        """Candle met high >= tp_price sluit de deal."""
        # 79 flate candles voor warmup, dan entry op candle 79 (prijs 80000)
        # Daarna een candle met high hoog genoeg voor TP (+3%)
        entry_price = 80000.0
        tp_price    = entry_price * 1.03  # 82400

        flat    = _make_candles([entry_price] * 79)
        tp_candle = _candle(
            flat[-1].timestamp + 3_600_000,
            entry_price, tp_price + 100, entry_price - 100, entry_price
        )
        candles = flat + [tp_candle]
        e = _engine_with_candles(candles, tp_pct=3.0)
        result = e.run()

        tp_deals = [d for d in result.closed_deals if d.close_reason == "tp"]
        assert len(tp_deals) >= 1

    def test_tp_price_is_correct(self):
        """TP sluit op tp_price, niet op candle high."""
        entry_price = 80000.0
        tp_price    = entry_price * 1.03

        flat      = _make_candles([entry_price] * 79)
        tp_candle = _candle(
            flat[-1].timestamp + 3_600_000,
            entry_price, tp_price + 500, entry_price - 100, entry_price
        )
        candles = flat + [tp_candle]
        e = _engine_with_candles(candles, tp_pct=3.0)
        result = e.run()

        tp_deals = [d for d in result.closed_deals if d.close_reason == "tp"]
        if tp_deals:
            assert abs(tp_deals[0].close_price - tp_price) < 0.01


# ── BacktestEngine: Stop Loss ─────────────────────────────────────────────────

class TestEngineSL:
    def test_fixed_sl_closes_deal(self):
        """Candle met low <= sl_price sluit de deal."""
        entry_price = 80000.0
        sl_price    = entry_price * (1 - 0.06)  # 75200

        flat     = _make_candles([entry_price] * 79)
        sl_candle = _candle(
            flat[-1].timestamp + 3_600_000,
            entry_price, entry_price + 100, sl_price - 100, entry_price
        )
        candles = flat + [sl_candle]
        e = _engine_with_candles(candles, sl_type="fixed", sl_pct=6.0, tp_pct=50.0)
        result = e.run()

        sl_deals = [d for d in result.closed_deals if d.close_reason == "sl"]
        assert len(sl_deals) >= 1


# ── BacktestEngine: DCA ───────────────────────────────────────────────────────

class TestEngineDCA:
    def test_dca_placed_on_drop(self):
        """Prijsdaling van > spacing_pct triggert een DCA order."""
        entry_price = 80000.0
        dca_price   = entry_price * (1 - 0.025)  # 78000 = -2.5%

        flat      = _make_candles([entry_price] * 79)
        dca_candle = _candle(
            flat[-1].timestamp + 3_600_000,
            entry_price, entry_price, dca_price - 100, dca_price
        )
        # Hoge TP zodat deal niet sluit tijdens DCA
        candles = flat + [dca_candle] + _make_candles([dca_price] * 20)
        # offset timestamp
        for i, c in enumerate(candles[80:], start=80):
            candles[i] = _candle(
                flat[-1].timestamp + i * 3_600_000,
                c.open, c.high, c.low, c.close
            )

        e = _engine_with_candles(candles, tp_pct=50.0, sl_pct=50.0, spacing=2.5)
        e.run()
        # Check of er deals zijn met meer dan 1 order
        all_closed = e.state.get_closed_deals_snapshot()
        all_open   = list(e.state.get_open_deals_snapshot().values())
        combined   = all_closed + all_open
        dca_deals  = [d for d in combined if d.dca_count > 0]
        assert len(dca_deals) >= 1

    def test_max_orders_respected(self):
        """DCA plaatst niet meer orders dan max_orders - 1."""
        entry_price = 80000.0
        candles = _make_candles([entry_price] * 79)
        # Voeg candles toe met steeds lagere prijzen
        for i in range(10):
            price = entry_price * (1 - (i + 1) * 0.025)
            ts    = candles[-1].timestamp + 3_600_000
            candles.append(_candle(ts, price, price, price * 0.99, price))

        e = _engine_with_candles(candles, tp_pct=50.0, sl_pct=50.0,
                                 spacing=2.5, max_orders=3)
        e.run()

        all_open = e.state.get_open_deals_snapshot()
        for deal in all_open.values():
            assert deal.dca_count <= 2  # max 3 orders = 1 base + 2 DCA


# ── BacktestEngine: fees ──────────────────────────────────────────────────────

class TestEngineFees:
    def test_fees_are_paid(self):
        """
        Fees worden berekend bij elke entry en exit.
        Bij kleine posities (0.001 BTC) zijn fees zo klein (~7e-12 BTC)
        dat ze na round(..., 10) nul worden. We verifiëren daarom dat de
        engine de fee-berekening aanroept, niet dat het eindgetal > 0 is.
        In plaats daarvan: gebruik een grote positiegrootte zodat fees meetbaar zijn.
        """
        entry_price = 80000.0
        tp_price    = entry_price * 1.03

        flat      = _make_candles([entry_price] * 79)
        tp_candle = _candle(
            flat[-1].timestamp + 3_600_000,
            entry_price, tp_price + 100, entry_price - 100, entry_price
        )
        candles = flat + [tp_candle]
        # Grote positiegrootte zodat fees meetbaar zijn (1 BTC = 1 contract per $1)
        e = _engine_with_candles(candles, tp_pct=3.0, base_size=100.0)
        result = e.run()

        assert result.total_deals >= 1
        assert result.fees_paid_btc > 0

    def test_final_balance_accounts_for_fees(self):
        """Eindbalans = beginbalans + PnL - fees."""
        candles = _make_candles([80000.0] * 100)
        e = _engine_with_candles(candles)
        result = e.run()
        expected = round(
            result.initial_balance_btc + result.total_pnl_btc - result.fees_paid_btc, 8
        )
        assert abs(result.final_balance_btc - expected) < 1e-7


# ── BacktestResult statistieken ───────────────────────────────────────────────

class TestBacktestResult:
    def _make_result(self, pnls: list[float]):
        from datetime import datetime, UTC
        from paper.paper_state import PaperDeal, PaperOrder

        deals = []
        for i, pnl in enumerate(pnls):
            order = PaperOrder(order_number=1, price=80000.0, size=0.001,
                               timestamp=datetime.now(UTC), order_type="base")
            deal = PaperDeal(
                id=f"BT-{i:04d}", bot_name="bt", symbol="BTC/USD",
                side="long", leverage=1, orders=[order],
                is_open=False, pnl_btc=pnl, pnl_pct=pnl * 100,
                close_reason="tp" if pnl > 0 else "sl",
            )
            deals.append(deal)

        cfg = _config()
        return BacktestResult(
            config=cfg,
            candles_total=1000,
            candles_processed=922,
            initial_balance_btc=0.1,
            final_balance_btc=0.1 + sum(pnls),
            closed_deals=deals,
            fees_paid_btc=0.0,
        )

    def test_win_rate_all_wins(self):
        result = self._make_result([0.001, 0.002, 0.003])
        assert result.win_rate == 100.0

    def test_win_rate_all_losses(self):
        result = self._make_result([-0.001, -0.002])
        assert result.win_rate == 0.0

    def test_win_rate_mixed(self):
        result = self._make_result([0.001, -0.001, 0.001, -0.001])
        assert result.win_rate == 50.0

    def test_total_pnl(self):
        result = self._make_result([0.001, 0.002, -0.001])
        assert abs(result.total_pnl_btc - 0.002) < 1e-9

    def test_best_worst_deal(self):
        result = self._make_result([0.005, -0.003, 0.001])
        assert result.best_deal_btc == 0.005
        assert result.worst_deal_btc == -0.003

    def test_max_drawdown_no_deals(self):
        result = self._make_result([])
        assert result.max_drawdown_pct == 0.0

    def test_tp_sl_count(self):
        result = self._make_result([0.001, -0.001, 0.002])
        assert result.tp_count == 2
        assert result.sl_count == 1

    def test_to_dict_has_required_keys(self):
        result = self._make_result([0.001])
        d = result.to_dict()
        for key in ["total_pnl_btc", "win_rate", "total_deals", "max_drawdown_pct"]:
            assert key in d
