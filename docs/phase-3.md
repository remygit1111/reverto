# Phase-3 scoping

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

Phase-3 activeert echte multi-user functionaliteit: inloggen,
sessies die naar een user-id resolven, en een admin-rol voor
cross-user operaties. Het doel van dit document is vast te
leggen wat er al besloten is en waar de open keuzes zitten,
niet om de implementatie op voorhand uit te werken.

## 2. Vastgestelde beslissingen

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
zowel in de initiële snapshot (regel 1563 in `web/app.py`) als in
de periodieke broadcast uit `watch_state_files` (1513). Dat
betekent dat de WebSocket-handshake authentiek de user moet
identificeren (niet alleen "heeft een geldig session-cookie") en
de broadcaster per-user subscriptions moet bijhouden. De huidige
`state_broadcaster` heeft één gedeelde client-set; die wordt
opgesplitst per user_id.

**Admin-rol komt als nieuwe kolom in de users tabel.** Waarden
`'admin'` en `'user'`, default `'user'` voor nieuwe accounts. De
bestaande admin seed (`users(id=1, username='admin')`) krijgt
`role='admin'` bij de schema-bump. Dat vereist ofwel een schema
v4 met een destructieve drop-and-recreate zoals v3, ofwel een
idempotent `ALTER TABLE users ADD COLUMN role TEXT NOT NULL
DEFAULT 'user'` gevolgd door een `UPDATE users SET role='admin'
WHERE id=1`. De ADD COLUMN route is simpeler en geen data-loss —
sterk voorkeur, maar de beslissing hoort bij de implementatie.

## 3. Open vragen

**Auth-model.** De veiligste standaard-optie wint: server-side
sessies met een httpOnly + Secure + SameSite=Lax cookie, sessie-
store in SQLite (dezelfde `reverto.db`) of Redis. Geen JWT —
revocation is pijnlijk en we hebben geen stateless scaling-eis.
Geen OAuth — overkill zonder derde-partij SSO eis, introduceert
afhankelijkheden voor een solo-portal. De keuze staat nog niet
vast maar ligt stevig in die richting; de definitieve beslissing
valt vlak vóór de implementatie.

**Session-cookie lifecycle.** Sliding expiry (verlengt bij elke
request) versus absolute expiry (na X uur moet je opnieuw
inloggen) is nog onbeslist. Idem voor de logout-flow (server-side
sessie-invalidatie versus alleen cookie-delete) en CSRF-protectie
(SameSite=Lax dekt het grootste deel, maar sensitive POSTs
hebben mogelijk een extra token nodig).

**Signup-flow.** Self-service versus admin-invite-only. Voor een
platform dat met echt geld handelt leunt veiligheid richting
invite-only: een nieuwe user kan pas bestaan na een admin-actie.
Self-service zou een complete e-mail + verificatie + captcha
stack vereisen; dat is disproportioneel voor de huidige scope.
Uitstel tot de Phase-3 implementatie; waarschijnlijk invite-only.

**`tail_logs` startup scan (`web/app.py:1599`).** De huidige
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

## 4. Call-sites die Phase-3 attentie vragen

| Locatie                                     | Huidige scope     | Phase-3 scope                    | Notes                                           |
|---------------------------------------------|-------------------|----------------------------------|-------------------------------------------------|
| `web/routes/admin.py:121` emergency-stop    | alle bots         | admin cross-user + user bulk-stop | auth-check `role=admin`; user endpoint is nieuw |
| `web/app.py:1480` WS `watch_state_files`    | alle bots         | user-scoped subscriptions        | WS-handshake moet user identificeren            |
| `web/app.py:1513` WS broadcast iteratie     | alle bots         | per-user client set              | idem                                            |
| `web/app.py:1563` WS `/ws/state` seed       | alle bots         | alleen eigen bots                | snapshot filtert op user.id                     |
| `web/app.py:1604` `tail_logs` scan          | alle logs         | TBD (waarschijnlijk infra)       | verifieer of endpoint user-data surfacet        |
| `web/app.py` `_scan_user_dirs` DB-fail pad  | fail-open + WARN  | overweeg fail-closed / cache     | commit 8f0448a documenteert het trade-off       |

## 5. Afhankelijkheden / volgorde

De schema-bump met `role`-kolom moet eerst — anders heeft geen
enkele admin-check iets om op te rusten. Daarna het auth-model
(sessies, login endpoint, cookie-wiring) zodat `_request_user`
eindelijk een echte user uit de cookie kan halen in plaats van
de hardcoded admin. Pas dan hebben admin-rol checks op endpoints
zin, en als laatste komt de WebSocket-authenticatie +
user-scoped broadcaster. De WS-laag hangt op de auth, niet andersom.

## 6. Verwijzingen

- [Architecture: Multi-tenant foundation (Fase 1)](architecture.md#multi-tenant-foundation-fase-1)
- [Architecture: Multi-tenant filesystem layout (Fase 2)](architecture.md#multi-tenant-filesystem-layout-fase-2)
- [Runbook: Database reset](runbook.md#database-reset-multi-tenant-migration)
- [Runbook: Filesystem migration](runbook.md#filesystem-migration-fase-2)
- Audit v24 findings: MEDIUM #1 (chart.py scope) en #2 (registry
  users-cross-check) zijn closed in commits `67a136a` en `8f0448a`.
  De overige registry.all() call-sites uit de v24 survey staan in
  §4 hierboven en vallen onder de Phase-3 scope.
