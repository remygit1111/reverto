"""Deal + annotation routes extracted from web/app.py.

Routes:
  GET    /api/db/deals                              — list ledger rows
  GET    /api/db/deals/{deal_id}/orders             — orders per deal
  GET    /api/db/stats                              — win-rate summary
  GET    /api/bots/{slug}/deals/{deal_id}           — single deal (live or ledger)
  PATCH  /api/bots/{slug}/deals/{deal_id}           — edit override sentinels
  DELETE /api/bots/{slug}/deals/{deal_id}           — close / cancel sentinel
  POST   /api/db/annotations                        — new chart annotation
  GET    /api/db/annotations                        — list annotations
  DELETE /api/db/annotations/all                    — bulk-delete annotations
  DELETE /api/db/annotations/{ann_id}               — delete one annotation
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from config.config_loader import load_bot_config
from core import deal_store, paths
from core.file_lock import LockTimeoutError, exclusive_lock
from core.user import User
from notifications.telegram import TelegramNotifier
from paper.close_handler import DealCloseHandler
from paper.state_io import load_paper_state_from_file
from web.app import (
    _audit,
    _DEAL_ID_RE,
    _request_actor,
    _request_user,
    limiter,
    registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deals"])


def _validate_deal_id(deal_id: str) -> None:
    if not _DEAL_ID_RE.match(deal_id):
        raise HTTPException(
            status_code=422,
            detail="Invalid deal_id format (expected e.g. 202604191342-7392)",
        )


# ── DB ledger reads ─────────────────────────────────────────────────────────

@router.get("/api/db/deals")
@limiter.limit("60/minute")
async def api_db_deals(
    request: Request,
    bot_slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    user: User = Depends(_request_user),
):
    limit = max(1, min(1000, int(limit)))
    uid = user.id

    def _query():
        deals = deal_store.get_deals(
            user_id=uid, bot_slug=bot_slug, status=status, limit=limit,
        )
        # Audit r1-020: batch-fetch orders so N deals cost one
        # query instead of N+1. Preserves the same response shape
        # ({"deal": ..., "orders": [...]}) so the SPA + Active Deals
        # page stay untouched.
        deal_ids = [d["id"] for d in deals]
        orders_by_deal = deal_store.get_orders_for_deal_ids(deal_ids, uid)
        return [
            {"deal": d, "orders": orders_by_deal.get(d["id"], [])}
            for d in deals
        ]

    return await asyncio.to_thread(_query)


@router.get("/api/db/deals/{deal_id}/orders")
@limiter.limit("60/minute")
async def api_db_deal_orders(
    deal_id: str, request: Request,
    user: User = Depends(_request_user),
):
    return await asyncio.to_thread(
        deal_store.get_deal_orders, deal_id, user.id,
    )


@router.get("/api/db/stats")
@limiter.limit("60/minute")
async def api_db_stats(
    request: Request,
    bot_slug: Optional[str] = None,
    user: User = Depends(_request_user),
):
    return await asyncio.to_thread(deal_store.compute_stats, user.id, bot_slug)


# ── Deal management sentinels ───────────────────────────────────────────────

class DealEditBody(BaseModel):
    tp_enabled: Optional[bool] = None
    tp_target_pct: Optional[float] = None
    sl_enabled: Optional[bool] = None
    sl_type: Optional[str] = None
    sl_pct: Optional[float] = None
    dca_enabled: Optional[bool] = None


@router.get("/api/bots/{slug}/deals/{deal_id}")
@limiter.limit("60/minute")
async def api_deal_get(
    slug: str, deal_id: str, request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    _validate_deal_id(deal_id)
    bot = await registry.get(user.id, slug)
    if bot:
        state = bot.read_state()
        for d in state.get("open_deals", []):
            if d.get("id") == deal_id:
                return {"deal": d, "orders": d.get("orders", [])}
    rows = await asyncio.to_thread(
        deal_store.get_deals, user_id=user.id, bot_slug=slug,
    )
    deal = next((d for d in rows if d["id"] == deal_id), None)
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    orders = await asyncio.to_thread(
        deal_store.get_deal_orders, deal_id, user.id,
    )
    return {"deal": deal, "orders": orders}


@router.patch("/api/bots/{slug}/deals/{deal_id}")
@limiter.limit("10/minute")
async def api_deal_edit(
    slug: str, deal_id: str, body: DealEditBody,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    _validate_deal_id(deal_id)
    settings: dict = {}
    tp_override = {}
    if body.tp_enabled is not None:
        tp_override["enabled"] = body.tp_enabled
    if body.tp_target_pct is not None:
        tp_override["target_pct"] = body.tp_target_pct
    if tp_override:
        settings["tp_override"] = tp_override

    sl_override = {}
    if body.sl_enabled is not None:
        sl_override["enabled"] = body.sl_enabled
    if body.sl_type is not None:
        sl_override["type"] = body.sl_type
    if body.sl_pct is not None:
        sl_override["pct"] = body.sl_pct
    if sl_override:
        settings["sl_override"] = sl_override

    if body.dca_enabled is not None:
        settings["dca_enabled"] = body.dca_enabled

    sentinel = paths.user_logs_dir(user.id) / f"{slug}.deal_edit_{deal_id}"
    sentinel.write_text(_json.dumps(settings), encoding="utf-8")
    _audit("deal_edit", slug, actor, user_id=user.id)
    return {"ok": True, "deal_id": deal_id}


async def _fetch_current_price_for_close(
    pair: str, exchange_name: str,
) -> float:
    """Portal-side current-price fetch for offline close operations.

    Uses ``PublicExchange`` (unauthenticated ticker endpoint) so no
    per-user API key is required — matches the pattern
    ``web/routes/chart.py`` already follows for the chart tab. Wrapped
    in ``asyncio.to_thread`` because ccxt is a sync library; calling
    it directly from the event loop would block.
    """
    from exchanges.public_exchange import PublicExchange

    def _fetch() -> float:
        client = PublicExchange(exchange_name.lower())
        ticker = client.get_ticker(pair)
        last = float(getattr(ticker, "last", 0.0) or 0.0)
        if last <= 0:
            raise ValueError(f"ticker.last is non-positive: {last!r}")
        return last

    return await asyncio.to_thread(_fetch)


def _build_portal_notifier(
    bot_config, *, user_id: int, slug: str,
) -> Optional[TelegramNotifier]:
    """Construct a TelegramNotifier for portal-origin close events.

    Returns None (no notification) when Telegram env-vars aren't set
    — ``TelegramNotifier.__init__`` raises ValueError in that case,
    which we translate to "silent, don't block the close". The close
    is the critical path; Telegram is a side channel.
    """
    try:
        notify_on = getattr(
            getattr(bot_config, "telegram", None), "notify_on", None,
        )
        return TelegramNotifier(notify_on=notify_on)
    except ValueError as e:
        # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — documented
        # path for operators who haven't configured Telegram. We log
        # at INFO rather than WARNING because this is a conscious
        # deployment choice, not an error.
        logger.info(
            "Portal close for %s/%s: Telegram notifier unavailable (%s) — "
            "close will proceed without notification",
            user_id, slug, e,
        )
        return None
    except Exception as e:
        # Any other instantiation failure → skip notification but
        # don't block the close. Log at WARNING because this is
        # unexpected (we already documented the no-env case above).
        logger.warning(
            "Portal close for %s/%s: unexpected TelegramNotifier error "
            "(%s) — close will proceed without notification",
            user_id, slug, type(e).__name__,
        )
        return None


async def _close_deal_offline(
    user_id: int, slug: str, deal_id: str, action: str, actor: str,
    bot_info,
) -> dict:
    """Offline-close path: bot process is stopped, portal closes the
    deal directly via ``DealCloseHandler``.

    All modes (paper, live, live-dry) share the same path because
    LiveEngine is still Phase 1 dry-run only — no real orders exist
    on the exchange yet. When Phase 3 enables real order placement,
    this will need to be extended with
    ``exchange.cancel_open_orders(...)`` before the state mutation;
    for now the offline close is state-only, identical to paper
    semantics.

    Raises ``HTTPException`` on any recoverable failure so the
    endpoint can surface clean HTTP status codes. The caller wraps
    unexpected exceptions in a 500 upstream.
    """
    # Load bot YAML — single source of truth for mode + pair +
    # exchange + taker_fee. State-file mode is lagging behind YAML
    # for never-started bots (see BotInfo._resolve_yaml_mode docs).
    try:
        bot_config = await asyncio.to_thread(
            load_bot_config, str(bot_info.config_file),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Bot config not found")
    except (ValueError, Exception) as e:
        logger.warning(
            "Offline close — invalid config for %s/%s: %s",
            user_id, slug, e,
        )
        raise HTTPException(status_code=400, detail="Invalid bot config")

    # Fetch current market price via the public ticker endpoint.
    # exchange_type resolves through the bot's exchange_account row —
    # the YAML now stores an account_id rather than a literal exchange
    # slug.
    from core import exchange_account_store
    account = exchange_account_store.get_account(
        bot_config.exchange_account_id,
    )
    if account is None or account["user_id"] != user_id:
        logger.warning(
            "Offline close — bot %s/%s references missing or foreign "
            "exchange_account_id=%d",
            user_id, slug, bot_config.exchange_account_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Bot's exchange account is missing or unowned",
        )
    exchange_name = str(account["exchange_type"])
    try:
        current_price = await _fetch_current_price_for_close(
            bot_config.pair, exchange_name,
        )
    except ValueError:
        # Audit r2-001 pattern: don't echo the upstream exception
        # into a 503 response — ccxt ValueError messages can carry
        # URL fragments or response-body snippets useful to an
        # attacker fingerprinting the exchange wiring.
        logger.exception(
            "offline close — invalid ticker response for user=%s slug=%s",
            user_id, slug,
        )
        raise HTTPException(
            status_code=503,
            detail="Exchange returned invalid ticker response",
        )
    except Exception as e:
        logger.warning(
            "Offline close — ticker fetch failed for %s/%s: %s",
            user_id, slug, e,
        )
        raise HTTPException(
            status_code=503,
            detail="Could not fetch current price from exchange",
        )

    # Load-mutate-persist runs inside an advisory file-lock that a
    # starting bot subprocess also claims during _load_state. Without
    # the lock the portal can read state.json, mutate in-memory, and
    # be mid-write while the bot's __init__ reads the pre-mutation
    # file — the bot then boots with stale state and its tick loop
    # may re-process a deal the portal just closed.
    #
    # Lock scope is tight: held only for the load + handler +
    # state-write trio. The ticker fetch above runs outside the lock
    # because it's a blocking network call and holding the lock
    # across it would starve a starting bot.
    lock_path = paths.bot_state_lock_path(user_id, slug)
    notifier = _build_portal_notifier(
        bot_config, user_id=user_id, slug=slug,
    )

    def _locked_close() -> dict:
        with exclusive_lock(lock_path, timeout=3.0):
            # Re-check running state inside the lock. The outer
            # running-check at the route handler may be stale by the
            # time we got here (registry cache TTL + event-loop
            # scheduling). BotInfo.running is a sync property that
            # does a pid-file + kill(pid, 0) probe, so it's cheap
            # and authoritative — safe to call inside the worker.
            if bot_info.running:
                return {
                    "__conflict__": True,
                    "status_code": 409,
                    "error": (
                        "Bot started during close attempt — "
                        "retry via the sentinel path."
                    ),
                }
            state, state_io = load_paper_state_from_file(
                bot_info.state_file, slug,
            )
            handler = DealCloseHandler(
                user_id=user_id,
                bot_slug=slug,
                bot_name=bot_config.name,
                state=state,
                state_io=state_io,
                taker_fee=bot_config.dca.taker_fee,
                notifier=notifier,
                # Portal context is synchronous — no tick loop, so
                # no non-blocking queue is needed. The notifier call
                # runs inline on the asyncio.to_thread worker.
                notify_enqueue=None,
            )
            return handler.close_deal(
                deal_id, current_price,
                action=action, triggered_by="portal",
            )

    try:
        result = await asyncio.to_thread(_locked_close)
    except LockTimeoutError as e:
        logger.warning(
            "Offline close — lock timeout for %s/%s: %s",
            user_id, slug, e,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not acquire state lock — bot may be starting. "
                "Retry."
            ),
        )

    if result.get("__conflict__"):
        raise HTTPException(
            status_code=result["status_code"],
            detail=result["error"],
        )
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("error") or "Could not close deal",
        )
    _audit(f"deal_{action}_offline", slug, actor, user_id=user_id)
    return {
        "ok": True,
        "method": "direct",
        "deal_id": deal_id,
        "action": action,
        "close_price": current_price,
        "deal": result["deal"],
    }


@router.delete("/api/bots/{slug}/deals/{deal_id}")
@limiter.limit("10/minute")
async def api_deal_action(
    slug: str, deal_id: str,
    request: Request,
    action: str = "close",
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Close or cancel an open deal.

    Running-bot path (default): write a sentinel file that the
    engine's tick loop consumes on its next iteration. Pre-this-
    refactor this was the ONLY path — if the bot was stopped the
    sentinel sat forever and the UI close button appeared to do
    nothing.

    Stopped-bot path (new): paper-mode bots close directly via
    ``DealCloseHandler`` — no sentinel, no wait. The portal fetches
    the current price via the public ticker, rehydrates the paper
    state from state.json, runs the handler, and persists the
    result. Live / dry-run bots still return 501 with a "start the
    bot first" hint (PR B).
    """
    _validate_deal_id(deal_id)
    if action not in ("cancel", "close"):
        raise HTTPException(
            status_code=400, detail="action must be cancel or close",
        )

    bot = await registry.get(user.id, slug)

    # Sentinel path — running bot OR unknown slug. Unknown-slug writes
    # match the pre-refactor behaviour (tests + tooling historically
    # write sentinels before the bot exists or after it's been
    # deleted); the file sits in logs/ until manually cleaned. Only
    # taking the offline handler branch for a KNOWN, STOPPED bot
    # keeps the refactor's new capability opt-in and the sentinel
    # fallback undisturbed.
    if bot is None or bot.running:
        sentinel = (
            paths.user_logs_dir(user.id) / f"{slug}.deal_{action}_{deal_id}"
        )
        sentinel.write_text("", encoding="utf-8")
        _audit(f"deal_{action}", slug, actor, user_id=user.id)
        return {
            "ok": True,
            "method": "sentinel",
            "deal_id": deal_id,
            "action": action,
        }

    # Offline path — known bot, not running. All modes share the
    # same state-only close (LiveEngine is Phase-1 dry-run only; no
    # real exchange orders exist yet to cancel). Phase 3 will add
    # an exchange.cancel_open_orders(...) call before mutation.
    return await _close_deal_offline(
        user.id, slug, deal_id, action, actor, bot,
    )


# ── Chart annotations ───────────────────────────────────────────────────────

class AnnotationBody(BaseModel):
    bot_slug: str
    type: str
    timeframe: str
    # Unix-second timestamps, clamped to a sane range (1970-01-01 .. ~2033).
    x1: int = Field(ge=0, le=2_000_000_000)
    y1: Optional[float] = None
    x2: Optional[int] = Field(default=None, ge=0, le=2_000_000_000)
    y2: Optional[float] = None
    label: Optional[str] = None
    color: str = "#00d4aa"


@router.post("/api/db/annotations")
@limiter.limit("30/minute")
async def api_db_annotations_create(
    body: AnnotationBody,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    uid = user.id

    def _insert():
        return deal_store.save_annotation(
            body.bot_slug,
            body.type,
            body.timeframe,
            body.x1,
            user_id=uid,
            y1=body.y1,
            x2=body.x2,
            y2=body.y2,
            label=body.label,
            color=body.color,
        )

    new_id = await asyncio.to_thread(_insert)
    return {"id": new_id}


@router.get("/api/db/annotations")
@limiter.limit("60/minute")
async def api_db_annotations_list(
    request: Request,
    bot_slug: str,
    timeframe: Optional[str] = None,
    user: User = Depends(_request_user),
):
    return await asyncio.to_thread(
        deal_store.list_annotations, bot_slug, user.id, timeframe,
    )


@router.delete("/api/db/annotations/all")
@limiter.limit("10/minute")
async def api_db_annotations_delete_all(
    request: Request,
    bot_slug: str,
    timeframe: Optional[str] = None,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Registered BEFORE the {ann_id} catch-all so FastAPI routes the
    literal `/all` path here instead of parsing "all" as an int."""
    removed = await asyncio.to_thread(
        deal_store.delete_annotations_for, bot_slug, user.id, timeframe,
    )
    _audit("annotations_clear", bot_slug, actor, user_id=user.id)
    return {"ok": True, "removed": removed}


@router.delete("/api/db/annotations/{ann_id}")
@limiter.limit("30/minute")
async def api_db_annotations_delete(
    ann_id: int,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    if not await asyncio.to_thread(deal_store.delete_annotation, ann_id, user.id):
        raise HTTPException(status_code=404, detail="Annotation not found")
    return {"ok": True}
