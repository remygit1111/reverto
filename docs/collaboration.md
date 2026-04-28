markdown# Working with Claude on Reverto

Dit document beschrijft hoe Remy en Claude samenwerken aan Reverto. 
Bedoeld voor:

1. Toekomstige Claude-sessies (geüpload aan begin van sessie zodat 
   Claude direct context heeft over de werkwijze)
2. Remy als referentie voor patterns die werken
3. Operator (Phase B+) onboarding wanneer er meerdere mensen aan 
   Reverto werken

Dit is **geen** product-documentatie — daarvoor zijn er aparte 
documents (`architecture.md`, `security-model.md`, `runbook.md`, etc.). 
Dit is **collaboration-documentatie**: hoe we werken, niet wat we 
bouwen.

Dit document evolueert. Wanneer een werkwijze niet meer past, 
update.

---

## Tabel met inhoud

1. [Rolverdeling](#1-rolverdeling)
2. [Hoe een werksessie verloopt](#2-hoe-een-werksessie-verloopt)
3. [Prompt-structuur die werkt](#3-prompt-structuur-die-werkt)
4. [Audit-discipline](#4-audit-discipline)
5. [Deploy-discipline](#5-deploy-discipline)
6. [Risk-management](#6-risk-management)
7. [Wat NIET te doen](#7-wat-niet-te-doen)
8. [Tempo-realiteit](#8-tempo-realiteit)
9. [Bekende patterns](#9-bekende-patterns)
10. [Communicatie-stijl](#10-communicatie-stijl)
11. [Hoe dit document te onderhouden](#11-hoe-dit-document-te-onderhouden)

---

## 1. Rolverdeling

Drie partijen werken samen:

**Remy (architect + operator)**

- Strategische beslissingen: wat bouwen we, in welke volgorde, met 
  welke trade-offs
- Code-review op PR-niveau (klopt deze diff conceptueel?)
- Deploy-acties op productie-VPS
- Visuele review (ziet dit eruit zoals bedoeld?)
- Kritisch tegenlezen wanneer Claude een fout maakt
- Beslissingsbevoegdheid bij scope-vragen

Remy heeft expliciet "geen senior-developer" achtergrond. Vijf jaar 
gebruiker van bot-platforms, geen jaren actieve code-typer. Daardoor: 
geen verplichting om zelf code te schrijven. Wel architect-niveau 
beoordeling van diffs en ontwerp-keuzes.

**Claude (prompt-schrijver in chat)**

- Strategische sparring (welke aanpak past bij Reverto?)
- Audit-analyse (welke findings zijn relevant, welke al opgelost?)
- Prompt-schrijven voor Claude Code (de implementatie-laag)
- Review van Claude Code's PR-rapportages (kritische lezing)
- Deploy-instructies opstellen
- Memory-updates voor toekomstige sessies

Claude in chat heeft GEEN directe codebase-toegang. Alle code-changes 
gebeuren via prompts naar Claude Code.

**Claude Code (implementatie-laag)**

- Code-changes uitvoeren in de Reverto-Dev WSL2 omgeving
- Tests schrijven en draaien
- Sanity-checks (git stash → fail → pop → pass)
- PRs aanmaken en pushen naar GitHub
- Audit-doc updates
- Findings-tracker YAML-updates

Claude Code heeft directe codebase-toegang en kan zelfstandig 
beslissen over implementatie-details. Werkt op basis van prompts die 
Claude in chat schrijft.

---

## 2. Hoe een werksessie verloopt

Een typische sessie heeft een vast patroon:

**Fase 1: Operator brengt iets in**

Remy zegt iets als:
- "Ik wil X bouwen / fixen / onderzoeken"
- "Ik vraag me af of Y nog open is"
- "Kan jij even kijken naar Z?"

**Fase 2: Claude analyseert**

Voor de prompt: Claude analyseert wat er werkelijk nodig is. Vaak 
betekent dit:

- Diagnostiek-greps die Remy uitvoert
- Database-queries om bestaande state te begrijpen
- Audit-doc lookups voor bestaande findings
- Memory-recall van eerdere sessies

Geen prompt schrijven zonder eerst de werkelijke staat te begrijpen. 
Aannemen dat code is zoals memory zegt = bron van fouten.

**Fase 3: Ontwerp-discussie**

Claude stelt vragen waar geen "juist" antwoord is:
- "Welke library?"
- "Welke severity?"
- "Welke scope?"

Remy beslist. Claude geeft expertise-advies maar respecteert dat 
operator de eindbeslissing maakt.

**Fase 4: Prompt schrijven**

Claude schrijft een gestructureerde prompt voor Claude Code. Zie 
sectie 3 voor wat in elke prompt zit.

**Fase 5: Claude Code voert uit**

Remy plakt de prompt in Claude Code. Claude Code:
- Leest codebase
- Maakt wijzigingen
- Schrijft tests
- Doet sanity-check
- Push naar GitHub
- Stuurt samenvatting terug

**Fase 6: Review**

Remy plakt de samenvatting in de chat. Claude reviewed:
- Klopt de implementatie?
- Zijn er deviations van de prompt? (Vaak goed, soms niet)
- Class-of-issue protectie aanwezig?
- Sanity-check geslaagd?

**Fase 7: Deploy**

Claude schrijft deploy-instructies. Remy voert ze uit op de VPS 
(Hetzner CX23, reverto.bot).

**Fase 8: Verificatie**

Remy plakt deploy-output. Claude verifieert dat alles werkt zoals 
verwacht.

**Fase 9: Findings-tracker update**

Als de PR een audit-finding sluit: operator markeert het in 
Admin → Findings als resolved met resolution_ref.

**Soms slaan we fasen over.** Bijv. tweede iteratie van een feature 
gaat snel naar Fase 4. Of een puur diagnostische sessie eindigt bij 
Fase 3 zonder code-changes.

---

## 3. Prompt-structuur die werkt

Iedere PR-prompt heeft deze secties (in volgorde):
Feature/Fix: [korte titel]
═══════════════════════════════════════════════════════════════
CONTEXT
═══════════════════════════════════════════════════════════════

Achtergrond: welk probleem lossen we op
Bron: audit-finding, operator-feedback, eigen ontdekking
Operator-beslissingen die al gemaakt zijn

═══════════════════════════════════════════════════════════════
SCOPE-DISCIPLINE
═══════════════════════════════════════════════════════════════
WEL in scope: [expliciete lijst]
NIET in scope: [expliciete lijst — voorkomt scope-creep]
═══════════════════════════════════════════════════════════════
EERST LEZEN
═══════════════════════════════════════════════════════════════

Files die Claude Code moet bestuderen
Specifieke functions of regels
Bestaande patterns om te respecteren
Cross-references naar gerelateerde code

═══════════════════════════════════════════════════════════════
WIJZIGING
═══════════════════════════════════════════════════════════════
Per wijziging:

Locatie (file + regel-range)
Voor/Na pseudo-code of exacte diff
KRITIEK markers voor non-negotiable items
Edge cases om mee rekening te houden

═══════════════════════════════════════════════════════════════
TESTS
═══════════════════════════════════════════════════════════════

Class-of-issue regression tests
SANITY-CHECK procedure verplicht
Verwacht test-aantal (van X naar Y)

═══════════════════════════════════════════════════════════════
AUDIT-DOCUMENT UPDATE
═══════════════════════════════════════════════════════════════

Welke STATUS-markers updaten
Welke audit-doc(s) raken

═══════════════════════════════════════════════════════════════
FINDINGS-TRACKER UPDATE
═══════════════════════════════════════════════════════════════
Operator-actie post-deploy in Admin → Findings UI:

finding_id → status: resolved
resolution_ref: branch-naam of commit-hash

═══════════════════════════════════════════════════════════════
CHANGELOG
═══════════════════════════════════════════════════════════════

WEL of NIET changelog-waardig
Indien wel: draft entry voor operator om te posten

═══════════════════════════════════════════════════════════════
CACHE-BUSTERS
═══════════════════════════════════════════════════════════════

Frontend asset bumps wanneer relevant
"Geen" wanneer pure backend

═══════════════════════════════════════════════════════════════
BRANCH & COMMIT
═══════════════════════════════════════════════════════════════

Branch-naam (bijv. fix/x, feat/y, cleanup/z, docs/w)
Commit message-template

═══════════════════════════════════════════════════════════════
GATES
═══════════════════════════════════════════════════════════════
make test && make lint groen
Verwacht test-aantal (van X naar Y)
═══════════════════════════════════════════════════════════════
PUSH
═══════════════════════════════════════════════════════════════
git push commando + meld branch + PR-URL
═══════════════════════════════════════════════════════════════
Na implementatie — VERPLICHTE UITGEBREIDE SAMENVATTING
═══════════════════════════════════════════════════════════════
Lijst van wat Claude Code moet rapporteren:

Files gewijzigd (+/-, beschrijving)
Codebase-bevindingen (afwijkingen van prompt-aannames)
Implementatie-keuzes
Tests (aantal, sanity-check resultaat)
Lint + test status
Push-status (branch, PR-URL, commit-hash)
Verificatie-stappen voor operator post-deploy
Edge cases tegengekomen

═══════════════════════════════════════════════════════════════
POST-COMMIT NOTIFICATION (ALLERLAATSTE ACTIE)
═══════════════════════════════════════════════════════════════
Na samenvatting volledig gerapporteerd:
make beep
Dit is letterlijk het laatste commando.

**Key principles voor goede prompts:**

- **Specificiteit boven volume.** Claude Code begrijpt natural language 
  prima. Maar specifieke regels-nummers, exacte function-namen, 
  concrete pseudo-code zijn beter dan vaag handwaving.

- **EERST LEZEN expliciet.** Claude Code zou anders kunnen aannemen 
  dat code is zoals jij denkt. Dwing de codebase-onderzoek-stap.

- **NIET in scope sectie.** Voorkomt dat Claude Code "even snel" 
  gerelateerde dingen meeneemt die scope-creep introduceren.

- **KRITIEK markers.** Voor items die niet onderhandelbaar zijn 
  (security-correctheid, backwards compatibility, test-discipline). 
  Helpt Claude Code prioriteren.

- **SANITY-CHECK verplicht.** Niet alleen "tests slagen" maar ook 
  "tests falen op pre-fix code." Voorkomt vacuous-truth tests.

- **Edge cases benoemen.** Toekomstige debugger heeft baat bij weten 
  welke gevallen al overwogen zijn.

---

## 4. Audit-discipline

Reverto heeft een rijke audit-cultuur — saas-readiness-v1, v1.1, 
pre-deploy, v2, production-readiness-audit-v3, RHA-v1, v26-report, 
v27-report, plus PT-v1 en PT-v2 pentests. Plus de admin findings-
tracker met ~290 entries.

**Patterns die werken:**

**1. Verifieer claims via grep**

Audit-docs zeggen "RESOLVED in commit X" — vertrouw dat niet blind. 
Voor elke "resolved"-claim doet Claude Code een grep om te bevestigen 
dat de fix werkelijk in code zit.

Voorbeeld:
```bash
# Claim: v26-15h fixed
# Verificatie:
grep -n "stop_bot(bot.user_id" web/routes/admin.py
# Verwacht: line 145 met die exacte signature
```

**2. Status-mismatch tussen docs en code is normaal**

Audit-doc zegt "open." Code toont "resolved." Beide kunnen kloppen 
voor verschillende baseline-momenten. Documenteer expliciet welke 
waarheid de huidige is, niet "dit is fout in de doc."

**3. Class-of-issue regression-tests verplicht**

Bij elke fix: een test die voorkomt dat het issue opnieuw geïntroduceerd 
wordt. Niet alleen "deze specifieke regel werkt" maar ook "het pattern 
dat tot deze bug leidde wordt gevangen."

Voorbeeld: pt-043 PnL-fix heeft een test `test_denominator_is_current_price_not_avg` 
die expliciet zegt "als deze test faalt, is de formule terug naar 
linear-perpetual."

**4. Findings-tracker discipline**

Wanneer een fix-PR een finding sluit, voeg een FINDINGS-TRACKER UPDATE 
sectie toe aan de prompt met:
- Welke findings deze PR sluit
- Operator-instructies om in Admin UI status → resolved te zetten
- Resolution_ref (branch-naam of commit-hash)

DB van findings staat op productie-VPS, niet bereikbaar vanuit Claude 
Code in Reverto-Dev. Daarom: operator-actie post-deploy.

**5. Twee waarheden separeren**

Markdown audit-docs zijn **historische** records: wat zag de auditor 
toen, wat was de remediation-suggestie. Findings-tracker DB is **living**: 
wat is de huidige status, wie heeft het opgelost, wanneer.

Beide zijn legitiem. Niet één in de andere proberen te dupliceren.

---

## 5. Deploy-discipline

Reverto draait op:
- **Productie:** Hetzner CX23 VPS, reverto.bot, systemd-managed
- **Development:** Lokale WSL2 (Reverto-Dev) waar Claude Code werkt

**Patterns die werken:**

**1. Twee gescheiden bash-blokken**

Deploy-instructies hebben altijd twee aparte bash-blokken:
- VPS: pull + restart + verifieer
- Reverto-Dev: pull + tests + lint

Niet combineren. Operator wisselt tussen terminals.

**2. KillMode=process bescherming**

Bots (paper_engine subprocesses) overleven portal-restart dankzij 
systemd's `KillMode=process`. Wanneer een PR een schema-versie bumpt, 
moet operator handmatig de bot herstarten om de nieuwe code te laden.

**3. Pre-deploy state-check**

Voor PRs die login-flow, schema, of permissions wijzigen: check de 
huidige state vóór deploy.

Voorbeeld: voor TOTP login-integration deploy, eerst:
```sql
SELECT id, username, totp_seed_encrypted IS NOT NULL FROM users;
```

Als state niet matcht met wat de prompt aanneemt: aanpassingen vóór 
deploy.

**4. Recovery-pad altijd paraat**

Bij elke deploy met lockout-risico: een tweede terminal met 
recovery-commando paraat. Niet uitvoeren, wel klaar om te plakken.

Voorbeeld bij TOTP login-integration:
```bash
# Recovery paraat:
sqlite3 ~/reverto/logs/reverto.db \
  "UPDATE users SET totp_seed_encrypted = NULL WHERE id = 1;"
```

**5. Post-deploy operator-acties expliciet markeren**

Sommige stappen kan Claude Code niet doen omdat het VPS-toegang 
vereist:
- Service-file aanpassingen
- ReadWritePaths uitbreiding
- Findings-tracker UI status-updates

Markeer deze expliciet in deploy-instructies als "operator-actie 
post-deploy."

**6. Schema-migration verifieer-stap**

Voor PRs die SCHEMA_VERSION bumpen, na deploy:
```bash
sqlite3 ~/reverto/logs/reverto.db "PRAGMA user_version;"
sqlite3 ~/reverto/logs/reverto.db "PRAGMA table_info(users);"
```

Bevestigt dat migration werkelijk gedraaid heeft (niet alleen dat 
service active is).

---

## 6. Risk-management

Niet elke PR heeft hetzelfde risico-profiel. Pattern voor risk-aware 
deploys:

**Lage risico (frontend tweaks, dead-code cleanup):**
- Standard pull + restart
- Browser hard-refresh
- Visual check

**Medium risico (backend logic, database changes):**
- Pre-deploy state-check
- Schema-migration verifieren
- Bot-overleving check (KillMode=process)

**Hoge risico (auth-flow, security-critical):**
- Recovery-procedure paraat in tweede terminal
- Test 1 = "wachtwoord-only login werkt nog" — STOP bij failure
- Stage-by-stage verificatie (geen "alle tests in één keer")
- Lockout-risico expliciet benoemen

**Patterns die expliciet risico hebben:**

1. **Service-file wijzigingen** — kunnen lockout veroorzaken via 
   systemd hardening
2. **Login-flow wijzigingen** — kunnen operator zelf uitsluiten
3. **Encryption-key handling** — corruptie betekent data-loss
4. **Schema migrations** — silent-fail mogelijk

**Voor elk type: dedicated recovery-procedure.**

**Bij issue na deploy:** STOP, diagnose voor doorgaan. Niet "even nog 
proberen." We hebben vandaag (2026-04-28) gezien dat 8 of 13 retries 
van een crash-loop oplevert in plaats van problemen oplossen.

---

## 7. Wat NIET te doen

Patterns die op het eerste gezicht goed lijken maar slechte uitkomsten 
hebben:

**1. Niet "alle gerelateerde issues" in één PR meenemen**

Claude Code voelt soms drang om "even alle exception-handlers op te 
ruimen" terwijl de prompt over één specifieke ging. Class-of-issue is 
prima, scope-creep niet. Expliciet NIET-in-scope sectie helpt.

**2. Niet blind audit-claims volgen**

PT-v2 zelf had een math-fout in pt-043's voorgestelde fix. Audit-docs 
worden geschreven door mensen, mensen maken fouten. Verifieer altijd 
empirisch (testnet-validatie, grep-bevestiging).

**3. Niet "alle tests met mocks"**

Voor security-flows, formule-validaties, en data-integriteit: real 
data of testnet-references zijn waardevol. Mocks zijn fijn voor 
unit-tests; class-of-issue regression-tests willen vaak echte 
referenties.

**4. Niet "alle UI-iteraties in één PR"**

Vandaag (2026-04-28) hadden we drie iteraties op rha-004 dashboard-
resilience. Eerste PR introduceerde een visuele tegenstrijdigheid. 
Tweede PR fixte het. Derde PR was een UX-bug fix. Apart was beter dan 
één grote PR met alle drie pogingen.

**5. Niet "code-fixes voor problemen die operator-actie vereisen"**

Soms is het juiste pad een service-file aanpassing of een SQL-query, 
geen code-PR. Dwing geen code-fix als de root cause buiten code ligt.

**6. Niet "vertrouwen op memory zonder verifiëren"**

Memory in chat-sessies is fragiel. Audit-doc-locaties veranderen, 
schema-versies bumpen, branches worden hernoemd. Voor elke claim die 
materieel is voor een PR-beslissing: verifieer met concrete grep of 
SQL-query.

**7. Niet "blind prompts volgen als codebase ze tegenspreekt"**

Mijn prompts kunnen API-fouten bevatten (zoals `pyotp.utils.base32_string` 
die niet bestaat). Claude Code moet correctie maken en expliciet 
documenteren in samenvatting.

---

## 8. Tempo-realiteit

Claude Code voltooit prompts in **minuten, niet dagen**. Voor 
estimates:

| Prompt-grootte | Claude Code tijd | Voorbeeld |
|---|---|---|
| 100-200 regels | 5-10 min | Tweak, dead-code cleanup |
| 200-500 regels | 10-20 min | Klein feature, bug-fix |
| 500-800 regels | 20-30 min | Medium feature, audit fix |
| 800+ regels | 30-50 min | Wrap-up PR, multi-deliverable |

**Wat dit betekent:**

- "4-6 dagen werk" estimates uit traditionele dev-context zijn 
  structureel 50-100x te hoog
- Een grote PR is geen meerdaags traject, hooguit een uur
- Multipele PRs op één dag is normaal, niet uitzonderlijk

**Maar tempo betekent niet "minder zorgvuldig":**

Elke PR krijgt:
- Codebase-onderzoek vooraf
- Sanity-check
- Class-of-issue regression-tests
- Audit-doc updates
- Operator-bewuste deploy-instructies

Tempo komt door:
1. Claude Code's directe codebase-toegang (geen file-by-file context-
   switching)
2. Mijn prompts die complete context vooraf geven (minder back-and-forth)
3. Reverto's discipline rondom tests + audit (minder regression-debugging)

---

## 9. Bekende patterns

Werkende design-patterns in Reverto die je kunt hergebruiken in 
nieuwe PRs:

**1. Per-user Fernet encryption via CredentialProvider**

Sinds Phase A wrap-up (PR #114). Generieke `encrypt_for_user(user_id, 
plaintext)` en `decrypt_for_user(user_id, ciphertext)` op 
FernetCredentialProvider. Phase C signing-service kan dit later 
overnemen via dezelfde interface.

**2. Cookie-based pending-state met itsdangerous**

Twee voorbeelden in code:
- Pending-TOTP enrollment cookie (PR 2): salt 
  `reverto.totp_pending.v1`, 10 min TTL
- Pending-login-TOTP cookie (PR 3): salt 
  `reverto.login_totp_pending.v1`, 2 min TTL

Aparte salts voorkomen confused-deputy attacks tussen cookie-types.

**3. Idempotente seed-scripts**

`scripts/seed_audit_findings.py` met `INSERT OR IGNORE`. Re-run is 
no-op. Pattern voor toekomstige bulk-imports.

**4. Schema-migration met additive-default**

`SCHEMA_VERSION` constant in `core/database.py`. Additive ALTER TABLE 
met DEFAULT NULL. Bestaande rows krijgen NULL bij upgrade. 
`_LAST_DESTRUCTIVE_VERSION` gate voor breaking changes.

**5. Audit-log shape**

`_audit(action, slug, user, user_id, request)` met:
- `slug` = target identifier (finding_id, bot_slug, deal_id)
- `user` = actor format `session:<username>`
- `ip` + `result` velden uit Phase A wrap-up

**6. Per-PR cache-busters**

`web/static/index.html` heeft:
```html
<link rel="stylesheet" href="/static/style.css?v=N">
<script src="/static/app.js?v=N"></script>
```

Bij elke frontend-wijziging: bump N. Tests pinnen `>= N` (niet `==`) 
zodat toekomstige bumps niet breken.

**7. Modal-pattern**

Bestaand `modal-overlay` + `modal-card` classes met `hidden` toggle. 
Hergebruik voor alle nieuwe modals. Geen nieuwe modal-frameworks 
introduceren.

**8. Test-fixture hergebruik**

`tmp_store` fixture monkeypatcht `paths.BASE_DIR` voor isolatie. 
Hergebruikt door credentials-, totp-, en findings-tests. Pattern voor 
elke test die filesystem raakt.

---

## 10. Communicatie-stijl

Hoe we met elkaar praten heeft impact op effectiviteit. Geleerde 
lessen:

**Stijl-elementen die werken:**

**1. Eerst-de-realiteit-checken**

Voor we acties nemen: weten we wat er werkelijk is? Zo niet: 
diagnostiek-greps voorrang boven actie. Voorbeelden:
- "Welke audit-docs bestaan er?" → ls + grep
- "Werkt de fix in productie?" → SQL + curl
- "Wat zegt het audit-document letterlijk?" → cat met view-range

**2. Aantonen, niet aannemen**

Wanneer een claim materieel is voor een beslissing: bewijs vragen.
- "v27-04 is fixed" → grep voor SRI hashes
- "Pt-043 is reëel" → testnet-validatie met echte cijfers
- "DB heeft v9 schema" → PRAGMA user_version

**3. Eerlijk over onzekerheid**

Claude maakt fouten. Wanneer Claude denkt dat iets klopt maar niet 
zeker is: zeg dat. "Mijn vermoeden" is beter dan stellige claim die 
fout kan zijn. Voorbeelden:
- "Mijn voorkeur is A maar het is jouw call"
- "Ik weet niet hoe X werkt zonder verifiëren"
- "Mijn API-call hierboven kan fout zijn — Claude Code moet 
  verifiëren"

**4. Concrete vragen, geen open-ended**

In plaats van "wat denk je?": specifieke A/B/C keuzes. Sneller 
beslissen, minder ambiguïteit.

**5. Erkennen wanneer prompt fout was**

Mijn prompts kunnen API-fouten of locatie-fouten bevatten. Claude 
Code documenteert deze in samenvattingen ("prompt zei X maar werkelijke 
API is Y"). Dat is zegen, niet kritiek. Verbetert toekomstige prompts.

**6. Niet over-engineeren in chat**

Wanneer een vraag een simpel antwoord verdient: simpel antwoord. Niet 
elke vraag heeft drie opties + matrix-tabel + aanbeveling. Calibratie 
op moeilijkheid van vraag.

**Stijl-elementen die NIET werken:**

**1. Niet "alles is uitstekend gedaan"**

Wanneer alles werkelijk uitstekend was: zeg dat één keer. Niet bij 
elke samenvatting. Inflatie van complimenten verwatert hun betekenis.

**2. Niet "we kunnen sowieso doorgaan"**

Wanneer er een echt risico of bug is: stop. Niet "we kunnen wel verder 
maar..." Direct benoemen.

**3. Niet "ik weet wel het juiste antwoord"**

Vooral bij architectuur-keuzes: er is vaak geen objectief juist 
antwoord. Een keuze is een trade-off. Benoem die trade-offs, laat 
operator beslissen.

---

## 11. Hoe dit document te onderhouden

Dit document evolueert met onze werkwijze. Wanneer een nieuw pattern 
ontstaat of een oud pattern niet meer past:

**Updaten via mini-PR:**
docs(collab): [korte beschrijving van wijziging]
[langere uitleg waarom de wijziging nodig was, wat nu anders is]

Net zoals andere docs in `docs/` folder. Version-controlled in git, 
reviewbaar voor toekomst.

**Wanneer updaten:**

- Na elke "interessante" sessie (ontdekking, ongebruikelijke 
  patroon, lesson-learned)
- Bij scope-keuzes die een nieuw beslissingsmodel vereisen
- Bij architecturale beslissingen die werkwijze veranderen
- Bij realisatie dat huidig document een verkeerde aanname bevat

**Wat NIET in dit document hoort:**

- Reverto-specifieke product-keuzes (gaan in `architecture.md` of 
  `phase-3.md`)
- Security-policy (gaat in `security-model.md`)
- Operator-procedures voor incidents (gaan in `runbook.md`)
- Specifieke audit-findings (gaan in audit-docs of findings-tracker)

Dit document blijft **collaboration-focused** — hoe we werken, niet 
wat we bouwen.

---

## Document changelog

- 2026-04-28: Initial v1 draft. Captures patterns from first 9 days 
  of Reverto development (2026-04-19 tot 2026-04-28). Will evolve.
