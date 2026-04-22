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

from config.config_loader import load_bot_config
from config.models import BotConfig, Mode
from core import paths, user_store
from core.database import DatabaseMigrationError, init_db as _init_db
from core.ids import DEAL_ID_RE
from core.user import User, get_default_user
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

# Maximum file size for state.json — voorkomt OOM bij corrupte/oversize files
_MAX_STATE_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Extra slack op top van de engine's notify-drain budget voor
# process-startup/teardown overhead tussen engine.stop() returning en
# de PID die van de procestabel verdwijnt. Portal-stop wait-deadline
# = NOTIFY_DRAIN_TIMEOUT_S + _STOP_SAFETY_MARGIN_S, zodat elke
# verhoging van de drain-budget automatisch doorwerkt in de portal.
_STOP_SAFETY_MARGIN_S = 3.0


class BotStateModel(BaseModel):
    """Pydantic schema voor logs/{slug}.state.json — beschermt tegen
    corrupte of geïnjecteerde JSON met onverwachte types of waarden.
    Extra velden worden genegeerd (niet gestript) zodat toekomstige
    velden niet crashen op oude portal versies."""

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
        # Last-resort fallback: if we genuinely can't write the file
        # the operator still needs the key, so log it. Should never
        # hit in practice — logs/ is writable on every supported host.
        logger.error(
            "REVERTO_API_KEY not set and could not write %s (%s). "
            "Ephemeral key (will be lost on restart): %s",
            _EPHEMERAL_API_KEY_FILE, e, _API_KEY,
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

    Audit v26-05: de pre-Phase-3a signature accepteerde ook een
    username-string met een fallback die uid=-1 mintte als de
    username niet resolvde. Die fallback was onbereikbaar — de login-
    flow passeert altijd een al-geresolvede User uit
    ``verify_password`` — dus de branch is nu weg. Tests die voorheen
    ``_create_session_cookie("admin")`` aanriepen moeten de admin-User
    zelf ophalen via ``user_store.get_user_by_username`` (of een
    test-helper daaromheen).
    """
    return _session_serializer.dumps({
        "uid": user.id,
        "u": user.username,
        "iat": int(time.time()),
        "ep": user_store.get_session_epoch(user.id),
    })


def _verify_session_cookie(token: Optional[str]) -> Optional[dict]:
    """Return the decoded payload when the cookie is valid, else None.

    Validation order:
      1. itsdangerous signature + TTL
      2. payload shape (dict, has 'uid' and 'u')
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
    if not isinstance(data, dict) or not data.get("u"):
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


def _require_session(request: Request) -> dict:
    """FastAPI dependency — reject if the caller has no valid session
    cookie OR if the backing user has been deactivated.

    Audit v26-01 MEDIUM: pre-fix this helper only checked the
    itsdangerous signature + TTL + session_epoch on the cookie. A
    user flipped to ``active = 0`` still had a cookie that passed
    those checks — the only way to lock them out was to wait for
    TTL expiry or bump their session_epoch. ``_request_user`` (the
    dependency used by almost every other route) already does the
    active-check, so this was a parity gap: one endpoint
    (``/api/auth/change-password`` at time of fix) would happily
    serve a deactivated account.

    Post-fix: same 401 + "User not found" response shape as
    ``_request_user`` on the inactive-user path, so clients see
    identical UI behaviour regardless of which helper a given
    endpoint happens to use.
    """
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    uid = payload.get("uid")
    if not isinstance(uid, int) or uid <= 0:
        raise HTTPException(status_code=401, detail="Invalid session")
    user = user_store.get_user_by_id(uid)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="User not found")
    return payload


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
    if payload and payload.get("u"):
        return f"session:{payload['u']}"
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

    API-key callers (``X-API-Key``) bypass the session cookie and get
    the Phase-1 admin stub — they're server-to-server traffic and
    don't carry a user context. Routes that care about the real
    caller must enforce cookie-auth explicitly.
    """
    cookie = request.cookies.get(_SESSION_COOKIE)
    payload = _verify_session_cookie(cookie)
    if payload is None:
        # No valid cookie — check for API-key. If that's the auth path,
        # fall back to the admin stub (matches the pre-3a behaviour
        # for script/CI traffic). If neither, the AuthMiddleware has
        # already short-circuited with 401 so this code path is only
        # reachable via an explicitly-exempted route — in that case
        # default to admin so audit trails stay consistent.
        provided = request.headers.get("X-API-Key")
        if provided and secrets.compare_digest(provided, _API_KEY):
            return get_default_user()
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
# Aparte logger "reverto.audit" → logs/audit.log met rotation. Propagate=False
# zodat audit events niet ook nog in portal.log belanden. Format:
#     2026-04-15T12:34:56+0000 | bot_start | btc_paper | a1b2c3d4
_audit_logger = logging.getLogger("reverto.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
if not _audit_logger.handlers:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _audit_handler = RotatingFileHandler(
        LOG_DIR / "audit.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _audit_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    )
    _audit_logger.addHandler(_audit_handler)


def _audit(action: str, slug: str = "-", key_hint: str = "-") -> None:
    """Schrijf één regel naar de audit log."""
    _audit_logger.info("%s | %s | %s", action, slug, key_hint)


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


_SLUG_RE = re.compile(r"[^a-z0-9_]+")
# Re-exported from core.ids so the engine, the web routes, and the
# route-level validators all agree on one canonical shape for
# YYYYMMDDHHMM-RRRR deal ids. Kept as the underscore-prefixed alias
# so existing imports from web/routes/deals.py keep working.
_DEAL_ID_RE = DEAL_ID_RE

# Validator for slugs that come straight off the URL — the slugify()
# helper above cleans wizard input, but path-parameter slugs must be
# checked before they hit Path() construction to block `../` escapes.
_BOT_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def slugify(name: str) -> str:
    """Zet een vrije bot-naam om naar een veilig filename stem.

    Lowercase, spaties → underscore, alles buiten [a-z0-9_] gestript,
    meervoudige underscores worden samengetrokken. Lege resultaten
    raise ValueError zodat de caller een 400 kan returnen.
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

    def read_state(self) -> dict:
        yaml_mode = self._resolve_yaml_mode()
        try:
            # Bounded read — lees maximaal _MAX_STATE_FILE_SIZE + 1 bytes in
            # één open()+read() zodat er geen TOCTOU gat is tussen een
            # aparte stat() en read_text(). Als er een byte extra binnenkomt
            # is de file groter dan toegestaan en vallen we terug op default.
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
            validated["running"]     = self.running
            validated["slug"]        = self.slug
            validated["config_file"] = self.config_file
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
# terug op integer-name-only matching — fail-open. In een multi-user
# Phase-3 omgeving kan één transient DB-glitch dan stil een orphan dir
# als valide tenant accepteren. Nu houden we de laatste bekend-goede
# users-set vast en hergebruiken die tot ``_MAX_STALE_REFRESHES`` achter
# elkaar falen; daarna returnt de scan leeg (fail-closed) met een
# ERROR in de log.
#
# _previously_logged_orphans voorkomt ook de Finding #7 log-spam:
# een orphan dir die bij elke 5-seconden scan opnieuw gelogd zou
# worden komt hier maar eenmaal doorheen totdat 'ie weer verschijnt.
_cached_active_users: set[int] | None = None
_db_failure_count: int = 0
_MAX_STALE_REFRESHES: int = 5  # ≈ 25 s bij de 5 s registry-refresh TTL
_previously_logged_orphans: set[Path] = set()
# TTL voor de DB-cache in _scan_user_dirs (audit v25 Finding #6). Zonder
# deze short-circuit deed elke 5 s scan een verse get_active_user_ids()
# call, terwijl de users-tabel in steady-state zelden verandert. 30 s
# is ruim binnen de refresh-cadans en scheelt ~6 DB-reads per minuut
# per portal. Een user-create/-delete endpoint kan
# ``_cache_last_refresh_ts = 0`` zetten om expliciet te invalideren
# (Phase-3 werk; nu pure-TTL).
_CACHE_TTL_S: float = 30.0
_cache_last_refresh_ts: float = 0.0


def _reset_user_dirs_cache() -> None:
    """Reset de module-level fail-closed state. Alleen voor gebruik
    door tests — productie-code raakt deze globals niet direct aan.
    """
    global _cached_active_users, _db_failure_count
    global _previously_logged_orphans, _cache_last_refresh_ts
    _cached_active_users = None
    _db_failure_count = 0
    _previously_logged_orphans = set()
    _cache_last_refresh_ts = 0.0


def _scan_active_dirs(active: set[int]) -> list[tuple[int, Path]]:
    """Match ``config/bots/<int>/`` directories tegen een vertrouwde
    active-set. Gedeeld tussen de cache-hit en cache-miss paden van
    ``BotRegistry._scan_user_dirs`` — een orphan-log-dedup is daarom
    onafhankelijk van of de DB dit tick geraadpleegd werd.

    ``active`` is áltijd pas gevalideerd door de caller (DB live óf
    last-known-good cache); deze helper doet geen DB-call.
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
    # Dedup-baseline voor de volgende scan. Orphans die verdwenen zijn
    # (operator ruimt op) vallen uit de set, en als ze ooit terugkomen
    # worden ze opnieuw gelogd.
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

    # TTL voor de filesystem-glob in refresh(). Bij hoge API frequentie
    # (dashboard polls elke 5s, plus /api/price, plus tail_logs) voerde
    # iedere call een eigen glob uit — overbodig en duur op trage
    # filesystems (NFS/SMB). 5s is ruim binnen de UI-refresh cadans.
    _REFRESH_TTL = 5.0

    def __init__(self):
        self._bots: dict[tuple[int, str], BotInfo] = {}
        self._lock = asyncio.Lock()
        self._last_refresh: float = 0.0
        # In-progress starts, keyed on (user_id, slug) so a dubble-klik
        # for user 1/rsi_test and user 2/rsi_test don't block each other.
        self._starting: set[tuple[int, str]] = set()
        # Initiële populatie: gebeurt vóór de event loop bestaat, dus
        # geen lock-contention mogelijk — direct synchroon vullen.
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
        ``get_active_user_ids()`` raises, we reuse the last bekend-
        goede set for up to ``_MAX_STALE_REFRESHES`` cycles; after
        that we return an empty list and log ERROR. A single transient
        glitch therefore never surfaces an orphan as a tenant.

        Happy-path cache (audit v25 Finding #6): within ``_CACHE_TTL_S``
        of a successful DB-call we reuse ``_cached_active_users``
        without touching the DB at all. Skips ~6 queries/minute per
        portal when the users-table is steady.
        """
        global _cached_active_users, _db_failure_count
        global _cache_last_refresh_ts

        now = time.time()
        # Cache-hit pad: cache vers genoeg → skip de DB-call en meteen
        # door naar de directory-scan. De DB-failure counter raken we
        # niet aan; een volgende cache-miss (na TTL-expiry) herleeft
        # de happy/failure-split normaal.
        if (
            _cached_active_users is not None
            and now - _cache_last_refresh_ts < _CACHE_TTL_S
        ):
            return _scan_active_dirs(_cached_active_users)

        # Cross-check tegen de users tabel. Importeer hier binnen de
        # functie zodat circular imports niet optreden als core.user
        # ooit bij init naar web.app zou willen kijken. De DB-query
        # komt vóór de CONFIG_DIR.exists() short-circuit zodat de
        # cache-invariant (ververst bij elke scan) onafhankelijk is
        # van of er al bot-yamls op disk staan — een fresh install
        # zonder yamls populeert alsnog de cache zodat latere failures
        # niet direct fail-closed gaan.
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
        """Voer de glob uit; caller moet de lock vasthouden (of init zijn)."""
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

async def start_bot(user_id: int, slug: str) -> dict:
    bot = await registry.get(user_id, slug)
    if not bot:
        return {"ok": False, "error": f"Unknown bot: {slug}"}
    if bot.running:
        return {"ok": False, "error": f"{slug} already running (PID {bot.pid})"}

    # Claim de start-slot. Voorkomt dat een dubbel-klik (beide calls zien
    # bot.running=False omdat main_paper.py nog niet is opgestart) twee
    # subprocessen spawnt.
    if not await registry.begin_start(user_id, slug):
        return {"ok": False, "error": "Bot is already starting"}

    try:
        paths.user_pid_dir(user_id)

        # Use absolute path to main_paper.py and same venv Python as portal
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BASE_DIR)

        # Context manager closes the parent's FD after Popen duplicates it —
        # the child process keeps its own handle, no FD leak in the portal.
        with open(bot.log_file, "a") as log_out:
            proc = subprocess.Popen(
                [PYTHON_BIN, str(BASE_DIR / "main_paper.py"),
                 "--bot", slug, "--user-id", str(user_id)],
                cwd=str(BASE_DIR),
                stdout=log_out,
                stderr=log_out,
                env=env,
                start_new_session=True
            )
        logger.info(f"Bot {slug} started (PID {proc.pid})")

        # Wacht maximaal 3s tot main_paper.py zijn eigen PID file heeft
        # geschreven. Zolang we die niet zien houdt de starting-slot stand
        # zodat een volgende klik netjes een "already starting" krijgt in
        # plaats van een tweede subprocess.
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

        env = os.environ.copy()
        env["PYTHONPATH"] = str(BASE_DIR)
        # main_live.py prompts the operator on non-dry-run launches and
        # also respects DRY_RUN=1 as a bypass — set it explicitly so a
        # non-TTY portal subprocess never hangs on input().
        env["DRY_RUN"] = "1"

        with open(bot.log_file, "a") as log_out:
            proc = subprocess.Popen(
                [PYTHON_BIN, str(BASE_DIR / "main_live.py"),
                 "--bot", slug, "--user-id", str(user_id), "--dry-run"],
                cwd=str(BASE_DIR),
                stdout=log_out,
                stderr=log_out,
                env=env,
                start_new_session=True,
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
    cfg = None
    try:
        cfg = load_bot_config(bot.config_file)
        notifier = TelegramNotifier(notify_on=cfg.telegram.notify_on)
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

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach standard hardening headers to every HTTP response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"]        = "no-referrer"
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
    "/auth/logout",
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


# Rate limiter — beperkt brute force en DoS op control endpoints. Sleutel
# per remote IP; in een setup achter een reverse proxy moet je X-Forwarded-For
# parsing toevoegen via een eigen key_func.
limiter = Limiter(key_func=get_remote_address)


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

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler.

    Startup: spawn background tasks that tail bot logs and watch
    state.json mtimes. References are stored so shutdown can cancel
    them cleanly.

    Shutdown: cancel background tasks and wait for them to return.
    Without this, uvicorn's graceful-shutdown path hangs waiting for
    asyncio.sleep() calls inside the tasks' while-True loops — which
    is what caused the SIGKILL fallback in stop.sh to fire on every
    make restart.
    """
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
    title="Reverto Portal", docs_url=None, redoc_url=None,
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
# Middleware order (Starlette wraps last-added = outermost):
#   add_middleware(AuthMiddleware)            → runs inner
#   add_middleware(SecurityHeadersMiddleware) → runs outer
# Request flow:  SecurityHeaders → Auth → route handler
# Response flow: route handler   → Auth → SecurityHeaders
# This lets 401/403 short-circuits from Auth still pass through
# SecurityHeaders on the way out, so *every* response — including
# authentication failures — carries CSP / X-Frame-Options / etc.
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
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
    """Single source of truth voor de dashboard summary-blok.

    Wordt gebruikt door zowel GET /api/bots als de /ws/state watcher,
    zodat het HTTP fallback-pad en de WS push exact dezelfde vorm
    hebben. closed_deals is geaggregeerd over alle bots zodat toekomstige
    WS-consumers het kunnen tonen zonder extra round-trip.
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
    """Valideer een rauwe dict via BotConfig. Accepteert zowel
    {"bot": {...}} als {...} aan top-level zodat de portal flexibel
    blijft. Raised ValueError bij invalide config."""
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
        # asyncio.Lock — essentieel zodra uvicorn meerdere workers krijgt
        # of meer dan één coroutine concurrent connect/disconnect/broadcast
        # uitvoert. Onder de huidige single-worker setup is het pad veilig
        # door de event loop, maar de lock maakt het future-proof.
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

# Module-level cache van laatst-geziene mtimes, per slug. Wordt door
# watch_state_files() gelezen/geüpdatet. Gereset op portal restart,
# wat prima is: de watcher stuurt in dat geval gewoon één extra
# broadcast bij de eerste iteratie.
_state_mtimes: dict[str, float] = {}


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
                    if _state_mtimes.get(bot.slug) == mtime:
                        continue
                    _state_mtimes[bot.slug] = mtime
                    state = bot.read_state()
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
                    snapshot = [b.read_state() for b in user_bots]
                    summary_payload = json.dumps({
                        "type": "summary",
                        "data": _compute_summary(snapshot),
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
            except Exception:
                continue
            snapshot.append(state)
            try:
                await websocket.send_text(json.dumps({
                    "type": "bot_state",
                    "slug": bot.slug,
                    "data": state,
                }))
            except Exception:
                pass
        try:
            await websocket.send_text(json.dumps({
                "type": "summary",
                "data": _compute_summary(snapshot),
            }))
        except Exception:
            pass

        # Keep the socket alive — broadcasts come from watch_state_files.
        while True:
            await asyncio.sleep(30)
            await websocket.send_text("__ping__")
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
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
            except Exception:
                pass
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
from web.routes import auth as _auth_routes  # noqa: E402
from web.routes import backtest as _backtest_routes  # noqa: E402
from web.routes import bots as _bots_routes  # noqa: E402
from web.routes import changelog as _changelog_routes  # noqa: E402
from web.routes import chart as _chart_routes  # noqa: E402
from web.routes import deals as _deals_routes  # noqa: E402
from web.routes import drawdown as _drawdown_routes  # noqa: E402
from web.routes import exchanges as _exchanges_routes  # noqa: E402

app.include_router(_admin_routes.router)
app.include_router(_admin_bots_routes.router)
app.include_router(_auth_routes.router)
app.include_router(_backtest_routes.router)
app.include_router(_bots_routes.router)
app.include_router(_changelog_routes.router)
app.include_router(_chart_routes.router)
app.include_router(_deals_routes.router)
app.include_router(_drawdown_routes.router)
app.include_router(_exchanges_routes.router)


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
    )
    server = uvicorn.Server(config)
    server.run()

if __name__ == "__main__":
    run_portal()
