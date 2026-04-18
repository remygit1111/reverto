"""Bot CRUD + lifecycle routes extracted from web/app.py.

Routes:
  GET    /api/bots                        — list all bots + summary
  GET    /api/bots/{slug}                 — read state for one bot
  POST   /api/bots                        — create a new bot YAML
  GET    /api/bots/{slug}/config          — read YAML
  PUT    /api/bots/{slug}/config          — overwrite YAML
  DELETE /api/bots/{slug}                 — delete YAML (bot must be stopped)
  POST   /api/bots/{slug}/start           — spawn main_paper.py subprocess
  POST   /api/bots/{slug}/start-dry-run   — spawn main_live.py --dry-run
  POST   /api/bots/{slug}/stop            — SIGTERM running bot
  POST   /api/bots/{slug}/restart         — stop + start (mode-aware)
  POST   /api/bots/{slug}/deal/start      — write manual-trigger sentinel

NOT migrated (still in web/app.py): WebSocket endpoints (/ws/logs/{slug},
/ws/state) — WS endpoints don't pass through include_router cleanly
with BaseHTTPMiddleware auth, and keeping them in web/app.py preserves
the existing auth flow.
"""

from __future__ import annotations

import logging

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from web.app import (
    _audit,
    _bot_yaml_path,
    _compute_summary,
    _request_actor,
    _validate_bot_payload,
    CONFIG_DIR,
    LOG_DIR,
    limiter,
    registry,
    restart_bot,
    slugify,
    start_bot,
    start_bot_dry_run,
    stop_bot,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bots"])


# ── Read ────────────────────────────────────────────────────────────────────

@router.get("/api/bots")
@limiter.limit("120/minute")
async def get_bots(request: Request):
    bots = [b.read_state() for b in await registry.all()]

    all_open = []
    for b in bots:
        for d in b.get("open_deals", []):
            d["bot_name"] = b.get("bot_name", b.get("slug"))
            d["bot_slug"] = b.get("slug")
            d["exchange"] = b.get("exchange")
            all_open.append(d)

    summary = _compute_summary(bots)
    # Backwards-compat: existing /api/bots callers expected exactly
    # the 4 keys below. closed_deals is extra in the new helper but
    # additive keys are safe (SPA reads by name).
    return {
        "bots": bots,
        "summary": {
            "total_pnl_btc": summary["total_pnl_btc"],
            "active_bots":   summary["active_bots"],
            "total_bots":    summary["total_bots"],
            "open_deals":    summary["open_deals"],
        },
        "all_open_deals": all_open,
    }


@router.get("/api/bots/{slug}")
@limiter.limit("120/minute")
async def get_bot(slug: str, request: Request):
    bot = await registry.get(slug)
    if not bot:
        return {"error": f"Unknown bot: {slug}"}
    return bot.read_state()


# ── Lifecycle ───────────────────────────────────────────────────────────────

@router.post("/api/bots/{slug}/start")
@limiter.limit("20/minute")
async def api_start(slug: str, request: Request, actor: str = Depends(_request_actor)):
    _audit("bot_start", slug, actor)
    return await start_bot(slug)


@router.post("/api/bots/{slug}/start-dry-run")
@limiter.limit("20/minute")
async def api_start_dry_run(
    slug: str, request: Request, actor: str = Depends(_request_actor),
):
    """Phase-1 launcher: boot a live-mode bot via main_live.py with the
    dry-run flag set. Refuses paper-mode bots at the helper level."""
    _audit("bot_start_dry_run", slug, actor)
    return await start_bot_dry_run(slug)


@router.post("/api/bots/{slug}/stop")
@limiter.limit("20/minute")
async def api_stop(slug: str, request: Request, actor: str = Depends(_request_actor)):
    _audit("bot_stop", slug, actor)
    return await stop_bot(slug)


@router.post("/api/bots/{slug}/restart")
@limiter.limit("20/minute")
async def api_restart(slug: str, request: Request, actor: str = Depends(_request_actor)):
    _audit("bot_restart", slug, actor)
    return await restart_bot(slug)


@router.post("/api/bots/{slug}/deal/start")
@limiter.limit("5/minute")
async def api_deal_start(slug: str, request: Request, actor: str = Depends(_request_actor)):
    """Manual deal trigger — writes a sentinel file that the running
    paper engine consumes on its next tick to force-open a deal."""
    bot = await registry.get(slug)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Unknown bot: {slug}")
    trigger = LOG_DIR / f"{slug}.manual_trigger"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        trigger.write_text("", encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write trigger: {e}")
    _audit("bot_manual_deal", slug, actor)
    return {"ok": True}


# ── CRUD ────────────────────────────────────────────────────────────────────

@router.post("/api/bots")
@limiter.limit("20/minute")
async def create_bot(
    body: dict,
    request: Request,
    actor: str = Depends(_request_actor),
):
    """Maak een nieuwe bot YAML aan. Slug komt uit de bot naam."""
    try:
        cfg = _validate_bot_payload(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    try:
        slug = slugify(cfg.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    path = _bot_yaml_path(slug)
    if path.exists():
        raise HTTPException(status_code=409, detail=f"Bot {slug} already exists")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    inner = body.get("bot", body)
    path.write_text(
        yaml.safe_dump({"bot": inner}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    await registry.invalidate()
    _audit("bot_create", slug, actor)
    return {"ok": True, "slug": slug}


@router.get("/api/bots/{slug}/config")
@limiter.limit("60/minute")
async def get_bot_config(
    slug: str,
    request: Request,
    actor: str = Depends(_request_actor),
):
    path = _bot_yaml_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bot not found")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise HTTPException(status_code=500, detail=f"YAML parse error: {e}")
    return raw


@router.put("/api/bots/{slug}/config")
@limiter.limit("10/minute")
async def update_bot_config(
    slug: str,
    body: dict,
    request: Request,
    actor: str = Depends(_request_actor),
):
    path = _bot_yaml_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bot not found")
    try:
        _validate_bot_payload(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    inner = body.get("bot", body)
    path.write_text(
        yaml.safe_dump({"bot": inner}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _audit("bot_update", slug, actor)
    return {"ok": True, "slug": slug}


@router.delete("/api/bots/{slug}")
@limiter.limit("10/minute")
async def delete_bot(
    slug: str,
    request: Request,
    actor: str = Depends(_request_actor),
):
    bot = await registry.get(slug)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.running:
        raise HTTPException(
            status_code=409, detail="Bot is running — stop it before deleting",
        )
    path = _bot_yaml_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="YAML not found")
    path.unlink()
    await registry.invalidate()
    _audit("bot_delete", slug, actor)
    return {"ok": True, "slug": slug}
