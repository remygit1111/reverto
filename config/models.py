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
    timeframe: Optional[Literal["15m", "30m", "1h", "2h", "4h", "12h", "1d"]] = None
    condition: Optional[str] = None
    # RSI
    price_source: Optional[str] = None  # close/open/high/low/hl2/hlc3/ohlc4
    # MACD
    macd_fast: Optional[int] = None
    macd_slow: Optional[int] = None
    macd_signal: Optional[int] = None
    oscillator_ma_type: Optional[str] = None  # EMA/SMA
    signal_ma_type: Optional[str] = None      # EMA/SMA
    use_percentile: Optional[bool] = None
    # Bollinger Bands / Supertrend
    multiplier: Optional[float] = None
    ma_type: Optional[str] = None   # SMA/EMA/WMA (Bollinger)
    squeeze_threshold: Optional[float] = Field(default=None, gt=0)
    value: Optional[str] = None     # lower/upper/middle (BB) or support/resistance (S&R)
    # Parabolic SAR
    initial_af: Optional[float] = None
    max_af: Optional[float] = None
    # Supertrend
    atr_period: Optional[int] = None
    # Market Structure
    lookback: Optional[int] = None
    trigger_type: Optional[str] = None  # market_based
    # Support & Resistance
    left_bars: Optional[int] = None
    right_bars: Optional[int] = None
    proximity_pct: Optional[float] = None
    volume_threshold: Optional[float] = Field(default=None, ge=0)
    min_touches: Optional[int] = Field(default=None, ge=1)
    # QFL Base Scanner
    base_periods: Optional[int] = None
    pump_periods: Optional[int] = None
    pump_from_base_pct: Optional[float] = None
    base_crack_pct: Optional[float] = None


class IndicatorGroup(BaseModel):
    model_config = _STRICT
    id: int = 1
    name: str = ""
    indicators: list[IndicatorConfig] = Field(default_factory=list)


class EntryConfig(BaseModel):
    model_config = _STRICT
    indicators: list[IndicatorConfig] = Field(default_factory=list)
    indicator_groups: list[IndicatorGroup] = Field(default_factory=list)


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
