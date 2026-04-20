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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import user_store
from web import app as _webapp
from web.app import (
    _audit,
    _create_session_cookie,
    _require_session,
    _SESSION_COOKIE,
    _SESSION_TTL,
    _verify_session_cookie,
    limiter,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


@router.post("/auth/login")
@limiter.limit("5/minute")
async def auth_login(body: LoginBody, request: Request):
    user = user_store.verify_password(body.username, body.password)
    if user is None:
        # Damp brute force without blocking the event loop. Identical
        # timing + generic error across every failure mode (missing
        # user, inactive user, NULL hash, wrong password) so an
        # attacker can't enumerate usernames via response differences.
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _create_session_cookie(user)
    resp = JSONResponse({"ok": True})
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
    _audit("auth_login", user.username, "-")
    return resp


@router.post("/auth/logout")
@limiter.limit("10/minute")
async def auth_logout(request: Request):
    """Bump the caller's session epoch so every browser holding this
    cookie is rejected on the next request, not just the one calling
    logout. Other users' sessions are unaffected (Phase-3a moved
    epoch-tracking from a global counter to a per-user column)."""
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
    payload = _verify_session_cookie(request.cookies.get(_SESSION_COOKIE))
    if payload:
        return {"authenticated": True, "username": payload.get("u")}
    return {"authenticated": False, "username": None}


@router.post("/api/auth/change-password")
@limiter.limit("10/minute")
async def auth_change_password(
    body: ChangePasswordBody,
    request: Request,
    session: dict = Depends(_require_session),
):
    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=400,
            detail="New password must be at least 8 characters",
        )
    username = session.get("u", "")
    user = user_store.verify_password(username, body.current_password)
    if user is None:
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if not user_store.set_password(user.id, body.new_password):
        raise HTTPException(status_code=500, detail="Failed to update password")
    # Bump this user's epoch so every existing cookie for them (incl.
    # the one that just made this request) is invalidated. A security-
    # routing choice: forcing a fresh login after password-change is
    # the standard expectation.
    user_store.bump_session_epoch(user.id)
    _audit("auth_change_password", username, "-")
    return {"ok": True}
