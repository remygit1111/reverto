"""Exchange-account routes — multi-account credential management.

Replaces the pre-multi-account ``/api/exchanges/{name}/keys`` shape
with a CRUD surface keyed on opaque account ids. An operator can now
maintain multiple accounts per exchange-type (e.g. "Bitget main" +
"Bitget test") and pick one per bot.

Endpoint map:
  GET    /api/exchanges/supported          — known exchange-type slugs
  GET    /api/exchange-accounts            — list user's accounts
  POST   /api/exchange-accounts            — create
  GET    /api/exchange-accounts/{id}       — read single
  PATCH  /api/exchange-accounts/{id}       — partial update (alias,
                                              is_default)
  DELETE /api/exchange-accounts/{id}       — delete (refuses if any
                                              bot still references it)
  POST   /api/exchange-accounts/{id}/test-connection
                                           — auth round-trip via
                                              fetch_balance()

Credential rotation goes through DELETE + CREATE rather than PATCH —
PATCH-ing api_key/api_secret would let an operator silently keep a
stale value paired with a fresh one. DELETE + CREATE forces the
operator to re-enter both, which also bumps the credentials_uuid so
any cached Fernet handle in a running engine becomes a hard mismatch
on the next reload.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import exchange_account_store, markets, paths
from core.credentials import CredentialFormatError
from core.user import User
from web.app import _audit, _request_actor, _request_user, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["exchanges"])

# Known exchange-type slugs. Mirrors core.exchange_account_store.
# Kept here AS WELL so the /supported endpoint has a stable wire
# contract independent of the store module's internals.
_KNOWN_EXCHANGES = exchange_account_store.KNOWN_EXCHANGE_TYPES


# ── Request bodies ─────────────────────────────────────────────────────────


class _AccountCreateBody(BaseModel):
    exchange_type: str = Field(min_length=1, max_length=32)
    # Bitget: spot / coin_m / usdt_m / usdc_m. Kraken: spot / futures.
    # See core.markets — that registry is the source of truth for
    # valid combinations.
    market_type: str = Field(
        min_length=1, max_length=exchange_account_store.MAX_MARKET_TYPE_LEN,
    )
    alias: str = Field(
        min_length=1, max_length=exchange_account_store.MAX_ALIAS_LEN,
    )
    api_key: str = Field(
        min_length=1, max_length=exchange_account_store.MAX_API_KEY_LEN,
    )
    api_secret: str = Field(
        min_length=1, max_length=exchange_account_store.MAX_API_SECRET_LEN,
    )
    # Bitget's third credential piece. Required for Bitget at the
    # handler layer; Pydantic-level it stays optional so Kraken bodies
    # can omit the field.
    passphrase: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=exchange_account_store.MAX_PASSPHRASE_LEN,
    )
    is_default: bool = False


class _AccountPatchBody(BaseModel):
    """Partial update — every field optional, ``None`` = leave alone.
    api_key / api_secret deliberately not included; see module
    docstring for the DELETE+CREATE rationale."""

    alias: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=exchange_account_store.MAX_ALIAS_LEN,
    )
    is_default: Optional[bool] = None


# ── Helpers ────────────────────────────────────────────────────────────────


def _require_user_account(account_id: int, user_id: int) -> dict:
    """Fetch the account dict and 404 if it doesn't exist OR belongs
    to a different user. The combined check is the cross-user
    isolation gate — a foreign account ID returns the same 404 as a
    nonexistent ID so the caller can't enumerate IDs to discover
    which ones belong to other tenants."""
    if not exchange_account_store.account_belongs_to_user(
        account_id, user_id,
    ):
        raise HTTPException(status_code=404, detail="Account not found")
    account = exchange_account_store.get_account(account_id)
    if account is None:
        # Race: account was deleted between the belongs-to-user check
        # and the fetch. Same 404 — the operator's view stays
        # consistent.
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _bots_referencing_account(user_id: int, account_id: int) -> list[str]:
    """Return the slugs of every bot YAML under ``user_id`` that
    references ``account_id``. Used by the DELETE handler to refuse a
    delete that would orphan a live bot config.

    Implementation note: scans YAML files directly rather than using
    the registry, because the registry trims by recently-active users
    while we want to catch every config on disk including those for
    quiescent users."""
    blocking: list[str] = []
    user_dir = paths.user_bots_dir(user_id)
    if not user_dir.exists():
        return blocking
    for yaml_path in sorted(user_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            # Malformed YAML is the operator's problem to fix, not
            # ours to block on. Skip silently — the relevant signal
            # would surface on the next ``load_bot_config`` call.
            continue
        bot_block = data.get("bot") if isinstance(data, dict) else None
        if not isinstance(bot_block, dict):
            continue
        if bot_block.get("exchange_account_id") == account_id:
            blocking.append(yaml_path.stem)
    return blocking


def _safe_error_message(exc: Exception) -> str:
    """Sanitise an exception message for the test-connection wire
    response. ccxt error strings can contain URL fragments, header
    snippets, or the api_key prefix — none of which the operator
    needs to debug a "wrong creds" event, and all of which could
    leak credential-shaped data into the audit log if echoed.

    Strip everything past the first newline and cap length at 200
    chars. The full exception text is still in the server log."""
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return msg[:200] if msg else type(exc).__name__


def _instantiate_authenticated_exchange(
    exchange_type: str, market_type: str, creds: dict,
):
    """Build an authenticated exchange client for the test-connection
    endpoint. Returns the client or raises a clean ValueError on
    misconfiguration. Bitget needs a passphrase; absence at this
    layer is treated as misconfiguration.

    ``market_type`` selects the wallet routing — the exchange client
    pulls ccxt_options / ccxt_params / balance_currency from
    ``core.markets`` and routes ``fetch_balance`` / order calls to
    the right wallet.

    Imports are deferred to function scope so the module-load path
    doesn't pull ccxt into routes/exchanges.py — a route module
    should stay cheap to import."""
    if exchange_type == "bitget":
        if not creds.get("passphrase"):
            raise ValueError(
                "Bitget account is missing a stored passphrase",
            )
        from exchanges.bitget import BitgetExchange
        return BitgetExchange(
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            passphrase=creds["passphrase"],
            market_type=market_type,
            paper=False,
        )
    if exchange_type == "kraken":
        from exchanges.kraken import KrakenExchange
        return KrakenExchange(
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            market_type=market_type,
            paper=False,
        )
    raise ValueError(f"Unsupported exchange_type {exchange_type!r}")


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("/api/exchanges/supported")
@limiter.limit("60/minute")
async def list_supported_exchanges(
    request: Request,
    user: User = Depends(_request_user),
):
    """List of exchange-type slugs and their supported market types
    (Bitget: spot/coin_m/usdt_m/usdc_m; Kraken: spot/futures). The
    frontend reads this once per session and uses it to populate the
    market dropdown after the exchange dropdown is chosen. Shape:

      {"exchanges": [{"name": "bitget", "markets": [
          {"key": "spot",   "label": "Spot"},
          {"key": "coin_m", "label": "Coin-M Perpetual"},
          ...
      ]}, ...]}
    """
    return {"exchanges": markets.supported_exchanges_payload()}


@router.get("/api/exchange-accounts")
@limiter.limit("60/minute")
async def list_user_accounts(
    request: Request,
    user: User = Depends(_request_user),
):
    """Every account belonging to the calling user, metadata only.
    api_key + api_secret never appear in the response — read paths
    only reveal storage shape, not credentials."""
    return {"accounts": exchange_account_store.list_accounts(user.id)}


@router.post("/api/exchange-accounts")
@limiter.limit("10/minute")
async def create_user_account(
    body: _AccountCreateBody,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Create a new exchange account. Validates exchange_type and the
    Bitget-passphrase rule, then delegates to the store. The store
    generates the credentials_uuid, writes the encrypted blob, and
    inserts the metadata row in a single guarded operation."""
    if body.exchange_type not in _KNOWN_EXCHANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown exchange_type {body.exchange_type!r}",
        )
    if body.exchange_type == "bitget" and not body.passphrase:
        raise HTTPException(
            status_code=400,
            detail="Bitget accounts require a passphrase (audit r1-012)",
        )
    try:
        new_id = exchange_account_store.create_account(
            user_id=user.id,
            exchange_type=body.exchange_type,
            market_type=body.market_type,
            alias=body.alias,
            api_key=body.api_key,
            api_secret=body.api_secret,
            passphrase=body.passphrase or "",
            is_default=body.is_default,
        )
    except CredentialFormatError as e:
        # r2-010: format-validator caught a typo before the encrypt
        # step. Surface as 400 with the validator's hint.
        raise HTTPException(status_code=400, detail=str(e))
    except exchange_account_store.AccountValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit(
        "exchange_account_create",
        f"{body.exchange_type}:{body.alias}",
        actor,
        user_id=user.id,
    )
    account = exchange_account_store.get_account(new_id)
    return {"ok": True, "account": account}


@router.get("/api/exchange-accounts/{account_id}")
@limiter.limit("60/minute")
async def get_user_account(
    account_id: int,
    request: Request,
    user: User = Depends(_request_user),
):
    """Single-account read. 404 covers both 'doesn't exist' and
    'belongs to another user' — same code path, no enumeration leak."""
    return _require_user_account(account_id, user.id)


@router.patch("/api/exchange-accounts/{account_id}")
@limiter.limit("30/minute")
async def update_user_account(
    account_id: int,
    body: _AccountPatchBody,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Update alias and/or is_default. Credential rotation goes
    through DELETE + CREATE (forces intentional action)."""
    _require_user_account(account_id, user.id)
    try:
        changed = exchange_account_store.update_account(
            account_id,
            alias=body.alias,
            is_default=body.is_default,
        )
    except exchange_account_store.AccountValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if changed:
        _audit(
            "exchange_account_update", str(account_id), actor,
            user_id=user.id, request=request,
        )
        if body.is_default is True:
            _audit(
                "exchange_account_set_default", str(account_id), actor,
                user_id=user.id, request=request,
            )
    return exchange_account_store.get_account(account_id)


@router.delete("/api/exchange-accounts/{account_id}")
@limiter.limit("10/minute")
async def delete_user_account(
    account_id: int,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Delete the account and its encrypted blob. Refuses (409) if
    any bot config still references the id — the operator must
    reassign or delete those bots first to keep the engine boot path
    from finding a dangling reference."""
    _require_user_account(account_id, user.id)
    blocking = _bots_referencing_account(user.id, account_id)
    if blocking:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Account is in use",
                "blocking_bots": blocking,
                "hint": (
                    "Reassign or delete the listed bots before "
                    "removing this account."
                ),
            },
        )
    removed = exchange_account_store.delete_account(account_id)
    if not removed:
        # Race with another DELETE — fine, treat as 404 for the
        # operator's mental model (the account is gone either way).
        raise HTTPException(status_code=404, detail="Account not found")
    _audit(
        "exchange_account_delete", str(account_id), actor,
        user_id=user.id, request=request,
    )
    return {"ok": True, "id": account_id}


@router.post("/api/exchange-accounts/{account_id}/test-connection")
@limiter.limit("10/minute")
async def test_account_connection(
    account_id: int,
    request: Request,
    actor: str = Depends(_request_actor),
    user: User = Depends(_request_user),
):
    """Attempt an authenticated call against the exchange using the
    stored credentials. On success, update ``last_tested_at`` and
    return a small balance summary so the UI can render a "Tested
    just now" indicator. On failure, return ``{ok: false, error: ...}``
    with a sanitised error string — the full exception still goes to
    the server log."""
    account = _require_user_account(account_id, user.id)
    creds = exchange_account_store.get_account_credentials(account_id)
    if creds is None:
        # The DB row exists but the .enc file is gone or corrupt — an
        # integrity event that the operator should fix. Surface a
        # 500-shaped failure rather than a clean ok=false so the
        # frontend doesn't paper over it with a normal red dot.
        raise HTTPException(
            status_code=500,
            detail="Stored credentials are unreadable",
        )
    try:
        client = _instantiate_authenticated_exchange(
            account["exchange_type"], account["market_type"], creds,
        )
        balance = client.get_balance()
    except Exception as e:  # noqa: BLE001 — ccxt raises many shapes
        logger.warning(
            "Test-connection failed for account %d: %s", account_id, e,
        )
        _audit(
            "exchange_account_test",
            f"{account_id}:fail", actor,
            user_id=user.id, request=request,
        )
        return {"ok": False, "error": _safe_error_message(e)}

    now = datetime.now(timezone.utc).isoformat()
    try:
        exchange_account_store.update_account(
            account_id, last_tested_at=now,
        )
    except exchange_account_store.AccountValidationError:
        # Shouldn't happen — last_tested_at has no validation, but
        # we don't want a phantom validation error to mask a
        # successful test.
        pass
    _audit(
        "exchange_account_test",
        f"{account_id}:ok", actor,
        user_id=user.id, request=request,
    )
    # Pull the human-readable market label from the registry so the
    # frontend doesn't have to maintain a parallel mapping.
    try:
        market_cfg = markets.get_market_config(
            account["exchange_type"], account["market_type"],
        )
        market_label = market_cfg["display_label"]
    except ValueError:
        market_label = account["market_type"]
    return {
        "ok": True,
        "balance": float(balance) if balance is not None else 0.0,
        "currency": client.balance_currency,
        "market": account["market_type"],
        "market_label": market_label,
        "tested_at": now,
    }
