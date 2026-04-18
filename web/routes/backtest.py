"""Backtest persistence routes extracted from web/app.py.

Routes:
  POST   /api/backtest/save          — persist a completed backtest run
  GET    /api/backtest/runs          — list runs, optionally per-bot
  DELETE /api/backtest/runs/{id}     — delete one run
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import deal_store
from core.user import User
from web.app import _audit, _request_actor, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backtest"])


class BacktestSaveBody(BaseModel):
    slug: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    params: dict = Field(default_factory=dict)
    summary: dict = Field(default_factory=dict)


@router.post("/api/backtest/save")
@limiter.limit("60/minute")
async def api_backtest_save(
    body: BacktestSaveBody,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Persist one completed backtest run.

    Called automatically by the frontend after a successful run so
    the Backtest History view has something to show. The body mirrors
    RevertoBacktest._buildResults flattened into a summary dict, plus
    the user-facing params (start/end/timeframe/initial_balance).
    """
    run_id = await asyncio.to_thread(
        deal_store.save_backtest_run,
        body.slug, body.name, body.params, body.summary,
        user.id,
    )
    return {"ok": True, "id": run_id}


@router.get("/api/backtest/runs")
@limiter.limit("60/minute")
async def api_backtest_runs(
    request: Request,
    slug: Optional[str] = None,
    limit: int = 100,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Return recent backtest runs, optionally filtered by bot slug."""
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    if slug:
        runs = await asyncio.to_thread(
            deal_store.get_backtest_runs, slug, user.id, limit,
        )
    else:
        runs = await asyncio.to_thread(
            deal_store.get_all_backtest_runs, user.id, limit,
        )
    return {"runs": runs}


@router.delete("/api/backtest/runs/{run_id}")
@limiter.limit("10/minute")
async def api_backtest_run_delete(
    run_id: int,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    deleted = await asyncio.to_thread(
        deal_store.delete_backtest_run, run_id, user.id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Run not found")
    _audit("backtest_delete", str(run_id), actor)
    return {"ok": True}
