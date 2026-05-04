# Architecture investigation — candle-close vs tick-based execution

## Executive summary

Reverto today runs three distinct execution models stitched together by
shared state shapes (`PaperState`, `PaperDeal`, `IndicatorEngine`):

- **Backtest** is purely candle-close-driven. It walks the driving
  timeframe candle-by-candle, evaluates indicators on the closes that
  precede the current candle, and uses intra-candle high/low to fill
  TP/SL ([backtest/backtest_engine.py:116-203](../backtest/backtest_engine.py#L116-L203)).
- **Paper / Live** is a tick loop at `poll_interval=10s` ([paper/paper_engine.py:622-624](../paper/paper_engine.py#L622-L624),
  hard-coded by the entrypoints — see [main_paper.py:149](../main_paper.py#L149) and
  [main_live.py:213-224](../main_live.py#L213-L224)). Indicator inputs are
  candle-close-cached per timeframe ([paper/paper_engine.py:862-911](../paper/paper_engine.py#L862-L911)),
  but the *evaluation* of entry/DCA/TP/SL fires on every tick.
- **LiveEngine** inherits PaperEngine wholesale ([live/live_engine.py:68](../live/live_engine.py#L68));
  the only execution-model differences are a clock-skew gate and a
  pending-order reconciler scaffold. There is no candle-close path on
  the live side either.

The key strategic finding is that the engines already agree on
indicator inputs (closed candles only) but disagree on *when* and on
*what price* a check fires. Backtest evaluates once per closed candle
at `candle.close`; paper/live evaluate every 10s at the live tick price.
For TP/SL paper has a wick-simulation cache ([paper/paper_engine.py:913-944](../paper/paper_engine.py#L913-L944))
that pulls the forming candle's high/low at most every 5–30s — narrowing
but not eliminating the gap. DCA placement is dynamic in both paper and
backtest (`if price <= next_dca_price` at [paper/paper_engine.py:1664](../paper/paper_engine.py#L1664)
and [backtest/backtest_engine.py:300](../backtest/backtest_engine.py#L300)). Nothing is
pre-placed on the exchange — the LiveEngine doesn't yet send any real
orders ([live/live_engine.py:280-284](../live/live_engine.py#L280-L284) raises
`NotImplementedError` for non-dry-run).

Three paths are viable. **Path A (pure candle-close)** matches 3Commas /
Gainium and brings backtest-live parity for free, but to keep TP/SL
risk-management latency acceptable on 1h+ timeframes it requires
exchange-side pre-placement of TP/SL/DCA — which is Phase-3 territory.
Estimated 12–20 dev-days, plus an awkward story for ASAP and indicator
TP. **Path B (hybrid, default candle-close, opt-in tick)** gives the
operator a per-bot escape hatch and naturally re-positions ASAP as the
canonical tick-based mode; admin-only first, premium-tier later.
Estimated 6–10 dev-days for the core split + 3–5 dev-days for the
wizard toggle. **Path C (status quo)** ships nothing and pays the cost
in backtest-live drift, scaling tax (every bot still polls on a 10s
loop), and feature-parity stories vs. competitors. ~0 engine
dev-days, but ~2–4 dev-days are still owed for a backtest-vs-paper
parity tool the operator doesn't have today.

## Methodology

Read-only static investigation, performed on 2026-05-04 from the
`chore/architecture-investigation-candle-vs-tick` branch.

- Read in full: [backtest/backtest_engine.py](../backtest/backtest_engine.py) (399 lines),
  [paper/paper_engine.py](../paper/paper_engine.py) (1720 lines, focus on tick loop +
  monitoring around the line-1662 DCA path the operator already
  flagged), [live/live_engine.py](../live/live_engine.py) (322 lines), and
  [live/order_reconciliation.py](../live/order_reconciliation.py) (259 lines).
- Read targeted sections of [strategies/indicator_engine.py](../strategies/indicator_engine.py),
  [strategies/indicators/rsi.py](../strategies/indicators/rsi.py), [config/models.py](../config/models.py),
  [main_paper.py](../main_paper.py), [main_live.py](../main_live.py), and the ASAP
  references in [web/static/app.js](../web/static/app.js).
- Read [scripts/parity_compare.py](../scripts/parity_compare.py) for the existing
  parity tooling (paper vs live-dry, not backtest vs paper).
- Cross-referenced [docs/scaling-audit-2026-05.md](./scaling-audit-2026-05.md) for
  per-tick polling cost context.

No engines were started, no orders were placed, no state was modified.
Effort estimates below are derived from line-of-code counts for the
files that change and from the operator's prior estimates in the
scaling-audit memo (`bot consolidation 5–10 dd`, `Postgres migration
8–12 dd`).

## Part 1: Current state inventory

### 1.1 Backtest engine — execution model

**Candle-close-driven, end-to-end.** [backtest/backtest_engine.py:101-133](../backtest/backtest_engine.py#L101-L133)
iterates the driving timeframe's candles in order; for each candle it
calls `_process_candle` ([backtest/backtest_engine.py:165-203](../backtest/backtest_engine.py#L165-L203))
which:

1. Builds `closes_per_tf` / `highs_per_tf` / `lows_per_tf` containing
   only candles that closed *strictly before* the current candle's
   timestamp ([backtest/backtest_engine.py:135-159](../backtest/backtest_engine.py#L135-L159) — pointer
   walk, O(N) total).
2. Monitors open deals with `_check_tp_sl_intracandle` ([backtest/backtest_engine.py:205-282](../backtest/backtest_engine.py#L205-L282))
   — reads `candle.high` for TP and `candle.low` for SL, falls back to
   `candle.close` for indicator-only exits.
3. Evaluates DCA at `candle.close` ([backtest/backtest_engine.py:284-316](../backtest/backtest_engine.py#L284-L316)).
4. If no deal is open, evaluates entry indicators on the
   "closed-candles-up-to-but-not-including" view ([backtest/backtest_engine.py:191-203](../backtest/backtest_engine.py#L191-L203)).

Warmup is 78 candles ([backtest/backtest_engine.py:114](../backtest/backtest_engine.py#L114)).
End-of-data forces a close at the last candle's close ([backtest/backtest_engine.py:124-131](../backtest/backtest_engine.py#L124-L131)).

There is no notion of "tick" anywhere in the backtest path.

### 1.2 Paper engine — execution model

**Tick loop with candle-close indicator inputs.** [paper/paper_engine.py:606-624](../paper/paper_engine.py#L606-L624)
calls `_tick()` then `time.sleep(self.poll_interval)`; default
`poll_interval=10` ([paper/paper_engine.py:171](../paper/paper_engine.py#L171), set to 10
explicitly by [main_paper.py:149](../main_paper.py#L149)).

Each tick ([paper/paper_engine.py:659-829](../paper/paper_engine.py#L659-L829)) does:

1. Fetch live ticker, prefer `mark_price`, fall back to `last`.
2. Refresh closed-candle cache per required TF — but only when the TTL
   for that TF has expired ([paper/paper_engine.py:862-911](../paper/paper_engine.py#L862-L911)). The
   TTL equals the candle interval, so the 1h bucket is re-fetched at
   most once an hour. Critically the fetch *excludes the currently
   forming candle* ([paper/paper_engine.py:891](../paper/paper_engine.py#L891)).
3. Refresh wick-simulation cache for the bot's timeframe ([paper/paper_engine.py:913-944](../paper/paper_engine.py#L913-L944)) — TTL `clamp(poll_interval*2, 5s, 30s)`.
4. `_monitor_open_deals` ([paper/paper_engine.py:1360-1395](../paper/paper_engine.py#L1360-L1395)):
   per open deal, run `_check_tp` then `_check_sl` then `_check_dca`,
   capped at `MAX_DCA_PER_TICK=1` ([paper/paper_engine.py:81](../paper/paper_engine.py#L81)).
5. `_check_entry` ([paper/paper_engine.py:1229-1250](../paper/paper_engine.py#L1229-L1250)) — only when
   no deal is open and the schedule guard says open.
6. `_write_state` ([paper/paper_engine.py:424-483](../paper/paper_engine.py#L424-L483)) — atomic
   `state.json` write every tick.

So: indicator inputs are candle-close (good). Entry, DCA, TP, SL are
all *evaluated* every 10s against the live tick price. Wick simulation
narrows the TP/SL gap — TP fires when `wick_high >= target` even if the
tick hasn't printed there yet ([paper/paper_engine.py:1432-1437](../paper/paper_engine.py#L1432-L1437)).

### 1.3 Live engine — execution model

**Same tick loop as paper, plus a pre-tick clock-skew gate.**
[live/live_engine.py:163-192](../live/live_engine.py#L163-L192) overrides `_tick` to call
`clock_monitor.check()`; if skew is over tolerance the tick still runs
but with `_paused_by_drawdown=True` flipped, which suppresses entry
and DCA in the inherited path. Beyond that, every order decision is
inherited from PaperEngine.

The only other addition is `_run_reconciliation` ([live/live_engine.py:203-231](../live/live_engine.py#L203-L231))
that runs every `RECONCILE_EVERY_N_TICKS=5` ticks (so every ~50s) — but
in Phase 1 this only surfaces *timeout* states, the actual fetch_order
branch is commented out ([live/order_reconciliation.py:133-147](../live/order_reconciliation.py#L133-L147)).

`_place_market_order` raises `NotImplementedError` for the non-dry-run
branch ([live/live_engine.py:280-284](../live/live_engine.py#L280-L284)). No real orders flow
today.

So in answer to the operator's hypothesis: yes, **live is tick-based,
and structurally identical to paper**. The differences are bookkeeping
(order log, reconciler, clock guard), not execution model.

### 1.4 Indicator engine — calculation model

All indicators consume `closes` (and optionally `highs`/`lows`/`opens`)
that come straight out of `closes_per_tf`. In paper that bucket is
populated from completed candles only ([paper/paper_engine.py:891](../paper/paper_engine.py#L891)),
so every indicator is **closed-candle-based**:

- RSI ([strategies/indicators/rsi.py:12-34](../strategies/indicators/rsi.py#L12-L34)) — pandas EWM on
  the closes list. Last value reflects the last *closed* candle.
- EMA ([strategies/indicators/ema.py](../strategies/indicators/ema.py)) — same.
- MACD ([strategies/indicators/macd.py](../strategies/indicators/macd.py)) — same.
- Bollinger ([strategies/indicators/bollinger.py](../strategies/indicators/bollinger.py)) — same.
- Parabolic SAR / Supertrend / Market Structure / S&R / QFL — all read
  the closed-candle highs/lows/closes that PaperEngine fetched via
  `_fetch_closes_if_needed`.

There is no indicator that consumes the *forming* candle. Wick
simulation is consumed only by `_check_tp` / `_check_sl` directly, not
by the indicator engine.

### 1.5 ASAP indicator — special case

ASAP is **not a real indicator** — it's a short-circuit in the entry
path. [strategies/indicator_engine.py:185-189](../strategies/indicator_engine.py#L185-L189) returns
`(True, {"group_id": 0, "group_name": "ASAP", "indicators": ["ASAP"]})`
the moment any indicator with `type=="ASAP"` appears in a group, and
[strategies/indicator_engine.py:492-493](../strategies/indicator_engine.py#L492-L493) returns `True`
for ASAP inside the per-indicator dispatch.

UI description ([web/static/app.js:4527-4528](../web/static/app.js#L4527-L4528)):

> Opens a deal immediately on the next tick, ignoring all other entry
> conditions.

That description matches paper/live behaviour today — `_check_entry`
runs every 10s, so an ASAP bot opens within 10s of any prior deal
closing. **The investigation could not find a "minute-based" guard
anywhere in the engine code.** If the operator believed ASAP was rate-
limited to 1/min, that belief is not supported by the current
implementation.

The candle-close gap surfaces clearly here:

- **Backtest ASAP**: opens once per closed candle on the bot timeframe
  (1h, 4h, etc). [web/static/app.js:10387](../web/static/app.js#L10387) already notes that
  "ASAP with `take_profit.target_pct ≤ 1%`" depends on same-candle
  re-entry to produce non-trivial deal counts.
- **Paper / Live ASAP**: opens within `poll_interval` (10s) of the
  prior deal closing, regardless of timeframe.

For a 1h-TF, TP=1% strategy this is a roughly 360× difference in deal
frequency between the two environments — easily the biggest
backtest-vs-live divergence in the codebase.

The description text lives ONLY in [web/static/app.js:4527](../web/static/app.js#L4527)
(no separate docs file, no model-level docstring).

### 1.6 Order placement — DCA grid

**Dynamic, runtime, not pre-placed.** Confirmed in both paper and live:

- Paper: [paper/paper_engine.py:1644-1702](../paper/paper_engine.py#L1644-L1702) — the line-1662
  pattern the operator already noticed:
  `next_dca_price = last_order_price * (1 - step / 100)` and
  `if price <= next_dca_price: ... PaperOrder(...)`. The engine creates
  an in-memory order at the moment price crosses the threshold; nothing
  is pre-placed.
- Live: same code path, inherited unchanged. Phase-3 will need to call
  `_place_market_order` from this branch (the override is in
  LiveEngine, but is currently a `NotImplementedError` for real
  orders — [live/live_engine.py:280-284](../live/live_engine.py#L280-L284)).
- Backtest: [backtest/backtest_engine.py:284-316](../backtest/backtest_engine.py#L284-L316) — also
  dynamic, but evaluated at `candle.close` not at tick price.

Step spacing scales with `step_scale ** dca_count`, ladder size scales
with `multiplier ** dca_count` ([paper/paper_engine.py:1659-1666](../paper/paper_engine.py#L1659-L1666)).
A pre-placed grid would need to know all spacing/sizing values up front
— which it does, since `dca.max_orders` and the multipliers are config-
fixed. So a future "pre-place the whole DCA grid as exchange limit
orders" path is mathematically achievable; it just isn't built.

### 1.7 Order placement — TP/SL

**Dynamic, runtime, not pre-placed.** Same story.

- Paper TP: [paper/paper_engine.py:1397-1533](../paper/paper_engine.py#L1397-L1533). Evaluates
  `price >= target_price` (or `wick_high >= target_price` when wick
  simulation is on) every tick. Indicator-based TP groups are
  re-evaluated every tick at [paper/paper_engine.py:1496-1509](../paper/paper_engine.py#L1496-L1509) —
  these inherently *cannot* be pre-placed, they require runtime indicator
  state.
- Paper SL: [paper/paper_engine.py:1535-1642](../paper/paper_engine.py#L1535-L1642). Fixed and
  trailing variants. Trailing peak is updated using wick simulation
  ([paper/paper_engine.py:1582-1596](../paper/paper_engine.py#L1582-L1596)) — also impossible
  to pre-place because the SL line moves.
- Backtest TP/SL: [backtest/backtest_engine.py:205-282](../backtest/backtest_engine.py#L205-L282). Reads
  candle.high / candle.low directly.

There is no per-timeframe configuration for TP/SL evaluation cadence.
Indicator-TP exists as a first-class feature ([config/models.py:128](../config/models.py#L128)
`indicator_groups`), and any strategy that enables it forces a runtime
check.

### 1.8 Multi-timeframe support

Multi-timeframe **at the indicator level, single-TF at the bot level**.

- Per-indicator: [config/models.py:71](../config/models.py#L71)
  `Optional[Literal["15m","30m","1h","2h","4h","12h","1d"]]`. Each
  indicator can override the bot's timeframe.
- Per-bot: [config/models.py:202](../config/models.py#L202) `Literal["15m","1h","4h","1d"]`.
  The wizard restricts the operator to four bot-level TFs (smaller set
  than the indicator-level set — note `30m`/`2h`/`12h` are indicator-
  only).
- The engine already collects every needed TF and refreshes them
  independently with their own TTLs ([strategies/indicator_engine.py:105-110](../strategies/indicator_engine.py#L105-L110),
  [paper/paper_engine.py:874-911](../paper/paper_engine.py#L874-L911)). So a 1h bot with a 4h
  EMA filter and a 15m RSI filter Just Works today.
- Entry timeframe (the candle the deal *opens on* in backtest) is the
  bot timeframe; TP/SL evaluate against the bot timeframe's wick
  candle. Cross-TF TP isn't a feature.

Multi-TF mismatches are common and supported. No strategy templates
exist on disk today ([config/bots/1/](../config/bots/1/) is empty), so the
"how often does TF mismatch happen in practice" question can only be
answered from runtime data; that's an open question (Appendix B).

### 1.9 Backtest-vs-live parity

There is **no formal backtest-vs-live parity tool**. [scripts/parity_compare.py](../scripts/parity_compare.py)
compares paper vs live-dry — both tick-driven with the same engine. The
script's existence implies the operator already knows backtest is the
odd one out, but the gap isn't quantified.

Concrete divergence scenarios from the code:

1. **ASAP frequency** (see 1.5): 360× deal-count difference for 1h-TF
   ASAP+1%-TP.
2. **DCA fill price**: backtest fills DCA at `candle.close`, paper at
   the live tick. For a 1h candle that swings 0.5%, DCA price can
   differ by tens of basis points.
3. **TP/SL fill price**: backtest fills at the candle's high/low,
   capped at the TP/SL line. Paper fills at `wick_high` or live tick
   (whichever crossed first), capped the same way. Outcomes match
   *iff* the wick-simulation cache is fresh — TTL is up to 30s, so a
   sub-30s wick that backtest catches can be missed.
4. **Entry timing**: backtest opens at `candle.close`. Paper opens at
   the first tick after candle close that satisfies the no-open-deal
   gate — typically within 10s of close, but if a deal was open at
   close it doesn't re-enter until that deal closes (which can be
   candles later).
5. **Same-candle re-entry on tight TP**: backtest does it ([web/static/app.js:10387](../web/static/app.js#L10387) comment, and
   [backtest/backtest_engine.py:184-201](../backtest/backtest_engine.py#L184-L201)). Paper also
   does it (entry check follows monitor-open-deals in the same tick).
   So this aspect is actually consistent — but only by coincidence.

### 1.10 Tick frequency in current implementation

`poll_interval=10` is set:

- As default constructor arg ([paper/paper_engine.py:171](../paper/paper_engine.py#L171),
  [live/live_engine.py:86](../live/live_engine.py#L86)).
- Explicitly to `10` in both runner entrypoints ([main_paper.py:149](../main_paper.py#L149),
  [main_live.py:218](../main_live.py#L218)).

The loop is a plain `time.sleep(self.poll_interval)` ([paper/paper_engine.py:624](../paper/paper_engine.py#L624)).
Not WebSocket-driven. Not event-driven. Each bot subprocess is a
constantly-spinning sleep+REST-poll loop. Heartbeat cadence
(`HEARTBEAT_INTERVAL_SEC=10`, [paper/paper_engine.py:41](../paper/paper_engine.py#L41)) is hard-
coded to match.

Cost per bot per tick (rough):

- 1× `get_ticker` REST call.
- 1× wick-candle `get_ohlcv(limit=1)` REST call (when wick sim is on).
- 0–N `get_ohlcv` calls per stale TF bucket (only when TTL expired —
  zero on most ticks for a 1h+ TF).
- 1× atomic state.json write (~33 KiB serialized).
- 1× `state.json`-driven Prometheus metric updates.

Per the scaling-audit numbers ([docs/scaling-audit-2026-05.md:284-289](./scaling-audit-2026-05.md#L284-L289)),
2500 bots at this cadence project to ~85 sustained DB writes/s. The
poll loop itself is the dominant CPU cost long before the engine model
matters for scaling.

## Part 2: The three paths forward

Effort is given as "engine refactor" (changes to `paper_engine.py`,
`live_engine.py`, `backtest_engine.py`), excluding test rewrites and
ops/UI. Test-suite mass-update typically adds 30–50% on top.

### Path A: Pure candle-close-based (refactor to industry standard)

**What changes.** The tick loop in `paper_engine.py` becomes a
candle-close subscriber. Indicator/entry/DCA/TP/SL all evaluate once
per closed candle. The 10-second sleep loop is replaced with either
(a) a per-TF timer that fires shortly after each candle close, or
(b) an event bus where multiple bots share one timer per (exchange,
TF) — see Path A's relationship to the scaling-audit's bot-
consolidation work below.

To keep TP/SL risk-management latency acceptable on 1h+ timeframes,
TP and SL must move to **exchange-side pre-placed orders**. Without
this, a 1h-TF bot's SL only checks at minute 0 of each hour — meaning
a 4% adverse move at minute 5 isn't caught for 55 minutes. That's
unacceptable for a leveraged perp bot.

DCA grid pre-placement is optional but desirable; 3Commas / Gainium do
it. Mathematically possible since `max_orders` × spacing × multiplier
is fully known at deal-open. Adds 2–4 dev-days but unlocks parity with
competitors.

ASAP becomes ill-defined in this path — there is no "next tick" to
fire on. Options: (i) deprecate, (ii) redefine as "open at next candle
close", (iii) keep ASAP as the *only* tick-based escape hatch for
sub-candle entry (which is essentially Path B in disguise). The
operator should decide.

Indicator-TP cannot be pre-placed; it stays as a runtime check that
runs once per candle close on the configured TF.

Trailing SL also can't be pre-placed; the cleanest pattern is the
"chandelier exit" approach — recompute the SL line on each candle
close, cancel the old exchange-side SL, place a new one. Cancel-replace
adds API budget pressure (audit `B-01`, `r1-029`) — non-trivial at
scale.

**Pros.**
- Backtest ≡ Live by construction. parity_compare can be repurposed
  for backtest-vs-live or retired entirely.
- Matches 3Commas / Gainium feature surface and customer expectations.
- The 10s sleep loop disappears, which removes a meaningful chunk of
  per-bot CPU/RAM cost — synergistic with the scaling-audit's bot-
  consolidation refactor.
- Indicator-engine cost drops by ~360× for a 1h bot (1 eval/h vs
  1 eval/10s). Big win at the tier-3+ scaling story.

**Cons.**
- Real-order pre-placement on exchange is Phase-3 territory ([live/live_engine.py:25-29](../live/live_engine.py#L25-L29)).
  Path A pulls Phase 3 forward by a substantial margin.
- ASAP needs a strategic answer.
- Cancel-replace SL on every candle is API-rate-limit-heavy. Bitget
  free-tier limits don't accommodate hundreds of bots doing
  cancel-replace per minute.
- Existing bots with open deals need a migration path — pre-placing
  the SL/DCA grid retroactively against an exchange that already has
  the position is fiddly.

**Effort.** 12–20 dev-days engine + 5–10 dev-days exchange wiring
(Phase-3 work) + 4–6 dev-days test rewrite. Total **~25–35 dev-days
realistic**, and that excludes the operator-facing migration story for
existing paper bots.

**Affected files.** [paper/paper_engine.py](../paper/paper_engine.py) (rewrite of
`start`/`_tick`), [live/live_engine.py](../live/live_engine.py) (real-order placement,
SL/DCA exchange orders), [exchanges/](../exchanges/) (place/cancel limit
orders, OCO support), [backtest/backtest_engine.py](../backtest/backtest_engine.py)
(largely unchanged), [strategies/indicator_engine.py](../strategies/indicator_engine.py) (ASAP
decision), the wizard ([web/static/app.js](../web/static/app.js) — ASAP UX), all
existing tests under [tests/](../tests/).

**Risks.** Trailing-SL latency under cancel-replace; partial-fill
handling on the pre-placed DCA grid (the engine needs to know which
grid slots filled and when); ASAP user-experience regression.

### Path B: Hybrid (default candle-close, opt-in tick-based)

**What changes.** Default execution is candle-close-driven (per Path
A's first half — the 10s sleep is replaced by a candle-close subscriber
for the bot's timeframe). A per-bot setting `execution_mode:
candle_close | tick` flips the engine into the existing tick-loop
behaviour. The toggle starts admin-only; premium-tier later via the
existing user_id-tagged config path.

ASAP becomes the canonical *tick-mode* indicator. A bot with ASAP
configured implicitly switches to tick mode (or refuses to run unless
tick mode is selected — operator's choice). This naturally answers the
"what about ASAP" question that Path A struggles with.

TP/SL on candle-close mode go to exchange pre-placement (same Phase-3
prerequisite as Path A). TP/SL on tick mode keep the runtime check.
This is genuinely more code than Path A or C — both modes coexist —
but the duplication is bounded: the indicator engine, state shape, DB
ledger, and notifier all stay shared.

**Pros.**
- Operator keeps the existing tick model for advanced cases (ASAP,
  scalping, fast SL) without giving up backtest parity for default
  bots.
- Premium-tier monetization story: "tick-based execution" = pro
  feature, candle-close = standard. Aligns with 3Commas / Gainium
  positioning where their non-standard modes are gated.
- Phased migration: ship candle-close as opt-in first, flip the
  default once the operator has confidence, deprecate tick later if
  desired.
- Backtest-vs-live parity is solved for the default case (which will
  be ~95% of bots once the toggle defaults to candle-close).

**Cons.**
- Two engine modes in production = two sets of bugs, two parity
  stories, two test matrices.
- Backtest still mismatches the tick-mode bots — parity_compare
  needs to know the bot's mode and gate its verdicts accordingly.
- The toggle is real surface area: wizard step, validation, UI copy,
  documentation. The operator's mental model has to encode it.
- Same exchange-side pre-placement requirement as Path A for the
  candle-close branch.

**Effort.** 6–10 dev-days for the candle-close branch (less than Path
A because tick-mode falls back to *current* code, not greenfield) + 3–5
dev-days for the wizard toggle and feature-flag plumbing. Phase-3
real-order work (5–10 dd) is still required for the candle-close
branch. Total **~14–25 dev-days realistic**.

**Affected files.** Same as Path A plus:
- A new `BotConfig.execution_mode` field ([config/models.py](../config/models.py)).
- Wizard step ([web/static/app.js](../web/static/app.js) — the wizard already has
  an "advanced settings" expansion pattern, this slots in there).
- `nbBuildBotConfig` payload mapping in the same file.
- Permission gate on the route that accepts the field (admin-only at
  first; admin check exists, see web/routes/auth-related code).

**Risks.** Hidden coupling between modes. ASAP-implicitly-flips-mode
needs careful thinking — operators who configure ASAP in candle-close
mode shouldn't get a silent surprise; they should get a wizard
warning.

### Path C: Status quo (keep tick-based)

**What changes.** Engine code is untouched. The work is:

1. Build a backtest-vs-paper parity tool analogous to
   [scripts/parity_compare.py](../scripts/parity_compare.py) so the gap is at least
   measured and tracked. ~2–3 dev-days.
2. Document the gap honestly in operator-facing copy. ~0.5 dd.
3. Tighten ASAP behaviour — e.g. add a configurable cooldown so
   ASAP+1%-TP doesn't generate 360× the backtested deal count. ~1 dd.

**Pros.**
- Zero engine risk. Today's bot keeps working bit-for-bit.
- Frees engineering capacity for the scaling-audit's higher-leverage
  items (bot consolidation, Postgres migration).
- Phase-3 real-order work can ship without an architecture rewrite
  riding alongside it.

**Cons.**
- Backtest-vs-live drift remains a real liability for any user who
  sees backtest = $X profit but their paper bot does something
  noticeably different. Likely the #1 support-ticket source post-
  signups.
- Per-tick polling cost stays as-is (see scaling-audit Path C's
  implications: every bot spends 10s of CPU sleeping on its own
  subprocess, contributing to the 140 MiB/bot RSS that Tier-3
  bot-consolidation is meant to fix anyway).
- Feature-parity gap with 3Commas / Gainium is not closed.
- Future changes (e.g. event-driven bot consolidation) have to model
  every bot as "tick at 10s" forever.

**Effort.** ~3–5 dev-days for the parity tool + ASAP cooldown + docs.
**~0 engine dev-days.**

## Part 3: Cross-cutting concerns

### 3.1 Backward compatibility

Existing bots store full state in [paper/paper_state.py](../paper/paper_state.py)
serialised to `state.json` ([paper/state_io.py](../paper/state_io.py)). The
schema is versioned ([paper/paper_engine.py:57](../paper/paper_engine.py#L57)
`STATE_SCHEMA_VERSION=2`).

- **Path A**: existing open deals can keep running; the engine restart
  reloads them and from that moment on monitors candle-close. The
  pre-placed exchange orders for *new* deals can ship behind a feature
  flag — but for existing in-flight deals you have a choice: (a) run
  them out under the new candle-close monitor without exchange-side
  protection (acceptable if the deal is paper or if leverage ≤ 1), or
  (b) place protective exchange orders retroactively on engine start.
  Option (b) is non-trivial because the exchange might already have
  the position from a prior dry-run path. Bump `STATE_SCHEMA_VERSION`
  → 3.
- **Path B**: existing bots default to whatever the operator decides
  (recommend candle-close as the new default *for new bots* and tick
  as the default *for existing bots* to avoid silent behaviour change).
  Easier than Path A because tick-mode is just-keep-the-current-code.
- **Path C**: nothing changes. No migration needed.

### 3.2 ASAP indicator implications per path

- **Path A**: ASAP is reinterpreted as "open at next candle close on
  the bot timeframe". Description must change. Tests that assert "ASAP
  triggers next tick" need rewrites.
- **Path B**: ASAP triggers tick-mode. Description should say "ASAP
  enables tick-based execution; this bot opens within `poll_interval`
  seconds". Wizard adds a confirmation when ASAP is added.
- **Path C**: ASAP keeps current behaviour. Recommended addition:
  cooldown setting (e.g. `ASAP min seconds between deals: 60s`) so
  paper-vs-backtest deal-count divergence is bounded.

The ASAP description text lives only in [web/static/app.js:4527](../web/static/app.js#L4527).
There is no docs-side description (no [docs/](./) entry references it).
Code-side, `IndicatorEngine` has the implementation ([strategies/indicator_engine.py:185-189](../strategies/indicator_engine.py#L185-L189),
[strategies/indicator_engine.py:492-493](../strategies/indicator_engine.py#L492-L493)) and
`BotConfig.IndicatorConfig.type` accepts `"ASAP"` purely as a string
([config/models.py:65](../config/models.py#L65)).

### 3.3 Premium / admin toggle considerations per path

The toggle is **trivial in Path B** (it's the entire architectural
premise) and **awkward in Paths A and C** (you'd be retro-fitting a
mode toggle onto an engine that doesn't natively support both).

Independent of which path is chosen, the toggle FEATURE itself costs:

- 1 BotConfig field with strict validation (~30 LOC).
- 1 wizard advanced-settings row + copy (~80 LOC JS).
- 1 admin/premium permission check on the validate-config / save-config
  routes (the user_id-routed save path already exists).
- 1 round of tests covering the field through the round-trip (model →
  YAML → engine).

Total **~1.5 dev-days** for the toggle in isolation.

In Path B that 1.5 dd is part of the path total; in Path A or C it'd
be additive and would only buy a ~useless toggle (Path A has no tick
mode to toggle to; Path C has nothing to toggle from).

### 3.4 Multi-timeframe under candle-close model

Today the engine fetches each TF independently with a per-TF TTL (1.4
above). Under a pure candle-close model the natural shape is:

```
event-bus / scheduler:
   on candle_close(exchange, tf):
      for bot in bots_subscribed_to(exchange, tf):
         bot.evaluate()
```

A 1h bot with a 4h indicator and a 15m indicator subscribes to all
three. The bot evaluates whenever ANY of its TFs closes a candle, not
just the bot's primary TF — otherwise a 1h bot whose entry depends on
a 15m indicator would still only evaluate hourly. (Backtest already
has this property: [backtest/backtest_engine.py:135-159](../backtest/backtest_engine.py#L135-L159) refreshes
all TFs on every driving-candle iteration, but the driving frequency
is the bot's primary TF — which means the backtest currently has the
*same* "primary-TF-only evaluation" property. Operator should be aware
this isn't strictly a regression.)

The scaling-audit's bot-consolidation work ([docs/scaling-audit-2026-05.md:258-265](./scaling-audit-2026-05.md#L258-L265),
"5–10 dev-days") naturally hosts this event bus — one shared scheduler
beats N per-bot timers. So Path A or B *amplifies* the scaling-audit's
already-recommended consolidation by giving it more reason to land.

### 3.5 Backtest parity per path

| Path | Backtest behaviour | Live default | Live opt-in | Parity tool needed? |
|------|--------------------|--------------|-------------|---------------------|
| A    | Candle-close      | Candle-close | —           | No (parity by construction) |
| B    | Candle-close      | Candle-close | Tick (gated)| Yes, but only for tick-mode bots |
| C    | Candle-close      | Tick         | —           | Yes — and it doesn't exist today |

[scripts/parity_compare.py](../scripts/parity_compare.py) compares paper-vs-live (both
tick), so under any path it remains useful for live-vs-paper drift
detection. None of the paths kill it.

## Part 4: Decision framework

### 4.1 Questions for the operator to weigh

1. **Is backtest-live parity a launch blocker for paying signups?** If
   yes → A or B. If no → C is on the table.
2. **How important is feature-parity with 3Commas / Gainium for the
   pitch deck?** Their candle-close model is part of their pitch.
3. **Is "tick-based execution" something the operator wants to charge
   for as a premium feature?** If yes → B is the natural fit.
4. **What's the timeline pressure for Phase 3a (real-order live
   trading)?** Path A pulls Phase 3 forward; Path C lets Phase 3 ship
   first then revisit architecture.
5. **How much risk does the operator carry from existing tick-mode
   paper bots that customers expect to keep working unchanged?**
   Affects migration-story complexity (3.1).
6. **Does the operator want one strategy template that works
   identically across backtest, paper, and live — or are users
   expected to backtest separately and then live-trade with a slightly
   different deal pattern?** A and B converge towards the first; C
   accepts the second.
7. **API budget**: Bitget rate limits at the candle-close cancel-
   replace pace (path A trailing SL) — has the operator measured how
   much headroom is available per user, particularly when bots run
   concurrently? See audit `B-01`, `r1-029`.
8. **Does the BTC-only / DCA-niche positioning weigh towards or
   against pre-placed exchange orders?** Bitget BTC inverse perp
   supports OCO and conditional orders, so technically yes; per-tier
   rate-limit headroom is the constraint.

### 4.2 What should be decided BEFORE writing code

- Which path. (Self-evidently — a, b, or c.)
- What ASAP becomes (deprecate / candle-only / tick-only / mode-flip).
- Exchange-side pre-placement support: a single yes/no decision that
  both A and B depend on; if no, both paths collapse into "monitor
  candle-close, raise SL/TP latency to operator's risk budget".
- Whether existing in-flight deals on paper must run out under the new
  monitor model (recommend yes for Path A — anything else is a much
  bigger compatibility story than the architecture itself).

### 4.3 What can be deferred until after path choice

- DCA grid pre-placement. Always optional in Path A; can ship a release
  later. Adds 2–4 dd but does not block the architecture.
- ASAP cooldown / configurability — Path C's specific item; only
  applies if C is chosen.
- Wizard copy / docs rewrite — fast, can be done after the engine is
  stable on staging.
- Premium-tier gate — Path B's specific item; only applies if B is
  chosen and only after admin gate is shipped first.
- Backtest re-warmup logic. Today's 78-candle warmup is a
  conservative lower bound; the new model doesn't strictly need to
  change it.

## Appendix A: Code references

**Engines.**
- [backtest/backtest_engine.py:101-203](../backtest/backtest_engine.py#L101-L203) — backtest run loop and per-candle process.
- [backtest/backtest_engine.py:205-282](../backtest/backtest_engine.py#L205-L282) — backtest TP/SL intra-candle.
- [backtest/backtest_engine.py:284-316](../backtest/backtest_engine.py#L284-L316) — backtest DCA at candle.close.
- [paper/paper_engine.py:606-624](../paper/paper_engine.py#L606-L624) — paper engine start loop.
- [paper/paper_engine.py:659-829](../paper/paper_engine.py#L659-L829) — paper `_tick`.
- [paper/paper_engine.py:862-911](../paper/paper_engine.py#L862-L911) — closed-candle fetch, per-TF TTL.
- [paper/paper_engine.py:913-944](../paper/paper_engine.py#L913-L944) — wick-simulation cache.
- [paper/paper_engine.py:1229-1250](../paper/paper_engine.py#L1229-L1250) — entry check (every tick).
- [paper/paper_engine.py:1397-1533](../paper/paper_engine.py#L1397-L1533) — TP including indicator-TP groups.
- [paper/paper_engine.py:1535-1642](../paper/paper_engine.py#L1535-L1642) — SL fixed and trailing.
- [paper/paper_engine.py:1644-1702](../paper/paper_engine.py#L1644-L1702) — DCA, the line-1662 dynamic placement.
- [live/live_engine.py:163-192](../live/live_engine.py#L163-L192) — live `_tick` override (clock-skew gate).
- [live/live_engine.py:242-284](../live/live_engine.py#L242-L284) — live `_place_market_order` (NotImplementedError).
- [live/order_reconciliation.py:100-155](../live/order_reconciliation.py#L100-L155) — pending-order reconciler.

**Indicators.**
- [strategies/indicator_engine.py:105-110](../strategies/indicator_engine.py#L105-L110) — `required_timeframes`.
- [strategies/indicator_engine.py:170-207](../strategies/indicator_engine.py#L170-L207) — `check_entry_signal` + ASAP short-circuit.
- [strategies/indicator_engine.py:486-501](../strategies/indicator_engine.py#L486-L501) — `_evaluate_indicator` ASAP=True.

**Config.**
- [config/models.py:71](../config/models.py#L71) — indicator timeframe override.
- [config/models.py:202](../config/models.py#L202) — bot-level timeframe.
- [config/models.py:208](../config/models.py#L208) — `use_wick_simulation` toggle.

**Entry points.**
- [main_paper.py:149](../main_paper.py#L149) — `poll_interval=10` for paper.
- [main_live.py:218](../main_live.py#L218) — `poll_interval=10` for live.

**ASAP UI surface.**
- [web/static/app.js:4527-4528](../web/static/app.js#L4527-L4528) — UI description text.
- [web/static/app.js:4996](../web/static/app.js#L4996) — wizard select option.
- [web/static/app.js:10387](../web/static/app.js#L10387) — backtest re-entry comment about ASAP+TP1%.

**Parity tooling.**
- [scripts/parity_compare.py](../scripts/parity_compare.py) — paper-vs-live-dry compare.

## Appendix B: Open questions

These cannot be definitively answered from code alone:

1. **How many existing bots use ASAP?** No bot YAMLs on disk
   ([config/bots/1/](../config/bots/1/) is empty); the answer requires runtime
   data from production state files. The operator's tolerance for
   ASAP-behaviour change pivots on this.
2. **How many bots use indicator-level multi-TF?** Same — needs runtime
   config inspection. If most bots are single-TF, the multi-TF event
   bus complexity in Path A/B is over-engineering.
3. **How often do backtest results diverge from live in current
   production?** No backtest-vs-live parity tool exists today (Path C's
   2–3 dd item builds it). A measured divergence would dramatically
   change the case for A vs C.
4. **Does Bitget's per-user rate-limit budget allow pre-placed TP/SL +
   cancel-replace on candle-close cadence at, say, 100 concurrent
   bots/user?** This is the single biggest unknown blocking Path A's
   trailing-SL story. Audit `B-01` and `r1-029` already flag the
   centralised-rate-budget gap; quantifying the budget is its own
   investigation.
5. **What's the operator's actual lower bound on TP/SL latency?** Paper
   today: ~10s (poll interval). Backtest: 0s (within the candle).
   Pure candle-close on 1h: up to 60 minutes. The tolerable answer
   depends on max-leverage policy and product positioning.
6. **Does the operator consider "no real orders yet on live" a feature
   or a bug?** ([live/live_engine.py:280-284](../live/live_engine.py#L280-L284)). Path A
   forces an answer; Paths B and C let the answer slide.
7. **Is the Phase-3 work scoped to "support real orders on the existing
   tick model" or "support real orders on whatever model we're
   committing to"?** The README in [live/README.md](../live/README.md) (not read in
   this investigation) and [live/live_engine.py:25-29](../live/live_engine.py#L25-L29) suggest
   the former; this investigation suggests the operator should
   reconsider.
8. **Cumulative-DCA cap behaviour** ([paper/paper_engine.py:1668-1674](../paper/paper_engine.py#L1668-L1674)
   notes there is no config-driven cumulative cap by design). Under
   pre-placed DCA grids the cumulative size is locked at deal-open;
   the lack of a cap matters less. Under runtime DCA placement (today)
   the lack of a cap is a separate operator decision. Confirm that
   this investigation's ASAP/path discussion is independent of the
   cumulative-cap discussion (they share the line-numbers but not the
   concerns).
