# Reverto Threat Model

**Intern referentie-document voor security-beslissingen.**

> Dit document beschrijft het security threat-model van Reverto: welke
> deployment-contexten worden ondersteund, wie wordt vertrouwd, welke
> threats relevant zijn, en hoe severity te bepalen.
>
> Gebruik dit document bij:
> - Findings-review (audit + pentest hercalibratie)
> - Nieuwe pentest-batches (PT-v4 AZ/FS/NW/MK/EI)
> - Architectuur-keuzes met security-impact
>
> Operationele risico's (data-loss, downtime, supply-chain) zitten in
> een apart document `OPERATIONAL_RISKS.md`. Compliance-grenzen
> (MiCA, marketing, aansprakelijkheid) in `COMMERCIAL_BOUNDARIES.md`.

---

## 1. Context en uitgangspunten

### 1.1 Waarom dit document bestaat

Tot mei 2026 was Reverto opgezet als multi-tenant SaaS-platform. Veel
bestaande audit-findings (RHA-v1, v26, v27, PT-v1, PT-v2, PT-v3, PT-v4)
zijn opgesteld onder die aanname. Belangrijke threats waren bijvoorbeeld:

- Tenant-isolation breach (gebruiker A ziet data van gebruiker B)
- Cross-tenant credential leak
- Authorization-bypass tussen tenants
- Abuse door betalende SaaS-klanten

Met de verschuiving naar self-host commercieel (zie
`COMMERCIAL_BOUNDARIES.md` §1.1) zijn veel van deze threats
**fundamenteel anders gewogen of obsoleet**. Een tenant-isolation bug
is niet langer relevant als er per definitie maar één tenant per
installatie is.

Dit document maakt het nieuwe threat-model expliciet zodat:

1. Findings consequent geherclassificeerd kunnen worden
2. Toekomstige audits binnen het juiste kader opereren
3. Architectuur-beslissingen tegen het juiste model getoetst worden

### 1.2 Verhouding tot andere documenten

| Document | Doel |
|---|---|
| `COMMERCIAL_BOUNDARIES.md` | Compliance-grenzen (MiCA, marketing, aansprakelijkheid) |
| `THREAT_MODEL.md` (dit document) | Security threat-model |
| `OPERATIONAL_RISKS.md` (komt later) | Operationele risico's (uptime, backups, supply-chain) |
| `architecture.md` | Technische architectuur, geen security-specifiek |

### 1.3 Wat dit document NIET is

- **Geen volledige risico-analyse** van Reverto als organisatie
- **Geen incident-response runbook** (zie `OPERATIONS.md`)
- **Geen compliance-document** (zie `COMMERCIAL_BOUNDARIES.md`)
- **Geen vervanging voor pentest-rapporten.** Die blijven authoritative
  voor specifieke vulnerabilities

---

## 2. Deployment-contexten

Reverto ondersteunt drie deployment-contexten. Elke context heeft een
ander threat-profiel; dezelfde finding kan in context A kritiek zijn en
in context B obsoleet.

### 2.1 Context A: Lokaal self-host

**Beschrijving:** Reverto draait op de eigen machine van de gebruiker
(thuis-PC, laptop, NAS), bereikbaar via `http://localhost:8080`.
Geen TLS, geen externe blootstelling.

**Aanvalsoppervlak:**
- Localhost-only (geen externe netwerk-bereikbaarheid)
- Mogelijk LAN-blootstelling als de gebruiker dit configureert
- Lokale processen (andere apps op dezelfde machine)
- Browser-context (XSS, CSRF) als gebruiker via lokale browser werkt

**Trust-context:**
- De gebruiker = volledig vertrouwd (eigen machine)
- Andere lokale gebruikers van dezelfde machine = afhankelijk van setup
- Externe internet = niet rechtstreeks bereikbaar tot de Reverto-instance

**Karakteristieke threats (relevant):**
- ⚠️ Browser-context aanvallen (XSS in eigen Reverto UI, CSRF van
  andere tabs)
- ⚠️ Lokale process-isolatie (malware op machine kan credentials
  lezen)
- ⚠️ Filesystem-permissions (andere user accounts op dezelfde machine)
- ⚠️ Supply-chain (kwaadaardige dependency in pip/npm/ccxt)

**Karakteristieke threats (NIET relevant):**
- ❌ Externe netwerk-aanvallers (geen route)
- ❌ DDoS / rate-limiting issues (geen publiek endpoint)
- ❌ TLS-configuratie (geen TLS in gebruik)
- ❌ Tenant-isolation (single-tenant)

### 2.2 Context B: Self-host op publieke VPS

**Beschrijving:** Reverto draait op een publieke VPS (Hetzner,
DigitalOcean, AWS Lightsail), achter een TLS reverse proxy
(Caddy/nginx) op een eigen domein.

**Aanvalsoppervlak:**
- Publiek bereikbaar via HTTPS
- Internet-exposed: brute-force login, automated scanning
- Reverse-proxy laag (Caddy/nginx config)
- VPS-systeem (SSH, OS-updates, andere services)
- Browser-context aanvallen (XSS, CSRF) blijven relevant

**Trust-context:**
- De gebruiker = volledig vertrouwd
- Internet = niet vertrouwd
- VPS-provider = redelijk vertrouwd (storage, networking)
- Reverse-proxy software = vertrouwd mits up-to-date

**Karakteristieke threats (relevant):**
- ⚠️ Externe netwerk-aanvallers (volledig aanvalsoppervlak)
- ⚠️ Brute-force login (Reverto admin-credentials)
- ⚠️ TLS-configuratie en certificate-management
- ⚠️ Browser-context aanvallen (XSS, CSRF)
- ⚠️ Rate-limiting en DoS
- ⚠️ Reverse-proxy mis-configuratie (header-injection, host-bypass)
- ⚠️ Supply-chain
- ⚠️ Credential-stuffing (lekkende wachtwoorden van andere sites)
- ⚠️ Information disclosure via error-pages

**Karakteristieke threats (NIET relevant):**
- ❌ Tenant-isolation (single-tenant)
- ❌ Cross-tenant credential leak
- ❌ Multi-user authorization bugs

### 2.3 Context C: Reverto-eigen productie

**Beschrijving:** De huidige `app.reverto.bot` deployment op de
operator's eigen VPS. Bedoeld voor operator-gebruik (Remy zelf), niet
voor klanten.

**Aanvalsoppervlak:**
- Identiek aan Context B (publieke VPS)
- Plus: bevat operator's eigen trade-data, exchange-credentials, en
  evt. eerdere multi-tenant data
- Plus: kan Reverto's "showcase"-instance zijn (publieke
  reverto.bot-pagina linkt eventueel hierheen)

**Trust-context:**
- Identiek aan Context B
- Plus: hogere impact bij compromittering omdat dit de operator's
  primary trading-instance is

**Karakteristieke threats:**
- Alle threats van Context B
- Plus extra gewicht voor:
  - ⚠️ Credential-leak (operator's exchange-keys = eigen funds-risico)
  - ⚠️ Trade-data leak (operator's strategie-info)
  - ⚠️ Supply-chain (kwaadaardige update kan funds compromitteren)

### 2.4 Context-vergelijking

| Threat-categorie | Context A (lokaal) | Context B (VPS self-host) | Context C (Reverto-eigen) |
|---|---|---|---|
| Externe aanvallers | ❌ | ⚠️ | ⚠️ |
| Brute-force login | ❌ | ⚠️ | ⚠️ |
| TLS/cert-issues | ❌ | ⚠️ | ⚠️ |
| Browser XSS/CSRF | ⚠️ | ⚠️ | ⚠️ |
| Tenant-isolation | ❌ | ❌ | ❌ |
| Cross-user data leak | ❌ | ❌ | ❌ |
| Local process-attack | ⚠️ | ⚠️ (minder waarschijnlijk) | ⚠️ |
| Supply-chain | ⚠️ | ⚠️ | ⚠️⚠️ (hoger gewicht) |
| Credential-leak | ⚠️ | ⚠️ | ⚠️⚠️ (hoger gewicht) |
| Rate-limit/DoS | ❌ | ⚠️ | ⚠️ |

Legenda: ❌ = niet relevant, ⚠️ = relevant, ⚠️⚠️ = verhoogd gewicht.

---

## 3. Trust-grenzen en actoren

### 3.1 Vertrouwde actoren

**De gebruiker / operator.** Volledig vertrouwd. Heeft fysieke of
remote toegang tot de installatie, beheert API keys, beheert
admin-wachtwoord. Deze persoon is altijd "de juiste persoon". Alle
authorizatie-logica gaat ervan uit dat een geslaagde authenticatie =
de gebruiker.

**Reverto-code (eigen codebase).** Vertrouwd zolang de codebase niet
gecompromitteerd is. Code-reviews, lint-checks, en pentests bewaken
deze trust-aanname.

**Dependencies via lockfile.** Vertrouwd na due diligence (review van
significant nieuwe dependencies). Zie supply-chain in sectie 4.

**Reverse-proxy (Caddy/nginx).** Vertrouwd mits correct
geconfigureerd. Security headers, TLS, request-handling zijn de
verantwoordelijkheid van de proxy-laag.

### 3.2 Niet-vertrouwde actoren

**Het internet.** In Context B en C: alle inkomend verkeer dat niet
authenticated is, is potentieel kwaadaardig.

**Niet-authenticated browser-clients.** Pre-login pagina's en API-
endpoints moeten zo min mogelijk informatie lekken.

**Externe webhook-bronnen.** Als webhook-functionaliteit later wordt
toegevoegd: webhook-payloads zijn untrusted input.

**Exchange API-responses.** Treat as untrusted (een gecompromitteerde
exchange of MITM kan misleidende data sturen). Validatie en sanity-
checks zijn vereist.

### 3.3 Grijze actoren

**De gebruiker's browser.** Vertrouwd voor authenticated sessies, maar
moet beschermd worden tegen externe XSS/CSRF. Browser is niet
"compromised" in normaal gebruik, maar wel doelwit van aanvallen.

**De gebruiker's lokale machine (Context A).** Vertrouwd in normale
omstandigheden, maar kan gecompromitteerd raken door malware. Reverto
moet dit niet veronderstellen, maar ook niet expliciet tegen
verdedigen (out of scope).

**Andere processen op dezelfde VPS.** Idealiter geen, maar in praktijk
wel (SSH-daemon, monitoring, backups). Beperk Reverto's blast-radius
door least-privilege (bv. eigen user-account, beperkte file-
permissions).

---

## 4. Threat-categorieën

### 4.1 Authentication & session management

**Relevante threats:**
- Brute-force tegen admin-wachtwoord
- Credential-stuffing
- Session-fixation, session-hijacking
- TOTP-bypass of replay
- Password reset abuse (indien geïmplementeerd)
- Cookie-theft via XSS

**Mitigaties die in scope zijn:**
- Bcrypt password-hashing (rounds=12)
- TOTP 2FA voor admin login
- Session-cookies met `Secure`, `HttpOnly`, `SameSite=Lax`
- Rate-limiting op login-endpoints
- Session-epoch bump bij password-reset / TOTP-reset

### 4.2 Authorization

**Relevante threats:**
- Privilege-escalation binnen single-user context (bv. via
  parameter-tampering)
- Forgotten authorization-checks op admin-endpoints
- IDOR-achtige issues (insecure direct object references) waar
  gebruiker andere data ziet dan bedoeld

**NIET relevant (was wel multi-tenant):**
- Cross-tenant access
- Tenant impersonation
- Per-user data-isolatie

**Mitigaties die in scope zijn:**
- Consistente decorator-based auth-checks
- Geen authorization-logic verspreid over routes
- Defense-in-depth (zelfs als één laag faalt, andere lagen vangen op)

### 4.3 Input validation & injection

**Relevante threats:**
- SQL-injection (sqlite3 met parameterized queries)
- Command-injection in subprocess-calls (bot-spawning)
- Path-traversal in file-operations
- YAML-injection in bot-configs
- HTML-injection / XSS in user-controlled output

**Mitigaties die in scope zijn:**
- Pydantic-validatie op alle API-input
- Geen `shell=True` in subprocess-calls
- Safe YAML-loading (`yaml.safe_load`)
- Output-encoding in templates (Jinja2 autoescape)

### 4.4 Browser-context (XSS, CSRF)

**Relevante threats:**
- Reflected XSS via query-parameters
- Stored XSS via bot-config of finding-text
- CSRF op state-changing endpoints
- Clickjacking (iframe-embedding)

**Mitigaties die in scope zijn:**
- Content Security Policy (CSP) headers
- X-Frame-Options / frame-ancestors
- CSRF-tokens op state-changing endpoints
- SameSite cookies

### 4.5 Cryptografie & secret-management

**Relevante threats:**
- Zwakke encryption van credentials (Fernet key-management)
- Hard-coded secrets in code
- Secrets in logs / error-pages
- Onveilige random-number-generation
- Deprecated algoritmes

**Mitigaties die in scope zijn:**
- Fernet voor credential-encryption
- Per-user encryption-key derivation
- `secrets.token_hex(32)` voor session/API-keys
- Geen secrets in git-history (.env in .gitignore)
- Log-sanitization

### 4.6 Network / TLS (Context B en C)

**Relevante threats:**
- TLS mis-configuratie (oude protocols, zwakke ciphers)
- HTTP-naar-HTTPS redirect missing
- Certificate-renewal failure
- HSTS missing of verkeerd geconfigureerd
- Mixed-content issues

**Mitigaties die in scope zijn:**
- Caddy auto-TLS met Let's Encrypt
- HSTS header (preload-eligible config)
- TLS 1.2+ enforced
- Proper redirect from :80 to :443

### 4.7 Supply-chain

**Relevante threats:**
- Kwaadaardige update in pip-dependency
- Typo-squatting (verkeerd geïnstalleerde package)
- Compromised exchange-API library (ccxt)
- Compromised CDN-asset (frontend libraries)

**Mitigaties die in scope zijn:**
- `requirements.txt` met pinned versions
- Periodic `pip-audit` of vergelijkbaar
- Subresource integrity (SRI) op CDN-assets
- Review van significant nieuwe dependencies
- Vendor / pin frontend-libraries lokaal waar mogelijk

### 4.8 Trade-execution integriteit (KRITIEK)

**Specifiek voor Reverto.** Bug in trade-logica = direct geld-verlies.

**Relevante threats:**
- Verkeerde PnL-berekening (linear vs inverse perpetual mismatch)
- Verkeerde size-eenheid bij order-placement
- Race-conditions in deal-state-updates
- Onbedoelde leverage-toepassing
- Funding-rate berekenfouten
- Liquidation-prijs mis-berekening
- Partial-fills incorrect verwerkt

**Mitigaties die in scope zijn:**
- Class-of-issue regression-tests bij elke fix
- Testnet-validatie voor structurele wijzigingen
- Pre-flight sanity-checks vóór order-submit
- Circuit-breakers (drawdown-limits, error-thresholds)
- Conservatieve defaults (geen leverage tenzij expliciet)

### 4.9 Information disclosure

**Relevante threats:**
- Stack-traces in error-pages
- Verbose API-error-messages die interne structuur lekken
- Debug-modus aan in productie
- Log-bestanden publiek toegankelijk
- Diagnostische endpoints (/healthz, /readyz) lekken te veel

**Mitigaties die in scope zijn:**
- Generic error-pages in productie
- Structured logging zonder secrets
- Debug-mode expliciet uit
- /healthz en /readyz minimal output

---

## 5. Wat is verdwenen of veranderd t.o.v. multi-tenant

### 5.1 Threats die OBSOLEET zijn

Onder single-tenant model zijn de volgende threat-categorieën niet
langer relevant:

- ❌ **Tenant-isolation breaches.** Geen tenants meer
- ❌ **Cross-user data leak.** Eén gebruiker per installatie
- ❌ **Per-user encryption-key isolation** (was relevant voor
  tenant-isolation; nu meer een nice-to-have voor in-place backup-
  hygiene)
- ❌ **Authorization tussen users.** Niet van toepassing
- ❌ **User-impersonation attacks.** Niet van toepassing
- ❌ **Quota / rate-limiting tussen users.** Niet van toepassing
- ❌ **Billing / subscription abuse.** Niet van toepassing (tenzij
  later commercieel met license-server)

### 5.2 Threats die VERANDERD zijn

- 🔄 **Authentication**: was "alle gebruikers veilig", nu "operator
  veilig". Lagere total-impact bij compromittering (één account, niet
  N), maar gelijke individuele impact.
- 🔄 **Authorization**: was "tenant-grenzen handhaven", nu "admin vs.
  niet-admin", en dat is zelfs geen onderscheid meer in single-user
  context. Meeste authz-findings worden lager geprioriteerd.
- 🔄 **Information disclosure**: was "lekt info over andere users",
  nu "lekt info over self of system", minder kritiek tenzij het
  credentials of trade-data raakt.
- 🔄 **Rate-limiting**: was "tegen abuse door betalende klanten", nu
  "tegen externe brute-force". Andere drijfveer, andere mitigatie.

### 5.3 Threats die NIEUW of VERHOOGD GEWICHT hebben

- ⬆️ **Single point of failure**: één installatie = directe impact
  op operator. Geen "andere tenants gaan door". Als Reverto down is,
  is alles down.
- ⬆️ **Supply-chain**: in multi-tenant kon je bv. testen op één tenant
  voor rollout. Nu landt elke update direct bij de gebruiker. Hogere
  zorgvuldigheid bij dependency-updates.
- ⬆️ **Self-update mechanism**: als/wanneer Reverto auto-update
  introduceert, wordt dit een aanvalsvector. Niet relevant nu, wel
  voor toekomstige overweging.
- ⬆️ **License-server (toekomst)**: bij commerciële launch wordt de
  licensing-server een nieuwe attack-surface, gescheiden van de
  Reverto-instance.

---

## 6. Severity-rubric

### 6.1 Algemene rubric

Severity wordt bepaald door **impact × likelihood × context**.

**CRITICAL** (P0, fix immediately, block release):
- Direct geld-verlies mogelijk (trade-execution bugs, credential-leak)
- Remote code execution
- Authentication-bypass die admin-toegang geeft
- Cryptografische zwakte die credentials breekt

**HIGH** (P1, fix before next release):
- Significante credential-blootstelling (bv. logs)
- Stored XSS dat persisteert
- Authorization-bypass binnen single-user context
- TLS-issues die MITM mogelijk maken

**MEDIUM** (P2, fix in normale cyclus):
- Reflected XSS / non-persistent issues
- Information disclosure zonder credentials
- Missing security headers (afhankelijk van impact)
- Rate-limiting gaten zonder concrete exploit-pad

**LOW** (P3, fix when convenient):
- Defense-in-depth verbeteringen
- Verbose error-messages zonder concrete data-leak
- Best-practice deviations zonder concrete threat
- Cosmetische security-issues

**INFO** (P4, document or accept):
- Observation zonder actuele threat
- Theoretical risico in een onbereikbare context
- Architectuur-observaties zonder specifieke fix

### 6.2 Context-modifiers

Severity moet aangepast worden aan de deployment-context:

| Issue-type | Context A (lokaal) | Context B (VPS) | Context C (Reverto-eigen) |
|---|---|---|---|
| Externe XSS in login-flow | LOW–MED | HIGH | HIGH |
| TLS-config issue | INFO | MED–HIGH | MED–HIGH |
| Brute-force op login | LOW | HIGH | HIGH |
| Local file-permission issue | MED | LOW–MED | MED |
| Supply-chain risk | MED | MED | HIGH |
| Tenant-isolation issue | OBSOLEET | OBSOLEET | OBSOLEET |
| Trade-execution bug | CRITICAL | CRITICAL | CRITICAL |
| Information disclosure (system info) | LOW | MED | MED |
| Information disclosure (credentials) | HIGH | CRITICAL | CRITICAL |

### 6.3 Hercalibratie-richtlijnen

Bij het herwegen van bestaande findings:

1. **Multi-tenant findings → meestal naar OBSOLEET** of significant
   lager. Documenteer de reden ("OBSOLEET: assumes multi-tenant context
   that no longer applies in self-host model").
2. **Authentication-findings → meestal gelijk gewogen** (was operator
   bescherming, blijft operator bescherming).
3. **Trade-integriteit findings → behouden of opgewaardeerd** (direct
   geld-verlies blijft kritiek ongeacht context).
4. **Information disclosure → context-afhankelijk** herwegen.
5. **Bij twijfel: omhoog classificeren, niet omlaag**. Een te hoge
   severity kost extra werk dat geen kwaad doet; een te lage kan
   schade veroorzaken.

---

## 7. Anti-patterns bij hercalibratie

Vermijd de volgende denkfouten tijdens findings-review:

**❌ "Alles wat multi-tenant is, kan weg."**
Sommige multi-tenant findings hebben single-user equivalenten. Een
"cross-tenant data leak via cookie X" wordt geen issue meer, maar de
onderliggende cookie-mishandling kan nog steeds een single-user
session-fixation zijn. Lees finding inhoudelijk, niet alleen de label.

**❌ "Lokaal self-host = geen security nodig."**
Browser-context aanvallen werken op localhost. XSS in een lokale
Reverto-instance is nog steeds reëel. Reduceer waar gerechtvaardigd,
elimineer niet.

**❌ "Geen externe blootstelling = geen issue."**
Supply-chain en lokale malware hebben niets te maken met externe
blootstelling. Hou breed perspectief.

**❌ "Severity downgraden om de lijst korter te maken."**
Verleiding richting publicatie/launch. Tegenwicht: bij twijfel omhoog
classificeren. Liever een "extra fix" dan een gemiste threat.

**❌ "Niemand heeft hier ooit iets van gevonden, dus het is OK."**
Afwezigheid van bewijs ≠ bewijs van afwezigheid. Beoordeel op het
threat-model, niet op observed-frequency.

---

## Appendix A: Findings-review template

Per finding tijdens de review-sessie, vul in:

```
Finding ID: [bv. v26-15h]
Original severity: [CRIT/HIGH/MED/LOW/INFO]
Original context: [multi-tenant / generiek / specifiek]

Status onder nieuw threat-model:
[ ] Still valid, same severity
[ ] Still valid, severity changed: [new severity]
[ ] Still valid, but context-modifier applied: [Context A/B/C, new severity]
[ ] OBSOLEET: niet langer van toepassing
[ ] NEW INTERPRETATION: oud probleem, nieuwe lezing

Rationale (verplicht bij elke wijziging):
[1-3 zinnen waarom de classificatie verandert]

Resolution_ref: [branch-naam of finding-status]
Reviewer: [Remy]
Date: [YYYY-MM-DD]
```

### A.1 Voorbeeld-classificaties

**Voorbeeld 1, multi-tenant obsoleet:**

```
Finding ID: r1-059
Original severity: HIGH
Original context: cross-tenant credential leak via shared cache

Status: OBSOLEET
Rationale: Multi-tenant context no longer exists. Single-tenant model
has only one set of credentials, so "cross-tenant leak" is not a
meaningful threat category. The underlying cache-handling code remains
correct for single-tenant context.

Resolution_ref: obsoleet-by-context-shift-2026-05-08
```

**Voorbeeld 2, context-modifier toegepast:**

```
Finding ID: pt-v3-NW-014
Original severity: HIGH (multi-tenant SaaS)
Original context: missing rate-limit on /api/login

Status: Still valid, severity changed to MED
Rationale: Threat shifts from "abuse by malicious tenants" to
"external brute-force attack." Severity adjusted: still relevant in
Context B/C, less critical without abuse-vector. Context A: LOW.

Resolution_ref: severity-adjusted-2026-05-08
```

**Voorbeeld 3, onveranderd:**

```
Finding ID: pt-043
Original severity: CRITICAL
Original context: PnL formule mogelijk linear ipv inverse perpetual

Status: Still valid, same severity
Rationale: Trade-execution integriteit blijft CRITICAL ongeacht
deployment-context. Direct geld-verlies risico.

Resolution_ref: unchanged-2026-05-08
```

---

## Appendix B: Snelle context-detectie checklist

Wanneer onduidelijk is welke context van toepassing is op een finding:

1. **Vereist de threat externe netwerk-toegang?**
   - Ja → niet relevant in Context A; relevant in B + C
   - Nee → mogelijk in alle contexten

2. **Vereist de threat meerdere users?**
   - Ja → OBSOLEET (geen multi-user meer)
   - Nee → herbeoordelen op single-user impact

3. **Is de impact direct geld-verlies?**
   - Ja → CRITICAL of HIGH ongeacht context
   - Nee → context-modifier toepassen

4. **Wordt de threat alleen door een gecompromitteerde gebruiker
   uitgevoerd?**
   - Ja → mogelijk OBSOLEET (gebruiker is vertrouwd in trust-model)
   - Nee → blijft relevant

---

## Document changelog

- 2026-05-08: Initial v1 draft. Capture van threat-model na
  strategische verschuiving naar self-host commercieel model.
  Bedoeld voor findings-review en toekomstige pentest-batches.
