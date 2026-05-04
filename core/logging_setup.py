"""Shared logging setup helpers for the main_paper / main_live entry
points.

Operators can override the default INFO log level via the
``REVERTO_LOG_LEVEL`` environment variable — useful for retrospective
debugging sessions where DEBUG output on disk is needed temporarily,
without making DEBUG the permanent default (which would grow
logs/<uid>/*.log faster than the existing rotation handles).

Example operator workflow:

    REVERTO_LOG_LEVEL=DEBUG make restart     # DEBUG on disk
    # … investigate …
    make restart                             # back to INFO default
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


_VALID_LEVELS: tuple[str, ...] = (
    "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
)

# PT-v4-FS-008 — bot subprocess log rotation. Pre-fix the bot logs
# under logs/<uid>/<slug>.log grew unbounded (only Caddy access logs
# were rotated). 10 MiB × 3 backups = ~40 MiB ceiling per bot, in
# line with the per-bot RAM budget the scaling-audit memo uses for
# tier projections. Both knobs are env-overridable so an operator
# investigating an outage can crank up retention temporarily.
_DEFAULT_BOT_LOG_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_BOT_LOG_BACKUP_COUNT = 3


def _resolve_bot_log_max_bytes() -> int:
    """Read ``REVERTO_BOT_LOG_MAX_BYTES`` once per bot startup. Fall
    back to the default on missing / malformed / non-positive
    values, matching the resolver style elsewhere in the project
    (``_max_bots_per_user`` etc.)."""
    raw = os.environ.get("REVERTO_BOT_LOG_MAX_BYTES")
    if raw is None:
        return _DEFAULT_BOT_LOG_MAX_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(
            f"Warning: REVERTO_BOT_LOG_MAX_BYTES={raw!r} is not an "
            f"integer — falling back to default "
            f"{_DEFAULT_BOT_LOG_MAX_BYTES}.",
            file=sys.stderr,
        )
        return _DEFAULT_BOT_LOG_MAX_BYTES
    if value <= 0:
        print(
            f"Warning: REVERTO_BOT_LOG_MAX_BYTES={value} is non-"
            f"positive — falling back to default "
            f"{_DEFAULT_BOT_LOG_MAX_BYTES}.",
            file=sys.stderr,
        )
        return _DEFAULT_BOT_LOG_MAX_BYTES
    return value


def _resolve_bot_log_backup_count() -> int:
    """Read ``REVERTO_BOT_LOG_BACKUP_COUNT``. 0 is allowed (disables
    rotation while keeping the cap-and-truncate behaviour). Negative
    values fall back."""
    raw = os.environ.get("REVERTO_BOT_LOG_BACKUP_COUNT")
    if raw is None:
        return _DEFAULT_BOT_LOG_BACKUP_COUNT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(
            f"Warning: REVERTO_BOT_LOG_BACKUP_COUNT={raw!r} is not "
            f"an integer — falling back to default "
            f"{_DEFAULT_BOT_LOG_BACKUP_COUNT}.",
            file=sys.stderr,
        )
        return _DEFAULT_BOT_LOG_BACKUP_COUNT
    if value < 0:
        print(
            f"Warning: REVERTO_BOT_LOG_BACKUP_COUNT={value} is "
            f"negative — falling back to default "
            f"{_DEFAULT_BOT_LOG_BACKUP_COUNT}.",
            file=sys.stderr,
        )
        return _DEFAULT_BOT_LOG_BACKUP_COUNT
    return value


def configure_bot_file_logging(
    log_path: Path,
    level: Optional[int] = None,
) -> RotatingFileHandler:
    """Replace the root logger's handlers with a single
    ``RotatingFileHandler`` writing to ``log_path``.

    Called from ``main_paper.py`` / ``main_live.py`` once the slug +
    user_id are resolved (so we know the canonical log path). Removes
    any handlers a prior ``logging.basicConfig`` set up so log lines
    aren't double-emitted to stdout AND the file (which would race
    rotation — Popen's redirected stdout fd holds the old inode after
    rotation, so post-rotation stdout writes land in the rotated-out
    file).

    Trade-off: post-rotation stdout/stderr writes (Python interpreter
    crash dumps, anything via ``print()``) still flow to whatever the
    portal redirected fd 1/2 to — typically the same path. After the
    first rotation those writes land in the rotated-out file
    (``<slug>.log.1``) instead of the active log. Acceptable: regular
    bot output goes through the rotated logger, and crash dumps are
    rare enough that an operator looking at ``.log`` first and ``.log.1``
    second is fine. Documented here so a future engineer knows why
    we don't add a stream handler back.

    Returns the handler so the caller can adjust formatter / level
    if needed. Idempotent: calling twice replaces the handler with a
    fresh one (existing handlers are removed first), which is what
    you want if a test or operator forced reconfiguration.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if level is None:
        level = parse_log_level_env()

    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=_resolve_bot_log_max_bytes(),
        backupCount=_resolve_bot_log_backup_count(),
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger()
    # Drop existing handlers so basicConfig's StreamHandler doesn't
    # double-write to the same path via Popen's stdout redirect.
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    root.addHandler(handler)
    root.setLevel(level)
    return handler


# ── Request-ID plumbing (audit r1-034) ─────────────────────────────────────
# The contextvar + filter live here (not web/app.py) so main_web.py can
# attach the filter to its handlers at boot — before any module-level
# log lines are emitted. If the filter only attached later, every
# handler whose formatter uses ``%(request_id)s`` would KeyError on
# records emitted during startup.
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "reverto_request_id", default="-",
)


class RequestIdFilter(logging.Filter):
    """Injects ``record.request_id`` on every log record so the
    formatter can resolve ``%(request_id)s`` without KeyError.
    Records emitted outside a request pick up the contextvar's
    default ``"-"``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def parse_log_level_env(
    env_name: str = "REVERTO_LOG_LEVEL",
    default_level: int = logging.INFO,
) -> int:
    """Resolve a logging level from an env var.

    Case-insensitive; unset or empty falls back to ``default_level``.
    Invalid values print a one-line warning to stderr and also fall
    back — we never abort boot over a typo in a non-critical knob.
    The env-var itself is not mutated.
    """
    raw = os.environ.get(env_name, "")
    if not raw:
        return default_level
    name = raw.strip().upper()
    if name in _VALID_LEVELS:
        return getattr(logging, name)
    print(
        f"Warning: {env_name}={raw!r} is not a valid Python log level "
        f"(expected one of {'/'.join(_VALID_LEVELS)}). "
        f"Falling back to {logging.getLevelName(default_level)}.",
        file=sys.stderr,
    )
    return default_level
