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
import re
import signal as _signal
import sys
from pathlib import Path

from config.config_loader import load_bot_config
from config.models import Mode
from core import paths
from core.file_lock import LockTimeoutError, exclusive_lock
from core.logging_setup import (
    configure_bot_file_logging,
    parse_log_level_env,
)
from exchanges.public_exchange import PublicExchange
from notifications.telegram import TelegramNotifier
from paper.paper_engine import PaperEngine

# Bot slugs come via CLI and become filenames / state paths. Constrain
# them to `[A-Za-z0-9_-]+` so a malformed --config or bot name can never
# traverse out of config/bots/ via `..` or absolute paths.
_BOT_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# Logging setup — local time. stdout/stderr are typically redirected by
# the portal to logs/{slug}.log via subprocess.Popen, so basicConfig on
# the default stream lands in the right place without extra handlers.
# Level comes from REVERTO_LOG_LEVEL (default INFO), so an operator
# can opt into DEBUG-on-disk for a single restart without editing code.
logging.basicConfig(
    level=parse_log_level_env(),
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
        "--bot",
        default=None,
        help="Bot slug — resolves to config/bots/<user-id>/<slug>.yaml",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=1,
        help="User ID owning this bot (multi-tenant scope). Phase 1/2 "
             "runs everything under user 1; leave at default unless "
             "you know what you're doing.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to bot YAML config (overrides --bot).",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=0.1,
        help="Initial paper balance in BTC",
    )
    args = parser.parse_args()

    user_id = int(args.user_id)

    if args.config:
        config_path = Path(args.config)
    elif args.bot:
        if not _BOT_SLUG_RE.match(args.bot):
            logger.error(
                "Invalid bot slug %r — must match %s",
                args.bot, _BOT_SLUG_RE.pattern,
            )
            sys.exit(1)
        config_path = paths.bot_yaml_path(user_id, args.bot)
    else:
        parser.error("Specify either --config or --bot")
        return

    # Slug = YAML filename stem. The registry + every on-disk artifact
    # keys off this stem, so renaming the bot inside the YAML must not
    # break the mapping.
    slug = config_path.stem
    if not _BOT_SLUG_RE.match(slug):
        logger.error(
            "Invalid bot slug %r — must match %s",
            slug, _BOT_SLUG_RE.pattern,
        )
        sys.exit(1)

    # Phase-2 per-user filesystem layout via core.paths.
    pid_file = paths.bot_pid_path(user_id, slug)
    state_file = paths.bot_state_path(user_id, slug)
    manual_trigger_file = paths.bot_manual_trigger_path(user_id, slug)
    log_file = paths.bot_log_path(user_id, slug)

    # PT-v4-FS-008 — install a RotatingFileHandler at the canonical
    # log path now that slug + user_id are resolved. Replaces the
    # module-level basicConfig handler so we don't double-write
    # via the portal's stdout-redirect path. See
    # ``configure_bot_file_logging`` for the trade-off discussion.
    configure_bot_file_logging(log_file)

    # Write PID file early so the portal's start_bot() polling sees it
    # within the 3s starting-slot window. atexit removes it on a clean
    # shutdown; SIGKILL leaves a stale pid file, but BotInfo.running then
    # does a kill(pid, 0) probe and cleanly reports the bot as stopped.
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: pid_file.exists() and pid_file.unlink())

    logger.info(
        "Starting Reverto paper trading — slug=%s user=%d config=%s pid=%s",
        slug, user_id, config_path, os.getpid(),
    )

    config = load_bot_config(str(config_path))

    # Hard mode check — this runner is strictly for Mode.PAPER. Previously
    # we only rejected Mode.LIVE which let Mode.BACKTEST slip through as
    # a paper run; now anything that isn't explicitly PAPER is refused.
    if config.mode != Mode.PAPER:
        logger.error(
            "Bot %s has mode=%s; main_paper.py only accepts PAPER. "
            "Use main_live.py for live, main_backtest.py for backtest.",
            slug, config.mode.value,
        )
        sys.exit(1)

    # Resolve the bot's exchange account → exchange_type. Paper bots
    # still need a real exchange-type for the public market-data
    # endpoint (and for state-snapshot labels). A missing/foreign
    # account is a hard refusal — falling back to a different
    # exchange would silently flip the market data feed.
    from core import exchange_account_store
    account = exchange_account_store.get_account(config.exchange_account_id)
    if account is None or account["user_id"] != user_id:
        logger.error(
            "Bot %s references exchange_account_id=%d which does not "
            "exist for user_id=%d. Recreate the account via the "
            "Exchanges admin tile and update the bot YAML.",
            slug, config.exchange_account_id, user_id,
        )
        sys.exit(1)
    exchange_type = str(account["exchange_type"])

    exchange = PublicExchange(exchange_type)
    notifier = TelegramNotifier(notify_on=config.telegram.notify_on)

    # Engine construction runs _load_state internally. Hold an
    # advisory cross-process lock on a sibling path so a portal-side
    # offline-close cannot mutate state.json mid-load; the portal
    # claims the same lock around its mutation. Released immediately
    # after __init__ returns — in-memory state is now owned by this
    # process and the portal's offline path will see running=True
    # and route to the sentinel fallback.
    lock_path = paths.bot_state_lock_path(user_id, slug)
    try:
        with exclusive_lock(lock_path, timeout=5.0):
            engine = PaperEngine(
                config=config,
                exchange=exchange,
                notifier=notifier,
                initial_balance_btc=args.balance,
                poll_interval=10,
                state_file=str(state_file),
                manual_trigger_file=str(manual_trigger_file),
                slug=slug,
                user_id=user_id,
                exchange_type=exchange_type,
            )
    except LockTimeoutError:
        logger.error(
            "Could not acquire state lock for %s within 5s — portal "
            "may be mid-close. Aborting startup; try `make start` "
            "again.", slug,
        )
        sys.exit(2)

    _install_signal_handlers(engine, exchange_type)

    engine.start()


def _install_signal_handlers(engine: PaperEngine, exchange_type: str) -> None:
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
                exchange_type,
            )
            engine.stop()
        finally:
            sys.exit(0)

    _signal.signal(_signal.SIGTERM, _on_sigterm)


if __name__ == "__main__":
    main()
