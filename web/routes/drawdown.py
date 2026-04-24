"""Drawdown-reset route extracted from web/app.py.

Routes:
  POST /api/bots/{slug}/drawdown/reset — clear triggered drawdown guard
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from core.user import User
from web.app import (
    _BOT_SLUG_RE, _audit, _request_actor, _request_user, limiter, registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["drawdown"])


@router.post("/api/bots/{slug}/drawdown/reset")
@limiter.limit("10/minute")
async def api_drawdown_reset(
    slug: str, request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Clear the drawdown guard's triggered state for a bot.

    Rewrites the bot's state.json with a cleared ``drawdown_guard`` blob.
    The engine's ``_load_state`` picks up the change on the next tick —
    we can't call DrawdownGuard.reset() directly because the engine
    runs in a separate subprocess. state.json is the shared contract.
    """
    if not _BOT_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Invalid slug")

    bot = await registry.get(user.id, slug)
    if not bot:
        raise HTTPException(status_code=404, detail="Unknown bot")

    state_file = bot.state_file
    if not state_file.exists():
        raise HTTPException(status_code=404, detail="Bot state not found")

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Audit pd-001: the state-file path and decoder errno land
        # in the raw exception. Generic detail keeps the attacker
        # blind; logger.exception preserves every detail operators
        # need to triage.
        logger.exception(
            "drawdown state read failed user=%s slug=%s",
            user.id, slug,
        )
        raise HTTPException(
            status_code=500, detail="Failed to read bot state",
        )

    data["drawdown_guard"] = {
        "peak_value": None,
        "triggered": False,
        "trigger_reason": None,
    }
    data["paused_by_drawdown"] = False

    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(state_file)

    _audit("drawdown_reset", slug, actor, user_id=user.id)
    logger.warning("Drawdown guard reset for %s by %s", slug, actor)
    return {"ok": True, "bot": slug}
