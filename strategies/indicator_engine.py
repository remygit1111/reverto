# strategies/indicator_engine.py
# Combines all indicators to produce entry and TP confirmation signals.
#
# Each indicator can declare its own `timeframe` override in the YAML
# config. The engine receives `closes_per_tf: dict[str, list[float]]`
# (close prices per timeframe bucket) and a `bot_timeframe` fallback,
# then routes each indicator to the right bucket. Indicators whose
# timeframe data is missing cause the engine to block entry (safer
# default than silently skipping — an operator who configured a
# filter expects it to be enforced).

import logging
from config.models import BotConfig, IndicatorConfig
from strategies.indicators.rsi import calculate_rsi, check_rsi_signal
from strategies.indicators.ema import calculate_ema, check_ema_cross_signal
from strategies.indicators.macd import calculate_macd, check_macd_signal
from strategies.indicators.bollinger import check_bollinger_signal
from strategies.indicators.parabolic_sar import check_parabolic_sar_signal
from strategies.indicators.supertrend import check_supertrend_signal
from strategies.indicators.market_structure import check_market_structure_signal
from strategies.indicators.support_resistance import check_support_resistance_signal
from strategies.indicators.qfl import check_qfl_signal

logger = logging.getLogger(__name__)


class IndicatorEngine:
    """
    Evaluates all configured indicators and returns entry/TP signals.
    All configured indicators must agree for a signal to be True.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        # Copy the list at init time so mutations to config after init
        # do not affect the engine's indicator list.
        self.entry_indicators = list(config.entry.indicators)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _closes_for(
        self,
        closes_per_tf: dict[str, list[float]],
        timeframe: str,
        context: str,
    ) -> list[float] | None:
        """Return closes for `timeframe` or None if missing. Logs a warning."""
        closes = closes_per_tf.get(timeframe)
        if not closes:
            logger.warning(
                "%s: timeframe %r not available in closes_per_tf (keys=%s) "
                "— indicator cannot be evaluated",
                context, timeframe, sorted(closes_per_tf.keys()),
            )
            return None
        return closes

    def required_timeframes(self, bot_timeframe: str) -> set[str]:
        """Set of timeframes the engine needs closes for.

        Used by the paper engine to decide which candle buckets to fetch,
        and by the backtest loader to validate that the caller supplied
        all required tf data.
        """
        tfs = {bot_timeframe}
        for ind in self.entry_indicators:
            tfs.add(ind.timeframe or bot_timeframe)
        return tfs

    # ------------------------------------------------------------------
    # Entry signal
    # ------------------------------------------------------------------

    def check_entry_signal(
        self,
        closes_per_tf: dict[str, list[float]],
        bot_timeframe: str,
        highs_per_tf: dict[str, list[float]] | None = None,
        lows_per_tf: dict[str, list[float]] | None = None,
        opens_per_tf: dict[str, list[float]] | None = None,
    ) -> bool:
        """
        Check all configured entry indicators.
        Returns True only if ALL indicators confirm an entry signal.
        If any indicator's timeframe data is missing we return False —
        we refuse to enter a deal while a configured filter can't be
        evaluated (fail-closed).

        highs_per_tf / lows_per_tf are optional OHLC plumbing used by
        indicators that need high/low data (e.g. Supertrend). When
        omitted, those indicators fail-closed.
        """
        if not self.entry_indicators:
            logger.debug("No entry indicators configured — signal always True")
            return True

        # ASAP short-circuit: if any configured entry indicator is ASAP,
        # the entry signal is unconditionally True. Useful for manual
        # trigger strategies where the operator wants to bypass filters.
        for ind in self.entry_indicators:
            if ind.type.upper() == "ASAP":
                logger.info("Entry signal: ASAP indicator present — bypassing all filters")
                return True

        results = []
        for indicator in self.entry_indicators:
            tf = indicator.timeframe or bot_timeframe
            closes = self._closes_for(
                closes_per_tf, tf, f"Entry indicator {indicator.type}"
            )
            if closes is None:
                return False  # fail-closed
            highs = (highs_per_tf or {}).get(tf)
            lows  = (lows_per_tf  or {}).get(tf)
            opens = (opens_per_tf or {}).get(tf)
            result = self._evaluate_indicator(indicator, closes, highs, lows, opens)
            results.append(result)
            logger.info(
                f"Indicator {indicator.type}@{tf} → "
                f"{'✅ SIGNAL' if result else '❌ no signal'}"
            )

        all_confirmed = all(results)
        logger.info(
            f"Entry signal: {'✅ CONFIRMED' if all_confirmed else '❌ NOT confirmed'} "
            f"({sum(results)}/{len(results)} indicators agree)"
        )
        return all_confirmed

    # ------------------------------------------------------------------
    # TP confirmation
    # ------------------------------------------------------------------

    def check_tp_confirmation(
        self,
        closes_per_tf: dict[str, list[float]],
        bot_timeframe: str,
    ) -> bool:
        """
        Check take profit confirmation indicator if configured.
        Returns True if no TP indicator is configured, or if confirmed.
        Fail-closed on missing timeframe data — we prefer holding the
        position over closing with an unvalidated signal.
        """
        tp_indicator = self.config.take_profit.indicator_confirm
        if not tp_indicator:
            return True

        closes = self._closes_for(
            closes_per_tf, bot_timeframe, "TP confirmation"
        )
        if closes is None:
            return False
        confirmed = check_macd_signal(closes, tp_indicator)
        logger.info(f"TP confirmation ({tp_indicator}): {'✅' if confirmed else '❌'}")
        return confirmed

    # ------------------------------------------------------------------
    # Dashboard snapshot
    # ------------------------------------------------------------------

    def get_indicator_snapshot(
        self,
        closes_per_tf: dict[str, list[float]],
        bot_timeframe: str,
    ) -> dict:
        """
        Returns current indicator values on the bot's primary timeframe
        for the dashboard. Indicators on overridden timeframes are NOT
        currently shown separately — they'd need a multi-tf UI.
        """
        closes = closes_per_tf.get(bot_timeframe)
        if not closes:
            return {}

        snapshot = {}
        try:
            snapshot["rsi_14"] = calculate_rsi(closes, 14)
        except Exception as e:
            logger.warning(f"RSI calculation failed: {e}")
        try:
            snapshot["ema_9"] = calculate_ema(closes, 9)
            snapshot["ema_21"] = calculate_ema(closes, 21)
        except Exception as e:
            logger.warning(f"EMA calculation failed: {e}")
        try:
            macd = calculate_macd(closes)
            snapshot["macd"] = macd["macd"]
            snapshot["macd_signal"] = macd["signal"]
            snapshot["macd_histogram"] = macd["histogram"]
        except Exception as e:
            logger.warning(f"MACD calculation failed: {e}")
        return snapshot

    # ------------------------------------------------------------------
    # Per-indicator dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_price_source(
        source: str | None,
        closes: list[float],
        highs: list[float] | None,
        lows: list[float] | None,
        opens: list[float] | None,
    ) -> list[float]:
        if not source or source == "close":
            return closes
        if source == "high" and highs:
            return highs
        if source == "low" and lows:
            return lows
        if source == "open" and opens:
            return opens
        if source == "hl2" and highs and lows:
            return [(h + lo) / 2 for h, lo in zip(highs, lows)]
        if source == "hlc3" and highs and lows:
            return [(h + lo + c) / 3 for h, lo, c in zip(highs, lows, closes)]
        if source == "ohlc4" and opens and highs and lows:
            return [(o + h + lo + c) / 4 for o, h, lo, c in zip(opens, highs, lows, closes)]
        return closes

    def _evaluate_indicator(
        self,
        indicator: IndicatorConfig,
        closes: list[float],
        highs: list[float] | None = None,
        lows: list[float] | None = None,
        opens: list[float] | None = None,
    ) -> bool:
        """
        Route indicator config to the correct check function.
        Returns False on unknown indicator type to prevent unvalidated
        entries from slipping through.
        """
        itype = indicator.type.upper()

        if itype == "ASAP":
            # ASAP always fires. check_entry_signal short-circuits before
            # reaching here, but we keep this branch so a direct call to
            # _evaluate_indicator stays consistent.
            return True
        if itype == "RSI":
            src = self._resolve_price_source(
                indicator.price_source, closes, highs, lows, opens)
            return check_rsi_signal(
                src,
                period=indicator.period or 14,
                threshold=indicator.threshold or "below_35",
            )
        elif itype == "EMA_CROSS":
            return check_ema_cross_signal(
                closes,
                fast=indicator.fast or 9,
                slow=indicator.slow or 21,
                signal=indicator.signal or "bullish",
            )
        elif itype == "MACD":
            return check_macd_signal(
                closes,
                condition=indicator.condition or indicator.threshold or "histogram_positive",
            )
        elif itype == "BOLLINGER":
            return check_bollinger_signal(
                closes,
                period=indicator.period or 20,
                multiplier=indicator.multiplier or 2.0,
                condition=indicator.condition or "price_below_lower",
                ma_type=indicator.ma_type or "SMA",
                value=indicator.value or "lower",
            )
        elif itype == "PARABOLIC_SAR":
            return check_parabolic_sar_signal(
                closes,
                initial_af=indicator.initial_af or 0.02,
                max_af=indicator.max_af or 0.20,
                condition=indicator.condition or "bullish",
            )
        elif itype == "SUPERTREND":
            if highs is None or lows is None:
                logger.warning(
                    "Supertrend needs high/low data but none provided — blocking entry"
                )
                return False
            return check_supertrend_signal(
                highs, lows, closes,
                atr_period=indicator.atr_period or 10,
                multiplier=indicator.multiplier or 3.0,
                condition=indicator.condition or "bullish",
            )
        elif itype == "MARKET_STRUCTURE":
            return check_market_structure_signal(
                closes,
                lookback=indicator.lookback or 3,
                condition=indicator.condition or "bullish_bos",
            )
        elif itype == "SUPPORT_RESISTANCE":
            if not highs or not lows:
                logger.warning("SUPPORT_RESISTANCE requires highs/lows — "
                               "returning False (fail-closed)")
                return False
            return check_support_resistance_signal(
                highs, lows, closes,
                left_bars=indicator.left_bars or 15,
                right_bars=indicator.right_bars or 15,
                proximity_pct=indicator.proximity_pct or 1.0,
                condition=indicator.condition or "price_crossing_down",
                value=indicator.value or "resistance",
            )
        elif itype == "QFL":
            return check_qfl_signal(
                closes,
                lookback=indicator.lookback or 3,
                crack_pct=indicator.crack_pct or 3.0,
                base_candles=indicator.base_candles or 5,
                max_bases=indicator.max_bases or 5,
                below_pct=indicator.below_pct if indicator.below_pct is not None else 0.0,
                condition=indicator.condition or "below_base",
                base_periods=indicator.base_periods,
                pump_periods=indicator.pump_periods,
                pump_from_base_pct=indicator.pump_from_base_pct,
                base_crack_pct=indicator.base_crack_pct,
            )
        else:
            logger.warning(
                f"Unknown indicator type: '{indicator.type}' — "
                f"returning False to block entry. Check your YAML config."
            )
            return False
