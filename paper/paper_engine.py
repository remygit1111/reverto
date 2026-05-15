# paper/paper_engine.py
# Paper trading engine — thin subclass of TradingEngine.
#
# After Phase 2 Task 2.1: this module's purpose is to host the
# paper-specific implementation of TradingEngine's abstract method
# (_deduct_balance). All other engine logic lives in
# core/trading_engine.py.
#
# External callers and tests import PaperEngine and several private
# helpers/constants from this module — those imports are preserved
# via the re-exports below so no caller or test file needs to change.
# A later Task 2.10 documentation pass may migrate callers to import
# from core.trading_engine directly; that is out of scope for 2.1.

import logging

from core.trading_engine import (
    TradingEngine,
    STATE_SCHEMA_VERSION,
    NOTIFY_DRAIN_TIMEOUT_S,
    _DEFAULT_NOTIFY_QUEUE_MAX,
    _resolve_notify_queue_max,
    _deal_to_dict,
    _dict_to_deal,
    _collect_active_indicator_types,
)

__all__ = [
    "PaperEngine",
    "TradingEngine",
    "STATE_SCHEMA_VERSION",
    "NOTIFY_DRAIN_TIMEOUT_S",
    "_DEFAULT_NOTIFY_QUEUE_MAX",
    "_resolve_notify_queue_max",
    "_deal_to_dict",
    "_dict_to_deal",
    "_collect_active_indicator_types",
]

logger = logging.getLogger(__name__)


class PaperEngine(TradingEngine):
    """Paper trading engine implementation.

    Provides simulated order execution against a virtual balance.
    All trading + infrastructure logic is inherited from
    TradingEngine; PaperEngine only supplies the paper-specific
    balance gate (_deduct_balance), which simulates an
    insufficient-funds rejection against the in-memory PaperState
    balance. Live mode replaces this with a hard pre-flight check
    against the exchange's reported balance.
    """

    def _deduct_balance(self, amount: float, reason: str) -> bool:
        """Safe balance debit with pre-flight insufficient-funds check.

        Returns True if the deduction succeeded, False if balance was too
        low. Every engine path that used to do ``balance_btc -= fee``
        directly now routes through here — on paper the net effect is the
        same (no exchange ever rejects), but the check logs clearly when
        a bot would have tripped an InsufficientFunds in live mode. For
        Phase 3 live this becomes a hard gate that prevents accidental
        over-spending.
        """
        if self.state.balance_btc < amount:
            logger.error(
                "InsufficientFunds: need %.8f BTC for %s, have %.8f",
                amount, reason, self.state.balance_btc,
            )
            self._notify(
                self.notifier.notify_error, self.config.name,
                f"Insufficient balance: {reason}",
            )
            return False
        self.state.balance_btc -= amount
        return True
