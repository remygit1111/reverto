"""Per-exchange market registry — single source of truth for the
(exchange, market_type) tuple.

Each exchange has multiple wallets (spot, coin-margined futures,
USDT-margined futures, …) and each wallet has its own balance, its
own ccxt routing, and its own native settlement currency. This
module owns the mapping; everything else (the credential store, the
admin routes, the exchange clients, the engine boot path) looks up
the right config here.

Adding a new market: extend ``MARKETS[exchange_type][market_type]``
with the four required keys (``ccxt_client``, ``ccxt_options``,
``ccxt_params``, ``balance_currency``, ``display_label``) — no other
file needs editing.

ccxt routing notes (ccxt 4.5.48):

* Bitget's ``fetch_balance`` for derivatives reads ``productType``
  from per-call ``params``, NOT from client-construction options.
  The values are ``USDT-FUTURES`` / ``USDC-FUTURES`` /
  ``COIN-FUTURES`` (the legacy v1/v2 names ``UMCBL/CMCBL/DMCBL``
  were renamed in Bitget's v2 API and ccxt uses the current spelling).
* Kraken splits spot and derivatives into two ccxt classes:
  ``ccxt.kraken`` and ``ccxt.krakenfutures``. The ``ccxt_client`` key
  controls which class the BaseExchange-subclass instantiates.
"""

from __future__ import annotations


# ── Registry ───────────────────────────────────────────────────────────────
#
# Each leaf dict carries five keys:
#
#   ccxt_client       — name of the ccxt class to instantiate
#                       (``getattr(ccxt, name)``). Matters for Kraken
#                       (spot vs futures are different classes) and
#                       is uniform "bitget" for every Bitget market.
#   ccxt_options      — ``options`` dict passed at client construction.
#                       Steers ccxt's *default* routing (``defaultType``
#                       spot/swap, ``defaultSubType`` linear/inverse).
#   ccxt_params       — extra ``params`` merged into every method call.
#                       Used for Bitget's ``productType`` which ccxt
#                       only reads from per-call params, not from
#                       client options.
#   balance_currency  — the currency key to read from
#                       ``fetch_balance()`` — BTC for inverse contracts,
#                       USDT for linear, USD for Kraken spot.
#   display_label     — human-readable label for the operator UI.

MARKETS: dict[str, dict[str, dict]] = {
    "bitget": {
        "spot": {
            "ccxt_client": "bitget",
            "ccxt_options": {"defaultType": "spot"},
            "ccxt_params": {},
            "balance_currency": "USDT",
            "display_label": "Spot",
        },
        "coin_m": {
            "ccxt_client": "bitget",
            # defaultType=swap + defaultSubType=inverse steer ccxt's
            # auto-resolution toward COIN-FUTURES; productType in
            # ccxt_params makes that explicit on the wire so a future
            # ccxt change to the default-resolution heuristic doesn't
            # silently flip the routing.
            "ccxt_options": {
                "defaultType": "swap",
                "defaultSubType": "inverse",
            },
            "ccxt_params": {"productType": "COIN-FUTURES"},
            "balance_currency": "BTC",
            "display_label": "Coin-M Perpetual",
        },
        "usdt_m": {
            "ccxt_client": "bitget",
            "ccxt_options": {
                "defaultType": "swap",
                "defaultSubType": "linear",
            },
            "ccxt_params": {"productType": "USDT-FUTURES"},
            "balance_currency": "USDT",
            "display_label": "USDT-M Perpetual",
        },
        "usdc_m": {
            "ccxt_client": "bitget",
            "ccxt_options": {
                "defaultType": "swap",
                "defaultSubType": "linear",
            },
            "ccxt_params": {"productType": "USDC-FUTURES"},
            "balance_currency": "USDC",
            "display_label": "USDC-M Perpetual",
        },
    },
    "kraken": {
        "spot": {
            "ccxt_client": "kraken",
            "ccxt_options": {},
            "ccxt_params": {},
            "balance_currency": "USD",
            "display_label": "Spot",
        },
        "futures": {
            "ccxt_client": "krakenfutures",
            "ccxt_options": {},
            "ccxt_params": {},
            # Coin-margined inverse contracts — balance is in BTC.
            "balance_currency": "BTC",
            "display_label": "Futures",
        },
    },
}


# ── Cross-contract validation ─────────────────────────────────────────────
#
# Some bot ``contract_type`` choices only make sense against certain
# market_types. ``inverse_perpetual`` is BTC-margined → must be Bitget
# coin_m or Kraken futures. As new contract_types are added (linear,
# spot-rebalance, …) extend this dict.

_CONTRACT_TYPE_TO_MARKETS: dict[str, set[tuple[str, str]]] = {
    "inverse_perpetual": {
        ("bitget", "coin_m"),
        ("kraken", "futures"),
    },
}


# ── Lookups ───────────────────────────────────────────────────────────────


def allowed_markets(exchange_type: str) -> list[str]:
    """Return the registered market_type keys for ``exchange_type`` in
    display order (spot first, then derivatives, in dict-insertion
    order). Returns ``[]`` for an unknown exchange — callers that
    need to fail-loud should use ``validate_combo`` instead."""
    return list(MARKETS.get(exchange_type, {}).keys())


def get_market_config(exchange_type: str, market_type: str) -> dict:
    """Look up the per-(exchange, market) config dict. Raises
    ``ValueError`` if the combination isn't registered."""
    ex = MARKETS.get(exchange_type)
    if not ex:
        raise ValueError(f"Unknown exchange_type: {exchange_type}")
    m = ex.get(market_type)
    if not m:
        raise ValueError(
            f"Unknown market_type '{market_type}' for {exchange_type}. "
            f"Allowed: {', '.join(allowed_markets(exchange_type))}"
        )
    return m


def validate_combo(exchange_type: str, market_type: str) -> None:
    """Raise ``ValueError`` if (exchange_type, market_type) is not a
    known combo. Use in routes / store before INSERT to fail fast
    with a clean 400. Subclasses of ValueError raised by callers
    (e.g. ``AccountValidationError``) still flow through this
    interface unchanged."""
    if exchange_type not in MARKETS:
        raise ValueError(f"Unknown exchange_type: {exchange_type}")
    if market_type not in MARKETS[exchange_type]:
        raise ValueError(
            f"market_type '{market_type}' is not supported for "
            f"{exchange_type}. Supported: "
            f"{', '.join(MARKETS[exchange_type].keys())}"
        )


def validate_contract_type(
    contract_type: str, exchange_type: str, market_type: str,
) -> None:
    """Raise ``ValueError`` if a bot's ``contract_type`` is incompatible
    with the (exchange_type, market_type) of its account.

    Called at engine boot (main_live / main_paper / main_backtest) so
    the operator gets a clear refusal before the engine starts placing
    orders into the wrong wallet. Cannot run in Pydantic / config
    validation because the contract_type ↔ market_type mapping lives
    in this registry and the bot YAML only carries an account_id, not
    a market_type.

    Unknown contract_types pass through (additive — a future
    contract_type that isn't in ``_CONTRACT_TYPE_TO_MARKETS`` yet
    shouldn't block the operator until we explicitly characterise it).
    """
    allowed = _CONTRACT_TYPE_TO_MARKETS.get(contract_type)
    if allowed is None:
        return
    if (exchange_type, market_type) not in allowed:
        pairs = ", ".join(sorted(f"{e}/{m}" for e, m in allowed))
        raise ValueError(
            f"contract_type {contract_type!r} requires one of: "
            f"{pairs}. Account is {exchange_type}/{market_type}."
        )


def supported_exchanges_payload() -> list[dict]:
    """Return the shape the ``/api/exchanges/supported`` endpoint
    delivers — list of exchanges with their markets in display order.
    Kept in this module so route + frontend share a single source of
    truth for the menu structure."""
    out: list[dict] = []
    for ex_name, markets in MARKETS.items():
        out.append({
            "name": ex_name,
            "markets": [
                {"key": k, "label": v["display_label"]}
                for k, v in markets.items()
            ],
        })
    return out
