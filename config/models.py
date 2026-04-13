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
    size: int = 1
    liquidation_guard: LiquidationGuard = LiquidationGuard()


class DCAConfig(BaseModel):
    model_config = _STRICT
    base_order_size: float
    max_orders: int = 5      # Total orders including base order. DCA orders = max_orders - 1.
    order_spacing_pct: float = 2.5
    multiplier: float = 1.0
    taker_fee: float = 0.0006  # Bitget BTCUSD inverse taker fee, 0.06%


class IndicatorConfig(BaseModel):
    model_config = _STRICT
    type: str
    period: Optional[int] = None
    threshold: Optional[str] = None
    fast: Optional[int] = None
    slow: Optional[int] = None
    signal: Optional[str] = None
    # Per-indicator fields the wizard may set. The engine currently
    # ignores these but we accept them so extra='forbid' doesn't reject
    # wizard payloads round-tripped through edit.
    timeframe: Optional[str] = None
    condition: Optional[str] = None


class EntryConfig(BaseModel):
    model_config = _STRICT
    # Field(default_factory=list) prevents shared mutable default across instances
    indicators: list[IndicatorConfig] = Field(default_factory=list)


class TakeProfitConfig(BaseModel):
    model_config = _STRICT
    target_pct: float
    indicator_confirm: Optional[str] = None


class StopLossConfig(BaseModel):
    model_config = _STRICT
    # Literal type ensures invalid values like "traling" raise a validation error
    # rather than silently falling through to fixed stop behaviour
    type: Literal["fixed", "trailing"] = "fixed"
    pct: float = 5.0


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
    timezone: str = "Europe/Amsterdam"
    trading_windows: list[ScheduleWindow] = []
    blackout_dates: list[str] = []


class TelegramConfig(BaseModel):
    model_config = _STRICT
    # Controls which events trigger a Telegram notification.
    # Valid values: entry, dca_trigger, tp_hit, sl_hit, liquidation_warn,
    #               schedule_open, schedule_close, error, startup, shutdown
    notify_on: list[Literal[
        "entry", "dca_trigger", "tp_hit", "sl_hit", "liquidation_warn",
        "schedule_open", "schedule_close", "error", "startup", "shutdown"
    ]] = [
        "entry", "dca_trigger", "tp_hit", "sl_hit", "liquidation_warn",
        "schedule_open", "schedule_close", "error", "startup", "shutdown"
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
