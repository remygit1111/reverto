# Reverto

[![Version](https://img.shields.io/github/v/release/remygit1111/reverto)](https://github.com/remygit1111/reverto/releases)
[![License](https://img.shields.io/badge/license-BSL%201.1-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)
[![Tests](https://img.shields.io/badge/tests-2113%20passing-success)](#)

> **An open-source (BSL 1.1) automated trading framework for BTC/USD inverse perpetual contracts on Bitget and Kraken.** Self-hosted, paper-trading-ready out of the box, with a commercial Live Plugin coming in a future release.

> ⚠️ **Trading cryptocurrencies involves substantial financial risk. You may lose all your capital.** Read [DISCLAIMERS.md](DISCLAIMERS.md) before deploying any code from this repository.

---

## Table of Contents

- [What is Reverto?](#what-is-reverto)
- [Who is this for?](#who-is-this-for)
- [Status](#status)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [License](#license)
- [Author](#author)

---

## What is Reverto?

Reverto is a Python-based automated trading framework that runs DCA (Dollar-Cost Averaging) bots against perpetual futures markets. It includes:

- **Multi-bot architecture** with per-bot configuration and isolated state
- **Paper trading engine** for strategy validation without real capital
- **Backtest engine** for historical strategy evaluation
- **Live trading scaffold** for real-money deployment (commercial Live Plugin coming separately)
- **Web portal** with TOTP 2FA, per-user encrypted credentials, and a workspace dashboard
- **Indicator library** with RSI, EMA crossover, MACD, Bollinger Bands, PSAR, Supertrend, and more
- **Telegram notifications** for trade alerts and lifecycle events

Reverto targets **inverse perpetual contracts** specifically (where margin and PnL are denominated in the base asset, e.g., BTC). The math, position sizing, and liquidation logic differ from linear perpetuals.

## Who is this for?

**You might like Reverto if:**

- You want to self-host a Bitcoin DCA bot on your own infrastructure
- You're comfortable with Python, Linux, and managing your own API keys
- You prefer transparency (audit the code) over convenience (hosted service)
- You want to evaluate the strategies via paper trading before risking real capital
- You're interested in BTC inverse perpetual futures specifically

**Reverto is probably NOT for you if:**

- You want a "set and forget" hosted service
- You don't have a technical background (Python, Linux, command line)
- You expect support and SLAs typical of paid commercial products
- You're trading anything other than BTC inverse perpetuals (no spot, no linear perps)

See [DISCLAIMERS.md](DISCLAIMERS.md) for the full list of caveats and what Reverto is NOT.

## Status

**Current version: Reverto v0.5.0**

Check the running version:

```bash
python main_paper.py --version
```

See [docs/RELEASES.md](docs/RELEASES.md) for the release history and [LICENSE](LICENSE) for current license terms.

Reverto is functional for paper trading and backtesting. The framework — paper trading, backtest engine, indicators, web portal — is published under BSL 1.1 (see [License](#license) section) and free for non-production use.

The live trading capability currently ships as an in-tree scaffold; it is being separated into a commercial `reverto-live` plugin in a future release. The framework will continue to be free; the live plugin will be sold separately. Pricing and availability will be announced at [reverto.bot](https://reverto.bot) when ready.

The original maintainer (`remy1111`) uses Reverto for personal trading. There is no service-level agreement; this is software you self-host.

## Quick Start

**Requirements:** Python 3.12+, Linux (tested on Ubuntu 24 / WSL2)

```bash
# Clone and set up venv
git clone https://github.com/remygit1111/reverto.git
cd reverto
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env: fill in REVERTO_API_KEY, REVERTO_SECRET_KEY (generate via Python),
# and exchange credentials (Bitget/Kraken API keys).

# Initialize and start the portal
make start

# Set the admin password (separate terminal, with portal still running)
REVERTO_ADMIN_PW="your_strong_password_min_12_chars" make setup-admin

# Restart portal to pick up admin user
make restart
```

Open <http://localhost:8080>, log in with `admin` + your password, complete TOTP enrollment, and create your first paper bot via the dashboard.

For detailed self-hoster documentation:

- [docs/INSTALL.md](docs/INSTALL.md) — installation guide
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — env vars, bot YAML, strategies
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — backups, schema migrations, troubleshooting
- [docs/architecture.md](docs/architecture.md) — codebase overview
- [docs/exchange-permissions.md](docs/exchange-permissions.md) — API key permissions
- [SECURITY.md](SECURITY.md) — security disclosure policy

## Configuration

Required environment variables (in `.env`):

- `REVERTO_API_KEY`: random 64-char hex (`python3 -c 'import secrets; print(secrets.token_hex(32))'`)
- `REVERTO_SECRET_KEY`: same generation method
- `BITGET_API_KEY`, `BITGET_API_SECRET`: from your Bitget account
- `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`: from your Kraken account (if using Kraken)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: optional, for trade alerts

See [.env.example](.env.example) for the complete list.

## Architecture

High-level component overview:

```
┌──────────────────────────────────────────────────────────────┐
│  Portal (web/app.py via main_web.py)                         │
│  ├── FastAPI: auth, routing, WebSocket log streaming         │
│  ├── TOTP 2FA + per-user encrypted exchange credentials      │
│  ├── BotRegistry — manages bot lifecycle from YAML config    │
│  └── Spawns bots as subprocesses (paper or live)             │
└────────────────┬────────────────────────────────────────────-┘
                 │ subprocess.Popen
      ┌──────────┴──────────┐
      ▼                     ▼
┌────────────────┐    ┌────────────────┐
│ main_paper.py  │    │ main_live.py   │
│   (per bot)    │    │   (per bot)    │
│                │    │                │
│ Paper trading  │    │ Live trading   │
│ engine         │    │ engine         │
└───────┬────────┘    └───────┬────────┘
        │                     │
        ▼                     ▼
┌──────────────────┐    ┌──────────────────┐
│ PaperEngine      │    │ LiveEngine       │
│ + simulated      │    │ + real exchange  │
│   fills          │    │   orders         │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
         ┌──────────────────────────┐
         │ TradingEngine (ABC)      │
         │ Shared: tick loop, DCA,  │
         │ TP/SL, sentinels, state, │
         │ notifications            │
         └──────────────────────────┘
                     │
                     ▼
         ┌──────────────────────────┐
         │ BaseExchange             │
         │ Bitget + Kraken adapters │
         │ via ccxt                 │
         └──────────────────────────┘
```

Directory layout:

```
~/reverto/
├── core/              Shared business logic (trading engine, indicators, paths)
├── paper/             Paper-trading engine (subclass of TradingEngine)
├── backtest/          Backtest engine
├── live/              Live-trading scaffold (separating to plugin in future release)
├── exchanges/         Bitget + Kraken adapters via ccxt
├── strategies/        DCA strategy + indicator implementations
├── web/               FastAPI portal (auth, dashboard, API)
├── notifications/     Telegram notifier
├── config/            Pydantic config models
└── tests/             2113 tests covering core paths
```

Each bot runs as a separate `main_paper.py` (or `main_live.py`) subprocess managed by the portal. See [docs/architecture.md](docs/architecture.md) for the full architecture overview including the LiveProvider Protocol that mediates the future plugin separation.

## License

Reverto is licensed under the Business Source License 1.1 (BSL).

**What this means:**

- The source code is publicly available for inspection and audit.
- You may use Reverto freely for non-production purposes including
  evaluation, paper trading, and backtesting.
- Production use (live trading with real funds) requires either:
  - A separate commercial license from the Licensor, or
  - Use together with a separately-licensed Reverto Live Plugin
- Four years after each release, that specific version automatically
  converts to Apache License 2.0 (see [docs/RELEASES.md](docs/RELEASES.md)
  for the conversion schedule).

**Why BSL?**

The BSL provides a rolling commercial protection window while keeping
the source code transparent. Crypto traders can audit how Reverto
handles API keys, while preventing direct commercial redistribution
during the protection period.

For the full license terms, see [LICENSE](LICENSE).

For background on the licensing strategy, see
[docs/plugin_split_decisions.md](docs/plugin_split_decisions.md) O2.

## Author

Maintained by **remy1111** ([@remygit1111](https://github.com/remygit1111)).

The Reverto framework is open-source (BSL 1.1) and self-hosted. A separately-licensed Reverto Live Plugin for live trading is planned for a future release. Issues and pull requests on the framework will be reviewed when time permits, but there is no guaranteed response time.

---

**Before deploying:** Read [DISCLAIMERS.md](DISCLAIMERS.md) and [LICENSE](LICENSE).
