# Phase-3 scoping

> **Status note (Phase-3a shipped):** de DB-based auth refactor is
> gemerged. `users` heeft nu `password_hash` + `role` +
> `session_epoch` kolommen, `logs/.auth.json` is uit de runtime-paden
> verdwenen (wordt automatisch gearchiveerd op eerste init_db()),
> `_request_user` / `_ws_extract_user_id` resolven uit cookie-`uid`,
> en `core/user_store.py` is de canonieke auth-API. Secties hieronder
> die dat proces beschrijven zijn historisch — actuele docs staan in
> `docs/architecture.md` (module-structure) en `docs/runbook.md`
> (`setup-admin` flow). Phase-3b (multi-user login UI) + Phase-3c
> (admin-portal) + Phase-3d (password-reset) blijven pending.

Stand-van-zaken document. De multi-tenant foundation (Fase 1 + 2)
staat. Dit is wat we tot nu toe besloten hebben over Phase-3, welke
vragen nog open zijn, en welke call-sites in Phase-3 aandacht vragen.
Geen code, geen implementatiedetails — alleen denkwerk dat anders
verdwijnt in commit-berichten en audit-rapporten.

## 1. Doel & status

Fase 1 bumpte het DB-schema naar v3 met een `users` tabel en
`user_id NOT NULL FK` op elke owned tabel; Fase 2 verschoof het
filesystem naar user-scoped subdirs en gaf elke user een eigen
Fernet key voor exchange credentials. Alle routes + engines
dragen een `user_id` mee, maar elke request draait op de hardcoded
admin (`User(id=1)`) via de `_request_user` stub in
`web/app.py`. Audit v24 scoorde 95.8% READY.

Een eerste auth-laag bestaat al in de codebase: single-admin met een
Fernet-encrypted credential store, signed session cookies, en een
login endpoint (details in §7). Phase-3 breidt deze uit naar N users —
de bestaande mechanismen blijven grotendeels intact, alleen de
credential-opslag en user-resolution vereisen aanpassing.

Phase-3 activeert echte multi-user functionaliteit: inloggen voor
andere users dan de admin, sessies die naar een user-id resolven, en
een admin-rol voor cross-user operaties. Het doel van dit document is
vast te leggen wat er al besloten is en waar de open keuzes zitten,
niet om de implementatie op voorhand uit te werken.

## 2. Vastgestelde beslissingen

> **Historical note (v26-24).** Deze sectie bevat pre-Phase-3a
> beslissingen. Phase-3a heeft de auth-stack geïmplementeerd:
> `users.password_hash` (bcrypt) in plaats van `.auth.json`,
> session_epoch per-user in plaats van global, `_request_user`
> leest uit cookie `uid`. De beslissingen hieronder beschrijven
> wat toen besloten werd; voor de huidige auth-stack zie
> `docs/architecture.md` sectie "Multi-tenant foundation" +
> "Admin provisioning (post-Phase-3a)", en
> `docs/security-model.md` Part 3.3 voor de multi-layered
> authentication spec.

**Emergency-stop is admin-cross-user by design.** `web/routes/admin.py`
laat de admin rol alle bots in het systeem stoppen (ook die van
andere users), met een UI-confirmatie die expliciet zegt hoeveel
users geraakt worden — bijvoorbeeld _"je staat op het punt om bots
van 3 users te stoppen"_. Parallel komt er een user-facing bulk-stop
endpoint dat user-scoped is: dezelfde drang om alles tegelijk
stop te zetten, maar alleen voor de eigen bots. De bestaande
`/api/emergency-stop` blijft dus bestaan, met een auth-check erbij
die `role=admin` vereist.

**WebSocket state-broadcaster is user-scoped.** Een ingelogde user
mag via `/ws/state` alleen de state van zijn eigen bots ontvangen —
zowel in de initiële snapshot als in de periodieke broadcast uit
`watch_state_files`. Dat betekent dat de WebSocket-handshake
authentiek de user moet identificeren (niet alleen "heeft een
geldig session-cookie") en de broadcaster per-user subscriptions
moet bijhouden. De huidige `state_broadcaster` heeft één gedeelde
client-set; die wordt opgesplitst per user_id.

**Admin-rol komt als nieuwe kolom in de users tabel.** Waarden
`'admin'` en `'user'`, default `'user'` voor nieuwe accounts. De
bestaande admin seed (`users(id=1, username='admin')`) krijgt
`role='admin'` bij de schema-bump. Dat vereist ofwel een schema
v4 met een destructieve drop-and-recreate zoals v3, ofwel een
idempotent `ALTER TABLE users ADD COLUMN role TEXT NOT NULL
DEFAULT 'user'` gevolgd door een `UPDATE users SET role='admin'
WHERE id=1`. De ADD COLUMN route is simpeler en geen data-loss —
sterk voorkeur, maar de beslissing hoort bij de implementatie.
Belangrijk: deze kolom komt **naast** de bestaande credential-
store. De users-tabel bevat identiteit + metadata (username, role,
active); ~~password hashes + session-epoch blijven in
`.auth.json` (per-user na Phase-3, zie §3)~~. _(v26-24:
historical — Phase-3a verhuisde password hashes en session-epoch
naar de `users` tabel; `.auth.json` is verwijderd.)_

**Credential-opslag per user.** ~~De huidige `logs/.auth.json`
wordt gesplitst in per-user bestanden langs de Fase-2 layout-lijn
(exacte plek — `credentials/<uid>/.auth.json` of
`logs/<uid>/.auth.json` — is een implementatie-detail voor bij
het schrijven van de migratie). De huidige admin-blob verhuist
bij de schema-v4 bump naar het pad voor `user_id=1`, zodat
bestaande operators na migratie gewoon kunnen blijven inloggen.
Deze keuze is consistent met `credentials/<uid>/` en
`keys/<uid>.key` uit Fase 2.~~ _(v26-24: historical — Phase-3a
verving `.auth.json` met DB-columns op `users`. Per-user
exchange-credentials blijven onveranderd in `credentials/<uid>/`
+ per-user Fernet keys in `keys/<uid>.key`.)_

## 3. Open vragen

> **Historical note (v26-24).** De vragen hieronder zijn
> pre-Phase-3a gesteld. Veel zijn inmiddels beantwoord door de
> Phase-3a implementatie of door follow-up audits: de
> `_scan_user_dirs` fail-closed keuze is gemaakt (commit
> `1ee4737`), de CI session-epoch test is gedebugged en
> groen (SameSite-fix in `5a4d97b`). Voor de actuele security-
> keuzes zie `docs/security-model.md`; voor open Phase-3b/c/d
> items de preface van dit document.

**Auth-model.** Geen open vraag meer — zie §7. Signed session
cookies (itsdangerous, niet JWT) + Fernet-encrypted credential
store + bcrypt password hashes staan er al. De eerdere
JWT-revocation / Redis-store afweging is irrelevant: we hebben
een werkend lokaal model dat per-user schaalt zodra credential-
opslag gesplitst is.

**CSRF-protectie voor sensitive POSTs.** Cookies zijn al
`SameSite=strict` (`web/routes/auth.py:82`), wat de meeste
cross-origin request forgery blokkeert. Vraag is of
mutation-endpoints (`/api/emergency-stop`, `/api/bots/.../start`,
password-change) bovenop `SameSite=strict` nog een expliciet
CSRF-token willen — "diepte in verdediging" argument tegen
afhankelijkheid van één cookie-attribuut. Uitstel tot de
Phase-3 implementatie; de auth-stack moet eerst N-user
gereed zijn voordat deze sub-keuze inhoudelijk gemaakt kan worden.

**Signup-flow.** Self-service versus admin-invite-only. Voor een
platform dat met echt geld handelt leunt veiligheid richting
invite-only: een nieuwe user kan pas bestaan na een admin-actie.
Self-service zou een complete e-mail + verificatie + captcha
stack vereisen; dat is disproportioneel voor de huidige scope.
Infrastructuur voor user-creation bestaat nog niet — alleen
`_bootstrap_auth_if_missing()` voor de admin op eerste boot
(`web/app.py:162`). Uitstel tot de Phase-3 implementatie;
waarschijnlijk invite-only.

**Password-reset flow.** Nieuwe vraag. Kan de admin andermans
password resetten? Zo ja, wordt er automatisch een nieuw
"initial password" geschreven naar `credentials/<uid>/.initial_password`
(analoog aan de bestaande bootstrap)? Of moet een reset altijd
via de user zelf (oud wachtwoord vereist, dus effectief een
change-flow en geen reset)? Geen antwoord — te bespreken zodra
signup-flow gekozen is, want de antwoorden hangen samen.

**`tail_logs` startup scan (`web/app.py:1618`).** De huidige
implementatie scant alle bot-logs + `portal.log` voor live
streaming. Onduidelijk of dit een infra-bron is (alle logs,
alleen voor admin) of user-surfaced (alleen eigen logs).
Waarschijnlijk infra — het endpoint streamt naar de portal-UI,
en per-user log-filtering moet één level hoger in de route.
Te verifiëren zodra we de log-viewer endpoint tegenkomen.

**Fail-open versus fail-closed in `_scan_user_dirs` bij DB-failure.**
De Phase-2 fix valt nu terug op integer-name-only matching als
`get_active_user_ids()` faalt, met een WARNING in portal.log.
Phase-1 veilig omdat er één user is, maar met meerdere users is
fail-open een zwakte: een transient DB-glitch zou stilletjes
een orphan dir als valide tenant kunnen accepteren. Fail-closed
(registry leeg laten + ERROR loggen) is veiliger maar maakt de
boot kwetsbaar voor DB-beschikbaarheid. Overweeg een hybride:
cached-last-known-good lijst van active user_ids die bij
DB-failure blijft gelden voor enkele refresh-cycli. Beslissing
hoort bij de Phase-3 auth wiring.

**Session-epoch test faalt op CI, lokaal groen.**
`test_fresh_login_after_logout_works` in
`tests/test_web_routes.py` slaagt op WSL2/Python 3.12.3 maar
geeft een 401 op GitHub Actions Ubuntu runners. Workaround:
`@pytest.mark.skipif(os.getenv("CI") == "true")` met TODO-reason
(toegevoegd in `fix/ci-green-all-jobs`). Root cause
onbekend — vermoedelijk een verschil in hoe TestClient
cookies over requests bewaart tussen de twee omgevingen.
Niet blokkerend voor Phase-3 implementatie, maar een harde
eis voor publieke lancering: CI-green is non-negotiable vóór
we buiten solo-deploy treden.

## 4. Call-sites die Phase-3 attentie vragen

> **Historical note (v26-24).** Line-numbers in de tabel
> hieronder wijzen naar de pre-Phase-3a web/app.py en zijn niet
> meer up-to-date. De meeste call-sites zijn inmiddels in
> Phase-3a geadresseerd (zie commits `16485f4` `_request_user`
> cookie-resolution, `9da608e` + `c74b393` WS user-scope). Voor
> de resterende Phase-3b werk (per-user broadcaster filtering)
> zie audit v26-report finding v26-16 en de TODO's in
> `web/app.py` rond de LogBroadcaster / StateBroadcaster
> klassen.


| Locatie                                         | Huidige scope     | Phase-3 scope                     | Notes                                           |
|-------------------------------------------------|-------------------|-----------------------------------|-------------------------------------------------|
| `web/app.py:310` `_request_user` stub           | hardcoded admin   | lookup user_id via cookie-payload | kern-bridge; alle andere Phase-3 werk hangt hier op |
| `web/routes/admin.py:121` emergency-stop        | alle bots         | admin cross-user + user bulk-stop | auth-check `role=admin`; user endpoint is nieuw |
| `web/app.py:1432` `/ws/logs` auth-check         | session-cookie    | + user-resolution                 | cookie-payload moet user_id yielden             |
| `web/app.py:1575` `/ws/state` auth-check        | session-cookie    | + user-resolution                 | idem                                            |
| `web/app.py:1523` `watch_state_files` iteratie  | alle bots         | per-user client set               | broadcaster moet per-user subscriptions bijhouden |
| `web/app.py:1582` WS `/ws/state` seed           | alle bots         | alleen eigen bots                 | snapshot filtert op user.id                     |
| `web/app.py:1618` `tail_logs` scan              | alle logs         | TBD (waarschijnlijk infra)        | verifieer of endpoint user-data surfacet        |
| `web/app.py` `_scan_user_dirs` DB-fail pad      | fail-open + WARN  | overweeg fail-closed / cache      | commit 8f0448a documenteert het trade-off       |

## 5. Afhankelijkheden / volgorde

> **Historical note (v26-24).** De ordering hieronder beschrijft
> de route die Phase-3a gevolgd heeft: schema-v4 (destructief),
> credential-migratie, `_request_user` bridge. Alle drie
> gerealiseerd (commits `e3d9199`, `58ddf5c`, `16485f4`).
> Ordering voor Phase-3b is afzonderlijk en staat nog niet
> gedocumenteerd; hoort in een nieuw phase-3b-roadmap document
> wanneer die fase start.


De schema-v4 bump met `role`-kolom komt eerst — `ALTER TABLE
users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'` +
`UPDATE users SET role='admin' WHERE id=1`, geen data-verlies.
Zonder deze kolom heeft geen enkele admin-check iets om op te
rusten. Daarna de credential-migratie: huidige
`logs/.auth.json` verhuist naar het per-user pad voor
`user_id=1` en de bootstrap-helper leert het per-user pad
aan te maken voor toekomstige accounts. Pas dan kan de
`_request_user` bridge de cookie-payload lezen, een user_id
resolven via `get_user_by_id`, en een echte User returnen in
plaats van de hardcoded admin. Op die bridge hangen de
admin-rol checks op endpoints (emergency-stop, straks user-
management) en de WebSocket user-scoping — WS-handshake
identificeert de user en de broadcaster splitst de
subscriber-set per user_id. User-creation (signup of
admin-invite endpoint) komt als laatste, afhankelijk van het
antwoord op de signup-vraag in §3. De auth-laag zelf (login
endpoint, session cookies, bcrypt) staat al — die fase van
het werk is gedaan.

## 6. Verwijzingen

- [Architecture: Multi-tenant foundation (Fase 1)](architecture.md#multi-tenant-foundation-fase-1)
- [Architecture: Multi-tenant filesystem layout (Fase 2)](architecture.md#multi-tenant-filesystem-layout-fase-2)
- [Runbook: Database reset](runbook.md#database-reset-multi-tenant-migration)
- [Runbook: Filesystem migration](runbook.md#filesystem-migration-fase-2)
- Bestaande auth-code: `web/app.py:100-280` (helpers + config),
  `web/app.py:970-1030` (AuthMiddleware), `web/routes/auth.py`
  (login + password-change).
- Audit v24 findings: MEDIUM #1 (chart.py scope) en #2 (registry
  users-cross-check) zijn closed in commits `67a136a` en `8f0448a`.
  De overige registry.all() call-sites uit de v24 survey staan in
  §4 hierboven en vallen onder de Phase-3 scope.

## 7. Bestaande auth-infrastructuur

> **Historical note (v26-24).** Deze sectie beschrijft de
> pre-Phase-3a auth-architectuur (logs/.auth.json +
> `_bootstrap_auth_if_missing` + globale session_epoch).
> **Geen van de hieronder beschreven bestanden, helpers of
> code-locaties is nog up-to-date.** Phase-3a heeft:
>
> - `.auth.json` verwijderd (archived naar
>   `.auth.json.pre_phase3.<ts>` op eerste init_db),
> - `_bootstrap_auth_if_missing` vervangen door
>   `scripts/setup_admin.py`,
> - `_load_auth / _save_auth / _bump_session_epoch /
>   _current_session_epoch` verwijderd uit `web/app.py`,
> - password-hash + role + session_epoch naar kolommen in
>   `users` verhuisd (bcrypt rounds=12 via
>   `core.user_store.set_password`),
> - session_epoch van globaal naar per-user gezet.
>
> Voor de huidige auth-stack: `docs/architecture.md`
> "Admin provisioning (post-Phase-3a)" +
> `docs/security-model.md` Part 3.3. De tekst hieronder blijft
> bewaard als implementatie-anker voor audit-trail doeleinden;
> lees niet als spec.


Inventaris van wat er al staat. Deze stack is productie-rijp voor
single-admin (Phase-1); Phase-3 breidt 'm uit zonder het
fundamentele model om te gooien. De aanpassingen zitten in
credential-opslag (per-user), users-tabel (role-kolom), en
`_request_user` bridge — niet in het auth-model zelf.

**Credential-opslag.** `logs/.auth.json` is een Fernet-encrypted
blob met `username` (admin), `password_hash` (bcrypt, rounds=12),
en `session_epoch`. Gemaakt op eerste boot door
`_bootstrap_auth_if_missing()` (`web/app.py:162`) die ook een
eenmalig plaintext `logs/.initial_password` (mode 0600) wegschrijft
zodat de operator de eerste login kan doen. Dat bestand wordt
automatisch gewist bij de eerste password-change.

**Session-cookies.** `itsdangerous.URLSafeTimedSerializer`
(`web/app.py:138`), HMAC-**signed** maar **niet encrypted** — de
payload (`{"u": username, "iat": ..., "ep": epoch}`) is leesbaar in
devtools na base64-decode. Dit is relevant voor het security-model:
de cookie onthult **welke gebruiker** ingelogd is, niet de
credentials zelf. `_SECRET_KEY` komt uit `REVERTO_SECRET_KEY`
env var; zonder env var wordt een ephemeral key gegenereerd met
een WARNING dat alle sessies bij restart vervallen. TTL is
absolute (24h, `_SESSION_TTL`), geen sliding — `max_age` op
cookie + `max_age=` bij `loads()` zorgen dat een oude cookie
altijd vervalt.

**AuthMiddleware** (`web/app.py:978`). Gate elke HTTP-request
behalve `_PUBLIC_PATHS` (`/`, `/favicon.ico`, `/health`,
`/healthz`, `/readyz`, `/metrics`, `/auth/status`, `/auth/login`,
`/auth/logout`) en `/static/*`. Dual-path respons: API-paden
(`/api/*`, `/ws*`, `Accept: application/json`) krijgen 401 JSON;
browsers krijgen 303 redirect naar `/`. De middleware draait
**niet** op WebSocket upgrades — die checken de session cookie
handmatig (zie onder).

**Login + logout + password-change.** Alle drie in
`web/routes/auth.py`. `/auth/login` doet `bcrypt.checkpw` tegen
de opgeslagen hash en set de session cookie met
`httponly=True`, `samesite="strict"`, `secure=_COOKIE_SECURE`.
`/auth/logout` bumpt de session-epoch (logt iedereen uit, niet
alleen de caller) en wist het cookie. `/api/auth/change-password`
verifieert het oude wachtwoord, schrijft een nieuwe bcrypt-hash,
bumpt epoch opnieuw, en wist `logs/.initial_password` als die
nog bestaat.

**API-key fallback.** Voor scripts en CI-tools: `X-API-Key`
header, gecheckt met `secrets.compare_digest` in de
AuthMiddleware. De query-string variant (`?api_key=...`) is
bewust verwijderd — query strings lekken in proxy logs, nginx
access logs en browser history, en de API-key is long-lived.
`REVERTO_API_KEY` env var, fallback naar ephemeral +
`logs/.api_key` op 0600.

**Session-epoch.** Integer in `.auth.json`. Elke cookie embed
de epoch waaronder 'ie gemint is; `_verify_session_cookie` vergelijkt
en weigert op mismatch. Logout en password-change bumpen de
epoch, waardoor elke browser die nog een oud cookie heeft
direct uitgelogd is. Globale invalidatie — geen per-user
granulariteit vandaag (komt in Phase-3 vanzelf als credentials
per-user zijn).

**Rate-limiter.** SlowAPI per remote-IP (`web/app.py:1020`),
`5/minute` op `/auth/login` en `10/minute` op password-change.
Niet per-user — een reverse-proxy met `X-Forwarded-For` parsing
moet in een eigen `key_func` komen als het portal achter een
proxy landt.

**WebSocket auth.** Omdat `BaseHTTPMiddleware` niet op WS-upgrades
draait, doen `/ws/logs/{slug}` (`web/app.py:1432`) en
`/ws/state` (`web/app.py:1575`) handmatig
`_verify_session_cookie(websocket.cookies.get(_SESSION_COOKIE))`
en sluiten met code 4401 bij mismatch. Dezelfde cookie-logica
als de HTTP-gate, alleen lokaal aangeroepen. Query-string API-key
fallback is hier ook bewust weggelaten — browsers sturen het
cookie op same-origin WS-upgrades automatisch mee.
