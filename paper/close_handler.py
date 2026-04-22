"""Standalone deal-close / deal-cancel handler.

Extracted from ``paper/paper_engine._check_deal_sentinels`` so the
close / cancel logic can run from TWO contexts with a single source
of truth:

  * **Running-bot path** — the engine's tick loop consumes a
    ``{slug}.deal_close_{id}`` sentinel and delegates to this
    handler. Identical behaviour to the pre-refactor inline code.
  * **Stopped-bot path** — the portal's
    ``DELETE /api/bots/{slug}/deals/{deal_id}`` endpoint instantiates
    the handler directly, fetches the current market price via the
    public exchange, and closes the deal without ever touching a
    sentinel file.

Scope: **paper-mode only**. Live / dry-run bots need to cancel open
exchange orders before flipping the deal to closed; that surface
lives behind a ``LiveCloseHandler`` subclass in a follow-up PR.
Callers in the portal that target a live-mode bot with the bot
stopped must refuse with 501 — the handler itself is paper-only by
construction, no live-mode branches inside.

Thread safety: ``PaperState`` carries its own lock. This handler is
stateless across calls, so two concurrent ``close_deal`` invocations
race at the state level only — ``PaperState.close_deal`` pops atomic
under the lock, so the second call sees an empty ``open_deals`` and
returns a benign "deal not found" error.

Race with a starting bot: the portal's offline path reads + mutates
state.json without holding any cross-process lock. If a bot starts
mid-close, the bot's own ``_load_state`` may miss this mutation. This
is accepted as a Phase-3b item — the portal only takes the offline
path when ``BotInfo.running == False`` (PID file stale), which is
already a narrow window; a proper SQLite advisory lock would close
it properly.
"""

from __future__ import annotations

import logging
from typing import Optional

from core import deal_store
from paper.paper_state import PaperDeal, PaperState
from paper.state_io import StateIO, deal_to_dict

logger = logging.getLogger(__name__)


# Valid actions on the DELETE sentinel + the portal endpoint. "close"
# realises PnL at the exit price; "cancel" drops the deal without
# computing PnL (operator accepts responsibility for any open
# exchange position themselves).
_VALID_ACTIONS: frozenset[str] = frozenset({"close", "cancel"})

# Origin identifiers passed through ``close_deal(..., triggered_by=...)``
# so the handler can dispatch a distinct notification per-path:
#   * "engine" — running-bot tick-loop consumed a sentinel; fires the
#     legacy TP/SL-style notification to keep Telegram output identical
#     to pre-refactor behaviour.
#   * "portal" — operator clicked close in the UI while the bot was
#     stopped; fires notify_manual_close / notify_manual_cancel so
#     recipients can trace operator-initiated events separately.
_ORIGIN_ENGINE = "engine"
_ORIGIN_PORTAL = "portal"
_VALID_ORIGINS: frozenset[str] = frozenset({_ORIGIN_ENGINE, _ORIGIN_PORTAL})


class DealCloseHandler:
    """Close or cancel a single paper-mode deal.

    Instantiated with enough context to mutate state + persist the
    changes on both the tick-loop and portal code paths. Each call
    is independent — the same handler instance can process multiple
    deals if a caller wants to batch, but the canonical flow is one
    handler per request.

    Parameters
    ----------
    user_id
        Owning user_id for DB ledger rows. Required — every
        ``deal_store`` write is user-scoped (Phase-2 multi-tenant
        FK).
    bot_slug
        Canonical slug used for DB writes (``deals.bot_slug``) and
        log context. When empty / ``None`` the DB write is skipped
        (matches the engine's slug-less fallback so test fixtures
        that build a handler without DB plumbing keep working).
    bot_name
        Human-readable bot name for Telegram messages and logs. Pulled
        from the bot config; falls back to the slug when not provided.
    state
        The in-memory ``PaperState`` whose ``open_deals`` + balance +
        ``closed_deals`` are mutated by ``close_deal``. Caller retains
        ownership — the handler mutates in place.
    state_io
        Persists the post-close ``PaperState`` to ``state.json``. The
        atomic ``write(...)`` method on ``StateIO`` guarantees the
        on-disk file is never partially updated.
    taker_fee
        Exit-order fee rate (identical to ``config.dca.taker_fee`` on
        the running-bot path). Passed explicitly rather than via a
        config object so the handler has no dependency on the full
        ``BotConfig`` shape — keeps unit tests minimal and the
        contract clear.
    notifier
        Optional Telegram notifier. Running-bot path always provides
        one; portal path passes ``None`` so manual closes don't
        double-notify (the user clicked close in the UI themselves —
        they don't need a push back).
    notify_enqueue
        Optional callable that queues a notification function +
        args without blocking. Matches the engine's
        ``self._notify`` pattern so Telegram calls stay off the
        close-path's hot code. When ``None`` the handler calls
        ``notifier`` directly — fine for portal context where
        ``notifier`` is also ``None``.
    """

    def __init__(
        self,
        *,
        user_id: int,
        bot_slug: Optional[str],
        bot_name: str,
        state: PaperState,
        state_io: StateIO,
        taker_fee: float,
        notifier=None,
        notify_enqueue=None,
    ):
        self.user_id = int(user_id)
        self.bot_slug = bot_slug or None
        self.bot_name = bot_name or (bot_slug or "unknown")
        self.state = state
        self.state_io = state_io
        self.taker_fee = float(taker_fee)
        self.notifier = notifier
        self._notify_enqueue = notify_enqueue

    # ── Public API ──────────────────────────────────────────────────

    def close_deal(
        self,
        deal_id: str,
        current_price: float,
        action: str = "close",
        triggered_by: str = _ORIGIN_ENGINE,
    ) -> dict:
        """Close ("close") or cancel ("cancel") an open deal.

        Parameters
        ----------
        triggered_by
            ``"engine"`` (default) for the running-bot tick-loop path.
            ``"portal"`` for portal-initiated offline closes. Drives
            which Telegram notification is fired — the state + DB
            mutations are identical across both. See the
            ``_ORIGIN_*`` constants above for the full contract.

        Returns a dict:
          * success → ``{"ok": True, "action": ..., "deal": {...}, ...}``
          * failure → ``{"ok": False, "error": "..."}``

        Never raises — caller (tick-loop or portal route) decides how
        to surface the result. Tick-loop logs the error; portal
        translates to HTTP 400.
        """
        if action not in _VALID_ACTIONS:
            return {
                "ok": False,
                "error": f"invalid action {action!r}; expected close or cancel",
            }
        if triggered_by not in _VALID_ORIGINS:
            return {
                "ok": False,
                "error": (
                    f"invalid triggered_by {triggered_by!r}; "
                    f"expected engine or portal"
                ),
            }
        deal = self.state.open_deals.get(deal_id)
        if deal is None:
            return {
                "ok": False,
                "error": f"deal {deal_id} not found in open deals",
            }
        try:
            current_price = float(current_price)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": f"invalid current_price {current_price!r}",
            }
        if current_price <= 0:
            return {
                "ok": False,
                "error": "current_price must be > 0",
            }

        if action == "close":
            return self._close(deal, current_price, triggered_by)
        return self._cancel(deal, current_price, triggered_by)

    # ── Internal paths ──────────────────────────────────────────────

    def _close(
        self, deal: PaperDeal, price: float, triggered_by: str,
    ) -> dict:
        """Manual-close branch — realise PnL at ``price``, deduct exit
        fee, persist state + DB, fire the origin-appropriate
        notification.

        Matches the pre-refactor ``_check_deal_sentinels`` "close"
        branch 1:1 for state + DB effects — any drift would re-
        introduce the divergence this handler closes. The Telegram
        notification is origin-specific: engine → TP-style green/red
        (unchanged); portal → dedicated "Manual close" message.
        """
        pnl_btc, pnl_pct = deal.calculate_pnl(price)
        exit_size = deal.total_size
        exit_trigger = {"type": "manual"}
        deal.exit_trigger = exit_trigger

        self.state.close_deal(deal.id, price, "manual")
        fee = self._calc_fee(exit_size)
        self._deduct_balance(fee, f"manual_close:{deal.id}")
        self._db_close_deal(
            deal.id, price, "manual", pnl_btc, pnl_pct, exit_trigger,
        )
        logger.info(
            "Deal %s manually closed at $%.2f PnL: %+.6f BTC (fee %.8f) "
            "[origin=%s]",
            deal.id, price, pnl_btc, fee, triggered_by,
        )
        if triggered_by == _ORIGIN_PORTAL:
            self._maybe_notify_manual_close(
                deal.symbol, price, pnl_btc, pnl_pct,
            )
        else:
            self._maybe_notify_tp(deal.symbol, price, pnl_btc, pnl_pct)
        self._persist_state()

        return {
            "ok": True,
            "action": "close",
            "deal_id": deal.id,
            "close_price": price,
            "pnl_btc": pnl_btc,
            "pnl_pct": pnl_pct,
            "fee_btc": fee,
            "deal": deal_to_dict(deal, current_price=price),
        }

    def _cancel(
        self, deal: PaperDeal, price: float, triggered_by: str,
    ) -> dict:
        """Cancel branch — drop the deal without realising PnL.

        ``state.close_deal`` computes + applies PnL internally (that's
        its contract), but we record ``0.0 / 0.0`` in the DB ledger
        because the gain/loss has not been crystallised through an
        actual exit trade. The exchange position stays open; operator
        is responsible for closing it manually (documented in UI +
        runbook). Telegram dispatch mirrors ``_close``: engine path
        uses the legacy SL-style line; portal path uses a dedicated
        "Manual cancel" message.
        """
        exit_trigger = {"type": "cancelled"}
        deal.exit_trigger = exit_trigger
        self.state.close_deal(deal.id, price, "cancelled")
        self._db_close_deal(
            deal.id, price, "cancelled", 0.0, 0.0, exit_trigger,
        )
        logger.info(
            "Deal %s cancelled at $%.2f [origin=%s]",
            deal.id, price, triggered_by,
        )
        if triggered_by == _ORIGIN_PORTAL:
            self._maybe_notify_manual_cancel(deal.symbol)
        else:
            # Legacy running-bot path — kept identical to pre-refactor
            # behaviour so engine-driven cancels look the same on
            # Telegram as they always did.
            self._maybe_notify_sl(deal.symbol, price, 0.0, 0.0)
        self._persist_state()

        return {
            "ok": True,
            "action": "cancel",
            "deal_id": deal.id,
            "close_price": price,
            "pnl_btc": 0.0,
            "pnl_pct": 0.0,
            "fee_btc": 0.0,
            "deal": deal_to_dict(deal, current_price=price),
        }

    # ── Helpers mirroring paper_engine's ledger + balance plumbing ──

    def _calc_fee(self, size: float) -> float:
        """Exit-order taker fee in BTC — same inverse-contract math
        paper_engine uses (``size * taker_fee``)."""
        return size * self.taker_fee

    def _deduct_balance(self, amount: float, reason: str) -> None:
        """Subtract ``amount`` from ``state.balance_btc`` without the
        insufficient-funds guard.

        The running-bot ``_deduct_balance`` refuses and notifies on
        insufficient funds, because it's the single path every fee +
        DCA routes through. Here the context is narrower: we're
        closing a deal whose exit fee is bounded by the exit size,
        and paper-mode balance can legitimately go negative after a
        drawdown. Matching the old behaviour: deduct unconditionally,
        log when we'd have tripped in live mode.
        """
        if self.state.balance_btc < amount:
            logger.error(
                "InsufficientFunds on manual close: need %.8f BTC for %s, "
                "have %.8f (paper-mode: proceeding with negative balance)",
                amount, reason, self.state.balance_btc,
            )
        self.state.balance_btc -= amount

    def _db_close_deal(
        self, deal_id: str, close_price: float, reason: str,
        pnl_btc: float, pnl_pct: float, exit_trigger: dict,
    ) -> None:
        """Mirror of ``paper_engine._db_close_deal`` — soft-fails on
        any DB exception so a DB-side crash never prevents the in-
        memory state from converging."""
        if not self.bot_slug:
            return
        try:
            deal_store.close_deal(
                deal_id, close_price, reason, pnl_btc, pnl_pct,
                user_id=self.user_id,
                exit_trigger=exit_trigger,
            )
        except Exception as e:
            logger.warning("deal_store.close_deal failed: %s", e)

    def _persist_state(self) -> None:
        """Write the post-close ``PaperState`` snapshot to state.json,
        merging into any existing on-disk content so drawdown-guard /
        pause-state / other engine-owned fields survive the close.

        ``StateIO.write`` uses tmp-file + ``os.replace`` so the on-disk
        file is POSIX-atomically updated — no partial file can leak to
        an engine-on-restart even under SIGKILL mid-dump.

        Read-merge-write rationale: the handler owns only
        balance/open_deals/closed_deals; the engine's own
        ``_save_state`` writes a richer snapshot including
        ``drawdown_guard``, ``paused_by_drawdown``, ``indicators``,
        etc. An overwrite on the portal offline path would drop those
        fields until the bot next tick-writes — which on a stopped
        bot is "never". Merging preserves whatever the last engine
        tick left behind.
        """
        try:
            open_deals = [
                deal_to_dict(d) for d in self.state.open_deals.values()
            ]
            closed_deals = [
                deal_to_dict(d) for d in self.state.closed_deals
            ]
            # Start from whatever the engine last wrote so drawdown /
            # pause / indicator state is preserved. None → empty dict
            # (first close on a fresh install with no prior state).
            snapshot = self.state_io.load() or {}
            snapshot.update({
                "bot_name":            self.bot_name,
                "balance_btc":         self.state.balance_btc,
                "initial_balance_btc": self.state.initial_balance_btc,
                "open_deals":          open_deals,
                "closed_deals":        closed_deals,
                "open_deals_count":    len(open_deals),
                "closed_deals_count":  len(closed_deals),
            })
            self.state_io.write(snapshot)
        except Exception as e:
            logger.warning("Failed to persist state after close: %s", e)

    # ── Notification helpers — tolerate missing notifier ────────────

    def _maybe_notify_tp(
        self, symbol: str, price: float, pnl_btc: float, pnl_pct: float,
    ) -> None:
        if self.notifier is None:
            return
        self._fire(
            self.notifier.notify_take_profit,
            self.bot_name, symbol, price, pnl_btc, pnl_pct,
        )

    def _maybe_notify_sl(
        self, symbol: str, price: float, pnl_btc: float, pnl_pct: float,
    ) -> None:
        if self.notifier is None:
            return
        self._fire(
            self.notifier.notify_stop_loss,
            self.bot_name, symbol, price, pnl_btc, pnl_pct,
        )

    def _maybe_notify_manual_close(
        self, symbol: str, price: float, pnl_btc: float, pnl_pct: float,
    ) -> None:
        """Portal-origin close notification — handler-new method on
        TelegramNotifier. Tolerant of notifiers that haven't been
        upgraded yet (AttributeError → skip) so a test fixture using
        a stub doesn't need the full API surface."""
        if self.notifier is None:
            return
        fn = getattr(self.notifier, "notify_manual_close", None)
        if fn is None:
            return
        self._fire(fn, self.bot_name, symbol, price, pnl_btc, pnl_pct)

    def _maybe_notify_manual_cancel(self, symbol: str) -> None:
        """Portal-origin cancel notification. Same tolerant shape as
        ``_maybe_notify_manual_close`` — a notifier without the
        method quietly skips instead of erroring the close path."""
        if self.notifier is None:
            return
        fn = getattr(self.notifier, "notify_manual_cancel", None)
        if fn is None:
            return
        self._fire(fn, self.bot_name, symbol)

    def _fire(self, fn, *args, **kwargs) -> None:
        """Route a notifier call through the optional queue hook so
        running-bot context stays non-blocking. Portal context uses
        no queue + no notifier so this is a no-op path there."""
        if self._notify_enqueue is not None:
            self._notify_enqueue(fn, *args, **kwargs)
            return
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.warning("Notifier call failed: %s", e)
