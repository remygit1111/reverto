# Audit v27 Backlog

Verzameling van observaties die tijdens dagelijks werk zijn tegengekomen
maar nog geen volledige audit-ronde waard zijn. Bij volgende audit-sweep
consolideren naar `v27-report.md`.

## Status: COLLECTING

Items:

### B-01 — Rate-limiting architectuur voor multi-bot Bitget calls

Context: op 2026-04-21 09:00 triggerde bot **RSI Paper Test** een Bitget
429 (Too Many Requests) op `fetchTicker`. Mogelijk oorzaak: timeframe-
boundary spike waarbij meerdere bots tegelijk 15m candles opvragen via
`_fetch_closes_if_needed`, bovenop de per-tick `get_ticker` calls.

Huidige staat: ccxt doet per-client rate-limiting (one client per bot),
maar Reverto's bots coördineren niet onderling. Elke bot heeft zijn
eigen `ccxt.bitget` instance met zijn eigen throttle-state; Bitget
ziet de som van alle calls onder één IP / API-key zonder dat Reverto
die som ergens bijhoudt.

Richting: centrale rate-limit-manager die Bitget's limits respecteert
over alle bots heen. Past natuurlijk in de Phase-B-C rond signing-
service introductie — de signing-service is toch al het choke-point
voor alle exchange-calls en kan daar een shared token-bucket of
leaky-bucket bijhouden per (exchange, API-key).

Cross-reference: `docs/security-model.md` Part 3.3 noemt per-exchange
rate-limiting als verdedigingslaag, maar architectuur-detail staat
daar niet uitgewerkt.

Prioriteit: MEDIUM (niet urgent; wel belangrijk voor SaaS waar multi-
tenant load onder één public-IP de kans op 429's schaalt met het
aantal users).

### B-02 — Auto-stop bij persistent tick-error

Context: de persistent-error Telegram-notification uit
`fix/error-reporting-ux` labelt een failure als ⚠️ degraded (transient
exhausted) of ⛔ stopped (non-transient), maar de engine blijft in
beide gevallen gewoon door-retryen. "Bot stopped" in de message is
daarmee een user-facing framing, niet de feitelijke process-state.

Huidige staat: `PaperEngine._tick` increment een consecutive-error
counter en firet één Telegram-message als de threshold wordt geraakt.
`self.running` blijft `True`; de portal ziet de bot als running maar
de Telegram-user wordt verteld "restart via portal". De mismatch is
niet catastrofaal (user neemt gewoon actie) maar wel verwarrend.

Richting: bij persistent-threshold-hit een lifecycle-transition naar
`auto_stopped` state. Kost twee puzzelstukken:
- `self.running = False` + graceful stop() call (idempotentie-check
  nodig zodat SIGTERM-handler niet met eigen stop() gaat racen).
- Portal-zijde: tonen dat een bot `auto_stopped` is i.p.v. `stopped`
  zodat operator het onderscheid kan maken met een bewuste `make stop`.

Cross-reference: `paper/paper_engine.py` → `TICK_ERROR_PERSISTENT_THRESHOLD`
en de gate in de tick-except block. `docs/runbook.md` zou een korte
uitleg moeten krijgen van de `auto_stopped` state zodra dit landt.

Prioriteit: LOW (UX-koek, geen safety-issue — de bot doet in degraded
staat niks kwaadaardigs, alleen ongewenste API-load).

### B-03 — ML pipeline `_persist_results` niet user-scoped

Context: tijdens de v26-18 fix gespot dat 
`ml/nightly_pipeline._persist_results(bot_slug, results)` schrijft 
naar `ml/results_<slug>.json` zonder user_id in de filename. Bij 
multi-tenant met overlappende bot-slugs over users zou dat file-
collisions geven — de ene user overschrijft de ML-results van 
de andere.

Huidige staat: `ml/nightly_pipeline.py:_persist_results` gebruikt 
alleen `bot_slug` in het pad. Phase-3b rollout zal dit raken.

Richting: filename-pattern aanpassen naar `ml/results_<user_id>_
<slug>.json` of `ml/<user_id>/results_<slug>.json` (consistent 
met Phase-2 user-scoped layout in `core/paths.py`).

Cross-reference: v26-18 fixte de config-path laag van dezelfde 
file; _persist_results is de output-laag die hetzelfde patroon 
nodig heeft.

Prioriteit: MEDIUM (niet urgent voor solo-user; Phase-3b blocker).
