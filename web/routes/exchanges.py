"""Exchange-credentials routes extracted from web/app.py.

Routes:
  GET    /api/exchanges              — list supported exchanges + has_keys
  POST   /api/exchanges/{name}/keys  — store API keys (Fernet-encrypted)
  DELETE /api/exchanges/{name}/keys  — remove stored keys
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import credentials
from core.user import User
from web.app import _audit, _request_actor, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["exchanges"])

_KNOWN_EXCHANGES = ("bitget", "kraken")


class ExchangeKeysBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)
    api_secret: str = Field(min_length=1, max_length=512)
    # Audit r1-012: Bitget requires a passphrase alongside the api
    # key/secret; other exchanges (Kraken) don't. Optional on the
    # wire — the handler validates per exchange-name so Bitget
    # without a passphrase 400s with a clear error instead of
    # silently writing an incomplete credential file.
    passphrase: Optional[str] = Field(
        default=None, min_length=1, max_length=512,
    )


@router.get("/api/exchanges")
@limiter.limit("60/minute")
async def list_exchanges(
    request: Request,
    user: User = Depends(_request_user),
):
    """Welke exchanges Reverto kent en of er credentials voor opgeslagen zijn."""
    return {
        "exchanges": [
            {"name": name, "has_keys": credentials.has_keys(name, user.id)}
            for name in _KNOWN_EXCHANGES
        ]
    }


@router.post("/api/exchanges/{name}/keys")
@limiter.limit("10/minute")
async def save_exchange_keys(
    name: str,
    body: ExchangeKeysBody,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    if name not in _KNOWN_EXCHANGES:
        raise HTTPException(status_code=404, detail="Unknown exchange")
    # Bitget needs a passphrase; refuse to write an incomplete
    # credential file that would fail silently later at exchange
    # init. Other exchanges (Kraken) don't use passphrases and
    # accept ``passphrase=None`` → empty string in storage.
    if name == "bitget" and not body.passphrase:
        raise HTTPException(
            status_code=400,
            detail="Bitget credentials require a passphrase (audit r1-012)",
        )
    credentials.save_keys(
        name, body.api_key, body.api_secret, user.id,
        passphrase=body.passphrase or "",
    )
    _audit("exchange_keys_set", name, actor, user_id=user.id)
    return {"ok": True, "exchange": name}


@router.delete("/api/exchanges/{name}/keys")
@limiter.limit("10/minute")
async def delete_exchange_keys(
    name: str,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    if name not in _KNOWN_EXCHANGES:
        raise HTTPException(status_code=404, detail="Unknown exchange")
    removed = credentials.delete_keys(name, user.id)
    if not removed:
        raise HTTPException(status_code=404, detail="No keys stored for exchange")
    _audit("exchange_keys_delete", name, actor, user_id=user.id)
    return {"ok": True, "exchange": name}
