# main_paper.py
# Entry point for a single paper-trading bot subprocess.
#
# Slug comes from the YAML filename stem — NOT from config.name — so the
# portal (BotRegistry, state files, log files, PID files) always has a
# stable 1:1 mapping regardless of how the operator named the bot inside
# the YAML.

import argparse
import atexit
import logging
import os
import signal as _signal
import sys
from pathlib import Path

from config.config_loader import load_bot_config
from exchanges.public_exchange import PublicExchange
from notifications.telegram import TelegramNotifier
from paper.paper_engine import PaperEngine

# Logging setup — local time. stdout/stderr are typically redirected by
# the portal to logs/{slug}.log via subprocess.Popen, so basicConfig on
# the default stream lands in the right place without extra handlers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)

# Suppress httpx — hides Telegram token and general noise from logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reverto paper trading engine")
    parser.add_argument(
        "--config",
        default="config/bots/btc_paper.yaml",
        help="Path to bot YAML config",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=0.1,
        help="Initial paper balance in BTC",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    # Slug = YAML filename stem (never config.name). The portal keys
    # everything — BotInfo.pid_file, log_file, state_file — on this stem,
    # so renaming the bot inside the YAML must not break the mapping.
    slug = config_path.stem

    base_dir = Path(__file__).parent
    log_dir = base_dir / "logs"
    pid_dir = log_dir / "pids"
    log_dir.mkdir(parents=True, exist_ok=True)
    pid_dir.mkdir(parents=True, exist_ok=True)

    pid_file = pid_dir / f"{slug}.pid"
    state_file = log_dir / f"{slug}.state.json"

    # Write PID file early so the portal's start_bot() polling sees it
    # within the 3s starting-slot window. atexit removes it on a clean
    # shutdown; SIGKILL leaves a stale pid file, but BotInfo.running then
    # does a kill(pid, 0) probe and cleanly reports the bot as stopped.
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: pid_file.exists() and pid_file.unlink())

    logger.info(
        "Starting Reverto paper trading — slug=%s config=%s pid=%s",
        slug, args.config, os.getpid(),
    )

    config = load_bot_config(str(config_path))
    exchange = PublicExchange(config.exchange.value)
    notifier = TelegramNotifier(notify_on=config.telegram.notify_on)

    engine = PaperEngine(
        config=config,
        exchange=exchange,
        notifier=notifier,
        initial_balance_btc=args.balance,
        poll_interval=10,
        state_file=str(state_file),
    )

    _install_signal_handlers(engine)

    engine.start()


def _install_signal_handlers(engine: PaperEngine) -> None:
    """Translate SIGTERM into a clean engine.stop() call.

    The portal stops bots via os.kill(pid, SIGTERM). Without an explicit
    handler the default SIGTERM action terminates the Python process
    immediately — so engine.stop() never runs and the queued
    notify_shutdown / notify_stop messages never reach Telegram. Wiring
    SIGTERM to engine.stop() lets the notify worker flush its queue
    (engine.stop() joins it with a 15s timeout) before we exit.

    SIGINT is intentionally NOT touched so the existing KeyboardInterrupt
    path in PaperEngine.start() keeps working unchanged.
    """
    def _on_sigterm(_signum, _frame):
        logger.info("Received SIGTERM — stopping engine cleanly")
        try:
            # Queue the notify_stop BEFORE engine.stop() so the drain
            # loop inside stop() flushes it. Calling it after would race
            # the daemon notify worker's exit.
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
