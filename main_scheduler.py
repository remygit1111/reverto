# main_scheduler.py
# Standalone process that captures hourly portfolio snapshots.
#
# Runs as the ``reverto-scheduler`` systemd service (separate from
# the portal), so a portal restart never loses an hourly tick and a
# scheduler crash never brings the portal down. Each tick:
#
#   1. waits until the next top-of-hour
#   2. enumerates every active exchange_accounts row
#   3. for each (user, account): fetches the balance via an
#      authenticated ccxt client, converts to USD via
#      ``core.price_feed.get_usd_rate``, and INSERTs one
#      ``portfolio_snapshots`` row with source='auto'
#   4. one failing account never aborts the loop — its error is logged
#      and the next account is processed
#   5. SIGTERM finishes the current batch before exiting
#
# Logs land in ``logs/scheduler.log`` (also wired by the systemd unit's
# StandardOutput=append). The PID file at logs/pids/scheduler.pid lets
# ``make status`` see whether the service is up.

from __future__ import annotations

import atexit
import logging
import os
import signal as _signal
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from core import (
    database,
    exchange_account_store,
    portfolio_store,
    price_feed,
    telegram_config_store,
)
from core.exchange_clients import (
    ExchangeClientError,
    build_authenticated_exchange,
)

# Logging — file + stdout (systemd captures stdout into the same log
# anyway via StandardOutput=append). RotatingFileHandler bounds the
# file at 5 MB × 3 generations so a long-running scheduler doesn't
# fill the disk.
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_PATH = "logs/scheduler.log"


def _configure_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    os.makedirs("logs/pids", exist_ok=True)

    handlers: list[logging.Handler] = []
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        _LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handlers.append(console)
    try:
        rfh = RotatingFileHandler(
            _LOG_PATH,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        rfh.setFormatter(logging.Formatter(
            _LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handlers.append(rfh)
    except OSError as e:
        # Disk full / permission error — log to stdout only.
        print(f"Could not open {_LOG_PATH}: {e}", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO, handlers=handlers, force=True,
    )
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger("reverto.scheduler")


# ── Signal handling ────────────────────────────────────────────────────────


# Flipped to True by the SIGTERM handler. The main loop checks this
# between sleeps and exits cleanly. We deliberately do NOT raise from
# the signal handler: a SIGTERM mid-snapshot lets that snapshot
# complete + persist, then exits after the row commits. That matches
# the spec ("finish current snapshot batch, then exit").
_shutdown_requested = False


def _on_sigterm(_signum, _frame) -> None:
    global _shutdown_requested
    logger.info(
        "SIGTERM received — finishing current snapshot batch then exiting",
    )
    _shutdown_requested = True


def _install_signal_handlers() -> None:
    _signal.signal(_signal.SIGTERM, _on_sigterm)
    # SIGINT (Ctrl-C in foreground) flips the same flag so an
    # operator running ``python main_scheduler.py`` interactively
    # can stop cleanly between ticks.
    _signal.signal(_signal.SIGINT, _on_sigterm)


# ── PID file ───────────────────────────────────────────────────────────────


_PID_FILE = "logs/pids/scheduler.pid"


def _write_pid_file() -> None:
    with open(_PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(
        lambda: os.path.exists(_PID_FILE) and os.remove(_PID_FILE),
    )


# ── Tick body ──────────────────────────────────────────────────────────────


def _all_user_ids() -> list[int]:
    """Return every distinct user_id in ``exchange_accounts``.

    We enumerate by exchange_accounts (not users) because a fresh
    user with no connected exchange has nothing to snapshot. The
    DISTINCT keeps the row count down even when an operator has
    many accounts.
    """
    conn = database.get_db()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM exchange_accounts "
        "ORDER BY user_id ASC",
    ).fetchall()
    return [int(r["user_id"]) for r in rows]


def _capture_one_account(
    user_id: int, account: dict, source: str,
) -> bool:
    """Build the authenticated client, fetch the balance, write the
    snapshot row. Returns True on success.

    Any exception is caught + logged so the outer loop continues with
    the next account. We intentionally swallow ccxt's varied exception
    shapes here (``BLE001``) — a network blip on one account must not
    prevent the next one from being captured.
    """
    account_id = int(account["id"])
    try:
        creds = exchange_account_store.get_account_credentials(account_id)
        if creds is None:
            logger.warning(
                "Skipping account %d (user %d): stored credentials "
                "are unreadable", account_id, user_id,
            )
            return False
        client = build_authenticated_exchange(
            account["exchange_type"], account["market_type"], creds,
        )
        balance_native = float(client.get_balance())
        currency = client.balance_currency
    except ExchangeClientError as e:
        logger.warning(
            "Skipping account %d (user %d): %s",
            account_id, user_id, e,
        )
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Skipping account %d (user %d): exchange call failed: %s",
            account_id, user_id, e,
        )
        return False

    try:
        usd_rate, rate_source = price_feed.get_usd_rate(currency)
    except price_feed.PriceFeedError as e:
        logger.warning(
            "Skipping account %d (user %d): cannot price %s: %s",
            account_id, user_id, currency, e,
        )
        return False

    balance_usd = balance_native * usd_rate
    try:
        portfolio_store.create_snapshot(
            user_id=user_id,
            exchange_account_id=account_id,
            balance_native=balance_native,
            currency=currency,
            balance_usd=balance_usd,
            usd_rate=usd_rate,
            rate_source=rate_source,
            source=source,
        )
    except Exception as e:  # noqa: BLE001 — DB IntegrityError, OSError
        logger.warning(
            "Snapshot write failed for account %d (user %d): %s",
            account_id, user_id, e,
        )
        return False

    logger.info(
        "Snapshot captured: user=%d account=%d %.8f %s = $%.2f "
        "(rate=%.4f via %s)",
        user_id, account_id, balance_native, currency,
        balance_usd, usd_rate, rate_source,
    )
    return True


def run_tick(*, source: str = "auto") -> tuple[int, int]:
    """One full pass: capture a snapshot for every (user, account).

    Returns ``(succeeded, attempted)`` so callers (the loop body, the
    manual-snapshot route once it lands) can log a summary.

    Exposed as a module-level function so tests can drive a single
    iteration without spinning up the loop.

    Side effect: expired telegram_link_tokens are dropped at the
    top of the tick. The cleanup is sub-millisecond and dropping
    it in here saves a separate scheduler-cron entry.
    """
    try:
        deleted = telegram_config_store.cleanup_expired_tokens()
        if deleted:
            logger.info(
                "Expired Telegram link tokens deleted: %d", deleted,
            )
    except Exception as e:  # noqa: BLE001 — never abort the snapshot run
        logger.warning(
            "cleanup_expired_tokens failed (snapshot run continues): %s", e,
        )

    user_ids = _all_user_ids()
    succeeded = 0
    attempted = 0
    for user_id in user_ids:
        accounts = exchange_account_store.list_accounts(user_id)
        for account in accounts:
            attempted += 1
            if _capture_one_account(user_id, account, source):
                succeeded += 1
    return succeeded, attempted


def _seconds_until_next_hour(now: datetime) -> float:
    """Seconds until the next ``HH:00:00`` boundary in UTC.

    Always returns ≥ 1 second so a tick that finishes exactly on
    the hour doesn't immediately re-tick.
    """
    next_hour = (now + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0,
    )
    delta = (next_hour - now).total_seconds()
    return max(delta, 1.0)


def _sleep_until_next_hour() -> None:
    """Sleep in 1-second slices so a SIGTERM during the wait is
    picked up promptly rather than only at the next tick boundary.
    """
    target = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0,
    )
    while not _shutdown_requested:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def main() -> None:
    _configure_logging()
    _install_signal_handlers()
    _write_pid_file()
    # The portal also calls init_db on its boot path. Calling it here
    # too is idempotent (``CREATE TABLE IF NOT EXISTS``) and makes the
    # scheduler bootable independently of the portal.
    database.init_db()

    now = datetime.now(timezone.utc)
    next_run = (now + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0,
    )
    logger.info(
        "Scheduler started — next snapshot at %s UTC (in %ds)",
        next_run.isoformat(),
        int(_seconds_until_next_hour(now)),
    )

    while not _shutdown_requested:
        _sleep_until_next_hour()
        if _shutdown_requested:
            break
        try:
            succeeded, attempted = run_tick(source="auto")
            logger.info(
                "Hourly snapshot batch complete: %d/%d succeeded",
                succeeded, attempted,
            )
        except Exception:  # noqa: BLE001 — last-resort guard
            # Failing one tick must not kill the loop; the next
            # hour is a fresh attempt.
            logger.exception("Hourly snapshot batch crashed")

    logger.info("Scheduler shut down cleanly")


if __name__ == "__main__":
    from core._version import __version__

    if "--version" in sys.argv:
        print(f"Reverto v{__version__}")
        sys.exit(0)
    main()
