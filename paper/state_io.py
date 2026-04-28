"""State persistence primitives for PaperEngine.

Extracted from ``paper/paper_engine.py`` (audit v22 recommendation) to
isolate the file-I/O surface from engine business logic. The engine
still owns what goes INTO the state dict and how the restore affects
internal members; this module owns the atomic-write / atomic-read /
orphan-cleanup / serialisation details.

Backwards compatibility note: the two deal serialisation helpers are
also re-exported from ``paper.paper_engine`` (as ``_deal_to_dict`` and
``_dict_to_deal``) so existing imports in the test suite and anywhere
else keep working unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from paper.paper_state import PaperDeal, PaperOrder, PaperState

logger = logging.getLogger(__name__)


def deal_to_dict(deal: PaperDeal, current_price: float = 0.0) -> dict:
    """Convert a PaperDeal to a JSON-serialisable dict.

    Closed deals keep their stamped PnL — re-deriving from ``current_price``
    would be wrong, the realised PnL was already written at ``close_deal()``
    time. Open deals get an unrealised PnL if ``current_price`` is supplied;
    otherwise PnL defaults to 0 (keeps the state file valid when the engine
    has no live price yet).
    """
    if not deal.is_open:
        pnl_btc = deal.pnl_btc
        pnl_pct = deal.pnl_pct
    elif current_price:
        pnl_btc, pnl_pct = deal.calculate_pnl(current_price)
    else:
        pnl_btc, pnl_pct = 0.0, 0.0
    return {
        "id":              deal.id,
        "bot_name":        deal.bot_name,
        "symbol":          deal.symbol,
        "side":            deal.side,
        "leverage":        deal.leverage,
        "order_count":     len(deal.orders),
        # ``dca_count`` excludes the base order (order_number == 1) so
        # a deal that closed on base alone reports 0. The Closed Deals
        # tab on the bot-detail page renders this as a dedicated DCA
        # column. Computed from the existing PaperDeal.dca_count
        # property so the per-row aggregation engine code already uses
        # stays the single source of truth.
        "dca_count":       deal.dca_count,
        "entry_price":     round(deal.orders[0].price, 2) if deal.orders else 0.0,
        "avg_entry_price": round(deal.avg_entry_price, 2),
        "total_size":      deal.total_size,
        "pnl_btc":         round(pnl_btc, 8),
        "pnl_pct":         round(pnl_pct, 4),
        "opened_at":       deal.opened_at.isoformat() if deal.opened_at else None,
        "closed_at":       deal.closed_at.isoformat() if deal.closed_at else None,
        "close_price":     deal.close_price,
        "close_reason":    deal.close_reason,
        "is_open":         deal.is_open,
        "_peak_price":     deal._peak_price,  # persist trailing stop peak
        "_tp_override":    deal._tp_override,
        "_sl_override":    deal._sl_override,
        "_dca_enabled":    deal._dca_enabled,
        # Since-open wick trackers — see paper_state.PaperDeal docstring.
        # Persisted so a portal restart doesn't reset the tracker to
        # the entry price and hand the deal a full window in which a
        # pre-existing wick could retroactively trigger TP/SL.
        "_wick_high_since_open": deal._wick_high_since_open,
        "_wick_low_since_open":  deal._wick_low_since_open,
        "entry_trigger":   deal.entry_trigger,
        "exit_trigger":    deal.exit_trigger,
        # Full order list — required to reconstruct the deal on restart.
        # The dashboard only reads order_count, so adding this list is
        # backwards-compatible with the frontend.
        "orders": [
            {
                "order_number": o.order_number,
                "price":        o.price,
                "size":         o.size,
                "timestamp":    o.timestamp.isoformat() if o.timestamp else None,
                "order_type":   o.order_type,
            }
            for o in deal.orders
        ],
    }


def dict_to_deal(d: dict) -> PaperDeal:
    """Reconstruct a PaperDeal (with orders) from a state-file dict.

    Used at startup to restore deals that survived a previous run. Only
    fields that affect engine logic are restored; cosmetic fields like
    rounded prices are recomputed from the order list.
    """
    def _parse_dt(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    orders = [
        PaperOrder(
            order_number=int(o.get("order_number", 1)),
            price=float(o.get("price", 0.0)),
            size=float(o.get("size", 0.0)),
            timestamp=_parse_dt(o.get("timestamp")) or datetime.now(UTC),
            order_type=str(o.get("order_type", "base")),
        )
        for o in d.get("orders", [])
    ]

    deal = PaperDeal(
        id=str(d["id"]),
        bot_name=str(d.get("bot_name", "")),
        symbol=str(d.get("symbol", "")),
        side=str(d.get("side", "long")),
        leverage=int(d.get("leverage", 1)),
        orders=orders,
        is_open=bool(d.get("is_open", True)),
        opened_at=_parse_dt(d.get("opened_at")) or datetime.now(UTC),
        closed_at=_parse_dt(d.get("closed_at")),
        close_price=d.get("close_price"),
        close_reason=d.get("close_reason"),
        pnl_btc=float(d.get("pnl_btc", 0.0)),
        pnl_pct=float(d.get("pnl_pct", 0.0)),
    )
    deal._peak_price = float(d.get("_peak_price", 0.0))
    deal._tp_override = d.get("_tp_override")
    deal._sl_override = d.get("_sl_override")
    deal._dca_enabled = d.get("_dca_enabled", True)
    # Since-open wick trackers — backwards-compat with pre-fix state
    # files that didn't have these fields: fall back to avg_entry_price
    # (the same sentinel ``__post_init__`` uses for fresh deals). The
    # tracker will then reflect "no ticks observed since reload" which
    # is the only honest value we can synthesise from the persisted
    # state alone.
    fallback = deal.avg_entry_price if deal.orders else 0.0
    deal._wick_high_since_open = float(
        d.get("_wick_high_since_open", fallback)
    )
    deal._wick_low_since_open = float(
        d.get("_wick_low_since_open", fallback)
    )
    et = d.get("entry_trigger")
    deal.entry_trigger = et if isinstance(et, dict) else None
    xt = d.get("exit_trigger")
    deal.exit_trigger = xt if isinstance(xt, dict) else None
    return deal


class StateIO:
    """Atomic state-file I/O with orphan .tmp cleanup.

    Thin persistence layer that does NOT understand the contents of the
    state dict — the engine builds / consumes the dict, this class just
    reads and writes it safely.

    Contract preserved from the pre-refactor paper_engine methods:
      * ``load()`` returns the raw dict, or ``None`` if missing / corrupt.
        It also sweeps orphan ``<state_file>*.tmp`` siblings left by a
        SIGKILL mid-write.
      * ``write(d)`` uses tmp-file + ``os.replace`` for POSIX-atomic write.
      * ``mark_stopped()`` keeps the engine's original ``_clear_state``
        semantics: overwrite ``running`` / ``current_price`` with the
        "stopped" values, leaving the rest of the state intact. This is
        different from the filesystem-level ``unlink`` that a naive
        "clear" might imply — the portal polls the file to see the
        stopped state, and deleting it would look like "bot never ran".
    """

    def __init__(self, state_file: Optional[Path], slug: Optional[str] = None):
        self.state_file = state_file
        self.slug = slug or ""

    # ── Orphan cleanup ──────────────────────────────────────────────

    def cleanup_orphan_tmps(self) -> None:
        """Sweep orphan ``<state_file>*.tmp`` siblings.

        Only touches tmps whose name starts with the state-file's name
        (e.g. ``bot.state.json.tmp``) — other modules in the same dir
        write their own tmp files (credentials.json.tmp, etc.) and MUST
        NOT be swept.
        """
        if self.state_file is None:
            return
        parent = self.state_file.parent
        stem = self.state_file.name
        try:
            for orphan in parent.glob("*.tmp"):
                if orphan.name.startswith(stem):
                    try:
                        orphan.unlink()
                        logger.warning("Removed orphan state tmp: %s", orphan)
                    except OSError as e:
                        logger.warning("Failed to remove %s: %s", orphan, e)
        except OSError:
            # Directory unreadable — e.g. tmp_path was deleted between
            # construction and sweep. Nothing to do.
            pass

    # ── Read / write ────────────────────────────────────────────────

    def load(self) -> Optional[dict]:
        """Read the state file and return the parsed dict.

        Orphan cleanup runs FIRST so a sweep failure can't mask a
        real read. Returns ``None`` on missing file or parse failure;
        the caller is expected to log the missing-file case as "fresh
        start" and the parse-failure case as a warning.
        """
        self.cleanup_orphan_tmps()
        if self.state_file is None or not self.state_file.exists():
            return None
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "State file %s exists but could not be parsed (%s) — "
                "starting clean", self.state_file, e,
            )
            return None

    def write(self, state_dict: dict) -> None:
        """Atomically write ``state_dict`` to disk.

        Swallowed-and-logged on failure: this is called from the hot
        tick path and a transient disk-full condition must not kill
        the engine. The log line drops to DEBUG because a filesystem
        error here is usually transient and engine stays responsive.
        """
        if self.state_file is None:
            return
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(state_dict, indent=2), encoding="utf-8",
            )
            tmp.replace(self.state_file)
        except Exception as e:
            logger.debug("State write failed: %s", e)

    def mark_stopped(self) -> None:
        """Record ``running=False`` in the state file.

        Preserves the pre-refactor ``_clear_state`` behaviour: reads
        the current state, overwrites the running / current_price
        fields, writes back atomically. Does not delete — the portal
        polls the file for the stopped-state signal.
        """
        if self.state_file is None:
            return
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
            else:
                data = {}
            data["running"]       = False
            data["current_price"] = 0.0
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as e:
            logger.warning(
                "Failed to clear state for %s: %s",
                self.slug or "unknown", str(e)[:200],
            )


# ── Standalone state loader (used by portal's offline close path) ───────

def load_paper_state_from_file(
    state_file: Path, slug: Optional[str] = None,
) -> tuple[PaperState, StateIO]:
    """Rehydrate a ``PaperState`` from a state.json file without
    invoking the full ``PaperEngine`` constructor.

    Used by the portal's ``DELETE /api/bots/{slug}/deals/{deal_id}``
    offline branch: when a paper bot isn't running, the portal needs
    a PaperState instance + a StateIO to hand to ``DealCloseHandler``.
    Engine-construction would pull in ccxt clients, schedule guards,
    indicator engines — none of which we need just to close a deal.

    The restore logic mirrors the balance + closed_deals + open_deals
    subset of ``PaperEngine._load_state``. Fields the close path
    doesn't touch (drawdown_guard, fees_paid_btc, paused_by_drawdown,
    etc.) stay on disk via the handler's read-merge-write — they're
    not materialised into the returned ``PaperState`` because the
    handler doesn't read them, but the next engine start will
    rehydrate them directly from the preserved fields.

    Returns
    -------
    (state, state_io)
        Both ready to pass into ``DealCloseHandler``. If the file
        doesn't exist, returns an empty ``PaperState`` + bound
        ``StateIO`` — the caller will find no matching deal and
        surface a 404-equivalent error.
    """
    state_io = StateIO(state_file, slug=slug)
    state = PaperState()
    raw = state_io.load()
    if not raw:
        return state, state_io

    # Balance + initial_balance: floats that survive a restart as-is.
    try:
        state.balance_btc = float(raw.get("balance_btc", 0.0))
    except (TypeError, ValueError):
        pass
    try:
        state.initial_balance_btc = float(raw.get(
            "initial_balance_btc", state.initial_balance_btc,
        ))
    except (TypeError, ValueError):
        pass

    # Closed + open deal history.
    for entry in raw.get("closed_deals", []):
        try:
            state.closed_deals.append(dict_to_deal(entry))
        except Exception as e:
            logger.warning(
                "load_paper_state_from_file: skipping unparseable closed deal: %s", e,
            )
    for entry in raw.get("open_deals", []):
        try:
            deal = dict_to_deal(entry)
            state.open_deals[deal.id] = deal
        except Exception as e:
            logger.warning(
                "load_paper_state_from_file: skipping unparseable open deal: %s", e,
            )

    return state, state_io
