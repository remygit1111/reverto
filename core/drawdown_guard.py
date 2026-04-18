"""Drawdown guard — kill-switch for paper and live engines.

Tracks the peak value of a chosen metric (equity or balance) since the
guard was instantiated and triggers when the current value is
``max_drawdown_pct`` below that peak. Once triggered the guard stays
triggered until ``reset()`` is called — the decision to resume trading
after a drawdown event should always be a deliberate operator action,
not an automatic bounce back on a recovered price.

The guard itself only observes and reports. Translating the trigger
into engine behaviour (pause new entries, stop the engine, notify the
operator) lives in the engine's tick loop so the guard can stay a
thin, test-only component.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class DrawdownGuardConfig(BaseModel):
    """Configuration for a single DrawdownGuard instance.

    Modelled with Pydantic so it can be embedded directly in
    ``BotConfig`` without a separate YAML schema mirror — constructing
    ``DrawdownGuardConfig(enabled=True, max_drawdown_pct=5)`` from test
    code still works because Pydantic's ``__init__`` accepts kwargs the
    same way a dataclass would.

    enabled:
        When False the guard is a permanent no-op — ``update()`` always
        returns False, ``is_triggered`` stays False. Lets callers wire
        the guard unconditionally without a surrounding ``if``.

    max_drawdown_pct:
        Percentage drop from peak that fires the trigger. A value of
        10.0 means "trigger when current <= peak * 0.9".

    metric:
        ``"equity"`` — balance + unrealised PnL (sensitive to open deals).
        ``"balance"`` — realised balance only (ignores paper-gain swings).

    action:
        ``"pause"`` — engine should skip new entries, keep managing open deals.
        ``"stop"``  — engine should halt entirely.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    metric: Literal["equity", "balance"] = "equity"
    action: Literal["pause", "stop"] = "pause"


class DrawdownGuard:
    """Observe a value series and fire once it drops ``max_drawdown_pct``
    from its running peak."""

    def __init__(self, config: DrawdownGuardConfig):
        self.config = config
        self._peak_value: Optional[float] = None
        self._triggered: bool = False
        self._trigger_reason: Optional[str] = None

    def update(self, current_value: float) -> bool:
        """Feed the latest metric reading and return True iff the guard
        is (or has become) triggered.

        Idempotent once triggered — subsequent calls keep returning True
        without overwriting ``_trigger_reason``. A disabled guard never
        triggers regardless of input.
        """
        if not self.config.enabled:
            return False

        if self._triggered:
            return True

        # First reading establishes the peak; no drawdown computable yet.
        if self._peak_value is None or current_value > self._peak_value:
            self._peak_value = current_value
            return False

        if self._peak_value <= 0:
            # A zero or negative peak would make the % calculation
            # meaningless. Pathological, but guard against it.
            return False

        drawdown_pct = (self._peak_value - current_value) / self._peak_value * 100

        if drawdown_pct >= self.config.max_drawdown_pct:
            self._triggered = True
            self._trigger_reason = (
                f"Drawdown {drawdown_pct:.2f}% exceeded threshold "
                f"{self.config.max_drawdown_pct}%"
            )
            logger.error("[DRAWDOWN GUARD TRIGGERED] %s", self._trigger_reason)
            return True

        return False

    @property
    def is_triggered(self) -> bool:
        return self._triggered

    @property
    def trigger_reason(self) -> Optional[str]:
        return self._trigger_reason

    @property
    def peak_value(self) -> Optional[float]:
        return self._peak_value

    def reset(self) -> None:
        """Clear triggered state so the engine can resume. The next
        ``update()`` call re-anchors the peak to whatever value it sees,
        which is usually what you want after a manual recovery."""
        self._triggered = False
        self._trigger_reason = None
        self._peak_value = None
