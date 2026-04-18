"""Bot CRUD + lifecycle routes extracted from web/app.py.

Routes:
  GET    /api/bots                        — list all bots + summary
  GET    /api/bots/{slug}                 — read state for one bot
  POST   /api/bots                        — create a new bot YAML
  POST   /api/bots/validate-config        — advisory warnings (no side effects)
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

import json
import logging

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from web.app import (
    _audit,
    _BOT_SLUG_RE,
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

# Maximum request-body size for /api/bots/validate-config (bytes).
# A fully-loaded bot YAML round-trips to ~4 KB of JSON; 64 KB gives
# ~16× headroom for pathological configs while keeping an authenticated
# DoS (30 reqs/min × n-megabyte bodies) bounded. The handler checks
# Content-Length BEFORE awaiting the JSON body so oversized payloads
# never land in memory.
MAX_CONFIG_BODY_BYTES = 64 * 1024

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
    dry-run flag set. Refuses paper-mode bots at the helper level.

    Slug is regex-validated here for defense-in-depth parity with
    ``main_live.py``'s own _BOT_SLUG_RE check — even though the
    subprocess re-validates and the registry only knows slugs produced
    by ``slugify()``, the extra layer makes path-traversal attempts
    fail fast with a clean 400 instead of sliding into the registry
    lookup path.
    """
    if not _BOT_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Invalid slug")
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


# ── Advisory config analysis ────────────────────────────────────────────────


def _config_warnings(cfg) -> tuple[list[dict], dict]:
    """Pure analyser — takes a validated BotConfig-like object and
    returns ``(warnings, summary)``. Split out so the endpoint stays
    thin and the logic is straightforward to unit-test.

    Warnings are advisory only; none of these ever blocks a save or a
    bot boot. The wizard's Review step renders them so the operator
    sees the shape of the ladder before committing."""
    warnings: list[dict] = []
    mode = cfg.mode.value
    dca = cfg.dca
    bos = float(dca.base_order_size)

    # Worst-case single-order analysis.
    max_orders = max(int(dca.max_orders), 1)
    multiplier = float(dca.multiplier)
    worst = bos * (multiplier ** max(max_orders - 1, 0))
    worst_multiple = (worst / bos) if bos > 0 else 0.0

    if worst_multiple > 50:
        warnings.append({
            "level": "high",
            "field": "dca",
            "message": (
                f"Worst-case DCA order is {worst_multiple:.0f}× base order "
                f"size ({worst:.6f} BTC). This can require significant "
                f"capital on the final levels."
            ),
        })
    elif worst_multiple > 20:
        warnings.append({
            "level": "medium",
            "field": "dca",
            "message": (
                f"Worst-case DCA order is {worst_multiple:.0f}× base order "
                f"size. Verify your account can fund the deepest level."
            ),
        })

    # Cumulative (summed base + every DCA).
    cumulative = sum(bos * (multiplier ** i) for i in range(max_orders))
    cumulative_multiple = (cumulative / bos) if bos > 0 else 0.0

    if cumulative_multiple > 150:
        warnings.append({
            "level": "high",
            "field": "dca",
            "message": (
                f"Total position can reach {cumulative_multiple:.0f}× base "
                f"({cumulative:.6f} BTC). Ensure your balance supports this."
            ),
        })
    elif cumulative_multiple > 100:
        warnings.append({
            "level": "medium",
            "field": "dca",
            "message": (
                f"Total position can reach {cumulative_multiple:.0f}× base "
                f"order size."
            ),
        })

    # Live-mode base-order sanity check. Paper + backtest run with
    # simulated balance so the same size doesn't carry the same risk.
    if mode == "live" and bos > 0.001:
        warnings.append({
            "level": "high",
            "field": "dca.base_order_size",
            "message": (
                f"Base order size {bos} BTC exceeds the conservative "
                f"0.001 BTC starting point for live trading. Consider "
                f"starting smaller until the strategy is proven."
            ),
        })

    # Geometric-explosion pattern — legal but rarely intentional.
    if multiplier >= 2.0 and max_orders >= 8:
        warnings.append({
            "level": "high",
            "field": "dca",
            "message": (
                f"Multiplier {multiplier} × {max_orders} orders produces "
                f"very fast escalation (order 8 = {multiplier ** 7:.0f}× "
                f"base). Double-check this is intentional."
            ),
        })

    # Live bots without a drawdown guard have no automatic brake beyond
    # the balance guard. Flag as medium, not high — some operators
    # deliberately run without it.
    if mode == "live" and not cfg.drawdown_guard.enabled:
        warnings.append({
            "level": "medium",
            "field": "drawdown_guard",
            "message": (
                "Drawdown guard is disabled. Enabling it is recommended "
                "for live trading as a last-resort kill switch."
            ),
        })

    summary = {
        "mode": mode,
        "base_order_size": bos,
        "worst_case_dca": worst,
        "worst_case_multiple": worst_multiple,
        "cumulative_position": cumulative,
        "cumulative_multiple": cumulative_multiple,
        "max_orders": max_orders,
        "multiplier": multiplier,
    }
    return warnings, summary


@router.post("/api/bots/validate-config")
@limiter.limit("30/minute")
async def validate_config(
    request: Request,
    actor: str = Depends(_request_actor),
):
    """Analyse a bot config and return advisory warnings + a numeric
    summary. Never enforces — the wizard renders the result so the
    operator can adjust ladder/DCA parameters before saving.

    Body-size cap: the handler refuses requests larger than
    ``MAX_CONFIG_BODY_BYTES`` before reading the body, so an
    authenticated client cannot DoS the process with giant JSON.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        # Fall back to consuming up to MAX+1 bytes so chunked-encoded
        # clients (no Content-Length header) are still bounded — we
        # read lazily and abort once the limit is crossed.
        body_bytes = b""
        async for chunk in request.stream():
            body_bytes += chunk
            if len(body_bytes) > MAX_CONFIG_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Config body too large "
                        f"(>{MAX_CONFIG_BODY_BYTES} bytes)"
                    ),
                )
    else:
        try:
            cl_int = int(content_length)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length",
            )
        if cl_int > MAX_CONFIG_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Config body too large "
                    f"({cl_int} > {MAX_CONFIG_BODY_BYTES} bytes)"
                ),
            )
        body_bytes = await request.body()

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="Config must be a JSON object",
        )

    try:
        cfg = _validate_bot_payload(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    warnings, summary = _config_warnings(cfg)
    return {"warnings": warnings, "summary": summary}


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
