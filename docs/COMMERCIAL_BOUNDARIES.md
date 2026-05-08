# Reverto Commercial & Compliance Boundaries

**Intern referentie-document. NIET publiceren.**

> Dit document beschrijft de juridische en commerciële grenzen
> waarbinnen Reverto moet opereren om problemen met MiCA/CASP,
> aansprakelijkheid en marketing-regelgeving te voorkomen. Het is een
> eigen checklist en referentie — geen juridisch advies, geen
> vervanging voor een echte jurist bij commerciële launch.
>
> Bij twijfel over een nieuwe feature, marketing-uiting, of
> architectuur-keuze: raadpleeg dit document. Bij blijvende twijfel:
> raadpleeg een NL fintech/crypto-jurist.

---

## 1. Context en uitgangspunten

### 1.1 Waarom dit document bestaat

In mei 2026 is geconstateerd dat een commercieel multi-tenant SaaS
bot-platform voor EU-gebruikers onder MiCA een vergunningplicht (CASP)
oplevert die voor een solo dev praktisch onhaalbaar is (€50k–150k+
kapitaalvereisten, €10k+ juridisch werk, 3–6 maanden processing-tijd).

De gekozen strategische richting is:

- **Reverto als self-host software** distribueren
- **Optioneel commercieel** via licentie-model (eenmalig of abonnement)
- **MiCA-veilig** door geen CASP-diensten te leveren
- **Niche-positionering**: BTC inverse perpetual DCA voor self-host
  prosumers

### 1.2 Het kernprincipe — software vs. dienst

Het verschil tussen MiCA-veilig en MiCA-vergunningplichtig zit in één
fundamenteel onderscheid:

| Aspect | Software-distributie (veilig) | Dienst aanbieden (CASP) |
|---|---|---|
| Code draait op | Machine van de gebruiker | Server van de aanbieder |
| API keys beheerd door | Gebruiker zelf | Aanbieder |
| Orders geplaatst door | Software van de gebruiker | Server van aanbieder |
| Beslissing tot trade | Gebruiker (via eigen configuratie) | Aanbieder (via signalen of advies) |
| Klantfondsen bij | Exchange (gebruikersaccount) | Aanbieder (custody) |
| Juridisch object | Licentie voor software | Dienstenovereenkomst |

Reverto **moet** in elke rij links blijven. Eén overstap naar rechts in
een willekeurige rij creëert MiCA-exposure.

### 1.3 Het anti-circumvention principe

MiCA Artikel 3 bevat een "form follows substance"-beginsel. Een
toezichthouder kijkt naar de **economische functie** van het product,
niet naar de technische verpakking. Implicaties:

- **Het uitsplitsen van een dienst over meerdere componenten met als
  doel CASP te ontwijken triggert anti-circumvention.** Voorbeeld: een
  server die signalen genereert + een aparte client-side app die
  orders plaatst, kan alsnog als één geïntegreerde dienst worden
  geclassificeerd.

- **De zelftest**: zou de architectuur er hetzelfde uit zien als MiCA
  niet bestond? Als het antwoord "nee" is, is het waarschijnlijk
  ontwijking. Bestaande open source bots (Freqtrade, Hummingbot,
  OctoBot, Gunbot) bestaan in hun huidige vorm los van MiCA — daarom
  is hun architectuur verdedigbaar.

- **Eén ecosysteem onder één merk = één dienst** voor anti-
  circumvention doeleinden. Reverto-app + Reverto-signal-service +
  Reverto-cloud-X worden samen beoordeeld, ook als ze technisch los
  staan.

---

## 2. MiCA/CASP architectuur-grenzen

### 2.1 Verboden architecturen — HARDE LIJN

De volgende constructies zijn ❌ niet toegestaan, omdat ze direct CASP-
exposure creëren:

**❌ Reverto-server die trading-signalen of "buy/sell"-aanbevelingen
naar gebruikers stuurt.**
Ook al gebeurt de uitvoering elders (extensie, app, eigen script). Een
gepersonaliseerde "koop nu BTC"-boodschap = beleggingsadvies onder
MiCA.

**❌ Reverto-server die orders uitvoert namens gebruikers.**
Klassieke CASP-categorie "execution of orders." Niet onderhandelbaar.

**❌ Reverto-server die API keys, klantfondsen, of klant-credentials
opslaat (anders dan bij self-host).**
Custody. Direct CASP.

**❌ Multi-tenant SaaS waar gebruikers inloggen en bots configureren
op infrastructuur van Reverto.**
Dit is precies wat 3Commas doet en waarvoor zij CASP-aanpassingen
moeten maken (transitional period). Voor nieuwe spelers zonder pre-
MiCA legacy: niet veilig.

**❌ Reverto-cloud + Reverto-extensie als gekoppeld systeem.**
Anti-circumvention triggert. Zelfs als de extensie open source en
zelfgehost is: als de signaal-bron Reverto-eigen is, vormt het samen
één dienst.

**❌ Pretrained ML-modellen distribueren die specifieke trade-
aanbevelingen produceren.**
Een "Reverto-trained model" dat zegt "koop nu" is functioneel een
signaal-generator. Advies-territorium.

### 2.2 Toegestane architecturen — VEILIGE BASIS

De volgende constructies zijn ✅ waarschijnlijk veilig (gebaseerd op
precedent: Freqtrade, Hummingbot, OctoBot, Gunbot opereren al jaren in
EU zonder MiCA-issues):

**✅ Self-host software die de gebruiker zelf draait.**
Open source, source-available, of closed source — allemaal veilig
mits gebruiker volledig zelf host.

**✅ Software die externe webhooks ontvangt (bv. van TradingView).**
Mits de webhook-bron extern is en de gebruiker zelf kiest welke bron.
Reverto is dan pure execution-laag op gebruiker's eigen machine.

**✅ Licentie-validatie server.**
Een minimaal servertje dat alleen valideert "is deze license-key
geldig?" raakt geen orders, geen funds, geen advies. Vergelijkbaar
met JetBrains/Adobe license activation. Geen CASP.

**✅ Documentatie en educatieve content.**
Algemene uitleg over indicators, DCA-strategieën, risk management.
Mits niet gepersonaliseerd ("voor jou raden wij aan...") en mits geen
rendementsclaims.

**✅ Webhook-bron-agnostische bot-software.**
De bot werkt met willekeurige signaal-bronnen (TradingView, eigen
scripts, derde-partij services). Geen lock-in op één bron.

### 2.3 Grijze zones — VERMIJDEN ZONDER JURIDISCH ADVIES

De volgende zijn niet automatisch verboden, maar vereisen schriftelijk
advies van een MiCA-jurist voor commerciële launch:

**🟡 Server-side technische analyse die alerts genereert.**
TradingView opereert in deze zone met pre-MiCA legacy. Voor een
nieuwe speler is dit risicovoller. Anti-circumvention kan triggeren
als de alerts effectief signaal-generatie zijn.

**🟡 Curated strategie-bundels of "best practice"-configuraties.**
Pre-configured bot-instellingen die specifiek aangeven wanneer te
kopen/verkopen kunnen als advies worden geclassificeerd. Algemene
educatieve voorbeelden mét disclaimer zijn veiliger.

**🟡 ML-features waar inferentie server-side gebeurt.**
Als de ML-output naar gebruikers gaat als trade-instructie, is het
advies. Veilige variant: ML-inferentie volledig client-side, geen
data-flow naar Reverto-server.

**🟡 Performance-tracking dashboards die rendementen tonen.**
Op zich neutrale data, maar in marketing-context kan het als
rendementsbelofte worden gelezen. Hou tracking strikt eigen-historisch
(wat heeft de bot gedaan), niet projectief (wat zal de bot doen).

### 2.4 Anti-circumvention zelftest

Voor elke nieuwe feature of architectuur-keuze:

1. Zou deze keuze er hetzelfde uit zien als MiCA niet bestond? **Ja**
   → veilig. **Nee** → onderzoek of het ontwijking is.
2. Bestaat dit pattern al in pre-MiCA open source software (Freqtrade,
   Hummingbot, etc.)? **Ja** → precedent in jouw voordeel. **Nee** →
   nieuwe categorie, hogere bewijslast.
3. Als ik dit aan een toezichthouder zou uitleggen, kan ik dan zeggen
   "dit is functioneel het beste ontwerp" zonder te liegen? **Ja** →
   veilig. **Nee** → ontwijking, niet doen.
4. Zou een gebruiker zonder Reverto-account de software ook kunnen
   gebruiken (met willekeurige andere signaal-bron)? **Ja** → veilig.
   **Nee** → gesloten ecosysteem, anti-circumvention risico.

---

## 3. Marketing-grenzen

### 3.1 Toon en framing — VEILIG

Reverto-marketing **moet** beschrijvend en technisch zijn, vergelijkbaar
met hoe Freqtrade, Hummingbot of een Linux-distributie zichzelf
positioneren.

**✅ Toegestane formuleringen:**

- "Open source crypto trading bot for self-hosted use"
- "DCA strategie-engine voor BTC perpetuals"
- "Connects to exchanges via your own API keys"
- "Supports webhooks from TradingView and custom sources"
- "Use at your own risk, this is software not financial advice"
- "Software may contain bugs that result in financial loss. Do not
  use with funds you cannot afford to lose."

### 3.2 Toon en framing — VERBODEN

**❌ Verboden formuleringen:**

- "Reverto helpt je winstgevend traden"
- "Onze ML-strategie levert gemiddeld X% per kwartaal"
- "Het beste platform voor crypto-investeerders"
- "Smart algorithm finds profitable trades"
- "Average user returns of X%"
- "Make money trading crypto with Reverto"
- "Voor jouw situatie raden wij Reverto aan" (gepersonaliseerd advies)
- "Reverto Pro Strategy Bundle — guaranteed entries"

### 3.3 De vuistregel

Hou de toon dichter bij **"een Linux-distributie pitchen"** dan bij
**"een hedge fund pitchen"**. Bij twijfel: zou een toezichthouder dit
als advies of rendementsbelofte kunnen lezen? Zo ja: herformuleren.

### 3.4 Disclaimers — VERPLICHT op publicatie

Bij elke publieke uiting (README, productpagina, marketing-content):

- **Use at your own risk** — software wordt geleverd zonder garanties
- **Not financial advice** — Reverto geeft geen advies
- **Trading involves substantial risk of loss** — algemene risico-
  waarschuwing
- **You are responsible for compliance with applicable laws in your
  jurisdiction** — verschuift compliance-verantwoordelijkheid naar
  gebruiker
- **Software may contain bugs that result in financial loss** —
  expliciete bug-waiver

Deze disclaimers worden prominent geplaatst (README top, productpagina
hero-sectie, NOTICE.md), niet weggestopt op regel 47.

### 3.5 Community-interactie — RICHTLIJNEN

In Discord/GitHub Issues/Reddit:

**✅ Toegestaan:**
- Algemene technische uitleg ("RSI is een momentum oscillator")
- Documentatie-verwijzingen
- Bug-rapportages bespreken
- Configuration-syntax uitleggen

**❌ Vermijden:**
- Gepersonaliseerd advies ("voor jouw situatie zou ik X doen")
- Specifieke trade-aanbevelingen ("nu BTC kopen lijkt me goed")
- Rendementsverwachtingen ("met deze setup kan je X% verwachten")
- Het beoordelen of Reverto "winstgevend" is voor specifieke users

---

## 4. Aansprakelijkheid

### 4.1 Apache 2.0 als basisbescherming

Reverto staat onder Apache License 2.0. Sectie 7 (Disclaimer of
Warranty) en sectie 8 (Limitation of Liability) leveren materiële
bescherming:

- Software wordt geleverd "AS IS" zonder garanties
- Geen aansprakelijkheid voor "direct, indirect, incidental, special,
  exemplary, or consequential damages"

Dit dekt het scenario "user heeft DCA op 1000% leverage gezet en is
geliquideerd" goed.

### 4.2 Beperkingen van Apache 2.0 onder NL recht

Disclaimers zijn geen magisch schild:

- **Opzet of grove schuld kan niet uitgesloten worden** in NL
  (Burgerlijk Wetboek)
- **Disclaimers moeten prominent** zijn — niet weggestopt
- **Consumentenrechten** kunnen sommige clausules ondergraven, maar
  vereisen een consumentrelatie (bij gratis open source: geen relatie)

### 4.3 Aansprakelijkheidsverhoging bij commercieel

Zodra Reverto betaald wordt, ontstaat een commerciële relatie. Dit
verhoogt aansprakelijkheid op meerdere manieren:

- **Contractuele relatie** ontstaat tussen koper en verkoper
- **Consumentenrecht** wordt actief van toepassing bij EU-consumenten
- **"Geen warranty"** is moeilijker vol te houden na geld aannemen
- **Productaansprakelijkheid** kan in scope komen

### 4.4 Mitigaties bij commerciële launch

Voor een commerciële launch zijn de volgende stappen aanbevolen:

1. **EULA opstellen** door NL fintech/crypto-jurist (€500–1000
   éénmalig)
2. **Aansprakelijkheid maximeren tot betaald bedrag** in EULA
3. **Beperkingen op direct/indirect schade** opnemen
4. **Bedrijfsstructuur overwegen** (zie sectie 5)
5. **Bekende issues openlijk documenteren** (CHANGELOG, Known Issues
   in README) — voorkomt "verzwegen defect"-claims
6. **Geen "production-ready" claims** doen voor componenten die nog
   experimenteel zijn (live trading bv.)

### 4.5 Bekende issues — discipline

Een eerlijke "Known Limitations"-lijst in README of CHANGELOG is
juridisch waardevol:

- Wat publiek erkend is, is geen verzwegen defect
- Toont due diligence van de aanbieder
- Beschermt tegen "u wist hiervan en hebt het niet gemeld"-claims

Voorbeeld: bij commerciële launch met live trading, expliciet vermelden
welke testnet-blockers nog open staan en wat de implicaties zijn.

---

## 5. Bedrijfsstructuur

### 5.1 Eenmanszaak (huidige situatie)

Voor de eerste 6–12 maanden of bij omzet onder ~€10k/jaar:

**Voordelen:**
- Lage opzetkosten (KvK-registratie, ~€80)
- Geen jaarrekening-verplichting (alleen IB-aangifte)
- Eenvoudig administratief
- Zelfstandigenaftrek mogelijk bij voldoende uren

**Nadelen:**
- Privé-aansprakelijk voor schulden en claims
- Minder professioneel imago bij grotere klanten

### 5.2 BV (overweging bij groei)

Bij omzet boven ~€20k/jaar of bij verhoogd aansprakelijkheidsrisico:

**Voordelen:**
- Aansprakelijkheid beperkt tot vermogen BV
- Professioneler imago
- Fiscaal interessant bij hogere winst (vennootschapsbelasting +
  dividendbelasting kan onder hoogste IB-schaal uitkomen)

**Nadelen:**
- Oprichtingskosten (~€500 + notaris)
- Jaarrekening-verplichting (~€1500/jaar boekhouder)
- Loonadministratie als DGA
- Minimum DGA-salaris (in 2026: ~€56k)

### 5.3 Wanneer overstappen

**Niet overstappen als:**
- Omzet onder €10k/jaar
- Geen substantieel aansprakelijkheidsrisico
- Reverto blijft hobby/bijverdienste

**Wel overstappen als:**
- Omzet boven €30k/jaar consistent
- Live trading commercieel aangeboden (verhoogd risico)
- Plannen voor groei richting hoofd-inkomen

---

## 6. BTW en belasting

### 6.1 Digitale producten aan EU-consumenten

Verkoop van digitale software aan EU-consumenten valt onder de
**MOSS/OSS-regeling** (One-Stop-Shop):

- BTW heffen op basis van land van de koper
- Aangifte via Belastingdienst OSS-portaal (kwartaal)
- Tarieven verschillen per land (15–27%)

### 6.2 Lemon Squeezy / Paddle als merchant-of-record

**Sterke aanbeveling**: gebruik een platform zoals Lemon Squeezy of
Paddle dat **merchant of record** wordt. Dan:

- Zij innen de BTW per land
- Zij dragen af aan EU-belastingdienst
- Jij ontvangt netto bedrag, geen OSS-aangifte nodig
- Kosten: ~5% + €0.50 per transactie

Voor solo-dev volume rechtvaardigt dit de fee ruimschoots.

### 6.3 Inkomstenbelasting

Bij eenmanszaak: omzet valt in **box 1** (inkomen uit werk).

- Bij voldoende uren (1225+/jaar): zelfstandigenaftrek mogelijk
- Bij weinig uren: bijverdienst, gewoon belast als loon
- Administratie: facturen 7 jaar bewaren, jaaroverzicht maken

---

## 7. Feature-evaluatie checklist

Bij elke nieuwe feature of architectuur-keuze, beantwoord:

### 7.1 MiCA-check

- [ ] Komt deze feature in conflict met sectie 2.1 (Verboden
      architecturen)?
- [ ] Valt deze feature in sectie 2.3 (Grijze zones)? Dan: jurist
      raadplegen.
- [ ] Slaag ik voor de anti-circumvention zelftest (sectie 2.4)?

### 7.2 Marketing-check

- [ ] Kan deze feature gemarket worden zonder rendementsclaims of
      advies-framing?
- [ ] Vereist deze feature disclaimers die nog niet bestaan?

### 7.3 Aansprakelijkheid-check

- [ ] Verhoogt deze feature het risico op user-claims?
- [ ] Is deze feature stabiel genoeg om als "production-ready" te
      labelen, of moet hij als "experimenteel" gepubliceerd?
- [ ] Staan eventuele bekende issues in een Known Limitations-lijst?

### 7.4 Operationeel

- [ ] Kan ik support voor deze feature 6+ maanden volhouden?
- [ ] Vereist deze feature infrastructuur (servers, services) die
      ik moet onderhouden?

---

## 8. Escalatie — wanneer een jurist raadplegen

Niet alle vragen kunnen via dit document worden beantwoord. Raadpleeg
een NL fintech/crypto-jurist (Bird & Bird, Loyens & Loeff, Charco &
Dique, of vergelijkbaar) bij:

- **Voor commerciële launch**: EULA opstellen + bevestiging dat de
  architectuur MiCA-veilig is (€500–2000)
- **Bij twijfel over een feature** in een grijze zone (sectie 2.3)
- **Bij contact van AFM, FIOD, of toezichthouder** (uiteraard)
- **Bij overweging van bedrijfsstructuur-wijziging** (BV, holding,
  buitenlandse entity)
- **Bij internationale expansie** (US, UK, Singapore, andere
  jurisdicties hebben eigen regels)
- **Bij claim van een gebruiker** wegens vermeende schade

Begroting: €200–500 voor een uur consult, €1500–5000 voor een
schriftelijk advies over een specifieke architectuur-vraag. Goed
besteed geld vergeleken met een €5M MiCA-boete.

---

## Document changelog

- 2026-05-08: Initial v1 draft. Capture van strategische beslissing
  (mei 2026) om van multi-tenant SaaS naar self-host commercieel te
  schakelen vanwege MiCA/CASP-onhaalbaarheid voor solo dev. Document
  reflecteert sparring-sessie 2026-05-07/08.
