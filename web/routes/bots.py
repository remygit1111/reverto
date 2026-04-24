"""Bot CRUD + lifecycle routes extracted from web/app.py.

Routes:
  GET    /api/bots                        — list all bots + summary
  GET    /api/bots/{slug}                 — read state for one bot
  POST   /api/bots                        — create a new bot YAML
  POST   /api/bots/validate-config        — advisory warnings (no side effects)
  GET    /api/bots/{slug}/config          — read YAML
  PUT    /api/bots/{slug}/config          — overwrite YAML
  DELETE /api/bots/{slug}                 — delete YAML (bot must be stopped)
  GET    /api/bots/{slug}/export          — YAML download with metadata header
  POST   /api/bots/{slug}/duplicate       — server-side copy to new slug
  POST   /api/bots/import                 — create a new bot from uploaded YAML
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
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from core import paths
from core.user import User
from web.app import (
    _audit,
    _BOT_SLUG_RE,
    _bot_yaml_path,
    _compute_summary,
    _request_actor,
    _request_user,
    _validate_bot_payload,
    limiter,
    registry,
    restart_bot,
    slugify,
    start_bot,
    start_bot_dry_run,
    stop_bot,
)

logger = logging.getLogger(__name__)

# Maximum request-body size for config-ingesting endpoints (bytes).
# Applies to validate-config, POST /api/bots (create), and
# PUT /api/bots/{slug}/config (update). A fully-loaded bot YAML
# round-trips to ~4 KB of JSON; 64 KB gives ~16× headroom for
# pathological configs while keeping an authenticated DoS (20-30
# reqs/min × n-megabyte bodies) bounded. The helpers below check
# Content-Length BEFORE awaiting the JSON body so oversized payloads
# never land in memory.
MAX_CONFIG_BODY_BYTES = 64 * 1024

router = APIRouter(tags=["bots"])


# ── Body-size helpers (shared by every config-ingesting endpoint) ───────────

async def _read_body_with_cap(request: Request, cap: int) -> bytes:
    """Read the request body enforcing a byte cap.

    Two paths, matching the audit-v23 validate-config handler:

    * **Content-Length present** — parse the header and refuse
      (``413``) before touching the body if it's already over cap.
      Malformed Content-Length maps to ``400`` so the client gets a
      meaningful error instead of a 500.
    * **Content-Length absent** (chunked / Transfer-Encoding) — stream
      the body and abort the moment the accumulated size crosses the
      cap. Without this branch a chunked client could still DoS the
      process by sending an unbounded number of chunks.

    Raises ``HTTPException`` in both refusal paths so the caller just
    ``await``s and lets FastAPI surface the response. Returns the raw
    body bytes on success.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        body_bytes = b""
        async for chunk in request.stream():
            body_bytes += chunk
            if len(body_bytes) > cap:
                raise HTTPException(
                    status_code=413,
                    detail=f"Config body too large (>{cap} bytes)",
                )
        return body_bytes

    try:
        cl_int = int(content_length)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid Content-Length",
        )
    if cl_int > cap:
        raise HTTPException(
            status_code=413,
            detail=f"Config body too large ({cl_int} > {cap} bytes)",
        )
    return await request.body()


def _parse_json_object_body(body_bytes: bytes) -> dict:
    """Decode + parse a body that must be a JSON object.

    Returns the parsed dict. Anything that's not a JSON object —
    malformed JSON, a top-level list/string/number, non-UTF-8 bytes —
    raises ``HTTPException(400)`` so the three config endpoints share
    the same error contract.
    """
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="Config must be a JSON object",
        )
    return body


# ── Read ────────────────────────────────────────────────────────────────────

@router.get("/api/bots")
@limiter.limit("120/minute")
async def get_bots(
    request: Request, user: User = Depends(_request_user),
):
    bots = [b.read_state() for b in await registry.all(user_id=user.id)]

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
async def get_bot(
    slug: str, request: Request, user: User = Depends(_request_user),
):
    # Audit v26-17: pre-fix returnde dit endpoint ``{"error": ...}``
    # met HTTP 200 bij een onbekende slug — HTTP-semantics eist 404
    # voor "resource niet gevonden." Andere slug-endpoints (config,
    # drawdown, delete) raisen al HTTPException(404), dus alignen.
    bot = await registry.get(user.id, slug)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Unknown bot: {slug}")
    return bot.read_state()


# ── Lifecycle ───────────────────────────────────────────────────────────────

@router.post("/api/bots/{slug}/start")
@limiter.limit("20/minute")
async def api_start(
    slug: str, request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    _audit("bot_start", slug, actor, user_id=user.id)
    return await start_bot(user.id, slug)


@router.post("/api/bots/{slug}/start-dry-run")
@limiter.limit("20/minute")
async def api_start_dry_run(
    slug: str, request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
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
    _audit("bot_start_dry_run", slug, actor, user_id=user.id)
    return await start_bot_dry_run(user.id, slug)


@router.post("/api/bots/{slug}/stop")
@limiter.limit("20/minute")
async def api_stop(
    slug: str, request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    _audit("bot_stop", slug, actor, user_id=user.id)
    return await stop_bot(user.id, slug)


@router.post("/api/bots/{slug}/restart")
@limiter.limit("20/minute")
async def api_restart(
    slug: str, request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    _audit("bot_restart", slug, actor, user_id=user.id)
    return await restart_bot(user.id, slug)


@router.post("/api/bots/{slug}/deal/start")
@limiter.limit("5/minute")
async def api_deal_start(
    slug: str, request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Manual deal trigger — writes a sentinel file that the running
    paper engine consumes on its next tick to force-open a deal."""
    bot = await registry.get(user.id, slug)
    if not bot:
        raise HTTPException(status_code=404, detail=f"Unknown bot: {slug}")
    trigger = paths.bot_manual_trigger_path(user.id, slug)
    try:
        trigger.write_text("", encoding="utf-8")
    except OSError:
        # Audit pd-001: scrub the raw OSError detail from the
        # response — it leaks on-disk paths / mount-point info.
        # Full detail lands in portal.log via logger.exception.
        logger.exception(
            "manual-deal trigger write failed user=%s slug=%s",
            user.id, slug,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to write manual-deal trigger",
        )
    _audit("bot_manual_deal", slug, actor, user_id=user.id)
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

    Body-size cap: ``_read_body_with_cap`` refuses requests larger
    than ``MAX_CONFIG_BODY_BYTES`` before reading the body, so an
    authenticated client cannot DoS the process with giant JSON.
    """
    body_bytes = await _read_body_with_cap(request, MAX_CONFIG_BODY_BYTES)
    body = _parse_json_object_body(body_bytes)

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
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Maak een nieuwe bot YAML aan. Slug komt uit de bot naam.

    Body-size cap identical to validate-config: a DoS via a
    gigantic YAML payload is the same attack surface here — every
    authenticated endpoint that ingests config JSON needs the
    guard. Audit v24/v25 flagged this as MEDIUM #3.
    """
    body_bytes = await _read_body_with_cap(request, MAX_CONFIG_BODY_BYTES)
    body = _parse_json_object_body(body_bytes)

    try:
        cfg = _validate_bot_payload(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    try:
        slug = slugify(cfg.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    path = _bot_yaml_path(user.id, slug)
    if path.exists():
        raise HTTPException(status_code=409, detail=f"Bot {slug} already exists")

    # user_bots_dir ensures config/bots/<user_id>/ exists.
    paths.user_bots_dir(user.id)
    inner = body.get("bot", body)
    path.write_text(
        yaml.safe_dump({"bot": inner}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    await registry.invalidate()
    _audit("bot_create", slug, actor, user_id=user.id)
    return {"ok": True, "slug": slug}


@router.get("/api/bots/{slug}/config")
@limiter.limit("60/minute")
async def get_bot_config(
    slug: str,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    path = _bot_yaml_path(user.id, slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bot not found")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        # Audit pd-001: YAML parser errors echo line/col numbers
        # and sometimes the offending snippet — tame in principle
        # but not worth leaking to a remote caller. Operator gets
        # the full trace via logger.exception.
        logger.exception(
            "bot config YAML parse failed user=%s slug=%s",
            user.id, slug,
        )
        raise HTTPException(
            status_code=500, detail="Failed to parse bot config",
        )
    return raw


@router.put("/api/bots/{slug}/config")
@limiter.limit("10/minute")
async def update_bot_config(
    slug: str,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Overwrite a bot YAML in place. Body-size cap mirrors
    ``create_bot`` + ``validate_config`` — same DoS surface, same
    64 KB guard."""
    path = _bot_yaml_path(user.id, slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bot not found")

    body_bytes = await _read_body_with_cap(request, MAX_CONFIG_BODY_BYTES)
    body = _parse_json_object_body(body_bytes)

    try:
        _validate_bot_payload(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    inner = body.get("bot", body)
    path.write_text(
        yaml.safe_dump({"bot": inner}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _audit("bot_update", slug, actor, user_id=user.id)
    return {"ok": True, "slug": slug}


@router.delete("/api/bots/{slug}")
@limiter.limit("10/minute")
async def delete_bot(
    slug: str,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    bot = await registry.get(user.id, slug)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.running:
        raise HTTPException(
            status_code=409, detail="Bot is running — stop it before deleting",
        )
    path = _bot_yaml_path(user.id, slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="YAML not found")
    path.unlink()
    await registry.invalidate()
    _audit("bot_delete", slug, actor, user_id=user.id)
    return {"ok": True, "slug": slug}


# ── Import / Export / Duplicate ─────────────────────────────────────────────

def _reverto_version() -> str:
    """Best-effort version string for export metadata. Short git SHA when
    available, 'unknown' otherwise so the header never crashes the export
    on a deployment without git in PATH."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2.0,
            cwd=Path(__file__).resolve().parent.parent.parent,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except Exception as e:
        logger.debug("_reverto_version: git sha lookup failed: %s", e)
    return "unknown"


@router.get("/api/bots/{slug}/export")
@limiter.limit("30/minute")
async def export_bot(
    slug: str,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Export a bot config as YAML. Strategy-only: no credentials, no
    state, no deal history — just the YAML the operator could re-import
    on another deployment. Header comments record export metadata so a
    future compatibility check has something to anchor on."""
    path = _bot_yaml_path(user.id, slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bot not found")

    config_yaml = path.read_text(encoding="utf-8")
    header = (
        "# Reverto bot export\n"
        f"# Exported: {datetime.now(timezone.utc).isoformat()}\n"
        f"# Original slug: {slug}\n"
        f"# Reverto version: {_reverto_version()}\n"
        "#\n"
        "# Import via: Portal → Bots → Import bot\n"
        "\n"
    )

    _audit("bot_export", slug, actor, user_id=user.id)
    return Response(
        content=header + config_yaml,
        media_type="application/x-yaml",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}.yaml"',
        },
    )


@router.post("/api/bots/{slug}/duplicate")
@limiter.limit("10/minute")
async def duplicate_bot(
    slug: str,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Duplicate a bot config to a new slug. Request body is a JSON
    object with ``new_slug``. State, deals, orders, and credentials are
    NOT copied — the duplicate starts fresh."""
    body_bytes = await _read_body_with_cap(request, MAX_CONFIG_BODY_BYTES)
    body = _parse_json_object_body(body_bytes)
    new_slug = str(body.get("new_slug", "")).strip()

    if not _BOT_SLUG_RE.match(new_slug):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid slug. Must match {_BOT_SLUG_RE.pattern}",
        )

    source_path = _bot_yaml_path(user.id, slug)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source bot not found")

    target_path = _bot_yaml_path(user.id, new_slug)
    if target_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Bot with slug '{new_slug}' already exists",
        )

    try:
        raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        # Audit r2-001: mirror the pd-001 scrub pattern. YAMLError
        # __str__ embeds line/column/snippet of the offending source
        # — leaking that via HTTPException detail on a public endpoint
        # fingerprints on-disk layout. Full trace goes to portal.log
        # via logger.exception; client gets a generic 500.
        logger.exception(
            "bot duplicate source YAML parse failed user=%s source=%s target=%s",
            user.id, slug, new_slug,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to parse source bot config for duplication",
        )

    # Keep the source layout: _bot_yaml_path files are all wrapped in
    # {"bot": {...}} by create_bot/update_bot_config, so the duplicate
    # round-trips through the same envelope.
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="Source YAML is malformed")
    inner = raw.get("bot", raw)
    if isinstance(inner, dict) and "name" in inner:
        # Human-readable name: derive from the new slug so the card
        # label matches the identifier. Operator can rename afterwards
        # via the config editor if they prefer something else.
        inner["name"] = new_slug.replace("_", " ").replace("-", " ").title()

    paths.user_bots_dir(user.id)
    target_path.write_text(
        yaml.safe_dump({"bot": inner}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    await registry.invalidate()
    _audit(f"bot_duplicate from={slug}", new_slug, actor, user_id=user.id)
    return {"ok": True, "slug": new_slug}


@router.post("/api/bots/import")
@limiter.limit("10/minute")
async def import_bot(
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Create a new bot from a YAML body. Target slug comes from the
    ``?slug=`` query param so the operator can pick a non-conflicting
    name at import time. Full Pydantic schema validation runs before
    anything hits disk — bad YAML or schema violations are refused
    without side effects."""
    target_slug = request.query_params.get("slug", "").strip()
    if not _BOT_SLUG_RE.match(target_slug):
        raise HTTPException(
            status_code=400,
            detail=f"Query param 'slug' must match {_BOT_SLUG_RE.pattern}",
        )

    target_path = _bot_yaml_path(user.id, target_slug)
    if target_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Bot with slug '{target_slug}' already exists",
        )

    body_bytes = await _read_body_with_cap(request, MAX_CONFIG_BODY_BYTES)
    try:
        yaml_text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Body must be UTF-8 YAML")

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400, detail="YAML must be a top-level object",
        )

    # _validate_bot_payload accepts both {"bot": {...}} (standard export
    # format) and the flat {...} fallback, so either import shape works.
    try:
        _validate_bot_payload(parsed)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail=f"Schema validation failed: {e}",
        )

    inner = parsed.get("bot", parsed)
    if isinstance(inner, dict) and "name" in inner:
        inner["name"] = target_slug.replace("_", " ").replace("-", " ").title()

    paths.user_bots_dir(user.id)
    target_path.write_text(
        yaml.safe_dump({"bot": inner}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    await registry.invalidate()
    _audit("bot_import", target_slug, actor, user_id=user.id)
    return {"ok": True, "slug": target_slug}
