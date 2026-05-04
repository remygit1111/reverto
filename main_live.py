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
from live.live_engine import LiveEngine
from notifications.telegram import TelegramNotifier

# Bot slugs drive config file resolution + PID/state paths. A value like
# "../../etc/passwd" would otherwise escape config/bots/.
_BOT_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# DRY_RUN environment values treated as truthy. Case-insensitive match
# so CI systems that set DRY_RUN=true or DRY_RUN=yes work without
# surprising the operator at a non-TTY container start.
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "y", "on"})


def _env_is_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_ENV_VALUES

# Level resolves from REVERTO_LOG_LEVEL (default INFO) so an operator
# can opt into DEBUG-on-disk for a single restart without a code edit.
logging.basicConfig(
    level=parse_log_level_env(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _print_live_banner(slug: str, config, dry_run: bool) -> None:
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
        f"Mode             : {mode_line}\n"
    )


def _require_confirmation(dry_run: bool) -> None:
    """Ask the operator to confirm unless DRY_RUN=1 (automated starts).

    Dry-run launches auto-confirm to keep the Phase 1 ``make live-dry``
    workflow non-interactive. A real live launch (``dry_run=False``)
    will still prompt until Phase 3 flips this to default-confirm.
    """
    if _env_is_truthy("DRY_RUN"):
        logger.info("DRY_RUN set — skipping confirmation prompt")
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
        help="Bot slug — resolves to config/bots/<user-id>/<slug>.yaml",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=1,
        help="User ID owning this bot (multi-tenant scope).",
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
    args = parser.parse_args()
    user_id = int(args.user_id)

    # Config path resolution — --config wins, then --bot <slug>. The
    # --bot path goes through core.paths so Phase-2 layout is enforced.
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

    slug = config_path.stem
    if not _BOT_SLUG_RE.match(slug):
        logger.error(
            "Invalid bot slug derived from config path: %r", slug,
        )
        sys.exit(1)

    pid_file = paths.bot_pid_path(user_id, slug)
    state_file = paths.bot_state_path(user_id, slug)
    manual_trigger_file = paths.bot_manual_trigger_path(user_id, slug)
    log_file = paths.bot_log_path(user_id, slug)

    # PT-v4-FS-008 — same rotating-handler wiring as main_paper.py.
    # Replaces the module-level basicConfig stream handler now that
    # the canonical log path is known.
    configure_bot_file_logging(log_file)

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: pid_file.exists() and pid_file.unlink())

    logger.info(
        "Starting Reverto live trading — slug=%s user=%d config=%s pid=%s",
        slug, user_id, str(config_path), os.getpid(),
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

    _print_live_banner(slug, config, args.dry_run)
    _require_confirmation(args.dry_run)

    # Exchange selection — Phase 1 dry-run is fine with the read-only
    # PublicExchange (no real orders reach it anyway). Phase 3 real
    # orders REQUIRE an authenticated client. The branch below already
    # refuses to boot a real-order run without credentials, so operators
    # can't accidentally ship a live bot against a public-only client.
    dry_run_effective = args.dry_run or _env_is_truthy("DRY_RUN")
    if dry_run_effective:
        exchange = PublicExchange(config.exchange.value)
        logger.info(
            "Using PublicExchange (dry-run) — real orders are not possible"
        )
    else:
        exchange = _authenticated_exchange(config.exchange.value, user_id)
        if exchange is None:
            logger.error(
                "Live mode requires exchange credentials — configure via "
                "the portal (Exchanges → Add Keys) before launching."
            )
            sys.exit(1)
        logger.warning(
            "Using AUTHENTICATED %s client — real orders WILL be placed",
            config.exchange.value,
        )
    notifier = TelegramNotifier(notify_on=config.telegram.notify_on)

    # Engine construction runs _load_state internally. Hold an
    # advisory cross-process lock on a sibling path so a portal-side
    # offline-close cannot mutate state.json mid-load; the portal
    # claims the same lock around its mutation. See core/file_lock.py
    # and the parallel block in main_paper.py for rationale.
    lock_path = paths.bot_state_lock_path(user_id, slug)
    try:
        with exclusive_lock(lock_path, timeout=5.0):
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
                user_id=user_id,
            )
    except LockTimeoutError:
        logger.error(
            "Could not acquire state lock for %s within 5s — portal "
            "may be mid-close. Aborting startup; try `make start` "
            "again.", slug,
        )
        sys.exit(2)

    _install_signal_handlers(engine)

    engine.start()


def _authenticated_exchange(name: str, user_id: int):
    """Build an authenticated exchange client for live (non-dry-run) use.

    Loads credentials through ``core.credentials.get_keys`` under the
    bot's owning user. Returns None when no credentials are saved —
    the caller must refuse to boot in that case.

    Only Bitget is wired today; Kraken follows the same shape once
    implemented. Audit r1-012: Bitget's passphrase comes from
    ``core.credentials.get_bitget_passphrase`` which prefers the
    per-user credentials store and only falls back to the legacy
    ``BITGET_PASSPHRASE`` env-var while the operator is migrating.
    If neither source yields a passphrase the helper raises and we
    refuse to boot live — matches the pre-fix behaviour of returning
    None on missing passphrase.
    """
    from core.credentials import get_bitget_passphrase, get_keys
    keys = get_keys(name, user_id=user_id)
    if not keys:
        return None

    if name == "bitget":
        from exchanges.bitget import BitgetExchange
        try:
            passphrase = get_bitget_passphrase(user_id)
        except ValueError as e:
            logger.error("Bitget passphrase unavailable for user %d: %s", user_id, e)
            return None
        return BitgetExchange(
            api_key=keys["api_key"],
            api_secret=keys["api_secret"],
            passphrase=passphrase,
            paper=False,
        )

    logger.error("Live trading not yet wired for exchange %r", name)
    return None


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
