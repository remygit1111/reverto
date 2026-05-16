# Configuring Reverto

Reverto reads configuration from three places:

1. **`.env`** in the repo root holds process-level settings: portal
   security, exchange env-var fallbacks, Telegram tokens, log
   level. Sourced by `start.sh` and inherited by every bot
   subprocess via an explicit allowlist.
2. **`config/bots/<user_id>/<slug>.yaml`** holds per-bot strategy
   configuration. One file per bot, validated against the
   Pydantic models in `config/models.py`.
3. **`logs/reverto.db`** + **`credentials/<user_id>/*.enc`** hold
   runtime state and per-user encrypted exchange credentials.
   Both are managed by the portal; you don't edit them by hand.

The Pydantic schema in `config/models.py` is the source of truth
for what fields exist; `.env.example` is the source of truth for
which environment variables are recognised. This document
explains what each field does, when you'd change it, and what to
watch out for.

For installation see [INSTALL.md](INSTALL.md); for operational
procedures see [OPERATIONS.md](OPERATIONS.md).

## Environment variables (`.env`)

`.env` lives in the repo root and is gitignored. `start.sh`
sources it via `set -a` so every variable assigned while sourcing
is exported into the portal process and inherited by every bot
subprocess. The bot subprocess environment is then filtered down
to an explicit allowlist (audit r1-023). If you add a new env
var that bots need to see, the allowlist in
[web/app.py](../web/app.py) needs an entry.

Group-by-group:

### Portal security

These are the only **required** env vars for a working portal.
Without them Reverto generates throwaway keys per restart and
emits a WARNING. That is fine for a 5-minute trial but it loses every
session on every restart and breaks any client that pinned the
old API key.

| Variable | Required | Default | What it does |
|----------|----------|---------|--------------|
| `REVERTO_API_KEY` | Yes (effectively) | ephemeral | API key checked by `X-API-Key` header on `/api/*` routes that bypass session-cookie auth (cron scripts, emergency-stop tooling). Generate with `python3 -c 'import secrets; print(secrets.token_hex(32))'`. |
| `REVERTO_SECRET_KEY` | Yes (effectively) | ephemeral | Secret used by `itsdangerous` to sign session cookies. Rotation invalidates every open session. Generate the same way. |
| `REVERTO_INSECURE_COOKIES` | No | unset | Set to `1` for local `http://localhost` development. Drops the `Secure` flag on session cookies so the browser will send them over plain HTTP. **Leave empty / unset in production behind TLS.** |
| `REVERTO_ADMIN_PW` | Optional | unset | Read by `scripts/setup_admin.py` (= `make setup-admin`) to write the bcrypt admin-password hash without a TTY prompt. Useful for automation; not read at portal-runtime. |
| `REVERTO_LOG_LEVEL` | No | `INFO` | Verbosity of every Python logger that calls `logging.getLogger(...)`. Valid: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Flip to `DEBUG` for incident-debug; revert after so `portal.log` doesn't bloat the rotation budget. |

### Exchange credentials

For Phase 2 + Phase 3a, exchange credentials are stored per-user,
encrypted at rest, in `credentials/<user_id>/<exchange>.enc`. The
env vars below are a **migration fallback**, used only if the
encrypted store is empty.

| Variable | What it does |
|----------|--------------|
| `KRAKEN_API_KEY` | Fallback Kraken API key. Prefer saving via the portal's Exchanges page so it lands in the encrypted per-user store. |
| `KRAKEN_API_SECRET` | Matching secret. Same caveat. |
| `BITGET_API_KEY` | Fallback Bitget API key. |
| `BITGET_API_SECRET` | Matching secret. |
| `BITGET_PASSPHRASE` | **Deprecated** (audit r1-012). Bitget's third credential piece is now part of the per-user encrypted blob. Setting this env var still works as a fallback but emits a WARNING in `portal.log` on every bot start that consults it. Migrate via `POST /api/exchanges/bitget/keys` (or the Exchanges page) and remove the env var. |

For the API permissions to enable on each exchange, see
[exchange-permissions.md](exchange-permissions.md).

### Telegram notifications

Optional. Configure two pairs of tokens:

| Variable | What it does |
|----------|--------------|
| `TELEGRAM_BOT_TOKEN` | Trade-alert bot token. Used by paper/live engines for entry/DCA/TP/SL/liquidation events. |
| `TELEGRAM_CHAT_ID` | Numeric chat ID where the bot posts. From `https://api.telegram.org/bot<token>/getUpdates` after sending the bot any message. |
| `TELEGRAM_CLAUDE_BOT_TOKEN` | Optional separate bot for `make beep` dev pings, so dev-workflow noise doesn't clutter the trade-alert channel. |
| `TELEGRAM_CLAUDE_CHAT_ID` | Matching chat ID. `scripts/notify.sh` prefers this pair when both are set; falls back to the `TELEGRAM_*` pair when either is empty. |

To set up:

1. Talk to `@BotFather` on Telegram, `/newbot`, follow the
   prompts. You get a token like
   `123456:ABCdefGHIjklMNOpqrSTUvwxYZ-1234567`.
2. Send your new bot any message from your account.
3. Visit `https://api.telegram.org/bot<token>/getUpdates` in a
   browser; copy the `chat.id` from the JSON response.
4. Drop both into `.env` and `make restart`.

Test with `make beep` (which uses the CLAUDE pair if set, the
trade-alert pair otherwise).

### Schema-migration override

| Variable | What it does |
|----------|--------------|
| `REVERTO_DESTRUCTIVE_MIGRATE` | Set to `1` to consent to a destructive schema migration (DROP + CREATE of owned tables). Normally unset. The portal refuses to boot with a destructive migration pending until you set this. **Read [OPERATIONS.md](OPERATIONS.md) "Schema migrations" first**. This exists to prevent a routine `make start` from silently wiping data. |

## Bot configuration (`config/bots/<user_id>/<slug>.yaml`)

Each bot has one YAML file. The path encodes ownership: `1/` is
the seeded admin user; in a multi-user deployment each user gets
their own integer-named subdirectory.

The wizard generates and validates these files; you typically
don't edit them by hand. When you do, validation is strict:
unknown keys at any level fail with a Pydantic `ValidationError`
instead of being silently dropped.

### Top-level fields

```yaml
name: "RSI 1h paper"            # required, 1–100 chars, [a-zA-Z0-9 _-]
mode: paper                     # paper | live | backtest
exchange: bitget                # bitget | kraken
pair: "BTC/USD"                 # default
contract_type: inverse_perpetual  # only value supported today
direction: long                 # long | short
timeframe: 1h                   # 15m | 1h | 4h | 1d
use_wick_simulation: true       # default
```

- **`name`**: human-readable label shown in the dashboard.
  Slug is derived separately from the filename.
- **`mode`**: `paper` simulates execution against the live ticker
  feed, never places real orders. `live` places real orders but
  is gated by Phase 1 dry-run guards (see
  [OPERATIONS.md](OPERATIONS.md) "Live bot dry-run via the
  portal"). `backtest` is for `main_backtest.py`, not for the
  portal.
- **`exchange`**: which `BaseExchange` subclass the engine uses.
- **`pair`**: a CCXT-recognised symbol. Bitget and Kraken use
  the same `BTC/USD` shape.
- **`contract_type`**: only `inverse_perpetual` is implemented
  (audit pt-043 fixed the PnL formula for it). Linear perpetuals
  and spot would each need their own PnL math.
- **`direction`**: long-only is the well-tested path; short
  works but the strategy library is tuned for long entries.
- **`timeframe`**: candle bucket the entry rules and TP indicators
  evaluate on.
- **`use_wick_simulation`**: when `true`, the paper engine pulls
  the forming candle's high/low on every tick and fires TP/SL
  against those values instead of only the live tick price.
  Matches the backtest behaviour and removes the 10s tick-poll
  blind spot at the cost of one extra OHLCV fetch per timeframe
  cache window.

### Leverage + liquidation guard

```yaml
leverage:
  enabled: false
  size: 1                    # 1..125
  liquidation_guard:
    warn_pct: 15.0
    emergency_close_pct: 5.0
```

- **`size: 1`** is no leverage. Above 1 multiplies position size
  on the exchange and brings liquidation risk into play.
- **`warn_pct`**: distance from the entry to the liquidation
  price (in percent of entry) below which the engine fires a
  warning. Default 15%.
- **`emergency_close_pct`**: distance below which the engine
  closes the deal on the next tick. Default 5%. Applied even
  in paper mode so the simulation reflects what the live engine
  would do.

### DCA

```yaml
dca:
  enabled: true
  base_order_size: 0.001     # > 0, in BTC for inverse perpetuals
  max_orders: 5              # 0..50; total = base + (max_orders - 1) DCAs
  order_spacing_pct: 2.5     # > 0, ≤ 50
  multiplier: 1.0            # > 0, ≤ 10; size of each DCA = prev * multiplier
  step_scale: 1.0            # > 0, ≤ 5; spacing of each step = prev * step_scale
  taker_fee: 0.0006          # exchange-specific; Bitget/Kraken default
  max_cumulative_size: null  # null or > 0; ceiling on total position
```

- **`max_orders`**: total orders including the base. `5` =
  1 base + 4 DCAs. `0` or `1` disables DCA (base order only).
- **`order_spacing_pct`**: how far below the avg entry the first
  DCA sits. Subsequent DCAs use `prev * step_scale` distance.
- **`multiplier`**: position-sizing growth per DCA. `1.0` = each
  DCA same size as the base. `1.5` = each DCA 1.5x the previous.
  Compounds quickly; an aggressive multiplier with high
  `max_orders` produces an order well beyond any realistic cap.
- **`max_cumulative_size`**: hard ceiling on `sum(base + every
  DCA)`. `null` = no cap (paper default). **Strongly recommended
  for live bots**. Without it a strategy that worsens monotonically
  can hit `max_orders` worth of progressively larger fills.
  The LiveEngine preflight rejects bots whose
  `multiplier × max_orders` produces an order beyond
  `max_cumulative_size`.

### Entry indicators

```yaml
entry:
  indicators: []                  # legacy single-list (AND across all)
  indicator_groups:               # newer grouped form (OR between groups)
    - id: 1
      name: "RSI oversold"
      indicators:
        - type: rsi
          period: 14
          threshold: "<30"
          timeframe: 1h
          price_source: close
```

A bot enters when at least one `indicator_group` confirms (OR
between groups) and every indicator inside that group passes
(AND inside a group). The legacy flat `indicators` list is
treated as a single implicit group.

Available indicator types (`type:` field):

- `rsi`: period, threshold (e.g. `<30`, `>70`), price source.
- `ema_cross`: fast, slow.
- `macd`: macd_fast, macd_slow, macd_signal, signal direction.
- `bollinger`: period, multiplier, ma_type, value (lower/upper).
- `parabolic_sar`: initial_af, max_af.
- `supertrend`: atr_period, multiplier.
- `market_structure`: lookback, trigger_type.
- `support_resistance`: left_bars, right_bars, proximity_pct,
  volume_threshold, min_touches.
- `qfl_base_scanner`: base_periods, pump_periods,
  pump_from_base_pct, base_crack_pct.

The full list of recognised parameters per indicator is the
`IndicatorConfig` model in [config/models.py](../config/models.py).
Most strategies do well with one or two indicators; stacking many
makes the entry rare and parity-testing harder.

### Take profit

```yaml
take_profit:
  enabled: true
  target_pct: 3.0              # > 0, ≤ 100; price-TP target as % above avg entry
  price_enabled: true          # set false to disable price-TP, keep indicator-TP
  indicator_confirm: null      # legacy single-indicator confirm
  minimum_tp_pct: null         # optional floor on profit % before any indicator-TP fires
  indicator_groups: []         # OR-of-AND structure same as entry
```

- **Price TP** (`price_enabled: true`) closes when
  `current_price >= avg_entry * (1 + target_pct/100)` for longs.
  Standard "fixed % above entry" exit.
- **Indicator TP** (`indicator_groups`) closes on indicator
  signals, useful for trend-following exits where the price
  threshold is a guess.
- **`minimum_tp_pct`**: even when an indicator group fires, only
  close if the deal is above this profit floor. Prevents an
  early indicator flip from forcing a tiny-profit close.

### Stop loss

```yaml
stop_loss:
  type: fixed                  # none | fixed | trailing
  pct: 5.0                     # 0..100
```

- **`type: none`** disables the SL entirely. Liquidation-guard is
  separate; in leveraged bots that's still in play.
- **`type: fixed`** fires at `entry * (1 - pct/100)` (longs).
- **`type: trailing`** tracks the peak since entry and fires when
  price falls `pct` below the running peak.

### Drawdown guard

```yaml
drawdown_guard:
  enabled: false
  max_drawdown_pct: 10.0       # % drop from peak that triggers
  metric: equity               # equity | balance
  action: pause                # pause (skip new entries) | stop
```

When the watched metric (equity = realized + unrealized,
balance = realized only) drops more than `max_drawdown_pct` from
its all-time peak since the bot started, the guard triggers.

- **`action: pause`** keeps existing deals running but blocks new
  entries until the operator resets via the portal or the API.
- **`action: stop`** closes every open deal at market and stops
  the bot.

The peak survives restarts via `state.json`. Reset via
`POST /api/bots/<slug>/drawdown/reset` (see
[OPERATIONS.md](OPERATIONS.md) "Drawdown guard").

### Schedule (trading windows)

```yaml
schedule:
  enabled: false
  timezone: "Europe/Amsterdam"
  trading_windows:
    - days: [Mon, Tue, Wed, Thu, Fri]
      from: "09:00"
      to: "17:00"
  blackout_dates: []           # ISO dates to skip entirely
```

When `enabled: true`, the bot only takes new entries inside a
configured window. Existing open deals keep running outside
windows (TP/SL still fire). `from` > `to` (e.g. `22:00` → `06:00`)
indicates an overnight window; both days must be listed in
`days`.

### Telegram per-bot

```yaml
telegram:
  notify_on:
    - entry
    - dca_trigger
    - tp_hit
    - sl_hit
    - liquidation_warn
    - schedule_open
    - schedule_close
    - error
    - startup
    - shutdown
    - stop
    - restart
```

Controls which event types trigger a Telegram notification. Drop
events you don't want to see; the engine still emits them
internally. `shutdown` is a legacy synonym kept for
back-compat; new bots use `stop` / `restart` for portal-driven
lifecycle events.

### ML (placeholder)

```yaml
ml:
  enabled: false
  model: lightgbm
  retrain_interval: 7d
  features: []
```

ML functionality is not implemented. Setting `enabled: true` has
no effect on engine behaviour beyond a startup WARNING.

## Strategy options

Reverto centres on **DCA (Dollar-Cost Averaging)** as the
position-sizing model: a base order, then progressively-sized
buys below it as the price worsens, exiting the whole stack on
TP/SL/indicator. This shape works well for mean-reverting
markets and badly for trending ones; pair selection and
`max_cumulative_size` are how you tune the risk envelope.

Common indicator combinations:

- **Mean reversion**: RSI oversold (`<30`) on the bot timeframe,
  optionally confirmed by Bollinger lower-band touch. Exit on
  RSI crossing 50 or hitting the price TP.
- **Trend continuation**: EMA cross on a higher timeframe
  (e.g. `4h` 50/200 cross) as a regime filter, plus an entry
  trigger on the bot timeframe.
- **Breakout**: support/resistance proximity + Supertrend flip,
  with a trailing SL.

Backtest each candidate on the bot's intended timeframe and
exchange before paying live fees. `make backtest BOT=<slug>`
runs the same DCA/TP/SL logic the paper engine uses against
historical OHLCV.

## Exchange-specific configuration

- **Bitget**: primary support, inverse-perpetual focus. Save
  api_key + api_secret + passphrase via the portal; legacy env
  vars work but emit a WARNING. PnL formula validated against
  Bitget testnet (audit pt-043).
- **Kraken**: secondary support, no passphrase. Same shape as
  Bitget minus the third credential piece.

For exact API permissions, see
[exchange-permissions.md](exchange-permissions.md). Save
credentials via `POST /api/exchanges/{exchange}/keys` or the
portal's Exchanges page; both write to the per-user encrypted
store under `credentials/<user_id>/<exchange>.enc`.

## Multi-tenant (advanced)

Reverto's codebase supports multiple users (the multi-tenant
foundation in Phase 1 + 2). For a self-hosted single-user
deployment the defaults are appropriate: everything runs on
`user_id=1` (the seeded admin), one user-directory layer
under `config/bots/1/`, one Fernet key at `keys/1.key`.

For multi-user deployments (per-user config paths, isolated
state, separate session epochs) the wiring exists in the code
but is not currently documented for self-hosters. The
[architecture.md](architecture.md) "Multi-tenant filesystem
layout (Phase 2)" section is the closest reference. Treat
multi-user setups as a "you are reading the source" path until
this guide grows.

This guide otherwise focuses on the single-user case.
