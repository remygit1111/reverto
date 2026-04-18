# Reverto Operator Runbook

Operational procedures for running Reverto in paper or (Phase-3+) live
mode. Follow these instead of hunting through source when something
needs attention.

## Database reset (multi-tenant migration)

Voor de migratie van pre-MT (schema ≤ 2) naar v3 is een eenmalige
DB reset vereist. Het v3 schema voegt een `users` tabel toe en
plaatst een `user_id NOT NULL FK` op elke owned tabel — die kan niet
idempotent toegevoegd worden aan bestaande rijen, dus we gooien
owned tabellen weg en herbouwen ze.

```bash
make reset-db    # backupt logs/reverto.db + *.state.json naar .pre_mt.<ts>
make start       # portal boot, init_db() herbouwt op v3
```

Bot YAML configs (`config/bots/*.yaml`) worden NIET aangeraakt —
die overleven de reset en worden door de portal vers gelezen. De
backup-files in `logs/` blijven staan totdat de operator ze expliciet
opruimt; de `.pre_mt.<timestamp>` suffix maakt ze sorteerbaar en
voorkomt dat herhaalde resets elkaar overschrijven.

De migratie kicks in automatisch zodra `init_db()` een DB detecteert
met `PRAGMA user_version < 3`. Als je dat pad liever zelf in de
hand houdt, draai eerst `make reset-db` zodat er geen rijen zijn
om kwijt te raken. De `_migrate_schema` warning-log signaleert de
drop expliciet.

## Startup checklist (fresh machine)

```bash
# 1. Venv + deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Production-grade env vars (paste into ~/.bashrc)
export REVERTO_API_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export REVERTO_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
# For localhost dev only:
export REVERTO_INSECURE_COOKIES=1

# 3. Start portal
make start
make status   # confirm pid + log
```

Sanity: `curl http://localhost:8080/healthz` returns 200.

## Graceful shutdown

```bash
make stop        # stops portal only; bots keep running
make stop-all    # stops portal + every bot via SIGTERM
```

`make stop-all` waits up to 10s for each bot to flush Telegram
notifications via its `notify_stop` / `notify_shutdown` queue drain.

## Live bot dry-run via het portal (Phase 1)

Onder Phase 1 zijn live-mode bots alleen toegestaan in dry-run: de
runner gebruikt de echte exchange client voor ticker-data maar
weigert `_place_market_order` in non-dry-run. Het portal laat je
dit nu starten zonder via `make live-dry` te hoeven.

- **Overview**: bots met `mode: live` in hun YAML tonen een oranje
  **▶ Start dry-run** knop (geen groene Start). Klik → bevestig in
  het dialog → portal spawnt `main_live.py --bot <slug> --dry-run`
  met `DRY_RUN=1` in de env (confirmation prompt wordt overgeslagen).
- **Running state**: het "Running" pill krijgt een gele banner
  **🟡 DRY RUN — no real orders placed** zolang de bot draait. Dit
  blijft staan tot Phase 3 real execution toelaat.
- **Stop / Restart**: werken ongewijzigd. Restart is mode-aware —
  een live bot herstart weer als dry-run, een paper bot als paper.
- **API**: `POST /api/bots/<slug>/start-dry-run` (auth, rate-limited
  20/min, audited als `bot_start_dry_run`). Paper-mode bots worden
  door de helper geweigerd met een duidelijke foutmelding in plaats
  van stil subprocess-exit.

Alternatief blijft `make live-dry BOT=<slug>` voor ops-flows zonder
portal.

## Parity testing

Om te verifiëren dat het paper engine een betrouwbare proxy is voor
live trading, draai een paper bot en een live-dry bot parallel met
identieke strategie config (alleen `mode` en `name` verschillen).
Na ≥ 1 week produceert `scripts/parity_compare.py` een side-by-side
diff van de deals:

```bash
make parity-compare PAPER=rsi_paper_test LIVE=rsi_real_test
# of met een ingekorte periode:
make parity-compare PAPER=rsi_paper_test LIVE=rsi_real_test SINCE=2026-04-18
```

Het rapport (Markdown op stdout, JSON met `--json`) toont:

- **Summary** — aantal deals per bot, matched pairs, match rate,
  gemiddelde timing Δ / price Δ / PnL Δ, PnL-correlatie (≥ 10 pairs).
- **Flags** — per-pair warnings (`timing_warn > 30s`, `price_warn
  > 10 bp`, `pnl_warn > 0.5 pp`, `exit_mismatch`, `dca_mismatch`).
- **Unmatched tables** — deals die uitsluitend in één engine
  voorkwamen; bruikbaar om entry-filter flakiness op te sporen.
- **Interpretation** — gedrempelde één-regel-oordelen zodat je niet
  handmatig elke metric hoeft te interpreteren.

Matching algoritme: greedy nearest-neighbour op `opened_at`, venster
standaard 120 s (`--window` om te tweaken). Elk live deal kan maar
één keer gematcht worden.

Het script is side-effect-free (alleen SELECTs) — draai het op een
productie-DB zonder risico.

## Emergency stop

Portal → profile menu → **🛑 Emergency stop**. Confirm the dialog.
Or via API:

```bash
curl -X POST -H "X-API-Key: $REVERTO_API_KEY" \
     http://localhost:8080/api/emergency-stop
```

Effect: every running bot gets SIGTERM. Open positions on the exchange
are **not** auto-closed — the operator reconciles manually if needed.
Recommended when a drawdown alert fires or a suspected runaway DCA.

## State file recovery

When a state file is corrupt (malformed JSON, partial write):

```bash
# 1. Identify corrupt file
ls -la logs/<slug>.state.json
# 2. Check for orphan .tmp
ls -la logs/<slug>.state.tmp
# 3. Start bot (engine's _load_state sweeps orphan .tmp and
#    falls back to clean state on JSON parse failure)
```

`_load_state` logs a warning on parse failure and starts with a clean
PaperState. The closed-deal history stored in `logs/reverto.db` is
still intact.

## Drawdown guard

Reset after a triggered guard:

```bash
curl -X POST -H "X-API-Key: $REVERTO_API_KEY" \
     http://localhost:8080/api/bots/<slug>/drawdown/reset
```

This rewrites `state.json` to `triggered=false, peak=null`. The bot
picks it up on the next tick (or a bot restart if the reset happens
while the process is stopped).

Guard config lives in the bot YAML:

```yaml
drawdown_guard:
  enabled: true
  max_drawdown_pct: 10.0     # % drop from peak that triggers
  metric: equity              # equity | balance
  action: pause               # pause (skip new entries) | stop
```

## Credential rotation

```bash
# 1. Stop every bot so no engine reads mid-rotation
make stop-all

# 2. Rotate
.venv/bin/python scripts/rotate_credentials.py

# 3. Verify
cat logs/credentials.json | python3 -m json.tool     # ciphertext changed
ls -la logs/.credentials.key.bak                     # 7-day rollback copy

# 4. Restart portal
make start
```

If step 2 fails mid-way: the old key + old ciphertext are still valid
(rotate is atomic: key first, then creds). If the key rotated but the
creds didn't, restore the creds from `logs/credentials.json.tmp` or
re-save via the portal (Exchanges → Add Keys).

## Log analysis

- `logs/portal.log` — portal lifecycle + API requests.
- `logs/audit.log` — start/stop/restart/drawdown-reset/emergency-stop.
- `logs/<slug>.log` — per-bot output (stdout + stderr + Python logger).

External `logrotate` is the recommended rotation strategy for bot
logs — the portal log uses `RotatingFileHandler` internally, but bot
logs go through `subprocess.Popen`'s `stdout` redirect which Python's
handler can't rotate.

Sample `/etc/logrotate.d/reverto`:

```
/home/bot/reverto/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
```

## Common errors + fixes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Portal 503 on `/readyz` | SQLite unreachable (disk full, locked) | `df -h logs/`, clear WAL (`sqlite3 logs/reverto.db 'PRAGMA wal_checkpoint(FULL)'`) |
| Bot refuses to start, "Invalid bot slug" | `--config` path or `--bot` slug contains `..`, `/`, or spaces | Rename YAML to `[A-Za-z0-9_-]+.yaml` |
| Live bot crashes with `BITGET_PASSPHRASE required` | Env var not exported | Add `export BITGET_PASSPHRASE=...` to `~/.bashrc` |
| LiveEngine preflight: "Worst-case DCA" / "Cumulative DCA" | Multiplier × max_orders produces an order beyond cap | Lower `multiplier`, `max_orders`, or set `dca.max_cumulative_size` explicitly |
| Drawdown triggered unexpectedly | Peak anchored low after restart (pre-persistence YAML) | Verify `drawdown_guard.peak_value` in `state.json`; reset via API endpoint |
| Deal opened on wrong side | Old YAML missing `direction`; engine used to default to `long` | Set `direction: long` or `direction: short` in YAML explicitly |
| Every API call returns 401 | `REVERTO_API_KEY` env not set at portal start | Export and `make restart` (ephemeral key is discarded) |

## CI / pip-audit strategie

Het `.github/workflows/test.yml` draait twee aparte pip-audit passes:

- **Direct deps (blocking)** — scant `requirements.txt` en
  `requirements-ml.txt`. Een CVE in een expliciet gepinde dependency
  faalt de build. Direct deps staan onder onze controle: versie bumpen,
  tests draaien, committen is de standaard-respons.
- **Transitive deps (non-blocking)** — volledige scan van de
  geïnstalleerde site-packages. Transitive vulnerabilities kunnen
  vaak upstream-coordinatie vereisen en mogen een PR niet blokkeren.
  Output zichtbaar in de GitHub Actions logs; beoordeel elk kwartaal
  of een upgrade nodig is.

Handmatige scan vanaf je werkstation:

```bash
# Wat CI draait als "blocking":
.venv/bin/pip-audit \
    --requirement requirements.txt \
    --requirement requirements-ml.txt \
    --strict

# Wat CI draait als "non-blocking":
.venv/bin/pip-audit
```

Bij een blocking failure:

1. Identificeer de kwetsbare package + versie.
2. Kies de minimale versie die de CVE dicht (meestal vermeld in het
   pip-audit rapport).
3. Update `requirements.txt` of `requirements-ml.txt`.
4. `.venv/bin/pip install -r requirements.txt` + `make test`.
5. Commit met audit-ID in het bericht.

## Backup procedure

Nightly cron (recommended):

```cron
0 3 * * * cd /home/bot/reverto && tar czf /backup/reverto-$(date +\%Y\%m\%d).tgz logs/reverto.db logs/.credentials.key logs/credentials.json config/
```

Restore: stop portal + bots, untar into a fresh clone, start portal.
Credential rotation: restore `.credentials.key.bak` if the rotation
itself is what you're rolling back.
