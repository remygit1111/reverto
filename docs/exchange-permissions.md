# Exchange API Permission Matrix

This document enumerates every exchange API call Reverto issues and the
minimum API-key permission scope each call requires. Use it as the
reference when provisioning a fresh exchange API key for a Reverto
deploy: the key MUST have the listed permissions and SHOULD NOT have
any others (least-privilege).

The matrix has two purposes:

1. **Operator onboarding.** When a user creates a Bitget or Kraken API
   key, this is the canonical list of toggles to enable.
2. **Audit anchor.** A future change that introduces a call requiring
   a new permission scope (e.g. transfers, sub-account ops) must update
   this doc in the same PR. That prevents permission scope creep from
   slipping in unnoticed.

> **Withdrawal permission is intentionally never required.** Reverto
> does not issue withdrawal calls on any code path. If a key with
> withdrawal scope is provisioned anyway (e.g. exchange UI defaults
> the toggle on), strip it before saving. A leaked credential should
> never authorise moving funds out.

## Permission tiers

The matrix uses three tiers that map onto the standard exchange-side
toggles:

| Tier            | What it lets the key do                            |
| --------------- | -------------------------------------------------- |
| **Public**      | No authentication required at all (market data).   |
| **Read**        | Account balance, positions, orders (read-only).    |
| **Trade**       | Place / amend / cancel orders, set leverage.       |

Reverto requires **Read + Trade** on a live key. Public-only data is
fetched from a separate unauthenticated client and does not consume
the trading key's quota.

## Bitget

Source: [`exchanges/bitget.py`](../exchanges/bitget.py)

| Reverto method        | ccxt call                  | Tier    | Notes                                                     |
| --------------------- | -------------------------- | ------- | --------------------------------------------------------- |
| `get_ticker`          | `fetch_ticker`             | Public  | No auth. Used by both portal and engine.                  |
| `get_ohlcv`           | `fetch_ohlcv`              | Public  | Backtest + live candle fetcher. Capped at 200 bars.       |
| `get_balance`         | `fetch_balance`            | Read    | BTC equity used for sizing + drawdown gate.               |
| `get_position`        | `fetch_positions`          | Read    | Inverse-perp position state.                              |
| `get_open_orders`     | `fetch_open_orders`        | Read    | Used by the order reconciler.                             |
| `_with_order_retries` | `fetch_order`              | Read    | Idempotency probe inside order-retry helper.              |
| `place_market_order`  | `create_order` (`market`)  | Trade   | Carries `clientOrderId` for retry-safe placement.         |
| `place_limit_order`   | `create_order` (`limit`)   | Trade   | Same idempotency contract as the market path.             |
| `cancel_order`        | `cancel_order`             | Trade   | Failures are logged + returned as `False`, never raised.  |
| `set_leverage`        | `set_leverage`             | Trade   | Called once per (symbol, bot-start).                      |

**Third credential: passphrase.** Bitget API keys carry a third
factor (the passphrase chosen at key-creation time). Reverto stores
it inside the encrypted credential blob alongside `api_key` /
`api_secret`; see [`core/credentials.py`](../core/credentials.py) for
the `passphrase` field handling and the env-var migration path
(`BITGET_PASSPHRASE`).

**Symbol mapping.** All calls go through `_symbol()` which maps the
unified `BTC/USD` to ccxt's inverse-perpetual symbol `BTC/USD:BTC`.
A key without inverse-perp futures access will surface as a permission
error from ccxt on the first authenticated call.

## Kraken

Source: [`exchanges/kraken.py`](../exchanges/kraken.py)

| Reverto method       | ccxt call                  | Tier    | Notes                                                                     |
| -------------------- | -------------------------- | ------- | ------------------------------------------------------------------------- |
| `get_ticker`         | `fetch_ticker`             | Public  | Used by portal price card and engine signal.                              |
| `get_ohlcv`          | `fetch_ohlcv`              | Public  | Backtest + live fetch.                                                    |
| `get_balance`        | `fetch_balance`            | Read    | XBT free balance.                                                         |
| `get_position`       | `fetch_positions`          | Read    | Inverse-perp position via Kraken Futures.                                 |
| `get_open_orders`    | `fetch_open_orders`        | Read    | Reconciler input.                                                         |
| `place_market_order` | `create_order` (`market`)  | Trade   | NOTE: clientOrderId idempotency NOT yet plumbed (pt-037 / r2-002, open).  |
| `place_limit_order`  | `create_order` (`limit`)   | Trade   | Same caveat.                                                              |
| `cancel_order`       | `cancel_order`             | Trade   | Same logging-and-swallow contract as Bitget.                              |
| `set_leverage`       | `set_leverage`             | Trade   | Once per (symbol, bot-start).                                             |

**No passphrase.** Kraken API keys are `(api_key, api_secret)` only.
The `passphrase` field on the stored credential blob remains an empty
string and is ignored by `KrakenExchange.__init__`.

**Outstanding hardening.** The retry/idempotency helper used on Bitget
does not yet wrap Kraken order placement (finding pt-037, r2-002, still
open). When that lands, the table will gain a Read-tier `fetch_order`
entry and the create_order calls will move to clientOrderId-based
idempotency. The required permission scope does not change.

## Threat model: why this matters

A credential that was provisioned with scopes wider than this matrix
expands the blast radius of every credential-leak path on the host:

* **Withdrawal scope on a leaked key** turns a host compromise into
  fund loss, not just position loss. Refusing withdrawal scope
  upstream is the only scope-aware control that survives a full
  process compromise. No in-process check can prevent a leaked key
  from being driven by an attacker outside the Reverto process.
* **Sub-account / transfer scope** is similarly out-of-band for
  Reverto's design. Don't enable it.
* **Read-only keys** are fine for paper-mode and for monitoring
  dashboards but will fail at the first `create_order` when wired
  into a live engine. The engine reports the failure and refuses to
  start, but it would be a costly diagnostic round-trip. Provision
  with Trade from the start when intent is live.

The matrix is the source of truth; if the code grows a new call,
update this file in the same PR so the next operator onboarding is
not surprised.
