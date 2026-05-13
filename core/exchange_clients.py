"""Authenticated exchange-client construction.

Builds a ready-to-call exchange client (Bitget or Kraken) from the
encrypted credentials of an ``exchange_accounts`` row. Lives in
``core`` rather than in ``web/routes/exchanges.py`` so two callers
can share it without a circular import:

  * ``web.routes.exchanges`` — the test-connection endpoint.
  * ``main_scheduler.py``    — the hourly snapshot loop.

Why a dedicated module and not a method on the store: the store
deliberately does not import the ``exchanges/*`` package — that
package pulls in ccxt + several network-shaped helpers, and the
store's writers (notably ``create_account``) sit on the hot path of
test-suite fixtures. Keeping the heavy ccxt dependency one import
hop away means the bulk of the test suite still loads in a fraction
of a second.

Imports of ``exchanges.bitget`` / ``exchanges.kraken`` are deferred
to function scope so importing this module is itself cheap.
"""

from __future__ import annotations

from typing import Any

from core import exchange_account_store


class ExchangeClientError(Exception):
    """Raised when a client cannot be built — unknown exchange_type,
    missing required credential piece, or unreadable stored blob.

    Subclasses ``Exception`` (not ``ValueError``) so callers can
    distinguish "credential layout was wrong" from "user passed a
    bogus integer". The route layer translates both to clean 4xx
    statuses.
    """


def build_authenticated_exchange(
    exchange_type: str, market_type: str, creds: dict,
) -> Any:
    """Construct an authenticated exchange client.

    ``creds`` is the dict returned by
    ``core.credentials.get_keys_by_uuid`` (or
    ``exchange_account_store.get_account_credentials``); it must
    contain ``api_key`` + ``api_secret``, plus ``passphrase`` for
    Bitget (audit r1-012).

    ``market_type`` selects the wallet routing via ``core.markets`` —
    ``BitgetExchange`` / ``KrakenExchange`` read the matching
    ``ccxt_options`` and ``balance_currency`` from the registry.

    Raises ``ExchangeClientError`` on misconfiguration. Real network
    failures (auth rejection, rate-limit) surface from the returned
    client's first call, not from this constructor.
    """
    if exchange_type == "bitget":
        if not creds.get("passphrase"):
            raise ExchangeClientError(
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
    raise ExchangeClientError(
        f"Unsupported exchange_type {exchange_type!r}",
    )


def build_client_for_account(account_id: int) -> Any:
    """Convenience wrapper: fetch the account + creds and build the
    client in one call. The scheduler uses this on every tick.

    Raises ``ExchangeClientError`` if the account row is missing or
    its credentials cannot be decrypted; callers should treat this
    as a per-account failure rather than fatal so one bad row does
    not kill the whole snapshot batch.
    """
    account = exchange_account_store.get_account(account_id)
    if account is None:
        raise ExchangeClientError(
            f"Exchange account id={account_id} not found",
        )
    creds = exchange_account_store.get_account_credentials(account_id)
    if creds is None:
        raise ExchangeClientError(
            f"Stored credentials for account id={account_id} "
            f"are unreadable",
        )
    return build_authenticated_exchange(
        account["exchange_type"], account["market_type"], creds,
    )
