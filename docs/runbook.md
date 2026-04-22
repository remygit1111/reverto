# Reverto Operator Runbook

Operational procedures for running Reverto in paper or (Phase-3+) live
mode. Follow these instead of hunting through source when something
needs attention.

## Machines

Reverto draait op twee machines: **Reverto-Server** voor productie
(Mele Quieter 4C mini-PC, fanless, low-power) en **Reverto-Dev** voor
development (workstation waar features worden gebouwd). Operationele
commands in dit runbook draaien op Reverto-Server tenzij expliciet
anders vermeld; remote-deploy flow vanaf Reverto-Dev staat onderaan.

## First-time setup

Bij een fresh install — of na een destructieve schema-migratie (zie
"Schema migrations" hieronder) — moet het admin-wachtwoord handmatig
worden ingesteld voordat login mogelijk is. De `users` tabel seedt
wel een admin-row, maar `password_hash` staat op `NULL`:
`verify_password()` faalt closed op NULL, dus zonder setup-admin
blijft elke login 401.

**Stap 1: install dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Stap 2: zet env-vars via `.env` (eenmalig per host)**

```bash
cp .env.example .env
# Genereer de security-keys:
python3 -c 'import secrets; print("REVERTO_API_KEY=" + secrets.token_hex(32))' >> .env
python3 -c 'import secrets; print("REVERTO_SECRET_KEY=" + secrets.token_hex(32))' >> .env
# Open .env en vul in:
# - REVERTO_API_KEY / REVERTO_SECRET_KEY: gegenereerd hierboven
# - REVERTO_INSECURE_COOKIES=1 voor lokale dev over http://localhost;
#   leeg laten in productie achter TLS reverse proxy
# - Exchange credentials (BITGET_*, KRAKEN_*) voor live/paper bots
# - Telegram tokens indien gewenst
```

`start.sh` sourcet `.env` voordat het portal-proces start.
`make(1)` gebruikt `/bin/sh` en leest geen `.bashrc`, dus `.env` is
de single source of truth — ook na `make restart` vanuit een
nieuwe SSH-sessie. `.env` staat in `.gitignore` en verlaat de host
nooit.

Zonder `REVERTO_API_KEY` / `REVERTO_SECRET_KEY` genereert Reverto
ephemeral keys met een WARNING; dat is OK voor eerste
kennismaking maar verliest alle sessies bij elke restart.

**Stap 3: initialize database**

```bash
make start          # runt init_db() die de schema op v4 zet
# wacht tot "Portal started" in logs/portal.log
# dan: Ctrl-C om te stoppen
```

Op een fresh install maakt `init_db()` lege owned-tabellen +
admin-seed zonder tussenkomst. Bij een upgrade van een eerdere
schema-versie zie "Schema migrations" voor de opt-in flow.

**Stap 4: zet admin-wachtwoord**

```bash
REVERTO_ADMIN_PW="een_sterk_wachtwoord" make setup-admin
```

Dit roept `scripts/setup_admin.py` aan die een bcrypt-hash (rounds=12)
schrijft naar `users.password_hash` voor user_id=1. Minimum lengte
is 12 tekens (`PASSWORD_MIN_LENGTH` in `core/user_store.py`, gedeeld
met `/api/auth/change-password`); kortere wachtwoorden worden
geweigerd.

Het script is idempotent — herhaald aanroepen met een andere
env-var overschrijft de hash. Let op: setup-admin bumpt **niet**
`session_epoch`, dus bestaande sessies blijven geldig tot ze
verlopen of de gebruiker expliciet uitlogt.

**Stap 5: login**

```bash
make start
make status     # confirm pid + log
```

Browse naar `http://localhost:8080` en login met username `admin`
+ het hierboven gezette wachtwoord. Sanity-check:
`curl http://localhost:8080/healthz` returns 200.

**Password wijzigen na setup**

Twee paden:

- **Via portal** (aanbevolen): login, profile-menu → Change
  Password. Dit roept `/api/auth/change-password` aan, die ook
  `session_epoch` bumpt zodat elke andere open sessie meteen
  uitlogt.
- **Via CLI**: `REVERTO_ADMIN_PW="nieuw" make setup-admin` — snel
  maar bumpt geen epoch, dus oude sessies blijven geldig.

## Schema migrations

Reverto versioneert zijn DB-schema via `PRAGMA user_version`. Bij
opstart detecteert `init_db()` de huidige versie en migreert
indien nodig.

**Non-destructive migrations** (`ADD COLUMN`, nieuwe tabellen zonder
bestaande-data-conflict) lopen automatisch bij `make start`. Geen
opt-in, geen backup nodig — er is niks dat gedropt wordt.

**Destructive migrations** (DROP + CREATE van owned tables) vereisen
een expliciete operator-opt-in sinds audit v26-10 (2026-04-20). Dit
voorkomt dat een routine `make start` na een version-upgrade stil
alle deals/orders/users wist. Bij een vereiste destructieve
migratie weigert `init_db()` en toont:

```
[FATAL] Destructive schema migration required (v3 → v4). This will
DROP owned tables (deals, orders, annotations, backtest_runs, and
user password/role/session_epoch data).
To proceed, restart with REVERTO_DESTRUCTIVE_MIGRATE=1 set. A
pre-migration backup will be created automatically at
logs/pre-migration-backup-YYYYMMDD-HHMMSS.db.
See docs/runbook.md section 'Schema migrations' for details
including restore procedure.
```

Om door te gaan:

1. **Lees de release notes** om te bevestigen welke data wordt
   gewist (in Phase-3a bijvoorbeeld: alle deal/order/annotation/
   backtest history + users.password_hash).

2. **Overweeg een handmatige backup** bovenop de automatische. De
   guard maakt zelf een backup (zie stap 4), maar een extra
   handmatige snapshot voor belangrijke data schaadt nooit:

   ```bash
   sqlite3 logs/reverto.db ".backup logs/manual-backup-$(date +%Y%m%d-%H%M).db"
   ```

3. **Start met opt-in env-var**:

   ```bash
   REVERTO_DESTRUCTIVE_MIGRATE=1 make start
   ```

4. **Locate de pre-migration backup**. De guard schrijft
   automatisch naar `logs/pre-migration-backup-YYYYMMDD-HHMMSS.db`
   via `sqlite3.Connection.backup()` (WAL-aware). Bewaar deze
   file tot je zeker weet dat het nieuwe schema correct werkt.

5. **Post-migratie: setup-admin opnieuw** als de destructieve
   migratie de users-tabel raakte (zoals v3 → v4 Phase-3a). Zonder
   een nieuwe password_hash is login weer onmogelijk. Zie
   "First-time setup" stap 4.

### Restore procedure

Als de destructieve migratie ongewenst is (bv. operator heeft per
ongeluk opt-in gezet, of de nieuwe schema blijkt bugs te hebben):

```bash
# 1. Stop de portal (Ctrl-C in make start terminal, of make stop)
make stop

# 2. Vervang de huidige DB met de pre-migration backup
cp logs/pre-migration-backup-YYYYMMDD-HHMMSS.db logs/reverto.db

# 3. Downgrade code naar de versie van vóór de migratie
git log --oneline    # zoek de laatste pre-migration commit
git checkout <sha>   # of checkout de branch van voor de upgrade

# 4. Start opnieuw — init_db() ziet een DB op de oude versie en
#    de code verwacht dezelfde versie, dus geen migratie nodig.
make start
```

De backup is een volwaardige SQLite-file; `sqlite3 <backup>.db
'PRAGMA user_version'` laat zien op welke schema-versie 'ie zit.

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

## Filesystem migration (Fase 2)

Na de DB-reset volgt de filesystem-migratie naar de multi-tenant
layout. Alle per-bot assets (YAML configs, state files, logs, PIDs,
credentials) verhuizen onder `<root>/<user_id>/` subdirs zodat
meerdere users in de toekomst niet op elkaars slugs botsen.

```bash
# 1. Stop ALLE bots via het portal (stop-all ook mogelijk).
#    Een live migratie tijdens een tick-heavy run laat state.json
#    half-gecopieerd staan.
make stop-all

# 2. Draai de migratie (idempotent — een tweede run is een no-op).
make migrate-fs

# 3. Start het portal weer. De registry scant nu config/bots/<uid>/.
make start
```

Wat verhuist (onder user_id=1):

| Van                              | Naar                                      |
|----------------------------------|-------------------------------------------|
| `config/bots/*.yaml`             | `config/bots/1/*.yaml`                   |
| `logs/*.state.json`              | `logs/1/*.state.json`                    |
| `logs/*.log`                     | `logs/1/*.log`                           |
| `logs/*.manual_trigger`          | `logs/1/*.manual_trigger`                |
| `logs/pids/*.pid`                | `logs/1/pids/*.pid`                      |
| `logs/credentials.json` + `.key` | `credentials/1/<exchange>.enc` + `keys/1.key` |

Wat NIET verhuist (system files blijven staan):

- `logs/reverto.db` + `-wal` + `-shm` (SQLite, system state).
- `logs/audit.log` + gerotateerde variants.
- `logs/portal.log`, `logs/.api_key_ephemeral`.
- `logs/.credentials.key` + zijn `.bak.*` backups (system Fernet
  key voor eventuele portal-level encrypted files).
- `logs/credentials.json` wordt geleegd wat exchanges betreft (de
  plaintext daaruit is in de per-user `.enc` files beland), maar
  het bestand zelf wordt niet verwijderd — operator doet dat na
  handmatige verificatie dat alle exchanges correct werken.
- `logs/.auth.json` (pre-Phase-3a auth blob) wordt door `init_db()`
  automatisch naar `.auth.json.pre_phase3.<ts>` gearchiveerd zodra
  de portal de eerste keer boot na de v4 migratie.

Backups worden NIET automatisch gemaakt door `migrate-fs` — de
migratie is een move, niet een copy. Bij twijfel maak eerst een
`git tag` of handmatige `tar -cz` van `config/bots/` en `logs/`.

Na de migratie heeft de bots-startup-flow een `--user-id` argument
(default 1). Het portal geeft het expliciet mee aan
`main_paper.py` / `main_live.py` zodat de subprocess exact weet
welk user tree hij bedient.

## Startup checklist (fresh machine)

Zie "First-time setup" bovenaan dit document voor de volledige
flow (install → env-vars → init_db → setup-admin → login).
Samenvatting voor wie al bekend is:

```bash
# 1. Venv + deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Env vars via .env (start.sh sources this before launching)
cp .env.example .env
python3 -c 'import secrets; print("REVERTO_API_KEY=" + secrets.token_hex(32))' >> .env
python3 -c 'import secrets; print("REVERTO_SECRET_KEY=" + secrets.token_hex(32))' >> .env
# Edit .env to add REVERTO_INSECURE_COOKIES=1 for localhost dev,
# plus exchange + Telegram credentials if needed.

# 3. Init database + set admin password
make start          # runs init_db(); stop after "Portal started"
REVERTO_ADMIN_PW="een_sterk_wachtwoord" make setup-admin

# 4. Start portal
make start
make status   # confirm pid + log
```

Sanity: `curl http://localhost:8080/healthz` returns 200.
Login: `http://localhost:8080`, username `admin` + het gezette
wachtwoord.

## Graceful shutdown

```bash
make stop        # stops portal only; bots keep running
make stop-all    # stops portal + every bot via SIGTERM
```

`make stop-all` waits up to 10s for each bot to flush Telegram
notifications via its `notify_stop` / `notify_shutdown` queue drain.

## Remote deployment from Reverto-Dev

Reverto draait op twee machines: **Reverto-Server** (Mele Quieter 4C,
productie, IP `192.168.178.227`) en **Reverto-Dev** (development
workstation, waar nieuwe features worden gebouwd). De deploy-flow:

1. Code-werk gebeurt op Reverto-Dev.
2. Feature wordt gemerged naar `main` via PR (CI draait op 3.12 +
   3.13 matrix vóór merge).
3. Reverto-Server trekt de nieuwe main binnen via SSH vanaf
   Reverto-Dev:

```bash
ssh bot@192.168.178.227 'cd ~/reverto && make deploy'
```

`make deploy` doet **alleen** `git pull origin main` met
informatieve echo-berichten. Het raakt bewust niet:

- **Geen automatische portal-restart** — code-wijzigingen die de
  portal-code zelf raken vereisen een handmatige `make restart` op
  de server. Voor pure config-wijzigingen of doc-updates is geen
  restart nodig.
- **Geen automatische bot-restart** — welke bots (en in welke
  volgorde) herstart worden is een operator-beslissing via de
  portal-UI. Automatisering hiervan vereist een aparte design-
  sessie over bot-state-preservation (open-deal-hydration volgorde,
  drawdown-guard state, indicator-warmup) en valt buiten de scope
  van een triviale deploy-wrapper.
- **Geen schema-migratie opt-in** — als de nieuwe code een
  destructive schema-migratie vereist (zie "Schema migrations"
  hieronder) weigert `init_db()` te boot'en zonder expliciete
  `REVERTO_DESTRUCTIVE_MIGRATE=1` env-var. `make deploy` zet die
  flag **NOOIT** automatisch — destructive migrations horen
  expliciet, niet via een routine-deploy.

Na een succesvolle `git pull` toont het deploy-target de vervolg-
stappen:

```
[deploy] git pull complete.
[deploy] Next steps (manual):
  - Restart het portal als code-wijzigingen dat vereisen:
      make restart
  - Herstart relevante bots via de portal-UI
  - Bij schema-migration prompts: zie docs/runbook.md
    sectie 'Schema migrations' voor de opt-in flow
```

Ssh-keys tussen Reverto-Dev en Reverto-Server moeten vooraf zijn
uitgewisseld zodat de remote `make deploy` zonder
password-prompt loopt. Bij elke rebuild van één van de hosts is dit
een eerste-setup stap.

### Bij een destructive schema migration

Als `make deploy` gevolgd door `make restart` het `[FATAL]` bericht
over destructive migration triggert, is de flow:

1. Log SSH'd in op Reverto-Server (niet via `ssh '... && make
   deploy'`, maar een interactieve sessie) — de operator moet de
   melding zien en een handmatig oordeel vellen.
2. Volg de procedure in "Schema migrations" hierboven: optionele
   handmatige backup, dan `REVERTO_DESTRUCTIVE_MIGRATE=1 make
   start`.
3. Bij schema-migraties die users-tabel raken (bv. v3 → v4 Phase-3a):
   ook `make setup-admin` opnieuw draaien met
   `REVERTO_ADMIN_PW="..."`.

Dit proces is bewust handmatig — een remote SSH-één-liner die
destructive migrations zonder operator-bevestiging doorloopt, is
precies het scenario dat de `REVERTO_DESTRUCTIVE_MIGRATE` guard
(audit v26-10) voorkomt.

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

## Wipe deals (complete data reset)

Use case: parity-test afronden, staging reset, debug na een corrupte
state. Gooit **ALLE** deal / order / annotation history weg — NIET
idempotent. Altijd eerst een backup van `logs/reverto.db`.

Voorbereiding:

- Stop ALLE draaiende bots (`make stop-all` of via het portal).
- Check geen alive PID-files meer: `ls logs/<user_id>/pids/`.

Executie:

```bash
make wipe-deals
```

Wat het doet:

1. Acquireert exclusieve `fcntl.flock` op `logs/.wipe.lock` —
   concurrent wipe-aanroepen worden geblokkeerd (`RuntimeError:
   Another wipe operation is already in progress`).
2. Scant `logs/<user_id>/pids/*.pid` + `os.kill(pid, 0)`; als één
   proces alive is, aborts met een overzicht van wat nog draait.
3. `DELETE FROM orders` → `DELETE FROM deals` (orders eerst i.v.m.
   FK op deals) → `VACUUM`.
4. Reset elke `logs/<user_id>/<slug>.state.json`: `balance_btc`
   terug naar `initial_balance_btc`, `open_deals=[]`,
   `closed_deals=[]`, `*_count=0`. Overige velden blijven staan.

Backups:

- Het script maakt GEEN DB-backup automatisch. Draai vooraf
  `cp logs/reverto.db logs/reverto.db.pre_wipe.$(date +%s)` als je
  rollback-optie wilt.
- Per-state.json wordt een `*.state.json.pre_wipe_backup` copie
  geschreven vóór de reset (overschreven bij herhaalde wipes).

Na de wipe: bots kunnen opnieuw gestart worden. Ze pakken de
resetted state.json op (open=0, closed=0).

Als `RuntimeError: already in progress` oneindig aanhoudt zonder dat
er daadwerkelijk een wipe draait: `fuser logs/.wipe.lock` om te
checken, daarna eventueel `rm logs/.wipe.lock` (lock-file is safe om
te verwijderen als geen enkel proces hem openhoudt — flock is
kernel-advisory, stale files schaden niks).

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

## Bot copy / export / import

### Duplicate bot

Portal → **Bots** → ⋮ menu op een bot-card → **Duplicate**. Prompt
vraagt om een nieuw slug (alleen `[A-Za-z0-9_-]+`). Server-side
kopie — géén deal-history, géén state, géén credentials
meegekopieerd. De duplicate start met lege state en moet zelf
gestart worden via het portal.

### Export bot config

Portal → **Bots** → ⋮ → **Export**. Browser downloadt
`<slug>.yaml` met een metadata-header (Reverto git SHA, export
timestamp, origineel slug). Alleen strategy — geen credentials,
geen state.

### Import bot config

Portal → **Bots** pagina → **Import Bot** knop naast **New Bot**.
Upload een `.yaml` of `.yml` bestand. Prompt vraagt om een target
slug (default: filename zonder extensie). Validatie via
`config.models.BotConfig` — malformed YAML of schema-conflict
geeft een toast met de exacte foutboodschap.

Naam-conflict (target slug bestaat al): response 409, prompt
verschijnt opnieuw voor een andere slug. De import schrijft pas
naar disk ná een geslaagde Pydantic-validatie, dus een half-
gevalideerd config komt nooit op disk terecht.

Gebruikscases: strategy-experimenten zonder risico op de
parity-test, configs delen tussen omgevingen, template-workflows
("copy 'RSI 5m' om een 'RSI 15m' variant te maken").

## Log level override

Bot-subprocesses loggen standaard op INFO. Voor retrospective
DEBUG-info (bv. "waarom triggerde deze deal niet?"):

```bash
REVERTO_LOG_LEVEL=DEBUG make restart
```

Dat (her)start ALLE bots met DEBUG-level. Per-tick indicator
evaluaties, timeframe snapshots, en interne state-transities komen
dan in `logs/<user_id>/<slug>.log` terecht. Log-files groeien
~20× sneller (~30 MB/dag/bot vs. ~1.5 MB op INFO).

Terugschakelen:

```bash
make restart
```

(zonder de env-var) gebruikt weer de default.

Portal-UI heeft per bot-log tab ook een filter-dropdown (ALL /
WARNING + ERROR). Dat is **client-side visibility** — het
beïnvloedt niet wat de engine naar disk schrijft, alleen wat de
browser toont.

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
| Live bot crashes with `BITGET_PASSPHRASE required` | Env var missing from `.env` (or portal was started before it was added) | Add `BITGET_PASSPHRASE=...` to `.env` and `make restart` |
| LiveEngine preflight: "Worst-case DCA" / "Cumulative DCA" | Multiplier × max_orders produces an order beyond cap | Lower `multiplier`, `max_orders`, or set `dca.max_cumulative_size` explicitly |
| Drawdown triggered unexpectedly | Peak anchored low after restart (pre-persistence YAML) | Verify `drawdown_guard.peak_value` in `state.json`; reset via API endpoint |
| Deal opened on wrong side | Old YAML missing `direction`; engine used to default to `long` | Set `direction: long` or `direction: short` in YAML explicitly |
| Every API call returns 401 | `REVERTO_API_KEY` missing from `.env` at portal start | Add to `.env` and `make restart` (ephemeral key is discarded) |

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

## Dependency upgrades

### ccxt upgrade procedure

ccxt is de primary exchange-library. Audit v26-13 flagt dat we
geen documented upgrade-cadence hadden; deze sectie vult dat gat.
Procedure bij een upgrade:

1. Check de [ccxt CHANGELOG](https://github.com/ccxt/ccxt/blob/master/CHANGELOG.md)
   op breaking changes sinds de huidige pin in `requirements.txt`.
2. Bij een minor version bump (bijv. 4.5.x → 4.6.x): review
   breaking changes specifiek voor de supported exchanges
   (Bitget, Kraken). ccxt publiceert patches per exchange-rij;
   alleen de relevante rijen lezen.
3. Bij een major version bump (bijv. 4.x → 5.x): draai de full
   test-suite én een manuele smoke-test op paper-trading voor
   minimaal 1 week voordat je de upgrade naar main merged. Major
   bumps van ccxt breken vaker dan de ccxt-team admits.
4. Update de pin in `requirements.txt`.
5. Documenteer de upgrade reasoning in de commit-message; verwijs
   naar de specifieke ccxt-CHANGELOG entry die de motivator was.

### requirements cadence

- **Core** (`requirements.txt`): review elke 3 maanden op CVE's
  via `pip-audit --strict`. Bij een blocking CVE: fix meteen
  volgens de pip-audit strategie hierboven.
- **ML** (`requirements-ml.txt`): alleen upgraden wanneer
  training-resultaten aantoonbaar verbeteren door een nieuwere
  versie, of een CVE-fix vereist is. ML-deps drijven af van core
  als je ze onafhankelijk upgrade — zie constraint-pinning
  hieronder.

### requirements-ml.txt constraint pinning

Sinds audit v26-26 staat bovenaan `requirements-ml.txt` een
`-c requirements.txt` regel. Die zorgt dat shared transitive
dependencies (numpy, pandas, scipy, etc.) in de ML-context
dezelfde versies krijgen als in core. Zonder deze constraint kan
een ML-install een nieuwere numpy oplossen, wat runtime-
incompatibilities veroorzaakt zodra het ML-subsystem in hetzelfde
proces als de paper-engine draait.

Als je bewust een hogere versie in ML wilt dan in core: upgrade
eerst core (inclusief compatibility-check), commit, pas dan ML.
Niet andersom.

## Backup procedure

Nightly cron (recommended):

```cron
0 3 * * * cd /home/bot/reverto && tar czf /backup/reverto-$(date +\%Y\%m\%d).tgz logs/reverto.db logs/.credentials.key logs/credentials.json config/
```

Restore: stop portal + bots, untar into a fresh clone, start portal.
Credential rotation: restore `.credentials.key.bak` if the rotation
itself is what you're rolling back.
