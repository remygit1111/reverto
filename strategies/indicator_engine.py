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
from strategies.indicators.ema import calculate_ema
from strategies.indicators.macd import calculate_macd, check_macd_signal
from strategies.indicators.bollinger import (
    calculate_bollinger_bands, check_bollinger_signal,
)
from strategies.indicators.parabolic_sar import (
    calculate_parabolic_sar, check_parabolic_sar_signal,
)
from strategies.indicators.supertrend import (
    calculate_supertrend, check_supertrend_signal,
)
from strategies.indicators.market_structure import (
    _swing_points, check_market_structure_signal,
)
from strategies.indicators.support_resistance import (
    find_support_resistance, check_support_resistance_signal,
)
from strategies.indicators.qfl import calculate_qfl_series, check_qfl_signal

logger = logging.getLogger(__name__)


class IndicatorEngine:
    """
    Evaluates all configured indicators and returns entry/TP signals.
    All configured indicators must agree for a signal to be True.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.entry_indicators = list(config.entry.indicators)
        self.indicator_groups = list(config.entry.indicator_groups)

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

    def _all_indicators(self) -> list:
        """Flat list of all indicators across groups + legacy flat list."""
        inds = list(self.entry_indicators)
        for g in self.indicator_groups:
            inds.extend(g.indicators)
        return inds

    def _find_indicator_config(self, itype: str) -> IndicatorConfig | None:
        """Return the first matching IndicatorConfig across entry groups,
        the legacy flat entry list, and TP indicator groups. Used by
        ``get_indicator_snapshot`` so the per-tick log line reflects
        the parameters the bot ACTUALLY evaluates instead of hard-coded
        defaults. Returns None if the type isn't configured — callers
        fall back to sensible defaults so a snapshot is still produced
        even for indicators that aren't part of the strategy."""
        wanted = itype.upper()
        # Entry groups first — that's where most operators put filters.
        for g in self.indicator_groups:
            for ind in getattr(g, "indicators", []) or []:
                if ind.type.upper() == wanted:
                    return ind
        for ind in self.entry_indicators:
            if ind.type.upper() == wanted:
                return ind
        # TP groups — check last so an entry filter wins if both exist
        # with different params (unlikely, but deterministic).
        tp_groups = getattr(
            getattr(self.config, "take_profit", None),
            "indicator_groups", None,
        ) or []
        for g in tp_groups:
            for ind in getattr(g, "indicators", []) or []:
                if ind.type.upper() == wanted:
                    return ind
        return None

    def required_timeframes(self, bot_timeframe: str) -> set[str]:
        """Set of timeframes the engine needs closes for."""
        tfs = {bot_timeframe}
        for ind in self._all_indicators():
            tfs.add(ind.timeframe or bot_timeframe)
        return tfs

    # ------------------------------------------------------------------
    # Entry signal
    # ------------------------------------------------------------------

    def _eval_groups(
        self,
        groups,
        closes_per_tf: dict[str, list[float]],
        bot_timeframe: str,
        highs_per_tf: dict[str, list[float]] | None = None,
        lows_per_tf: dict[str, list[float]] | None = None,
        opens_per_tf: dict[str, list[float]] | None = None,
        context: str = "Entry",
    ) -> tuple[bool, dict | None]:
        """Evaluate indicator groups with AND/OR logic.

        Returns (triggered, trigger_info). trigger_info is a dict with
        group_id, group_name, indicators when triggered, else None.
        """
        for group in groups:
            if not hasattr(group, 'indicators') or not group.indicators:
                continue
            group_ok = True
            for indicator in group.indicators:
                tf = indicator.timeframe or bot_timeframe
                closes = self._closes_for(
                    closes_per_tf, tf,
                    f"{context} indicator {indicator.type}")
                if closes is None:
                    group_ok = False
                    break
                highs = (highs_per_tf or {}).get(tf)
                lows = (lows_per_tf or {}).get(tf)
                opens = (opens_per_tf or {}).get(tf)
                result = self._evaluate_indicator(
                    indicator, closes, highs, lows, opens)
                logger.info(
                    f"{context} {indicator.type}@{tf} → "
                    f"{'✅ SIGNAL' if result else '❌ no signal'}")
                if not result:
                    group_ok = False
                    break
            if group_ok:
                gid = getattr(group, 'id', 0)
                gname = getattr(group, 'name', '')
                trigger = {
                    "group_id": gid,
                    "group_name": gname,
                    "indicators": [
                        ind.type for ind in group.indicators
                    ],
                }
                logger.info(f"{context} signal: ✅ CONFIRMED (group {gname})")
                return True, trigger

        logger.info(f"{context} signal: ❌ NOT confirmed")
        return False, None

    def check_entry_signal(
        self,
        closes_per_tf: dict[str, list[float]],
        bot_timeframe: str,
        highs_per_tf: dict[str, list[float]] | None = None,
        lows_per_tf: dict[str, list[float]] | None = None,
        opens_per_tf: dict[str, list[float]] | None = None,
    ) -> tuple[bool, dict | None]:
        """Check entry indicators. Returns (triggered, trigger_info)."""
        all_inds = self._all_indicators()
        has_groups = bool(self.indicator_groups)
        if not all_inds and not has_groups:
            logger.debug("No entry indicators configured — signal always True")
            return True, None

        for ind in all_inds:
            if ind.type.upper() == "ASAP":
                logger.info("ASAP indicator present — bypassing all filters")
                return True, {"group_id": 0, "group_name": "ASAP",
                              "indicators": ["ASAP"]}

        groups_to_eval = []
        if self.indicator_groups:
            groups_to_eval = [g for g in self.indicator_groups if g.indicators]
        elif self.entry_indicators:
            class _LegacyGroup:
                def __init__(self, inds):
                    self.id = 1
                    self.name = "Default"
                    self.indicators = inds
            groups_to_eval = [_LegacyGroup(self.entry_indicators)]

        if not groups_to_eval:
            return False, None

        return self._eval_groups(
            groups_to_eval, closes_per_tf, bot_timeframe,
            highs_per_tf, lows_per_tf, opens_per_tf, "Entry")

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

    def check_tp_indicator_groups(
        self,
        closes_per_tf: dict[str, list[float]],
        bot_timeframe: str,
        highs_per_tf: dict[str, list[float]] | None = None,
        lows_per_tf: dict[str, list[float]] | None = None,
    ) -> tuple[bool, dict | None]:
        """Evaluate TP indicator groups. Returns (triggered, trigger_info)."""
        tp_groups = getattr(self.config.take_profit, 'indicator_groups', [])
        if not tp_groups:
            return False, None
        return self._eval_groups(
            tp_groups, closes_per_tf, bot_timeframe,
            highs_per_tf, lows_per_tf, context="TP")

    # ------------------------------------------------------------------
    # Dashboard snapshot
    # ------------------------------------------------------------------

    def get_indicator_snapshot(
        self,
        closes_per_tf: dict[str, list[float]],
        bot_timeframe: str,
        highs_per_tf: dict[str, list[float]] | None = None,
        lows_per_tf: dict[str, list[float]] | None = None,
    ) -> dict:
        """
        Returns current indicator values on the bot's primary timeframe
        for the dashboard + per-tick log line. Indicators on overridden
        timeframes are NOT currently shown separately — they'd need a
        multi-tf UI.

        Parameters mirror the operator's indicator-group config where
        available (BB period/multiplier, PSAR AF, Supertrend ATR etc.)
        via ``_find_indicator_config``. Unconfigured indicators fall
        back to sensible defaults so a bot without BOLLINGER still gets
        a bb_pct_b snapshot for the dashboard — that's advisory, never
        consulted by the signal evaluator.
        """
        closes = closes_per_tf.get(bot_timeframe)
        if not closes:
            return {}
        highs = (highs_per_tf or {}).get(bot_timeframe)
        lows = (lows_per_tf or {}).get(bot_timeframe)

        snapshot: dict = {}

        # ── Close-only indicators ────────────────────────────────────
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

        # Bollinger %B — normalised position of price inside the bands.
        # 0 = at lower band, 1 = at upper band. Can overshoot <0 / >1
        # when price pokes outside the envelope.
        bb_cfg = self._find_indicator_config("BOLLINGER")
        bb_period = (bb_cfg.period if bb_cfg and bb_cfg.period else 20)
        bb_multiplier = (bb_cfg.multiplier if bb_cfg and bb_cfg.multiplier else 2.0)
        bb_ma_type = (bb_cfg.ma_type if bb_cfg and bb_cfg.ma_type else "SMA")
        try:
            bands = calculate_bollinger_bands(
                closes, period=bb_period, multiplier=bb_multiplier,
                ma_type=bb_ma_type,
            )
            upper, lower = bands["upper"], bands["lower"]
            if upper > lower:
                snapshot["bb_pct_b"] = (closes[-1] - lower) / (upper - lower)
        except Exception as e:
            logger.debug(f"BB snapshot skipped: {e}")

        # Market structure — classify the most recent swing relative to
        # the prior one of the same type. HH/LH come from swing highs,
        # HL/LL from swing lows. Whichever swing is most recent wins.
        ms_cfg = self._find_indicator_config("MARKET_STRUCTURE")
        ms_lookback = (ms_cfg.lookback if ms_cfg and ms_cfg.lookback else 3)
        try:
            ms_highs, ms_lows = _swing_points(closes, lookback=ms_lookback)
            last_high = ms_highs[-1] if ms_highs else None
            last_low = ms_lows[-1] if ms_lows else None
            if last_high is not None and (
                last_low is None or last_high[0] > last_low[0]
            ):
                prev = ms_highs[-2] if len(ms_highs) >= 2 else None
                if prev is not None:
                    snapshot["market_structure"] = (
                        "HH" if last_high[1] > prev[1] else "LH"
                    )
            elif last_low is not None:
                prev = ms_lows[-2] if len(ms_lows) >= 2 else None
                if prev is not None:
                    snapshot["market_structure"] = (
                        "HL" if last_low[1] > prev[1] else "LL"
                    )
        except Exception as e:
            logger.debug(f"MarketStructure snapshot skipped: {e}")

        # ── HLC-backed indicators (need highs + lows too) ────────────
        if highs and lows and len(highs) == len(lows) == len(closes):
            psar_cfg = self._find_indicator_config("PARABOLIC_SAR")
            psar_initial = (
                psar_cfg.initial_af if psar_cfg and psar_cfg.initial_af
                else 0.02
            )
            psar_max = (
                psar_cfg.max_af if psar_cfg and psar_cfg.max_af else 0.20
            )
            try:
                psar_series = calculate_parabolic_sar(
                    highs, lows, closes,
                    initial_af=psar_initial, max_af=psar_max,
                )
                if psar_series:
                    sar, trend = psar_series[-1]
                    snapshot["psar"] = sar
                    snapshot["psar_trend"] = "bull" if trend == 1 else "bear"
            except Exception as e:
                logger.debug(f"PSAR snapshot skipped: {e}")

            st_cfg = self._find_indicator_config("SUPERTREND")
            st_atr = (
                st_cfg.atr_period if st_cfg and st_cfg.atr_period else 10
            )
            st_mult = (
                st_cfg.multiplier if st_cfg and st_cfg.multiplier else 3.0
            )
            try:
                st_series = calculate_supertrend(
                    highs, lows, closes,
                    atr_period=st_atr, multiplier=st_mult,
                )
                if st_series:
                    st_val, st_trend = st_series[-1]
                    snapshot["supertrend"] = st_val
                    snapshot["supertrend_dir"] = (
                        "up" if st_trend == 1 else "down"
                    )
            except Exception as e:
                logger.debug(f"Supertrend snapshot skipped: {e}")

            sr_cfg = self._find_indicator_config("SUPPORT_RESISTANCE")
            sr_left = (
                sr_cfg.left_bars if sr_cfg and sr_cfg.left_bars else 15
            )
            sr_right = (
                sr_cfg.right_bars if sr_cfg and sr_cfg.right_bars else 15
            )
            try:
                active_sup, active_res = find_support_resistance(
                    highs, lows, closes,
                    left_bars=sr_left, right_bars=sr_right,
                )
                if active_sup:
                    snapshot["sr_support"] = active_sup[0]
                if active_res:
                    snapshot["sr_resistance"] = active_res[0]
            except Exception as e:
                logger.debug(f"S&R snapshot skipped: {e}")

            qfl_cfg = self._find_indicator_config("QFL")
            qfl_base_periods = (
                qfl_cfg.base_periods if qfl_cfg and qfl_cfg.base_periods
                else 36
            )
            qfl_pump_periods = (
                qfl_cfg.pump_periods if qfl_cfg and qfl_cfg.pump_periods
                else 8
            )
            qfl_pump_pct = (
                qfl_cfg.pump_from_base_pct
                if qfl_cfg and qfl_cfg.pump_from_base_pct
                else 3.0
            )
            qfl_crack_pct = (
                qfl_cfg.base_crack_pct
                if qfl_cfg and qfl_cfg.base_crack_pct
                else 3.0
            )
            try:
                qfl = calculate_qfl_series(
                    highs, lows, closes,
                    base_periods=qfl_base_periods,
                    pump_periods=qfl_pump_periods,
                    pump_pct=qfl_pump_pct,
                    base_crack_pct=qfl_crack_pct,
                )
                base_last = qfl["base"][-1] if qfl.get("base") else None
                if base_last is not None:
                    snapshot["qfl_base"] = base_last
            except Exception as e:
                logger.debug(f"QFL snapshot skipped: {e}")

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
            return True

        try:
            return self._dispatch_indicator(indicator, itype, closes, highs, lows, opens)
        except Exception as e:
            logger.warning(
                f"Indicator {itype} error: {e} — returning False (fail-closed)"
            )
            return False

    def _dispatch_indicator(
        self,
        indicator: IndicatorConfig,
        itype: str,
        closes: list[float],
        highs: list[float] | None = None,
        lows: list[float] | None = None,
        opens: list[float] | None = None,
    ) -> bool:
        if itype == "RSI":
            src = self._resolve_price_source(
                indicator.price_source, closes, highs, lows, opens)
            return check_rsi_signal(
                src,
                period=indicator.period or 14,
                threshold=indicator.threshold or "below_35",
            )
        elif itype == "MACD":
            return check_macd_signal(
                closes,
                condition=indicator.condition or indicator.threshold or "histogram_positive",
                use_percentile=bool(indicator.use_percentile),
            )
        elif itype == "BOLLINGER":
            return check_bollinger_signal(
                closes,
                period=indicator.period or 20,
                multiplier=indicator.multiplier or 2.0,
                condition=indicator.condition or "price_below_lower",
                squeeze_threshold=indicator.squeeze_threshold or 0.02,
                ma_type=indicator.ma_type or "SMA",
                value=indicator.value or "lower",
            )
        elif itype == "PARABOLIC_SAR":
            if not highs or not lows:
                logger.warning("PARABOLIC_SAR requires highs/lows — "
                               "returning False (fail-closed)")
                return False
            return check_parabolic_sar_signal(
                highs, lows, closes,
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
                volume_threshold=indicator.volume_threshold or 0.0,
                min_touches=indicator.min_touches or 1,
            )
        elif itype == "QFL":
            return check_qfl_signal(
                closes,
                condition=indicator.condition or "below_base",
                base_periods=indicator.base_periods or 36,
                pump_periods=indicator.pump_periods or 8,
                pump_from_base_pct=indicator.pump_from_base_pct or 3.0,
                base_crack_pct=indicator.base_crack_pct or 3.0,
                highs=highs,
                lows=lows,
            )
        else:
            logger.warning(
                f"Unknown indicator type: '{indicator.type}' — "
                f"returning False to block entry. Check your YAML config."
            )
            return False
