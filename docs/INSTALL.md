# Installing Reverto

This guide takes a self-hoster from a clean machine to a running
Reverto portal with one paper bot. It assumes you are comfortable
on a Linux command line, have used `pip` and `venv` before, and
can read a `.env` file. It does NOT cover trading-strategy design,
exchange-specific quirks, or production-grade hardening. Those
are in [CONFIGURATION.md](CONFIGURATION.md), [exchange-permissions.md](exchange-permissions.md),
and [OPERATIONS.md](OPERATIONS.md) respectively.

For ongoing maintenance after install, switch to
[OPERATIONS.md](OPERATIONS.md). For configuration reference,
[CONFIGURATION.md](CONFIGURATION.md).

## Requirements

- **Python** 3.12+. Older Pythons miss type-hint syntax used in
  the codebase.
- **Linux**. Tested on Ubuntu 24.04 and Ubuntu under WSL2. macOS
  may work but is not tested; Windows native is not supported.
- **~500 MB** of disk space for the venv + repo + a few weeks of
  logs and backups.
- **A Bitget or Kraken account with API keys.** Paper-mode bots
  read live ticker prices from the exchange's public endpoints
  and need no keys; once you save credentials they unlock live
  bots and authenticated paper-mode features. See
  [exchange-permissions.md](exchange-permissions.md) for the
  exact permissions to enable.
- **Basic familiarity with terminal commands**: running scripts,
  editing files, reading logs.

This guide does not cover Docker. A Docker reference setup is
sketched in [deployment.md](deployment.md); this guide focuses
on the bare-metal path that the codebase is exercised against.

## Quick start (local development)

These steps get you to a working portal on `http://localhost:8080`
with a paper bot you can start.

### 1. Clone the repository

```bash
git clone https://github.com/remygit1111/reverto.git
cd reverto
```

### 2. Set up a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

The `Makefile` and the start scripts call `.venv/bin/python3`
directly, so the venv path is fixed. Don't rename `.venv`.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

Optional ML dependencies (NOT required to run the portal):

```bash
pip install -r requirements-ml.txt
```

The ML stack is a forward-looking placeholder; the engine ignores
ML config today. Skip it on a first install.

### 4. Configure environment variables

Reverto reads its environment from `.env` in the repo root.
`start.sh` sources this file before launching the portal, so
`make` (which spawns `/bin/sh` and skips `.bashrc`) sees the same
values.

Copy the template and fill it in:

```bash
cp .env.example .env
```

Generate the two security keys and append them:

```bash
python3 -c 'import secrets; print("REVERTO_API_KEY=" + secrets.token_hex(32))' >> .env
python3 -c 'import secrets; print("REVERTO_SECRET_KEY=" + secrets.token_hex(32))' >> .env
```

For local `http://localhost` development you also want:

```bash
echo "REVERTO_INSECURE_COOKIES=1" >> .env
```

Without this flag the session cookie carries the `Secure` attribute
and the browser refuses to send it over plain HTTP, so login looks
broken even though the credentials are correct.

Exchange credentials and Telegram tokens are optional at install
time. You can add them later via the portal UI. See
[CONFIGURATION.md](CONFIGURATION.md) for the full list of
environment variables.

### 5. Initialize the database

The first portal start creates the SQLite schema, seeds the admin
row, and exits cleanly when you Ctrl-C:

```bash
make start
# wait until you see "Portal started" in the logs:
tail -f logs/portal.log
# Ctrl-C the tail, then:
make stop
```

`init_db()` runs idempotently inside `main_web.py` on every start.
On a fresh install it creates the v4 schema and a single admin
row with `password_hash=NULL`. Login is blocked until you set the
password (next step).

### 6. Set the admin password

```bash
REVERTO_ADMIN_PW="a_strong_password" make setup-admin
```

This calls `scripts/setup_admin.py`, which writes a bcrypt hash
(rounds=12) to `users.password_hash` for user_id=1. The password
must be at least 12 characters; `setup_admin` rejects shorter
ones.

The script is idempotent. Re-running it with a different
password overwrites the hash. It does NOT bump `session_epoch`,
so any open sessions stay valid until they expire.

### 7. Start the portal

```bash
make start
make status   # confirm pid + log
```

Visit `http://localhost:8080` in a browser, log in with username
`admin` and the password you set in step 6.

### 8. Create your first bot

In the portal:

1. Click **+ New Bot**.
2. Give it a name (alphanumerics, spaces, dashes, underscores).
3. Pick `paper` mode and Bitget or Kraken as the exchange.
4. Pick a trading pair (e.g. `BTC/USD`), timeframe, and direction.
5. Configure DCA (base order size, max orders, spacing), TP, SL.
6. Add at least one entry indicator (RSI is a sensible default).
7. Save → start the bot from the dashboard.

The bot subprocess writes to `logs/<user_id>/<slug>.log`. The
state file at `logs/<user_id>/<slug>.state.json` is rewritten on
every tick.

If you don't yet have exchange credentials, paper-mode bots still
work. They fetch tickers from the exchange's public endpoints.
For live or live-dry bots, save credentials via the portal's
Exchanges page first; see
[exchange-permissions.md](exchange-permissions.md) for the
permissions to enable.

## Cloud VPS deployment

The bare-metal path above also works on a VPS. The differences
are minor; this section calls out what changes.

### Requirements

- A small Linux VPS (Hetzner CX22, DigitalOcean basic, AWS Lightsail,
  etc.). 2 GB RAM and 20 GB disk are comfortable for a single-user
  install. CPU is rarely the bottleneck.
- **Ubuntu 24.04 LTS** is the tested target. Other distros work
  if Python 3.12+ is available.
- A non-root user (typically `bot`) for running the portal. Do
  NOT run as root.

### Bootstrapping

SSH in as your non-root user, then follow steps 1–8 of the Quick
start above. A few VPS-specific notes:

- **Install Python and build deps**:
  ```bash
  sudo apt update
  sudo apt install -y python3 python3-venv python3-pip build-essential libffi-dev curl
  ```
- **Open port 8080** on your VPS firewall ONLY if you are
  fronting it with a reverse proxy on the same host. Do not
  expose port 8080 directly to the public internet without TLS;
  the portal carries trading credentials behind login and must
  never speak plain HTTP across an untrusted network.
- **Use a reverse proxy** like Caddy or nginx to terminate TLS
  on a real domain. Caddy is the simplest:
  ```Caddyfile
  reverto.example.com {
      reverse_proxy localhost:8080
  }
  ```
  Caddy auto-provisions Let's Encrypt certificates. nginx + certbot
  is the equivalent in a more verbose configuration.
- **Drop `REVERTO_INSECURE_COOKIES=1`** from `.env` once you are
  behind TLS. It stays unset/empty in production. The session
  cookie's `Secure` flag is non-negotiable in production.

### Keeping the portal alive across reboots

The simplest path is `make start` from a tmux/screen session, fine
for a single-operator setup that you check daily. For a
restart-on-reboot guarantee, write a tiny systemd unit. The repo
does not ship one because the right invocation depends on your
user / paths / venv layout. A minimal example:

```ini
# /etc/systemd/system/reverto.service
[Unit]
Description=Reverto portal
After=network.target

[Service]
Type=forking
User=<user>
WorkingDirectory=/home/<user>/reverto
ExecStart=/usr/bin/bash /home/<user>/reverto/start.sh
ExecStop=/usr/bin/bash /home/<user>/reverto/stop.sh
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

> Replace `<user>` with the username running the service. systemd
> unit files do not support `~`, `$HOME`, or shell variable
> expansion in `WorkingDirectory`/`ExecStart`, so absolute paths
> are required.

> Note: The files in `deploy/` and `ops/caddy/` in this repository
> contain the maintainer's own production configuration (with
> username `bot`). When self-hosting, copy them to the appropriate
> system location and adjust the paths and username to match your
> setup. Do not assume they are templates with placeholders.

`systemctl enable --now reverto` after dropping that in.
[OPERATIONS.md](OPERATIONS.md) covers shutdown semantics,
graceful-stop windows, and the auto-restart budget interaction.

### Production hardening

The hardening that matters most for a self-hosted Reverto:

- **Daily backups**: schedule `scripts/backup.sh` via cron.
  Procedure in [OPERATIONS.md](OPERATIONS.md) "Backup and restore".
- **TLS**: see the reverse-proxy paragraph above.
- **Firewall**: deny inbound everything except SSH + 443. The
  portal does not need any other inbound port.
- **Off-host backups**: `scripts/backup.sh` writes to
  `backups/` on the same host today. Mirror it elsewhere with
  `rsync`, S3, or your favourite block-storage replication; a
  ransomware event that takes the host also takes the local
  backups.

Production-ops topics live in [OPERATIONS.md](OPERATIONS.md);
this guide stops at "the portal is reachable and you can log in".

## Docker (advanced)

Reverto can run in Docker. The repo does NOT currently include a
production Dockerfile. [deployment.md](deployment.md) carries a
reference Dockerfile and `docker-compose.yml` (with Prometheus +
Grafana sidecars) that you can adapt. A self-hoster who lands on
a working Docker setup is welcome to contribute it back as a
tested addition to the repo.

If you go down the Docker path:

- Persist `logs/` and `config/bots/` as named volumes. Losing
  either wipes credentials and configs.
- Keep secrets out of the image. Use `env_file:` or Docker
  secrets to inject `.env`, never `COPY .env` into a layer.
- Front the container with a TLS terminator just like the
  bare-metal path.

## Verification

After install, confirm the install is healthy:

1. **Portal reachable**: `curl http://localhost:8080/healthz`
   returns 200. The browser login page loads at
   `http://localhost:8080/`.
2. **Login works**: username `admin` + the password from
   step 6 returns a session cookie and lands on the dashboard.
3. **Bot creation succeeds**: the wizard accepts your config
   without a 4xx response.
4. **Bot generates orders**: start a paper bot with permissive
   entry conditions; within a few minutes the dashboard shows
   the first base order in the open-deals panel and ticks update
   via the WebSocket.

If any step fails, see Troubleshooting below.

## Next steps

- [CONFIGURATION.md](CONFIGURATION.md): every env var, every
  bot-YAML field, strategy options.
- [OPERATIONS.md](OPERATIONS.md): daily ops: backups, restores,
  schema migrations, credential rotation, emergency stop.
- [exchange-permissions.md](exchange-permissions.md): which
  Bitget / Kraken API permissions to enable (and which to leave
  off).
- [architecture.md](architecture.md): codebase tour: the engine,
  the portal, the persistence layer.

## Troubleshooting

### "Python version mismatch" or syntax errors on import

The codebase uses Python 3.12+ syntax (e.g. `dict | None`,
`match` statements). Older Pythons fail at import time with a
`SyntaxError`. Confirm with `python3 --version`; if it's older
than 3.12, install a newer Python. On Ubuntu 24.04 the system
Python is 3.12.

### `pip install -r requirements.txt` fails on a build step

The `cryptography` and `bcrypt` wheels need a C toolchain on some
systems. Install build deps first:

```bash
sudo apt install -y build-essential libffi-dev python3-dev
```

Then re-run `pip install -r requirements.txt` from the active venv.

### Permission errors on first start

`logs/`, `keys/`, and `credentials/` are created by the portal
at runtime with `0700`/`0600` modes. If you cloned as one user
and run as another, those modes can land wrong. Quick fix:

```bash
chmod -R u+rwX,go-rwx logs/ keys/ credentials/ 2>/dev/null || true
```

### "Database init failed" or schema-version errors

A first start should leave `logs/reverto.db` at schema v4. If you
upgraded code that bumped the schema and now `init_db()` refuses
to boot, you have a destructive migration on your hands. See
[OPERATIONS.md](OPERATIONS.md) "Schema migrations" for the opt-in
flow + restore procedure. Don't blindly add
`REVERTO_DESTRUCTIVE_MIGRATE=1`; read the section first.

### Login returns 401 even with the right password

The most common cause is `REVERTO_INSECURE_COOKIES` not set on a
local `http://` install. The browser silently drops the session
cookie because it's flagged `Secure`. Check `.env` for the line,
restart the portal, clear the browser cookie, log in again.

The second-most-common cause is `setup-admin` not having been
run. `verify_password()` fails closed on a NULL hash, so every
login returns 401 until you provision the password. Run
`REVERTO_ADMIN_PW="..." make setup-admin` and retry.

### Ephemeral key warnings on every restart

If `REVERTO_API_KEY` or `REVERTO_SECRET_KEY` are missing from
`.env`, the portal generates throwaway keys and emits a WARNING
on startup. Sessions don't survive a restart in that mode. Add
both keys (step 4) and `make restart`.

For ongoing operational issues (bots crashing, drawdown
triggers, log-level overrides), see
[OPERATIONS.md](OPERATIONS.md) "Common errors + fixes".
