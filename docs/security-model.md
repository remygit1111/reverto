# Reverto Security Model

**Classification:** Internal — bedoeld voor intern gebruik binnen
Reverto development. Niet extern delen zonder review, omdat het
document concrete drempel-waarden, detection-windows en architectural
specifics bevat die in een latere phase naar een separate restricted
operational-params document kunnen verhuizen.

**Status:** Engineering-spec, v1 (2026-04-20).
**Scope:** Multi-tenant SaaS-transitie, horizon 12-24 maanden.
**Audience:** Eigen team + toekomstige externe security-review.

Dit document is de referentie voor alle security-gerelateerde keuzes.
Het beschrijft de _doel_-architectuur, niet de huidige code.
Gaps tussen huidige state en target worden expliciet gemaakt zodat de
migration-roadmap (Part 4) per laag stuurbaar blijft.

---

## Part 1 · Context & Principes

### 1.1 Waarom dit document bestaat

Reverto draait nu als single-tenant bot-platform op één operator-host
(Reverto-Server, WSL2 + local Python). Phase-3a (gemerged 2026-04-20) heeft de
auth-laag op DB-basis gezet en de `_request_user` bridge opgeleverd, wat
de foundation legt voor multi-user. Het concrete doel is om binnen
12-24 maanden een multi-tenant SaaS te draaien waar andere users hun
eigen bots kunnen aanmaken, hun eigen exchange-credentials opslaan en
hun eigen trades laten uitvoeren.

Multi-tenant SaaS met custody-achtige properties (we bewaren exchange
API-keys) verandert het threat-model fundamenteel. Eén gecompromitteerde
host betekent in de single-tenant wereld "de operator is zijn eigen
geld kwijt"; in de SaaS wereld betekent hetzelfde "N users zijn hun
geld kwijt en Reverto is stuk." Dit document beschrijft hoe we dat
tweede scenario voorkomen, of ten minste welke maatregelen blijven
overeind als delen van de stack alsnog vallen.

### 1.2 Referentie-incident: 3Commas (december 2022)

3Commas was een custodial bot-platform vergelijkbaar met Reverto. In
december 2022 zijn API-keys van duizenden users gelekt. Het aanvalspad
is nooit volledig publiek gemaakt; consensus in de community is dat
ofwel een insider ofwel een server-compromise de credential-store heeft
uitgelezen. Users verloren tot honderden miljoenen USD cumulatief.

Relevante lessen voor Reverto:

- **API-keys zonder withdrawal-permission zijn niet waardeloos.** De
  attacker kan met trade-only keys nog steeds pump-and-dump-achtige
  market-manipulation orders plaatsen om de balance uit te wassen
  tegen een pre-bepaalde coin die de attacker vooruit heeft gekocht.
- **IP-whitelisting op exchange-niveau hielp users die het hadden
  gezet.** Users die deze stap hadden overgeslagen bij onboarding
  verloren wel, users met IP-whitelist verloren niet (de attacker kon
  orders plaatsen maar niet vanaf het attacker-IP).
- **Detection was traag.** Users merkten het pas toen hun balance
  al weg was. Geen onafhankelijk monitoring-systeem vergeleek
  expected-state met actual-balance.

Deze drie observaties sturen Part 3.5 (exchange-hardening bij
onboarding) en Part 3.6 Laag 7 (onafhankelijke watchdog).

### 1.3 Core principes

**Defense-in-depth.** Geen enkele laag is sufficient. Als de
hoofd-applicatie volledig gecompromitteerd is, moeten nog steeds
meerdere defense-mechanismen overeind blijven — in de limiet moet
één gecompromitteerde Reverto-server _niet_ alle users' balance
kunnen legen. Elke laag in Part 3.6 wordt getoetst op deze
voorwaarde (zie Part 3.7 Independence Verification).

**Least-privilege.** Elke credential heeft de minimale scope die de
functie vereist. Exchange-keys krijgen alleen trade-permissions
(geen withdraw). De hoofdapp heeft geen toegang tot
exchange-api-secrets (die leven in de signing-service). Nieuwe
operators krijgen `role='user'`, niet `admin`.

**Independent defenses.** Defense-mechanismen die op dezelfde
infrastructuur leven als wat ze beschermen, beschermen tegen
domheid maar niet tegen compromise. Daarom krijgt de watchdog
(Part 3.6 Laag 7) een aparte deployment en leven approval-keys op
user-devices (Part 3.3).

**Fail-closed defaults.** Bij twijfel zegt het systeem nee. Een
defecte cap-calculatie weigert de trade. Een onbeschikbare
signing-service schort trading op. Een missende onboarding-
verification blokkeert account-activatie. Dit principe is al in
Phase-3a toegepast op `_scan_user_dirs` (fail-closed na N
DB-failures) en `verify_password` (NULL hash = weigeren).

### 1.4 Non-goals

Expliciet niet in scope van dit document:

- **On-chain / DeFi trading.** Reverto is een CEX-bot-platform
  (Bitget, Kraken). On-chain wallets en smart contracts liggen
  buiten de architectuur.
- **Fiat-onramp.** Users brengen zelf hun balance naar de exchange.
  Reverto raakt geen fiat.
- **KYC / AML.** De exchange doet de KYC op hun kant. Reverto
  gebruikt daaromheen.
- **Custody van coins.** Reverto houdt geen coins vast — alleen
  exchange-credentials om te handelen namens de user op zijn eigen
  exchange-account.
- **Real-time market-making of HFT strategies.** Reverto is DCA /
  swing-trading; de latency-requirements zijn seconds-scale, niet
  millisecond.
- **Jurisdiction-compliance spec.** Apart document zodra we de
  eerste jurisdictie kiezen (zie Part 6).

---

## Part 2 · Threat Model

Zeven scenarios. Per scenario: welke aanvaller, welke capabilities,
wat de huidige state (single-tenant Reverto-Server) biedt, wat de
target state (multi-tenant SaaS) moet bieden. Elke rij in de vergelijkings-tabel
benoemt één capability — de aanvals-capability wordt uitgesplitst
zodat gaps per laag zichtbaar blijven.

### 2.1 Scenario: Reverto-server compromise (full infra takeover)

Attacker heeft root/admin op de Reverto-host. Kan bestanden lezen,
processen starten, netwerk-verkeer onderscheppen, de DB muteren. Het
klassieke post-breach scenario.

| Capability | Current state (single-tenant Reverto-Server) | Target state (multi-tenant SaaS) |
|------------|------------------------------------|----------------------------------|
| Lees alle user passwords | bcrypt-gehasht, niet plaintext. `password_hash` is in DB — hashes leaken, passwords niet tenzij zwak. | Ongewijzigd: bcrypt rounds=12. Reverse vereist offline cracking. |
| Lees alle exchange API-keys | **Ja** — `credentials/<uid>/*.enc` + `keys/<uid>.key` staan beide op dezelfde host. Fernet-decrypt is triviaal met beide files. | **Nee** — API-secrets leven in de signing-service, niet in de main app. Main app ziet alleen de public `api_key` voor display/routing. |
| Plaats orders namens users | **Ja** — direct via de geladen credentials. | Alleen binnen de scope-whitelist van de signing-service, en alleen onder de rolling caps. Kan niet buiten de user's daily/weekly/monthly limits trade. |
| Withdraw user funds | **Nee**, mits user IP-whitelist + withdraw-blacklist heeft gezet op exchange (dit is de onboarding-requirement uit Part 3.5). | Ongewijzigd: exchange-side hardening. Reverto kan deze step niet overslaan. |
| Modify cap-settings | **N/A** single-user, operator is zichzelf. | Caps-wijziging vereist user-approval via PWA WebAuthn of TOTP plus cooldown. Server-side code-wijziging raakt de user-device approval niet. |
| Ontsla onafhankelijke watchdog | **N/A** geen aparte watchdog. | Watchdog leeft op separate infrastructuur (Part 3.6 Laag 7); main-app compromise raakt de watchdog niet. |
| Draai emergency-stop en rollback balances | **Nee** — balance zit op exchange, niet bij Reverto. | Ongewijzigd — structurele eigenschap van custodial-API, niet custodial-fund model. |

Belangrijkste delta: de main-app bezit niet langer de sleutels die
nodig zijn om te handelen. Server-compromise kan signing requests
uitlokken, maar alleen binnen de scope die de signing-service toestaat.

### 2.2 Scenario: User-credential compromise (phishing, credential stuffing)

Attacker heeft een geldig `username + password` paar van de user.
Phishing, credential-stuffing uit een ander lek, of gewoon een zwak
password dat offline is gekraakt.

| Capability | Current state | Target state |
|------------|---------------|--------------|
| Inloggen op Reverto-portal | **Ja** — alleen password vereist. | **Nee** — TOTP is verplicht (Part 3.3). Password zonder TOTP-code levert een 401. |
| Inloggen als TOTP-seed ook gelekt | **N/A** geen TOTP. | Ja — compromise van zowel password als TOTP-seed is een device-compromise (Part 2.3). Valt terug op WebAuthn/YubiKey approvals voor mutating operations. |
| Placeholder-trades binnen threshold | **Ja**, volledige trading-access. | Ja tot per-trade threshold; erboven vereist approval-channel. |
| Grote trade plaatsen (> per-trade threshold) | **Ja**. | **Nee** — approval-channel blokkeert (Part 3.4). |
| Dagelijkse / wekelijkse caps verhogen | **N/A** geen caps. | Alleen met approval-channel + cooldown (Part 3.4: 48h voor cap-changes). |
| Nieuwe exchange-key toevoegen | **Ja**. | Alleen met approval-channel + cooldown (24h voor new-key; onboarding-verification blijft). |
| Dead-man-switch omzeilen | **N/A**. | **Nee** — dead-man-switch hangt op user-device activity, niet op login. Een passwordlekker kan niet de user-device namaken. |

### 2.3 Scenario: User-device compromise (malware op PC/telefoon)

Attacker heeft de browser-session of PWA-token van de user, of zelfs
volledige control over het user-device. Kan de user-UI sessie
overnemen, cookies exfiltreren, pasted TOTP-codes lezen.

| Capability | Current state | Target state |
|------------|---------------|--------------|
| Session-cookie stelen | **Ja** via XSS of device-compromise. `samesite=strict` + `httponly` beperken exfil via JS/CSS maar niet via volledige device-compromise. | Ongewijzigd qua cookie-properties. TOTP bij login nog niet ingewisseld = de session bestaat niet om te stelen; TOTP bij login ingewisseld = dezelfde exposure als nu. |
| Bypass TOTP | **N/A**. | TOTP-code is niet her-bruikbaar na gebruik (30s window + 1-use enforcement serverside) — device-compromise tijdens actieve session leest geen TOTP-code omdat die al verbruikt is. |
| Plaats trades via PWA | **N/A**. | Alleen binnen threshold (Part 3.4) zonder re-approval. Grote trades vereisen tap-to-sign op de PWA — een browser-session-diefstal kan deze device-bound key niet gebruiken, want de private key staat niet in de cookie maar op het device (secure-enclave waar beschikbaar, OS keystore anders). |
| Register new approval device | **N/A**. | Vereist approval van existing device + 48h cooldown. Device-compromise kan niet een tweede device koppelen zonder óf de eerste device te verliezen óf 48 uur te wachten terwijl de user merkt. |
| Exfiltreer exchange-credentials | **Ja** als device de volledige Reverto-sessie heeft — portal kan `has_keys` listen en implicit de api_key (niet de secret) tonen. | API-secrets leven nooit in de browser. Main-app toont alleen masked api_key strings. Signing-service is de enige die secrets heeft. |
| Dead-man-switch omzeilen | **N/A**. | Kan alleen omzeild worden door actief te blijven op het device — maar dan ziet de user het user-interface activity buiten zijn eigen sessies. Dead-man-timeout reset alleen op user-device authenticated actions, niet op passive cookie-present. |

Device-compromise is in elk threat-model het hardst — dit is waarom
WebAuthn en hardware-keys bestaan, en waarom de approval-hierarchy
(Part 3.4) zware acties aan device-bound keys koppelt, niet aan
"iemand heeft het cookie."

### 2.4 Scenario: Insider threat (medewerker met DB-access)

Attacker is operator / on-call engineer / contractor met legitieme
DB-access. Leest rows rechtstreeks, misschien zelfs DROP/UPDATE.

| Capability | Current state | Target state |
|------------|---------------|--------------|
| Lees password-hashes | **Ja** — `users.password_hash` in DB. bcrypt maakt offline cracking nodig. | Ongewijzigd. |
| Lees TOTP-seeds | **N/A**. | TOTP-seeds zijn encrypted met een key die niet in de main-DB leeft (Part 3.2). Access tot main-DB ≠ access tot decryption key. |
| Lees exchange-credentials | **Ja** — `keys/` + `credentials/` staan op de main-host. | **Nee** — credentials leven in signing-service, die eigen DB + sleutels heeft. Insider met main-DB-access heeft geen credentials-decryption capability. |
| Reset user session | **Ja** — `UPDATE users SET session_epoch = session_epoch + 999`. Effect: user wordt uitgelogd, moet opnieuw inloggen (vereist TOTP target state). | Ongewijzigd — deze capability is acceptabel (operator moet users kunnen uitloggen voor incident-response) maar gelogd in audit-trail. |
| Flip `active=0` om user te blokkeren | **Ja** — direct via DB. | Ongewijzigd, maar dit triggert watchdog alert (unexpected user-state change buiten portal-API). |
| Bypass caps door DB-state te muteren | **N/A**. | Cap-state leeft deels in signing-service (rolling counters); main-DB insider kan user's "max-balance" veld muteren maar niet de signing-service counters resetten zonder ook signing-service compromise. |
| Forge audit-log-entry | **Ja** — DB write-access. | Audit-log zou naar append-only store (apart filesystem of externe log-aggregator) moeten gaan; hierdoor wordt falsificatie detectable door ontbrekende of inconsistente records. |

Insider threat is in multi-tenant SaaS een reëel scenario. Key design
choice is dat de signing-service een aparte trust-boundary is — een
insider met main-app access komt nog niet bij exchange-credentials of
bij trading-execution-keys.

### 2.5 Scenario: Supply-chain attack (compromised dependency)

Attacker heeft een PyPI-package gecompromitteerd die Reverto
installeert (direct of transitive). Malicious code runt met dezelfde
rechten als de hoofd-process.

| Capability | Current state | Target state |
|------------|---------------|--------------|
| Detectie van bekende CVE's | `pip-audit --strict` in CI blocking op direct deps; transitive non-blocking. | Ongewijzigd, aangevuld met periodieke quarterly review van transitive deps (runbook). |
| Runtime verificatie van package-hashes | **Nee** — pin-by-version, geen `--require-hashes`. | Pin-by-hash in `requirements.txt` (pip kan dit als `package==X --hash=sha256:...` per line). |
| Attacker bypasst pip-audit door een net-niet-gerapporteerde backdoor | **Ja** — `pip-audit` detecteert alleen CVE's die in OSV/GHSA/etc staan. Een fresh supply-chain attack (xz-util-pattern) duurt dagen tot weken om publiek te worden. | Dezelfde limiet — `pip-audit` is probabilistic, niet deterministic. Mitigatie: minimaal aantal deps, pin-by-hash, smoke-import check (reeds in CI). |
| Extract exchange-secrets bij runtime | **Ja** — credentials zijn in het proces-geheugen gedurende `_user_fernet(uid).decrypt(...)`. | Main-app heeft geen secrets. Signing-service wel, maar is een veel kleiner stuk code met een smalle dependency-graph (Fernet + HTTP server + audit-log — ideaal enkele tientallen deps). Kleinere blast-radius. |
| Extract TOTP-seeds | **N/A**. | Seeds leven in DB van de main-app encrypted; de encryption-key leeft in de signing-service (Part 3.2). Main-app process-heap bevat TOTP-seeds alleen tijdens login-verify en dan kort. |

### 2.6 Scenario: Exchange-side API-leak

Attacker heeft de user's exchange-API-key + secret. Route irrelevant
— kan via phishing op een andere site zijn, een browser-extension,
een oude YAML-dump van de user zelf. Reverto is niet de bron van het
lek; de exchange-credentials zijn elders gecompromitteerd.

| Capability | Current state | Target state |
|------------|---------------|--------------|
| Plaats orders buiten Reverto | Ja, als exchange geen IP-whitelist heeft. | Ongewijzigd — dit is exchange-side. Reverto kan user onboarden met IP-whitelist als harde verificatie (Part 3.5). |
| Withdraw user funds | Alleen als API-key `withdraw` permission heeft. | Onboarding-flow verifieert dat de API-key _niet_ `withdraw` permission heeft — weigert anders account-activatie (Part 3.5). |
| Detect dat keys gelekt zijn | **Nee** — Reverto merkt niet dat een attacker orders plaatst buitenom. | Watchdog (Part 3.6 Laag 7) vergelijkt expected balance met actual balance; significant drift triggert alert. |
| Rotate keys snel | Manual: user moet in portal nieuwe keys uploaden + oude verwijderen, naar de exchange gaan om oude te revoken. | Onboarding UI heeft een "rotate keys" flow die de user door beide stappen heen loopt, plus watchdog-pauze tijdens rotation. |

### 2.7 Scenario: Key-rotation race

Reverto rotateert een user's exchange-keys (of zijn eigen Fernet-key).
Gedurende de rotation is er een venster waarin zowel oude als nieuwe
keys geldig zijn.

| Capability | Current state | Target state |
|------------|---------------|--------------|
| Rotation semantiek | `core/credentials.py:rotate_fernet_key(user_id=...)` roteert per-user; commit-order is key-first dan .enc-files. Backup in `.bak.<ts>`. | Ongewijzigd qua per-user Fernet-key. Exchange-key rotation is een nieuwe flow (niet geïmplementeerd in current) — spec: pause trading, rotate, verify new key works, kill old key. |
| Zichtbare race-window | Sub-seconde voor Fernet-rotation (bestands-ops). Exchange-key rotation is minutes-scale door user-interaction op exchange. | Exchange-key rotation tijdens de verify-step: trading is gepauzeerd, dus attacker met oude keys kan bij Reverto geen orders meer plaatsen via portal, maar wel direct bij de exchange. IP-whitelist blokkeert dat. |
| Crash-recovery mid-rotation | `.bak.<ts>` backup geeft rollback-pad; geen automatische detection van half-rotate state. | Explicit rotation-state in DB (`users.rotation_state in ('none', 'pending', 'verifying', 'complete')`) zodat het proces bij restart weet waar het stopte. |

### 2.8 Scenario: Signing-service compromise

Aanvaller heeft volledige toegang tot de signing-service
infrastructure: leest de credential-DB, kan de Master Key ontsluiten
en daarmee DEK's + TOTP-seeds + exchange-credentials decrypten, kan
detection-logica wijzigen, kan de interne audit-log manipuleren,
kan arbitrary signed trade-requests naar exchanges sturen. Dit
scenario is architectural-critical omdat Part 3.1's service-
separatie juist de signing-service als trust-boundary centraal
plaatst — een compromise hier is een systemic failure, geen
geïsoleerd incident.

| Capability | Current state (single-tenant Reverto-Server) | Target state (multi-tenant SaaS) |
|------------|------------------------------------|----------------------------------|
| Lees alle user API-keys in plaintext | **N/A** — signing-service bestaat nog niet; credentials leven in main-app. Scenario 2.1 dekt die current-state. | **Ja** — volledige exfiltratie van credentials mogelijk; alle per-user `.enc` files zijn decryptbaar met MK. |
| Bypass Lagen 2/3 (volume-caps) | **N/A** | **Ja** — de cap-counters en rolling-max-berekeningen leven in de signing-service DB. Enforcement wordt door de aanvaller gemaskeerd of uitgeschakeld. |
| Bypass Laag 4 (performance-scaling) | **N/A** | **Ja** — tier-evaluatie-logica en baseline-state leven hier. Anti-gaming asymmetry (alleen strengere caps bij underperformance) is weg zodra logic wordt omzeild. |
| Bypass Laag 6 (anomaly detection) | **N/A** | **Ja** — baseline-storage en detection-logica leven hier. |
| Bypass Laag 5 (emergency floor) | **N/A** | **Ja** — floor-berekening en pause-mechanisme leven hier. |
| Genereer valide trade-signatures zonder user-approval | **N/A** | **Ja** — scope-whitelist en approval-token-verificatie zijn lokaal in de signing-service. |
| Omzeilen van exchange-side IP-whitelist | Nee | Nee (exchange-side enforcement, leeft niet bij Reverto). Beperkt echter niet wat een aanvaller vanuit de signing-service-IP zelf doet. |
| Omzeilen van exchange-side withdraw-whitelist | Nee | Nee (exchange-side). Trade-only keys blijven trade-only. |
| Omzeilen van exchange-side trade-only permission | Nee | Nee (exchange-side). |
| Bypass watchdog balance-drop detection | N/A | Nee — mits de watchdog echt onafhankelijk gedeployed is per Laag 7 requirements 1-4. De watchdog-data-feed-eis (Part 3.6 Laag 7 requirement 4) zorgt dat de aanvaller niet via main-app of signing-service de expected-state kan manipuleren. |
| Bypass dead-man-switch (Laag 8) | N/A | Gedeeltelijk — signing-service enforced de pause, dus aanvaller kan de enforcement uitschakelen. User-device activity blijft echter onvervalsbaar; de watchdog kan ontbrekende user-activity detecteren. |
| Verzwijg audit-trail | N/A | Ja binnen signing-service; extern-shipped audit-log (naar append-only store) blijft intact als die bestaat. |

**Kernobservatie.** Signing-service compromise is een systemic
failure. Vrijwel alle interne defenses (Lagen 2, 3, 4, 5, 6 en
delen van 8) vallen tegelijk omdat ze architecturaal op dezelfde
trust-boundary hangen. De enige overblijvende verdediging is wat
**buiten Reverto's eigen infrastructure** leeft:

- Exchange-side hardening (trade-only keys, IP-whitelist,
  withdraw-whitelist, sub-account scope waar beschikbaar) — blijft
  intact. Beperkt wat een aanvaller maximaal kan doen met gestolen
  credentials.
- Onafhankelijke watchdog per Laag 7 eisen — blijft intact mits
  echt onafhankelijk gedeployed (separate credentials, deployment-
  pipeline, alerting-channel, én data-feed). Detecteert balance-
  drift en stuurt kill-signal.
- User-device approval-keys (WebAuthn, YubiKey) — blijven intact.
  Aanvaller heeft de device-private-keys niet; kan approval-
  tokens verifiëren uit-zetten binnen de signing-service maar kan
  geen valide user-signatures produceren voor endpoints die buiten
  de signing-service checken (bv. watchdog-side config-changes in
  Phase F+).

**Mitigatie-strategie.**

- **Attack-surface-minimalisatie van de signing-service.** Smalle
  RPC-interface met whitelist-enforced endpoints, beperkte
  dependency-graph (smaller dan main-app — doel: enkele tientallen
  deps), geen userland-HTTP-server beyond het RPC-endpoint, geen
  directe SSH-access voor operators (deploys via pipeline + mTLS-
  gauthenticeerde restart-endpoint).
- **Hardened deployment.** Eigen VPS of container met strict
  sysctl-hardening, auditd-style syscall-logging gekopieerd naar
  een externe append-only store, immutable-filesystem base-image
  waar praktisch. Operationele details in de apart
  aan-te-maken operational runbook (zie Part 7).
- **Defense-in-depth buiten Reverto.** Exchange-side controls en
  onafhankelijke watchdog zijn de laatste-lijn verdediging tegen
  dit scenario. Deze spec eist daarom dat exchange-onboarding alle
  hardening afdwingt (Part 3.5) en dat de watchdog écht
  onafhankelijk is (Laag 7 requirements).
- **Monitoring van signing-service zelf.** Een apart watchdog-type
  monitoring-proces dat ongebruikelijke sign-activity detecteert
  (plotselinge piek in trade-signatures, signatures zonder
  voorafgaand approval-token, baseline-config-wijzigingen zonder
  cooldown). Overlapt deels met Laag 7 maar richt zich op
  signing-service-interne signalen i.p.v. externe balance-drift.

**Bewuste limitation van single-signing-service architectuur.** De
signing-service is en blijft een single point of failure in dit
architectuur-model. Horizontale scaling (meerdere signing-services
met quorum- of threshold-signing) is R&D-spoor (zie Part 6.3b MPC
threshold-signing) dat deze limitation kan reduceren maar niet
elimineren — bij een compromise van ≥ threshold-many signing-
services valt de defense alsnog. Acceptatie: de spec kiest voor
één gehardende signing-service met externe watchdog-controle; MPC
is toekomst-R&D zonder nu de complexiteit te adopteren.

---

## Part 3 · Architectural Target State

### 3.1 Service Separation (Optie C, Niveau 2)

Het doel is één trust-boundary tussen de hoofd-applicatie en de
credential-opslag/signing-operatie. De hoofd-applicatie weet niet hoe
je een signed HMAC request naar Bitget bouwt — die kennis leeft in de
signing-service.

```
┌────────────────────────────────────────────────────────────────┐
│ Internet                                                       │
└────────────────────────────────────────────────────────────────┘
                         │  HTTPS / TLS
                         ▼
┌────────────────────────────────────────────────────────────────┐
│ Reverse proxy (nginx / caddy)                                  │
│ ├── TLS termination                                            │
│ ├── Rate-limiting (L7)                                         │
│ └── Trusted-proxy X-Forwarded-For parsing                      │
└────────────────────────────────────────────────────────────────┘
                         │  HTTP + real client IP
                         ▼
┌────────────────────────────────────────────────────────────────┐
│ reverto-web (main app)                                         │
│ ├── FastAPI + AuthMiddleware (bcrypt + TOTP + session cookie)  │
│ ├── User management (users, sessions, caps-settings)           │
│ ├── Strategy engine orchestration (paper + live bots spawnen)  │
│ ├── Portal UI + PWA bootstrap                                  │
│ ├── Deal / order ledger                                        │
│ └── Dependencies: strategy + indicator + DB client             │
│                                                                │
│ Reads: users, deals, orders, annotations, caps_config,          │
│        approvals_pending                                       │
│ Writes: audit_log entry bij elke auth + cap-change             │
│                                                                │
│ BEZIT NOOIT: exchange api_secret, TOTP-seed decryption key,    │
│              signing-service audit-log, watchdog-state.        │
└────────────────────────────────────────────────────────────────┘
                │                                │
                │ mTLS + request-signing         │ mTLS read-only
                │ RPC (JSON over HTTPS)          │ monitor-protocol
                ▼                                ▼
┌─────────────────────────────────┐  ┌─────────────────────────────┐
│ reverto-signer (signing-service)│  │ reverto-watchdog            │
│ ├── Exchange credential store   │  │ ├── Read-only exchange API  │
│ │   (Fernet-encrypted per user) │  │ │   pulls (balances)        │
│ ├── Scope whitelist per call    │  │ ├── Expected-balance model  │
│ ├── Cap-counters (rolling)      │  │ ├── Discrepancy detection   │
│ ├── Order-signing + idempotency │  │ ├── Alert pipeline          │
│ ├── Approval verification       │  │ └── Kill → signing-service  │
│ ├── Independent audit log       │  │                             │
│ └── Separate DB                 │  │ Separate deployment         │
│                                 │  │ (ander VPS, ander OS-image) │
│ Exposes RPC:                    │  └─────────────────────────────┘
│   place_trade(user_id, intent)  │
│   update_caps(user_id, ...)     │
│   register_exchange_key(...)    │
│   rotate_key(...)               │
│                                 │
│ Refuses silently on:            │
│   - scope outside whitelist     │
│   - cap exceeded                │
│   - missing approval-token      │
│   - watchdog kill-signal active │
└─────────────────────────────────┘
                │
                │ HMAC-signed requests
                ▼
        ┌───────────────────┐
        │ Exchange API      │
        │ (Bitget / Kraken) │
        └───────────────────┘
```

**Reverto-web (main app).** Bevat het huidige `web/app.py`, alle route
modules, de `PaperEngine`/`LiveEngine` orchestration, en de deal/order
ledger. Heeft toegang tot de `users` + `deals` + `orders` +
`annotations` tabellen. Heeft GEEN toegang tot exchange-credentials,
TOTP-decryption-keys, of de watchdog-kill-switch.

**Reverto-signer (signing-service).** Nieuw component. Eigen DB (Postgres, separate
schema of database). Bevat:

- `exchange_credentials(user_id, exchange, encrypted_blob)` — de
  api_key/api_secret paren.
- `fernet_keys(user_id, key_material)` — per-user decryption keys.
  Key-material kan in een envelope-encryption setup met een
  master-key uit het OS-keystore/KMS staan, niet in DB-plaintext.
- `caps_rolling_counters(user_id, exchange, window, amount_consumed,
  window_start)` — de daily/weekly/monthly running totals.
- `approval_tokens(token_id, user_id, action, expires_at, consumed)`
  — kortstondige approval-tokens die de main-app heeft opgeslagen bij
  een approval-request en die bij de signing-call worden meegestuurd.
- `signer_audit_log(...)` — elke operation, met timestamp,
  caller (main-app via mTLS client-cert), user_id, scope, result.

**Reverto-watchdog.** Derde component, bij voorkeur op compleet
aparte infrastructuur (tweede VPS, andere provider). Gebruikt
read-only API-keys (separate van de trade-keys, onboarding verifies
dat ook deze alleen read-permission hebben), poll exchange-balances
elke paar minuten, vergelijkt met het expected-model uit de main-app,
stuurt op discrepancy een kill-signal naar de signing-service.

**Interne communicatie.** mTLS tussen alle drie. Client-certs per
service, CA gerund door operator. Een gecompromitteerde main-app kan
niet de watchdog impersonaten (ander cert). Request-signing bovenop
mTLS voor audit-trail-robuustheid (incoming requests bevatten een
HMAC die in de signing-service audit-log komt).

**Wat niet verandert.** De exchange-HTTPS-naar-Bitget-of-Kraken
verandert niet. ccxt blijft de lib. De `BaseExchange` interface
(`exchanges/base_exchange.py`) verhuist naar de signing-service maar behoudt
zijn vorm.

### 3.2 Database Separation

Drie stores:

**Main DB (Postgres post-SQLite-migratie).** `users` (id, username,
password_hash, role, session_epoch, active, created_at), `deals`,
`orders`, `chart_annotations`, `backtest_runs`, `caps_config`
(user_id, per_trade_threshold, daily_cap_pct, etc.),
`approval_requests` (pending approvals, token_hash, expires_at),
`audit_log_main` (auth events, config changes).

TOTP-seed leeft hier **encrypted** — ciphertext is in de DB, maar de
decryption key is niet. Zie "TOTP-seed key management" hieronder voor
de envelope-encryption details.

**Signing-service DB.** `exchange_credentials` (encrypted),
`caps_rolling_counters`, `approval_tokens_consumed`,
`signer_audit_log`. Separate schema in een separate Postgres DB, of
separate Postgres cluster — de trust-boundary is op DB-niveau, niet
alleen op applicatie-niveau. Bevat ook de Master Key (MK) voor
TOTP-seed envelope-encryption (zie hieronder).

**Watchdog store.** Kleine DB (SQLite acceptabel — single-writer
service). `expected_balance_snapshots` (periodiek), `discrepancy_events`,
`kill_signals_sent`. Write-only vanuit de watchdog-process. Wordt
nooit door main-app of signing-service uitgelezen.

**TOTP-seed key management (envelope-encryption).**

Seeds worden per-user encrypted met een envelope-encryption patroon,
niet met een globale key:

- Elke user krijgt bij eerste TOTP-enrollment een unieke
  **Data Encryption Key** (DEK). De TOTP-seed wordt met DEK
  encrypted en als ciphertext in `users.totp_seed_encrypted`
  opgeslagen.
- De DEK zelf wordt encrypted met een **Master Key** (MK) en als
  `users.totp_dek_wrapped` opgeslagen. Main-DB heeft dus alleen
  wrapped DEK's, nooit plaintext DEK's.
- De MK leeft in de signing-service, niet in de main-DB. Login-flow:
  main-app stuurt de wrapped DEK + versleutelde seed naar de
  signing-service, die MK toepast om DEK te unwrappen, DEK om seed
  te unwrappen, en de TOTP-code tegen de seed verifieert. De
  main-app zelf ziet geen plaintext seed.

Rationale: compromise van alleen de main-DB geeft wrapped DEK's die
waardeloos zijn zonder MK. Compromise van alleen de signing-service
geeft MK maar niet de user-specifieke data — de aanvaller moet ook
main-DB-access hebben om de DEK's te halen waar MK op werkt. Beide
compromises apart falen; beide tegelijk is een catastrofaal
scenario waar defense-in-depth sowieso niet tegen beschermt.

Master Key rotation:

- Tijdens single-tenant Reverto-Server draait het systeem op de genesis-MK;
  rotation-flow is pas productie-relevant vanaf Phase C.
- Rotation-proces: nieuwe MK wordt gegenereerd, alle DEK's worden
  one-by-one gedecrypt met de oude MK en ge-re-encrypt met de
  nieuwe MK. Commit-order: oude MK blijft geldig tot alle DEK's
  zijn gemigreerd; dan wordt de nieuwe MK actief gezet en de
  oude MK bewaard in een tijdelijke `mk_previous` slot voor
  rollback.
- Bewaartermijn oude MK: tot rotation complete is (alle DEK's
  succesvol ge-migreerd en verified) plus een vooraf ingesteld
  venster (bijv. 7 dagen) waarin een rollback kan worden gedaan
  als rotation-artefacten worden gedetecteerd. Daarna wordt de
  oude MK vernietigd.
- Rotation-state is een DB-rij (`mk_rotation_state`) zodat een
  crash mid-rotation recoverable is: bij herstart weet het systeem
  welke DEK's al zijn gemigreerd en welke niet.

**Backup-strategie.**

- **Main DB:** nightly dumps, 30 dagen retentie, encrypted at rest
  in object-storage.
- **Signing-service DB:** nightly dumps, 90 dagen retentie (langere
  voor forensic), aparte encryption-key dan main-DB dumps, apart
  object-storage account. Waarom apart: dumps-access zou een
  insider-compromise kunnen compenseren — een insider met
  main-backup-access heeft geen signing-service-backup-access.
- **Watchdog store:** 30 dagen retentie is genoeg; historische
  discrepancy-events worden naar de operator (Slack/email) gestuurd
  zodra ze optreden, niet uit backup gelezen.

**Backup-strategie credentials-store.**

- **Encrypted DEK's in main-DB:** meegenomen in de standaard main-DB
  backup-flow — ze zijn waardeloos zonder MK.
- **Master Key in signing-service:** aparte backup naar offline
  cold storage, NIET in dezelfde backup-stream als de main-DB
  dumps. Zou een backup-stream worden gecompromitteerd, dan moet de
  aanvaller ook toegang tot een fysiek gescheiden store vinden.
- **Rotation-frequency MK-backup:** bij elke significante MK-
  rotation (zie hierboven). Oude MK-backups blijven bewaard tot de
  volgende rotation-cyclus afgerond is.
- **Restore-test procedure:** minimaal jaarlijks. Gedocumenteerd in
  de operational runbook, niet in deze spec.

### 3.3 Authentication Stack

Drie lagen:

**Login-gate.** Username + bcrypt-verified password + verplichte
TOTP-code. Nieuwe wiring bovenop huidige Phase-3a `verify_password`
flow:

- Na succesvolle `verify_password`: nog geen cookie gemint.
- User's TOTP-seed wordt decrypt door de signing-service (main-app
  stuurt de encrypted seed + login-session-intent, signing-service
  decrypt en vergelijkt
  de aangeleverde TOTP-code tegen `pyotp.TOTP(seed).now()` binnen
  een 30s window).
- Alleen na beide stappen wordt het session-cookie gemint.
- Rate-limiter op login gaat al per-IP; moet aangevuld met
  per-username limiter (anders kan één IP alle accounts proberen
  via password-spray).

**Session-cookie.** Ongewijzigd qua format (itsdangerous signed
payload, uid + u + ep). TTL blijft 24h absolute (geen sliding).
Cookie geeft toegang tot **read-only** operations en
kleine-threshold trades; boven de threshold komt approval erbij.
Concrete per-tier cap-waardes: zie 6.2 cap-tabel.

**Approval-channel.** Voor mutating operations boven threshold. Twee
kanalen per user, configureerbaar bij onboarding:

- **PWA WebAuthn (primair).** User installeert Reverto-PWA op
  telefoon/laptop. Bij onboarding wordt een WebAuthn-key ingeschreven
  die device-bound is (secure-enclave waar beschikbaar — iOS
  Passkeys, Android StrongBox, Windows Hello, macOS Touch ID). Voor
  een approval-prompt stuurt de server een challenge; het device
  signed met de private key; signing-service verifieert de
  signature.
- **TOTP (fallback).** Users zonder PWA kunnen approvals doen met
  TOTP-code. Zelfde seed als login-TOTP maar een apart "approval
  context" ruimte — een TOTP-code gebruikt voor login mag niet
  hergebruikt worden voor een pending approval binnen hetzelfde
  window. TOTP-only users krijgen lagere default-caps dan PWA
  WebAuthn-users (zie 6.2 cap-tabel voor de definitieve waardes
  per auth-tier).
- **YubiKey (premium tier).** Hardware-key voor users die
  config-wijzigingen met extra assurance willen doen. WebAuthn-based
  FIDO2 — protocol-compatible met PWA-key, andere device-class.

Users kiezen bij onboarding tussen: PWA-only, TOTP-only, PWA+YubiKey.
Downgrade van PWA naar TOTP-only vereist een 48h cooldown (net als
register-new-device, zie Part 3.4) om "attacker verliest de
device-bound key, downgradet naar het zwakkere kanaal" te
voorkomen.

**Rate-limiting architectuur.**

Rate-limiting in Reverto dient meerdere security-doelen tegelijk:

- Voorkomt credential-stuffing attacks op het login-endpoint.
- Voorkomt brute-force op TOTP-verificatie (30s window × N pogingen
  = lottery, niet gegarandeerd onmogelijk).
- Voorkomt API-quota-exhaustion naar exchanges. Bitget en Kraken
  leggen per-API-key rate-limits op die significant lager zijn dan
  de som van N actieve bots; per-user isolation voorkomt dat één
  agressieve user andere users' trading blokkeert.
- Mitigeert DDoS-by-legitimate-users bij plotselinge load (bv.
  market-crash triggert alle users tegelijk).

Reverto past drie onafhankelijke rate-limit-dimensies toe:

- **Per-IP rate-limiting** — huidige implementatie via SlowAPI
  (`web/app.py:1165`). Blijft ongewijzigd voor unauthenticated
  endpoints (login, register, password-reset). Handelt DDoS-
  achtige scenarios af op pre-auth niveau.
- **Per-user rate-limiting** — nieuwe laag bovenop per-IP,
  geactiveerd na authentication. Voorkomt dat een aanvaller met
  gestolen credentials via veel verschillende IPs brute-force of
  spray kan uitvoeren op user-specific operations (cap-wijzigingen,
  device-registration, config-endpoints). Keyed op `user_id` uit
  het session-cookie of PWA-request.
- **Per-exchange rate-limiting** — beschermt tegen exchange
  rate-limit bans door aggressive bot-activity. De signing-service
  handhaaft een maximum-API-calls-per-second per exchange per user,
  en een platform-brede ceiling om exchange-kant "Reverto draait
  te veel requests" bans te voorkomen. Ongeacht hoeveel users
  trades tegelijk triggeren, worden calls ge-queued zodat exchange-
  limits niet overschreden.

Concrete waarden worden gecalibreerd in Phase B (authentication
hardening). Drempel-configuratie zelf valt onder de normale
security-gate: wijzigingen op per-user-limieten vereisen user-
approval via PWA; platform-level ceilings worden beheerd via
admin-role-only config met audit-trail. Audit v26 v26-01 (per-
user rate-limit) zit in deze laag.

### 3.4 Approval Channel Hierarchy

Welke actie vereist welk minimum-kanaal. "Session" = het normale
session-cookie. "TOTP" = een TOTP-code door de user ingegeven.
"PWA WebAuthn" = tap-to-sign op het user-device. "YubiKey" =
hardware-FIDO2 key. Cooldown = tijd tussen aanvraag en effectuering
gedurende welke de user kan annuleren en na afloop een tweede
approval-step nodig heeft.

| Actie | Session | TOTP | PWA WebAuthn | YubiKey | Cooldown |
|-------|:-------:|:----:|:------------:|:-------:|:--------:|
| Read dashboard | ✓ | | | | — |
| Start/stop bot (paper) | ✓ | | | | — |
| Start/stop bot (live) | | ✓ | recommended | | — |
| Place trade ≤ per-trade threshold | ✓ | | | | — |
| Place trade > per-trade threshold | | ✓ | recommended | | — |
| Manual trade (portal-initiated) | | | ✓ | | — |
| Change per-trade threshold | | ✓ | | | 24h |
| Change daily/weekly/monthly cap | | | ✓ | | 48h |
| Add new exchange API-key | | ✓ | | | 24h |
| Rotate existing exchange-key | | ✓ | recommended | | 12h |
| Delete exchange-key | | ✓ | | | — |
| Register new approval device | | ✓ | ✓ (existing) | | 48h |
| Remove approval device | | ✓ | ✓ (existing) | | 24h |
| Downgrade approval channel (PWA → TOTP-only) | | | ✓ | | 48h |
| Change password | | ✓ | | | — |
| Change dead-man-switch timeout | | ✓ | | | 24h |
| Disable dead-man-switch entirely | | | ✓ | ✓ premium | 72h |
| Emergency stop (user's own bots) | ✓ | | | | — |
| Emergency stop (admin cross-user) | | ✓ | ✓ recommended | | — |
| Cross-user admin action | | | ✓ | recommended premium | — |
| Transfer ownership of account | | | ✓ | ✓ | 168h (7d) |

**Threshold-waardes.** De numerieke caps achter "per-trade
threshold" en "daily/weekly/monthly cap" in deze tabel zijn per
auth-tier vastgelegd in 6.2 cap-tabel. De channel-keuze (welke
kolom een actie minimum vereist) leeft hier; de cap-getallen
(welk volume bij welke channel hoort) leven daar — zo blijft de
kanaal-hiërarchie en de threshold-set onafhankelijk
herzienbaar.

**Cooldown semantiek.** De cooldown is geen throttle op aanroep-
frequentie maar een delay tussen aanvraag en effectuering. Een
cap-wijziging wordt ingediend door de user, token wordt gemint met
`effectuate_at = now + 48h`, en de user krijgt een mail/PWA-alert
"Over 48h gaat je daily cap naar 15% tenzij je klikt op Cancel." De
user kan tot effectuation-tijd annuleren met een enkele session-
bevestiging. Dit is waarom cooldown bij daily-cap 48h is: geeft de
legitimate user tijd om te merken dat iemand anders een wijziging
indient.

**Pending-approval count caps.** Een user kan niet tientallen
pending approvals stapelen — max 5 in vlucht per type. Boven dat
weigert de portal nieuwe approvals tot er eentje geannuleerd of
effectgeworden is. Voorkomt dat een attacker tijdens de cooldown
zoveel aanvragen stapelt dat de user de cancel-signalen mist.

### 3.5 Exchange-Side Hardening (Onboarding Requirements)

Wat de user MOET doen op de exchange voordat het Reverto-account
kan worden geactiveerd. De onboarding-flow verifieert dit actief —
een api_key die deze eisen niet haalt wordt geweigerd en de user
krijgt instructies per exchange.

**Algemene requirements (alle exchanges):**

- **Trade-only permissions.** De API-key mag GEEN `withdraw`
  permission hebben. Onboarding-flow probeert een read-call
  (`fetch_balance`) + een simulate-withdraw (of vraagt de exchange
  om de key's permission-set als de API dat ondersteunt) en weigert
  als `withdraw` aanstaat. Bitget: `tradeScope` field in de API-key
  response toont permissions. Kraken: `key-description` / futures-
  key permissions. Exact mapping per exchange hieronder.
- **IP-whitelist actief.** De key moet op de exchange gekoppeld zijn
  aan een IP-allowlist die de signing-service IP bevat. Onboarding
  stuurt een dummy-order vanuit de signing-service IP (en een
  simultaan
  vanuit een ander IP waar dat kan); als de tweede slaagt staat
  er geen whitelist en wordt de account geweigerd.
- **Sub-account (recommended, niet verplicht).** User maakt een
  sub-account aan met alleen de balance die voor Reverto bestemd
  is. Hoofd-account balance blijft buiten Reverto's blast-radius.
  Kraken support dit goed; Bitget's sub-account feature is retail-
  beschikbaar maar minder goed gedocumenteerd — onboarding flow
  informeert, forceert niet.

**Per-exchange checklist:**

Bitget:
- Login op bitget.com → User Center → API Management.
- Create API Key → naam "reverto-trade-only".
- Permissions: vink **alleen** "Contract Trading" en "Read" aan.
  **NIET** vinken: "Spot Trading" (tenzij user expliciet spot-
  strategie heeft), "Withdraw" (nooit), "Transfer".
- IP Bind: paste de signing-service IP (onboarding toont deze). Multiple
  IPs kan met komma's; voor de watchdog een aparte key met alleen
  read.
- Passphrase: door user gekozen tijdens key-creation. Reverto vraagt
  deze apart, encrypt en slaat op in de signing-service (samen met
  api_key +
  api_secret).
- Copy api_key + api_secret + passphrase direct — Bitget toont
  secret maar één keer.

Kraken:
- Login op kraken.com → Settings → API.
- Generate New Key → description "reverto-trade".
- Permissions: check **alleen** "Query Funds", "Query Open Orders",
  "Create & Modify Orders", "Cancel Orders", "Query Ledger Entries".
  **NIET** check: "Withdraw Funds" (nooit), "Deposit Funds",
  "Staking", "WebSocket Open Orders & Trades" (optioneel).
- API-key IP restriction: set to the signing-service IP.
- Futures API vereist een afzonderlijke key-set (Kraken-futures is
  een separate product met eigen API) — onboarding handelt dit af
  als twee keys per user als Kraken-futures gekozen is.
- Nonce window: default 1000ms is ok. Reverto gebruikt monotonic
  nonces per ccxt-default.

**Ongoing requirements.** Quarterly recheck: signing-service probeert bij
de user's eerste trade van het kwartaal een no-op withdraw; als die
slaagt is er ergens permission-drift en wordt de user gealerteerd
+ trading gepauzeerd tot re-verification. Vangt de case waar user
"per ongeluk" op de exchange een permission-upgrade heeft gedaan
omdat een andere tool die vroeg.

### 3.6 Defense-in-Depth Layers

Acht lagen. Elke laag is apart toegewijd aan bepaalde attack-
scenarios; geen laag beschermt tegen alles. De Independence-tabel
in Part 3.7 benoemt welke lagen overleven welke compromise.

**Laag 1 — Per-trade threshold met approval-escalation.**

Elke trade > threshold vereist approval-channel (zie Part 3.4).
Threshold is percentage van rolling-max balance over de laatste
30 dagen: default 2%, user-configureerbaar 1-5% (wijziging vereist
approval zelf).

Beschermt tegen: grote unauthorized trades bij session-compromise
(Scenario 2.2, 2.3).
Beschermt niet tegen: salami-attack binnen threshold (Laag 2
vangt dit); exchange-side lek (2.6, Laag 7 vangt dit).

Waar leeft dit: signing-service, niet main-app. Main-app genereert
intent; signing-service checkt threshold en weigert zonder
approval-token.

**Laag 2 — Daily volume cap (percentage-based).**

Cumulatief daily trading-volume binnen 10% van rolling-max balance
laatste 30 dagen (default). User-configureerbaar 5-15%. Per
exchange, niet cross-exchange samengeteld — compromise van één
exchange-key stopt op die exchange's cap, niet op de som.

Rolling-max window: 30 dagen zodat een dip niet de cap-limit
direct verlaagt (user die 50% drawdown doet heeft nog steeds de
originele cap voor een tijdje). Update-frequency: daily snapshot
om 00:00 UTC.

Beschermt tegen: salami-attack (Scenario 2.2, 2.3, 2.6); volume-
wash bij exchange-lek.
Beschermt niet tegen: een-shot grote trade (Laag 1); attack die
balance transfereren naar attacker's exchange-account (impossible
want trade-only keys); trade-frequency anomalies (Laag 6).

Waar leeft dit: signing-service, rolling counter per
`(user_id, exchange, day)`. Reset om middernacht UTC. Dit maakt
Laag 2 afhankelijk van signing-service integrity — zie Part 2
Scenario 2.8 voor behandeling van signing-service compromise als
threat-scenario.

*Ramp-up period voor nieuwe users.*

Rolling-max heeft pas statistical betekenis na een periode van
activiteit. Scenario: user registreert op dag 1, stort €10 000, doet
zijn eerste trade van €3 000. Met een dag-1 rolling-max van €10 000
is de daily cap €1 000 (10%) — de trade wordt geweigerd, de user is
in de war. Naast dat het UX-probleem de onboarding-conversie raakt,
is het ook een security-probleem omdat het de user naar workarounds
drijft (caps verhogen zonder goede reden).

De eerste 14 dagen na account-activation draait daarom met
gereduceerde caps:

- **Daily:** 5% van current balance (lager percentage dan mature
  account) met absolute ceiling €500. Ceiling beschermt nieuwe
  accounts met klein saldo; percentage beschermt grote instappers.
- **Weekly:** 10% van current balance met absolute ceiling €2 000.
- **Monthly:** nog niet actief — er is geen 30-dagen-window om
  tegen te meten in de ramp-up-periode.

Na 14 dagen gaat de user over naar de normale percentage-based caps
gebaseerd op de rolling-max-tot-dan-toe. De overgang is **gradueel**,
niet abrupt: daily-cap-factor verhoogt stapsgewijs 5% → 7% → 10%
over 3 dagen transition, idem voor weekly 10% → 15% → 20%. Monthly
cap wordt op dag 14 actief met zijn normale 35% drempel.

User-experience: de onboarding-UI maakt expliciet dat caps lager zijn
tijdens ramp-up, met countdown tot full-caps activation en tot het
moment dat monthly-cap actief wordt. Voorkomt de "waarom wordt mijn
trade geweigerd" confusion.

Ramp-up geldt ook na een significant deposit-event: als een user na
30 dagen plotseling 10× zijn balance bijstort, wordt de rolling-max
over-weighted door de nieuwe balance en kunnen caps onverwacht
springen. In Phase E wordt overwogen om deposits-detection aan de
signing-service toe te voegen met een soft-ramp-up bij > N×-balance-
events, maar dat valt buiten de v1 spec-scope.

*Legitiem-grote initial trades tijdens of direct na ramp-up.*

Een user die een legitieme grote trade wil doen boven de ramp-up
cap of boven de normale daily cap (bijvoorbeeld een bewuste grote
DCA-entry na een research-periode) heeft een tijdelijke cap-
verhoging nodig. De flow is:

- User dient via PWA-signed config-change een cap-verhoging in voor
  één-dag-window — dezelfde approval-channel en cooldown als
  reguliere cap-wijzigingen (Part 3.4).
- Cooldown-waarde zoals standaard voor daily-cap-wijziging (24-48
  uur voordat de verhoging actief wordt). Deze wachtperiode is
  bewust: legitieme strategy-setup plant zich niet op de minuut,
  maar een attacker die net credentials heeft buitgemaakt wil juist
  binnen minuten handelen.
- Na window-expiry terugval naar de voorheen geldende caps; de
  verhoging persisteert niet.

Dit voorkomt dat ramp-up of post-ramp-up caps legitieme strategy-
start blokkeren, zonder de security-eigenschappen te ondermijnen
(wijziging blijft auth'd + cooldown'd + tijdelijk + gelogd in de
signing-service audit-trail).

**Laag 3 — Weekly en monthly caps.**

Dezelfde structuur als Laag 2, maar:
- Weekly: 20% van rolling-max laatste 30 dagen, per exchange. Reset
  op Monday 00:00 UTC.
- Monthly: 35% van rolling-max laatste 90 dagen, per exchange.
  Reset eerste-van-de-maand 00:00 UTC.

Bestaat omdat een daily cap van 10% theoretisch naar 70% per week
kan accumuleren — weekly + monthly zijn de dampers die zeggen "ook
als je elke dag de daily hit, mag je niet bliksemsnel al je balance
wegtraden."

Beschermt tegen: slow-burn attacks die respecteren daily-cap
(attacker verdeelt stelen over meerdere dagen).
Beschermt niet tegen: wat Laag 2 niet vangt sneller in de tijd.

Waar leeft dit: signing-service, idem als Laag 2. Laag 3 valt dus
samen met Laag 2 onder hetzelfde signing-service integrity-model;
zie Part 2 Scenario 2.8 voor de compromise-analyse.

**Laag 4 — Performance-gemoduleerde cap-scaling.**

De signing-service past de daily/weekly/monthly caps uit Laag 2+3
omlaag aan wanneer realized P&L significant onder de bot's expected
baseline blijft. Doel: beperk de attack-surface op bots die systematisch
geld verliezen, of die nu door een adversarial order-stream (pump-
and-dump van attacker-gekozen coin) of een genuine broken strategy
komt.

**A. Expected strategy-behavior (baseline-opbouw).**

"Expected" is een rolling baseline van realized P&L, per bot, over
een trailing 30d-window. Niet een statische backtest-projectie —
die is te ver van live-realiteit. De signing-service berekent:

- Per-bot rolling realized P&L, 30d window, herberekend per 24h.
- Baseline wordt pas actief na 14 dagen observation-only data. In die
  periode verzamelt het systeem cijfers, fire-t geen triggers, en
  toont in de portal "Laag 4 learning" zodat de user weet dat deze
  defense nog niet live is. Vergelijkbaar met de learning-mode uit
  Laag 6 anomaly-detection.
- Berekening is **per-bot**, niet per-user. Een user met één
  conservatieve DCA-bot en één agressieve indicator-bot heeft twee
  onafhankelijke baselines; een verlies op bot A knijpt niet de caps
  van bot B.

**B. Trigger-drempels (tier-systeem).**

Drempels zijn start-waarden, te calibreren tijdens Phase A op basis
van real-world strategy-data. Een later besluit kan ze aanpassen;
tot die tijd staat de onderstaande mapping. Tiers zijn cumulatief —
een bot kan niet tegelijk in tier 1 en tier 2 zitten; tier 2 houdt
tier 1's toestand in.

| Tier | Trigger (realized P&L over trailing 7d) | Actie |
|:-:|---|---|
| 1 | < −2% | Alert naar user. Geen cap-wijziging. |
| 2 | < −5% | Caps gereduceerd naar 70% van normaal. Alert met aanbeveling bot te reviewen. |
| 3 | < −10%, OF tier 2 voor 3 opeenvolgende dagen | Caps gereduceerd naar 40%. Bot enters *enhanced scrutiny mode*: elke volgende trade vereist push-approval (WebAuthn of TOTP), ongeacht de threshold uit Laag 1. |

De −2% / −5% / −10% waarden zijn voorstellen op basis van typische
DCA- en indicator-strategies; echte calibratie volgt uit Phase-A
data. Een conservatieve strategy waar −2% in 7d een normale dip is,
verdient een per-strategy override — die mogelijkheid komt in het
portal-UI als deel van Phase-E implementatie.

*Tier 1 is een early-warning signal, geen zelfstandige defense.* Een
aanvaller die binnen tier-1 range blijft (bijvoorbeeld 2-4% verlies
per week) zou cumulatief significante schade kunnen doen zonder dat
Laag 4 escaleert. Absolute limitering bij tier-1-range verliezen
wordt geleverd door Laag 2 (daily cap), Laag 3 (weekly/monthly
caps) en Laag 5 (emergency floor). Tier 1 signaleert alleen aan de
user dat iets aandacht verdient; het is niet de plek waar het
bloeden stopt.

**C. Recovery-paden.**

Tier 2:
- Reductie blijft actief tot 3 opeenvolgende dagen zonder nieuwe
  tier-2 of tier-3 trigger.
- Daarna geleidelijke terugkeer naar normaal: 40% → 70% → 100%
  caps, met 24h tussen elke step. Gaat van tier 3 over tier 2
  terug als de bot daar tussendoor is beland.
- Recovery is automatisch, geen user-action vereist.

Tier 3:
- Geen automatische recovery. User moet actief uit *enhanced
  scrutiny mode* stappen via een PWA-approval waarin expliciet
  wordt bevestigd dat bot-performance is geëvalueerd en eventueel
  aangepast.
- Tot die approval blijft caps-reductie en trade-approval-
  requirement van kracht.
- De user-confirmatie is geen gewone cap-wijziging maar een
  expliciete erkenning: de portal toont de realized P&L, de
  verliezende trades, en vraagt "Doorgaan met huidige config?" De
  click is het audit-log-anker.

**D. Cool-down tussen scaling-events.**

- Minimum 6 uur tussen opeenvolgende cap-wijzigingen. Voorkomt
  flapping in volatile periodes waar een bot binnen een enkele dag
  meerdere keren een tier-grens kruist.
- Cool-down geldt alleen voor verlagingen + verhogingen in dezelfde
  richting. Escalatie naar een **hogere** tier (tier 2 → tier 3) mag
  direct, zonder cool-down. Rationale: defense-in-depth is gemaakt
  om snel omlaag te schakelen als het fout gaat, niet om tijd te
  kopen voor een broken strategy.

**E. Anti-gaming bescherming.**

Expliciet: **er is GEEN cap-relaxation bij outperformance, hoe hoog
de realized P&L ook is**. De asymmetrie tussen "onder-presteren
verlaagt caps" en "over-presteren verandert niks" is bewust.

Rationale: een aanvaller die toegang heeft tot een user-account (zie
scenario 2.2 / 2.3) zou anders een incentive hebben om eerst een
periode winstgevend te handelen om caps omhoog te krijgen voordat hij
de balance wegtraded. Door outperformance-neutraal te zijn, blijft
het attacker-scenario onveranderd: elke aanval loopt door dezelfde
caps als een passieve account, en elke underperformance door een
attacker verlaagt de caps juist.

**Beschermt tegen.** Attacks met systematisch verlies (pump-and-dump
exits, adversarial order-placement); ook tegen genuine broken
strategies die geld verliezen zonder attack.

**Beschermt niet tegen.** Attacks die binnen expected-range blijven
(attacker die toevallig winst draait); attacks op nieuwe bots binnen
het 14d observation-window (nog geen baseline).

**Waar leeft dit.** Performance-tracker en tier-evaluatie-logica
leven in de signing-service, niet in de main-app. De main-app kan
P&L-metrics lezen (read-only) voor UI-rendering, maar kan cap-
drempels of tier-grenzen niet wijzigen zonder user-approval via een
PWA-signed config-change request. Cap-counters leven óók in de
signing-service; main-app pusht alleen `{user_id, bot_id,
p_and_l_data}` als input voor de signing-service's eigen tier-
evaluatie. Een gecompromitteerde main-app kan kleinere P&L-waardes
injecteren (waardoor caps strenger worden — dat is acceptabel) maar
kan niet een hogere tier forceren of tier-drempels versoepelen
zonder ook in de signing-service te komen. Dit maakt Laag 4
afhankelijk van signing-service integrity — zie Part 2 Scenario
2.8 voor de behandeling van signing-service compromise als
threat-scenario.

**Laag 5 — Emergency floor.**

Trading stopt volledig als balance onder 60% van rolling-max over
90 dagen komt. User krijgt alert, bots staan op pause, re-
activation vereist user-action + 72h cooldown + 7d enhanced-
scrutiny mode (alle trades vereisen approval, caps 50% van
normaal).

Beschermt tegen: runaway-loss scenario — als een attack / broken
strategy voorbij Laag 2/3/4 is gekomen en toch significante
drawdown veroorzaakt, stopt Laag 5 het bloeden voordat de balance
volledig weg is.
Beschermt niet tegen: attacks die balance > 60% laten (zal veel
andere lagen eerder triggeren); exchange-side lek (Laag 7).

Waar leeft dit: signing-service, met rolling-max-monitor per user.
Dit maakt Laag 5 afhankelijk van signing-service integrity — zie
Part 2 Scenario 2.8. De watchdog (Laag 7) is de externe redundantie
op deze floor: bij signing-service compromise waar de floor stil
wordt gezet, detecteert de watchdog de balance-drop onafhankelijk.

**Laag 6 — Anomaly detection.**

Drie detectoren. Elk met eigen default-actie, maar alle drie staan
tijdens de eerste periode in observation-only mode om false-positives
te voorkomen op een vers-geactiveerde bot waarvoor nog geen
persoonlijke baseline bekend is.

*Detectoren:*

- **Trade-frequency baseline.** Per-bot gemiddelde trades/week
  laatste 60d. Significant afwijkend (>3σ, bijv. 10× meer trades in
  één week dan historisch) → log-only in de eerste instantie;
  escalatie via de graded-response tabel hieronder.
- **Trade-timing anomalies.** Trades buiten user's normale active
  hours (historisch afgeleid uit portal-login-timestamps,
  manual-trade-timestamps en PWA-activity, timezone-adapted per
  user). Niet-strategic buiten-uur trades → approval-channel
  vereist.
- **Asymmetric patterns.** Alleen-buy of alleen-sell bursts > 5
  opeenvolgende orders zonder tegenkant → reject de zesde order tot
  user explicit approval geeft. Vangt het "pump-jezelf-in" pattern.

*Learning baseline (pre-activation).*

De eerste 14-30 dagen per bot draait anomaly-detection in
observation-only mode:

- Systeem verzamelt baseline-metrics: trade-frequency per uur,
  typische trade-sizes, normal trading-hours per user
  (timezone-adapted), asymmetric-pattern norms per strategy.
- Geen triggers firen tijdens deze periode. Events worden wel
  gelogd zodat forensic-trail intact blijft.
- Na observation: user krijgt in de portal een summary van de
  detected baseline en mag per-metric handmatig grenzen bijstellen
  waar de auto-afleiding er naast zit. User bevestigt expliciet
  dat detection live mag — pas dan schakelt het systeem over naar
  live-mode.
- Een vers-aangemaakte bot draait altijd opnieuw door deze
  observation-periode, ook al heeft de user al andere bots met
  baselines. Verschillende strategies hebben verschillende normen.

*Graded responses (bij triggered anomaly).*

Triggers hebben een escalatie-pad per 24h-window, niet per-trade.
Een bot die twee verschillende anomalies triggert in één dag zit op
tier-2, niet op twee onafhankelijke tier-1's.

| Anomaly in 24h | Actie |
|:-:|---|
| 1e | Log-entry in audit-trail + low-priority alert in portal. Geen trading-impact. |
| 2e | Medium-priority alert + aanbeveling om bot te reviewen. Nog steeds geen trading-impact. |
| 3e | Caps gereduceerd naar 50%. High-priority alert (portal + email + PWA push). Volgende trade vereist push-approval via WebAuthn of TOTP. |
| 4e, OF anomaly tijdens active-halt | Volledige halt van signing-service voor deze user. Trade-requests worden geweigerd tot user via approval-channel de bot expliciet reactiveert. |

De halt bij 4 anomalies is bewust stricter dan de caps-reductie uit
Laag 4 tier 3 — anomaly-clustering is een sterker signaal dan pure
underperformance, omdat het patroon-gebaseerd is in plaats van
uitkomst-gebaseerd.

*Baseline-updates.*

- Baseline herberekent automatisch op elke legitimate strategy-
  wijziging die via de auth'd flow gaat (user past DCA-spacing,
  indicator-threshold, of timeframe aan via het portal). Vanaf de
  wijziging telt pre-existing baseline-data af; na ~14 dagen is de
  baseline volledig op de nieuwe config gebaseerd.
- Expliciete user-trigger: "herleer baseline na strategie-
  wijziging" knop in de bot-settings, vereist approval-channel.
  Wipet de baseline onmiddellijk en zet de bot terug in
  observation-only mode voor een volle leer-periode. Bedoeld voor
  grote strategy-shifts waar rolling-drift te traag is.
- Zonder trigger: baseline blijft rolling trailing 30d voor de
  frequency/size-metrics, 60d voor de timing-metrics. Past zich
  langzaam aan natural drift aan zonder dat gedrag van weken terug
  de toekomst domineert.

*Onafhankelijkheid-caveat.*

Baseline-storage en detection-logica leven in de signing-service.
Baseline-reset via legitimate user-trigger vereist PWA-approval (zie
Part 3.4 approval-hierarchy). Main-app compromise kan dus niet stil
de baseline herzetten om detection te verblinden.

De zwakkere kant: main-app levert de deal-stream waar detection op
draait. Een gecompromitteerde main-app kan trades zodanig voorfilteren
(niet rapporteren aan signing-service) dat anomaly-patterns niet
oppoppen. Dit is een scope-beperking, niet een defense-bypass: de
trades zelf moeten nog steeds door signing-service's cap-counters
(Laag 2/3) en per-trade-threshold (Laag 1), dus ongereporteerde
trades blokkeren niet de andere lagen. Laag 6 verliest wel zijn
pattern-detection-sterkte in dat scenario.

**Beschermt tegen.** Patterns die niet door caps worden gevangen
omdat ze qua volume binnen limits blijven maar qua shape verdacht
zijn (asymmetric burst, off-hours, frequency-spike).

**Beschermt niet tegen.** Patterns die legitimate strategy-behavior
mimicen; selective trade-underreporting door gecompromitteerde
main-app (andere lagen vangen wel volume, niet shape).

**Waar leeft dit.** Baseline-storage en detection-logica leven in
de signing-service. De main-app stuurt trade-events naar de
signing-service via het normale RPC-kanaal (elke trade-intent is
een signing-service call); die data voedt ook de anomaly-detectie.
Baseline-reset en detector-configuratie wijzigingen vereisen user-
approval via PWA en kunnen niet door main-app alleen worden gedaan.
Dit maakt Laag 6 afhankelijk van signing-service integrity — zie
Part 2 Scenario 2.8 voor de behandeling van signing-service
compromise als threat-scenario.

**Laag 7 — Balance-drop detection (read-only watchdog).**

Onafhankelijke service (reverto-watchdog), eigen infrastructuur.
Poll elke 3-5 minuten de exchange-balance via read-only API. Vergelijk
met expected-balance (opbouw: opening balance + realized PnL tot nu
+ expected position unrealized) die uit main-app komt. Drift > 5% of
> absolute-drop-threshold (bijv. $500) triggert:

1. Immediate alert naar user (email + SMS + PWA push) + operator.
2. Kill-signal naar signing-service: "weiger alle trades voor deze user tot
   handmatige reset."
3. Freeze user's caps-wijziging endpoints.

Beschermt tegen: elk scenario waar de attacker Reverto-side buiten
is maar exchange-side wel geld wegtrekt. Incl. exchange-side lek
(2.6), insider met direct-exchange-access.
Beschermt niet tegen: scenarios waar zowel Reverto als watchdog zijn
gecompromitteerd (dan heb je al verloren, maar separate infra +
read-only keys maakt dit veel minder waarschijnlijk).

Waar leeft dit: `reverto-watchdog` op aparte VPS/provider. Eigen
read-only API-keys per user. Eigen DB. Communiceert alleen naar
signing-service via mTLS "kill(user_id)" calls — kan user-state veranderen,
niet credentials uitlezen.

*Minimale onafhankelijkheids-eisen voor watchdog.*

Een watchdog is alleen zinvol als hij onafhankelijk blijft tijdens
compromise van de hoofd-infrastructuur. "Separate infra" is breed —
kan variëren van "andere container op dezelfde host" tot "andere
cloud-provider." Dit document pint drie minimale scheidingen als
harde requirement voor Phase F deployment:

1. **Separate access-credentials.** De watchdog deelt geen SSH-keys,
   cloud-IAM-rollen, VPN-credentials of API-tokens met de main
   Reverto-infrastructuur. Een aanvaller die de main-app heeft
   gecompromitteerd kan niet via dezelfde credentials ook de
   watchdog bereiken. Praktisch: aparte SSH-key per service, geen
   shared-key-agent, aparte cloud-accounts (of minimaal aparte
   IAM-users met strict-disjoint permission-sets).

2. **Separate deployment-pipeline.** Watchdog-code wordt niet via
   dezelfde CI/CD-flow gedeployed als de hoofdapp. Compromise van
   het CI/CD-systeem (GitHub Actions, self-hosted runners) raakt
   niet automatisch de watchdog. Watchdog-deploys zijn manual of
   via een fundamenteel andere pipeline (bv. een andere CI-provider,
   of een aparte self-hosted runner met eigen secrets).

3. **Separate alerting-channel.** Watchdog-alerts gaan via een
   andere route dan normale Reverto-alerts — niet dezelfde
   email-domain, niet dezelfde Slack-workspace, niet dezelfde
   Telegram-bot. Anders kan een aanvaller met main-app-compromise
   beide kanalen tegelijk muten en de watchdog-trigger onzichtbaar
   maken. Praktisch: watchdog krijgt eigen `@watchdog.reverto.example`
   subdomain en eigen alert-ontvanger (operator-persoonlijke
   telefoon-nummer of een fysiek secondary device).

4. **Geverifieerde data-feed voor expected-state vergelijkingen.**
   De watchdog moet weten wat de "verwachte balance" per user is
   om discrepancies te kunnen detecteren. Die informatie komt uit
   Reverto's eigen systemen (main-DB of signing-service state),
   maar het feed-kanaal zelf moet gehard zijn:

   - Watchdog pollt expected-state via eigen read-only credentials
     tegen een dedicated read-only endpoint — **geen push** van de
     main-app naar de watchdog. Pull-architectuur voorkomt dat een
     gecompromitteerde main-app fake expected-values actief
     injecteert op het moment dat de watchdog drift zou detecteren.
   - Elke watchdog-poll valideert data-feed integrity (signed
     messages, HMAC of vergelijkbaar; sleutel-materiaal apart van
     main-app's cookie-signing key om cross-key-misbruik te
     voorkomen).
   - Bij verlies van data-feed — endpoint onbereikbaar, timeout,
     of signature-mismatch — treat-as-potential-compromise: alert
     naar operator, bevries caps-wijziging voor álle users tot
     manual recovery. Liever false-positive halt dan stille blind-
     spot-periode.

   Zonder deze requirement kan een aanvaller met main-app-
   compromise de expected-state feed zo manipuleren dat een reële
   balance-drop "legitiem" lijkt en dus niet alerteert. De
   watchdog is dan in zichzelf nog beschikbaar maar leest verkeerde
   vergelijkingsdata — een zwakker-dan-bedoelde defense zonder dat
   iemand het merkt.

Aanvullend: overweeg een aparte cloud-provider of geografische regio
voor de watchdog. Niet verplicht als basis-requirement, wel expliciete
consideration in de Phase F deployment-planning. Kandidaat-providers
(DigitalOcean, Linode, OVHcloud) staan in Part 6 research-spoor.

**Laag 8 — Dead man's switch.**

User-configureerbare inactivity timeout (default 7 dagen, range
3-30 dagen). Meet: user-device authenticated actions (login, PWA
tap, manual trade, cap-wijziging). Passive cookie-present telt NIET
als activity.

Na timeout: alle bots pauzeren, alert naar user (email + SMS), 24h
pending-state waarin de user kan reactiveren zonder verdere checks;
daarna vol approval-channel nodig.

Beschermt tegen: attacker die rustig de tijd neemt nadat user
vakantie/ziek is; user die het niet meer merkt omdat ze de portal
niet bezoeken.
Beschermt niet tegen: attacker die zelf "activity" genereert op het
user-device (device-compromise, scenario 2.3) — maar dan heeft de
attacker sowieso al echte problemen voor de user.

Waar leeft dit: main-app (activity-tracker), signing-service
(enforces pause na timeout-trigger).

### 3.7 Independence Verification

De cruciale tabel. Voor elke laag: als de reverto-web host (main-
app) volledig gecompromitteerd is — attacker heeft root, leest
alles, muteert DB vrij — blijft deze defense dan overeind?

| Layer | Survives main-app compromise? | Depends on |
|-------|:-----------------------------:|------------|
| **L1** per-trade threshold + approval | Ja | Approval-token verificatie leeft in de signing-service; user-device bezit de WebAuthn-key. Main-app kan approval-requests posten, niet forgen. |
| **L2** daily cap | Ja | Cap-counters + rolling-max-berekening leeft in de signing-service DB. Main-app kan een cap-wijziging indienen maar die vereist approval + cooldown voordat de signing-service 'm honoreert. |
| **L3** weekly / monthly caps | Ja | Zelfde als L2 — signing-service state. |
| **L4** performance-scaling | Alleen als performance-metrics en tier-logica in de signing-service leven, en config-wijzigingen (inclusief tier-drempel-wijzigingen) user-approval vereisen | Signing-service isolatie + baseline-locking tegen unauthorized config-writes. Anti-gaming (geen cap-relaxation bij outperformance) voegt een tweede lock toe: zelfs als main-app P&L-data vervalst, kan dat alleen naar strengere caps leiden, niet naar versoepelde caps. |
| **L5** emergency floor | Ja | Floor-trigger berekent rolling-max uit de signing-service's eigen balance-snapshots (onafhankelijk van main-app-data), plus watchdog-balance-reads. Main-app compromise kan de floor niet verhogen. |
| **L6** anomaly detection | Alleen als baseline-storage en detection-logica in de signing-service leven | Signing-service isolatie. Als baseline in main-app leeft: defense is waardeloos bij main-app compromise. Architectural requirement uit Part 3.6 Laag 6: baseline-reset vereist PWA-approval. Zwakte die blijft: selective trade-underreporting door main-app kan patterns verbergen; andere lagen (L1/L2/L3) vangen nog steeds het volume. |
| **L7** watchdog | Ja, mits echt separate infra | Aparte VPS + andere provider + eigen read-only API-keys. Main-app compromise raakt watchdog niet. Kritieke eis: watchdog-kill-signaal is alleen valide met watchdog's eigen mTLS-cert. |
| **L8** dead-man-switch | Gedeeltelijk | Activity-tracker leeft in main-app; compromise kan fake-activity injecteren. Echter: activity-signal is ingebed in user-device authenticated actions (WebAuthn challenge-response, TOTP-codes). Main-app kan niet een TOTP-code verzinnen zonder ook de TOTP-seed te hebben — en die leeft in de signing-service. Dus gedeeltelijk independent: attacker kan dead-man-timer resetten door de user te laten tapp'en, maar niet uit zichzelf. |
| **IP-whitelist (exchange-side)** | Ja | Exchange enforces. Main-app + signing-service IP's zijn bekend bij de exchange; attacker op Reverto-infra moet vanuit Reverto's IP trade'en, wat hij mag — maar een attacker die via een ander kanaal keys heeft exfiltreerd kan niet vanuit attacker-IP. Tevens: watchdog detecteert buiten-Reverto trades. |
| **Withdraw-blacklist (exchange-side)** | Ja | Exchange enforces. Key-permissions-check op onboarding weigert withdraw-enabled keys; als user later op exchange de permissie aanpast, detect quarterly recheck + watchdog. |
| **TOTP / PWA WebAuthn / YubiKey private keys** | Ja | Private-keys leven op user-device. Main-app weet de challenges maar kan niet signen. |
| **bcrypt password hashes** | Ja, in de offline-cracking zin | Hashes kunnen gelezen worden; rounds=12 maakt offline cracking per-account duur. |
| **Audit-log forgery detection** | Afhankelijk van store | Main-app audit-log is muteerbaar door main-app compromise; signing-service audit-log is niet. Append-only externe log-aggregator (CloudWatch Logs / Loki / external syslog) zou ook main-app-logs robuust maken. |

**Conclusie.** Vier lagen (L1, L2/3, L5, L7) overleven volledig; twee
(L4, L6) overleven **alleen** als hun logica en state in de signing-
service leven — de architectuur-keuze is hun hele defense. Eén (L8)
blijft een partial defense die leunt op user-device-activity. De
zwakte bij L8 is bewust — main-app kan activity niet vervalsen maar
wel passief doen-alsof dat de user er is.

Dit dwingt: de signing-service isolatie en de aparte watchdog-
infrastructuur zijn niet optioneel. Zonder één van beide valt de hele
defense-in-depth stack. Specifiek voor L4 en L6: als de implementatie
in Phase E / Phase C afdwaalt en deze lagen in main-app belandt, is
de declaratie in deze tabel gelogen — daarom pint Part 7 Appendix
expliciet dat elke Phase-implementatie deze tabel moet raken.

**Signing-service als single point of failure.**

De spiegel-kant van bovenstaande tabel: omdat deze defense-in-depth
stack leunt op signing-service isolatie, neutraliseert een succesvolle
signing-service compromise de Lagen 2, 3, 4, 5 en 6 simultaan. De
interne defenses vallen als één — ze zijn architecturaal gekoppeld
aan dezelfde trust-boundary. Alleen defenses **buiten** Reverto's
infrastructure blijven dan intact:

- Exchange-side hardening (IP-whitelist, trade-only permission,
  withdraw-whitelist) — scenario 6-achtige exchange-controls op
  wat een aanvaller maximaal kan doen.
- Onafhankelijke watchdog (Laag 7) — mits écht onafhankelijk
  gedeployed volgens de 4 requirements uit Part 3.6 Laag 7,
  inclusief de geverifieerde data-feed.
- User-device approval-keys (WebAuthn, YubiKey) — private-keys
  leven op het device, niet in Reverto-infra.

Detectie + response in dit scenario verloopt via:
- Watchdog balance-drop alerts (Laag 7).
- Exchange-side permission-enforcement (scenario 6-style
  beperkingen op attacker-capability).
- User-observable symptoms (onverwachte trades, balance-movement
  zichtbaar in UI).

Voor de volledige dreigingsanalyse + mitigatie-strategie zie
Part 2 Scenario 2.8.

---

## Part 4 · Migration Roadmap

Phases zijn sequentieel genummerd. Dit document committeert niet aan
data — planning leeft in een separate document dat bewust wordt
bijgewerkt wanneer scope en capaciteit duidelijker zijn.

Fasen volgen elkaar op zonder harde datum-commits. Elke fase heeft
concrete deliverables; ordering is bewust zodat een halverwege-
gestaakt project niet in een half-werkende state eindigt.

### Phase A — Foundation

**Deliverables:**
- Audit v26 findings: alle HIGH + blocker-MEDIUMs (v26-15h, v26-01,
  v26-02, v26-10, v26-16, v26-19) fixed.
- `CredentialProvider` interface-abstractie toegevoegd in
  `core/credentials.py`: de huidige per-user Fernet-implementatie
  wordt een concrete implementation van een abstract interface, zonder
  nu een tweede implementation te bouwen. Voorbereiding voor Phase C.
- Exchange-call audit: matrix van elke call in `exchanges/bitget.py` +
  `exchanges/kraken.py` met de minimale permission-scope die hij
  nodig heeft. Output: `docs/exchange-permissions.md` als
  implementatie-referentie voor Phase D key-permissions-verifier.
- Structured audit-logging: huidige `_audit(action, slug, actor)` in
  `web/app.py` uitbreiden naar JSON-structuur met timestamp, user_id,
  ip, result. Preparatie voor externe log-aggregator in Phase G.
- Dit document (v1).

### Phase B — Authentication Hardening

Phase B landt in 5 PRs. Status per deliverable hieronder. PR 1
(foundation) is geland; PR 2..5 volgen incrementeel zodat elke PR
zelfstandig review-baar is en geen halve auth-stack tegelijk live
gaat.

**Deliverables:**
- TOTP 2FA-layer: nieuwe kolom `users.totp_seed_encrypted` +
  bijbehorende encryption-key-management (nog in main-app, verhuist
  in Phase C).
  **STATUS: foundation landed in `feat/totp-foundation` (PR 1).**
  DB-schema v8 → v9 (additive ALTER TABLE), `core/totp.py` met
  `pyotp`-backed RFC-6238 helpers (generate_secret,
  generate_provisioning_uri, verify_code), per-user Fernet
  encryption via uitgebreide `CredentialProvider` interface
  (`encrypt_for_user` / `decrypt_for_user`), 38 regression-tests
  inclusief schema-migration check + tampered-ciphertext +
  cross-user key-isolation. Geen endpoint of login-flow consumeert
  de kolom nog — pure structurele PR.
- TOTP-seed rotation endpoint (voor users die hun authenticator-app
  verliezen; requires admin-action initially).
  **STATUS: PARTIAL — disable + re-enroll path live in PR 2
  (`feat/totp-enrollment`). Admin-reset endpoint voor andere users
  (operator handmatig een seed clearen) blijft pending — tracked
  voor een aparte sweep-PR omdat het een role-gated mutating endpoint
  introduceert dat los staat van de self-service flow.**
- TOTP-verify endpoint + integratie in `/auth/login` flow.
  **STATUS: integration complete in PR 3
  (`feat/totp-login-integration`). `/auth/login` now branches on
  `user.totp_enabled`: users without 2FA get the historical
  password-only response (zero behaviour change), users with 2FA
  get `requires_totp: true` + a 2-minute pending-login-TOTP cookie
  (separate `URLSafeTimedSerializer` salt — confused-deputy attack
  with the enrollment cookie fails at the MAC). The new
  `/auth/login/totp` endpoint reads the pending cookie, re-resolves
  the user (catches active=0 + disable-mid-flow races), decrypts
  the seed, verifies the code, and mints the real session cookie
  via the shared `_mint_session_response` helper. 5 new audit-event
  types: `login_password_ok_totp_required`,
  `login_success_totp`, `login_totp_failed` (denied),
  `login_totp_user_inactive` (denied),
  `login_totp_disabled_mid_flow` (denied), plus
  `login_totp_decrypt_failed` (error) for the integrity-event
  alert path. Lockout-recovery via SQL fallback
  (`UPDATE users SET totp_seed_encrypted = NULL WHERE id = ?`)
  is pinned by `test_recovery_via_seed_null_returns_to_password_
  only_path` so a future refactor that introduces a separate
  `totp_required` gate can't silently break the operator's
  tested fallback.**
- Password-rotation prompt: forced password change elke 6 maanden
  voor admin-role; optional voor user-role.
  **STATUS: pending (uitgesteld; vereist UX-design eerst).**
- Rate-limiting per-user toegevoegd aan login-path
  (momenteel alleen per-IP).
  **STATUS: complete in PR 4 (`feat/per-user-login-rate-limit`).**
  10 failed attempts in een 15-minuten venster triggeren 429
  Too Many Requests met een `Retry-After` header. Counter wordt
  geïncrementeerd op zowel password-step failure als
  /auth/login/totp wrong-code; wordt pas gereset op FULL login
  success (no-TOTP path: na password verify; TOTP path: na
  /auth/login/totp success). Pre-PR4 reset gebeurde direct na
  password-verify, wat een password-cracker met TOTP-faal hun
  counter cheaply liet resetten. Twee nieuwe audit events:
  `login_rate_limit_hit` en `login_totp_rate_limit_hit` (beide
  result=denied). Window-constante `FAILED_LOGIN_WINDOW_S`
  aangescherpt van 3600 → 900 sec per de operator-decision in
  Phase B threshold-strategie. Dit sluit ook de tweede helft
  van v26-01 — eerst (Phase A) was de active-check parity gap
  gesloten, nu is de per-user rate-limit erbij gekomen.
- Cookie-posture regression test (audit v26 v26-22).
  **STATUS: complete in PR 5
  (`feat/cookie-posture-regression-test`).** 12 regression tests in
  `tests/test_cookie_posture.py` pin the attribute posture
  (HttpOnly, Secure, SameSite=Strict, Path=/) for all four
  production cookies — `reverto_session`, `reverto_csrf` (with
  intentional non-HttpOnly carve-out for the double-submit
  pattern), `reverto_totp_pending`, `reverto_login_totp_pending`.
  Defence-in-depth checks: no Domain attribute on any cookie
  (would broaden scope to subdomains) and `reverto_session` is
  NOT minted during the TOTP-pending phase (would bypass the
  2FA gate). Closes v26-22. Phase B is now feature-complete.

### Phase C — Service Separation

**Deliverables:**
- PostgreSQL migratie vanaf SQLite. Alembic-based migrations.
- Docker Compose setup met drie services: `reverto-web`,
  `reverto-signer`, `reverto-db`. Gedeelde network + isolated
  data-volumes per service. Nog op Reverto-Server — geen cloud-migratie in
  deze fase.
- `reverto-signer` scaffolding: FastAPI app, eigen Postgres schema,
  mTLS-ready. Begint als dumb-proxy (forwardt calls naar
  exchange-client) zonder scope-enforcement — scope whitelist komt
  incrementeel in Phase D.
- mTLS-setup met self-signed CA (operator-rooted).
- Move credential-store naar de signing-service DB. Main-app verliest
  read-access tot `keys/` en `credentials/`.
- Migration-script om bestaande credentials uit main-app naar de
  signing-service te verhuizen zonder user-downtime.

### Phase D — User-Facing Security

**Deliverables:**
- PWA met WebAuthn enrollment-flow. Web-manifest, service-worker,
  installable op iOS/Android/desktop browsers.
- Approval-request endpoints: main-app creëert een request, PWA
  haalt openstaande requests op, user tapt om te signen, signature
  gaat naar de signing-service voor verificatie.
- Onboarding-wizard: exchange-key upload + permission-check
  (weigert withdraw-enabled) + IP-whitelist verification (attempt
  order from wrong IP, expect exchange-rejection).
- Onboarding UI voor TOTP-fallback setup + recovery-codes.
- YubiKey registration-flow (FIDO2) — premium-tier later
  ontgrendelbaar.

### Phase E — Defense Layers

**Deliverables:**
- Percentage-based caps in de signing-service DB (Laag 2 + 3).
  `caps_rolling_counters` tabel + update-job op elke trade.
- Rolling-max berekening + user-visible dashboard van "current cap
  consumption this week/month."
- Performance-scaling (Laag 4): backtest-expected-range stored in
  `bot_configs` (nieuwe kolom), realized-vs-expected berekening,
  30d-rolling trigger.
- Emergency floor (Laag 5): floor-calculation + pause-trading on
  trigger.
- Anomaly detectors (Laag 6) per type; start met asymmetric-
  patterns (makkelijkst, hoogste signal).
- Dead-man-switch (Laag 8): activity-tracker + timeout-worker.

### Phase F — Independent Watchdog

**Deliverables:**
- Aparte VPS bij een andere provider dan de main-app. Andere OS-image
  (bijv. Debian waar main-app Ubuntu draait).
- `reverto-watchdog` service: read-only API-key storage per user,
  polling-loop, expected-balance-model, kill-signal-emitter.
- Watchdog onboarding: elke new user moet bij Phase F-launch ook
  een read-only API-key uploaden; onboarding-wizard krijgt stap
  "maak tweede read-only key voor monitoring."
- Kill-signal-protocol: watchdog → signing-service mTLS endpoint.

### Phase G — SaaS-launch readiness

**Deliverables:**
- Migratie van Reverto-Server → Hetzner / Linode / comparable VPS. DNS,
  TLS-cert-management (Let's Encrypt auto-renew), backup-strategy
  naar object-storage.
- Incident-response plan (apart document). Concrete runbooks voor:
  server-compromise, user-compromise, exchange-lek, watchdog-alert,
  mass-payout-request.
- Terms of Service met disclaimer-secties: geen guarantees, niet
  aansprakelijk voor trading-verliezen, custody-disclosure.
- Privacy-policy (GDPR-compliant voor EU users).
- Status-page (status.reverto.example) met uptime en recent incidents.
- External security-review: scoped aan infrastructure, auth-stack,
  credential-handling, signing-service scope-enforcement. Bij
  voorkeur een firm die cripto-bot-platforms eerder heeft gereviewed.
- Go/no-go beslissing: alle HIGH + MEDIUM audit-v26 findings
  gesloten, externe review zonder HIGHs, first 3 beta-users
  gedraaid ≥ 30 dagen zonder incident.

---

## Part 5 · Explicit Non-Decisions

Wat we expliciet NIET doen in dit model, en waarom. Elke keuze
blijft open voor heroverweging maar heeft een reden om voor nu niet.

- **Geen non-custodial architectuur.** UX-incompatible met de
  bot-markt: non-custodial zou vereisen dat de user voor elke trade
  real-time tap-to-sign doet, wat de hele waarde van een 24/7 DCA-
  bot ondermijnt. Alle concurrenten (3Commas, Pionex, Cryptohopper,
  Shrimpy) zijn custodial om dezelfde reden. Risico wordt
  gemitigeerd door Part 3.1 + 3.6 en door expliciete Terms of
  Service disclosure.

- **Geen hardware-wallet integratie voor trading.** Ledger/Trezor
  hebben geen CEX-API signing-modus — ze signen on-chain transactions
  en arbitrary-data-signing is niet overal consistent gesupported door
  Bitget/Kraken. YubiKey-WebAuthn voor portal-approvals is een andere
  (wel werkende) usage.

- **Geen MPC threshold-signing.** Techniek bestaat (Fireblocks, Copper)
  maar vereist dedicated co-signer-infrastructuur. Voegt complexiteit
  toe waar defense-in-depth al de gewenste robustness biedt. Mogelijk
  future R&D voor enterprise-tier — zie Part 6.

- **Geen SMS-based 2FA.** SIM-swap is de bekende aanvalsroute;
  Robinhood-2020 incident als case study. TOTP via een authenticator-
  app is toegankelijk voor dezelfde user-populatie zonder deze
  zwakheid.

- **Geen email-based trade-approval.** Email is een phishing-anchor:
  user klikt op een link die er officieel uitziet, belandt op
  attacker-portal, doet approve "trade." Email is OK als
  kennisgevings-kanaal (de alert dat er een cap-wijziging wacht met
  48h cooldown) maar niet als approval-channel zelf.

- **Geen GraphQL API.** REST is genoeg voor de huidige scope; GraphQL
  voegt attack-surface (query complexity, batching, introspection) toe
  zonder proportioneel voordeel.

- **Geen webhook-callbacks naar user-URLs.** Zou een SSRF-surface
  openen. Alerts lopen alleen via de kanalen die wij beheren
  (portal-UI, PWA push, email, optionele Telegram).

- **Geen multi-region deployment vóór SaaS-launch.** Single-region
  is simpler en sufficient voor de beta-scale. Multi-region wordt
  pas interessant bij > 1000 users of regulatory-data-residency
  requirements.

- **Geen recovery-flow die approval-auth kan herstellen zonder
  bestaande approval-device.** Elk recovery-mechanisme dat Reverto-
  medewerkers kan laten beslissen over user-access ondermijnt het
  hele threat-model. Social engineering is de bekendste attack-vector
  op custodial platforms; helpdesk-flows met manual-override zijn
  historisch de zwakste schakel (zie o.a. Coinbase-support en
  Binance-support incidenten). Als user alle approval-devices
  verliest wordt het account effectief gelockt voor mutating
  operations.

  Onboarding vereist daarom minimaal **twee** geregistreerde
  approval-methodes: PWA op een device + TOTP, of PWA op twee
  devices, of PWA + YubiKey. Bij verlies van één kan de user met
  de andere een nieuwe device registreren (48h cooldown uit Part
  3.4 blijft van kracht).

  Bij verlies van **alle** approval-devices: de user moet op de
  exchange-kant zelf de API-keys roteren (trade-only keys blijven
  laag-risico als ze achterblijven — IP-whitelist blokkeert
  attacker-access) en een fresh onboarding bij Reverto doen via
  nieuwe keys + nieuwe devices. De bestaande bot-state (deal-
  history, annotations, backtest-runs) blijft behouden, maar elke
  trading-actie is geblokkeerd tot de user een nieuwe approval-
  device heeft geregistreerd. Pijnlijk; het alternatief
  (helpdesk-override) is strategisch onacceptabel.

- **Audit-log retention en GDPR-compliance** is niet uitgewerkt in
  deze spec. Structured audit-logging van credential-interacties is
  in Phase A opgenomen als implementatie-requirement, maar
  retention-policy, access-control op logs, en GDPR
  data-subject-rights (recht op inzage, recht op vergetelheid) zijn
  operational concerns die apart gedocumenteerd moeten worden vóór
  SaaS-launch. Logs mogen in principe niet langer bewaard worden dan
  operationeel nodig; specifieke retention-waarden volgen uit
  compliance-onderzoek (zie Part 6 research-spoor).

- **Business continuity bij service-outage** is niet uitgewerkt in
  deze security-spec. Als Reverto's signing-service down is, kunnen
  bots geen trades meer uitvoeren — dat kan financieel schade geven
  bij open posities die normaal gesloten zouden worden. Beperkings-
  maatregelen (failover signing-service, auto-close bij extended
  outage, user-notification flows) zijn operational en beschreven in
  een aparte operational-runbook. Security-architectuur maakt
  expliciet dat outage **niet mag leiden tot security-bypass**
  (fail-closed, niet fail-open): bij outage stoppen alle trading-
  activiteiten, geen emergency trade-execution zonder signing-
  service-approval.

- **Incident-response communication protocol** voor security-
  incidents is niet gedekt in deze spec. Structuur voor
  user-notification bij (verdacht van) breach, communicatie-cadans,
  wat wel/niet direct publiek maken, coordination met exchanges,
  law-enforcement-engagement: dit zijn organisatorische processen
  die in een aparte incident-response playbook horen. De security-
  architectuur legt de technische detectie-capabilities vast
  (Part 3.6 Laag 7 watchdog, anomaly-alerts); de response daarop is
  operationeel.

---

## Part 6 · Open Questions & Future Research

Onderverdeeld in twee blokken: items die beantwoord moeten zijn
voordat Phase B kan launchen (dat is de eerstvolgende fase waar een
user-facing keuze wordt vastgelegd), en een research-spoor dat geen
launch-blocker is maar wel aandacht verdient op langere termijn.

### 6.1 Resolved pre-launch decisions (2026-04-28)

Op 2026-04-28 zijn de twee Phase B must-answer items met user-
facing scope geresolved (TOTP/PWA strategie + threshold-tabel).
Het derde must-answer item — Bitget-subaccount-support — is op
deze checkpoint NIET geresolved en blijft als open vraag onderaan
deze sectie staan. De resolved beslissingen bepalen het launch-
design van TOTP/PWA-implementatie en threshold-enforcement; Phase
B implementatie kan op basis hiervan starten.

#### Decision A — TOTP/PWA strategie: HYBRIDE

Phase B launcht met TOTP als baseline-2FA, verplicht voor alle
users. PWA WebAuthn is een optionele upgrade die verhoogde
thresholds unlocks (zie 6.2 cap-tabel).

**Rationale.**

- TOTP is een bekend pattern in de crypto-doelgroep met lage
  onboarding-friction. Verplichte PWA-only zou users uitsluiten
  die geen PWA willen of kunnen installeren.
- PWA WebAuthn biedt sterkere defense (device-bound key,
  phishing-resistant, secure-enclave waar beschikbaar) maar is
  nog niet 100 % naadloos op alle desktop-browsers.
- De cap-verschillen tussen tiers maken het verschil ook een
  product-onderscheid: "we belonen sterkere auth met hogere
  trade-thresholds" past bij de custodial-no-paternalistic
  stance uit Part 1.3.
- Migratie naar verplichte PWA wordt herzien wanneer PWA-support
  volwassener is. **Re-evaluation target: Q4 2027.**

**Operationele implicaties.**

- Setup-flow: een nieuwe user moet TOTP enrollen voordat de
  eerste live trade mogelijk is. Read-only en paper-mode acties
  blijven werken zonder TOTP zodat onboarding-friction beperkt
  blijft tot het moment dat het er echt toe doet.
- Profile-pagina toont een "Upgrade naar PWA WebAuthn voor
  verhoogde caps" call-to-action zodra een user de TOTP-flow
  heeft afgerond.
- Recovery-pad voor TOTP-loss in Phase B: admin-manual reset met
  audit-trail. Phase D vervangt dit door een user-initiated
  recovery-flow met cooldown.

#### Decision B — Threshold strategie: conservatieve defaults + operator-approved verhoging

Default caps zijn safe-by-default (low). Users vragen verhogingen
aan via support-flow; de operator (initially: ROOT) reviewed de
aanvraag handmatig en stelt cap-set bij voor de specifieke user.
Volume-based auto-tiering wordt overwogen voor Phase D zodra
voldoende verkeer-data beschikbaar is voor calibratie.

**Rationale.**

- Een gecompromiseerde session moet beperkt schade kunnen doen.
  Lage defaults zorgen dat zelfs een attacker met cookie + TOTP-
  device een kleine harm-budget heeft tot de operator-approved
  verhoging bekend is.
- Manuele review-step bij verhoging biedt human-in-the-loop voor
  high-trust accounts en geeft een natuurlijke moment om context
  uit te vragen ("wat voor strategy ga je draaien dat een
  weekly cap > 1 BTC nodig heeft?").
- Auto-tiering uitstellen tot Phase D voorkomt het ontwerpen van
  een algoritme zonder real-world calibratie-data — een common
  failure mode bij rate-limiter / cap-systemen.

**Implementatie-implicatie.**

- Phase B: hardcoded default-caps in config + per-user override-
  velden in de `users`-tabel (kolommen `per_trade_cap`,
  `daily_cap`, `weekly_cap`, `monthly_cap`, allemaal NULL ⇒
  default-tier-cap-uit-config).
- Phase D: replacement van per-user overrides door een
  auto-tiering algoritme dat op (account-age, trade-volume,
  realised-PnL-stability) tiers toekent. Per-user overrides
  blijven beschikbaar als operator-escape-hatch.

#### Open: Bitget subaccount-support (carry-over)

Het derde must-answer item uit de v1-revisie van dit document
blijft op 2026-04-28 onbeslist. Onduidelijk is of Bitget's
retail-subaccount-API de volledige flow ondersteunt die Part 3.5
"recommended" noemt. Wordt geadresseerd in een aparte exchange-
research-thread; zie 6.3b "Exchange-subaccount-mapping per
exchange" voor het bredere onderzoek waar deze sub-vraag in past.
Niet-blocking voor Phase B (TOTP/threshold-enforcement raakt geen
exchange-subaccount-flow); blocker voor de Phase B → live-launch
gate als Bitget de eerste live-exchange wordt.

### 6.2 Threshold cap-tabel (definitief voor Phase B launch)

Default caps per auth-tier voor nieuwe users. Alle waardes in BTC
(inverse-perpetual-aware: ze zijn position-size limits, niet
notional USD).

| Auth tier            | Per-trade | Daily    | Weekly   | Monthly |
|----------------------|-----------|----------|----------|---------|
| Session-only         | 0.005 BTC | 0.02 BTC | 0.05 BTC | 0.1 BTC |
| TOTP                 | 0.05 BTC  | 0.2 BTC  | 0.5 BTC  | 1 BTC   |
| PWA WebAuthn         | 0.5 BTC   | 2 BTC    | 5 BTC    | 10 BTC  |

**Notes.**

- Session-only caps gelden voor read-only en paper-trade actions.
  Live-trade actions vereisen ten minste TOTP-tier — de session-
  only kolom is hier voor read-paths en paper-mode opgenomen,
  niet als gangbare live-trade-tier.
- Verhogingen boven deze defaults vereisen operator-approval
  (Phase B); auto-tiering wordt geëvalueerd in Phase D.
- YubiKey-tier (FIDO2 hardware) is gepland voor Phase D met een
  "no cap" policy voor authenticated actions binnen het account.
  YubiKey-tier wordt gehouden achter een premium-product-tier
  (zie 6.3c "Tier-modelering").
- De caps zijn rolling windows die door de signing-service worden
  bijgehouden (Part 3.6 Laag 2/3). Een wijziging van de defaults
  in deze tabel raakt alleen NEW users — bestaande users blijven
  op hun reeds-geconfigureerde set tot een operator-approved
  verhoging anders bepaalt.
- Performance-gemoduleerde scaling uit Part 3.6 Laag 4 blijft
  bovenop deze defaults werken: de baseline-cap zoals hier
  gedefinieerd wordt door Laag 4 verlaagd bij underperformance,
  nooit verhoogd bij outperformance (anti-gaming asymmetry).

### 6.3 Research-spoor (geen launch-blocker)

Gegroepeerd voor scanability. Geen nieuwe items t.o.v. de vorige
revisie — alleen herpositioneerd in drie categorieën.

#### 6.3a Compliance & regulatory

- **Compliance-requirements per EU jurisdictie.** Nederland (AFM,
  DNB-voor-custody-aspecten), EU algemeen (MiCA van toepassing per
  2024-2025), mogelijk UK (FCA) — regulatory status voor custodial
  bot-platforms is een moving target. Beslissen welke
  jurisdictie(s) te targeten heeft implicaties op: KYC-niveau,
  licentie-requirements, insurance-requirements, data-residency,
  audit-log retention (zie Part 5). Apart beleidsdocument zodra een
  target gekozen is. Sub-items die in dat beleidsdocument horen:

  - GDPR data-subject-rights implementatie (recht op inzage, recht
    op vergetelheid, bewaartermijnen per data-categorie).
  - MiCA-implicaties: custodial-obligations, capital-requirements
    voor crypto-asset service providers, white-paper publishing
    eisen.
  - Nederland-specifieke compliance: DNB registratie-plicht,
    AFM-marktoezicht-positie voor trading-bot-platforms.

- **Incident-response playbook.** Apart document nodig; niet in
  scope van deze spec. Bij voorkeur geschreven door iemand met
  incident-response-ervaring (onze security-reviewer in Phase G
  kan adviseren). Zie ook Part 5 "Incident-response communication
  protocol".

#### 6.3b Technical R&D

- **MPC threshold-signing voor enterprise-tier.** Technisch
  interessant voor users met significant balance. Fireblocks +
  vergelijkbare providers hebben commercial APIs; vereist contract
  + monthly fees. Pas relevant als we enterprise-customers
  onboarden.

- **Ledger/Trezor API-signing compatibility.** Hardware-wallets
  kunnen on-chain transactions signen, maar CEX-API HMAC-signing
  is een andere primitive. Onderzoek of Ledger's developer-SDK een
  pad biedt waar een hardware-device een Bitget- of Kraken-API-
  request kan signen. Nu als non-decision gedocumenteerd (Part 5);
  kan heropend worden als hardware-support verschijnt.

- **Master Key rotation operational-overgangspad.** Tijdens Phase B
  leeft de Master Key voor envelope-encryption praktisch in de
  main-app (nog geen signing-service). In Phase C verhuist hij
  naar de signing-service. De migratie vereist een key-rotation
  dance: elke bestaande DEK moet worden ge-re-encrypt met de
  nieuwe MK-locatie, idealiter zonder user-downtime. Technisch
  oplosbaar (zie MK-rotation flow in Part 3.2) maar niet triviaal;
  concrete sequence te schrijven in een Phase-C implementation-doc.

- **Exchange-subaccount-mapping per exchange (waar supported).**
  Bitget en Kraken documenteren subaccount-API verschillend;
  onduidelijk of elke onboarded user een volledige
  subaccount-isolatie via de API kan krijgen zonder handmatige
  exchange-UI-stappen. Blocker voor automatische subaccount-
  onboarding in Part 3.5. Separate van Part 6.1 Bitget-vraag —
  dit stuk is breder (alle exchanges, alle flows, inclusief
  provisioning + teardown).

- **Watchdog infra-provider keuze.** Main-app likely op Hetzner;
  watchdog idealiter op een andere provider. Kandidaten:
  DigitalOcean, Linode, OVHcloud. Geen hard-onderzoek gedaan;
  main-criteria: andere netwerk-paden + onafhankelijke billing +
  geen gedeelde shared-hosting infra. Zie ook Laag 7 "Minimale
  onafhankelijkheids-eisen voor watchdog" (Part 3.6).

#### 6.3c Product positioning

- **Tier-modelering (basic TOTP vs PWA vs YubiKey premium) en
  pricing-security-tradeoff.** Part 3.3 noemt YubiKey als
  premium-tier optie zonder de feature-breakdown, pricing-strategy,
  of security-impact-tradeoff uit te werken. Hoe ver moet de
  security-downgrade gaan voor TOTP-only users voordat het een
  UX-probleem wordt? Gaat PWA altijd gratis zijn of ook achter een
  tier? Research nodig op: concurrent-benchmarks (3Commas, Pionex
  tier-structuren), user-research op onboarding-friction bij
  verschillende auth-methoden, cost-structure per tier.

- **Threshold-calibratie per strategy-type (DCA vs grid vs scalp).**
  Part 3.6 Laag 4 drempels (−2% / −5% / −10% over 7d) zijn voor
  DCA-achtige strategies gecalibreerd. Grid- en scalp-strategies
  hebben structureel andere volatility-profielen. Per-strategy
  override is geplande feature (Laag 4 subsection E), maar calibratie
  per strategy-type moet met real-world data gebeuren. Vergelijkbaar
  met de user-configureerbare-vs-platform-mandated threshold vraag
  voor "grote trade" (2% default, 1-5% range) — user-configureerbaar
  past bij een custodial-no-paternalistic stance; platform-mandated
  dwingt minimum-prudence af. Voorlopig user-configureerbaar;
  overwegen een platform-floor te introduceren als Laag 2/3
  usage-patroon toont dat users caps te hoog zetten.

- **Passkeys via iOS/Android OS-level.** Apple en Google syncen
  Passkeys via iCloud/Google-account. Dit is user-vriendelijk maar
  introduceert dependency op Apple/Google account-security. Voor
  trading-custody mogelijk te permissive. Te beslissen tijdens
  Phase D: alleen device-bound keys accepteren, of ook Passkey-
  gesynchroniseerde keys? Raakt de tier-modelering hierboven —
  strictere account types kunnen device-bound-only afdwingen.

- **Dead-man-switch-reset bij lange legitieme afwezigheid.** User
  gaat 3 weken op vakantie, had 7d-timeout gezet, bots pauzeren na
  7d. Heropstart-flow bij terugkomst moet simple zijn; als 'ie té
  simple is wordt de defense zinloos. Balance: 24h-pending-state
  plus one-tap reactivation is in Part 3.6 Laag 8 ingeschat;
  user-testing moet aantonen of dit werkt.

---

## Part 7 · Appendix: Cross-references

**Audit v26 findings die aan dit document raken:**

| Finding | Severity | Sectie in dit doc |
|---------|----------|-------------------|
| v26-15h | HIGH | Part 2.1 (server-compromise emergency-stop als mitigatie), Part 4 Phase A |
| v26-01 | MEDIUM | Part 3.3 authentication stack — consolidatie `_require_session` en `_request_user` |
| v26-02 | MEDIUM | Part 3.4 approval hierarchy — emergency-stop admin role |
| v26-03 | MEDIUM | Part 3.3 password policy — consolidatie min-length |
| v26-10 | MEDIUM | Part 4 Phase C PostgreSQL-migratie — operator-gate op destructieve migrations |
| v26-16 | MEDIUM | Part 3.1 service-separation — WS broadcaster per-user filtering is forward-looking requirement |
| v26-18 | MEDIUM | Part 3.5 onboarding — exchange-permissions-check is een concreet soort van "config path must be user-scoped" |
| v26-19 | MEDIUM | Part 4 Phase A — runbook-updates + setup-admin documentatie |

**Phase-3 scoping document (`docs/phase-3.md`):**

Dit security-model document breidt `docs/phase-3.md` uit op drie
punten:
- Phase-3a auth is shipped; dit doc beschrijft Phase-3b (TOTP), C
  (service-separation), D (user-facing security), E (defense-layers),
  F (watchdog).
- De emergency-stop admin-cross-user design in §2 van phase-3.md is
  hier geconcretiseerd in Part 3.4 met expliciete approval-channel
  requirements.
- Per-user credential-opslag uit §2 van phase-3.md blijft geldig; het
  MOVE-target verandert: niet alleen per-user directory, maar per-user
  in de signing-service DB (Part 3.2).

**Runbook entries die moeten worden bijgewerkt:**

- `docs/runbook.md` "Startup checklist": uitbreiden met
  `make setup-admin` (audit v26 v26-19) en — post-Phase-B — een TOTP-
  enrollment stap.
- `docs/runbook.md` "Credential rotation": uitbreiden met per-user
  Fernet-key rotation + exchange-key rotation als aparte procedures
  zodra Phase D er is.
- Nieuwe sectie "Incident response" — scope en volume vallen buiten
  deze spec (zie Part 6). Pas schrijven als incident-response
  playbook bestaat.

**Bestaande codebase-hooks waar target-state werk aan raakt:**

| File | Huidige rol | Target-state aanpassing |
|------|-------------|-------------------------|
| `web/app.py` AuthMiddleware | gatekeeps HTTP requests | Unchanged qua structuur; TOTP-check erin gehaakt (Phase B). |
| `web/routes/auth.py` | login / logout / change-password | Uitbreiden met `/auth/totp/enroll`, `/auth/totp/verify`, `/auth/webauthn/*` routes (Phase B+D). |
| `core/user_store.py` | DB-based user helpers | Uitbreiden met `totp_seed_get/set` + `webauthn_credentials` (Phase B+D). Password-policy constante consolideren (v26-03). |
| `core/credentials.py` | per-user Fernet + per-exchange .enc | Verhuist naar de signing-service in Phase C. Main-app-side wordt een `CredentialProvider` interface die naar signing-service RPC calls doet. |
| `exchanges/base_exchange.py` | ccxt abstraction | Verhuist naar de signing-service (Phase C). Main-app houdt alleen market-data calls (read-only, no api_secret). |
| `live/live_engine.py:281` | `NotImplementedError` voor real orders | Wordt RPC-call naar signing-service `place_trade(user_id, intent)` (Phase C). |
| `scripts/setup_admin.py` | admin password provisioning | Uitbreiden met TOTP-bootstrap (Phase B). |

**Independence-matrix (Part 3.7) als hard-requirement:**

Elke Phase waarin een defense-laag wordt gebouwd moet de
independence-tabel expliciet raken. Reviewer voegt bij Phase-end
een kolom toe: "implemented as designed?" — zonder dat wordt de
defense-in-depth stack incompleet.

**Operational runbook (separate document, nog aan te maken):**

De volgende operational concerns worden meermaals verwezen in dit
security-model maar worden hier niet uitgewerkt. Bij
implementatie-start moet `docs/operational-runbook.md` worden
aangemaakt als separate document met minimaal:

- **Incident-response communication protocol** — ref: Part 5
  non-decisions "Incident-response communication protocol".
- **Business continuity bij service-outage** — ref: Part 5
  non-decisions "Business continuity bij service-outage".
- **Backup-restore procedures per data-store** — ref: Part 3.2
  Backup-strategie en MK-rotation.
- **Audit-log retention en access-control** — ref: Part 5
  non-decisions "Audit-log retention en GDPR-compliance".
- **User-communication bij security-relevant events** — ref:
  Part 3.6 Laag 7 (watchdog-alerts) en Laag 8 (dead-man-switch
  triggers).

Dit document blijft focus op security-architectuur; operational
execution is een cross-cutting concern die apart wordt gemanaged.
Dangling references in dit doc naar "separate operational runbook"
wijzen straks naar dat bestand.

---

## Document changelog

- **2026-04-28 (latest+3)** — Phase B PR 5 status update in Part 4:
  cookie-posture regression test landed
  (`feat/cookie-posture-regression-test`). 12 tests pin the
  HttpOnly / Secure / SameSite=Strict / Path=/ attributes on all
  four production cookies. Closes v26-22 (was ACCEPTED 2026-04-21
  with three revisit-triggers; Trigger #1 — Phase B re-opens the
  auth-stack — fired). **Phase B is feature-complete.** Phase C
  (signing-service service-separation) follows when multi-tenant
  rollout becomes scope-relevant.
- **2026-04-28 (latest+2)** — Phase B PR 4 status update in Part 4:
  per-user login rate-limit complete (`feat/per-user-login-rate-
  limit`). New `check_login_rate_limit` helper in
  `core.user_store` (returns `(is_limited, retry_after_seconds)`),
  `Retry-After` header on 429, `login_rate_limit_hit` +
  `login_totp_rate_limit_hit` audit events. Window tightened from
  1 h → 15 min. Counter reset moved out of password-step success
  into FULL-login success so a password-cracker who fails TOTP
  can't reset their counter for free. Closes the second half of
  v26-01. 18 regression tests including user-enumeration defence
  + before-bcrypt placement check.
- **2026-04-28 (latest+1)** — Phase B PR 3 status update in Part 4:
  TOTP login-flow integratie compleet (`feat/totp-login-integration`).
  `/auth/login` gates op `totp_enabled`, nieuwe `/auth/login/totp`
  voor de tweede stap, separate-salt pending-cookie (2-min TTL),
  6 nieuwe audit-event types (success + 4 denied + 1 error). 19
  regression tests inclusief operator-recovery contract. Phase B
  is hierna feature-compleet voor de TOTP-track; PR 4 (per-user
  rate-limit) en PR 5 (cookie-posture regression test) blijven
  als hardening-werk over.
- **2026-04-28 (latest)** — Phase B PR 2 status update in Part 4:
  enrollment-flow live (`feat/totp-enrollment`). Three new endpoints,
  pending-TOTP cookie via separate `URLSafeTimedSerializer`, server-
  rendered SVG QR (deviation from PR-template's CDN approach —
  closes v27-04 supply-chain surface as side-effect). Profile-page
  UI + 23 regression tests. PR 3 will integrate TOTP into the
  `/auth/login` flow.
- **2026-04-28 (later)** — Phase B PR 1 status update in Part 4:
  TOTP foundation gemarkeerd als landed (DB-schema v9 +
  `core/totp.py` helpers + `CredentialProvider.encrypt_for_user` /
  `decrypt_for_user` extension + 38 regression tests). Andere
  Phase B deliverables (PR 2..5) als pending gemarkeerd zodat de
  status per-PR traceable is.
- **2026-04-28** — Resolved Phase B pre-launch decisions (sectie
  6.1, 6.2). TOTP/PWA hybride strategie + conservatieve
  threshold-cap-tabel vastgelegd; auto-tiering uitgesteld naar
  Phase D. Cross-references in 3.3 en 3.4 wijzen nu naar 6.2 voor
  numerieke waardes; kanaal-keuze blijft in 3.4. Bestaande 6.2
  research-spoor verschoven naar 6.3 (sub-secties hernoemd
  6.3a/b/c). Phase B implementatie kan op concrete waardes
  bouwen.
- **2026-04-20** — v1 publicatie. Volledige security-model spec:
  Part 1 (principes), Part 2 (threat model, 8 scenarios), Part 3
  (target state architecture), Part 4 (migration roadmap, Phases
  A–G), Part 5 (non-decisions), Part 6 (open questions), Part 7
  (cross-references naar audit v26).

---

_Document eind. v1 (2026-04-20). Revisie gepland bij start van
Phase C; eerder bij significante audit-findings of bij discovery van
niet-gemodelleerde threats._
