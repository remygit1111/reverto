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

import logging
import os
import sys


_VALID_LEVELS: tuple[str, ...] = (
    "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
)


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
