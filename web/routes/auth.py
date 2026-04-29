"""Auth routes extracted from web/app.py.

Routes:
  POST /auth/login                 — bcrypt-checked login, sets session cookie
  POST /auth/logout                — bumps per-user session epoch + clears cookie
  GET  /auth/status                — returns auth state (no auth required)
  POST /api/auth/change-password   — rotates password + session epoch
  POST /auth/totp/setup            — start TOTP enrollment (Phase B PR 2)
  POST /auth/totp/verify           — verify code + commit TOTP seed
  POST /auth/totp/disable          — disable TOTP (password + code required)

Phase-3a: every auth-state read/write goes via ``core.user_store``
(DB-backed). The .auth.json blob is gone — admin password is
provisioned via ``scripts/setup_admin.py`` post-migration.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import UTC, datetime

import qrcode
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from qrcode.image.svg import SvgPathImage
from slowapi.util import get_remote_address

from core import totp, user_store
from core.password_breach import is_password_pwned
from core.user import User
from core.user_store import FAILED_LOGIN_WINDOW_S, PASSWORD_MIN_LENGTH
from web import app as _webapp
from web.app import (
    _audit,
    _clear_pending_login_totp_cookie,
    _clear_pending_totp_cookie,
    _create_session_cookie,
    _read_pending_login_totp_cookie,
    _read_pending_totp_cookie,
    _request_actor,
    _request_user,
    _SESSION_COOKIE,
    _SESSION_TTL,
    _set_pending_login_totp_cookie,
    _set_pending_totp_cookie,
    _verify_session_cookie,
    limiter,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


# ── Login-security-hardening constants ──────────────────────────────────
# Exponential backoff schedule: delay = min(0.1 * 2^count, 30.0) seconds.
# A typo (count=0) pays 0.1s — same as the pre-hardening damping sleep,
# so legitimate users see no regression. A campaign of 5 wrong tries
# pays 3.2s on the 6th, 10 wrong tries pays 30s thereafter, rate-limit
# then blocks everything for the rest of the hour.
_BACKOFF_BASE_S = 0.1
_BACKOFF_CAP_S = 30.0

# Phase B PR 4: the per-account rate-limit threshold lives in
# ``core.user_store`` now (single source of truth — the
# ``check_login_rate_limit`` helper there owns the check, the
# 429-return shape, AND the sliding-window reset semantics inside
# ``increment_failed_login``). The limit is 10 failed attempts inside
# a ``FAILED_LOGIN_WINDOW_S = 900`` (15 min) window. Re-exported here
# for back-compat with the in-memory unknown-IP fallback below — that
# path still uses a count-based gate against the same ceiling.
_PER_ACCOUNT_FAIL_LIMIT = user_store.PER_ACCOUNT_FAIL_LIMIT

# Anomaly-log trigger: every N failures write a ``suspicious_login_pattern``
# line to audit.log so an operator scanning the log spots brute-force
# campaigns without needing to query the DB.
_ANOMALY_LOG_EVERY_N = 5

# In-memory fallback for failed logins against UNKNOWN usernames. Bot
# traffic spraying random usernames shouldn't write DB rows (each
# unknown attempt would otherwise require a sentinel row or a
# dedicated side-table). Keyed by source IP; resets on portal restart.
# Cap enforced to prevent counter-inflation DoS where an attacker
# cycles source IPs to pollute the dict unboundedly.
_UNKNOWN_USER_IP_CAP = 10_000
# Maps ``client_ip -> (count, last_failure_unix_ts)``.
_unknown_user_fails: dict[str, tuple[int, float]] = {}


def _unknown_user_fail_get(ip: str) -> int:
    """Return the current sliding-window failure count for an IP whose
    attempts target usernames that don't exist. Stale entries (>1h
    since last hit) count as zero, matching the per-account sliding
    window."""
    entry = _unknown_user_fails.get(ip)
    if entry is None:
        return 0
    count, last_ts = entry
    if time.time() - last_ts > FAILED_LOGIN_WINDOW_S:
        return 0
    return count


def _unknown_user_fail_bump(ip: str) -> int:
    """Increment the per-IP failure counter for unknown-username
    traffic. Sliding window + IP-cap trimming are both applied here
    so the handler has a single "fail is recorded" hook."""
    now = time.time()
    entry = _unknown_user_fails.get(ip)
    if entry is None:
        new_count = 1
    else:
        count, last_ts = entry
        if now - last_ts > FAILED_LOGIN_WINDOW_S:
            new_count = 1
        else:
            new_count = count + 1
    # Cap the dict size by evicting the oldest entry when over budget.
    # O(n) scan is fine — the cap is small relative to an actual
    # attack's cardinality, and eviction fires rarely in practice.
    if len(_unknown_user_fails) >= _UNKNOWN_USER_IP_CAP:
        oldest_ip = min(
            _unknown_user_fails,
            key=lambda k: _unknown_user_fails[k][1],
        )
        _unknown_user_fails.pop(oldest_ip, None)
    _unknown_user_fails[ip] = (new_count, now)
    return new_count


def _effective_count_from_state(
    count: int, last_at: datetime | None,
) -> int:
    """Apply the 1-hour sliding window to a raw (count, last_at) pair
    returned by ``user_store.get_failed_login_state``. The raw counter
    can carry stale failures from weeks ago; this helper returns the
    value that matters for backoff + rate-limit decisions right now."""
    if last_at is None:
        return 0
    if (datetime.now(UTC) - last_at).total_seconds() > FAILED_LOGIN_WINDOW_S:
        return 0
    return count


def _compute_backoff_s(pre_count: int) -> float:
    """Deterministic backoff delay for a given pre-attempt failure
    count. ``min(...)`` on the exponent guards against float overflow
    on pathological counts; ``min(..., CAP)`` bounds the wall time."""
    safe_exp = min(pre_count, 20)
    return min(_BACKOFF_BASE_S * (2 ** safe_exp), _BACKOFF_CAP_S)


def _round_retry_after_to_minute(seconds: int) -> int:
    """Round a retry-after value up to the next 60-second boundary.

    Audit pt-160 (PT-v3, INFO × MEDIUM): the known-user 429 path
    carried a timestamp-precise retry-after (e.g. 873 s, computed
    from the stored ``last_failed_login_at``) while the unknown-
    user path carried the full-window worst-case (900 s). An
    attacker comparing two 429 responses could distinguish "this
    username is rate-limited" from "this username doesn't exist
    BUT happens to have hit the per-IP limit" via the precision of
    the Retry-After header. Rounding both up to the next 60 s
    boundary collapses the precision channel without making the
    user wait longer than they would have anyway.

    Round UP, never down — rounding 1 s down to 0 would tell the
    SPA "retry now", which would re-trigger the 429 immediately.
    Edge cases: a value that's already on a 60 s boundary stays
    there; a value of 0 returns 0.
    """
    if seconds <= 0:
        return 0
    return ((seconds + 59) // 60) * 60


def _rate_limit_detail(retry_after_s: int) -> str:
    """User-facing detail-text for a 429 response. Same shape on the
    known-user path (precise retry-after computed from the stored
    timestamp) and the unknown-user path (worst-case = full window)
    so an attacker can't user-enumerate by reading the precision of
    the message — see ``check_login_rate_limit``'s docstring on the
    intentional symmetry. Caller is expected to pass a value that
    has already been rounded by ``_round_retry_after_to_minute`` so
    the rendered text never carries sub-minute precision."""
    minutes, seconds = divmod(int(retry_after_s), 60)
    if minutes and seconds:
        wait = f"{minutes} minutes and {seconds} seconds"
    elif minutes:
        wait = f"{minutes} minutes"
    else:
        wait = f"{seconds} seconds"
    return (
        f"Too many failed login attempts. Please try again in {wait}."
    )


# Audit v27-09: defence-in-depth character-class restriction on the
# login boundary. Length bounds alone accept whitespace, control chars,
# emoji, and SQL-injection-shaped payloads — Pydantic + parameterised
# queries elsewhere already block real exploits, but the audit asks for
# the rejection at the boundary so malformed traffic never reaches the
# DB-lookup at all. Pattern accepts alphanumerics + the three separators
# any real-world username convention uses (underscore, dot, dash).
# ChangePasswordBody has no username field (uid is resolved from the
# session cookie via ``_request_user``) so no parallel pattern needed.
_USERNAME_PATTERN = r"^[a-zA-Z0-9_.-]+$"


class LoginBody(BaseModel):
    username: str = Field(
        min_length=1, max_length=64, pattern=_USERNAME_PATTERN,
    )
    password: str = Field(min_length=1, max_length=512)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class LoginTotpBody(BaseModel):
    """Phase B PR 3: body for /auth/login/totp — the second login
    step for users with TOTP enabled. ``code`` is constrained the
    same way as TotpVerifyBody (six digits exact) so an obviously
    malformed value is rejected by Pydantic with 422 before the
    handler runs."""

    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


@router.post("/auth/login")
@limiter.limit("5/minute")
async def auth_login(body: LoginBody, request: Request):
    """Username + password login with exponential backoff + per-account
    rate-limit + anomaly logging.

    Flow:
      1. Look up the user by username to pick the right counter
         (DB-backed per-user row, or per-IP in-memory fallback for
         unknown usernames so random-username bot traffic doesn't
         inflate the users table).
      2. Apply the 1h sliding window to the prior failure count.
      3. If at or over the per-account limit → 429 before verify.
      4. Verify password.
      5. On failure: increment the appropriate counter, fire an
         anomaly audit line every N failures, pay the backoff delay
         (identical timing for unknown-vs-known usernames to deny
         enumeration), and raise 401.
      6. On success: reset the per-account counter and return the
         signed session cookie.
    """
    user_record = user_store.get_user_by_username(body.username)
    client_ip = get_remote_address(request)

    # Load pre-attempt failure state for backoff + rate-limit.
    if user_record is not None:
        raw_count, last_at = user_store.get_failed_login_state(
            user_record.id,
        )
        pre_count = _effective_count_from_state(raw_count, last_at)
    else:
        pre_count = _unknown_user_fail_get(client_ip)

    # Phase B PR 4: per-account rate-limit gate. Runs BEFORE password
    # verification so an attacker can't amortise CPU by piling bcrypt
    # calls against a locked account, AND emits a Retry-After header
    # so a well-behaved client can pace its retries — the existing
    # detail-string pre-PR4 was operator-text only with no machine-
    # readable retry hint.
    if user_record is not None:
        is_limited, retry_after = user_store.check_login_rate_limit(
            user_record.id,
        )
        if is_limited:
            _audit(
                "login_rate_limit_hit",
                body.username,
                "-",
                user_id=user_record.id,
                request=request,
                result="denied",
            )
            # Audit pt-160: round Retry-After UP to the next 60 s
            # boundary so the precision of this header doesn't tell
            # an attacker "this username is rate-limited" vs "this
            # username doesn't exist" (the unknown-user path returns
            # the full window worst-case, also rounded).
            rounded = _round_retry_after_to_minute(retry_after)
            raise HTTPException(
                status_code=429,
                detail=_rate_limit_detail(rounded),
                headers={"Retry-After": str(rounded)},
            )
    elif pre_count >= _PER_ACCOUNT_FAIL_LIMIT:
        # Unknown-username path: per-IP in-memory counter. No
        # Retry-After here because the in-memory counter has no
        # timestamp granularity finer than the sliding window — and
        # surfacing one would be a user-enumeration tell ("known
        # user gets a precise hint, unknown doesn't"). Detail text
        # matches the known-user shape for the same reason.
        raise HTTPException(
            status_code=429,
            detail=_rate_limit_detail(
                _round_retry_after_to_minute(FAILED_LOGIN_WINDOW_S),
            ),
        )

    user = user_store.verify_password(body.username, body.password)
    if user is None:
        # Record the failure against whichever counter tracks this
        # username shape (real user row → DB; unknown → in-memory IP
        # fallback). Both paths return the new cumulative count used
        # for the anomaly-log trigger.
        if user_record is not None:
            new_count = user_store.increment_failed_login(user_record.id)
        else:
            new_count = _unknown_user_fail_bump(client_ip)

        if new_count > 0 and new_count % _ANOMALY_LOG_EVERY_N == 0:
            # user_record is None for unknown usernames — keep the
            # per-user split opt-out in that case (nothing to key by).
            _audit(
                "suspicious_login_pattern",
                body.username,
                f"count={new_count}",
                user_id=user_record.id if user_record is not None else None,
                request=request,
                result="denied",
            )

        # Backoff uses the PRE-attempt count so the first failure
        # still pays 0.1s (same damping as the pre-hardening code)
        # and subsequent failures escalate. Applying identical timing
        # regardless of user_record presence keeps the enumeration
        # defence intact.
        await asyncio.sleep(_compute_backoff_s(pre_count))
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Phase B PR 3 + PR 4: branch on totp_enabled. Users without 2FA
    # get the historical "set session cookie + return ok" response;
    # users with 2FA get a 2-min pending cookie + ``requires_totp:
    # True``, no session yet. PR 4 moved the failed-login counter
    # reset OUT of this point — pre-PR4 the reset fired immediately
    # after password-verify, which let a user who password-cracked
    # but failed TOTP reset their counter cheaply. Now the reset
    # only fires on FULL login success: in this handler for the
    # no-TOTP path, in /auth/login/totp for the TOTP path.
    if user.totp_enabled:
        resp = JSONResponse({"ok": True, "requires_totp": True})
        _set_pending_login_totp_cookie(resp, user.id)
        _audit(
            "login_password_ok_totp_required",
            user.username,
            "-",
            user_id=user.id,
            request=request,
        )
        return resp

    # No-TOTP path = full login complete. Reset counter + mint
    # session in one go.
    user_store.reset_failed_login(user.id)
    return _mint_session_response(user, request, audit_action="auth_login")


def _mint_session_response(
    user: "User",
    request: Request,
    *,
    audit_action: str,
) -> JSONResponse:
    """Mint the post-login session + CSRF cookies and return the
    standard JSON shape.

    Shared by ``/auth/login`` (no-TOTP path) and ``/auth/login/totp``
    (TOTP-verified path). Centralising the cookie-set + audit-emit
    here keeps the two paths from drifting on cookie flags or audit
    field shape — every successful login lands the same observable
    state regardless of whether 2FA was in play.
    """
    token = _create_session_cookie(user)
    # Audit r1-073: mint a CSRF token on successful login. Random
    # per-session URL-safe value; readable by JS so the SPA can
    # echo it in the X-CSRF-Token header on mutating requests.
    csrf_token = _webapp._mint_csrf_token()
    resp = JSONResponse({
        "ok": True,
        "csrf_token": csrf_token,
        "requires_totp": False,
    })
    # Look up cookie flags on the module at call-time (not at import)
    # so tests can override _COOKIE_SECURE / _COOKIE_SAMESITE on the
    # web.app module and have the change take effect without touching
    # this file's local bindings.
    resp.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_SESSION_TTL,
        httponly=True,
        samesite=_webapp._COOKIE_SAMESITE,
        secure=_webapp._COOKIE_SECURE,
        path="/",
    )
    # CSRF cookie via shared helper — keeps the flag set in sync
    # with the graceful-migration mint path in CSRFMiddleware so
    # the two mint sites can't drift.
    _webapp._set_csrf_cookie_on_response(resp, csrf_token)
    _audit(
        audit_action,
        user.username,
        "-",
        user_id=user.id,
        request=request,
    )
    return resp


@router.post("/auth/login/totp")
@limiter.limit("10/minute")
async def auth_login_totp(
    body: LoginTotpBody,
    request: Request,
):
    """Phase B PR 3: complete a 2FA login by verifying the TOTP code
    against the user staged by the prior /auth/login password step.

    Reads the pending-login-TOTP cookie minted by /auth/login when
    ``user.totp_enabled`` was True. Re-resolves the user from the DB
    (so a deactivation or TOTP-disable that happened between steps
    surfaces) and verifies the code against the stored encrypted
    seed. On success the pending cookie is cleared and the standard
    session + CSRF cookies are minted via _mint_session_response —
    same shape /auth/login returns for the no-TOTP path, so the SPA
    can finish login uniformly regardless of whether 2FA was in
    play.

    Failure modes (each emits a separate audit event so an operator
    chasing an incident can distinguish them):
      * No / expired pending cookie       → 400, no audit (the
        pending cookie itself was the only authoritative state, so
        there is no user to attribute to).
      * User missing or deactivated       → 401, audit
        ``login_totp_user_inactive`` denied.
      * TOTP disabled mid-flow (race)     → 400, audit
        ``login_totp_disabled_mid_flow`` denied.
      * Stored seed won't decrypt         → 500, audit
        ``login_totp_decrypt_failed`` error. Operator action
        required (Fernet key rotation gone wrong, DB tamper).
      * Code rejected                     → 401, audit
        ``login_totp_failed`` denied. Pending cookie preserved so
        a typo can be retried; rate-limit caps abuse at 10/min.
    """
    user_id = _read_pending_login_totp_cookie(request)
    if user_id is None:
        # No authoritative state to attribute to; bail without an
        # audit row. The 400 response prods the SPA to reset to the
        # password form (see app.js handler).
        raise HTTPException(
            status_code=400,
            detail=(
                "No login in progress. Sign in again with your "
                "username and password."
            ),
        )

    user = user_store.get_user_by_id(user_id)
    if user is None or not user.active:
        # Race: user got deactivated between the two steps. Drop the
        # pending cookie so a follow-up call gets the same 400 a fresh
        # session would.
        resp = JSONResponse(
            status_code=401,
            content={"detail": "User not found"},
        )
        _clear_pending_login_totp_cookie(resp)
        _audit(
            "login_totp_user_inactive",
            user.username if user else "?",
            "-",
            user_id=user_id,
            request=request,
            result="denied",
        )
        return resp

    if not user.totp_enabled:
        # Race: user disabled TOTP via /auth/totp/disable in another
        # tab between the two steps. The right move is to send them
        # back to /auth/login — the password they already supplied
        # would now suffice on its own.
        resp = JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "TOTP is no longer enabled for this account. "
                    "Sign in again — your password alone is now "
                    "sufficient."
                ),
            },
        )
        _clear_pending_login_totp_cookie(resp)
        _audit(
            "login_totp_disabled_mid_flow",
            user.username,
            "-",
            user_id=user.id,
            request=request,
            result="denied",
        )
        return resp

    # Phase B PR 4: per-user rate-limit also gates the TOTP step. A
    # password-step success that gets paired with a brute-force on
    # the TOTP code would otherwise rack up 10/min via the slowapi
    # decorator only — the per-user gate adds a 15-min cooldown
    # against the same account regardless of source IP. Pending
    # cookie is cleared on the 429 path so the user has to re-prove
    # password ownership after the cooldown elapses.
    is_limited, retry_after = user_store.check_login_rate_limit(user.id)
    if is_limited:
        # Audit pt-160: round Retry-After UP to the next 60 s
        # boundary, mirroring the /auth/login known-user path.
        rounded = _round_retry_after_to_minute(retry_after)
        resp = JSONResponse(
            status_code=429,
            content={"detail": _rate_limit_detail(rounded)},
            headers={"Retry-After": str(rounded)},
        )
        _clear_pending_login_totp_cookie(resp)
        _audit(
            "login_totp_rate_limit_hit",
            user.username,
            "-",
            user_id=user.id,
            request=request,
            result="denied",
        )
        return resp

    # Decrypt — failure is an integrity event (DB tamper, key
    # rotation gone wrong, missing keyfile), surfaced as 500 so
    # ops sees it instead of a silent 401.
    encrypted = user.totp_seed_encrypted
    assert encrypted is not None  # totp_enabled guard above
    try:
        secret = totp.decrypt_seed_for_user(user.id, encrypted)
    except (InvalidToken, ValueError) as e:
        logger.error(
            "login_totp: decrypt failed for user_id=%d: %s",
            user.id, str(e)[:200],
        )
        _audit(
            "login_totp_decrypt_failed",
            user.username,
            "-",
            user_id=user.id,
            request=request,
            result="error",
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "TOTP verification temporarily unavailable. Contact "
                "an administrator."
            ),
        )

    if not totp.verify_code(secret, body.code):
        # Wrong code — preserve pending cookie so a typo can be
        # retried within the 2-minute TTL. Phase B PR 4 reversed
        # the pre-PR4 decision to NOT touch the failed-login
        # counter on this branch: a TOTP brute-force after a
        # successful password-step IS a brute-force, and bumping
        # the same counter the password-step uses keeps the
        # threshold uniform — 10 failures in 15 min trigger the
        # cooldown regardless of which factor the attacker is
        # spraying.
        user_store.increment_failed_login(user.id)
        _audit(
            "login_totp_failed",
            user.username,
            "-",
            user_id=user.id,
            request=request,
            result="denied",
        )
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    # Code verified — full login complete. Reset the per-account
    # counter (the reset moved here from /auth/login in Phase B
    # PR 4 so a password-cracker who fails TOTP can't reset the
    # counter for free), clear pending state, mint the real
    # session, audit the full-success row. ``login_success_totp``
    # is a distinct action from ``auth_login`` so operators can
    # grep 2FA-completed logins separately when investigating.
    user_store.reset_failed_login(user.id)
    resp = _mint_session_response(
        user, request, audit_action="login_success_totp",
    )
    _clear_pending_login_totp_cookie(resp)
    return resp


def _logout_rate_limit_key(request: Request) -> str:
    """Audit r1-043: key the logout limiter on the caller's uid
    when a valid cookie is present so one user can't swamp another's
    bucket. Invalid / missing cookie falls back to the standard IP
    key — which is still rate-limited but under a different bucket
    from any authenticated logout traffic.
    """
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if payload:
        uid = payload.get("uid")
        if isinstance(uid, int) and uid > 0:
            return f"logout:uid:{uid}"
    # Fallback: same shape the shared limiter uses for unauthed
    # traffic so log-line formatting stays consistent.
    return f"logout:ip:{request.client.host if request.client else '-'}"


@router.post("/auth/logout")
@limiter.limit("10/minute", key_func=_logout_rate_limit_key)
async def auth_logout(request: Request):
    """Bump the caller's session epoch so every browser holding this
    cookie is rejected on the next request, not just the one calling
    logout. Other users' sessions are unaffected (Phase-3a moved
    epoch-tracking from a global counter to a per-user column).

    Audit r1-043: rate-limit is keyed per-user via
    ``_logout_rate_limit_key`` so a noisy client for user A can't
    burn through user B's bucket. 10/minute × per-uid is still
    conservative enough to cap bump_session_epoch write-pressure.
    """
    # Best-effort: resolve the caller from their cookie so we bump the
    # right row. A missing / invalid cookie still returns 200 — logout
    # is idempotent from the client's perspective.
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if payload:
        uid = payload.get("uid")
        if isinstance(uid, int) and uid > 0:
            try:
                user_store.bump_session_epoch(uid)
            except Exception as e:
                logger.warning("logout: bump_session_epoch failed (%s)", e)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


@router.get("/auth/status")
@limiter.limit("120/minute")
async def auth_status(request: Request):
    """Lightweight auth-probe used by the SPA on boot. Returns user_id
    so the frontend can conditionally render admin-only UI (e.g. the
    "Admin" nav link) without a second round-trip. No sensitive fields
    — username and numeric id are already observable from the session
    cookie's signed payload, so leaking them here is a no-op."""
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if payload:
        uid = payload.get("uid")
        username = None
        # Audit r1-006: cookie no longer carries a username field —
        # resolve from ``uid`` instead. Missing user falls through
        # to ``username=None`` so the SPA's avatar-initial renders
        # its placeholder instead of crashing.
        totp_enabled = False
        if isinstance(uid, int) and uid > 0:
            u = user_store.get_user_by_id(uid)
            if u is not None:
                username = u.username
                totp_enabled = u.totp_enabled
        return {
            "authenticated": True,
            "username": username,
            "user_id": int(uid) if isinstance(uid, int) else None,
            "totp_enabled": totp_enabled,
        }
    return {
        "authenticated": False,
        "username": None,
        "user_id": None,
        "totp_enabled": False,
    }


@router.post("/api/auth/change-password")
@limiter.limit("10/minute")
async def auth_change_password(
    body: ChangePasswordBody,
    request: Request,
    user: User = Depends(_request_user),
):
    if len(body.new_password) < PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {PASSWORD_MIN_LENGTH} characters",
        )
    # Audit v26-01: dependency consolidated to ``_request_user``, which
    # already validated uid + active and resolved the row in one go,
    # so the prior helper-local DB re-lookup is gone.
    username = user.username
    # Audit pd-003: current-password verify MUST run before the HIBP
    # network round-trip. Otherwise an attacker with a valid session
    # cookie (or a stale tab) could spray change-password requests
    # with arbitrary current-password values and trigger outbound
    # HTTPS to haveibeenpwned.com on every attempt — wasting egress
    # + burning against the HIBP SLA. Gate the network call behind
    # the cheap local check.
    verified = user_store.verify_password(username, body.current_password)
    if verified is None:
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    # HIBP Pwned-Passwords check (k-anonymity API — see
    # core/password_breach.py for the protocol + fail-open rationale).
    # Runs only after the current-password verify confirms the caller
    # actually knows the existing credential.
    if await is_password_pwned(body.new_password):
        raise HTTPException(
            status_code=400,
            detail=(
                "This password has been found in data breaches "
                "and is unsafe to use. Please choose a different password."
            ),
        )
    if not user_store.set_password(user.id, body.new_password):
        raise HTTPException(status_code=500, detail="Failed to update password")
    # Bump this user's epoch so every existing cookie for them (incl.
    # the one that just made this request) is invalidated. A security-
    # routing choice: forcing a fresh login after password-change is
    # the standard expectation.
    user_store.bump_session_epoch(user.id)
    _audit(
        "auth_change_password",
        username,
        "-",
        user_id=user.id,
        request=request,
    )
    return {"ok": True}


# ── Phase B PR 2: TOTP enrollment + disable ────────────────────────────────
#
# Three endpoints back the profile-page TOTP flow. The flow is split
# across setup → verify because we mint the secret in one round-trip
# (so the user can scan the QR), then commit the encrypted seed to the
# DB only after the user has proven possession of the authenticator
# app by typing a valid code. The pending-state lives in a 10-minute
# server-signed cookie (``_set_pending_totp_cookie`` in web/app.py)
# that is uid-bound — one user's pending cookie cannot be replayed
# against another user's verify call.
#
# Disable requires BOTH the current password AND a current TOTP code.
# Password proves session ownership; TOTP code proves physical access
# to the authenticator app. Either alone would be insufficient — a
# stolen cookie would be enough to disable the second factor without
# the password gate, and a stolen device-with-code would be enough to
# disable without the password gate.
#
# Audit-event types (six total — three success, three denied):
#   * totp_setup_initiated   — user kicked off enrollment
#   * totp_enabled           — verify succeeded, secret committed
#   * totp_verify_failed     — verify code rejected (denied)
#   * totp_disabled          — disable succeeded
#   * totp_disable_failed_password  — wrong password (denied)
#   * totp_disable_failed_code      — wrong TOTP code (denied)


class TotpSetupResponse(BaseModel):
    """Response of POST /auth/totp/setup. Carries the secret + a
    server-rendered SVG QR code so the SPA does not need a CDN-loaded
    QR encoder library — the v27-04 supply-chain surface stays closed
    and the operator does not need to regenerate an SRI hash on every
    qrcode-library bump."""

    provisioning_uri: str
    secret: str
    qr_svg: str  # raw <svg>...</svg> markup, embed via innerHTML
    expires_at: datetime


class TotpVerifyBody(BaseModel):
    """Body for POST /auth/totp/verify. The ``pattern`` mirrors the
    six-digit-only contract enforced in core.totp.verify_code, so an
    obviously-bad shape gets rejected by Pydantic with a 422 before
    the handler runs."""

    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TotpDisableBody(BaseModel):
    """Body for POST /auth/totp/disable. Dual-factor: password +
    TOTP code. Both fields are required — Pydantic rejects an empty
    or shape-wrong submission before the handler weighs in."""

    current_password: str = Field(min_length=1, max_length=512)
    totp_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


def _render_qr_svg(provisioning_uri: str) -> str:
    """Render an otpauth:// URI as an inline SVG QR code.

    Uses ``qrcode.image.svg.SvgPathImage`` so the output is a single
    ``<path>`` element rather than per-module ``<rect>``s — keeps the
    response body small (sub-15 KB for our URIs). Returns the SVG
    markup as a UTF-8 string, ready to embed via ``innerHTML`` on
    the SPA side. Server-side rendering is the v27-04 supply-chain
    posture: no CDN-loaded QR library, no SRI hash to regenerate.
    """
    img = qrcode.make(
        provisioning_uri,
        image_factory=SvgPathImage,
    )
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


@router.post("/auth/totp/setup", response_model=TotpSetupResponse)
@limiter.limit("5/minute")
async def auth_totp_setup(
    request: Request,
    response: Response,
    user: User = Depends(_request_user),
    actor: str = Depends(_request_actor),
):
    """Start TOTP enrollment.

    Generates a fresh base32 seed, stores it server-signed in the
    pending-TOTP cookie (10-minute TTL, uid-bound), and returns the
    provisioning URI + an inline-renderable SVG QR. The user has 10
    minutes to scan the QR with an authenticator app and POST a
    matching code to ``/auth/totp/verify`` — that call is what
    actually commits the secret to the DB.

    Refuses with 400 if the user already has TOTP enabled. The
    disable-flow exists for re-enrolment, and forcing the user
    through it ensures both password + current TOTP code prove
    intent before a brand-new seed lands in the DB.
    """
    if user.totp_enabled:
        raise HTTPException(
            status_code=400,
            detail=(
                "TOTP already enabled. Disable first via "
                "/auth/totp/disable to re-enroll."
            ),
        )

    secret = totp.generate_secret()
    provisioning_uri = totp.generate_provisioning_uri(
        secret, user.username,
    )
    qr_svg = _render_qr_svg(provisioning_uri)
    expires_at = datetime.fromtimestamp(
        time.time() + _webapp._PENDING_TOTP_TTL, tz=UTC,
    )

    body = TotpSetupResponse(
        provisioning_uri=provisioning_uri,
        secret=secret,
        qr_svg=qr_svg,
        expires_at=expires_at,
    )
    json_response = JSONResponse(content=body.model_dump(mode="json"))
    _set_pending_totp_cookie(json_response, secret, user.id)

    _audit(
        "totp_setup_initiated",
        user.username,
        actor,
        user_id=user.id,
        request=request,
    )
    return json_response


@router.post("/auth/totp/verify")
@limiter.limit("10/minute")
async def auth_totp_verify(
    body: TotpVerifyBody,
    request: Request,
    response: Response,
    user: User = Depends(_request_user),
    actor: str = Depends(_request_actor),
):
    """Verify a TOTP code against the pending-secret and commit on
    success.

    On success the secret is encrypted via the user's Fernet key and
    stored in ``users.totp_seed_encrypted``; the pending cookie is
    cleared. On failure the cookie is left intact so the user can
    retry without re-running setup. Wrong-code emits a denied audit
    event so brute-force attempts surface in the audit trail.
    """
    pending_secret = _read_pending_totp_cookie(request, user.id)
    if pending_secret is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "No TOTP enrolment in progress. POST /auth/totp/setup "
                "first."
            ),
        )

    if not totp.verify_code(pending_secret, body.code):
        _audit(
            "totp_verify_failed",
            user.username,
            actor,
            user_id=user.id,
            request=request,
            result="denied",
        )
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    encrypted = totp.encrypt_seed_for_user(user.id, pending_secret)
    if not user_store.update_user_totp_seed(user.id, encrypted):
        # Race: user row deleted between the dependency-resolve and
        # here. Vanishingly unlikely on a single-tenant deploy, but
        # surfacing 500 here would mask a real auth-state bug — fall
        # closed with 401 + audit trail instead.
        _audit(
            "totp_enable_failed_user_missing",
            user.username,
            actor,
            user_id=user.id,
            request=request,
            result="error",
        )
        raise HTTPException(
            status_code=401, detail="User row no longer exists",
        )

    response_body = JSONResponse(content={"ok": True, "totp_enabled": True})
    _clear_pending_totp_cookie(response_body)

    _audit(
        "totp_enabled",
        user.username,
        actor,
        user_id=user.id,
        request=request,
    )
    return response_body


@router.post("/auth/totp/disable")
@limiter.limit("5/minute")
async def auth_totp_disable(
    body: TotpDisableBody,
    request: Request,
    user: User = Depends(_request_user),
    actor: str = Depends(_request_actor),
):
    """Disable TOTP — requires BOTH current password AND a current
    valid TOTP code.

    Two-factor by design: a stolen session cookie alone cannot turn
    off the second factor (password gate), and a stolen device-with-
    code alone cannot either (password gate). Both must succeed.
    The two failure modes are audited separately so an operator
    investigating an incident can tell whether the attacker had the
    cookie or the device.
    """
    if not user.totp_enabled:
        raise HTTPException(
            status_code=400,
            detail="TOTP is not enabled for this account.",
        )

    # Audit pd-003 carry-over: verify the current password BEFORE
    # touching the encrypted seed. A stolen cookie cannot drive a
    # decrypt round-trip without the password gate.
    verified = user_store.verify_password(
        user.username, body.current_password,
    )
    if verified is None:
        await asyncio.sleep(0.1)
        _audit(
            "totp_disable_failed_password",
            user.username,
            actor,
            user_id=user.id,
            request=request,
            result="denied",
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Decrypt the stored seed and check the supplied code. A
    # decrypt failure is an integrity event (DB tamper / wrong key /
    # corruption) — surface it as 500 with a denied audit so the
    # operator notices.
    encrypted = user.totp_seed_encrypted
    assert encrypted is not None  # totp_enabled guard above
    try:
        secret = totp.decrypt_seed_for_user(user.id, encrypted)
    except (InvalidToken, ValueError) as e:
        logger.error(
            "totp_disable: decrypt failed for user_id=%d: %s",
            user.id, str(e)[:200],
        )
        _audit(
            "totp_disable_failed_decrypt",
            user.username,
            actor,
            user_id=user.id,
            request=request,
            result="error",
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not decrypt stored TOTP seed. Contact an "
                "administrator."
            ),
        )

    if not totp.verify_code(secret, body.totp_code):
        _audit(
            "totp_disable_failed_code",
            user.username,
            actor,
            user_id=user.id,
            request=request,
            result="denied",
        )
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    user_store.update_user_totp_seed(user.id, None)
    _audit(
        "totp_disabled",
        user.username,
        actor,
        user_id=user.id,
        request=request,
    )
    return {"ok": True, "totp_enabled": False}
