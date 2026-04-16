# config/models.py
# Defines the structure and validation rules for bot configurations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal, Optional
from enum import Enum

_NAME_RE = re.compile(r"^[a-zA-Z0-9 \-_]+$")


class Mode(str, Enum):
    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"


class Exchange(str, Enum):
    KRAKEN = "kraken"
    BITGET = "bitget"


# Strict config: unknown keys anywhere in a bot YAML become a hard
# ValidationError instead of silently getting stripped. The wizard's
# nbBuildBotConfig() already filters its payload to known fields, so
# this only bites on legitimately malformed configs or stale YAML.
_STRICT = ConfigDict(extra="forbid")


class LiquidationGuard(BaseModel):
    model_config = _STRICT
    warn_pct: float = 15.0
    emergency_close_pct: float = 5.0


class LeverageConfig(BaseModel):
    model_config = _STRICT
    enabled: bool = False
    size: int = Field(default=1, ge=1, le=125)
    liquidation_guard: LiquidationGuard = LiquidationGuard()


class DCAConfig(BaseModel):
    model_config = _STRICT
    enabled: bool = True
    base_order_size: float = Field(gt=0)
    # Total orders including base order. DCA orders = max_orders - 1.
    # max_orders=0 or 1 disables DCA (base order only).
    max_orders: int = Field(default=5, ge=0, le=50)
    order_spacing_pct: float = Field(default=2.5, gt=0, le=50)
    multiplier: float = Field(default=1.0, gt=0, le=10)
    step_scale: float = Field(default=1.0, gt=0, le=5)
    taker_fee: float = Field(default=0.0006, ge=0, le=0.01)


class IndicatorConfig(BaseModel):
    model_config = _STRICT
    type: str
    period: Optional[int] = None
    threshold: Optional[str] = None
    fast: Optional[int] = None
    slow: Optional[int] = None
    signal: Optional[str] = None
    # Per-indicator timeframe override. None = use bot-level timeframe.
    # Must be one of the supported candle intervals so we can match it
    # against the engine's fetched data.
    timeframe: Optional[Literal["15m", "1h", "4h", "1d"]] = None
    condition: Optional[str] = None
    # MACD period knobs — optional, default to the classic 12/26/9.
    # Kept as separate fields (rather than reusing fast/slow) because
    # EMA Cross already uses those for its own pair.
    macd_fast: Optional[int] = None
    macd_slow: Optional[int] = None
    macd_signal: Optional[int] = None
    # Bollinger Bands — std dev multiplier (default 2.0)
    multiplier: Optional[float] = None
    # Parabolic SAR — acceleration factor floor and ceiling
    initial_af: Optional[float] = None
    max_af: Optional[float] = None
    # Supertrend — ATR period (multiplier shared with Bollinger field)
    atr_period: Optional[int] = None
    # Market Structure / Support-Resistance / QFL — swing lookback window
    lookback: Optional[int] = None
    # Support & Resistance — cluster merge + proximity sensitivities (%)
    tolerance_pct: Optional[float] = None
    proximity_pct: Optional[float] = None
    # QFL Base Scanner — rebound threshold / window / retention / break %
    crack_pct: Optional[float] = None
    base_candles: Optional[int] = None
    max_bases: Optional[int] = None
    below_pct: Optional[float] = None


class EntryConfig(BaseModel):
    model_config = _STRICT
    # Field(default_factory=list) prevents shared mutable default across instances
    indicators: list[IndicatorConfig] = Field(default_factory=list)


class TakeProfitConfig(BaseModel):
    model_config = _STRICT
    enabled: bool = True
    target_pct: float = Field(default=3.0, gt=0, le=100)
    indicator_confirm: Optional[str] = None
    minimum_tp_pct: Optional[float] = Field(default=None, ge=0, le=100)


class StopLossConfig(BaseModel):
    model_config = _STRICT
    type: Literal["none", "fixed", "trailing"] = "fixed"
    pct: float = Field(default=5.0, ge=0, le=100)


class MLConfig(BaseModel):
    model_config = _STRICT
    # NOTE: ML functionality is not yet implemented.
    # Setting enabled=true has no effect on engine behaviour.
    # A warning is logged at startup when enabled=true is detected.
    enabled: bool = False
    model: str = "lightgbm"
    retrain_interval: str = "7d"
    features: list[str] = []


class ScheduleWindow(BaseModel):
    days: list[str]
    from_time: str = Field(alias="from")
    to_time: str = Field(alias="to")

    # populate_by_name lets callers use either `from`/`to` (YAML) or
    # `from_time`/`to_time` (Python); extra='forbid' still rejects
    # anything outside that whitelist.
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ScheduleConfig(BaseModel):
    model_config = _STRICT
    enabled: bool = False
    timezone: str = "Europe/Amsterdam"
    trading_windows: list[ScheduleWindow] = []
    blackout_dates: list[str] = []


class TelegramConfig(BaseModel):
    model_config = _STRICT
    # Controls which events trigger a Telegram notification.
    # Valid values: entry, dca_trigger, tp_hit, sl_hit, liquidation_warn,
    #               schedule_open, schedule_close, error, startup, shutdown
    # "shutdown" is a legacy synonym kept for back-compat — new bots use
    # "stop" / "restart" which fire on portal-driven lifecycle events.
    notify_on: list[Literal[
        "entry", "dca_trigger", "tp_hit", "sl_hit", "liquidation_warn",
        "schedule_open", "schedule_close", "error", "startup",
        "shutdown", "stop", "restart"
    ]] = [
        "entry", "dca_trigger", "tp_hit", "sl_hit", "liquidation_warn",
        "schedule_open", "schedule_close", "error", "startup",
        "shutdown", "stop", "restart"
    ]


class BotConfig(BaseModel):
    # Top-level strict validation — every unknown key at bot.* level is
    # now a hard ValidationError instead of silently being dropped.
    model_config = _STRICT

    name: str
    mode: Mode
    exchange: Exchange
    pair: str = "BTC/USD"
    # contract_type determines PnL calculation formula.
    # Only inverse_perpetual is currently supported.
    contract_type: Literal["inverse_perpetual"] = "inverse_perpetual"
    # Direction and timeframe are wizard-level metadata — the engine
    # currently assumes long + 1h candles internally, but we accept
    # them on the model so the wizard can round-trip them without
    # being rejected by extra='forbid'.
    direction: Literal["long", "short"] = "long"
    timeframe: Literal["15m", "1h", "4h", "1d"] = "1h"
    # Wick simulation: when True, the paper engine pulls the current
    # forming candle's high/low on every tick and fires TP/SL against
    # those values instead of only the live tick price. Matches the
    # backtest behaviour and removes the 10s tick-poll blind spot, at
    # the cost of one extra OHLCV request per timeframe-cache window.
    use_wick_simulation: bool = True
    leverage: LeverageConfig = LeverageConfig()
    dca: DCAConfig
    entry: EntryConfig = EntryConfig()
    take_profit: TakeProfitConfig
    stop_loss: StopLossConfig = StopLossConfig()
    ml: MLConfig = MLConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    telegram: TelegramConfig = TelegramConfig()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        # Strip eerst control chars (ANSI escapes, null bytes) zodat een
        # YAML met "BotA\x1b[31m" niet via logs richting dashboard lekt.
        cleaned = "".join(c for c in v if ord(c) >= 32 or c in "\t").strip()
        if not (1 <= len(cleaned) <= 100):
            raise ValueError("name must be 1-100 characters after stripping control chars")
        if not _NAME_RE.match(cleaned):
            raise ValueError(
                "name may only contain letters, digits, spaces, '-' and '_'"
            )
        return cleaned
