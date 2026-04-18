"""Auth routes extracted from web/app.py.

Routes:
  POST /auth/login                 — bcrypt-checked login, sets session cookie
  POST /auth/logout                — bumps session epoch + clears cookie
  GET  /auth/status                — returns auth state (no auth required)
  POST /api/auth/change-password   — rotates password + session epoch

Uses session + auth-file primitives still defined in web/app.py
(_load_auth, _save_auth, _verify_session_cookie, _create_session_cookie,
_bump_session_epoch, _require_session, _SESSION_COOKIE, _SESSION_TTL,
_COOKIE_SECURE, _INITIAL_PW_FILE). These are imported from web.app
below and reused verbatim.
"""

from __future__ import annotations

import asyncio
import logging

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from web.app import (
    _audit,
    _bump_session_epoch,
    _COOKIE_SECURE,
    _create_session_cookie,
    _INITIAL_PW_FILE,
    _load_auth,
    _require_session,
    _save_auth,
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
    auth = _load_auth() or {}
    stored_hash = auth.get("password_hash", "")
    stored_user = auth.get("username", "")
    ok = False
    if stored_user and stored_hash and body.username == stored_user:
        try:
            ok = bcrypt.checkpw(
                body.password.encode("utf-8"), stored_hash.encode("utf-8"),
            )
        except ValueError:
            ok = False
    if not ok:
        # Damp brute force without blocking the event loop.
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _create_session_cookie(stored_user)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=_COOKIE_SECURE,
        path="/",
    )
    _audit("auth_login", stored_user, "-")
    return resp


@router.post("/auth/logout")
async def auth_logout():
    """Bump session epoch so every browser holding this cookie is
    rejected on the next request, not just the one calling logout."""
    try:
        _bump_session_epoch()
    except Exception as e:
        logger.warning("logout: failed to bump session epoch (%s)", e)
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
    auth = _load_auth() or {}
    stored_hash = auth.get("password_hash", "")
    try:
        ok = bool(stored_hash) and bcrypt.checkpw(
            body.current_password.encode("utf-8"),
            stored_hash.encode("utf-8"),
        )
    except ValueError:
        ok = False
    if not ok:
        await asyncio.sleep(0.1)
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    new_hash = bcrypt.hashpw(
        body.new_password.encode("utf-8"), bcrypt.gensalt(rounds=12),
    ).decode("utf-8")
    auth["password_hash"] = new_hash
    try:
        current_epoch = int(auth.get("session_epoch", 0))
    except (TypeError, ValueError):
        current_epoch = 0
    auth["session_epoch"] = current_epoch + 1
    _save_auth(auth)
    if _INITIAL_PW_FILE.exists():
        try:
            _INITIAL_PW_FILE.unlink()
        except OSError:
            pass
    _audit("auth_change_password", session.get("u", "-"), "-")
    return {"ok": True}
