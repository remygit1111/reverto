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
    - Driving timeframe = config.timeframe (bot-level)
    - Per tick wordt closes_per_tf gereconstrueerd: voor elke timeframe
      die de indicator engine nodig heeft bouwen we de lijst met
      afgesloten closes tot aan de timestamp van de huidige driving
      candle. Pointer-based walk, O(N) totaal per timeframe.
    - Checkt intra-candle high/low voor TP en SL (realistischer dan
      alleen close)
    - Past taker fees toe op elke entry en exit
    - Hergebruikt exact dezelfde DCA/TP/SL logica als de paper engine
    """

    def __init__(
        self,
        config: BotConfig,
        candles_per_tf: dict[str, list[BacktestCandle]],
        initial_balance_btc: float = 0.1,
    ):
        self.config           = config
        self.candles_per_tf   = candles_per_tf
        self.initial_balance  = initial_balance_btc
        self.taker_fee        = config.dca.taker_fee
        self.state            = PaperState(initial_balance_btc)
        self.indicator_engine = IndicatorEngine(config)

        self.bot_timeframe = config.timeframe
        required = self.indicator_engine.required_timeframes(self.bot_timeframe)
        missing = [tf for tf in required if tf not in candles_per_tf]
        if missing:
            raise ValueError(
                f"BacktestEngine: missing candles for timeframes {missing}. "
                f"Required: {sorted(required)}, provided: {sorted(candles_per_tf.keys())}"
            )
        if self.bot_timeframe not in candles_per_tf:
            raise ValueError(
                f"BacktestEngine: driving timeframe {self.bot_timeframe!r} "
                f"not in candles_per_tf"
            )

        self.driving_candles = candles_per_tf[self.bot_timeframe]

        # Pointer per tf tracking how many candles closed BEFORE the
        # current driving-candle timestamp. Advanced in _process_candle.
        self._tf_pointers: dict[str, int] = {tf: 0 for tf in candles_per_tf}

        # Statistieken
        self._candles_processed = 0
        self._fees_paid_btc     = 0.0

        # Per-tick OHLC slice cache — _process_candle refreshes these
        # every candle so _check_tp_sl_intracandle() can feed TP
        # indicator groups without the caller having to thread five
        # extra arguments through the call chain.
        self._closes_per_tf: dict[str, list[float]] = {}
        self._highs_per_tf:  dict[str, list[float]] = {}
        self._lows_per_tf:   dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Hoofdloop
    # ------------------------------------------------------------------

    def run(self) -> "BacktestResult":
        """
        Voer de backtest uit over alle driving-timeframe candles.
        Retourneert een BacktestResult met alle statistieken.
        """
        logger.info(
            f"Backtest gestart: {self.config.name} | "
            f"{len(self.driving_candles)} driving candles ({self.bot_timeframe}) | "
            f"timeframes: {sorted(self.candles_per_tf.keys())} | "
            f"balance: {self.initial_balance} BTC"
        )

        # Minimale warm-up voor indicators (MACD vereist 3*26=78 candles)
        warmup = 78

        for i, candle in enumerate(self.driving_candles):
            if i < warmup:
                continue  # wacht tot indicators betrouwbaar zijn

            ohlc = self._ohlc_up_to(candle.timestamp)
            self._process_candle(candle, *ohlc)
            self._candles_processed += 1

        # Sluit alle nog openstaande deals op de slotprijs van de laatste candle
        if self.driving_candles:
            last_price = self.driving_candles[-1].close
            for deal_id in list(self.state.open_deals.keys()):
                self._close_deal(
                    deal_id, last_price, "end_of_data",
                    exit_trigger={"type": "timeout"},
                )

        return self._build_result()

    def _ohlc_up_to(
        self, cur_ts: int
    ) -> tuple[dict[str, list[float]], dict[str, list[float]], dict[str, list[float]]]:
        """Return (closes, highs, lows) per tf for candles that closed
        strictly before `cur_ts`. Uses a persistent pointer per tf so
        the total walk is O(N) across the whole backtest.

        Three dicts are returned so OHLC-native indicators (Supertrend,
        Market Structure, etc.) can access high/low alongside close.
        """
        closes: dict[str, list[float]] = {}
        highs:  dict[str, list[float]] = {}
        lows:   dict[str, list[float]] = {}
        opens:  dict[str, list[float]] = {}
        for tf, candles in self.candles_per_tf.items():
            ptr = self._tf_pointers[tf]
            while ptr < len(candles) and candles[ptr].timestamp < cur_ts:
                ptr += 1
            self._tf_pointers[tf] = ptr
            window = candles[:ptr]
            closes[tf] = [c.close for c in window]
            highs[tf]  = [c.high  for c in window]
            lows[tf]   = [c.low   for c in window]
            opens[tf]  = [c.open  for c in window]
        return closes, highs, lows, opens

    # ------------------------------------------------------------------
    # Candle verwerking
    # ------------------------------------------------------------------

    def _process_candle(
        self,
        candle: BacktestCandle,
        closes_per_tf: dict[str, list[float]],
        highs_per_tf: dict[str, list[float]],
        lows_per_tf:  dict[str, list[float]],
        opens_per_tf: dict[str, list[float]] | None = None,
    ):
        """Verwerk één driving candle: check entry en monitor open deals."""
        close = candle.close

        # Hand the latest per-tf slices to the instance so
        # _check_tp_sl_intracandle can evaluate TP indicator groups
        # without re-threading these dicts through every helper.
        self._closes_per_tf = closes_per_tf
        self._highs_per_tf  = highs_per_tf
        self._lows_per_tf   = lows_per_tf

        # Monitor open deals — check TP/SL met intra-candle high/low
        for deal_id, deal in list(self.state.get_open_deals_snapshot().items()):
            closed = self._check_tp_sl_intracandle(deal, candle)
            if closed:
                continue
            if deal_id in self.state.open_deals:
                self._check_dca(deal, close)

        # Entry check — alleen als geen open deals
        if not self.state.open_deals and closes_per_tf.get(self.bot_timeframe):
            try:
                triggered, trigger_info = self.indicator_engine.check_entry_signal(
                    closes_per_tf, self.bot_timeframe,
                    highs_per_tf=highs_per_tf,
                    lows_per_tf=lows_per_tf,
                    opens_per_tf=opens_per_tf,
                )
                if triggered:
                    self._open_deal(close, candle.dt, entry_trigger=trigger_info)
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
        # Paper engine evalueert twee TP-paden naast elkaar: de prijs-TP
        # (wanneer price_enabled) en de indicator-TP groepen (OR). De
        # backtest moet hetzelfde gedrag spiegelen, anders drift backtest
        # ≠ paper zodra een strategie indicator-gebaseerde TP gebruikt.
        tp_config = self.config.take_profit
        tp_enabled = getattr(tp_config, 'enabled', True)
        price_enabled = getattr(tp_config, 'price_enabled', True)

        price_hit = False
        tp_price = avg * (1 + tp_config.target_pct / 100)
        if tp_enabled and price_enabled and candle.high >= tp_price:
            price_hit = True

        indicator_hit = False
        indicator_info: dict | None = None
        tp_groups = getattr(tp_config, 'indicator_groups', []) or []
        if tp_enabled and tp_groups and self._closes_per_tf:
            try:
                indicator_hit, indicator_info = (
                    self.indicator_engine.check_tp_indicator_groups(
                        self._closes_per_tf, self.bot_timeframe,
                        highs_per_tf=self._highs_per_tf,
                        lows_per_tf=self._lows_per_tf,
                    )
                )
            except Exception as e:
                logger.debug(f"TP indicator group eval failed at {candle.dt}: {e}")

        if price_hit or indicator_hit:
            # Prefer the actual price-TP fill price when the price path
            # fired; fall back to the candle close for indicator-only
            # closes (we can't simulate slippage past an indicator line).
            if price_hit:
                fill_price = tp_price
                exit_trigger = {"type": "price_tp"}
            else:
                fill_price = candle.close
                exit_trigger = {
                    "type": "indicator_tp",
                    "group_name": (indicator_info or {}).get("group_name", ""),
                    "indicators": (indicator_info or {}).get("indicators", []),
                }
            self._close_deal(deal.id, fill_price, "tp", exit_trigger=exit_trigger)
            return True

        # ── Stop Loss ─────────────────────────────────────────────────
        if self.config.stop_loss.type == "none":
            return False

        sl_pct = self.config.stop_loss.pct

        if self.config.stop_loss.type == "trailing":
            if deal._peak_price == 0.0:
                deal._peak_price = candle.open
            deal._peak_price = max(deal._peak_price, candle.high)
            sl_price = deal._peak_price * (1 - sl_pct / 100)
            sl_type_tag = "trailing_sl"
        else:
            sl_price = avg * (1 - sl_pct / 100)
            sl_type_tag = "price_sl"

        if candle.low <= sl_price:
            self._close_deal(
                deal.id, sl_price, "sl",
                exit_trigger={"type": sl_type_tag},
            )
            return True

        return False

    def _check_dca(self, deal: PaperDeal, price: float):
        """Voeg een DCA order toe als de prijs genoeg gedaald is."""
        if not getattr(self.config.dca, 'enabled', True):
            return
        # max_orders=0 means "base order only, never DCA".
        if self.config.dca.max_orders <= 1:
            return
        if deal.dca_count >= self.config.dca.max_orders - 1:
            return

        last_price   = deal.orders[-1].price
        step = self.config.dca.order_spacing_pct * (
            self.config.dca.step_scale ** deal.dca_count
        )
        next_dca     = last_price * (1 - step / 100)

        if price <= next_dca:
            multiplier = self.config.dca.multiplier ** deal.dca_count
            dca_size   = round(self.config.dca.base_order_size * multiplier, 8)
            fee        = self._calc_fee(dca_size)

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

    def _open_deal(
        self, price: float, dt: datetime, entry_trigger: dict | None = None,
    ):
        """Open een nieuw deal op de gegeven prijs."""
        deal_id    = self.state.new_deal_id()
        size       = self.config.dca.base_order_size
        fee        = self._calc_fee(size)

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
            entry_trigger=entry_trigger if isinstance(entry_trigger, dict) else None,
        )
        self.state.open_deal(deal)
        self.state.balance_btc  -= fee
        self._fees_paid_btc     += fee

        logger.debug(f"Deal geopend: {deal_id} @ ${price:,.2f} | fee: {fee:.8f} BTC")

    def _close_deal(
        self, deal_id: str, price: float, reason: str,
        exit_trigger: dict | None = None,
    ):
        """Sluit een deal en verwerk de exit fee.

        `exit_trigger` is a structured dict describing the type of exit
        (price_tp / indicator_tp / price_sl / trailing_sl / timeout) so
        backtest reports can show why each deal closed — mirroring the
        paper engine's PaperDeal.exit_trigger field.
        """
        deal = self.state.open_deals.get(deal_id)
        if not deal:
            return

        if isinstance(exit_trigger, dict):
            deal.exit_trigger = exit_trigger
        elif deal.exit_trigger is None:
            # Fall-through fallback so reports always have something to show.
            deal.exit_trigger = {"type": reason}

        fee = self._calc_fee(deal.total_size)
        self.state.close_deal(deal_id, price, reason)
        self.state.balance_btc  -= fee
        self._fees_paid_btc     += fee

        closed = self.state.closed_deals[-1]
        logger.debug(
            f"Deal gesloten: {deal_id} | reden: {reason} | "
            f"PnL: {closed.pnl_btc:+.6f} BTC | fee: {fee:.8f} BTC"
        )

    def _calc_fee(self, size: float) -> float:
        """Bereken de taker fee voor één order (in BTC, inverse contract)."""
        return round(size * self.taker_fee, 10)

    # ------------------------------------------------------------------
    # Resultaat
    # ------------------------------------------------------------------

    def _build_result(self) -> "BacktestResult":
        from backtest.backtest_report import BacktestResult
        return BacktestResult(
            config=self.config,
            candles_total=len(self.driving_candles),
            candles_processed=self._candles_processed,
            initial_balance_btc=self.initial_balance,
            final_balance_btc=self.state.balance_btc,
            closed_deals=self.state.get_closed_deals_snapshot(),
            fees_paid_btc=self._fees_paid_btc,
        )
