"""Exchange-credentials routes extracted from web/app.py.

Routes:
  GET    /api/exchanges              — list supported exchanges + has_keys
  POST   /api/exchanges/{name}/keys  — store API keys (Fernet-encrypted)
  DELETE /api/exchanges/{name}/keys  — remove stored keys
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import credentials
from web.app import _audit, _request_actor, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["exchanges"])

_KNOWN_EXCHANGES = ("bitget", "kraken")


class ExchangeKeysBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)
    api_secret: str = Field(min_length=1, max_length=512)


@router.get("/api/exchanges")
@limiter.limit("60/minute")
async def list_exchanges(request: Request):
    """Welke exchanges Reverto kent en of er credentials voor opgeslagen zijn."""
    return {
        "exchanges": [
            {"name": name, "has_keys": credentials.has_keys(name)}
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
):
    if name not in _KNOWN_EXCHANGES:
        raise HTTPException(status_code=404, detail="Unknown exchange")
    credentials.save_keys(name, body.api_key, body.api_secret)
    _audit("exchange_keys_set", name, actor)
    return {"ok": True, "exchange": name}


@router.delete("/api/exchanges/{name}/keys")
@limiter.limit("10/minute")
async def delete_exchange_keys(
    name: str,
    request: Request,
    actor: str = Depends(_request_actor),
):
    if name not in _KNOWN_EXCHANGES:
        raise HTTPException(status_code=404, detail="Unknown exchange")
    removed = credentials.delete_keys(name)
    if not removed:
        raise HTTPException(status_code=404, detail="No keys stored for exchange")
    _audit("exchange_keys_delete", name, actor)
    return {"ok": True, "exchange": name}
