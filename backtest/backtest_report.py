# backtest/backtest_report.py
# Berekent en toont alle statistieken van een backtest run.

import logging
from dataclasses import dataclass, field

from config.models import BotConfig
from paper.paper_state import PaperDeal

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """
    Alle statistieken van een voltooide backtest.
    Wordt aangemaakt door BacktestEngine._build_result().
    """
    config:               BotConfig
    candles_total:        int
    candles_processed:    int
    initial_balance_btc:  float
    final_balance_btc:    float
    closed_deals:         list[PaperDeal]
    fees_paid_btc:        float

    # Berekende velden — gevuld door __post_init__
    total_pnl_btc:        float = field(init=False)
    total_pnl_pct:        float = field(init=False)
    win_rate:             float = field(init=False)
    total_deals:          int   = field(init=False)
    winning_deals:        int   = field(init=False)
    losing_deals:         int   = field(init=False)
    avg_pnl_per_deal:     float = field(init=False)
    best_deal_btc:        float = field(init=False)
    worst_deal_btc:       float = field(init=False)
    max_drawdown_pct:     float = field(init=False)
    avg_dca_orders:       float = field(init=False)
    tp_count:             int   = field(init=False)
    sl_count:             int   = field(init=False)

    def __post_init__(self):
        deals = self.closed_deals

        self.total_deals      = len(deals)
        self.winning_deals    = sum(1 for d in deals if d.pnl_btc > 0)
        self.losing_deals     = sum(1 for d in deals if d.pnl_btc <= 0)
        self.total_pnl_btc    = round(sum(d.pnl_btc for d in deals), 8)
        self.fees_paid_btc    = round(self.fees_paid_btc, 8)

        # PnL als percentage van beginbalans
        if self.initial_balance_btc > 0:
            self.total_pnl_pct = round(
                (self.total_pnl_btc / self.initial_balance_btc) * 100, 2
            )
        else:
            self.total_pnl_pct = 0.0

        # Win rate
        self.win_rate = round(
            (self.winning_deals / self.total_deals * 100) if self.total_deals else 0.0, 2
        )

        # Gemiddelde PnL per deal
        self.avg_pnl_per_deal = round(
            self.total_pnl_btc / self.total_deals if self.total_deals else 0.0, 8
        )

        # Beste en slechtste deal
        pnls = [d.pnl_btc for d in deals]
        self.best_deal_btc  = round(max(pnls), 8) if pnls else 0.0
        self.worst_deal_btc = round(min(pnls), 8) if pnls else 0.0

        # Max drawdown (grootste daling van piek in cumulatieve PnL)
        self.max_drawdown_pct = self._calc_max_drawdown()

        # Gemiddeld aantal DCA orders per deal
        dca_counts = [d.dca_count for d in deals]
        self.avg_dca_orders = round(
            sum(dca_counts) / len(dca_counts) if dca_counts else 0.0, 2
        )

        # TP vs SL verdeling
        self.tp_count = sum(1 for d in deals if d.close_reason == "tp")
        self.sl_count = sum(1 for d in deals if d.close_reason == "sl")

    def _calc_max_drawdown(self) -> float:
        """Bereken max drawdown als percentage van de beginbalans."""
        if not self.closed_deals:
            return 0.0

        peak        = self.initial_balance_btc
        balance     = self.initial_balance_btc
        max_dd      = 0.0

        for deal in self.closed_deals:
            balance += deal.pnl_btc
            if balance > peak:
                peak = balance
            drawdown = (peak - balance) / peak * 100
            if drawdown > max_dd:
                max_dd = drawdown

        return round(max_dd, 2)

    def print(self):
        """Print een overzichtelijk rapport naar de console."""
        sep = "═" * 52

        def fmt(v: float, decimals: int = 8) -> str:
            """Formatteer een getal met correct teken — voorkomt +-0.000000."""
            # Normaliseer -0.0 naar 0.0
            v = v if v != 0.0 else 0.0
            sign = "+" if v >= 0 else ""
            return f"{sign}{v:.{decimals}f}"

        print(f"\n{sep}")
        print("  REVERTO BACKTEST RAPPORT")
        print(f"  Bot       : {self.config.name}")
        print(f"  Paar      : {self.config.pair}")
        print(f"  Candles   : {self.candles_processed:,} verwerkt / {self.candles_total:,} totaal")
        print(sep)

        print(f"  Beginbalans     : {self.initial_balance_btc:.8f} BTC")
        print(f"  Eindbalans      : {self.final_balance_btc:.8f} BTC")
        print(
            f"  Totale PnL      : {fmt(self.total_pnl_btc)} BTC"
            f"  ({fmt(self.total_pnl_pct, 2)}%)"
        )
        print(f"  Fees betaald    : -{abs(self.fees_paid_btc):.8f} BTC")
        print(sep)

        print(f"  Deals totaal    : {self.total_deals}")
        print(f"  Winnend         : {self.winning_deals}  ({self.win_rate:.1f}%)")
        print(f"  Verliezend      : {self.losing_deals}")
        print(f"  TP / SL         : {self.tp_count} / {self.sl_count}")
        print(f"  Gem. PnL/deal   : {fmt(self.avg_pnl_per_deal)} BTC")
        print(f"  Beste deal      : {fmt(self.best_deal_btc)} BTC")
        print(f"  Slechtste deal  : {fmt(self.worst_deal_btc)} BTC")
        print(f"  Max drawdown    : {self.max_drawdown_pct:.2f}%")
        print(f"  Gem. DCA orders : {self.avg_dca_orders:.1f}")
        print(sep)

        # Strategie instellingen ter referentie
        print(f"  TP target       : {self.config.take_profit.target_pct}%")
        print(f"  SL type/pct     : {self.config.stop_loss.type} / {self.config.stop_loss.pct}%")
        print(f"  DCA spacing     : {self.config.dca.order_spacing_pct}%")
        print(f"  DCA max orders  : {self.config.dca.max_orders}")
        print(f"  DCA multiplier  : {self.config.dca.multiplier}x")
        print(sep)

    def to_dict(self) -> dict:
        """Exporteer resultaten als dict (voor JSON opslag of vergelijking)."""
        return {
            "bot_name":           self.config.name,
            "pair":               self.config.pair,
            "candles_processed":  self.candles_processed,
            "initial_balance":    self.initial_balance_btc,
            "final_balance":      self.final_balance_btc,
            "total_pnl_btc":      self.total_pnl_btc,
            "total_pnl_pct":      self.total_pnl_pct,
            "fees_paid_btc":      self.fees_paid_btc,
            "total_deals":        self.total_deals,
            "win_rate":           self.win_rate,
            "tp_count":           self.tp_count,
            "sl_count":           self.sl_count,
            "avg_pnl_per_deal":   self.avg_pnl_per_deal,
            "best_deal_btc":      self.best_deal_btc,
            "worst_deal_btc":     self.worst_deal_btc,
            "max_drawdown_pct":   self.max_drawdown_pct,
            "avg_dca_orders":     self.avg_dca_orders,
        }
