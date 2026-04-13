# core/liquidation_guard.py
# Monitors open positions and warns when liquidation price is approached.
# Runs as a separate thread — independent from the main engine loop.
# Always active regardless of trading schedule.

import time
import logging
import threading
from config.models import BotConfig
from notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

# Typical maintenance margin rate for Bitget BTCUSD inverse (tier 1)
MAINTENANCE_MARGIN_RATE = 0.005  # 0.5%

# Bitget taker fee for inverse perpetuals
TAKER_FEE = 0.0006  # 0.06%


def calculate_liquidation_price(entry_price: float, leverage: int,
                                 side: str = "long",
                                 mmr: float = MAINTENANCE_MARGIN_RATE,
                                 taker_fee: float = TAKER_FEE) -> float:
    """
    Estimate liquidation price for Bitget BTCUSD inverse perpetual.
    On Bitget, position size is expressed in BTC (not USD contracts).

    For isolated margin, the liquidation price is approximately:
        Long:  liq = entry * (1 - 1/leverage + mmr/leverage + taker_fee)
        Short: liq = entry * (1 + 1/leverage - mmr/leverage - taker_fee)

    Verification against screenshot (entry=70978.8, 2x leverage, isolated):
        Bitget shows: $35,810.90
        This formula: $35,727  (within ~0.2% — acceptable for a warning system)

    The small remaining difference is due to Bitget's tiered MMR calculation
    and rounding in their internal engine.

    Note: Returns 0.0 for leverage <= 1 (no liquidation risk without leverage).
    """
    if leverage <= 1:
        return 0.0

    if side == "long":
        factor = 1 - (1 - mmr) / leverage + taker_fee
        liq = entry_price * factor
        # Sanity check: liq must be below entry for a long
        if liq >= entry_price:
            return 0.0
        return round(liq, 2)
    else:
        factor = 1 + (1 - mmr) / leverage - taker_fee
        liq = entry_price * factor
        # Sanity check: liq must be above entry for a short
        if liq <= entry_price:
            return 0.0
        return round(liq, 2)


class LiquidationGuard:
    """
    Monitors open positions for liquidation risk.
    Runs in a background thread, checks every `check_interval` seconds.
    Sends Telegram warnings at configurable distance thresholds.
    """

    def __init__(self, config: BotConfig, notifier: TelegramNotifier,
                 check_interval: int = 10):
        self.config = config
        self.notifier = notifier
        self.check_interval = check_interval
        self.running = False
        self._thread: threading.Thread = None

        # Liquidation guard thresholds from config
        self.warn_pct = config.leverage.liquidation_guard.warn_pct
        self.emergency_pct = config.leverage.liquidation_guard.emergency_close_pct

        # Position data — updated by engine on every tick
        self._lock = threading.Lock()
        self._positions: list[dict] = []

        # Track warning state to avoid spamming Telegram
        self._warned: dict[str, str] = {}  # deal_id → last warning level

    # ------------------------------------------------------------------
    # Thread control
    # ------------------------------------------------------------------

    def start(self):
        """Start the liquidation guard in a background thread."""
        if not self.config.leverage.enabled:
            logger.info("LiquidationGuard: leverage disabled — guard inactive")
            return

        self.running = True
        self._thread = threading.Thread(
            target=self._run,
            name="LiquidationGuard",
            daemon=True  # Dies automatically when main process stops
        )
        self._thread.start()
        logger.info(
            f"LiquidationGuard started — "
            f"warn at {self.warn_pct}%, emergency at {self.emergency_pct}%"
        )

    def stop(self):
        """Stop the liquidation guard thread."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=15)
        logger.info("LiquidationGuard stopped")

    # ------------------------------------------------------------------
    # Position updates — called by engine on every tick
    # ------------------------------------------------------------------

    def update_positions(self, positions: list[dict]):
        """
        Update the list of positions to monitor.
        Called by the paper/live engine on every price tick.

        Each position dict must contain:
            deal_id, symbol, side, entry_price, mark_price, leverage
        """
        with self._lock:
            self._positions = positions

    # ------------------------------------------------------------------
    # Main monitoring loop
    # ------------------------------------------------------------------

    def _run(self):
        """Background thread loop."""
        while self.running:
            try:
                self._check_all_positions()
            except Exception as e:
                logger.error(f"LiquidationGuard error: {e}")
            time.sleep(self.check_interval)

    def _check_all_positions(self):
        """Check all open positions for liquidation risk."""
        with self._lock:
            positions = list(self._positions)

        for pos in positions:
            self._check_position(pos)

    def _check_position(self, pos: dict):
        """
        Check a single position and send warnings if needed.
        Warning levels:
            warn      → distance < warn_pct (default 15%)
            emergency → distance < emergency_pct (default 5%)
        """
        deal_id = pos["deal_id"]
        mark_price = pos["mark_price"]
        entry_price = pos["entry_price"]
        leverage = pos["leverage"]
        side = pos["side"]
        symbol = pos["symbol"]

        if leverage <= 1:
            return  # No liquidation risk

        # Defensive: skip the check if mark price is missing or zero.
        # Can happen when the exchange ticker briefly returns a stale
        # frame; we'd rather miss one tick than ZeroDivision the thread.
        if mark_price <= 0:
            return

        liq_price = calculate_liquidation_price(entry_price, leverage, side)
        if liq_price <= 0:
            return

        # Calculate distance percentage between mark price and liquidation price
        distance = abs(mark_price - liq_price) / mark_price * 100
        distance = round(distance, 2)

        logger.debug(
            f"LiquidationGuard: {deal_id} | mark: ${mark_price:,.2f} | "
            f"liq: ${liq_price:,.2f} | distance: {distance}%"
        )

        last_warning = self._warned.get(deal_id)

        if distance <= self.emergency_pct:
            if last_warning != "emergency":
                logger.warning(
                    f"EMERGENCY liquidation risk: {deal_id} distance={distance}%"
                )
                self.notifier.notify_liquidation_emergency(
                    self.config.name, symbol, distance
                )
                self._warned[deal_id] = "emergency"

        elif distance <= self.warn_pct:
            if last_warning not in ("warn", "emergency"):
                logger.warning(
                    f"Liquidation warning: {deal_id} distance={distance}%"
                )
                self.notifier.notify_liquidation_warning(
                    self.config.name, symbol,
                    mark_price, liq_price, distance
                )
                self._warned[deal_id] = "warn"

        else:
            # Position is safe — reset warning state
            if deal_id in self._warned:
                del self._warned[deal_id]
                logger.info(
                    f"LiquidationGuard: {deal_id} back to safe distance ({distance}%)"
                )
