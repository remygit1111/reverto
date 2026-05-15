# Reverto

> **An open-source automated trading framework for BTC/USD inverse perpetual contracts on Bitget and Kraken.** Self-hosted, designed for personal use, published for educational and research purposes.

---

## ⚠️ Important Notice

**This software is for educational and research purposes.** It is NOT a financial service, NOT a hosted product, and the maintainers do NOT offer commercial support.

- **Trading cryptocurrencies involves substantial financial risk.** You may lose all your capital.
- **No guarantees of profitability.** Backtest results do not predict future performance.
- **Use at your own risk.** The software is provided "as is" without warranties of any kind. See [LICENSE](LICENSE) for full terms.
- **Regulatory compliance is your responsibility.** If you deploy this software for live trading, you are responsible for compliance with applicable laws in your jurisdiction (including but not limited to the EU's MiCA regulation).
- **The maintainers are not financial advisors.** Nothing in this repository constitutes financial advice.

If you intend to use this software, you should fully understand the code, the trading strategies, and the risks involved. **If you don't, don't use it.**

---

## What is Reverto?

Reverto is a Python-based automated trading framework that runs DCA (Dollar-Cost Averaging) bots against perpetual futures markets. It includes:

- **Multi-bot architecture** with per-bot configuration and isolated state
- **Paper trading engine** for strategy validation without real capital
- **Backtest engine** for historical strategy evaluation
- **Live trading scaffold** for real-money deployment (requires explicit configuration)
- **Web portal** with TOTP 2FA, per-user encrypted credentials, and a workspace dashboard
- **Indicator library** with RSI, EMA crossover, MACD, Bollinger Bands, PSAR, Supertrend, and more
- **Telegram notifications** for trade alerts and lifecycle events

Reverto targets **inverse perpetual contracts** specifically (where margin and PnL are denominated in the base asset, e.g., BTC). The math, position sizing, and liquidation logic differ from linear perpetuals.

## What Reverto is NOT

- **Not a service.** This is software you run yourself on your own infrastructure.
- **Not MiCA-compliant for commercial use.** If you want to offer this to others as a paid service in the EU, you will need to obtain CASP authorization separately. The maintainers do not provide such authorization.
- **Not a guaranteed profit machine.** Trading bots can and do lose money.
- **Not actively maintained as a product.** Bug fixes and improvements come on a best-effort basis.

## Status

**Current version: Reverto v0.5.0**

Check the running version:

```bash
python main_paper.py --version
```

See [docs/RELEASES.md](docs/RELEASES.md) for the release history and [LICENSE](LICENSE) for current license terms.

Reverto is functional for paper trading and backtesting. The live-trading scaffold exists but requires careful configuration and review before deploying real capital.

This project is published as a snapshot of working code. The original maintainer (`remy1111`) uses Reverto for personal trading. There is no commercial roadmap and no service-level agreement.

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

```
~/reverto/
├── core/              Shared business logic (positions, indicators, paths)
├── paper/             Paper-trading engine
├── backtest/          Backtest engine
├── live/              Live-trading scaffold
├── exchanges/         Bitget + Kraken adapters via ccxt
├── strategies/        DCA strategy + indicator implementations
├── web/               FastAPI portal (auth, dashboard, API)
├── notifications/     Telegram notifier
├── config/            Pydantic config models
└── tests/             ~1925 tests covering core paths
```

Each bot runs as a separate `main_paper.py` (or `main_live.py`) subprocess managed by the portal.

## Disclaimers

By cloning, modifying, or running this software, you acknowledge:

1. The maintainers provide no warranty, express or implied (see [LICENSE](LICENSE)).
2. You are responsible for testing and validating the software for your use case.
3. You are responsible for the security of your exchange API keys and trading capital.
4. You are responsible for regulatory compliance in your jurisdiction.
5. Past performance (backtest or paper) does not predict future results.

For the explicit declaration that this repository is published as personal-use software, see [PERSONAL_USE_DECLARATION.md](PERSONAL_USE_DECLARATION.md).

## License

Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.

## Author

Maintained by **remy1111** ([@remygit1111](https://github.com/remygit1111)).

This project is not actively maintained as a commercial product. Issues and pull requests will be reviewed when time permits, but there is no guaranteed response time.

---

*Reverto is independent software and is not affiliated with Bitget, Kraken, or any other exchange. Names and trademarks are the property of their respective owners.*
