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


def calculate_liquidation_price(entry_price: float, leverage: int,
                                 side: str = "long") -> float:
    """
    Estimate liquidation price for an inverse perpetual contract.
    This is a simplified calculation — real exchanges use maintenance margin.

    For a long position:
        liq_price = entry_price * (leverage / (leverage + 1))
    For a short position:
        liq_price = entry_price * (leverage / (leverage - 1))
    """
    if leverage <= 1:
        return 0.0  # No liquidation risk without leverage

    if side == "long":
        return round(entry_price * (leverage / (leverage + 1)), 2)
    else:
        return round(entry_price * (leverage / (leverage - 1)), 2)


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
            self._thread.join(timeout=15)  # verhoog van 5 naar 15
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

        logger.info(f"LiquidationGuard checking {len(positions)} positions")  # tijdelijk

        for pos in positions:
            self._check_position(pos)

    def _check_position(self, pos: dict):
        deal_id = pos["deal_id"]
        mark_price = pos["mark_price"]
        entry_price = pos["entry_price"]
        leverage = pos["leverage"]
        side = pos["side"]
        symbol = pos["symbol"]

        liq_price = calculate_liquidation_price(entry_price, leverage, side)

        if liq_price <= 0:
            return

        distance = abs(mark_price - liq_price) / mark_price * 100
        distance = round(distance, 2)

        logger.debug(
            f"LiquidationGuard: {deal_id} | mark: ${mark_price:,.2f} | "
            f"liq: ${liq_price:,.2f} | distance: {distance}%"
        )

        last_warning = self._warned.get(deal_id)

        if distance <= self.emergency_pct:
            if last_warning != "emergency":
                logger.warning(f"EMERGENCY liquidation risk: {deal_id} distance={distance}%")
                self.notifier.notify_liquidation_emergency(
                    self.config.name, symbol, distance
                )
                self._warned[deal_id] = "emergency"

        elif distance <= self.warn_pct:
            if last_warning not in ("warn", "emergency"):
                logger.warning(f"Liquidation warning: {deal_id} distance={distance}%")
                self.notifier.notify_liquidation_warning(
                    self.config.name, symbol,
                    mark_price, liq_price, distance
                )
                self._warned[deal_id] = "warn"

        else:
            if deal_id in self._warned:
                del self._warned[deal_id]
                logger.info(f"LiquidationGuard: {deal_id} back to safe distance ({distance}%)")