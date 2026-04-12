# config/models.py
# Defines the structure and validation rules for bot configurations

from pydantic import BaseModel, Field
from typing import Optional
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
    max_orders: int = 5
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
    indicators: list[IndicatorConfig] = []


class TakeProfitConfig(BaseModel):
    target_pct: float
    indicator_confirm: Optional[str] = None


class StopLossConfig(BaseModel):
    type: str = "fixed"
    pct: float = 5.0


class MLConfig(BaseModel):
    enabled: bool = False
    model: str = "lightgbm"
    retrain_interval: str = "7d"
    features: list[str] = []


class ScheduleWindow(BaseModel):
    days: list[str]
    from_time: str = Field(alias="from")
    to_time: str = Field(alias="to")

    class Config:
        populate_by_name = True


class ScheduleConfig(BaseModel):
    timezone: str = "Europe/Amsterdam"
    trading_windows: list[ScheduleWindow] = []
    blackout_dates: list[str] = []


class TelegramConfig(BaseModel):
    notify_on: list[str] = [
        "entry", "dca_trigger", "tp_hit", "sl_hit", "liquidation_warn"
    ]


class BotConfig(BaseModel):
    name: str
    mode: Mode
    exchange: Exchange
    pair: str = "BTC/USD"
    contract_type: str = "inverse_perpetual"
    leverage: LeverageConfig = LeverageConfig()
    dca: DCAConfig
    entry: EntryConfig = EntryConfig()
    take_profit: TakeProfitConfig
    stop_loss: StopLossConfig = StopLossConfig()
    ml: MLConfig = MLConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    telegram: TelegramConfig = TelegramConfig()