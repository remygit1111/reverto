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

from core import deal_store
from web.app import (
    _audit,
    _DEAL_ID_RE,
    _request_actor,
    limiter,
    LOG_DIR,
    registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deals"])


def _validate_deal_id(deal_id: str) -> None:
    if not _DEAL_ID_RE.match(deal_id):
        raise HTTPException(
            status_code=422,
            detail="Invalid deal_id format (expected e.g. PAPER-0001)",
        )


# ── DB ledger reads ─────────────────────────────────────────────────────────

@router.get("/api/db/deals")
@limiter.limit("60/minute")
async def api_db_deals(
    request: Request,
    bot_slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
):
    limit = max(1, min(1000, int(limit)))

    def _query():
        deals = deal_store.get_deals(bot_slug=bot_slug, status=status, limit=limit)
        return [
            {"deal": d, "orders": deal_store.get_deal_orders(d["id"])}
            for d in deals
        ]

    return await asyncio.to_thread(_query)


@router.get("/api/db/deals/{deal_id}/orders")
@limiter.limit("60/minute")
async def api_db_deal_orders(deal_id: str, request: Request):
    return await asyncio.to_thread(deal_store.get_deal_orders, deal_id)


@router.get("/api/db/stats")
@limiter.limit("60/minute")
async def api_db_stats(request: Request, bot_slug: Optional[str] = None):
    return await asyncio.to_thread(deal_store.compute_stats, bot_slug)


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
):
    _validate_deal_id(deal_id)
    bot = await registry.get(slug)
    if bot:
        state = bot.read_state()
        for d in state.get("open_deals", []):
            if d.get("id") == deal_id:
                return {"deal": d, "orders": d.get("orders", [])}
    rows = await asyncio.to_thread(deal_store.get_deals, bot_slug=slug)
    deal = next((d for d in rows if d["id"] == deal_id), None)
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    orders = await asyncio.to_thread(deal_store.get_deal_orders, deal_id)
    return {"deal": deal, "orders": orders}


@router.patch("/api/bots/{slug}/deals/{deal_id}")
@limiter.limit("10/minute")
async def api_deal_edit(
    slug: str, deal_id: str, body: DealEditBody,
    request: Request,
    actor: str = Depends(_request_actor),
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

    sentinel = LOG_DIR / f"{slug}.deal_edit_{deal_id}"
    sentinel.write_text(_json.dumps(settings), encoding="utf-8")
    _audit("deal_edit", slug, actor)
    return {"ok": True, "deal_id": deal_id}


@router.delete("/api/bots/{slug}/deals/{deal_id}")
@limiter.limit("10/minute")
async def api_deal_action(
    slug: str, deal_id: str,
    request: Request,
    action: str = "close",
    actor: str = Depends(_request_actor),
):
    _validate_deal_id(deal_id)
    if action not in ("cancel", "close"):
        raise HTTPException(status_code=400, detail="action must be cancel or close")
    sentinel = LOG_DIR / f"{slug}.deal_{action}_{deal_id}"
    sentinel.write_text("", encoding="utf-8")
    _audit(f"deal_{action}", slug, actor)
    return {"ok": True, "deal_id": deal_id, "action": action}


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
):
    new_id = await asyncio.to_thread(
        deal_store.save_annotation,
        body.bot_slug,
        body.type,
        body.timeframe,
        body.x1,
        body.y1,
        body.x2,
        body.y2,
        body.label,
        body.color,
    )
    return {"id": new_id}


@router.get("/api/db/annotations")
@limiter.limit("60/minute")
async def api_db_annotations_list(
    request: Request,
    bot_slug: str,
    timeframe: Optional[str] = None,
):
    return await asyncio.to_thread(deal_store.list_annotations, bot_slug, timeframe)


@router.delete("/api/db/annotations/all")
@limiter.limit("10/minute")
async def api_db_annotations_delete_all(
    request: Request,
    bot_slug: str,
    timeframe: Optional[str] = None,
    actor: str = Depends(_request_actor),
):
    """Registered BEFORE the {ann_id} catch-all so FastAPI routes the
    literal `/all` path here instead of parsing "all" as an int."""
    removed = await asyncio.to_thread(
        deal_store.delete_annotations_for, bot_slug, timeframe,
    )
    _audit("annotations_clear", bot_slug, actor)
    return {"ok": True, "removed": removed}


@router.delete("/api/db/annotations/{ann_id}")
@limiter.limit("30/minute")
async def api_db_annotations_delete(
    ann_id: int,
    request: Request,
    actor: str = Depends(_request_actor),
):
    if not await asyncio.to_thread(deal_store.delete_annotation, ann_id):
        raise HTTPException(status_code=404, detail="Annotation not found")
    return {"ok": True}
