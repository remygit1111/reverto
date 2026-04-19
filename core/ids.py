# core/ids.py
# Globally-unique ID generation for deals + orders.
#
# Cross-bot collision fix (2026-04-19): the old paper_state counter
# produced per-instance "PAPER-NNNN" IDs — two bots both start at 0001
# and the save_deal INSERT OR REPLACE in core/deal_store silently
# clobbered the first writer's row. This module replaces that with a
# globally-unique time-sortable format.
#
# Format: YYYYMMDDHHMM-RRRR
#   * 12 digits of UTC timestamp (year-month-day-hour-minute).
#   * 10_000 possible randoms per minute (4-digit suffix).
#   * Time-sortable as a string — year-month-day-hour-minute prefix.
#
# Collision safety:
#   * Per-minute: 10_000 random suffixes. With the engine's paper-bot
#     tick cadence (open ≤ 1 deal/tick, ≥ 1s between ticks), the
#     birthday-problem collision probability stays far below the DB's
#     PRIMARY KEY guard for realistic bot counts.
#   * Cross-minute: timestamp prefix changes every 60s, so no collision
#     possible across minutes.
#   * DB PRIMARY KEY on deals.id is the authoritative guard. Callers
#     (paper_engine._db_save_deal) retry on IntegrityError up to 3x
#     with a fresh random suffix — then log ERROR and refuse the deal.

from __future__ import annotations

import random
import re
from datetime import datetime, timezone

# Regex for ingress validation: sentinel filenames, route path params.
# Anchored to reject any decoration (trailing `;rm`, lowercase drift).
DEAL_ID_RE = re.compile(r"^\d{12}-\d{4}$")


def generate_deal_id(now_utc: datetime | None = None) -> str:
    """Generate a globally-unique deal ID.

    ``now_utc`` is an injection point for tests — pass a fixed datetime
    to make the timestamp prefix deterministic. Production callers
    always let it default to ``datetime.now(timezone.utc)``.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    prefix = now_utc.strftime("%Y%m%d%H%M")
    suffix = f"{random.randint(0, 9999):04d}"
    return f"{prefix}-{suffix}"


def generate_order_id(now_utc: datetime | None = None) -> str:
    """Same format as deal IDs — orders and deals live in different
    tables so a hypothetical deal_id == order_id is harmless.

    Note that paper_engine today still builds order IDs as
    ``{deal_id}:{order_number}`` (deterministic from the parent deal).
    This helper exists for future call-sites that want a standalone
    order id without a parent deal.
    """
    return generate_deal_id(now_utc)
