# Reverto — Forensic Audit v25

_Post-Phase-2 hardening audit · 14 commits · 9ab31b8 → 32780d6 · 2026-04-19_

## 1. Executive summary

Scope: de 14 commits sinds v24 (`61657b2` → `32780d6`), allemaal
geland binnen één werkdag (19 april 2026). Dat is 9 non-merge
commits plus 5 merges. De wijzigingen splitsen in zeven groepen:
twee v24-cleanups, een cross-bot deal-ID collision fix (grootste
van de dag), een parity-compare verbetering, een wipe-deals
uitbreiding, een favicon-fix, CI-infrastructuur, en twee
documentatie-commits (phase-3 scoping + revisie). Deze audit
bekijkt elk pad forensisch: wat was het probleem, wat is
daadwerkelijk gewijzigd, is de fix correct en regressie-veilig,
en welke nieuwe zwaktes zijn erbij gekomen.

### Score

| Audit | Score |
|---|---|
| v24 | 95.8 % |
| **v25** | **95.4 % (-0.4 pp)** |

Motivatie: twee van de drie v24 MEDIUM bevindingen zijn netjes
gesloten (MEDIUM #1 chart.py scope, MEDIUM #2 registry users-
check) met regressietests erbij. De derde (MEDIUM #3 body-size
caps) blijft onveranderd open. Daarnaast is er een forensisch
bevestigde latent-HIGH bug gevonden en gefixed (cross-bot deal-ID
collision met silent data-loss) — op zichzelf een netto positieve
week. Wat de score licht naar beneden trekt: twee nieuwe structurele
kwetsbaarheden die ten tijde van v24 niet bekend waren (DB +
state.json als twee bronnen van waarheid; `_scan_user_dirs`
fail-open bij DB-failure), een welbewust geskipte CI-test
zonder root-cause analyse, en een ontbrekend proces dat
`prometheus_client` + `xgboost` uit de pinned requirements liet
vallen ondanks dat ze productie-code zijn. Positief: 828 tests
(was 772 in v24 = +56 regressietests) en 86 % coverage.

### Top 3 bevindingen

- **MEDIUM** — `_scan_user_dirs` fail-open bij DB-failure
  (`web/app.py:595-605`). Als `get_active_user_ids()` een
  exception gooit, valt de registry terug op het oude
  integer-name-only gedrag dat commit `8f0448a` expliciet
  wilde vervangen. Phase-1 heeft één user dus niet
  exploiteerbaar, maar met meerdere users kan een transient
  DB-glitch een orphan dir alsnog als valide tenant
  toelaten. Phase-3.md §3 signaleert dit maar er is geen
  code-fix. Aanbevolen fix: fail-closed met cached
  last-known-good lijst van een eerdere succesvolle scan.
- **MEDIUM** — Session-epoch CI-test geskipt zonder root-cause
  (`tests/test_web_routes.py:287-297`).
  `test_fresh_login_after_logout_works` faalt alleen op GitHub
  Actions, niet lokaal. De workaround (`@pytest.mark.skipif(
  os.getenv("CI") == "true")`) parkeert het probleem. De skip-
  reason noemt terecht dat het niet verwijderd mag worden
  zonder begrip, maar het BEGRIP zelf is de open vraag. Zonder
  onderzoek weten we niet of het een test-bug is of een echte
  regressie in session-invalidation die alleen onder bepaalde
  omgevings-condities oppopt.
- **MEDIUM** — Architecturale zwakte: DB + state.json als twee
  bronnen van waarheid. De cross-bot deal-ID collision bug
  (`ac21b6f`) én de onvolledige wipe-deals (`4c1a233`) zijn
  beide symptomen van deze onderliggende design-keuze. Commit
  `4c1a233` benoemt het expliciet in de wipe_deals.py docstring
  maar adresseert alleen het symptoom. Zolang beide bronnen
  onafhankelijk muteren blijft het potentieel voor divergentie
  bestaan (portal-UI toont X, DB zegt Y). Niet op te lossen in
  deze audit-cyclus — Phase-3 scoping werk.

### Verdict

**CONDITIONAL READY** voor Phase-3 implementatie-start.

Er is geen blocker voor het begin van het Phase-3 werk (credential-
migratie, role-kolom, `_request_user` bridge). Wel drie items die
BEFORE een publieke lancering geadresseerd moeten zijn: de
session-epoch CI-test blinde vlek, de body-size cap gap op
`POST /api/bots` + `PUT /api/bots/{slug}/config`, en een proces
om te voorkomen dat productie-dependencies weer ontbreken in
requirements.txt. De cross-bot collision fix zelf is technisch
correct uitgevoerd en ruim getest.

## 2. Regressietoets v24 → v25

| v24 finding | Status | Bewijs |
|---|---|---|
| **MEDIUM #1** `/api/price` fallback zonder user filter (chart.py:64) | **CLOSED** | `web/routes/chart.py:65` — endpoint vereist nu `user: User = Depends(_request_user)`, fallback-loop is `for bot in await registry.all(user_id=user.id)`. Commit `67a136a`. Regressietest: `tests/test_chart_routes.py` is uitgebreid met user-scope assertions. |
| **MEDIUM #2** `_scan_user_dirs` trust integer subdirs zonder users check | **CLOSED (with caveat)** | `web/app.py:592-594` — cross-check `get_active_user_ids()` toegevoegd, orphan dirs krijgen WARNING + skip. Commit `8f0448a`. Regressietest `tests/test_registry_composite_key.py::TestOrphanUserDirs` (4 tests). Caveat: fail-open branch (regels 595-605) is de nieuwe MEDIUM in deze audit — zie §7. |
| **MEDIUM #3** body-size cap niet op POST `/api/bots` + PUT config | **OPEN** | `web/routes/bots.py:369-401` `create_bot` accepteert `body: dict` zonder Content-Length check. Idem `PUT /api/bots/{slug}/config` op regel 422. Alleen `/api/bots/validate-config` heeft de 64 KB cap. Niet aangeraakt in 14 commits. |
| **LOW #4** `credentials/` parent dir mode 0755 | **OPEN** | `core/paths.py:83-85` — `_ensure_dir(BASE_DIR / "credentials" / str(user_id), mode=0o700)`. `Path.mkdir(parents=True)` creëert tussenliggende dirs met de default umask, niet met de `mode` parameter (Python docs expliciet). De parent `credentials/` blijft dus 0755. Niet aangeraakt. |
| **LOW #5** `logs/pids/` blijft leeg post-migratie | **OPEN** | `scripts/migrate_to_user_fs.py:134-147` — `migrate_pid_files` verhuist de files maar laat de lege `logs/pids/` dir staan. Geen `rmdir if empty`. Niet aangeraakt. |

Twee van drie MEDIUMs zijn gesloten, beide met expliciete
regressietests. MEDIUM #3 en beide LOWs blijven staan — ze worden
opnieuw opgevoerd in §7 van dit document.

## 3. Nieuwe commits — forensisch per categorie

### 3.1 v24 cleanup — chart.py scope + registry users-check

**`67a136a` `fix(chart): scope /api/price fallback to requesting user`**

Diff: `web/routes/chart.py` + `tests/test_chart_routes.py`. De
originele fallback-lus was:

```python
for bot in await registry.all():
    state = bot.read_state()
    ...
```

Na de fix:

```python
for bot in await registry.all(user_id=user.id):
    ...
```

Plus het endpoint importeert nu `_request_user` en voegt
`user: User = Depends(_request_user)` toe aan de signature.
Extra: bij lege fallback returned het endpoint nu `503` in plaats
van een fake `{"price": 0.0, "source": "unavailable"}` — de
frontend tekende anders een lege grafiek alsof 0.0 een echte
koers was. Tweezijdige verbetering.

Regressie-dekking: `tests/test_chart_routes.py` — 12 tests, 98%
coverage. De fallback-paden zelf zijn niet diep getest omdat
ze externe Bitget-fetch mocken; de scope-assertie (user 1's
bot mag user 2's state.json niet lezen) is echter wel gepind.
Geen zorgen.

**`8f0448a` `fix(registry): cross-check _scan_user_dirs against users table`**

Diff: `web/app.py` + `core/user.py` + `tests/test_user_model.py`
+ `tests/test_registry_composite_key.py`. Nieuwe helper
`get_active_user_ids()` in `core/user.py:97-112` doet
`SELECT id FROM users WHERE active = 1` en returnt een `set[int]`.
`_scan_user_dirs` roept 'm aan per refresh (5 s TTL) en skipt
integer-named dirs die niet in de active-set zitten, met een
WARNING. Zie §2 voor de fail-open caveat op deze implementatie.

Regressie-dekking: `TestOrphanUserDirs` met 4 scenarios (orphan
integer-dir skipped met WARNING, legitieme user blijft scannen,
mixed orphan+valid, inactive user behandeld als orphan). Plus
`TestGetActiveUserIds` (4 tests) in `test_user_model.py`.
Coverage-depth is goed; de enige ontbrekende case is het
fail-open pad zelf, en dat is precies waarom het als nieuwe
MEDIUM opgevoerd wordt in §7.

### 3.2 Cross-bot deal-ID collision fix (`ac21b6f`) — grootste wijziging

Dit is de meest ingrijpende commit van de werkdag: +909 regels,
15 files, nieuwe module `core/ids.py`, nieuwe regressie-test-file
met 6 tests, plus 12 andere tests in `test_ids.py`.

**Root cause** (bevestigd via productie-trace in
`parity_test_start.txt` flow):

- `paper/paper_state.py:133` produceerde `f"PAPER-{counter:04d}"`
  met een **per-instance** counter. Elke bot begon bij
  PAPER-0001.
- `core/deal_store.py` gebruikte `INSERT OR REPLACE INTO deals`.
  Wanneer twee bots dezelfde deal.id aanleverden vuurde
  SQLite zonder waarschuwing een DELETE+INSERT: rij van bot A
  verdween, rij van bot B nam zijn plek in. Geen IntegrityError,
  geen log, geen exception.

**Verificatie bij live parity-test**: rsi_paper_test opende een
deal om `07:39:57`, rsi_real_test om `07:42:16`. Beide kregen
ID `PAPER-0001`. Resultaat in de DB: alleen rsi_real_test's
rij, met timestamp 07:42:16. rsi_paper_test's rij was
volledig weg — alleen in zijn `state.json` nog zichtbaar.
ML-pipeline `load_deal_history` trainde op deze gecorrupteerde
dataset; parity-compare produceerde een misleidend
"paper=0 live=1" rapport.

**Fix — nieuwe ID-generator** (`core/ids.py`):

```python
DEAL_ID_RE = re.compile(r"^\d{12}-\d{4}$")

def generate_deal_id(now_utc: datetime | None = None) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    prefix = now_utc.strftime("%Y%m%d%H%M")
    suffix = f"{random.randint(0, 9999):04d}"
    return f"{prefix}-{suffix}"
```

Format: `YYYYMMDDHHMM-RRRR` (12 digits UTC + 4 digits random).
Time-sortable als string. 10 000 slots per minuut. Import-
plaatsen geüpdatet op drie regex-ingress-sites (`web/app.py:371`,
`paper/paper_engine.py:40`, `web/routes/deals.py`) via re-export
van `core.ids.DEAL_ID_RE`.

**Fix — INSERT OR REPLACE geëlimineerd**:
`core/deal_store.py` opgesplitst in drie primitives:

- `create_deal(...)` — `INSERT INTO deals (...)`, raises
  `sqlite3.IntegrityError` bij duplicate PK.
- `update_deal(...)` — `UPDATE deals SET ... WHERE id = ? AND
  user_id = ? AND bot_slug = ?` — returnt `rowcount`.
- `save_deal(...)` — upsert wrapper: probeert eerst UPDATE, valt
  terug op INSERT als `rowcount == 0`. Cross-owner collisie
  raised nog steeds IntegrityError via de INSERT-val.

Alle 18 call-sites geïnspecteerd (grep in audit-uitvoering):
geen enkele `INSERT OR REPLACE` overgebleven in productie-code
(alleen in comment-strings). `save_order` werd `INSERT`-only
(geen REPLACE), `replay_deals_in_transaction` werd
`INSERT OR IGNORE` — de JSON→DB migratie loopt op elke restart
en re-migreert dezelfde rijen, dat moet idempotent blijven.

**Retry-on-collision in paper-engine**
(`paper/paper_engine.py:273-324`): nieuwe `_db_create_deal_with_retry`
method, max 3 attempts. Bij `IntegrityError` wordt
`deal.id = self.state.new_deal_id()` en opnieuw geprobeerd. Op
uitputting ERROR-log + `return False`, en `_open_deal` refuseert
de open — in-memory state blijft schoon zodat er nooit een
deal in de PaperState dict staat zonder DB-tegenhanger.

**Statistische analyse collision-probabiliteit**:

- 10 000 slots per minuut.
- Bij de parity-test-belasting (2 bots, elke ~5 min één deal):
  2/min piek → birthday-prob ≈ 2²/(2·10000) = 2e-4, effectief 0.
- Bij 100 bots, elke bot 1 deal/5 min = 20 opens/min over alle
  bots: birthday-prob in dezelfde minuut ≈ 20²/20000 = 2e-2 (2 %).
- Met 3-attempt retry wordt de kans op een onherstelbare
  failure ~(0.02)³ = 8e-6 per sequence.
- Bij 20/min × 60 × 24 = 28 800/dag → eens per ~125 000 uur =
  ~14 jaar. Acceptabel voor een trading-platform.

**Edge case gevlag in §7**: NTP-correctie die de clock
terug-springt. Als de clock ≥ 1 minuut teruggaat kunnen we
dezelfde `YYYYMMDDHHMM` prefix opnieuw bezoeken binnen dezelfde
minuut-"slot". Correct gedrag: DB UNIQUE op `deals.id` vangt
de collision, retry pakt het op. Niet kritisch maar mag
genoemd.

**Regressietest `test_simulated_two_bots_concurrent_open`**
(`tests/test_cross_bot_deal_isolation.py:209-254`): dekt het
exacte bug-scenario. Twee PaperEngines met verschillende
slugs, `patch("core.ids.datetime")` zodat beide
`generate_deal_id` dezelfde UTC-minuut zien, beide roepen
`_open_deal` aan. Assert beide rijen in DB staan, ids
verschillen, beide matchen `DEAL_ID_RE`. Test slaagt op
HEAD — gevalideerd.

**Data-loss audit-trail**: na de fix heeft de operator
`make wipe-deals` gedraaid (volgens runbook; zie §3.4).
Backups in `.pre_wipe_backup` format. Geen pre-fix
state-snapshot bewaard buiten de state.json backups, dus
voor specifieke "welke deals zijn verdwenen" analyse is er
alleen de historische flow via git blame op paper/paper_state.py
— onvoldoende voor een forensisch compensatie-proces. In de
praktijk was de DB al gecorrumpeerd en paper-trading dus niet
in reel-geld termen relevant; impact is ML-trainingsdata
kwaliteit die nu opnieuw moet worden opgebouwd.

**Zorg**: `_db_create_deal_with_retry` muteert `deal.id` in
place (regel 302). Dat is veilig omdat de deal nog niet in
`state.open_deals` staat (open gebeurt pas na succesvolle
persist), maar het contract is subtiel. Een toekomstige
refactor die de state-mutatie volgorde omdraait introduceert
een consistency-risk. Aanbevolen: test toevoegen die pint
dat `self.state.open_deal(deal)` alleen gebeurt na een
succesvolle `_db_create_deal_with_retry`.

### 3.3 Parity-compare verbetering (`dfffb6d`)

Diff: `scripts/parity_compare.py` + `tests/test_parity_compare.py`.
Drie tekortkomingen uit de live parity-run gefixt: asymmetrische
open-deal telling, misleidende "Low parity" verdict op <5
deals, geen open-pair matching wanneer beide sides open zijn.

Nieuwe constantes: `MIN_DEALS_FOR_PARITY_VERDICT = 5`. Nieuwe
helper `_deal_is_open`, nieuwe `Pair.is_open` property. Summary
gerestructureerd naar `paper | live` matrix met expliciete
open/closed split. Interpretation regel is nu volume-aware.

Regressie-dekking: 5 nieuwe tests — `test_open_deals_counted_symmetrically`,
`test_open_pair_matches_on_timing_and_price`,
`test_open_pair_no_match_if_far_apart_in_time`,
`test_interpretation_with_few_deals`,
`test_interpretation_with_many_deals_preserves_old_behavior`
— de laatste is een expliciete regressie-guard tegen het
optuigen van de volume-gate in een manier die 50/50/0
ook zou skippen.

Geen zorgen — tooling-script, geen security-implicaties, alleen
kwaliteit van rapportage.

### 3.4 wipe-deals state.json uitbreiding (`4c1a233`)

**Oorzaak**: operator ontdekte dat `make wipe-deals` de DB wel
leegde maar state.json ongemoeid liet. Portal toonde "Active
deals" die niet meer in de DB bestonden.

**Fix**: `scripts/wipe_deals.py` uitgebreid met:

- `_is_pid_alive(pid)` — `os.kill(pid, 0)` met
  ProcessLookupError/PermissionError handling.
- `_check_no_bots_running(base_dir, user_ids)` — scant
  `logs/<uid>/pids/*.pid`, raised SystemExit bij levende pids.
- `_reset_state_file(path)` — backup naar `.pre_wipe_backup`,
  reset deal-velden (`balance_btc` = `initial_balance_btc`,
  rest naar 0), bewaart bot_name/mode/exchange/pair/...
- `_wipe_state_files(base_dir, user_ids)` — batch walker.
- Makefile target-docstring geüpdatet.

**12 tests** in `tests/test_wipe_deals.py` (TestResetStateFile
5, TestCheckNoBotsRunning 4, TestWipeStateFiles 3). Coverage
van de helpers is 100 %; `main()` zelf is niet getest (`scripts/
wipe_deals.py` rapporteert 54 % coverage — de untested 46 %
is de main()-lus die een echte DB + confirmatie-prompt nodig
heeft).

**Bredere vraag (architectuur)**: de commit zelf signaleert
in de module-docstring _"The underlying architecture has two
sources of truth (DB + state.json)"_ en stelt dat deze fix
alleen het symptoom adresseert. Dat klopt. Zolang zowel de
DB als state.json onafhankelijk muteren bij elke tick, heb
je de mogelijkheid dat een handmatige ingreep (operator die
een `.state.json` bijwerkt), een partial-write, of een
crash tussen DB-commit en state.json write leidt tot
divergentie. Opgevoerd als MEDIUM in §7.

**TOCTOU-race**: `_check_no_bots_running` en de destructieve
DELETE/state-reset lopen sequentieel. Als een operator een
bot start tussen de check en de wipe (`make start` in een
andere shell), dan schrijft die nieuwe bot een state.json
die vervolgens door `_wipe_state_files` leeg wordt gezet.
Venster: beperkt tot de DB-wipe duration (sub-seconde bij
lege tabellen, een paar seconden bij volledige wipe + VACUUM).
De "Type WIPE to confirm" prompt blokkeert voor operator-
input, wat de facto de race versmalt tot het post-WIPE
venster, maar elimineert 'm niet. Opgevoerd als LOW in §7.

**Cross-platform**: `os.kill(pid, 0)` werkt op Linux en macOS
maar heeft afwijkende semantiek op Windows (Windows: `TerminateProcess`,
niet "check if alive"). CI-matrix is Ubuntu, productie is
WSL2-Linux volgens phase-3.md — geen directe expositie. Voor
een Windows-poort van het portal is dit een bug. LOW in §7.

### 3.5 Favicon (`6e9f3ca`)

Diff: 8 asset files in `web/static/`, 3 regels in
`web/static/index.html`, 1 FileResponse route, 3 regressietests.
AuthMiddleware `_PUBLIC_PATHS` bevatte `/favicon.ico` al
(web/app.py:967) — geen whitelist-wijziging was nodig.

Regressietests (`tests/test_web_routes.py::TestFavicon`, 3
tests): onauth'd 200 + ICO magic-bytes check, SVG + apple-touch
via static mount, HTML bevat de link-tags. Grondig getest voor
zo'n triviale fix.

Geen zorgen. Low-impact, low-risk, correct gepind. Sanity-check:
`curl -I /favicon.ico` returned `content-type: image/x-icon`,
`content-length: 5807`, magic bytes `00000100` — correct ICO
format.

### 3.6 CI infrastructure (`6425277`)

Diff: `.github/workflows/test.yml`, `requirements.txt`,
`requirements-ml.txt`, `tests/test_web_routes.py`.

**Wat gefixed**:
- Python 3.11 gedropt uit matrix (pandas-ta 0.4.71b0 vereist
  3.12+). Correct beslissing — de codebase gebruikte al 3.10+
  union syntax.
- `requirements-ml.txt` wordt nu geïnstalleerd in Install step,
  niet alleen geaudit door pip-audit. Voorheen kon sklearn/
  xgboost-afhankelijke code niet in CI draaien.
- `prometheus_client==0.25.0` toegevoegd aan `requirements.txt`.
  Was productie-code (`web/metrics.py:26`, `web/routes/admin.py:95`)
  maar alleen handmatig geïnstalleerd.
- `xgboost==3.2.0` toegevoegd aan `requirements-ml.txt`. Idem
  (`ml/nightly_pipeline.py:99`).
- `test_fresh_login_after_logout_works` @skipif(CI=="true")
  toegevoegd. Lokaal groen, CI 401.

**Wat NIET gefixed** (en waarom dat een nieuwe finding is):

- **Root cause van de session-epoch CI-failure.** De skip-reason
  is eerlijk ("Root cause not yet identified — likely a
  TestClient cookie-handling difference between WSL2 and Ubuntu
  CI runners") maar dat is een kennis-gat, geen antwoord. Als
  het een TestClient bug is moeten we weten hoe die in
  productie doorwerkt. Als het een echte session-invalidation
  bug is die alleen onder bepaalde omgevingen oppopt, hebben we
  een blinde vlek in de auth-stack op het moment dat Phase-3
  live gaat. MEDIUM in §7.

- **Geen proces om te voorkomen dat productie-deps uit
  requirements.txt vallen.** De `prometheus_client` en `xgboost`
  ontbraken omdat beide lazy-geïmporteerd worden in productie-
  code. Een `pip-audit --strict` vangt geen missing-deps.
  Een simpele `pip install -r requirements.txt && python -c
  "import <alle productie-modules>"` smoke-test in CI zou
  dit vangen. MEDIUM in §7.

- **Coverage floor `--cov-fail-under=55`** (regel 48 workflow).
  Actuele coverage is 86 % op HEAD. Een floor van 55 % betekent
  dat je 31 pp aan tests kan verwijderen voordat CI 't merkt.
  Niet blocking, maar verouderde floor. LOW in §7.

### 3.7 Documentatie (`9ab31b8` + `43deaa8`)

Opmerkelijk: het originele scoping-doc `phase-3.md` werd
geschreven om 10:53 UTC (commit 9ab31b8) en later dezelfde dag
om 14:20 UTC compleet herzien (commit 43deaa8) nadat tijdens
WebSocket-debugging ontdekt werd dat er al een functionele
auth-stack bestond waarover het oorspronkelijke doc verkeerde
aannames deed. Dit is een waarschuwing over de kwaliteit van
interne documentatie bij eenmans-projecten: als je het doc
schrijft voordat je de codebase gegreppped hebt, documenteer
je je aannames in plaats van de realiteit.

**Revisie-verificatie** (§7 van het doc vs feitelijke code):

| Claim in phase-3.md §7 | Code-bewijs | Status |
|---|---|---|
| `logs/.auth.json`, Fernet-encrypted | `web/app.py:158` `_AUTH_FILE` + `core/credentials.save_encrypted` | ✓ |
| Password: bcrypt rounds=12 | `web/app.py:177` `bcrypt.gensalt(rounds=12)` | ✓ |
| `itsdangerous.URLSafeTimedSerializer`, HMAC-signed niet encrypted | `web/app.py:138` | ✓ |
| `_SESSION_TTL = 86400` (24h absolute) | `web/app.py:136` | ✓ |
| `samesite="strict"` op login-cookie | `web/routes/auth.py:82` | ✓ |
| AuthMiddleware whitelist + dual-path | `web/app.py:965-1014` | ✓ |
| `/auth/login` bcrypt checkpw + set cookie | `web/routes/auth.py:56-87` | ✓ |
| `/auth/logout` bumps session-epoch | `web/routes/auth.py:90-100` | ✓ |
| `/api/auth/change-password` verify + new hash + epoch bump | `web/routes/auth.py:112-152` | ✓ |
| `X-API-Key` header-only, query-string verwijderd | `web/app.py:995-1003` | ✓ |
| Session-epoch integer in `.auth.json`, cookie embed | `web/app.py:241-273` | ✓ |
| SlowAPI per remote-IP | `web/app.py:1020` | ✓ |
| WS auth handmatig op /ws/logs + /ws/state | `web/app.py:1432` + `:1575` | ✓ |

Elke claim in §7 van het doc klopt tegen de actuele code. De
revisie is feitelijk accuraat. Eén kleine kanttekening: het doc
zegt dat het auth-model "grotendeels" herbruikbaar is voor
Phase-3 — dat klopt, maar de credential-migratie vereist een
schema-wijziging aan `.auth.json` (blob bevat nu alleen
`username`, `password_hash`, `session_epoch`; wordt per-user
waardoor het `user_id` impliciet uit het pad komt). Niet een
claim-fout, wel een detail dat in een implementatie-ticket
mag.

## 4. Security analyse

### 4.1 Deal-ID collision resolution

- Cryptografisch relevantie: **nee**. Deal-ID's zijn niet
  secret; ze mogen voorspelbaar zijn (time-based prefix
  onthult wanneer een deal is geopend, wat al via andere
  kanalen zichtbaar is — `opened_at` kolom, notifier Telegram
  bericht, portal UI).
- DB UNIQUE enforcement: PRIMARY KEY op `deals.id` is de
  enige authoritatieve gate tegen collisions. Geverifieerd
  in `tests/test_cross_bot_deal_isolation.py::test_insert_collision_raises_integrity_error`.
- Retry-semantiek: max 3 attempts, elke met een nieuwe random
  suffix. Op uitputting ERROR-log + refuse-open. Correct
  geïmplementeerd; geen silent loss pathway meer.
- Niet-resolving edge case: clock goes backward (NTP). DB
  UNIQUE vangt het — retry ook. LOW (zie §7).

### 4.2 wipe-deals safety

- Pid-check: robuust tegen stale files (ProcessLookupError
  tolereert) en tegen garbage files (ValueError op
  `int(pid_file.read_text())`). Overweegt PermissionError
  conservatief (treats als alive). Geverifieerd in
  `tests/test_wipe_deals.py::TestCheckNoBotsRunning` (4 tests).
- Race-condities: TOCTOU tussen check en wipe bestaat
  (§3.4). Venster is sub-seconde in de gelukkige flow maar
  bestaat.
- Destructive-action confirmation: `input("Type WIPE to
  confirm")` — geen shortcut via env var, geen `--yes` flag.
  Goed. Bij EOFError (non-interactive) exit 1.
- Backup-integriteit: `shutil.copy2` preserveert mtime, maar
  `.pre_wipe_backup` is deterministisch (overschreven bij
  herhaalde run). Kan een forensisch onderzoek saboteren
  als de operator per ongeluk twee keer achter elkaar draait.
  Niet verhelpen in deze audit (verandering van backup-naming
  zou de test `test_wipe_backup_is_overwrite_safe` breken en
  "multiple wipes zijn safe" is een bewuste eigenschap). Low
  bewustzijn-issue.

### 4.3 /api/price scope-fix triple-check

- De fallback-lus leest state.json files. `bot.read_state()`
  komt uit `BotInfo` en leest `self.state_file`, het pad
  wordt bepaald door `paths.bot_state_path(self.user_id, ...)`.
- Registry-filter `user_id=user.id` voorkomt dat BotInfo
  objecten van andere users in de lus belanden — dus
  `bot.read_state()` kan nooit een state.json van een andere
  user lezen.
- Geen log-regel in de fallback-lus die het pad lekt (zou
  ook geen cross-user issue zijn maar wel onnodig verbose).
- 503 response bevat geen user-specifieke info.

Fix is correct; geen resterende leak-pad.

### 4.4 Auth-stack inventaris (uit phase-3 doc)

Alle 13 claims in phase-3.md §7 zijn geverifieerd tegen code.
Zie tabel in §3.7 hierboven. Geen inconsistenties.

### 4.5 CI-skip van session-epoch test

Dit is de belangrijkste blinde vlek van deze audit-cyclus. De
test-methode in `tests/test_web_routes.py:297-309`:

```python
def test_fresh_login_after_logout_works(self, auth_client):
    auth_client.post("/auth/logout")
    r = auth_client.post("/auth/login", json={...})
    assert r.status_code == 200
    assert auth_client.get("/api/bots").status_code == 200
```

Op GitHub Actions Ubuntu 3.12/3.13 faalt de laatste assertion
met `401`. Lokaal (WSL2 Python 3.12.3) slagen alle assertions.
Hypotheses:

1. **TestClient-cookie handling**: Starlette's TestClient
   muteert `auth_client.cookies` via `Set-Cookie`-headers
   van eerdere calls. Als de cookie-store anders reageert
   op `delete_cookie` (logout) + set (login) in één client
   tussen Python versies, kan de effectieve cookie bij de
   derde call leeg zijn.
2. **Session-epoch race**: `_bump_session_epoch()` in logout
   schrijft naar `.auth.json`. Als de CI-runner snel genoeg
   is, gooit login een cookie met epoch N+1; maar de test
   vuurt direct door. Lokaal een paar ms lager zou een
   TOCTOU produceren.
3. **.auth.json state leak tussen tests**: de test draait
   mogelijk in een andere werkdir op CI waar een oudere
   `.auth.json` staat met een hogere epoch. Niet uitgesloten
   gezien de volgorde van autouse fixtures.

Geen van deze hypotheses is getoetst. Urgency: MEDIUM —
niet Phase-3 blocker (iteratie-werk kan voort), wel
**launch-blocker**. CI-green is harde eis voor publieke
lancering.

## 5. Test coverage verificatie

Commando: `.venv/bin/python3 -m pytest tests/ -q --cov=.
--cov-report=term-missing:skip-covered`

- Baseline v24: 772 tests
- HEAD: **828 tests** collected, **828 passed**, **0 failed**
- Delta: **+56 tests**

Distributie van de +56:

| Groep | Δ tests | Bestanden |
|---|---|---|
| Deal-ID collision (ac21b6f) | +18 | test_ids.py (12) + test_cross_bot_deal_isolation.py (6) |
| Wipe-deals (4c1a233) | +12 | test_wipe_deals.py |
| Parity-compare (dfffb6d) | +7 | test_parity_compare.py uitbreiding |
| Favicon (6e9f3ca) | +3 | test_web_routes.py::TestFavicon |
| Chart scope (67a136a) | ~+2-3 | test_chart_routes.py uitbreiding |
| Registry users-check (8f0448a) | +4 | test_registry_composite_key.py + test_user_model.py |
| **Totaal direct dekkend** | **+46** | |

De overige ~10 tests zijn verspreid over refactors in
bestaande test-bestanden (conftest helper updates,
`test_paper_state` counter-test vervangen door 2 shape-tests,
etc.).

**Coverage totals**:

- Totaal: 12 410 regels, 1 727 gemist → **86 %** coverage
- v24 rapporteerde geen expliciet coverage-percentage, alleen
  Δ-tests. Net baseline-bump.

**Onder-getest** (< 75 % coverage, gesorteerd op impact):

| Module | Cov | Opmerking |
|---|---|---|
| `web/routes/chart.py` | 37 % | Paginatie-logica + fallback-retry niet uitgetest. De scope-fix is wel gedekt (via test_chart_routes.py). |
| `scripts/wipe_deals.py` | 54 % | Alleen helpers getest, `main()` niet — prompt + DB-lus lastig zonder echte DB. |
| `web/app.py` | 60 % | 825 regels totaal, 333 gemist. Voornamelijk WS-broadcaster, tail_logs lus, subprocess spawn paths. |
| `web/routes/exchanges.py` | 63 % | Exchange-config endpoints — credential-mutatie paden niet gedekt. |
| `web/routes/bots.py` | 71 % | Config-mutatie paden niet volledig. |
| `web/routes/admin.py` | 74 % | Emergency-stop + metrics handler. |

Test-infra is gezond, maar drie specifieke gaten verdienen
aandacht:

1. **`scripts/wipe_deals.py:main()` end-to-end**. De
   integratie tussen safety-check + DB-wipe + state-wipe
   is niet dekkend gepind — een operator die de volgorde of
   error-handling breekt zou pas in de volgende productie-
   wipe merken dat het kapot is. Aanbeveling: een
   test die de echte `main()` aanroept met een stdin-mocked
   "WIPE" en een temp DB.

2. **`/ws/state` user-scoping heeft geen test**. De phase-3
   doc noemt het als Phase-3 werk maar de huidige
   auth-check (web/app.py:1575) is getest in
   `TestWsStateSmoke::test_ws_state_accepts_session_cookie`
   alleen voor de happy-path. Een test die assertert dat
   een user alléén zijn eigen bots in het initial-snapshot
   krijgt ontbreekt. Kan nu nog niet want single-user, maar
   als Phase-3 live gaat moet deze er vooraf staan.

3. **`_scan_user_dirs` fail-open pad** (zie §7 finding).
   Nergens gedekt door een test die DB faalt en asserteert
   dat het fallback-gedrag juist is (of juist niet).

## 6. Documentatie coherentie

| Document | Status | Bewijs |
|---|---|---|
| `docs/phase-3.md` (v2) | **COHERENT** | §3.7 tabel hierboven — 13/13 claims in §7 verifieerbaar. |
| `docs/architecture.md` | **PARTIAL** | Noemt niet: YYYYMMDDHHMM-RRRR deal-ID format, `make wipe-deals`, CI workflow, of de `create_deal`/`update_deal` split in deal_store. Fase 1 + 2 secties blijven accuraat. |
| `docs/runbook.md` | **GAP** | Geen paragraaf voor `make wipe-deals` terwijl het een destructief operator-commando is. Migratie-run is gedocumenteerd, wipe niet. Aanbevolen: toevoeg-sectie met "Stop alle bots → make wipe-deals → herstart" flow. |
| `README.md` | **OK** | Geen stale claims rond paths of deal-IDs. Hoge-niveau quick-start blijft relevant. |

**Zorg**: architecture.md + runbook.md zijn niet meegegroeid
met de 9 code-commits van deze werkdag. Docs staan ~1 dag
achter op code. Niet kritisch maar opgenomen als LOW in §7.

## 7. Nieuwe findings

| # | Severity | Titel | Locatie + fix |
|---|---|---|---|
| 1 | MEDIUM | `_scan_user_dirs` fail-open bij DB-failure | `web/app.py:595-605` · bij exception falt terug op integer-name-only matching. Phase-3 risico. Fix: fail-closed met cached last-known-good lijst (zoals phase-3.md §3 voorstelt), niet integer-name fallback. |
| 2 | MEDIUM | Session-epoch CI-test geskipt zonder root-cause | `tests/test_web_routes.py:287-297` · onderzoek waarom `test_fresh_login_after_logout_works` op GitHub Actions faalt maar lokaal niet. Vermoedelijk TestClient cookie-handling of `.auth.json` leak. Hard gate voor publieke lancering. |
| 3 | MEDIUM | Geen proces tegen missing productie-deps | `requirements.txt` miste `prometheus_client` + `xgboost` terwijl ze in productie-code werden gebruikt. CI greep pas in nadat push naar GitHub faalde. Fix: CI-step die een smoke-import doet van alle top-level productie-modules na `pip install -r requirements.txt`. |
| 4 | MEDIUM | DB + state.json dual-source (architecturaal) | Meerdere plekken (`core/deal_store.py` + `paper/paper_engine._write_state`). Noch commit ac21b6f noch 4c1a233 adresseert dit. Zolang beide onafhankelijk muteren: divergentie mogelijk. Phase-3 scoping werk. |
| 5 | MEDIUM | Body-size cap niet op `POST /api/bots` + PUT config (v24 #3 blijft) | `web/routes/bots.py:369,422` · FastAPI parst body als dict zonder Content-Length guard. DoS-surface. Fix: zelfde MAX_CONFIG_BODY_BYTES pattern als validate-config. |
| 6 | LOW | `_scan_user_dirs` runt DB-query per 5s refresh | `web/app.py:594` + registry refresh TTL. Onnodig werk onder steady-state. Cache de active-set voor X seconden + invalidate bij een user-mutatie endpoint. |
| 7 | LOW | Orphan warning log-lawaai niet rate-limited | `web/app.py:615-618` · elke `_scan_user_dirs` call (5s TTL) logt een WARNING per orphan dir. Één typo in config/bots/ = 720 WARNINGs/uur. Rate-limit of de-dupe. |
| 8 | LOW | Deal-ID NTP-backward correctie edge case | `core/ids.py:40-50` · als de systeemklok ≥1min terug-springt kan de `YYYYMMDDHHMM` prefix herhaald worden. DB UNIQUE + retry vangen het, maar vermelden in docstring als known-edge. |
| 9 | LOW | wipe-deals TOCTOU pid-check → wipe | `scripts/wipe_deals.py:85-113` versus `_wipe_state_files` daarna. Venster: de duration van DB-wipe. Operator-confirmation mitigeert maar elimineert niet. Accept als restrisico of voeg een `flock` op een sentinel toe. |
| 10 | LOW | wipe-deals `os.kill` is POSIX-only | `scripts/wipe_deals.py:75` · Windows-poort zou stilletjes fout gaan. Huidig target is Linux, maar documenteren. |
| 11 | LOW | CI coverage-floor 55 % terwijl actual 86 % | `.github/workflows/test.yml:45` · floor zo ver onder actual dat 't nooit flagt. Zet op 80 of 82 % om regressies te vangen. |
| 12 | LOW | `docs/architecture.md` + `docs/runbook.md` zijn niet geüpdatet voor 2026-04-19 werk | Geen paragrafen voor deal-ID format, wipe-deals, CI. Niet kritisch, wel drift. |
| 13 | LOW | `_db_create_deal_with_retry` muteert `deal.id` in place | `paper/paper_engine.py:302` · werkt omdat open_deals nog niet is bijgewerkt, maar contract is subtiel. Aanbevolen test: assert `state.open_deal(deal)` alleen na succesvolle persist. |

**Totaal: 0 HIGH · 5 MEDIUM · 8 LOW.**

## 8. Open issues na v25

Volgorde naar urgency:

1. **Finding #2** (session-epoch CI) — launch-blocker. Onderzoek
   MOET plaatsvinden vóór publieke deployment.
2. **Finding #5** (body-size cap gap) — DoS surface. Laag drempel
   om te fixen (~10 regels code), hoog effect.
3. **Finding #1** (fail-open) — Phase-3 blocker. Elke multi-user
   omgeving verdient fail-closed.
4. **Finding #3** (dep-manifest proces) — voorkomt dat dit
   opnieuw gebeurt.
5. **Finding #4** (dual-source architecturaal) — Phase-3 scoping.
6. **Rest van LOW** — technical debt backlog.

V24 carry-overs (MEDIUM #3, LOW #4, LOW #5) blijven open en zijn
als #5, plus de credentials-parent en logs/pids rmdir niet
opnieuw opgevoerd — ze staan al in v24. Aanbevolen ze in een
sweep-PR te bundelen.

## 9. Live trading readiness

Status **ongewijzigd** tov v24.

- `live/live_engine.py:281` `_place_market_order` raised nog
  steeds `NotImplementedError`.
- `live/order_reconciliation.py` fetch-branch nog steeds
  uitgecommentarieerd.
- Geen van de 14 commits in deze audit-cyclus raakt het
  real-order pad. Paper + dry-run zijn de enige draaiende
  modi; dat is ook het ontwerp voor Phase-1/2.

Phase-3 zal live-order implementatie moeten bundelen met de
auth-migratie. Niet aangeraakt in deze audit-cyclus.

## 10. Top issues (samenvatting)

Gesorteerd op severity + impact:

| # | Severity | Titel | Urgentie |
|---|---|---|---|
| 1 | MEDIUM | Session-epoch CI-test geskipt zonder root-cause | launch-blocker |
| 2 | MEDIUM | `_scan_user_dirs` fail-open bij DB-failure | Phase-3 blocker |
| 3 | MEDIUM | DB + state.json dual-source | Phase-3 scoping |
| 4 | MEDIUM | Geen proces tegen missing productie-deps | pre-launch |
| 5 | MEDIUM | Body-size cap gap op POST/PUT /api/bots (v24 #3) | pre-launch |
| 6 | LOW | Registry DB-query per 5s refresh | optimisatie |
| 7 | LOW | Orphan warning log-volume | operator-ergonomie |
| 8 | LOW | Deal-ID NTP-backward edge case | documentatie |
| 9 | LOW | wipe-deals TOCTOU race | restrisico |
| 10 | LOW | wipe-deals POSIX-only | portability |
| 11 | LOW | CI coverage-floor verouderd | regressie-gate kwaliteit |
| 12 | LOW | docs/architecture.md + runbook.md achterstand | doc-drift |
| 13 | LOW | `_db_create_deal_with_retry` in-place mutatie | test-pin |
| — | carry-over | `credentials/` parent dir 0755 (v24 LOW #4) | cosmetisch |
| — | carry-over | `logs/pids/` leeg post-migratie (v24 LOW #5) | cosmetisch |

**0 HIGH · 5 MEDIUM + 1 MEDIUM carry-over · 8 LOW + 2 LOW carry-over.**

## 11. Aanbevelingen

### Voor publieke lancering (launch-gates)

1. **Onderzoek session-epoch CI-failure**. Reproduce op een
   Ubuntu VM, check TestClient cookie-delete + re-set gedrag
   tussen calls, dump Set-Cookie headers, dump `.auth.json`
   inhoud voor + na logout/login. Root-cause in een follow-
   up commit + verwijder de skip.
2. **Body-size caps op `POST /api/bots` + `PUT /api/bots/{slug}/config`**.
   Zelfde 64 KB cap pattern als `validate-config`. ~30 regels
   code + 4 tests.
3. **CI smoke-import check**. Na `pip install -r requirements.txt`
   (zonder ml), een stap die alle productie-modules importeert:
   `python -c "import web.app, paper.paper_engine, live.live_engine,
   ml.nightly_pipeline, web.metrics, web.routes.admin"`. Faalt
   bij missing deps.

### Vóór Phase-3 implementatie-start

4. **Fail-closed voor `_scan_user_dirs`**. Implementatie-schets:
   module-level `_LAST_KNOWN_GOOD_USERS: set[int] | None = None`,
   gevuld bij elke succesvolle DB-call. Op DB-failure: hergebruik
   voor N refreshes (bv. 5 × 5 s = 25 s), ERROR log erbij. Na
   N fallback-cycli: fail-closed en return `[]`. Dit pint ook
   de fail-open regressie en voorkomt de orphan log-spam
   (finding #7).
5. **Coverage-floor raising**. Zet `--cov-fail-under=80` als
   minimum; verhoog incrementeel. Dit maakt toekomstige CI-
   gates betekenisvol.

### Phase-3 scoping (niet in deze cyclus)

6. **DB + state.json dual-source oplossen**. Mogelijke
   richtingen: state.json wordt afgeleid uit DB (read-only
   view), of DB wordt afgeschaft voor live-state en vervangen
   door WAL-style event log. Beide zijn grote herzieningen
   — hoort thuis in phase-3.md als aparte beslissing.
7. **WS-state user-scoping test**. Wanneer Phase-3
   `_request_user` een echte user uit de cookie haalt,
   voeg een test toe die verifieert dat user 1 geen
   state-frames van user 2 ontvangt op /ws/state.

### Backlog (sweep-PR)

8. Carry-overs v24 LOW #4 + #5 (credentials/ parent 0755,
   logs/pids/ rmdir).
9. Documentatie bijwerken: `docs/runbook.md` uitbreiden met
   wipe-deals flow, `docs/architecture.md` met deal-ID
   format + `create_deal`/`update_deal` contract.
10. Orphan-warning log-dedup (finding #7).
11. NTP-backward edge case in `core/ids.py` docstring
    (finding #8).

---

_Audit uitgevoerd op 2026-04-19. HEAD: `32780d6`. Parity-bots
`rsi_paper_test` + `rsi_real_test` zijn onaangeroerd gebleven
tijdens deze audit — state.json timestamps ≤ 1 min oud
bevestigd bij start en einde van de uitvoering._
