# main_live.py
# Entry point for a single LIVE trading bot subprocess.
#
# Phase 1 status: DRY-RUN ONLY. Real order execution is refused by
# LiveEngine._place_market_order until Phase 3. This file exists now
# so the operator-facing wiring (confirmation prompt, mode check,
# PID/state file layout) can shake out before real orders go through.
#
# Slug policy mirrors main_paper.py — the YAML filename stem is the
# single source of truth, never config.name.

import argparse
import atexit
import logging
import os
import signal as _signal
import sys
from pathlib import Path

from config.config_loader import load_bot_config
from config.models import Mode
from exchanges.public_exchange import PublicExchange
from live.live_engine import DEFAULT_MAX_BASE_ORDER_SIZE_BTC, LiveEngine
from notifications.telegram import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _print_live_banner(
    slug: str, config, dry_run: bool, max_size: float
) -> None:
    """Operator-facing banner. Intentionally uses plain print (not
    logger) so it lands on stdout even when the engine is captured
    into a log file by the portal."""
    mode_line = "DRY RUN (no orders placed)" if dry_run else "LIVE (real orders)"
    print(
        "\n"
        "╔═══════════════════════════════════════════════╗\n"
        "║  ⚠  LIVE TRADING MODE — REAL MONEY AT RISK   ║\n"
        "╚═══════════════════════════════════════════════╝\n"
        f"\n"
        f"Slug             : {slug}\n"
        f"Bot name         : {config.name}\n"
        f"Exchange         : {config.exchange.value}\n"
        f"Pair             : {config.pair}\n"
        f"Base order size  : {config.dca.base_order_size} BTC\n"
        f"Max allowed size : {max_size} BTC\n"
        f"Mode             : {mode_line}\n"
    )


def _require_confirmation(dry_run: bool) -> None:
    """Ask the operator to confirm unless DRY_RUN=1 (automated starts).

    Dry-run launches auto-confirm to keep the Phase 1 ``make live-dry``
    workflow non-interactive. A real live launch (``dry_run=False``)
    will still prompt until Phase 3 flips this to default-confirm.
    """
    if os.environ.get("DRY_RUN") == "1":
        logger.info("DRY_RUN=1 — skipping confirmation prompt")
        return
    if dry_run:
        logger.info("dry-run enabled — skipping confirmation prompt")
        return
    answer = input("Continue? [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Aborted by operator.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reverto live trading engine")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to bot YAML config (overrides --bot)",
    )
    parser.add_argument(
        "--bot",
        default=None,
        help="Bot slug — resolves to config/bots/<slug>.yaml",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=0.1,
        help="Starting balance in BTC (used for bookkeeping / drawdown baseline)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Phase 1: dry-run is forced on; no real orders placed",
    )
    parser.add_argument(
        "--max-base-order-size",
        type=float,
        default=DEFAULT_MAX_BASE_ORDER_SIZE_BTC,
        help="Refuse bots whose DCA base order size exceeds this cap",
    )
    args = parser.parse_args()

    # Config path resolution — --config wins, then --bot <slug>, then the
    # paper-style fallback. This matches main_paper.py so operators can
    # switch between runners without learning a second flag grammar.
    if args.config:
        config_path = Path(args.config)
    elif args.bot:
        config_path = Path("config/bots") / f"{args.bot}.yaml"
    else:
        parser.error("Specify either --config or --bot")
        return

    slug = config_path.stem

    base_dir = Path(__file__).parent
    log_dir = base_dir / "logs"
    pid_dir = log_dir / "pids"
    log_dir.mkdir(parents=True, exist_ok=True)
    pid_dir.mkdir(parents=True, exist_ok=True)

    pid_file = pid_dir / f"{slug}.pid"
    state_file = log_dir / f"{slug}.state.json"
    manual_trigger_file = log_dir / f"{slug}.manual_trigger"

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: pid_file.exists() and pid_file.unlink())

    logger.info(
        "Starting Reverto live trading — slug=%s config=%s pid=%s",
        slug, str(config_path), os.getpid(),
    )

    config = load_bot_config(str(config_path))

    # Hard mode check — this runner is for Mode.LIVE only. Paper bots
    # must use main_paper.py so they never even reach a real-exchange
    # client; backtest bots would be nonsensical here.
    if config.mode != Mode.LIVE:
        logger.error(
            "Bot %s has mode=%s. main_live.py only accepts mode=live bots. "
            "Use main_paper.py for paper bots.",
            slug, config.mode.value,
        )
        sys.exit(1)

    _print_live_banner(slug, config, args.dry_run, args.max_base_order_size)
    _require_confirmation(args.dry_run)

    exchange = PublicExchange(config.exchange.value)
    notifier = TelegramNotifier(notify_on=config.telegram.notify_on)

    try:
        engine = LiveEngine(
            config=config,
            exchange=exchange,
            notifier=notifier,
            initial_balance_btc=args.balance,
            poll_interval=10,
            state_file=str(state_file),
            manual_trigger_file=str(manual_trigger_file),
            slug=slug,
            dry_run=args.dry_run,
            max_base_order_size=args.max_base_order_size,
        )
    except ValueError as e:
        logger.error("LiveEngine refused to start: %s", e)
        sys.exit(1)

    _install_signal_handlers(engine)

    engine.start()


def _install_signal_handlers(engine: LiveEngine) -> None:
    """Same SIGTERM → engine.stop() pattern as main_paper.py."""
    def _on_sigterm(_signum, _frame):
        logger.info("Received SIGTERM — stopping engine cleanly")
        try:
            engine._notify(
                engine.notifier.notify_stop,
                engine.config.name,
                engine.config.mode.value,
                engine.config.exchange.value,
            )
            engine.stop()
        finally:
            sys.exit(0)

    _signal.signal(_signal.SIGTERM, _on_sigterm)


if __name__ == "__main__":
    main()
