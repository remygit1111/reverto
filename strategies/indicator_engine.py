# strategies/indicator_engine.py
# Combines all indicators to produce entry and TP confirmation signals.
# Reads indicator configuration from the bot YAML config.

import logging
from config.models import BotConfig, IndicatorConfig
from strategies.indicators.rsi import calculate_rsi, check_rsi_signal
from strategies.indicators.ema import calculate_ema, check_ema_cross_signal
from strategies.indicators.macd import calculate_macd, check_macd_signal

logger = logging.getLogger(__name__)


class IndicatorEngine:
    """
    Evaluates all configured indicators and returns entry/TP signals.
    All configured indicators must agree for a signal to be True.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        # Copy the list at init time so mutations to config after init
        # do not affect the engine's indicator list
        self.entry_indicators = list(config.entry.indicators)

    def check_entry_signal(self, closes: list[float]) -> bool:
        """
        Check all configured entry indicators.
        Returns True only if ALL indicators confirm an entry signal.
        Logs each indicator result for transparency.
        """
        if not self.entry_indicators:
            logger.debug("No entry indicators configured — signal always True")
            return True

        results = []

        for indicator in self.entry_indicators:
            result = self._evaluate_indicator(indicator, closes)
            results.append(result)
            logger.info(
                f"Indicator {indicator.type} → {'✅ SIGNAL' if result else '❌ no signal'}"
            )

        all_confirmed = all(results)
        logger.info(
            f"Entry signal: {'✅ CONFIRMED' if all_confirmed else '❌ NOT confirmed'} "
            f"({sum(results)}/{len(results)} indicators agree)"
        )
        return all_confirmed

    def check_tp_confirmation(self, closes: list[float]) -> bool:
        """
        Check take profit confirmation indicator if configured.
        Returns True if confirmed or if no TP indicator is configured.
        """
        tp_indicator = self.config.take_profit.indicator_confirm
        if not tp_indicator:
            return True

        confirmed = check_macd_signal(closes, tp_indicator)
        logger.info(f"TP confirmation ({tp_indicator}): {'✅' if confirmed else '❌'}")
        return confirmed

    def get_indicator_snapshot(self, closes: list[float]) -> dict:
        """
        Returns current values of all indicators for logging and dashboard.
        Failures are logged as warnings instead of silently ignored,
        so indicator bugs are visible in production logs.
        """
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

    def _evaluate_indicator(self, indicator: IndicatorConfig,
                             closes: list[float]) -> bool:
        """
        Route indicator config to the correct check function.
        Returns False (not True) on unknown indicator type to prevent
        unvalidated entries from slipping through.
        """
        itype = indicator.type.upper()

        if itype == "RSI":
            return check_rsi_signal(
                closes,
                period=indicator.period or 14,
                threshold=indicator.threshold or "below_35"
            )
        elif itype == "EMA_CROSS":
            return check_ema_cross_signal(
                closes,
                fast=indicator.fast or 9,
                slow=indicator.slow or 21,
                signal=indicator.signal or "bullish"
            )
        elif itype == "MACD":
            return check_macd_signal(
                closes,
                condition=indicator.threshold or "histogram_positive"
            )
        else:
            logger.warning(
                f"Unknown indicator type: '{indicator.type}' — "
                f"returning False to block entry. Check your YAML config."
            )
            return False  # Block entry on unknown indicator, do not silently allow
