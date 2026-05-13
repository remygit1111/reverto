# web/app.py
# Reverto Web Portal — FastAPI backend
# Multi-bot: reads state from logs/{slug}.state.json per bot.
# Manages bot processes via start/stop API.

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from uuid import uuid4

from config.config_loader import load_bot_config
from config.models import BotConfig, Mode
from core import paths, user_store
from core.database import DatabaseMigrationError, init_db as _init_db
from core.ids import DEAL_ID_RE
from core.logging_setup import RequestIdFilter as _RequestIdFilter
from core.logging_setup import request_id_ctx as _request_id_ctx
from core.user import User
from notifications.telegram import TelegramNotifier
from paper.paper_engine import NOTIFY_DRAIN_TIMEOUT_S

import ccxt
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Maximum file size for state.json — prevents OOM on corrupt/oversize files
_MAX_STATE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Extra slack on top of the engine's notify-drain budget for
# process-startup/teardown overhead between engine.stop() returning and
# the PID disappearing from the process table. Portal-stop wait-deadline
# = NOTIFY_DRAIN_TIMEOUT_S + _STOP_SAFETY_MARGIN_S, so any increase in
# the drain budget automatically propagates to the portal.
_STOP_SAFETY_MARGIN_S = 3.0


class BotStateModel(BaseModel):
    """Pydantic schema for logs/{slug}.state.json — protects against
    corrupt or injected JSON with unexpected types or values. Extra
    fields are ignored (not stripped) so future fields don't crash
    older portal versions."""

    model_config = ConfigDict(extra="ignore")

    bot_name:            str   = ""
    mode:                str   = ""
    exchange:            str   = ""
    pair:                str   = ""
    balance_btc:         float = Field(default=0.0, ge=-1000.0, le=1000.0)
    initial_balance_btc: float = 0.0
    total_pnl_btc:       float = 0.0
    win_rate:            float = 0.0
    open_deals_count:    int   = 0
    closed_deals_count:  int   = 0
    open_deals:          list  = Field(default_factory=list)
    closed_deals:        list  = Field(default_factory=list)
    current_price:       float = 0.0
    schedule_open:       bool  = False
    has_trading_windows: bool  = False
    started_at:          Optional[str] = None
    updated_at:          Optional[str] = None
    fees_paid_btc:       float = 0.0
    indicators:          dict  = Field(default_factory=dict)
    # Lifecycle-stability fields (PR: tweak/bot-lifecycle-stability).
    # last_heartbeat is stamped on every engine tick so the portal can
    # distinguish a live bot from a silent-exit (process gone but
    # state.json frozen on running=true). heartbeat_interval_sec lets
    # the staleness threshold scale with the engine's poll cadence.
    # stopped_at + stopped_reason are written by the portal's silent-
    # exit reconcile path; bots that exit gracefully via mark_stopped()
    # leave them None.
    last_heartbeat:          Optional[str] = None
    heartbeat_interval_sec:  Optional[int] = None
    stopped_at:              Optional[str] = None
    stopped_reason:          Optional[str] = None
    # Schema-version stamp (PR: tweak/killmode-process-mismatch-detection).
    # Engines stamp ``STATE_SCHEMA_VERSION`` on every tick so the
    # portal-startup reconcile can spot bots running on stale code
    # (e.g. surviving a portal-restart that didn't kill the cgroup).
    # Optional+None default keeps legacy state.json files validating
    # cleanly — the reconcile path treats None as "v1" (definitely
    # different from the current version → restart).
    state_schema_version:    Optional[int] = None


# ── Lifecycle-stability tunables (PR: tweak/bot-lifecycle-stability) ──────────

# Threshold for declaring a bot's heartbeat stale. Set to 6× the
# engine's tick cadence (10s) so a single missed write — common during
# a slow indicator fetch — does not flip the bot to "stopped" in the
# UI. A real silent-exit (cgroup kill, crash without atexit) blows past
# this in 60s and gets reconciled.
HEARTBEAT_STALE_THRESHOLD_SEC = 60


def _heartbeat_is_stale(
    state_dict: dict,
    threshold_sec: int = HEARTBEAT_STALE_THRESHOLD_SEC,
) -> bool:
    """Return True if ``last_heartbeat`` is older than ``threshold_sec``.

    Backwards-compat: state files written by pre-heartbeat builds have
    no ``last_heartbeat`` field. Returns False (NOT stale) so the
    PID-only liveness path keeps governing those bots — flipping all
    legacy state files to "stopped" on first read after upgrade would
    be a self-inflicted incident.
    """
    last_hb = state_dict.get("last_heartbeat")
    if not isinstance(last_hb, str) or not last_hb:
        return False
    try:
        # ``datetime.fromisoformat`` accepts the ISO 8601 form
        # ``2026-04-26T12:47:50.129933+00:00`` produced by the engine.
        ts = datetime.fromisoformat(last_hb)
    except ValueError:
        # Garbled timestamp — refuse to reconcile based on a value we
        # cannot trust. Log and let the PID-only check govern.
        logger.warning(
            "Unparseable last_heartbeat in state file: %r — falling "
            "back to PID-only liveness", last_hb,
        )
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
    return age_sec > threshold_sec


# ── Schema-version mismatch + bounded auto-restart ─────────────────────────────
# (PR: tweak/killmode-process-mismatch-detection)
#
# The systemd unit now uses ``KillMode=process`` so bot subprocesses
# survive a ``systemctl restart reverto`` (cgroup-cleanup no longer
# sweeps them). The trade-off: a freshly-deployed portal may find bots
# still running on the previous engine binary. We detect this via the
# ``state_schema_version`` field stamped by ``paper_engine._write_state``
# and auto-restart any bot whose stamp does not match the portal's
# current view of ``STATE_SCHEMA_VERSION``.
#
# Auto-restart is bounded so a permanently-broken bot (e.g. crashes on
# every start) cannot cause a restart-storm. The budget is in-memory
# only — persisting it would block manual operator recovery after a
# portal-restart.
RESTART_MAX_ATTEMPTS = 3
RESTART_WINDOW_SECONDS = 300  # 5 minutes

# Per-(user_id, slug) attempt timestamps (epoch float). Pruned in-place
# on every ``_attempt_bot_auto_restart`` call so the dict cannot grow
# unbounded across many short-lived bots.
_BOT_RESTART_HISTORY: dict[tuple[int, str], list[float]] = {}

# rha-003: explicit per-bot growth ceiling. The
# ``_attempt_bot_auto_restart`` flow already self-limits via the
# (RESTART_MAX_ATTEMPTS=3) × (RESTART_WINDOW_SECONDS=300) gate, but
# the implicit cap leaves no defence-in-depth if a future caller
# bypasses the prune-then-append pattern. ``_record_bot_restart``
# below is the single mutator and enforces this ceiling regardless.
# 100 entries per bot is generous against the typical prune cycle
# (3 entries clamp inside a 5-minute window) while still bounding
# any pathological caller that forgot to prune.
_BOT_RESTART_HISTORY_MAX_ENTRIES_PER_BOT = 100


def _record_bot_restart(
    user_id: int, slug: str, timestamp: float,
) -> list[float]:
    """Append a restart-timestamp for ``(user_id, slug)`` and enforce
    the per-bot growth ceiling. Returns the post-mutation history list
    so callers can read length / contents without a second dict lookup.

    rha-003 single source-of-truth for ``_BOT_RESTART_HISTORY``
    mutation. Callers are expected to do their own window-pruning
    BEFORE invoking this helper (see ``_attempt_bot_auto_restart``);
    this function only guarantees the absolute size cap, not the
    rolling-window semantics.
    """
    key = (user_id, slug)
    history = _BOT_RESTART_HISTORY.setdefault(key, [])
    history.append(timestamp)
    if len(history) > _BOT_RESTART_HISTORY_MAX_ENTRIES_PER_BOT:
        # In-place truncation of the prefix so the list identity is
        # preserved — any caller holding a reference to the list
        # (e.g. the dict-stored value) sees the truncation without a
        # rebind.
        del history[:-_BOT_RESTART_HISTORY_MAX_ENTRIES_PER_BOT]
    return history


def _bot_needs_restart(state: dict, current_version: int) -> bool:
    """Return True if a running bot should be auto-restarted because
    its on-disk ``state_schema_version`` does not match the portal's.

    Caller (startup-reconcile) is expected to have already routed
    silent-exit cases through ``read_state``'s reconcile path, so this
    helper only fires on bots that *are* alive but on stale code.
    Decoupling the two checks keeps the budget consumed only by real
    mismatch attempts — silent-exit paths never reach the restart
    machinery.

    Cases:
      * state.running is False           → no restart (the bot is
                                            already stopped).
      * state_schema_version is None     → legacy bot from before this
                                            field landed. Treated as a
                                            mismatch so it picks up
                                            current code on next portal
                                            start.
      * state_schema_version != current  → mismatch. Restart.
      * state_schema_version == current  → coherent. No restart.
    """
    if not state.get("running"):
        return False
    on_disk = state.get("state_schema_version")
    if on_disk is None:
        return True
    return on_disk != current_version


def _persist_stopped_reason_field(state_file, reason: str) -> None:
    """Write ``stopped_reason`` (and ``stopped_at``) into state.json
    without touching ``running`` or any other field.

    Used by the auto-restart budget-exceeded path: we want operators
    to see *why* the portal stopped trying to restart the bot, but the
    bot itself may still be alive (older code, but functional). Mutating
    only the reason field keeps the rest of the state intact for the
    next read.

    Best-effort: any IOError is logged and swallowed.
    """
    if state_file is None:
        return
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
        data["stopped_reason"] = reason
        data["stopped_at"] = datetime.now(timezone.utc).isoformat()
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(state_file)
    except OSError as e:
        logger.warning(
            "Failed to persist stopped_reason=%r to %s: %s",
            reason, state_file, e,
        )


async def _attempt_bot_auto_restart(bot) -> bool:
    """Restart ``bot`` if within the per-bot attempt budget.

    Returns True if the restart was issued (regardless of whether
    ``restart_bot`` itself reported success — the caller logs based
    on the returned dict if it cares); False if the budget was
    exhausted or any exception was raised reaching ``restart_bot``.

    The budget is per-(user_id, slug) and lives only in-process.
    Restart-storm protection is therefore scoped to a single portal
    lifetime; a fresh portal starts every bot's budget at zero.
    """
    # Audit PT-v4-EI-006 — auto-restart attempts emit audit events
    # on three signals: attempted, budget-exceeded, and exception.
    # Operators investigating "why did bot X stop accepting events"
    # can now query audit.jsonl for bot_auto_restart_* and see the
    # full restart history without parsing portal.log.
    key = (bot.user_id, bot.slug)
    now = time.time()
    history = _BOT_RESTART_HISTORY.get(key, [])
    # Prune attempts outside the rolling window so a bot that has
    # been stable for a day starts fresh after one new failure.
    history = [t for t in history if now - t < RESTART_WINDOW_SECONDS]

    if len(history) >= RESTART_MAX_ATTEMPTS:
        logger.error(
            "Bot %s/%s exceeded auto-restart budget (%d attempts in %ds) — "
            "leaving stopped. Manual intervention required.",
            bot.user_id, bot.slug,
            RESTART_MAX_ATTEMPTS, RESTART_WINDOW_SECONDS,
        )
        _audit(
            "bot_auto_restart_budget_exceeded",
            bot.slug,
            f"attempts={len(history)}",
            user_id=bot.user_id,
            result="error",
        )
        # Surface the give-up via state.json so the UI (and the next
        # read_state call) can see why the portal stopped trying.
        _persist_stopped_reason_field(bot.state_file, "restart_budget_exceeded")
        return False

    # Write back the window-pruned history first (caller-owned prune
    # semantics), then append via the rha-003 ceiling-enforcing
    # helper. Order matters: setting the dict slot before
    # ``_record_bot_restart`` makes ``setdefault`` see our pruned
    # list and append into it in place — preserving list identity.
    _BOT_RESTART_HISTORY[key] = history
    _record_bot_restart(bot.user_id, bot.slug, now)

    _audit(
        "bot_auto_restart_attempted",
        bot.slug,
        f"attempt={len(history) + 1}",
        user_id=bot.user_id,
    )

    try:
        result = await restart_bot(bot.user_id, bot.slug)
    except Exception as e:
        logger.error(
            "Bot %s/%s auto-restart raised: %s",
            bot.user_id, bot.slug, e,
        )
        _audit(
            "bot_auto_restart_failed",
            bot.slug,
            type(e).__name__,
            user_id=bot.user_id,
            result="error",
        )
        return False
    if not result.get("ok"):
        logger.warning(
            "Bot %s/%s auto-restart returned failure: %s",
            bot.user_id, bot.slug, result.get("error") or result,
        )
        _audit(
            "bot_auto_restart_failed",
            bot.slug,
            str(result.get("error") or "unknown")[:200],
            user_id=bot.user_id,
            result="failure",
        )
        return False
    logger.info(
        "Bot %s/%s auto-restarted (attempt %d/%d in window)",
        bot.user_id, bot.slug, len(history), RESTART_MAX_ATTEMPTS,
    )
    return True


# ── API key auth ──────────────────────────────────────────────────────────────
# Read from REVERTO_API_KEY or auto-generate one. Generated keys NEVER
# get logged — that would leak the credential into portal.log and any
# log shipper. Instead they're written to logs/.api_key_ephemeral with
# mode 0600 and removed at clean exit via atexit. The operator can
# `cat logs/.api_key_ephemeral` to retrieve it once.
_EPHEMERAL_API_KEY_FILE = (
    Path(__file__).parent.parent / "logs" / ".api_key_ephemeral"
)
_API_KEY = os.environ.get("REVERTO_API_KEY")
if not _API_KEY:
    _API_KEY = secrets.token_hex(32)
    try:
        _EPHEMERAL_API_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _EPHEMERAL_API_KEY_FILE.write_text(_API_KEY + "\n", encoding="utf-8")
        os.chmod(_EPHEMERAL_API_KEY_FILE, 0o600)
        import atexit
        atexit.register(
            lambda p=_EPHEMERAL_API_KEY_FILE: p.exists() and p.unlink()
        )
        logger.warning(
            "REVERTO_API_KEY not set — generated ephemeral key, written to %s "
            "(mode 0600). For production set REVERTO_API_KEY=... in your "
            "environment so the key survives restarts.",
            _EPHEMERAL_API_KEY_FILE,
        )
    except OSError as e:
        # Last-resort fallback: can't persist the key file. Audit
        # r1-035: log only a short SHA hint — never the full key
        # itself — so a log-shipping pipeline post-VPS can't leak
        # the auth secret. The operator's recovery path is: set
        # REVERTO_API_KEY=<their choice> in .env, restart; the
        # ephemeral key is inherently short-lived and clients using
        # it would have to re-auth anyway.
        hint = hashlib.sha256(_API_KEY.encode("utf-8")).hexdigest()[:8]
        logger.error(
            "REVERTO_API_KEY not set and could not write %s (%s). "
            "Ephemeral key in use (hint=%s); set REVERTO_API_KEY "
            "in .env and restart to recover a stable key.",
            _EPHEMERAL_API_KEY_FILE, e, hint,
        )


# ── Session auth ──────────────────────────────────────────────────────────────
# Username/password authentication for the portal UI. The session cookie is
# signed (not encrypted) with REVERTO_SECRET_KEY — falls back to an ephemeral
# key with a WARNING so casual local usage still works. Credentials themselves
# live in the ``users`` table (Phase-3a) — password_hash + per-user
# session_epoch. Resolved via core.user_store helpers; no .auth.json in
# the request-flow anymore.

_SECRET_KEY = os.environ.get("REVERTO_SECRET_KEY")
if not _SECRET_KEY:
    _SECRET_KEY = secrets.token_hex(32)
    logger.warning(
        "REVERTO_SECRET_KEY not set — generated ephemeral signing key. "
        "EVERY EXISTING SESSION COOKIE WILL BE INVALIDATED ON THE NEXT "
        "PORTAL RESTART. For production add to ~/.bashrc: "
        "export REVERTO_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    )

_SESSION_COOKIE = "reverto_session"
_SESSION_TTL = 86400  # 24h
_SESSION_SALT = "reverto.session.v1"
_session_serializer = URLSafeTimedSerializer(_SECRET_KEY, salt=_SESSION_SALT)

# Audit r1-073: double-submit cookie CSRF defence. The login
# endpoint mints a random token, sets it as a non-HttpOnly
# cookie (so the SPA's JS can read it), and CSRFMiddleware
# compares it against the ``X-CSRF-Token`` header on every
# mutating request. This adds defence-in-depth alongside the
# existing SameSite=strict cookies; a future subdomain-takeover
# or partial-SameSite-enforcement scenario would need to defeat
# both layers to hijack a session.
_CSRF_COOKIE = "reverto_csrf"
_CSRF_HEADER = "X-CSRF-Token"
_CSRF_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Paths that opt out of CSRF check. Intentionally MINIMAL:
#
#   * /auth/login — caller has no session + no CSRF cookie yet.
#     That endpoint issues the first CSRF cookie on success.
#   * /auth/login/totp (Phase B PR 3) — second step of the 2FA
#     login flow. Same rationale as /auth/login: no session and
#     no CSRF cookie are present yet (the password step staged
#     only the pending-login-TOTP cookie, intentionally not the
#     CSRF cookie). The endpoint is gated by the itsdangerous-
#     signed pending cookie which is HttpOnly + SameSite=Strict —
#     a cross-site form submit could not produce a valid one.
#
# Decisions about what stays NON-exempt (audit pd-042):
#
#   * /auth/logout stays under CSRF. Cross-site forced logout is
#     low-severity (the victim is inconvenienced, no data loss or
#     takeover), but defending it is cheap because the SPA already
#     echoes the header on every mutating fetch. Legacy sessions
#     without a CSRF cookie get one-shot granted + minted by the
#     graceful-migration path in CSRFMiddleware, so users aren't
#     locked out.
_CSRF_EXEMPT_PATHS = frozenset({
    "/auth/login",
    "/auth/login/totp",
})


def _mint_csrf_token() -> str:
    """URL-safe random token used for the double-submit cookie.
    Same shape as the login-path mint so call-sites share the
    entropy budget."""
    return secrets.token_urlsafe(32)


def _set_csrf_cookie_on_response(response, token: str) -> None:
    """Attach a fresh CSRF cookie to ``response``.

    Kept out of the login handler + graceful-migration path so the
    three flags (httponly=False, max_age, secure/samesite) stay in
    one place — drift between the login mint and the migration
    mint would break the double-submit contract.
    """
    response.set_cookie(
        key=_CSRF_COOKIE,
        value=token,
        max_age=_SESSION_TTL,
        # non-HttpOnly so the SPA's JS can read it + echo it back
        # in the X-CSRF-Token header. That's the whole point of
        # double-submit.
        httponly=False,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path="/",
    )

# Secure-cookie flag default. Cookies are marked secure (HTTPS-only) by
# default; this protects production deployments behind a TLS reverse
# proxy. Localhost / plain-http development opts out by exporting
# REVERTO_INSECURE_COOKIES=1 — without that flag set, a browser on
# http:// will silently drop the cookie and the SPA will look broken.
_INSECURE_COOKIES = os.environ.get("REVERTO_INSECURE_COOKIES") == "1"
_COOKIE_SECURE = not _INSECURE_COOKIES
if _COOKIE_SECURE:
    logger.info(
        "Session cookies: secure=True (TLS required). "
        "For local development add `export REVERTO_INSECURE_COOKIES=1` to ~/.bashrc."
    )
else:
    logger.warning(
        "Session cookies: secure=False (REVERTO_INSECURE_COOKIES=1 set "
        "— only safe on localhost / private LAN)."
    )

# SameSite policy. "strict" is the production default — it stops every
# cross-site request from carrying the session cookie, which is the
# strongest CSRF mitigation short of custom headers. Production clients
# always have a real Origin/Referer so the strict policy behaves as
# intended.
#
# Audit v26-22 (known limitation — ACCEPTED, 2026-04-21): the test
# fixture in tests/test_web_routes.py::auth_client flips this to "lax"
# for the duration of each test because httpx/TestClient in CI on
# Python 3.13 + Ubuntu runners dropped the cookie on follow-up requests
# that had no Origin header (DIAG-6 output on commit 88ce0e3). Our
# test-suite therefore validates the "lax" cookie-posture, not the
# production "strict" posture.
#
# Exploratory fix attempted on branch fix/audit-v26-22-testclient-
# samesite: build a SameSiteStrictTestClient wrapper that auto-injects
# ``Origin: http://testserver`` on every request, rolling back the
# ``_COOKIE_SAMESITE = "lax"`` override. Gate 1 (exploratory research)
# landed NO-GO for two concrete reasons:
#
#   1. httpx 0.28.1 does NOT actively enforce SameSite — it delegates
#      cookie storage to Python's stdlib http.cookiejar, which predates
#      the SameSite spec and has no enforcement branch. Confirmed via
#      source read (httpx/_models.py line 11 + 1079-1095) and the
#      upstream discussion at github.com/encode/httpx/discussions/2168
#      ("Cookie and CookieJar are used under the hood; SameSite is
#      stored as a nonstandard attribute, not enforced").
#
#   2. Locally (Python 3.12.3 + WSL2) the exact failing test-flow —
#      login → logout → cookies.clear() → re-login → gated GET —
#      delivers the cookie correctly with SameSite=strict regardless
#      of an Origin header. Standalone repro in /tmp/test_samesite_
#      fullflow.py showed all three variants (no header, matching
#      Origin, cross-site Origin) deliver the cookie.
#
# Consequence: the CI-specific 3.13-Ubuntu behaviour that triggered the
# original fix in commit 5a4d97b could not be reproduced in a Python
# 3.13 environment from this workstation (no 3.13 binary available),
# and the Origin-injection hypothesis has no mechanism in httpx 0.28.1
# to grip onto. Pushing an unvalidated wrapper for CI to judge violates
# the "tests pass at every commit" rule.
#
# Known limitation: auth-tests validate the "lax" cookie-posture only.
# Manual QA in staging / production validates the real "strict"
# behaviour. Revisit this decision when either:
#   - TOTP implementation (Phase B) re-opens the auth-stack for broader
#     rework, or
#   - httpx publishes a TestClient SameSite-aware release, or
#   - we gain Python 3.13 reproducibility on this workstation.
_COOKIE_SAMESITE: str = "strict"

# ── Phase-3a: auth state lives in the users table ─────────────────────────
# Pre-Phase-3a the bootstrap wrote a random admin password to
# logs/.auth.json and printed it to logs/.initial_password. Post-
# Phase-3a, scripts/setup_admin.py is the explicit provisioning path
# — users.password_hash starts NULL after init_db() and login fails
# closed until the operator runs the script.


def _create_session_cookie(user: User) -> str:
    """Sign + emit the session cookie for the given ``User`` instance.

    Audit v26-05: the pre-Phase-3a signature also accepted a
    username string with a fallback that minted uid=-1 if the
    username did not resolve. That fallback was unreachable — the
    login flow always passes an already-resolved User from
    ``verify_password`` — so the branch is gone now. Tests that
    previously called ``_create_session_cookie("admin")`` must
    fetch the admin User themselves via
    ``user_store.get_user_by_username`` (or a test helper around
    it).

    Audit r1-006: pre-fix payload also carried a ``"u": username``
    field left over from Phase-1. Every read-path resolves the
    User from ``uid`` now (via ``user_store.get_user_by_id``), so
    the string-username was dead payload. Dropped; pre-fix cookies
    with both ``u`` and ``uid`` still validate because the reader
    only requires ``uid``.
    """
    return _session_serializer.dumps({
        "uid": user.id,
        "iat": int(time.time()),
        "ep": user_store.get_session_epoch(user.id),
    })


def _verify_session_cookie(token: Optional[str]) -> Optional[dict]:
    """Return the decoded payload when the cookie is valid, else None.

    Validation order:
      1. itsdangerous signature + TTL
      2. payload shape (dict, has 'uid')
      3. per-user session_epoch match against DB

    Any failure returns None — callers translate that to 401.
    """
    if not token:
        return None
    try:
        data = _session_serializer.loads(token, max_age=_SESSION_TTL)
    except (BadSignature, SignatureExpired):
        return None
    except Exception as e:  # noqa: BLE001 — defensive catch-all
        # itsdangerous can raise on malformed base64 / non-JSON payloads
        # too. Treat anything weird as an invalid session — never let a
        # broken cookie escape as a 500 from the auth gate.
        logger.debug("session cookie parse failed: %s", e)
        return None
    # Audit r1-006: validation gate switched from ``data.get("u")``
    # (pre-fix username string) to ``data.get("uid")`` (the int that
    # every downstream read-path actually uses).
    uid = data.get("uid")
    if not isinstance(data, dict) or not isinstance(uid, int) or uid <= 0:
        return None
    # Per-user epoch check — reject every cookie whose embedded epoch
    # doesn't match the current DB value for the user. Logout / pw-
    # change bumps only the caller's row, so other users' sessions
    # survive (unlike the pre-Phase-3a global epoch).
    try:
        cookie_epoch = int(data.get("ep", 0))
        cookie_uid = int(data.get("uid", 0))
    except (TypeError, ValueError):
        return None
    if cookie_uid <= 0:
        return None
    server_epoch = user_store.get_session_epoch(cookie_uid)
    if cookie_epoch != server_epoch:
        return None
    return data


# ── Phase B: pending-TOTP-enrollment cookie ────────────────────────────────
#
# /auth/totp/setup mints a base32 secret and stores it server-signed in a
# short-lived cookie. /auth/totp/verify reads it back, validates the
# user-supplied 6-digit code against the secret, and on success commits
# the encrypted secret to ``users.totp_seed_encrypted``.
#
# Why a cookie and not a DB row:
#   - Pending state per (user, browser tab); a DB column adds a write
#     path that has to clean itself up on abandonment.
#   - itsdangerous signing keeps the secret off the client's
#     localStorage / non-HttpOnly surface — only the SPA's setup-flow
#     ever sees the plaintext (in the response body, on screen for the
#     QR-code render).
#   - 10-minute TTL via URLSafeTimedSerializer so a forgotten tab
#     auto-expires the secret without operator action.
#
# The salt is distinct from the session-cookie salt — a confused-deputy
# attack that swapped one cookie value for the other would fail at the
# itsdangerous signature check (different salt = different MAC).

_PENDING_TOTP_COOKIE = "reverto_totp_pending"
_PENDING_TOTP_TTL = 600  # 10 minutes
_PENDING_TOTP_SALT = "reverto.totp_pending.v1"
_pending_totp_serializer = URLSafeTimedSerializer(
    _SECRET_KEY, salt=_PENDING_TOTP_SALT,
)


def _set_pending_totp_cookie(response, secret: str, user_id: int) -> None:
    """Sign + emit the pending-TOTP cookie. ``secret`` is the freshly-
    generated base32 seed; ``user_id`` is bound into the payload so a
    cookie minted for user A cannot be replayed against user B's
    /auth/totp/verify call.

    Reuses the global cookie-flag posture (Secure, Strict, HttpOnly)
    so a single REVERTO_INSECURE_COOKIES override flips both the
    session and the pending-TOTP cookie together — no drift between
    them under local-dev or production deploys.
    """
    payload = _pending_totp_serializer.dumps({
        "secret": secret,
        "uid": user_id,
        "iat": int(time.time()),
    })
    response.set_cookie(
        key=_PENDING_TOTP_COOKIE,
        value=payload,
        max_age=_PENDING_TOTP_TTL,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path="/",
    )


def _read_pending_totp_cookie(
    request: Request, expected_user_id: int,
) -> Optional[str]:
    """Return the pending base32 secret for ``expected_user_id`` if the
    cookie is present, signed, within TTL, and bound to that user.
    Returns ``None`` for every other case (missing, tampered, expired,
    user-mismatch) so the caller can translate a single None to a
    400/401.

    User-binding is the load-bearing check post-itsdangerous: the
    signature alone proves "we minted this", but a multi-user portal
    must also reject "user A's pending cookie replayed by user B's
    session". The signed payload carries ``uid`` so this is a single
    int-compare after the signature passes.
    """
    raw = request.cookies.get(_PENDING_TOTP_COOKIE)
    if not raw:
        return None
    try:
        data = _pending_totp_serializer.loads(
            raw, max_age=_PENDING_TOTP_TTL,
        )
    except (BadSignature, SignatureExpired):
        return None
    except Exception as e:  # noqa: BLE001 — defensive
        logger.debug("pending-TOTP cookie parse failed: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("uid") != expected_user_id:
        return None
    secret = data.get("secret")
    if not isinstance(secret, str) or not secret:
        return None
    return secret


def _clear_pending_totp_cookie(response) -> None:
    """Remove the pending-TOTP cookie from the client. Called on
    successful verify (commit succeeded — pending state served its
    purpose) and on expired-pending cleanup (don't leave a stale
    signature lingering past the failed flow)."""
    response.delete_cookie(
        key=_PENDING_TOTP_COOKIE,
        path="/",
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        httponly=True,
    )


# ── Phase B PR 3: pending-login-TOTP cookie ───────────────────────────────
#
# Distinct from the enrollment-pending cookie above. /auth/login mints
# this cookie when the password step succeeds AND the user has
# ``totp_enabled = True``; /auth/login/totp reads it back, verifies the
# code against the user's stored seed, and on success swaps the
# pending-state for a real session cookie. The two cookies use
# different salts so a confused-deputy attack that swapped enrollment-
# pending and login-pending values fails at the itsdangerous signature
# check (different salt = different MAC).
#
# 2-minute TTL — the login flow is supposed to be quick, and a longer
# window widens the brute-force surface against the TOTP code without
# any UX benefit.

_PENDING_LOGIN_TOTP_COOKIE = "reverto_login_totp_pending"
_PENDING_LOGIN_TOTP_TTL = 120
_PENDING_LOGIN_TOTP_SALT = "reverto.login_totp_pending.v1"
_pending_login_totp_serializer = URLSafeTimedSerializer(
    _SECRET_KEY, salt=_PENDING_LOGIN_TOTP_SALT,
)


def _set_pending_login_totp_cookie(response, user_id: int) -> None:
    """Stage password-step success. The cookie carries only the
    user_id (and a mint timestamp via itsdangerous's signed envelope
    + max_age) — no username, no password, no TOTP secret. The
    caller owns the password row at the moment we mint, so binding
    just the uid is sufficient: /auth/login/totp re-resolves the
    user from the DB before reaching the TOTP-verify step.
    """
    payload = _pending_login_totp_serializer.dumps({
        "uid": user_id,
        "iat": int(time.time()),
    })
    response.set_cookie(
        key=_PENDING_LOGIN_TOTP_COOKIE,
        value=payload,
        max_age=_PENDING_LOGIN_TOTP_TTL,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path="/",
    )


def _read_pending_login_totp_cookie(request: Request) -> Optional[int]:
    """Return the user_id staged by a prior /auth/login password-step,
    or ``None`` for missing / tampered / expired. The caller (
    /auth/login/totp) translates a single None into 400 and cleans
    up any lingering cookie."""
    raw = request.cookies.get(_PENDING_LOGIN_TOTP_COOKIE)
    if not raw:
        return None
    try:
        data = _pending_login_totp_serializer.loads(
            raw, max_age=_PENDING_LOGIN_TOTP_TTL,
        )
    except (BadSignature, SignatureExpired):
        return None
    except Exception as e:  # noqa: BLE001 — defensive
        logger.debug("pending-login-TOTP cookie parse failed: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    uid = data.get("uid")
    if not isinstance(uid, int) or uid <= 0:
        return None
    return uid


def _clear_pending_login_totp_cookie(response) -> None:
    response.delete_cookie(
        key=_PENDING_LOGIN_TOTP_COOKIE,
        path="/",
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        httponly=True,
    )


def _request_actor(request: Request) -> str:
    """Return a short audit-log identifier for the caller.

    Resolution order:
      * session cookie  → `session:<username>`
      * X-API-Key header → `apikey:<8-char sha256 hint>`
      * neither         → `-` (the AuthMiddleware would normally have
                          rejected the request before we get here, so
                          this branch only fires for the public /auth
                          paths that opt out of the gate).
    Used as a `Depends(_request_actor)` on mutating endpoints in place
    of the old `Depends(_request_actor)` so the audit trail still
    captures who took the action even though the API-key dependency
    itself is gone.
    """
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if payload:
        # Audit r1-006: cookie no longer carries ``u`` (username) —
        # resolve from uid so the audit log still reads as
        # ``session:<username>`` rather than a bare numeric id.
        # Miss (user deleted between request-start and here) falls
        # through to the ``-`` branch so audit lines don't crash.
        uid = payload.get("uid")
        if isinstance(uid, int) and uid > 0:
            u = user_store.get_user_by_id(uid)
            if u is not None:
                return f"session:{u.username}"
    # Header-only — query-string fallback removed, see AuthMiddleware.
    provided = request.headers.get("X-API-Key")
    if provided and secrets.compare_digest(provided, _API_KEY):
        hint = hashlib.sha256(provided.encode("utf-8")).hexdigest()[:8]
        return f"apikey:{hint}"
    return "-"


def _request_user(request: Request) -> User:
    """FastAPI dependency — resolve the request to a User instance.

    Phase-3a: reads ``uid`` from the signed session cookie, looks up
    the row in users, and refuses the request if the user is missing
    or deactivated. Pre-Phase-3a this helper hardcoded the admin user
    because session cookies carried only a username and Phase-1/2
    only ever had one real user; that Phase-1 assumption is now gone.

    API-key callers (``X-API-Key``) are resolved against the admin row
    in the DB (id=1, the setup-flow convention). Audit r1-001 closed
    the prior stub-return hole where a valid key matched a frozen
    DEFAULT_USER without a DB lookup — that bypassed ``active`` (a
    deactivated admin could still authenticate) and, post-multi-user
    seed, would have granted cross-tenant admin to anyone holding the
    shared key. The single indexed lookup per API-key call is cheap;
    fail closed on missing or inactive admin.
    """
    cookie = request.cookies.get(_SESSION_COOKIE)
    payload = _verify_session_cookie(cookie)
    if payload is None:
        # No valid cookie — check for API-key. AuthMiddleware has
        # already short-circuited with 401 for unauth routes, so this
        # branch only runs for explicitly-exempted endpoints and real
        # API-key traffic. Route the key to the actual admin row so
        # ``active`` gates, role-swaps, and session-epoch bumps all
        # apply consistently.
        provided = request.headers.get("X-API-Key")
        if provided and secrets.compare_digest(provided, _API_KEY):
            admin_user = user_store.get_user_by_id(1)
            if admin_user is None or not admin_user.active:
                # Deliberately log state, not the key. Observability
                # for operators chasing a sudden 401 without leaking
                # anything a request-logger would already capture.
                logger.warning(
                    "API-key auth rejected: admin row %s (active=%s)",
                    "missing" if admin_user is None else "present",
                    admin_user.active if admin_user is not None else "n/a",
                )
                raise HTTPException(
                    status_code=401, detail="Not authenticated",
                )
            return admin_user
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = payload.get("uid")
    if not isinstance(user_id, int) or user_id <= 0:
        raise HTTPException(status_code=401, detail="Invalid session")
    user = user_store.get_user_by_id(user_id)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def _ws_extract_user_id(websocket: WebSocket) -> Optional[int]:
    """Resolve the WebSocket caller to a user_id from their session
    cookie. Returns ``None`` for missing / invalid / expired cookies
    so the caller can ``await websocket.close(code=4401)``.

    ``BaseHTTPMiddleware`` does not run on WS upgrades, so WS-
    endpoints can't use ``Depends(_request_user)`` — this helper is
    the equivalent. Phase-3a: reads ``uid`` from the cookie and
    validates the user exists + is active.
    """
    payload = _verify_session_cookie(websocket.cookies.get(_SESSION_COOKIE))
    if payload is None:
        return None
    uid = payload.get("uid")
    if not isinstance(uid, int) or uid <= 0:
        return None
    user = user_store.get_user_by_id(uid)
    if user is None or not user.active:
        return None
    return uid

# Module-level ccxt client — reused across /api/price calls so we don't pay
# instantiation overhead on every request.
_bitget_client = ccxt.bitget({"options": {"defaultType": "swap"}})

# ccxt clients muteren interne state (rate-limit window, request id, cookie jar)
# en zijn niet thread-safe. Serialiseer alle /api/price calls met deze lock zodat
# concurrent worker threads vanuit asyncio.to_thread elkaar niet corrumperen.
_price_lock = asyncio.Lock()

BASE_DIR   = Path(__file__).parent.parent
STATIC_DIR = Path(__file__).parent / "static"
# Legacy aliases — kept because existing callers import them. The
# Phase-2 per-user dirs live under these paths but every new helper
# goes through core.paths so the layout is declared in exactly one
# place.
CONFIG_DIR = BASE_DIR / "config" / "bots"
LOG_DIR    = BASE_DIR / "logs"
PID_DIR    = LOG_DIR / "pids"
PYTHON_BIN = sys.executable

# ── Audit logging ─────────────────────────────────────────────────────────────
# Separate logger "reverto.audit" → logs/audit.log with rotation. Propagate=False
# so audit events don't also land in portal.log. Format:
#     2026-04-15T12:34:56+0000 | bot_start | btc_paper | a1b2c3d4
_audit_logger = logging.getLogger("reverto.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False

# Audit rhav2-001: audit log files must not be world- or group-readable
# by users outside the deployment account. The previous configuration
# inherited the umask of whoever ran the portal (often 0o022, leaving
# files at 0o644 — readable by anyone in the same group, and on a
# multi-tenant host that's a leak channel for usernames + IPs +
# request ids). We narrow umask around every audit-file create and
# explicitly chmod 0o640 after the file exists so the result is
# deterministic regardless of process umask.
_AUDIT_FILE_MODE = 0o640


def _chmod_audit_file_if_exists(path: Path) -> None:
    """Best-effort chmod 0o640 on an audit-log file. Failures are
    logged at DEBUG and swallowed — a chmod that fails on an exotic
    filesystem (Windows-mounted, FAT, container overlay) must not
    break the audit write that just happened."""
    try:
        if path.exists():
            os.chmod(path, _AUDIT_FILE_MODE)
    except OSError as e:
        logger.debug("audit chmod failed for %s: %s", path, e)


if not _audit_logger.handlers:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Narrow umask while the RotatingFileHandler creates audit.log so
    # the file is group-readable but not world-readable from the
    # moment it lands. The handler keeps its own fd open after this,
    # but the explicit chmod below makes the final mode independent
    # of whatever the running process's umask happens to be.
    _prev_umask = os.umask(0o077)
    try:
        _audit_handler = RotatingFileHandler(
            LOG_DIR / "audit.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    finally:
        os.umask(_prev_umask)
    _audit_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    )
    _audit_logger.addHandler(_audit_handler)
    _chmod_audit_file_if_exists(LOG_DIR / "audit.log")


def _extract_client_ip(request: Optional[Request]) -> Optional[str]:
    """Return the client IP for the request, or ``None`` when no
    request is in-scope (e.g. an audit emitted from a background
    task that has no HTTP context).

    Trust model: the reverse proxy is configured to *overwrite*
    ``X-Forwarded-For`` rather than append (Caddy's ``reverse_proxy``
    default; same for nginx's ``proxy_set_header X-Forwarded-For
    $remote_addr;`` recipe). That means the leftmost entry is the
    first proxy-trusted hop and we can use it directly without
    scanning the chain — a client cannot inject a fake leftmost
    value because the proxy strips whatever they sent. Falls back
    to ``request.client.host`` when no XFF header is present (local
    dev or direct exposure).
    """
    if request is None:
        return None
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return get_remote_address(request)


def _audit(
    action: str,
    slug: str = "-",
    key_hint: str = "-",
    user_id: Optional[int] = None,
    *,
    request: Optional[Request] = None,
    result: str = "ok",
) -> None:
    """Append one record to the audit trail.

    Audit r1-031: dual-write. The legacy pipe-delimited line goes
    to ``logs/audit.log`` (RotatingFileHandler, grep-friendly) so
    existing tooling keeps working. A structured JSONL record
    lands in ``logs/audit.jsonl`` (parser-friendly — Loki, Vector,
    DataDog agents can ingest it without regex).

    If ``user_id`` is provided, a *secondary* JSONL record also
    lands in ``logs/<user_id>/audit.jsonl`` so per-tenant audit
    pulls are a single file read instead of a grep over the global
    log. Callers don't have to pass user_id — legacy call sites
    that omit it still produce the global JSONL + pipe lines
    (just no per-user copy).

    The JSON record embeds the current request id (r1-034) so
    an operator correlating an audit event with surrounding
    portal-log lines can filter both streams on the same id.

    Phase-A wrap-up: ``request`` and ``result`` are optional
    keyword-only fields. ``request`` is used to extract the client
    IP (per-r1-004 trust model — see ``_extract_client_ip``) and
    ``result`` records whether the action succeeded ("ok") or was
    refused ("denied", "error", etc.) so the audit trail captures
    failed attempts as well as successes. Both default to absent /
    "ok" so existing callers keep their previous behaviour.
    """
    # Pipe-format (legacy, grep-friendly). Format unchanged so the
    # existing grep recipes operators rely on keep matching.
    _audit_logger.info("%s | %s | %s", action, slug, key_hint)

    # JSONL — dual-write. Failures stay local to the audit path;
    # an unwritable logs/ dir must never break the mutating
    # endpoint that invoked us.
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts": ts,
        "action": action,
        "slug": slug,
        "user": key_hint,
        "user_id": user_id,
        "ip": _extract_client_ip(request),
        "result": result,
        "request_id": _request_id_ctx.get(),
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    # Audit rhav2-001: narrow umask while the JSONL files are
    # opened-for-append so a fresh-create lands at 0o640. The
    # follow-up chmod is the authoritative fix (umask only governs
    # the create-time mode, not pre-existing files), but the
    # umask-narrow closes a brief window where another process on
    # the same host could open the file while it's still 0o644.
    global_path = LOG_DIR / "audit.jsonl"
    _prev_umask = os.umask(0o077)
    try:
        try:
            with open(global_path, "a", encoding="utf-8") as f:
                f.write(line)
            _chmod_audit_file_if_exists(global_path)
        except OSError as e:
            logger.debug("audit.jsonl write failed: %s", e)

        # Per-user split — only when an explicit user_id was passed
        # by the caller. Deriving user_id from ``key_hint`` string
        # parsing would be fragile (session:<username> vs apikey:
        # <hint> vs "-"), so we keep it opt-in at the call-site.
        if user_id is not None:
            try:
                user_dir = paths.user_logs_dir(user_id)
                user_path = user_dir / "audit.jsonl"
                with open(user_path, "a", encoding="utf-8") as f:
                    f.write(line)
                _chmod_audit_file_if_exists(user_path)
            except OSError as e:
                logger.debug(
                    "per-user audit.jsonl write failed for user=%d: %s",
                    user_id, e,
                )
    finally:
        os.umask(_prev_umask)


def _log_to_bot_log(user_id: int, slug: str, line: str) -> None:
    """Append a timestamped ``[ADMIN]`` line to a specific bot's log.

    Admin cross-user lifecycle actions surface here so a bot's owner
    sees what happened on their bot by tailing the normal log instead
    of having to cross-reference audit.log. The central audit trail
    still gets its entry via ``_audit()``; this helper is additive.

    Swallows OSError (disk full, permission denied) with a warning
    because an admin action must not fail just because writing the
    courtesy line to the owner's log didn't work.
    """
    bot_log = paths.user_logs_dir(user_id) / f"{slug}.log"
    try:
        # Local time (no tz arg) so ADMIN lines interleave cleanly with
        # the surrounding log output, which comes from the subprocess's
        # logging.basicConfig asctime — that formatter renders local
        # time too. UTC here would mean a 1-2h offset from the
        # adjacent engine log lines and make correlation painful.
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(bot_log, "a", encoding="utf-8") as f:
            f.write(f"{ts} [ADMIN] {line}\n")
    except OSError as e:
        logger.warning(
            "Could not write admin-action line to %s/%s log: %s",
            user_id, slug, e,
        )


# pd-007 design note: the project deliberately uses two slug regexes
# with non-identical character classes. They cover different stages
# of the slug lifecycle and harmonising them would be a regression:
#
#   * ``_SLUG_RE`` is a SANITISATION mask for free-form wizard input.
#     ``slugify()`` first lowercases the input + replaces spaces with
#     underscores, THEN strips anything outside ``[a-z0-9_]``. The
#     narrow charset matches the post-lowercase result; widening it
#     would change the on-disk filesystem layout for any slug a user
#     enters with mixed case.
#   * ``_BOT_SLUG_RE`` is a VALIDATION mask for slugs that arrive on
#     URL paths (path-parameter handlers across web/routes/*). It
#     accepts the wider ``[A-Za-z0-9_-]`` superset because the slug
#     it sees was generated elsewhere — by ``slugify()`` (narrow), by
#     a YAML-import flow (legacy hyphenated slugs), or directly on
#     disk (operator-edited config). The validator is purely
#     "is this URL-safe and not a path-traversal attempt".
#
# The asymmetry is design-intent, not drift. Pinned by
# ``tests/test_slug_regex_harmonization.py``.
_SLUG_RE = re.compile(r"[^a-z0-9_]+")
# Re-exported from core.ids so the engine, the web routes, and the
# route-level validators all agree on one canonical shape for
# YYYYMMDDHHMM-RRRR deal ids. Kept as the underscore-prefixed alias
# so existing imports from web/routes/deals.py keep working.
_DEAL_ID_RE = DEAL_ID_RE

# Validator for slugs that come straight off the URL — the slugify()
# helper above cleans wizard input, but path-parameter slugs must be
# checked before they hit Path() construction to block `../` escapes.
# Wider charset than _SLUG_RE on purpose — see the design note above.
_BOT_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def slugify(name: str) -> str:
    """Convert a free-form bot name into a safe filename stem.

    Lowercase, spaces → underscore, everything outside [a-z0-9_]
    stripped, multiple underscores collapsed. Empty results raise
    ValueError so the caller can return a 400.
    """
    cleaned = name.strip().lower().replace(" ", "_")
    cleaned = _SLUG_RE.sub("", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        raise ValueError(f"name {name!r} produces empty slug")
    return cleaned


# ── Bot registry ──────────────────────────────────────────────────────────────

class BotInfo:
    """Registry record for a single bot — scoped to (user_id, slug).

    Phase-2 composite key: two users can own a bot with the same slug
    without collision on the pid/state/log/trigger files, because every
    path flows through ``core.paths`` which partitions by user_id.
    """

    def __init__(self, user_id: int, slug: str, config_file: str):
        self.user_id     = int(user_id)
        self.slug        = slug
        self.config_file = config_file

    @property
    def pid_file(self)   -> Path:
        return paths.bot_pid_path(self.user_id, self.slug)
    @property
    def log_file(self)   -> Path:
        return paths.bot_log_path(self.user_id, self.slug)
    @property
    def state_file(self) -> Path:
        return paths.bot_state_path(self.user_id, self.slug)
    @property
    def manual_trigger_file(self) -> Path:
        return paths.bot_manual_trigger_path(self.user_id, self.slug)

    @property
    def running(self) -> bool:
        if not self.pid_file.exists():
            return False
        try:
            pid = int(self.pid_file.read_text().strip())
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    @property
    def pid(self) -> Optional[int]:
        try:
            return int(self.pid_file.read_text().strip())
        except Exception:
            return None

    def _resolve_yaml_mode(self) -> str:
        """Read the authoritative mode straight from the bot YAML.

        The state-file mode is lagging — it only exists after the engine's
        first tick writes it, and the default-state fallback hardcodes
        ``"paper"``. A live-mode bot that has never started therefore
        surfaces as mode=paper in the /api/bots response, which flips
        the UI (mode label, Start/Start-dry-run button, DRY RUN badge)
        to the wrong path. The YAML is what main_paper.py / main_live.py
        will actually load, so it is the single source of truth.

        Returns "" on any failure so callers fall back to whatever the
        state file says. One yaml.safe_load per bot per listing call is
        cheap (config fits in <4 KB) and the results are not cached
        because a YAML edit via PUT /api/bots/{slug}/config must be
        visible on the next GET without touching the 5 s registry TTL.
        """
        try:
            cfg_path = paths.bot_yaml_path(self.user_id, self.slug)
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return ""
        inner = raw.get("bot", raw) if isinstance(raw, dict) else None
        if not isinstance(inner, dict):
            return ""
        mode = inner.get("mode")
        return mode.lower() if isinstance(mode, str) else ""

    def _persist_silent_exit_reconcile(self, raw_state: dict, reason: str) -> None:
        """Write back the corrected lifecycle fields after a silent-exit.

        Called from ``read_state`` when on-disk ``running=true`` but the
        process is gone (or its heartbeat is stale). Mutates a copy of
        ``raw_state`` to mark the bot as stopped and writes back via
        the same atomic tmp+rename pattern the engine itself uses.

        Idempotent: if a future call hits the same path the on-disk
        ``running`` is already False, so the caller's gate
        (``raw.get("running") is True``) skips this method entirely.

        Best-effort: any IOError is logged and swallowed. The validated
        dict returned to the caller still reflects the corrected state
        in-memory, so the API response is correct even if the disk
        write transiently fails (it just means the next read will
        repeat the reconcile until the disk write succeeds).

        rha-014 — semantic counterpart of ``StateIO.mark_stopped()``
        in ``paper/state_io.py``. Both write ``running=False`` +
        ``current_price=0.0`` atomically, but they are deliberately
        separate. ``mark_stopped`` runs in the **engine** on graceful
        shutdown and leaves ``stopped_at``/``stopped_reason`` as
        ``None``. This method runs in the **portal** post-mortem
        when the engine never got to mark itself stopped (cgroup
        SIGKILL / OOM / hung-heartbeat) and stamps both fields so
        operators can grep audit logs for the failure mode.
        Consolidating them would either lose the ``stopped_reason``
        signal or falsely stamp graceful shutdowns with one. See
        ``StateIO.mark_stopped`` docstring for the full rationale.
        """
        if self.state_file is None:
            return
        try:
            stopped_at = datetime.now(timezone.utc).isoformat()
            patched = dict(raw_state)
            patched["running"] = False
            patched["current_price"] = 0.0
            patched["stopped_at"] = stopped_at
            patched["stopped_reason"] = reason
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(patched, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
            # Mirror the field on raw_state so the caller's
            # ``raw.get("stopped_at")`` lookup returns the value we
            # just persisted.
            raw_state["stopped_at"] = stopped_at
            raw_state["stopped_reason"] = reason
            logger.warning(
                "Silent-exit reconcile: bot %s (user %d) marked stopped "
                "(reason=%s) — state.json corrected on disk",
                self.slug, self.user_id, reason,
            )
        except OSError as e:
            logger.warning(
                "Silent-exit reconcile failed to persist for %s: %s",
                self.slug, e,
            )

    def read_state(self) -> dict:
        yaml_mode = self._resolve_yaml_mode()
        try:
            # Bounded read — read at most _MAX_STATE_FILE_SIZE + 1 bytes in
            # a single open()+read() so there is no TOCTOU gap between a
            # separate stat() and read_text(). If one extra byte comes in
            # the file is larger than allowed and we fall back to default.
            try:
                with open(self.state_file, "rb") as fh:
                    raw_bytes = fh.read(_MAX_STATE_FILE_SIZE + 1)
            except FileNotFoundError:
                return self._default_state(yaml_mode=yaml_mode)
            except MemoryError:
                logger.warning(
                    "State file %s triggered MemoryError, using defaults",
                    self.state_file,
                )
                return self._default_state(yaml_mode=yaml_mode)

            if len(raw_bytes) > _MAX_STATE_FILE_SIZE:
                logger.warning(
                    "State file %s exceeds %d bytes, using defaults",
                    self.state_file, _MAX_STATE_FILE_SIZE,
                )
                return self._default_state(yaml_mode=yaml_mode)

            raw = json.loads(raw_bytes.decode("utf-8"))
            validated = BotStateModel.model_validate(raw).model_dump()
            # Lifecycle-stability: silent-exit reconciliation. Truth =
            # PID liveness AND fresh heartbeat; on-disk ``running``
            # may be stale if the bot died without running its atexit
            # hook (cgroup SIGKILL, OOM, etc.) or if the engine is
            # hung past HEARTBEAT_STALE_THRESHOLD_SEC. Reconcile the
            # state file so the next read converges without re-running
            # this branch — idempotent because the second read sees
            # ``running=False`` on disk and skips reconciliation.
            on_disk_running = raw.get("running") is True
            pid_alive = self.running
            heartbeat_stale = _heartbeat_is_stale(raw)
            if on_disk_running and (not pid_alive or heartbeat_stale):
                reason = "silent_exit" if not pid_alive else "heartbeat_stale"
                self._persist_silent_exit_reconcile(raw, reason)
                validated["running"] = False
                validated["stopped_reason"] = reason
                # ``stopped_at`` mirrors what _persist_silent_exit_reconcile
                # wrote so the API response matches what's now on disk.
                validated["stopped_at"] = raw.get("stopped_at")
            else:
                validated["running"] = pid_alive
            validated["slug"]        = self.slug
            validated["config_file"] = self.config_file
            # Audit r1-042: stamp the owning user so downstream
            # aggregators (WS fan-out, cross-tenant summaries) have
            # the identity inline rather than re-deriving it from
            # registry state. Canonical keys ``bot_user_id`` +
            # ``bot_slug`` avoid collision with any state-file field
            # the engines might introduce later.
            validated["bot_user_id"] = self.user_id
            validated["bot_slug"]    = self.slug
            # YAML wins over state-file mode so that an operator-edited
            # YAML (paper→live or vice versa) surfaces immediately in
            # the UI instead of lagging behind the next tick's state
            # write.
            if yaml_mode:
                validated["mode"] = yaml_mode
            return validated
        except ValidationError as e:
            logger.warning("State validation failed for %s: %s", self.slug, e)
        except Exception as e:
            logger.warning("State read failed for %s: %s", self.slug, type(e).__name__)

        return self._default_state(yaml_mode=yaml_mode)

    def _default_state(self, yaml_mode: str = "") -> dict:
        return {
            "slug":                self.slug,
            "config_file":         self.config_file,
            # Audit r1-042: stamp identity in the default-state path
            # too so consumers see the same shape regardless of
            # whether the state-file exists.
            "bot_user_id":         self.user_id,
            "bot_slug":            self.slug,
            "bot_name":            self.slug,
            "mode":                yaml_mode or "paper",
            "exchange":            "—",
            "pair":                "BTC/USD",
            "running":             self.running,
            "current_price":       0.0,
            "schedule_open":       False,
            "has_trading_windows": False,
            "balance_btc":         0.0,
            "initial_balance_btc": 0.0,
            "total_pnl_btc":       0.0,
            "win_rate":            0.0,
            "open_deals_count":    0,
            "closed_deals_count":  0,
            "open_deals":          [],
            "closed_deals":        [],
            "indicators":          {},
            "started_at":          None,
            "updated_at":          None,
        }


# ── Fail-closed cache voor _scan_user_dirs (audit v25 Finding #1) ────────────
# Vóór de fix viel _scan_user_dirs bij een get_active_user_ids()-failure
# back to integer-name-only matching — fail-open. In a multi-user
# Phase-3 environment a single transient DB-glitch could then silently
# accept an orphan dir as a valid tenant. We now hold on to the last
# known-good users-set and reuse it until ``_MAX_STALE_REFRESHES``
# consecutive failures; after that the scan returns empty (fail-closed)
# with an ERROR in the log.
#
# _previously_logged_orphans also prevents the Finding #7 log-spam:
# an orphan dir that would otherwise be re-logged on every 5-second
# scan comes through here only once until it reappears.
_cached_active_users: set[int] | None = None
_db_failure_count: int = 0
_MAX_STALE_REFRESHES: int = 5  # ≈ 25 s at the 5 s registry-refresh TTL
_previously_logged_orphans: set[Path] = set()
# TTL for the DB-cache in _scan_user_dirs (audit v25 Finding #6). Without
# this short-circuit, every 5 s scan would issue a fresh
# get_active_user_ids() call while the users table changes rarely in
# steady state. 30 s sits well within the refresh cadence and saves
# ~6 DB reads per minute per portal. A user-create/-delete endpoint
# can set ``_cache_last_refresh_ts = 0`` to invalidate explicitly
# (Phase-3 work; pure-TTL for now).
_CACHE_TTL_S: float = 30.0
_cache_last_refresh_ts: float = 0.0


def _reset_user_dirs_cache() -> None:
    """Reset the module-level fail-closed state. For test use only —
    production code does not touch these globals directly.
    """
    global _cached_active_users, _db_failure_count
    global _previously_logged_orphans, _cache_last_refresh_ts
    _cached_active_users = None
    _db_failure_count = 0
    _previously_logged_orphans = set()
    _cache_last_refresh_ts = 0.0


def _scan_active_dirs(active: set[int]) -> list[tuple[int, Path]]:
    """Match ``config/bots/<int>/`` directories against a trusted
    active-set. Shared between the cache-hit and cache-miss paths of
    ``BotRegistry._scan_user_dirs`` — orphan-log dedup is therefore
    independent of whether the DB was queried this tick.

    ``active`` is always pre-validated by the caller (live DB or
    last-known-good cache); this helper makes no DB call.
    """
    global _previously_logged_orphans

    out: list[tuple[int, Path]] = []
    if not CONFIG_DIR.exists():
        _previously_logged_orphans = set()
        return out

    current_orphans: set[Path] = set()
    for child in sorted(CONFIG_DIR.iterdir()):
        if not child.is_dir():
            continue
        try:
            uid = int(child.name)
        except ValueError:
            continue
        if uid in active:
            out.append((uid, child))
            continue
        current_orphans.add(child)
        if child not in _previously_logged_orphans:
            logger.warning(
                "orphan user dir %s (no matching active user in DB), skipped",
                str(child),
            )
    # Dedup baseline for the next scan. Orphans that are gone
    # (operator cleaned up) drop out of the set, and if they ever
    # come back they get logged again.
    _previously_logged_orphans = current_orphans
    return out


class BotRegistry:
    """In-memory index of every bot YAML keyed on ``(user_id, slug)``.

    Phase 2 introduces the composite key so two users can own a bot
    with the same slug name without colliding. The scan walks
    ``config/bots/<user_id>/*.yaml`` for every integer-named
    subdirectory under ``config/bots/``; Phase 1's single-level layout
    is no longer supported (the migration script moves it under 1/).
    """

    # TTL for the filesystem glob in refresh(). At high API
    # frequency (dashboard polls every 5s, plus /api/price, plus
    # tail_logs) every call ran its own glob — redundant and
    # expensive on slow filesystems (NFS/SMB). 5s sits well within
    # the UI refresh cadence.
    _REFRESH_TTL = 5.0

    def __init__(self):
        self._bots: dict[tuple[int, str], BotInfo] = {}
        self._lock = asyncio.Lock()
        self._last_refresh: float = 0.0
        # In-progress starts, keyed on (user_id, slug) so a double-click
        # for user 1/rsi_test and user 2/rsi_test don't block each other.
        self._starting: set[tuple[int, str]] = set()
        # Initial population: happens before the event loop exists,
        # so no lock-contention is possible — fill synchronously.
        self._refresh_locked()
        self._last_refresh = time.time()

    def _scan_user_dirs(self) -> list[tuple[int, Path]]:
        """Return a list of ``(user_id, user_dir)`` for every integer-
        named subdirectory under CONFIG_DIR whose id matches an active
        row in the users table.

        Non-integer names (backup folders, operator-placed directories)
        are skipped silently. Integer-named dirs that don't match any
        active user (operator typo, stale state, deactivated tenant)
        are skipped with a WARNING — but only once per drift event,
        not per 5 s scan (see ``_previously_logged_orphans``).

        Fail-closed at DB-failure (audit v25 Finding #1): if
        ``get_active_user_ids()`` raises, we reuse the last
        known-good set for up to ``_MAX_STALE_REFRESHES`` cycles;
        after that we return an empty list and log ERROR. A single
        transient glitch therefore never surfaces an orphan as a
        tenant.

        Happy-path cache (audit v25 Finding #6): within ``_CACHE_TTL_S``
        of a successful DB-call we reuse ``_cached_active_users``
        without touching the DB at all. Skips ~6 queries/minute per
        portal when the users-table is steady.
        """
        global _cached_active_users, _db_failure_count
        global _cache_last_refresh_ts

        now = time.time()
        # Cache-hit path: cache fresh enough → skip the DB call and
        # go straight to the directory scan. The DB-failure counter
        # is left alone; a subsequent cache miss (after TTL expiry)
        # revives the happy/failure split normally.
        if (
            _cached_active_users is not None
            and now - _cache_last_refresh_ts < _CACHE_TTL_S
        ):
            return _scan_active_dirs(_cached_active_users)

        # Cross-check against the users table. Import inside the
        # function so circular imports don't occur if core.user
        # ever wants to look at web.app at init time. The DB query
        # comes before the CONFIG_DIR.exists() short-circuit so the
        # cache invariant (refreshed on every scan) is independent
        # of whether bot YAMLs exist on disk yet — a fresh install
        # without YAMLs still populates the cache so later failures
        # don't go fail-closed immediately.
        try:
            from core.user import get_active_user_ids
            active: set[int] = get_active_user_ids()
            _cached_active_users = active
            _db_failure_count = 0
            _cache_last_refresh_ts = now
        except Exception as e:
            _db_failure_count += 1
            if (
                _cached_active_users is None
                or _db_failure_count > _MAX_STALE_REFRESHES
            ):
                logger.error(
                    "_scan_user_dirs DB-failure: %s "
                    "(failure count=%d, cache=%s). Returning empty "
                    "registry (fail-closed). Reason: %s",
                    (
                        "no prior cache"
                        if _cached_active_users is None
                        else f">{_MAX_STALE_REFRESHES} stale cycles"
                    ),
                    _db_failure_count,
                    (
                        "empty"
                        if _cached_active_users is None
                        else f"{len(_cached_active_users)} users"
                    ),
                    e,
                )
                # Clear orphan dedup — als de registry leeg is heeft
                # een volgende scan (bv. na DB-recovery) een schone
                # lei nodig om orphans opnieuw te signaleren.
                globals()["_previously_logged_orphans"] = set()
                return []
            logger.warning(
                "_scan_user_dirs DB-failure (%d/%d stale cycles, "
                "using cached users-set). Reason: %s",
                _db_failure_count, _MAX_STALE_REFRESHES, e,
            )
            active = _cached_active_users

        return _scan_active_dirs(active)

    def _refresh_locked(self) -> None:
        """Run the glob; caller must hold the lock (or be init)."""
        current: set[tuple[int, str]] = set()
        for uid, user_dir in self._scan_user_dirs():
            for f in sorted(user_dir.glob("*.yaml")):
                slug = f.stem
                key = (uid, slug)
                current.add(key)
                if key not in self._bots:
                    self._bots[key] = BotInfo(
                        user_id=uid,
                        slug=slug,
                        config_file=str(f.relative_to(BASE_DIR)),
                    )
        for stale in [k for k in self._bots if k not in current]:
            del self._bots[stale]

    async def refresh(self) -> None:
        async with self._lock:
            if time.time() - self._last_refresh <= self._REFRESH_TTL:
                return
            self._refresh_locked()
            self._last_refresh = time.time()

    async def all(self, user_id: Optional[int] = None) -> list[BotInfo]:
        """All bots across every user, or filtered to one user when
        ``user_id`` is given. Phase-1 callers (no user context) still
        work — they get the flat list."""
        await self.refresh()
        async with self._lock:
            if user_id is None:
                return list(self._bots.values())
            return [b for (uid, _), b in self._bots.items() if uid == user_id]

    async def get(self, user_id: int, slug: str) -> Optional[BotInfo]:
        """Lookup by composite key. Returns None when the pair doesn't
        exist in the registry."""
        await self.refresh()
        async with self._lock:
            return self._bots.get((int(user_id), slug))

    async def invalidate(self) -> None:
        """Forceer een refresh bij de volgende all()/get() call.
        Aanroepen na YAML create/delete in de bot management endpoints."""
        async with self._lock:
            self._last_refresh = 0.0

    async def begin_start(self, user_id: int, slug: str) -> bool:
        """Claim de start-slot voor (user_id, slug). Retourneert True
        als we de slot kregen, False als er al een start in progress is."""
        key = (int(user_id), slug)
        async with self._lock:
            if key in self._starting:
                return False
            self._starting.add(key)
            return True

    async def end_start(self, user_id: int, slug: str) -> None:
        """Release de start-slot. Idempotent."""
        async with self._lock:
            self._starting.discard((int(user_id), slug))


registry = BotRegistry()


# ── Process control ───────────────────────────────────────────────────────────

# Allowlist of env-vars safe to forward into bot subprocesses. See
# ``_bot_subprocess_env`` for the full rationale; the set is kept
# module-level so tests can reference it without duplicating the
# membership list.
_BOT_ENV_ALLOWLIST = frozenset({
    # System / locale
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ",
    # Python runtime
    "PYTHONPATH", "PYTHONUNBUFFERED",
    # Reverto process-level config (not secrets)
    "REVERTO_LOG_LEVEL",
})


def _bot_subprocess_env(user_id: int) -> dict[str, str]:
    """Build an explicit env dict for bot subprocesses (audit r1-023).

    Previously ``start_bot`` / ``start_bot_dry_run`` passed
    ``os.environ.copy()`` to ``subprocess.Popen``, which handed every
    bot every env-var the portal process happened to hold —
    ``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``,
    ``BITGET_PASSPHRASE``, ``REVERTO_API_KEY``, plus anything else in
    the operator's .env. Post-multi-user seed that's a cross-tenant
    credential leak: user A's bot inherits user B's tokens verbatim.

    This helper restricts the subprocess env to:

      1. A small allowlist of process-level config (PATH, locale,
         Python runtime flags, ``REVERTO_LOG_LEVEL``) — see
         ``_BOT_ENV_ALLOWLIST``.
      2. Per-user scoping via ``REVERTO_BOT_USER_ID`` so the child
         knows which tenant it runs for (not consumed today; kept as
         a zero-cost breadcrumb for observability + future per-user
         configuration).
      3. ``PYTHONUNBUFFERED=1`` so state-log lines land on disk
         without the portal's extra buffering.

    Everything else — secrets included — is intentionally withheld.
    Per-user credentials land in the subprocess via
    ``core.credentials`` (encrypted, user-scoped) at load time, not
    via env. If a future bot-code path needs a new non-secret env
    var, add it to ``_BOT_ENV_ALLOWLIST`` after confirming it holds
    no cross-tenant material.

    Related: r1-012 moves ``BITGET_PASSPHRASE`` into the per-user
    credentials payload, closing the last env-secret gap for live
    mode.
    """
    env = {k: os.environ[k] for k in _BOT_ENV_ALLOWLIST if k in os.environ}
    # Forced defaults — unbuffered stdio is mandatory for the log
    # tailer to see bot output in near-real time, regardless of what
    # the portal's own env looks like.
    env["PYTHONUNBUFFERED"] = "1"
    # Per-user breadcrumb. Not consumed by main_paper / main_live
    # today (they take --user-id on the CLI) but a child that wants
    # to self-identify without re-parsing argv can consult this.
    env["REVERTO_BOT_USER_ID"] = str(user_id)
    # Drop empty values — subprocess envs with "" entries are
    # technically valid but noisy in `env | sort` when the operator
    # debugs a stuck bot.
    return {k: v for k, v in env.items() if v != ""}


async def start_bot(user_id: int, slug: str) -> dict:
    bot = await registry.get(user_id, slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}
    if bot.running:
        return {"ok": False, "error": f"{slug} already running (PID {bot.pid})"}

    # Claim the start slot. Prevents a double-click (both calls see
    # bot.running=False because main_paper.py has not started yet)
    # from spawning two subprocesses.
    if not await registry.begin_start(user_id, slug):
        return {"ok": False, "error": "Bot is already starting"}

    try:
        paths.user_pid_dir(user_id)

        # Use absolute path to main_paper.py and same venv Python as portal.
        # Env is built from an explicit allowlist (r1-023) — every
        # entry must either be process-level config the child genuinely
        # needs or the per-user scoping breadcrumb. Secrets are
        # intentionally withheld; the bot loads them via
        # core.credentials at runtime.
        env = _bot_subprocess_env(user_id)
        env["PYTHONPATH"] = str(BASE_DIR)

        # Context manager closes the parent's FD after Popen duplicates it —
        # the child process keeps its own handle, no FD leak in the portal.
        # ``start_new_session=True`` is the modern Python equivalent of
        # ``preexec_fn=os.setsid`` — it calls ``setsid()`` in the child
        # before exec, putting the bot in its own process-group (PGID =
        # bot PID). That breaks the systemd cgroup-cleanup chain on a
        # portal-restart so the bot subprocess is not auto-killed when
        # the portal unit stops (KillMode=mixed only signals the main
        # process; cgroup-side SIGKILL still arrives but the bot's own
        # PGID lets us ratchet detection + state-correction via the
        # heartbeat path below).
        with open(bot.log_file, "a") as log_out:
            proc = subprocess.Popen(
                [PYTHON_BIN, str(BASE_DIR / "main_paper.py"),
                 "--bot", slug, "--user-id", str(user_id)],
                cwd=str(BASE_DIR),
                stdout=log_out,
                stderr=log_out,
                env=env,
                start_new_session=True,  # ≡ preexec_fn=os.setsid
            )
        logger.info(f"Bot {slug} started (PID {proc.pid})")

        # Wait up to 3s until main_paper.py has written its own PID
        # file. As long as we don't see it the starting-slot stays
        # claimed so a follow-up click cleanly gets "already starting"
        # instead of a second subprocess.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if bot.pid_file.exists():
                break
            await asyncio.sleep(0.1)

        return {"ok": True, "message": f"{slug} started (PID {proc.pid})"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await registry.end_start(user_id, slug)


def _pid_alive(pid: int) -> bool:
    """Return True if `pid` is still a live process. Uses signal 0 probe."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — treat as alive.
        return True


async def stop_bot(user_id: int, slug: str) -> dict:
    bot = await registry.get(user_id, slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}
    if not bot.running:
        # Clean up stale PID file if the process is gone but file remains.
        if bot.pid_file.exists():
            try:
                bot.pid_file.unlink()
                logger.info(f"Bot {slug}: removed stale PID file")
            except OSError:
                pass
        return {"ok": False, "error": f"{slug} is not running"}

    pid = bot.pid
    try:
        os.kill(pid, signal.SIGTERM)
        logger.info(f"Bot {slug}: sent SIGTERM to PID {pid}")
    except ProcessLookupError:
        if bot.pid_file.exists():
            try:
                bot.pid_file.unlink()
            except OSError:
                pass
        return {"ok": False, "error": "Process not found — already stopped?"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Wait for graceful exit (poll PID file + liveness). The deadline
    # is derived from the engine's notify-drain budget so the two are
    # always consistent: if NOTIFY_DRAIN_TIMEOUT_S grows, the portal's
    # patience grows with it. Portal-wait must be STRICTLY greater than
    # the drain budget, else we SIGKILL while the engine is still
    # waiting for its notify-worker to flush (single HTTP POST per
    # Telegram message, 10s httpx timeout each). A previous fix
    # bumped the portal-wait from 5s to 10s but forgot the rekensom —
    # 10 < 15 still meant 100% SIGKILL. _STOP_SAFETY_MARGIN_S adds
    # slack for process-teardown overhead between engine.stop()
    # returning and the PID actually leaving the procestable.
    stop_timeout = NOTIFY_DRAIN_TIMEOUT_S + _STOP_SAFETY_MARGIN_S
    deadline = time.time() + stop_timeout
    while time.time() < deadline:
        if not _pid_alive(pid) or not bot.pid_file.exists():
            break
        await asyncio.sleep(0.1)

    if _pid_alive(pid):
        # Graceful shutdown timed out — escalate to SIGKILL.
        logger.warning(
            f"Bot {slug}: PID {pid} still alive after "
            f"{stop_timeout:.0f}s — escalating to SIGKILL"
        )
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Brief wait for the kernel to reap the process.
        for _ in range(20):
            if not _pid_alive(pid):
                break
            await asyncio.sleep(0.1)

    # Remove any lingering PID file so the next start doesn't see stale state.
    if bot.pid_file.exists():
        try:
            bot.pid_file.unlink()
            logger.info(f"Bot {slug}: cleaned up PID file after stop")
        except OSError:
            pass

    logger.info(f"Bot {slug} stopped (PID {pid})")
    return {"ok": True, "message": f"{slug} stopped (PID {pid})"}


async def start_bot_dry_run(user_id: int, slug: str) -> dict:
    """Spawn a LIVE-mode bot in dry-run via main_live.py.

    Phase 1 counterpart to start_bot(): uses main_live.py --bot <slug>
    --dry-run with DRY_RUN=1 so the confirmation prompt is skipped.
    The resulting subprocess writes the SAME PID/state/log files as
    the paper runner, so stop_bot/restart_bot work unchanged.
    """
    # Defense-in-depth: the route handler validates slug via
    # _BOT_SLUG_RE and main_live.py's own regex re-validates. Belt-
    # and-braces here so any non-route caller (tests, scripts) still
    # gets a safe early-exit instead of reaching subprocess.Popen.
    if not _BOT_SLUG_RE.match(slug):
        return {"ok": False, "error": f"Invalid bot slug: {slug!r}"}
    bot = await registry.get(user_id, slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}
    if bot.running:
        return {"ok": False, "error": f"{slug} already running (PID {bot.pid})"}

    # Only live-mode bots may be launched dry-run. A paper bot would
    # fail the hard mode check inside main_live.py, but bouncing it at
    # the portal is friendlier than letting the subprocess exit 1 and
    # surface as a silent no-op.
    try:
        cfg = load_bot_config(bot.config_file)
    except Exception as e:
        return {"ok": False, "error": f"Could not load config: {e}"}
    if cfg.mode != Mode.LIVE:
        return {
            "ok": False,
            "error": (
                f"{slug} is mode={cfg.mode.value}; dry-run is only for live-mode bots"
            ),
        }

    if not await registry.begin_start(user_id, slug):
        return {"ok": False, "error": "Bot is already starting"}

    try:
        paths.user_pid_dir(user_id)

        # Same allowlist-only env as start_bot (r1-023). DRY_RUN is
        # set explicitly below because this spawn path deliberately
        # asks main_live.py to skip its input() confirmation.
        env = _bot_subprocess_env(user_id)
        env["PYTHONPATH"] = str(BASE_DIR)
        # main_live.py prompts the operator on non-dry-run launches and
        # also respects DRY_RUN=1 as a bypass — set it explicitly so a
        # non-TTY portal subprocess never hangs on input().
        env["DRY_RUN"] = "1"

        # ``start_new_session=True`` ≡ ``preexec_fn=os.setsid`` — see the
        # commentary on ``start_bot`` for the full rationale. Identical
        # PGID-isolation argument applies to live-mode dry-run subprocs.
        with open(bot.log_file, "a") as log_out:
            proc = subprocess.Popen(
                [PYTHON_BIN, str(BASE_DIR / "main_live.py"),
                 "--bot", slug, "--user-id", str(user_id), "--dry-run"],
                cwd=str(BASE_DIR),
                stdout=log_out,
                stderr=log_out,
                env=env,
                start_new_session=True,  # ≡ preexec_fn=os.setsid
            )
        logger.info(f"Bot {slug} started in DRY-RUN (PID {proc.pid})")

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if bot.pid_file.exists():
                break
            await asyncio.sleep(0.1)

        return {
            "ok": True,
            "message": f"{slug} started in DRY-RUN (PID {proc.pid})",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await registry.end_start(user_id, slug)


async def restart_bot(user_id: int, slug: str) -> dict:
    bot = await registry.get(user_id, slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}

    # Fire restart notification before tearing the subprocess down.
    # The portal owns the restart lifecycle, so the bot itself never
    # gets a chance to send this from inside its own engine loop.
    # Notifier resolves chat_id from the bot owner's telegram_config
    # — silent no-op if they haven't connected yet.
    cfg = None
    try:
        cfg = load_bot_config(bot.config_file)
        notifier = TelegramNotifier(user_id=user_id)
        notifier.notify_restart(cfg.name)
    except Exception as e:
        logger.warning("restart notify failed for %s: %s", slug, e)

    if bot.running:
        stop_result = await stop_bot(user_id, slug)
        if not stop_result.get("ok"):
            return stop_result

    # Poll up to 5s for PID file to disappear so start_bot() doesn't see
    # stale "already running" state from a slow-exiting previous process.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not bot.pid_file.exists() and not bot.running:
            break
        await asyncio.sleep(0.1)

    # Dispatch by mode — live bots restart back into dry-run (Phase 1
    # only supports that), paper bots restart into paper. If config
    # loading failed above, fall through to the paper path — that's the
    # historical behaviour.
    if cfg is not None and cfg.mode == Mode.LIVE:
        return await start_bot_dry_run(user_id, slug)
    return await start_bot(user_id, slug)


# ── FastAPI ───────────────────────────────────────────────────────────────────

# ── Request-ID context (audit r1-034) ───────────────────────────────────────
# The contextvar + filter live in core.logging_setup so main_web.py
# can attach the filter to its handlers at boot, before any module-
# import log lines fire. We import them above as _request_id_ctx /
# _RequestIdFilter and wire middleware + helpers against them here.


def current_request_id() -> str:
    """Accessor used by ``_audit`` (r1-031) and any other code that
    wants to correlate records. Returns ``'-'`` outside a request."""
    return _request_id_ctx.get()


# ── Global request-body size limit — PT-v4-NW-004 ──────────────────────────
#
# Audit PT-v4-NW-004 (MEDIUM, open) flagged that ``_read_body_with_cap``
# in web/routes/bots.py only protects the bot-config endpoints. Every
# OTHER POST endpoint (auth/login, admin/*, dashboard/layout, deals,
# annotations, …) reads its request body unbounded — an authenticated
# attacker (or accidental misconfigured client) can pin RAM by sending
# a 200 MB JSON payload. The middleware below caps every request body
# at ``REVERTO_MAX_REQUEST_BODY_BYTES`` (default 1 MiB) BEFORE auth
# runs, so an oversized body is refused without the engine ever having
# to allocate space for it. Endpoint-specific helpers like
# ``_read_body_with_cap`` (64 KiB for bot config) keep their tighter
# caps — the middleware is defence-in-depth above them, not a
# replacement.
_DEFAULT_MAX_REQUEST_BODY_BYTES = 1024 * 1024  # 1 MiB


def _max_request_body_bytes() -> int:
    """Resolve the global body-size cap from
    ``REVERTO_MAX_REQUEST_BODY_BYTES``. Read on each request so a
    monkeypatched test sees the override without restarting the
    process. Malformed / non-positive values fall back to the default
    so a typo can't silently disable the cap.
    """
    raw = os.environ.get("REVERTO_MAX_REQUEST_BODY_BYTES")
    if raw is None:
        return _DEFAULT_MAX_REQUEST_BODY_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "REVERTO_MAX_REQUEST_BODY_BYTES=%r is not an integer — "
            "falling back to default %d",
            raw, _DEFAULT_MAX_REQUEST_BODY_BYTES,
        )
        return _DEFAULT_MAX_REQUEST_BODY_BYTES
    if value <= 0:
        logger.warning(
            "REVERTO_MAX_REQUEST_BODY_BYTES=%d is non-positive — "
            "falling back to default %d",
            value, _DEFAULT_MAX_REQUEST_BODY_BYTES,
        )
        return _DEFAULT_MAX_REQUEST_BODY_BYTES
    return value


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds the global cap with HTTP 413.

    Two paths, mirroring the validate-config helper in
    ``web/routes/bots.py:_read_body_with_cap``:

    * **Content-Length header present** — parse and refuse before
      consuming the body. Malformed Content-Length is a 400 (matches
      the existing helper's contract). The well-known ``Content-Length:
      0`` short-circuits as a pass.
    * **Content-Length absent** (chunked / Transfer-Encoding) — wrap
      the ASGI ``receive`` callable so the byte counter trips inside
      the route handler the moment the cap is crossed. Without this
      a chunked client could still pin RAM by sending unbounded
      chunks.

    Methods without a body (GET, HEAD, OPTIONS, DELETE) skip both
    paths so the middleware only touches mutating requests. Note
    DELETE is treated as bodyless here — Reverto's DELETE endpoints
    don't accept payloads today (audit-checked), and adding the body
    cap would just slow down the safe path with no defensive value.

    Order: registered AFTER (= outer of) AuthMiddleware so an
    oversized body is refused without paying for an auth lookup.
    The middleware itself does no DB or filesystem work — it's a
    pure in-memory size check.
    """

    _BODYLESS_METHODS = {"GET", "HEAD", "OPTIONS", "DELETE"}

    async def dispatch(self, request, call_next):
        if request.method in self._BODYLESS_METHODS:
            return await call_next(request)

        cap = _max_request_body_bytes()

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                cl_int = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length"},
                )
            if cl_int > cap:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"Request body too large "
                            f"({cl_int} > {cap} bytes)"
                        ),
                    },
                )
            # Content-Length within cap → no need to wrap receive.
            # Endpoint-specific helpers (e.g. ``_read_body_with_cap``)
            # may still impose a tighter limit further down.
            return await call_next(request)

        # No Content-Length → streaming/chunked. Wrap ``receive`` so
        # the route handler's first ``await request.body()`` (or
        # ``async for chunk in request.stream()``) trips the cap as
        # soon as the running byte count crosses it.
        original_receive = request.receive
        accumulated = 0

        async def _capped_receive():
            nonlocal accumulated
            message = await original_receive()
            if message["type"] != "http.request":
                return message
            body_chunk = message.get("body", b"") or b""
            accumulated += len(body_chunk)
            if accumulated > cap:
                # Synthesise a "client disconnected" message so the
                # downstream handler short-circuits, and surface a 413
                # by raising; the surrounding except block converts.
                raise _RequestBodyTooLarge(accumulated, cap)
            return message

        # Replace the request's receive callable. Starlette's Request
        # exposes ``_receive`` as the source of every body read, so
        # this swap is the canonical extension point.
        request._receive = _capped_receive  # type: ignore[attr-defined]

        try:
            return await call_next(request)
        except _RequestBodyTooLarge as exc:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": (
                        f"Request body too large "
                        f"(>{exc.cap} bytes, observed {exc.observed})"
                    ),
                },
            )


class _RequestBodyTooLarge(Exception):
    """Internal sentinel raised by the wrapped ASGI ``receive`` so the
    surrounding middleware can map it to a 413 response. Not exposed
    via the public API."""

    def __init__(self, observed: int, cap: int) -> None:
        super().__init__(f"body {observed} > cap {cap}")
        self.observed = observed
        self.cap = cap


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign a 12-char request-id to every HTTP request. If the
    caller sent ``X-Request-Id``, honour it so an upstream proxy /
    tracing tool can stitch logs across service boundaries;
    otherwise mint a fresh one from uuid4().hex[:12]. The id lands
    back on the response as ``X-Request-Id`` for the client. Audit
    r1-034.
    """

    async def dispatch(self, request, call_next):
        raw = request.headers.get("X-Request-Id")
        # Defensive: ignore header values outside the allowed shape
        # so an attacker can't inject control characters into the
        # log-line via a spoofed header.
        if raw and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw):
            req_id = raw
        else:
            req_id = uuid4().hex[:12]
        token = _request_id_ctx.set(req_id)
        try:
            response = await call_next(request)
        finally:
            _request_id_ctx.reset(token)
        response.headers["X-Request-Id"] = req_id
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit-cookie CSRF defence (audit r1-073).

    On mutating requests (POST/PUT/PATCH/DELETE) the middleware
    compares the ``reverto_csrf`` cookie with the
    ``X-CSRF-Token`` header. Both must be present and equal —
    otherwise 403. This complements (not replaces) the existing
    SameSite=strict session cookie; an attacker who finds a way
    around SameSite (subdomain takeover, stale browser) still
    can't mint a matching token because the cookie is same-
    origin-only readable.

    Scope carve-outs:
      * Only authenticated requests are checked. A caller
        without the session cookie can't have meaningful side
        effects anyway — the auth layer rejects them.
      * ``/auth/login`` itself is exempt because the caller has
        no token yet; that endpoint issues the first CSRF
        cookie on success.
      * Only mutating HTTP methods are checked. GET/HEAD/OPTIONS
        are read-only by HTTP spec; letting them through is the
        standard CSRF pattern.

    Graceful migration (hotfix): sessions minted before VPS-1
    shipped have no ``reverto_csrf`` cookie. A strict check
    would 403 every mutating request from those sessions until
    the user logs out + back in. Instead, when an authenticated
    request arrives without the CSRF cookie, the middleware
    mints one on the fly and attaches it to the response. On
    mutating requests this is a one-shot bypass — the NEXT
    mutating request will have the cookie and face normal
    enforcement. Safe because SameSite=strict on the session
    cookie already blocks the cross-site attack this check
    defends against; the bypass window is a single same-origin
    request per legacy session.

    Timing-safe compare via ``secrets.compare_digest`` so a
    malicious client can't infer token bytes from response
    timing.
    """

    async def dispatch(self, request, call_next):
        authenticated = bool(request.cookies.get(_SESSION_COOKIE))
        has_csrf_cookie = bool(request.cookies.get(_CSRF_COOKIE))

        # Graceful-migration path: authenticated request that pre-
        # dates the CSRF rollout. Mint a cookie + attach it to the
        # response so the SPA has one for the next request. The
        # current request is allowed through regardless of method
        # — SameSite=strict on the session cookie means this can
        # only have been same-origin, so the CSRF threat model
        # isn't broken by the one-shot grant.
        migrate_legacy_session = authenticated and not has_csrf_cookie

        if migrate_legacy_session:
            logger.info(
                "CSRF graceful-migration: minting cookie for legacy "
                "session path=%s method=%s",
                request.url.path, request.method,
            )
            new_token = _mint_csrf_token()
            response = await call_next(request)
            _set_csrf_cookie_on_response(response, new_token)
            return response

        if request.method not in _CSRF_MUTATING_METHODS:
            return await call_next(request)
        if request.url.path in _CSRF_EXEMPT_PATHS:
            return await call_next(request)
        # No session cookie → unauth path, let the auth layer
        # handle the rejection. Checking CSRF here would just
        # produce a 403 instead of the auth layer's 401; the
        # latter is the correct signal to the client.
        if not authenticated:
            return await call_next(request)

        cookie_token = request.cookies.get(_CSRF_COOKIE)
        header_token = request.headers.get(_CSRF_HEADER)
        if not cookie_token or not header_token:
            logger.warning(
                "CSRF check failed (missing token): path=%s cookie=%s header=%s",
                request.url.path,
                bool(cookie_token),
                bool(header_token),
            )
            return JSONResponse(
                {"detail": "CSRF token required"},
                status_code=403,
            )
        if not secrets.compare_digest(cookie_token, header_token):
            logger.warning("CSRF check failed (token mismatch): path=%s",
                           request.url.path)
            return JSONResponse(
                {"detail": "CSRF token mismatch"},
                status_code=403,
            )
        return await call_next(request)


# SHA-256 hash of the anti-flash safety-net inline script in
# ``web/static/index.html`` (the ~5-line setTimeout that flips
# body.auth-checked after 3s if app.js stalls). The script must
# stay inline because it is the visibility-fallback for app.js
# itself failing to load — we cannot externalise it without
# defeating the whole point. Whitelisting via SHA-256 keeps the
# strict ``script-src 'self' …`` posture (no ``'unsafe-inline'``)
# while letting this one specific script through.
#
# **If you change the inline script in index.html, regenerate
# this hash:**
#
#   python3 -c "import hashlib, base64, pathlib; \
#     html = pathlib.Path('web/static/index.html').read_text(); \
#     body = html.split('<script>', 1)[1].split('</script>', 1)[0]; \
#     d = hashlib.sha256(body.encode()).digest(); \
#     print('sha256-' + base64.b64encode(d).decode())"
#
# Or grab it from a browser CSP-violation console line — Chrome/
# Firefox both report the expected hash inline with the violation.
# A class-of-issue regression test in
# ``tests/test_security_headers.py::test_csp_inline_script_hash_matches_index_html``
# fails fast if the hash drifts from the script content.
_INLINE_SCRIPT_CSP_HASH = "sha256-XQeafA09ntbXPkIJAbfACudywWm3RsGcY2+OrHfznMc="


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach standard hardening headers to every HTTP response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            # Inline-script hash whitelists exactly the auth-checked
            # safety-net in index.html (see _INLINE_SCRIPT_CSP_HASH
            # comment block above). Every OTHER inline script stays
            # blocked — no ``'unsafe-inline'`` here.
            f"script-src 'self' '{_INLINE_SCRIPT_CSP_HASH}' https://unpkg.com; "
            # unpkg also hosts the GridStack stylesheet that the
            # Workspace view pulls in. Without this entry the
            # panel grid loses its layout rules and panels render
            # as plain vertically-stacked divs.
            # r1-076: 'unsafe-inline' on style-src stays by necessity
            # for now — the SPA uses inline styles across chart
            # tooltips, dynamic panel layouts, theme-switching, and
            # several dashboards. Full removal needs a refactor of
            # every inline style-attribute + <style> block into CSS
            # classes; deferred to post-launch hardening (tracked
            # in the audit report).
            "style-src 'self' 'unsafe-inline' https://unpkg.com; "
            "img-src 'self' data:; "
            # unpkg.com is allowed here so DevTools can fetch the
            # Lightweight Charts + GridStack sourcemap files that
            # accompany the minified bundles we load. Pure-
            # developer-ergonomics: the browser emits CSP-violation
            # console noise for every missing .map otherwise. No
            # data-endpoint risk — the files are public static
            # assets.
            #
            # r1-076 (VPS-1): ws:/wss: wildcards removed. All
            # Reverto WebSocket endpoints (/ws/state, /ws/logs/*)
            # are same-origin and are covered by 'self', which
            # matches the request scheme (ws:// on http://,
            # wss:// on https://). Browsers with partial SameSite
            # implementations or subdomain-takeover scenarios no
            # longer have an open WS channel to abuse.
            "connect-src 'self' https://unpkg.com; "
            "frame-ancestors 'none'; "
            # Audit PT-v4-NW-001 — added 2026-05-04. Two OWASP-
            # recommended hardening directives that the marketing-side
            # reverto.bot Caddy CSP already had; the app-side CSP
            # asymmetry was the wrong direction (more sensitive site
            # had weaker CSP). With any HTML-injection primitive on
            # app.reverto.bot, ``base-uri 'self'`` blocks an injected
            # ``<base href="https://attacker">`` tag from hijacking
            # every relative URL on the page (script srcs, form
            # actions, link hrefs); ``form-action 'self'`` blocks
            # injected ``<form action="https://attacker">`` from
            # exfiltrating credentials + CSRF tokens via attacker-
            # controlled submission endpoints. Both are defence-in-
            # depth on top of the existing strict ``script-src``,
            # but the strictest CSP wins when XSS surfaces unexpectedly.
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"]        = "no-referrer"
        # Permissions-Policy (audit pd-011) — opt out of every
        # browser sensor / device API. Reverto is a trading portal:
        # it has no legit use for camera / microphone / geolocation
        # / payment / usb / sensors. Denying them here means an XSS
        # or compromised third-party script can't prompt the user
        # for those permissions either. Empty allowlist `=()` is
        # the "deny-all-origins" syntax.
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), "
            "payment=(), usb=(), bluetooth=(), "
            "accelerometer=(), gyroscope=(), magnetometer=()"
        )
        # HSTS — audit r1-075: instruct browsers to pin HTTPS for the
        # portal host. Only emit on an actual HTTPS request so an
        # operator running `make start` on http://localhost doesn't
        # end up with the browser stuck in a forced-HTTPS state. The
        # scheme check covers both direct TLS and the standard
        # Forwarded headers that reverse proxies (Caddy, nginx)
        # inject — Starlette's ``url.scheme`` reads the
        # ``X-Forwarded-Proto`` header via its Trusted Host setup, so
        # this works transparently behind a TLS-terminating proxy.
        # max-age=31536000 (1 year) + includeSubDomains matches the
        # industry-standard HSTS policy recommended by OWASP.
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        # Audit r3-003 (defense-in-depth): the **primary** fix for
        # the uvicorn ``Server`` fingerprint is ``server_header=False``
        # in ``uvicorn.Config(...)`` — uvicorn injects the header at
        # the H11 protocol layer, AFTER Starlette's middleware chain,
        # so the only effective place to suppress it is the config
        # call. This middleware-side delete is a belt-and-braces
        # guard against a future reverse-proxy or upstream that might
        # inject ``Server`` into the response. Starlette's
        # MutableHeaders raises on `del` if the key is absent, so
        # gate on membership (case-insensitive lookup).
        if "server" in response.headers:
            del response.headers["server"]
        return response


_PUBLIC_PATHS = {
    "/",
    "/favicon.ico",
    "/health",
    "/healthz",
    "/readyz",
    "/metrics",
    "/auth/status",
    "/auth/login",
    # Phase B PR 3: /auth/login/totp is the second step of the
    # 2FA login flow. By definition it runs BEFORE the session
    # cookie is minted (the password step staged a pending cookie
    # only) — so AuthMiddleware would otherwise 303 it to /.
    # The endpoint enforces its own auth via the signed pending-
    # login-TOTP cookie that /auth/login set.
    "/auth/login/totp",
    "/auth/logout",
    # Note: /api/roadmap and /api/changelog used to live here as
    # public-shell endpoints (logged-out visitors saw them inside
    # the SPA at /#roadmap + /#changelog). PR 3 of the marketing-
    # app split moved that public surface to the static site at
    # https://reverto.bot — the marketing pages read from JSON
    # snapshots written by core/marketing_export.py. The /api
    # routes themselves still exist but are now session-required;
    # they back the in-app SPA (logged-in only) and the admin
    # counterparts at /api/admin/{roadmap,changelog}/*.
}


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate every HTTP request on a valid session cookie, except for the
    small set of public paths (landing page, static assets, auth endpoints).

    API requests without a cookie get a JSON 401 so the SPA fetch helpers
    can handle them. Non-API requests get a redirect to /, where the SPA
    will render the login view.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        # Telegram webhook: auth surface is the URL-path secret,
        # not a session cookie. The handler verifies the secret +
        # 404s on mismatch so an unauthenticated bypass cannot reach
        # the consume_link_token path with a guessed token.
        if path.startswith("/api/telegram/webhook/"):
            return await call_next(request)

        if _verify_session_cookie(request.cookies.get(_SESSION_COOKIE)):
            return await call_next(request)

        # API key callers still pass through, but ONLY via the X-API-Key
        # header. The legacy `?api_key=` query-string fallback was
        # dropped — query strings end up in proxy / nginx / cloud
        # access logs and the browser's history, which leaked the
        # long-lived API key to every collector that tailed those
        # files. Scripts and CI tools must send the header.
        provided = request.headers.get("X-API-Key")
        if provided and secrets.compare_digest(provided, _API_KEY):
            return await call_next(request)

        accept = request.headers.get("accept", "")
        if (
            path.startswith("/api/")
            or path.startswith("/ws")
            or "application/json" in accept
        ):
            return JSONResponse(
                {"detail": "Authentication required"}, status_code=401
            )
        return RedirectResponse(url="/", status_code=303)


# Rate limiter — beperkt brute force en DoS op control endpoints.
# Audit r1-004: the key function prefers the leftmost ``X-Forwarded-For``
# entry when present so every client behind a reverse proxy (Caddy,
# nginx, Cloudflare) gets its own bucket. Without this every request
# reaching the portal would share the proxy's IP and rate-limits
# effectively disappear.
#
# Trust model: the reverse proxy MUST overwrite X-Forwarded-For (not
# append), otherwise a client can inject a fake leftmost entry and
# sidestep the limiter. This is the default for Caddy's
# ``reverse_proxy`` and nginx's ``proxy_set_header X-Forwarded-For
# $remote_addr;`` pattern. Document in runbook.
def _rate_limit_key_func(request: Request) -> str:
    # Audit r1-044: prefer a per-user key when the caller has a
    # valid session cookie. Two users behind the same NAT IP
    # (office, VPN, household) then don't stomp on each other's
    # rate-limit buckets. Unauthenticated paths (login, health
    # probes, password-reset) fall through to the IP-based keying
    # because there's no user identity to key on yet.
    cookie = request.cookies.get(_SESSION_COOKIE)
    if cookie:
        payload = _verify_session_cookie(cookie)
        if payload is not None:
            uid = payload.get("uid")
            if isinstance(uid, int) and uid > 0:
                return f"user:{uid}"
    # IP fallback — same X-Forwarded-For handling as the audit
    # logger (r1-004 trust model). Both pathways must agree on
    # what counts as "the client IP" or rate-limit attribution
    # and audit attribution will diverge under a Caddy-misconfig
    # incident — exactly the moment they need to match.
    return _extract_client_ip(request) or get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key_func)


async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Too many requests"})


# Initialise the SQLite ledger on portal boot. Idempotent — safe to call
# on every restart; creates logs/reverto.db + schema on first run.
# Phase-3a: admin password is NULL after init; operator provisions via
# scripts/setup_admin.py. Login endpoint's verify_password fails closed
# until that runs.
try:
    _init_db()
except DatabaseMigrationError as _dbe:
    # Audit v26-10 guard: destructive schema migration was refused
    # because the operator hasn't opted in. Fail HARD at startup
    # with a stderr-visible message — the alternative (logging a
    # warning and continuing) leaves the portal running against a
    # stale schema, which was the original bug this guard closes.
    sys.stderr.write(f"\n[FATAL] {_dbe}\n\n")
    sys.stderr.flush()
    sys.exit(1)
except Exception as _e:  # pragma: no cover - defensive
    logger.warning("init_db failed on portal startup: %s", _e)

def _validate_config() -> None:
    """Check critical env-vars at portal boot (audit r1-058, pd-025).

    Raises ``RuntimeError`` for missing *required* vars so uvicorn
    fails fast and the operator sees a clear stderr message
    instead of discovering the gap at first user-action. Logs a
    ``WARNING`` for missing *recommended* vars (features that
    degrade gracefully but should be flagged — live-mode bots
    without exchange credentials, say).

    Required:
      * REVERTO_SECRET_KEY — session-cookie signing. The module-
        import fallback generates an ephemeral key with a warning
        for local dev, but in a real deploy an ephemeral key
        invalidates every live session on the next restart. Hard
        fail at boot is strictly better observability.
      * REVERTO_API_KEY — X-API-Key authentication (audit pd-025).
        Same reasoning as SECRET_KEY: the module-import fallback
        writes an ephemeral key to ``logs/.api_key_ephemeral`` and
        deletes it at shutdown, so without this env var every
        portal restart rotates the key and silently breaks CI /
        backup scripts / integrations that cached the prior value.
        Elevated from *recommended* to *required* so the deploy
        fails fast rather than drifting.

    Recommended (warn only):
      * BITGET_API_KEY    — required for live-mode Bitget bots
      * BITGET_API_SECRET — required for live-mode Bitget bots
    """
    required = {
        "REVERTO_SECRET_KEY": "required for session signing",
        "REVERTO_API_KEY":
            "required for API-key authentication — prevents "
            "ephemeral-key rotation on portal restart",
    }
    recommended = {
        "BITGET_API_KEY":    "required for live-mode Bitget bots",
        "BITGET_API_SECRET": "required for live-mode Bitget bots",
    }

    missing_required = [
        f"{k} ({desc})" for k, desc in required.items()
        if not os.environ.get(k)
    ]
    if missing_required:
        raise RuntimeError(
            "Missing required env-vars: " + ", ".join(missing_required)
            + ". Set them in .env (see .env.example) before starting the "
            "portal.",
        )

    missing_recommended = [
        f"{k} ({desc})" for k, desc in recommended.items()
        if not os.environ.get(k)
    ]
    if missing_recommended:
        logger.warning(
            "Missing recommended env-vars: %s",
            ", ".join(missing_recommended),
        )


def _validate_config_completeness() -> None:
    """Warn the operator if ``.env.example`` lists an env-var that
    isn't set in the running process. Catches drift between the
    template and the actual ``.env`` (audit r1-059).

    Runs after ``_validate_config`` in lifespan startup. Best-effort:
    missing template file = silent no-op (development workflow may
    not ship one). ``_VALIDATE_CONFIG_SUPPRESS_EXAMPLE_CHECK=1``
    skips the check entirely, for the rare CI where a .env.example
    is intentionally a superset (e.g. testing a feature that isn't
    merged yet).
    """
    if os.environ.get("_VALIDATE_CONFIG_SUPPRESS_EXAMPLE_CHECK") == "1":
        return
    example_path = BASE_DIR / ".env.example"
    if not example_path.exists():
        return
    example_vars: set[str] = set()
    try:
        for line in example_path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                # Guard against inline comments that include '=' —
                # an env-var name must match the standard shell
                # identifier shape.
                if key and key.replace("_", "").isalnum():
                    example_vars.add(key)
    except OSError as e:
        logger.debug("_validate_config_completeness: %s read failed: %s",
                     example_path, e)
        return
    missing = sorted(v for v in example_vars if not os.environ.get(v))
    if missing:
        logger.warning(
            "Env-vars listed in .env.example but not set in the "
            "running environment: %s. Update your .env from "
            ".env.example if these are needed (audit r1-059).",
            ", ".join(missing),
        )


def _maybe_seed_audit_findings() -> None:
    """Run the audit-findings YAML seed on first boot only.

    Idempotent: the seed importer's INSERT OR IGNORE means re-runs
    are no-ops on already-present rows, but we additionally short-
    circuit when the table is non-empty so a slow seed (~240 rows)
    doesn't add measurable startup latency on every boot.

    Best-effort: any error logs a warning and returns. The admin UI
    is graceful in the empty-table case so a failed seed does not
    block portal startup or any operator workflow.
    """
    try:
        from core import audit_findings_store
        if audit_findings_store.count_total() > 0:
            return
    except Exception as e:
        logger.warning("Audit-findings seed: count check failed: %s", e)
        return
    try:
        from scripts.seed_audit_findings import (
            DEFAULT_SEED_PATH, import_seed, load_seed,
        )
        items = load_seed(DEFAULT_SEED_PATH)
        inserted, _ = import_seed(items, quiet=True)
        logger.info(
            "Audit-findings seed: imported %d findings on first boot",
            inserted,
        )
    except Exception as e:
        logger.warning("Audit-findings seed failed: %s", e)


async def _reconcile_bot_states_on_startup() -> None:
    """Walk every registered bot and force a ``read_state`` call.

    ``BotInfo.read_state`` already performs the silent-exit reconcile
    (state.running=true but PID dead OR heartbeat stale → write
    running=false on disk) — calling it once per bot at startup
    flushes any drift left by the previous portal invocation before
    the first API request lands. Without this, a bot whose subprocess
    was killed by the prior cgroup-cleanup would still show as
    "RUNNING" until an operator opened its detail page (which
    triggers read_state via /api/bots/<slug>).

    Best-effort: any per-bot exception is logged and skipped so one
    bad state file cannot block portal startup.
    """
    # Lazy import — STATE_SCHEMA_VERSION lives next to the engine code
    # whose schema it gates. Importing at module-load time would force
    # paper.paper_engine into memory before the engines actually need
    # it; deferring keeps lifespan startup as cheap as possible.
    from paper.paper_engine import STATE_SCHEMA_VERSION

    try:
        bots = await registry.all()
    except Exception as e:
        logger.warning("Startup reconcile: registry.all() failed: %s", e)
        return
    if not bots:
        return
    reconciled = 0
    restarted = 0
    for bot in bots:
        try:
            state = bot.read_state()
            if state.get("stopped_reason") in ("silent_exit", "heartbeat_stale"):
                reconciled += 1
                # A bot that just got reconciled to "stopped" is
                # already dead — the schema-mismatch path is for
                # *running* bots on stale code. Skip the mismatch
                # check here.
                continue
            if _bot_needs_restart(state, STATE_SCHEMA_VERSION):
                logger.warning(
                    "Bot %s/%s schema mismatch (running=v%s, "
                    "portal=v%s) — auto-restart",
                    bot.user_id, bot.slug,
                    state.get("state_schema_version", "unknown"),
                    STATE_SCHEMA_VERSION,
                )
                if await _attempt_bot_auto_restart(bot):
                    restarted += 1
        except Exception as e:
            logger.warning(
                "Startup reconcile failed for %s/%s: %s",
                bot.user_id, bot.slug, e,
            )
    logger.info(
        "Startup reconcile: walked %d bot(s), %d silent-exit reconciled, "
        "%d schema-mismatch auto-restart",
        len(bots), reconciled, restarted,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler.

    Startup: validate critical env-vars (audit r1-058) then spawn
    background tasks that tail bot logs and watch state.json
    mtimes. References are stored so shutdown can cancel them
    cleanly.

    Shutdown: cancel background tasks and wait for them to return.
    Without this, uvicorn's graceful-shutdown path hangs waiting for
    asyncio.sleep() calls inside the tasks' while-True loops — which
    is what caused the SIGKILL fallback in stop.sh to fire on every
    make restart.
    """
    _validate_config()
    _validate_config_completeness()
    # Audit pd-044: sweep orphan ``.tmp`` files left behind by
    # ungraceful previous shutdowns. Cheap per-process; scoped to
    # directories Reverto owns so we never touch anything else.
    from core.cleanup import cleanup_orphaned_tmp_files
    cleanup_orphaned_tmp_files(
        BASE_DIR / "logs",
        BASE_DIR / "credentials",
        # PT-v4-FS-004: a Fernet-key rotation that crashes between
        # creating ``keys/<uid>.key.tmp`` and ``os.replace`` would
        # otherwise leave the orphan on disk indefinitely.
        BASE_DIR / "keys",
    )
    # Lifecycle-stability: reconcile every bot's on-disk state with
    # PID + heartbeat truth at startup. If the previous portal
    # invocation (or its bots) crashed silently, state.json may still
    # say ``running: true`` — surface the correction now so the UI is
    # consistent the moment the portal starts serving requests.
    await _reconcile_bot_states_on_startup()
    # First-boot seed of the audit-findings tracker. Idempotent: the
    # importer skips rows that already exist, so a re-run on every
    # restart is cheap and self-correcting if the seed file ever
    # gains entries between deploys. Best-effort — a YAML parse
    # error must not block portal startup.
    _maybe_seed_audit_findings()
    logger.info("=== Portal started ===")

    background_tasks: list[asyncio.Task] = [
        asyncio.create_task(tail_logs(), name="tail_logs"),
        asyncio.create_task(watch_state_files(), name="watch_state_files"),
    ]

    try:
        yield
    finally:
        logger.info("=== Portal shutting down ===")
        for task in background_tasks:
            task.cancel()
        # Wait with timeout so a wedged task cannot block shutdown
        # indefinitely. 2s is well under stop.sh's 5s grace period so
        # we still leave headroom for uvicorn's own cleanup.
        try:
            await asyncio.wait_for(
                asyncio.gather(*background_tasks, return_exceptions=True),
                timeout=2.0,
            )
            logger.info("Background tasks cancelled cleanly")
        except asyncio.TimeoutError:
            logger.warning(
                "Background task cancellation timed out after 2s — "
                "uvicorn will force-close remaining tasks"
            )
        logger.info("=== Portal stopped ===")


app = FastAPI(
    title="Reverto Portal",
    # Audit r1-048 (ACCEPTED): Swagger UI + ReDoc + the raw
    # ``/openapi.json`` are all disabled. Reverto is a single-
    # operator platform today; exposing the spec would leak
    # internal route semantics (payload shapes, auth patterns,
    # admin-gated endpoints) without a user benefit. Defense-in-
    # depth: ``openapi_url=None`` too so the JSON spec isn't
    # reachable even if Starlette's defaults ever change.
    # Revisit if an external integration partner ever needs it.
    docs_url=None, redoc_url=None, openapi_url=None,
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
# Middleware order (Starlette wraps last-added = outermost):
#   add_middleware(AuthMiddleware)             → runs innermost
#   add_middleware(CSRFMiddleware)
#   add_middleware(SecurityHeadersMiddleware)  → runs middle
#   add_middleware(BodySizeLimitMiddleware)
#   add_middleware(RequestIdMiddleware)        → runs outermost
# Request flow:  RequestId → BodySizeLimit → SecurityHeaders →
#                CSRF → Auth → route handler
# Response flow: route handler → Auth → CSRF → SecurityHeaders →
#                BodySizeLimit → RequestId
# RequestId sits outermost so the X-Request-Id header lands on
# *every* response (including auth 401/403 short-circuits) and so
# the contextvar is populated before any middleware's log lines
# reference it. BodySizeLimit sits OUTSIDE Auth/CSRF so an
# oversized body is refused (PT-v4-NW-004) without paying for the
# auth lookup or token validation.
app.add_middleware(AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(RequestIdMiddleware)

# Attach the request-id filter to the root logger + every existing
# handler so ``%(request_id)s`` in any formatter resolves to the
# active request's id. Filter is safe to attach multiple times —
# logging.Filter is checked with .addFilter's identity semantics,
# but a fresh instance is cheap. We wire a new instance into each
# handler to avoid any cross-filter state concern.
_root_logger = logging.getLogger()
_root_logger.addFilter(_RequestIdFilter())
for _h in _root_logger.handlers:
    _h.addFilter(_RequestIdFilter())
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Auth endpoints ────────────────────────────────────────────────────────────


# Auth routes: moved to web/routes/auth.py.


@app.get("/", response_class=HTMLResponse)
async def index():
    f = STATIC_DIR / "index.html"
    return HTMLResponse(f.read_text(encoding="utf-8") if f.exists() else "<h1>Not found</h1>")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the multi-res ICO from web/static/ at the canonical root
    path. Browsers hit /favicon.ico on every pageload regardless of
    whatever <link rel="icon"> the HTML declares, and they do it
    before the session cookie is set — which is why /favicon.ico is
    already in AuthMiddleware._PUBLIC_PATHS.
    """
    f = STATIC_DIR / "favicon.ico"
    if not f.exists():
        raise HTTPException(status_code=404, detail="favicon missing")
    return FileResponse(f, media_type="image/x-icon")


def _compute_summary(bots: list[dict]) -> dict:
    """Single source of truth for the dashboard summary block.

    Used by both GET /api/bots and the /ws/state watcher, so the
    HTTP fallback path and the WS push have exactly the same shape.
    closed_deals is aggregated across all bots so future WS
    consumers can show it without an extra round-trip.
    """
    total_pnl  = sum(b.get("total_pnl_btc", 0) for b in bots)
    active     = sum(1 for b in bots if b.get("running"))
    open_cnt   = sum(b.get("open_deals_count", 0) for b in bots)
    closed_cnt = sum(b.get("closed_deals_count", 0) for b in bots)
    return {
        "total_pnl_btc": round(total_pnl, 8),
        "active_bots":   active,
        "total_bots":    len(bots),
        "open_deals":    open_cnt,
        "closed_deals":  closed_cnt,
    }


# Bot read + lifecycle routes: moved to web/routes/bots.py.


# ── Ops endpoints — health checks + Prometheus ──────────────────────────────
#
# The actual route handlers now live in web/routes/admin.py and
# web/routes/drawdown.py — included at the bottom of this module so
# the module-level names they import (limiter, _request_actor,
# registry, stop_bot, _audit, _BOT_SLUG_RE,
# _check_db_sync_blocking) are already defined by the time the
# route modules load.


def _check_db_sync_blocking() -> None:
    """Blocking DB ping used by /readyz. Lives at module level so the
    route module (web/routes/admin.py) can import it without pulling
    in the routing decorators."""
    from core.database import get_db
    conn = get_db()
    conn.execute("SELECT 1").fetchone()


# /api/price moved to web/routes/chart.py.


# ── Chart OHLCV endpoint ──────────────────────────────────────────────────────
# In-memory cache for chart fetches. Keyed on (pair_normalized, timeframe,
# limit) → (expires_at, payload). 60s TTL keeps Bitget's REST endpoint
# reasonably idle even when several tabs poll the same chart simultaneously.
# Bounded LRU: 60s TTL plus a 256-entry hard cap. Without the cap the
# key space (pair × timeframe × limit) was effectively unbounded, so a
# misbehaving or hostile client could grow the cache indefinitely just
# by walking the limit parameter. OrderedDict.move_to_end gives us
# O(1) recency tracking; the eldest entry is evicted on miss when the
# cap is reached. Expired entries are dropped on access.
_chart_cache: "OrderedDict[tuple, tuple[float, list]]" = OrderedDict()
# TTL per timeframe — the longer the bar, the slower the data moves,
# so we can hold each cached payload longer without hiding a real
# market move from the user. 15m bars go stale fast (operators want
# to see the latest wick); 1d bars are essentially static.
_CHART_CACHE_TTL = {
    "15m": 30.0,
    "30m": 60.0,
    "1h":  120.0,
    "2h":  180.0,
    "4h":  300.0,
    "12h": 450.0,
    "1d":  600.0,
    "3d":  900.0,
    "1w":  1800.0,
}
_CHART_CACHE_TTL_DEFAULT = 60.0
_CHART_CACHE_MAX = 256
_CHART_TIMEFRAMES = (
    "15m", "30m", "1h", "2h", "4h", "12h", "1d", "3d", "1w",
)
# Audit r1.1-002: allowlist of pairs the chart/ticker/candles endpoints
# accept. Without this, any string passed through ``_normalize_chart_pair``
# lands in the ccxt call + the LRU caches briefly — a hostile authenticated
# client could evict legitimate cache entries by spamming unlisted symbols.
# Keep the set conservative; extend when we wire a new trading pair
# end-to-end (exchange SYMBOL_MAPS + UI dropdown + state schema). Today
# Reverto trades BTC/USD inverse-perp; BTC/USDT is future-proofing for
# when spot wiring lands.
_CHART_PAIRS_ALLOWLIST = frozenset({"BTC/USD", "BTC/USDT"})
_chart_lock = asyncio.Lock()


def _normalize_chart_pair(raw: str) -> str:
    """Accept BTCUSD or BTC/USD and return BTC/USD form expected by
    PublicExchange.SYMBOL_MAPS."""
    s = raw.strip().upper()
    if "/" in s:
        return s
    if s.endswith("USDT"):
        return f"{s[:-4]}/USDT"
    if s.endswith("USD"):
        return f"{s[:-3]}/USD"
    return s


# /api/chart/{pair}/{timeframe} moved to web/routes/chart.py.


# ── Ticker cache (for /api/ticker — workspace chart-panel info-sidebar) ─────
# 10 s TTL covers the sidebar's 5 s poll without hitting Bitget on every
# request. 32-entry cap is generous — we rarely serve more than a
# handful of distinct pairs. Shares the same _price_lock semantic as
# /api/price because both call ccxt.fetch_ticker on the module-level
# _bitget_client, and ccxt clients aren't thread-safe.
_ticker_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_TICKER_CACHE_TTL = 10.0
_TICKER_CACHE_MAX = 32
_ticker_lock = asyncio.Lock()


# ── Candle range endpoint (for client-side backtester) ───────────────────────
#
# Separate cache from /api/chart: different TTL needs and different key shape
# (start/end ISO timestamps). 5-minute TTL, 64-entry cap. ccxt's fetch_ohlcv
# caps at 1000 bars per call, so we paginate transparently here, dedupe on
# ms-timestamp and return the same JSON shape as /api/chart.
_candles_cache: "OrderedDict[tuple, tuple[float, list]]" = OrderedDict()
_CANDLES_CACHE_TTL = 300.0
_CANDLES_CACHE_TTL_LARGE = 1800.0  # 30 min for limit > 5000
_CANDLES_CACHE_LARGE_THRESHOLD = 5000
_CANDLES_CACHE_MAX = 64
_CANDLES_MAX_BARS  = 300000
_CANDLES_PAGE_SLEEP_S = 0.15  # pause between paginated ccxt calls

# Dedicated lock for _candles_cache so cache hit / miss / insert is a
# single atomic critical section. Separate from _chart_lock because the
# candles endpoint can hold its lock for seconds while paginating a
# large range, and blocking /api/chart on that would starve the
# dashboard polling loop.
_candles_lock = asyncio.Lock()

_TF_SECONDS = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h":  60 * 60,
    "2h":  2 * 60 * 60,
    "4h":  4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d":  24 * 60 * 60,
    "3d":  3 * 24 * 60 * 60,
    "1w":  7 * 24 * 60 * 60,
}


def _parse_iso_utc(s: str) -> datetime:
    """Parse an ISO 8601 date string into an aware UTC datetime.
    Accepts trailing 'Z' (Python < 3.11 doesn't parse it natively)."""
    if not isinstance(s, str) or not s:
        raise ValueError("empty timestamp")
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _fetch_ohlcv_page_with_retry(
    client, symbol: str, timeframe: str, since_ms: int, limit: int
) -> list:
    """Single ccxt fetch_ohlcv call with exponential-backoff retry.

    Bitget occasionally returns error code 40017 ("Parameter verification
    failed startTime || endTime") on large ranges or transient hiccups —
    retry up to 3 times with 0.5s / 1.0s / 2.0s backoff before giving
    up. The caller treats a final failure as "no more data from this
    cursor" so one bad page doesn't throw the whole fetch away.
    """
    last_err = None
    for attempt in range(3):
        try:
            return await asyncio.to_thread(
                client.client.fetch_ohlcv,
                client._symbol(symbol),
                timeframe,
                since_ms,
                limit,
            )
        except Exception as e:  # noqa: BLE001 — surface every ccxt error
            last_err = e
            wait = 0.5 * (2 ** attempt)
            logger.warning(
                "fetch_ohlcv attempt %d/3 failed for %s %s since=%d: %s "
                "(retrying in %.1fs)",
                attempt + 1, symbol, timeframe, since_ms, e, wait,
            )
            await asyncio.sleep(wait)
    logger.error(
        "fetch_ohlcv exhausted retries for %s %s since=%d: %s",
        symbol, timeframe, since_ms, last_err,
    )
    return []


async def _fetch_ohlcv_range(
    client, symbol: str, timeframe: str, start_ms: int, end_ms: int
) -> list:
    """Paginated fetch_ohlcv driven purely by `since`.

    ccxt / Bitget's inverse-swap endpoint is unreliable when both
    startTime and endTime are passed on large windows (error 40017
    "Parameter verification failed"). Walking forward with `since`
    only — letting the exchange pick its own page size, then bumping
    `since` past the newest returned bar — is the most portable
    pattern and what every other ccxt-based backtester does. Stops
    as soon as the newest returned bar reaches end_ms or two
    consecutive empty pages come back.
    """
    tf_ms = _TF_SECONDS[timeframe] * 1000
    bars: dict[int, list] = {}
    since = start_ms
    empty_pages = 0
    pages_fetched = 0
    max_pages = (_CANDLES_MAX_BARS // 200) + 16
    expected_pages = max(1, ((end_ms - start_ms) // tf_ms + 199) // 200)
    logger.info(
        "Fetching ~%d pages for %s %s (max %d, range %d→%d)",
        expected_pages, symbol, timeframe, max_pages, start_ms, end_ms,
    )
    for _ in range(max_pages):
        if since >= end_ms:
            break
        if pages_fetched > 0:
            await asyncio.sleep(_CANDLES_PAGE_SLEEP_S)
        page = await _fetch_ohlcv_page_with_retry(
            client, symbol, timeframe, since, 200,
        )
        pages_fetched += 1
        if page:
            page_last = int(page[-1][0])
            logger.info(
                "Page %d: since=%d got=%d last=%d",
                pages_fetched, since, len(page), page_last,
            )
        else:
            logger.info("Page %d: since=%d empty", pages_fetched, since)
        if not page:
            empty_pages += 1
            if empty_pages >= 2:
                logger.info(
                    "Two empty pages in a row for %s %s — stopping with %d bars",
                    symbol, timeframe, len(bars),
                )
                break
            since += tf_ms * 200
            continue
        empty_pages = 0
        page_max_ts = 0
        for row in page:
            ts = int(row[0])
            if ts > page_max_ts:
                page_max_ts = ts
            if ts < start_ms or ts > end_ms:
                continue
            bars[ts] = row
        if page_max_ts >= since + tf_ms:
            since = page_max_ts + tf_ms
        elif page_max_ts > 0:
            since = page_max_ts + tf_ms
        else:
            since += tf_ms
    logger.info(
        "Fetch complete for %s %s: %d bars over %d pages",
        symbol, timeframe, len(bars), pages_fetched,
    )
    return [bars[k] for k in sorted(bars.keys())]


# /api/candles/{pair}/{timeframe} moved to web/routes/chart.py.


# ── Exchange credentials API ──────────────────────────────────────────────────

# Exchange-credentials routes: moved to web/routes/exchanges.py.


# ── Bot YAML beheer ───────────────────────────────────────────────────────────


def _bot_yaml_path(user_id: int, slug: str) -> Path:
    """Legacy helper — delegates to core.paths.bot_yaml_path so the
    composite-key layout is defined in exactly one module."""
    return paths.bot_yaml_path(user_id, slug)


def _validate_bot_payload(payload: dict) -> BotConfig:
    """Validate a raw dict via BotConfig. Accepts both
    {"bot": {...}} and {...} at the top level so the portal stays
    flexible. Raises ValueError on invalid config."""
    inner = payload.get("bot", payload)
    try:
        return BotConfig(**inner)
    except Exception as e:
        raise ValueError(str(e)) from e


# Bot CRUD routes: moved to web/routes/bots.py.


# ── WebSocket log streaming ───────────────────────────────────────────────────

class LogBroadcaster:
    """Fan bot-log lines to every subscribed WS client.

    Audit v26-16: delivery is filtered by ``owner_user_id`` so a
    broadcast for bot A of user 1 never reaches a WS client that
    connected as user 2. Subscribe-side already enforces ownership
    via ``registry.get(user_id, slug)``, but the broadcast layer
    itself carries the check too — defence in depth for any future
    code path that bypasses subscribe (e.g. infra-triggered
    broadcasts on the "portal" slug, which are fanned to every
    admin via ``get_admin_user_ids()``).
    """

    def __init__(self):
        self._clients: dict[str, set[WebSocket]] = {}
        # Per-socket user_id so broadcast() can filter without going
        # back to the DB on every frame.
        self._user_map: dict[WebSocket, int] = {}
        # asyncio.Lock — essential once uvicorn gains multiple workers
        # or more than one coroutine concurrently connects/disconnects/
        # broadcasts. Under the current single-worker setup the path
        # is safe via the event loop, but the lock makes it
        # future-proof.
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, slug: str, user_id: int):
        await ws.accept()
        async with self._lock:
            self._clients.setdefault(slug, set()).add(ws)
            self._user_map[ws] = int(user_id)

    async def disconnect(self, ws: WebSocket, slug: str):
        async with self._lock:
            if slug in self._clients:
                self._clients[slug].discard(ws)
            self._user_map.pop(ws, None)

    async def broadcast(self, slug: str, line: str, owner_user_id: int):
        """Send ``line`` to clients on ``slug`` whose user_id matches
        ``owner_user_id``. A socket on the same slug owned by a
        different user is silently skipped — the check is O(clients
        on slug), not O(all clients), so cost scales with the group
        size that actually cares about this slug.
        """
        async with self._lock:
            targets = [
                ws for ws in self._clients.get(slug, set())
                if self._user_map.get(ws) == owner_user_id
            ]
        dead: set[WebSocket] = set()
        for ws in targets:
            try:
                await ws.send_text(line)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                if slug in self._clients:
                    self._clients[slug] -= dead
                for ws in dead:
                    self._user_map.pop(ws, None)


broadcaster = LogBroadcaster()


@app.websocket("/ws/logs/{slug}")
async def ws_logs(websocket: WebSocket, slug: str):
    # WebSocket auth — BaseHTTPMiddleware doesn't run on WS, so we check
    # the session cookie here. The legacy `?api_key=` query param fallback
    # was removed: query strings end up in proxy / access logs and browser
    # history, which leaked the API key. Browsers always send the session
    # cookie on a same-origin WS upgrade, so this is no regression for the
    # SPA. Reject before accept() so unauthenticated clients never see logs.
    user_id = _ws_extract_user_id(websocket)
    if user_id is None:
        await websocket.close(code=4401)
        return

    # Special slug "portal" streams the portal's own log. portal.log
    # can surface cross-user admin actions (failed logins, admin route
    # hits, audit events) — audit v26-16 requires admin role to
    # subscribe. Non-admins get close 4403 before accept().
    if slug == "portal":
        user = user_store.get_user_by_id(user_id)
        if user is None or user.role != "admin":
            await websocket.close(code=4403)
            return
        portal_log = LOG_DIR / "portal.log"
        await broadcaster.connect(websocket, "portal", user_id)
        try:
            if portal_log.exists():
                lines = portal_log.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in lines[-100:]:
                    await websocket.send_text(line)
            while True:
                await asyncio.sleep(30)
                await websocket.send_text("__ping__")
        except WebSocketDisconnect:
            await broadcaster.disconnect(websocket, "portal")
        except Exception:
            await broadcaster.disconnect(websocket, "portal")
        return

    # Reject unknown slugs before accepting the socket — prevents tailing
    # arbitrary file paths and keeps broadcaster keys bounded. Scope to
    # the caller's own user_id so bot A of user 1 can never surface as
    # bot A of user 2 through this endpoint (audit v25 Phase-2 follow-up).
    bot = await registry.get(user_id, slug)
    if bot is None:
        await websocket.close(code=4004)
        return

    await broadcaster.connect(websocket, slug, user_id)
    try:
        if bot and bot.log_file.exists():
            lines = bot.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-150:]:
                await websocket.send_text(line)
        while True:
            await asyncio.sleep(30)
            await websocket.send_text("__ping__")
    except WebSocketDisconnect:
        await broadcaster.disconnect(websocket, slug)
    except Exception:
        await broadcaster.disconnect(websocket, slug)


class StateBroadcaster:
    """Push bot-state updates to connected /ws/state clients.

    Audit v26-16: delivery is filtered by ``target_user_id`` so a
    bot-state frame for user 1 never lands on a client that connected
    as user 2. The initial-snapshot path in ``ws_state`` is already
    user-scoped via ``registry.all(user_id=...)``; this lock closes
    the loop for the periodic push from ``watch_state_files``.
    Summary frames are per-user (one payload per owner in the
    watcher), so each client only sees aggregates over their own
    bots.
    """

    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._user_map: dict[WebSocket, int] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, user_id: int) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            self._user_map[ws] = int(user_id)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            self._user_map.pop(ws, None)

    async def broadcast(self, payload: str, target_user_id: int) -> None:
        """Send ``payload`` to clients whose user_id matches
        ``target_user_id``. The caller decides whose view this frame
        represents — typically ``bot.user_id`` for bot-state frames
        and the subscriber's own id for per-user summary frames.
        """
        async with self._lock:
            targets = [
                ws for ws in self._clients
                if self._user_map.get(ws) == target_user_id
            ]
        stale: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        if stale:
            async with self._lock:
                for ws in stale:
                    self._clients.discard(ws)
                    self._user_map.pop(ws, None)


state_broadcaster = StateBroadcaster()

# Module-level cache of last-seen mtimes. Audit r1-041: key is
# ``(user_id, slug)`` so that two users with the same slug name
# don't pollute each other — otherwise user A's mtime update would
# block user B's change detection. Reset on portal restart, which
# is fine: the watcher then just sends one extra broadcast on the
# first iteration.
_state_mtimes: dict[tuple[int, str], float] = {}


async def watch_state_files():
    """Poll logs/*.state.json mtimes every 2s and push changes to
    /ws/state clients. Also broadcasts the computed summary every cycle
    so the SPA's overview cards stay in sync with bot life-cycle events
    (a bot starting/stopping doesn't touch state.json, but the PID
    liveness flag flips).

    Delivery is per-owner (audit v26-16): bot-state frames go to
    clients whose user_id matches ``bot.user_id``, and summary frames
    are computed per-user so each client sees aggregates over their
    own bots only.
    """
    while True:
        try:
            # Infrastructure task: scan ALL bots across ALL users.
            # Per-user filtering happens at broadcaster-delivery time
            # via target_user_id — this cross-user scan is correct.
            bots = await registry.all()
            for bot in bots:
                try:
                    sf = bot.state_file
                    if not sf.exists():
                        continue
                    mtime = sf.stat().st_mtime
                    key = (bot.user_id, bot.slug)
                    if _state_mtimes.get(key) == mtime:
                        continue
                    _state_mtimes[key] = mtime
                    # r1-067: bot.read_state() does blocking file I/O
                    # (open + read + json.loads + a possible
                    # silent-exit-reconcile state-file rewrite). Pre-fix
                    # this stalled the asyncio loop for ~ms per bot,
                    # which on N bots delays every other coroutine
                    # — including the broadcaster fan-out for the
                    # PREVIOUS bot in this same loop. asyncio.to_thread
                    # pushes the I/O onto the default thread pool so
                    # the loop stays responsive.
                    state = await asyncio.to_thread(bot.read_state)
                    payload = json.dumps({
                        "type": "bot_state",
                        "slug": bot.slug,
                        "data": state,
                    })
                    await state_broadcaster.broadcast(
                        payload, target_user_id=bot.user_id,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("watch_state_files: bot %s failed: %s", bot.slug, e)
                    continue

            # Always broadcast summary — cheap and keeps the overview
            # cards honest even when no state.json file changed (PID
            # liveness flips as bots start/stop without touching JSON).
            # Compute one summary per owner so each user's cards
            # reflect only their own bots.
            try:
                bots_by_user: dict[int, list] = {}
                for b in bots:
                    bots_by_user.setdefault(b.user_id, []).append(b)
                for uid, user_bots in bots_by_user.items():
                    # r1-067 (continued): the per-user summary
                    # computation does N more reads per cycle. Run them
                    # concurrently in the thread pool so a slow file
                    # system on one bot can't head-of-line the rest of
                    # this user's summary. Serial-in-thread would be
                    # equally non-blocking for the loop, but gather
                    # halves the wall-clock for the summary step at
                    # the cost of ~N pool slots per cycle — acceptable
                    # since reads are short-lived.
                    snapshot = await asyncio.gather(
                        *[asyncio.to_thread(b.read_state) for b in user_bots]
                    )
                    summary_payload = json.dumps({
                        "type": "summary",
                        "data": _compute_summary(list(snapshot)),
                    })
                    await state_broadcaster.broadcast(
                        summary_payload, target_user_id=uid,
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug("watch_state_files: summary failed: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("watch_state_files iteration error: %s", e)

        await asyncio.sleep(2.0)


@app.websocket("/ws/state")
async def ws_state(websocket: WebSocket):
    # Session cookie gate — mirrors ws_logs. The legacy ?api_key=
    # query-string fallback was intentionally dropped portal-wide.
    # _ws_extract_user_id is the WS-equivalent of Depends(_request_user);
    # we use its user_id to scope the initial-snapshot registry call so
    # one user's client never receives another user's bot state.
    user_id = _ws_extract_user_id(websocket)
    if user_id is None:
        await websocket.close(code=4401)
        return
    await state_broadcaster.connect(websocket, user_id)
    try:
        # Initial snapshot so the SPA can render without waiting for a
        # file-change event.
        bots = await registry.all(user_id=user_id)
        snapshot: list[dict] = []
        for bot in bots:
            try:
                state = bot.read_state()
            except Exception as e:
                logger.debug(
                    "ws_state: read_state failed for bot %s: %s",
                    bot.slug, e,
                )
                continue
            snapshot.append(state)
            try:
                await websocket.send_text(json.dumps({
                    "type": "bot_state",
                    "slug": bot.slug,
                    "data": state,
                }))
            except Exception as e:
                logger.debug("ws_state: initial send failed: %s", e)
        try:
            await websocket.send_text(json.dumps({
                "type": "summary",
                "data": _compute_summary(snapshot),
            }))
        except Exception as e:
            logger.debug("ws_state: summary send failed: %s", e)

        # Keep the socket alive — broadcasts come from watch_state_files.
        while True:
            await asyncio.sleep(30)
            await websocket.send_text("__ping__")
    except WebSocketDisconnect:
        pass  # Expected lifecycle — client disconnected cleanly.
    except Exception as e:
        logger.debug("ws_state: loop exited with %s", e)
    finally:
        await state_broadcaster.disconnect(websocket)


async def tail_logs():
    last: dict[str, int] = {}
    while True:
        # Infrastructure task: tail ALL bot logs across ALL users.
        # Per-user delivery happens inside LogBroadcaster.broadcast
        # via owner_user_id — this scan is cross-user by design so a
        # single tail cursor per file handles every tenant.
        all_bots = await registry.all()
        # (slug, log_file, owner_user_id) — owner_user_id is None for
        # the "portal" pseudo-slug so we can fan it to every admin
        # after the size-probe below without re-reading the users
        # table when no new lines have appeared.
        targets: list[tuple[str, Path, Optional[int]]] = [
            (bot.slug, bot.log_file, bot.user_id) for bot in all_bots
        ]
        targets.append(("portal", LOG_DIR / "portal.log", None))

        for slug, log_file, owner_user_id in targets:
            try:
                if not log_file.exists():
                    continue
                size = log_file.stat().st_size
                prev = last.get(slug, 0)
                if size > prev:
                    with open(log_file, encoding="utf-8", errors="replace") as f:
                        f.seek(prev)
                        new = f.read()
                    last[slug] = size
                    new_lines = [
                        line for line in new.splitlines() if line.strip()
                    ]
                    if not new_lines:
                        continue
                    if slug == "portal":
                        # System-wide log — fan to every admin client.
                        # Lookup lives inside the "lines appeared"
                        # branch so a quiet portal.log doesn't hammer
                        # the users table every second.
                        admin_ids = user_store.get_admin_user_ids()
                        for line in new_lines:
                            for admin_id in admin_ids:
                                await broadcaster.broadcast(
                                    slug, line, owner_user_id=admin_id,
                                )
                    else:
                        for line in new_lines:
                            await broadcaster.broadcast(
                                slug, line, owner_user_id=owner_user_id,
                            )
                elif size < prev:
                    last[slug] = size
            except Exception as e:
                logger.debug(
                    "tail_logs: scan failed for %s: %s", slug, e,
                )
        await asyncio.sleep(1)


# ── SQLite ledger endpoints ───────────────────────────────────────────────────
# Reads are public (historical data the operator already owns). Writes use the
# existing API key dependency + rate limiter.


# Deal + annotation routes: moved to web/routes/deals.py.


# Backtest persistence routes: moved to web/routes/backtest.py.


# ── Sub-routers ─────────────────────────────────────────────────────────────
# Routes extracted from this file into web/routes/*.py. Included at the
# bottom so the module-level names the routers import (limiter,
# _request_actor, _audit, registry, stop_bot, _BOT_SLUG_RE,
# _check_db_sync_blocking) are all defined by the
# time the import-machinery follows those imports.
#
# Additional domains (auth, bots, deals, chart, exchanges, backtest)
# still live in this file; a follow-up pass can migrate them using the
# same pattern.
from web.routes import admin as _admin_routes  # noqa: E402
from web.routes import admin_bots as _admin_bots_routes  # noqa: E402
from web.routes import admin_findings as _admin_findings_routes  # noqa: E402
from web.routes import auth as _auth_routes  # noqa: E402
from web.routes import backtest as _backtest_routes  # noqa: E402
from web.routes import bots as _bots_routes  # noqa: E402
from web.routes import changelog as _changelog_routes  # noqa: E402
from web.routes import chart as _chart_routes  # noqa: E402
from web.routes import dashboard as _dashboard_routes  # noqa: E402
from web.routes import deals as _deals_routes  # noqa: E402
from web.routes import drawdown as _drawdown_routes  # noqa: E402
from web.routes import exchanges as _exchanges_routes  # noqa: E402
from web.routes import marketing as _marketing_routes  # noqa: E402
from web.routes import portfolio as _portfolio_routes  # noqa: E402
from web.routes import roadmap as _roadmap_routes  # noqa: E402
from web.routes import telegram as _telegram_routes  # noqa: E402
from web.routes import telegram_webhook as _telegram_webhook_routes  # noqa: E402

app.include_router(_admin_routes.router)
app.include_router(_admin_bots_routes.router)
app.include_router(_admin_findings_routes.router)
app.include_router(_auth_routes.router)
app.include_router(_backtest_routes.router)
app.include_router(_bots_routes.router)
app.include_router(_changelog_routes.router)
app.include_router(_chart_routes.router)
app.include_router(_dashboard_routes.router)
app.include_router(_deals_routes.router)
app.include_router(_drawdown_routes.router)
app.include_router(_exchanges_routes.router)
app.include_router(_marketing_routes.router)
app.include_router(_portfolio_routes.router)
app.include_router(_roadmap_routes.router)
app.include_router(_telegram_routes.router)
app.include_router(_telegram_webhook_routes.router)


def run_portal(host="0.0.0.0", port=8080):
    """Start the uvicorn server with explicit Config + Server
    instantiation.

    Why not uvicorn.run()? The convenience wrapper has caused
    SIGTERM-handling to silently fail in our setup — signals never
    triggered lifespan shutdown, forcing stop.sh to fall back on
    SIGKILL every make restart. The explicit Server instance pattern
    installs signal handlers deterministically and respects
    timeout_graceful_shutdown so lifespan's task-cancellation (2s
    budget) can complete before uvicorn exits.
    """
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        # Must be >= lifespan's internal 2s cancellation timeout so
        # task-shutdown has room; 5s matches stop.sh's grace period
        # ceiling, so uvicorn will never linger longer than the shell
        # wrapper allows.
        timeout_graceful_shutdown=5,
        # Audit r3-003: suppress the ``Server: uvicorn`` response
        # header at the protocol layer. uvicorn injects this header
        # in its H11 serialisation (AFTER Starlette's middleware
        # chain), so a `del response.headers["server"]` in
        # SecurityHeadersMiddleware would have no effect. The
        # ``server_header=False`` kwarg is the only place that
        # actually keeps the fingerprint off the wire.
        server_header=False,
    )
    server = uvicorn.Server(config)
    server.run()

if __name__ == "__main__":
    run_portal()
