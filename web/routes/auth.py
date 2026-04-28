"""Auth routes extracted from web/app.py.

Routes:
  POST /auth/login                 — bcrypt-checked login, sets session cookie
  POST /auth/logout                — bumps per-user session epoch + clears cookie
  GET  /auth/status                — returns auth state (no auth required)
  POST /api/auth/change-password   — rotates password + session epoch

Phase-3a: every auth-state read/write goes via ``core.user_store``
(DB-backed). The .auth.json blob is gone — admin password is
provisioned via ``scripts/setup_admin.py`` post-migration.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi.util import get_remote_address

from core import user_store
from core.password_breach import is_password_pwned
from core.user import User
from core.user_store import FAILED_LOGIN_WINDOW_S, PASSWORD_MIN_LENGTH
from web import app as _webapp
from web.app import (
    _audit,
    _create_session_cookie,
    _request_user,
    _SESSION_COOKIE,
    _SESSION_TTL,
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

# Per-account rate limit: once the failure counter reaches this, further
# attempts within the 1-hour sliding window are refused with 429 before
# password verification. Deliberately NOT a hard account lockout per
# NIST SP 800-63B — sliding window + backoff gives equivalent brute-
# force resistance without the DoS vector against legitimate users.
_PER_ACCOUNT_FAIL_LIMIT = 10

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


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


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

    # Per-account rate limit. Returns 429 BEFORE password verification
    # so an attacker can't amortise CPU by piling bcrypt calls against
    # a locked account.
    if pre_count >= _PER_ACCOUNT_FAIL_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many failed login attempts. "
                "Please wait and try again later."
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

    # Success — clear per-account counter so the sliding window
    # starts fresh on any future failures. Unknown-IP fallback is
    # left alone; a successful login doesn't imply the attacker's IP
    # has reformed.
    user_store.reset_failed_login(user.id)

    token = _create_session_cookie(user)
    # Audit r1-073: mint a CSRF token on successful login. Random
    # per-session URL-safe value; readable by JS so the SPA can
    # echo it in the X-CSRF-Token header on mutating requests.
    csrf_token = _webapp._mint_csrf_token()
    resp = JSONResponse({"ok": True, "csrf_token": csrf_token})
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
        "auth_login",
        user.username,
        "-",
        user_id=user.id,
        request=request,
    )
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
        if isinstance(uid, int) and uid > 0:
            u = user_store.get_user_by_id(uid)
            if u is not None:
                username = u.username
        return {
            "authenticated": True,
            "username": username,
            "user_id": int(uid) if isinstance(uid, int) else None,
        }
    return {"authenticated": False, "username": None, "user_id": None}


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
