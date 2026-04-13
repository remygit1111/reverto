# backtest/backtest_engine.py
# Simuleert de trading strategie op historische OHLCV data.
# Hergebruikt PaperState, PaperDeal en IndicatorEngine exact zoals de paper engine.
# Geen exchange calls, geen Telegram, geen sleep — zo snel mogelijk.

import logging
from datetime import datetime, UTC
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backtest.backtest_report import BacktestResult

from config.models import BotConfig
from paper.paper_state import PaperState, PaperDeal, PaperOrder
from strategies.indicator_engine import IndicatorEngine

logger = logging.getLogger(__name__)

# Taker fee voor Bitget inverse perpetual (0.06%)
DEFAULT_TAKER_FEE = 0.0006


@dataclass
class BacktestCandle:
    """Eén OHLCV candle voor de backtest."""
    timestamp: int    # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp / 1000, tz=UTC)


class BacktestEngine:
    """
    Backtester voor Reverto.

    Werkt candle-voor-candle door historische data:
    - Gebruikt de close prijs als simulatieprijs (conservatief)
    - Checkt intra-candle high/low voor TP en SL (realistischer dan alleen close)
    - Past taker fees toe op elke entry en exit
    - Hergebruikt exact dezelfde DCA/TP/SL logica als de paper engine
    """

    def __init__(
        self,
        config: BotConfig,
        candles: list[BacktestCandle],
        initial_balance_btc: float = 0.1,
        taker_fee: float = DEFAULT_TAKER_FEE,
    ):
        self.config           = config
        self.candles          = candles
        self.initial_balance  = initial_balance_btc
        self.taker_fee        = taker_fee
        self.state            = PaperState(initial_balance_btc)
        self.indicator_engine = IndicatorEngine(config)

        # Statistieken
        self._candles_processed = 0
        self._fees_paid_btc     = 0.0

    # ------------------------------------------------------------------
    # Hoofdloop
    # ------------------------------------------------------------------

    def run(self) -> "BacktestResult":
        """
        Voer de backtest uit over alle candles.
        Retourneert een BacktestResult met alle statistieken.
        """
        logger.info(
            f"Backtest gestart: {self.config.name} | "
            f"{len(self.candles)} candles | "
            f"balance: {self.initial_balance} BTC"
        )

        # Minimale warm-up voor indicators (MACD vereist 3*26=78 candles)
        warmup = 78

        for i, candle in enumerate(self.candles):
            if i < warmup:
                continue  # wacht tot indicators betrouwbaar zijn

            # Sluitprijzen tot en met huidige candle (exclusief huidige = completed)
            closes = [c.close for c in self.candles[:i]]

            self._process_candle(candle, closes)
            self._candles_processed += 1

        # Sluit alle nog openstaande deals op de slotprijs van de laatste candle
        if self.candles:
            last_price = self.candles[-1].close
            for deal_id in list(self.state.open_deals.keys()):
                self._close_deal(deal_id, last_price, "end_of_data")

        return self._build_result()

    # ------------------------------------------------------------------
    # Candle verwerking
    # ------------------------------------------------------------------

    def _process_candle(self, candle: BacktestCandle, closes: list[float]):
        """Verwerk één candle: check entry en monitor open deals."""
        close = candle.close

        # Monitor open deals — check TP/SL met intra-candle high/low
        for deal_id, deal in list(self.state.get_open_deals_snapshot().items()):
            closed = self._check_tp_sl_intracandle(deal, candle)
            if closed:
                continue
            if deal_id in self.state.open_deals:
                self._check_dca(deal, close)

        # Entry check — alleen als geen open deals
        if not self.state.open_deals and closes:
            try:
                if self.indicator_engine.check_entry_signal(closes):
                    self._open_deal(close, candle.dt)
            except Exception as e:
                logger.debug(f"Entry check fout op candle {candle.dt}: {e}")

    def _check_tp_sl_intracandle(self, deal: PaperDeal, candle: BacktestCandle) -> bool:
        """
        Check TP en SL met intra-candle high en low.
        Realistischer dan alleen close — de prijs passeerde het niveau binnen de candle.
        Retourneert True als de deal gesloten is.
        """
        avg = deal.avg_entry_price

        # ── Take Profit ───────────────────────────────────────────────
        tp_price = avg * (1 + self.config.take_profit.target_pct / 100)
        if candle.high >= tp_price:
            # TP geraakt — sluit op tp_price (conservatief: niet op high)
            self._close_deal(deal.id, tp_price, "tp")
            return True

        # ── Stop Loss ─────────────────────────────────────────────────
        sl_pct = self.config.stop_loss.pct

        if self.config.stop_loss.type == "trailing":
            if deal._peak_price == 0.0:
                deal._peak_price = candle.open
            deal._peak_price = max(deal._peak_price, candle.high)
            sl_price = deal._peak_price * (1 - sl_pct / 100)
        else:
            sl_price = avg * (1 - sl_pct / 100)

        if candle.low <= sl_price:
            self._close_deal(deal.id, sl_price, "sl")
            return True

        return False

    def _check_dca(self, deal: PaperDeal, price: float):
        """Voeg een DCA order toe als de prijs genoeg gedaald is."""
        if deal.dca_count >= self.config.dca.max_orders - 1:
            return

        last_price   = deal.orders[-1].price
        next_dca     = last_price * (1 - self.config.dca.order_spacing_pct / 100)

        if price <= next_dca:
            multiplier = self.config.dca.multiplier ** deal.dca_count
            dca_size   = round(self.config.dca.base_order_size * multiplier, 8)
            fee        = self._calc_fee(price, dca_size)

            dca_order = PaperOrder(
                order_number=deal.dca_count + 2,
                price=price,
                size=dca_size,
                timestamp=datetime.now(UTC),
                order_type="dca",
            )
            deal.orders.append(dca_order)
            self.state.balance_btc   -= fee
            self._fees_paid_btc      += fee

            logger.debug(f"DCA #{dca_order.order_number} bij ${price:,.2f} | fee: {fee:.8f} BTC")

    def _open_deal(self, price: float, dt: datetime):
        """Open een nieuw deal op de gegeven prijs."""
        deal_id    = self.state.new_deal_id()
        size       = self.config.dca.base_order_size
        fee        = self._calc_fee(price, size)

        order = PaperOrder(
            order_number=1,
            price=price,
            size=size,
            timestamp=dt,
            order_type="base",
        )
        deal = PaperDeal(
            id=deal_id,
            bot_name=self.config.name,
            symbol=self.config.pair,
            side="long",
            leverage=self.config.leverage.size,
            orders=[order],
            opened_at=dt,
        )
        self.state.open_deal(deal)
        self.state.balance_btc  -= fee
        self._fees_paid_btc     += fee

        logger.debug(f"Deal geopend: {deal_id} @ ${price:,.2f} | fee: {fee:.8f} BTC")

    def _close_deal(self, deal_id: str, price: float, reason: str):
        """Sluit een deal en verwerk de exit fee."""
        deal = self.state.open_deals.get(deal_id)
        if not deal:
            return

        fee = self._calc_fee(price, deal.total_size)
        self.state.close_deal(deal_id, price, reason)
        self.state.balance_btc  -= fee
        self._fees_paid_btc     += fee

        closed = self.state.closed_deals[-1]
        logger.debug(
            f"Deal gesloten: {deal_id} | reden: {reason} | "
            f"PnL: {closed.pnl_btc:+.6f} BTC | fee: {fee:.8f} BTC"
        )

    def _calc_fee(self, price: float, size: float) -> float:
        """Bereken de taker fee voor één order (in BTC, inverse contract)."""
        # Voor inverse perpetual: fee = size / price * fee_rate
        return round((size / price) * self.taker_fee, 10)

    # ------------------------------------------------------------------
    # Resultaat
    # ------------------------------------------------------------------

    def _build_result(self) -> "BacktestResult":
        from backtest.backtest_report import BacktestResult
        return BacktestResult(
            config=self.config,
            candles_total=len(self.candles),
            candles_processed=self._candles_processed,
            initial_balance_btc=self.initial_balance,
            final_balance_btc=self.state.balance_btc,
            closed_deals=self.state.get_closed_deals_snapshot(),
            fees_paid_btc=self._fees_paid_btc,
        )
