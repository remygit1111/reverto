# config/models.py
# Defines the structure and validation rules for bot configurations

from pydantic import BaseModel, Field, ConfigDict
from typing import Literal, Optional
from enum import Enum


class Mode(str, Enum):
    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"


class Exchange(str, Enum):
    KRAKEN = "kraken"
    BITGET = "bitget"


class LiquidationGuard(BaseModel):
    warn_pct: float = 15.0
    emergency_close_pct: float = 5.0


class LeverageConfig(BaseModel):
    enabled: bool = False
    size: int = 1
    liquidation_guard: LiquidationGuard = LiquidationGuard()


class DCAConfig(BaseModel):
    base_order_size: float
    max_orders: int = 5      # Total orders including base order. DCA orders = max_orders - 1.
    order_spacing_pct: float = 2.5
    multiplier: float = 1.0


class IndicatorConfig(BaseModel):
    type: str
    period: Optional[int] = None
    threshold: Optional[str] = None
    fast: Optional[int] = None
    slow: Optional[int] = None
    signal: Optional[str] = None


class EntryConfig(BaseModel):
    # Field(default_factory=list) prevents shared mutable default across instances
    indicators: list[IndicatorConfig] = Field(default_factory=list)


class TakeProfitConfig(BaseModel):
    target_pct: float
    indicator_confirm: Optional[str] = None


class StopLossConfig(BaseModel):
    # Literal type ensures invalid values like "traling" raise a validation error
    # rather than silently falling through to fixed stop behaviour
    type: Literal["fixed", "trailing"] = "fixed"
    pct: float = 5.0


class MLConfig(BaseModel):
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

    model_config = ConfigDict(populate_by_name=True)


class ScheduleConfig(BaseModel):
    timezone: str = "Europe/Amsterdam"
    trading_windows: list[ScheduleWindow] = []
    blackout_dates: list[str] = []


class TelegramConfig(BaseModel):
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
    name: str
    mode: Mode
    exchange: Exchange
    pair: str = "BTC/USD"
    # contract_type determines PnL calculation formula.
    # Only inverse_perpetual is currently supported.
    contract_type: Literal["inverse_perpetual"] = "inverse_perpetual"
    leverage: LeverageConfig = LeverageConfig()
    dca: DCAConfig
    entry: EntryConfig = EntryConfig()
    take_profit: TakeProfitConfig
    stop_loss: StopLossConfig = StopLossConfig()
    ml: MLConfig = MLConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    telegram: TelegramConfig = TelegramConfig()
