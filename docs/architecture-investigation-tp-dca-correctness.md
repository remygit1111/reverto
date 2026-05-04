# Investigation — TP correctness with DCA orders, wick simulation, and inverse-perpetual math

## Executive summary

Three things were investigated against the current code on `main`:
(1) whether the TP target moves with the post-DCA average entry price,
(2) whether the wick-simulation TP path uses the correct (current)
target value, and (3) whether the linear-derived TP target interacts
incorrectly with the inverse-perpetual PnL formula (audit finding
**pt-041**).

**The TP target does move with avg entry.** [paper/paper_engine.py:1419-1420](../paper/paper_engine.py#L1419-L1420)
recomputes `target_price = avg * (1 + tp_pct / 100)` from
`deal.avg_entry_price` on every tick, and `avg_entry_price` is a
volume-weighted property over `deal.orders` ([paper/paper_state.py:100-106](../paper/paper_state.py#L100-L106)).
After a DCA fill appends an order to the list, the next tick's TP
check sees the new (lower) average and a correspondingly lower target.
This behaviour is pinned by [tests/test_trading_engine.py:76-84](../tests/test_trading_engine.py#L76-L84)
(`test_tp_uses_avg_entry`). The wick-simulation path uses the
*current* target ([paper/paper_engine.py:1432-1437](../paper/paper_engine.py#L1432-L1437) reads
`target_price` computed two lines earlier), so it does not freeze a
stale value.

**The pt-041 asymmetry is real and small.** The TP target is derived
with linear math (`avg * (1+tp_pct/100)`) but realised PnL is computed
with the post-pt-043 inverse formula
`size * (current - avg) / current * leverage` ([paper/paper_state.py:163-166](../paper/paper_state.py#L163-L166)).
At TP-target-hit, realised `pnl_pct` for a long is
`tp_pct / (1 + tp_pct/100)`, which is ~2.913 % when the operator
configured 3 %. Error scales roughly with `tp_pct²/100` — pt-041's
"Error grows with TP size" matches. Severity for the live-trading
rollout: **HIGH but not launch-blocking standalone**; it's a known
finding the operator already tracks, and the magnitude (≤10 % relative
shortfall for tp_pct ≤ 10%) is bounded.

**The 0.67 % anecdote does not match what the code does today.** A
walk-through of the most plausible scenarios (Part 4) cannot reproduce
0.67 % realised PnL at TP-fire from the current code path with any
reasonable inputs *unless* the configured `tp_pct` was itself ≈ 0.7 %.
The previous-session explanation ("TP wordt berekend op de eerste
entry prijs, maar PnL wordt berekend op de gemiddelde entry prijs
inclusief DCAs") is *the opposite* of what the code does — and even if
it were true, the math doesn't yield 0.67 % from a normal long DCA
ladder; it yields a number *higher* than tp_pct, not lower. Verdict:
**option B** (the anecdote was a misleading prior explanation), with
the small **option C** asymmetry from pt-041 layered on top as a real
but unrelated structural shortfall. **Option A is ruled out by the
code reading.**

A separate finding surfaced as a side effect: short-direction bots
have a TP/SL/DCA logic that is *hardcoded for long*. `_open_deal`
correctly sets `side="short"` based on `config.direction`
([paper/paper_engine.py:1280-1285](../paper/paper_engine.py#L1280-L1285)) and `calculate_pnl`
honours side via the long/short branches ([paper/paper_state.py:163-166](../paper/paper_state.py#L163-L166)),
but `_check_tp` / `_check_sl` / `_check_dca` ignore `deal.side` and use
the long-direction price formulas only. A short bot's "TP at +3 %"
target is `avg * 1.03`, which is a 3 % *adverse* move for a short.
This is independent of the DCA/inverse questions but worth noting as a
related real-money bug — see Part 6.3.

## Methodology

Read-only static analysis on 2026-05-04 from branch
`chore/investigation-tp-dca-correctness`.

Files read:

- [paper/paper_engine.py](../paper/paper_engine.py) — TP/SL/DCA paths, lines
  1397–1702 in particular.
- [paper/paper_state.py](../paper/paper_state.py) — `PaperDeal.avg_entry_price`,
  `calculate_pnl`, `close_deal`.
- [paper/close_handler.py](../paper/close_handler.py) — manual-close PnL path.
- [backtest/backtest_engine.py](../backtest/backtest_engine.py) — TP target derivation
  at line 223, hard-coded `side="long"` at line 337.
- [data/findings_seed.yaml](../data/findings_seed.yaml) — pt-041 (HIGH/open),
  pt-042 (LOW/accepted), pt-043 (HIGH/open in tracker; in fact already
  fixed in code per the 2026-04-28 testnet validation).
- [tests/test_trading_engine.py](../tests/test_trading_engine.py),
  [tests/test_paper_state.py](../tests/test_paper_state.py),
  [tests/test_paper_engine.py](../tests/test_paper_engine.py) — coverage map.

Worked numerical examples were computed by hand using the literal
formulas in code; no engine was run.

## Part 1: TP target calculation

### 1.1 Initial TP target — first entry only

The TP target is **not** captured at deal-open time. Inspection of
`_open_deal` ([paper/paper_engine.py:1256-1325](../paper/paper_engine.py#L1256-L1325)) shows the
deal is constructed with the base order, side, leverage, and entry
trigger, but no `target_price` field is stored on the deal. There is
no `tp_target` or similar on `PaperDeal` either ([paper/paper_state.py:22-93](../paper/paper_state.py#L22-L93)).

The target is **derived per tick** inside `_check_tp` from the deal's
current `avg_entry_price`:

```python
# paper/paper_engine.py:1419-1420
avg          = deal.avg_entry_price
target_price = avg * (1 + tp_pct / 100)
```

`avg_entry_price` is a property over `deal.orders` ([paper/paper_state.py:100-106](../paper/paper_state.py#L100-L106)):

```python
@property
def avg_entry_price(self) -> float:
    if not self.orders:
        return 0.0
    total_value = sum(o.price * o.size for o in self.orders)
    return total_value / self.total_size
```

So the formula is volume-weighted across **all** orders (base + every
DCA fill). The math used to derive the price target is **linear**: a
3% TP target sits at `avg * 1.03`. That choice — linear-style price
math on an inverse-perp instrument — is exactly what audit finding
**pt-041** flags ([data/findings_seed.yaml:2224-2233](../data/findings_seed.yaml#L2224-L2233)).

The same shape exists in [backtest/backtest_engine.py:223](../backtest/backtest_engine.py#L223):
`tp_price = avg * (1 + tp_config.target_pct / 100)`.

### 1.2 TP recalculation after DCA fills

There is **no explicit "recompute TP" step**. Instead, TP is implicitly
re-derived every tick because:

1. `_check_dca` appends a new `PaperOrder` to `deal.orders`
   ([paper/paper_engine.py:1676-1683](../paper/paper_engine.py#L1676-L1683)).
2. The next tick's `_check_tp` re-reads `deal.avg_entry_price`
   ([paper/paper_engine.py:1419](../paper/paper_engine.py#L1419)), which is a property that
   re-runs the volume-weighted sum every call.
3. The new lower average produces a new lower `target_price`
   ([paper/paper_engine.py:1420](../paper/paper_engine.py#L1420)).

Within a single tick, the order is `_check_tp` first, then `_check_sl`,
then `_check_dca` ([paper/paper_engine.py:1381-1395](../paper/paper_engine.py#L1381-L1395)). So a DCA
that fires on tick N is reflected in the TP target as of tick N+1 (the
*next* tick), never on the same tick. With a 10 s `poll_interval`
([paper/paper_engine.py:171](../paper/paper_engine.py#L171), [main_paper.py:149](../main_paper.py#L149)),
that's a ~10 s window where the old (higher) target is still active —
which is the conservative direction (TP harder to hit) so no spurious
fires.

### 1.3 What the operator intended

Operator intent: "TP moves with avg entry; TP-hit yields ~configured
% PnL". The code matches the **first** half of that intent. It does
**not** match the second half — see Part 3 below — but the gap there
is the linear-vs-inverse asymmetry, not a TP-tracking bug.

### 1.4 Industry-standard comparison

Cannot verify directly from a code-only investigation. The Reverto
codebase carries no comments / wizard text that explicitly cite 3Commas
or Gainium TP semantics. The closest internal signal is the test
[tests/test_trading_engine.py:76-84](../tests/test_trading_engine.py#L76-L84) — its existence and
docstring shape suggests the avg-entry-following behaviour was
deliberate, but no comment cites a competitor as the reference.

## Part 2: Wick simulation and TP triggering

### 2.1 Wick simulation mechanism

Two layers — engine-level forming-candle cache and per-deal
since-open trackers.

**Forming-candle cache** ([paper/paper_engine.py:913-944](../paper/paper_engine.py#L913-L944)): pulls
the most recent (forming) candle's `(high, low, close)` from
`exchange.get_ohlcv(..., limit=1)`. Cached per timeframe under
`self._wick_candle[tf]`. TTL is
`clamp(poll_interval * 2, WICK_TTL_MIN_S=5, WICK_TTL_MAX_S=30)` —
5–30 seconds in practice ([paper/paper_engine.py:92-93](../paper/paper_engine.py#L92-L93)).
Disabled when `config.use_wick_simulation=False` (default `True`,
[config/models.py:208](../config/models.py#L208)).

**Per-deal since-open trackers** ([paper/paper_state.py:81-93](../paper/paper_state.py#L81-L93),
[paper/paper_engine.py:1330-1358](../paper/paper_engine.py#L1330-L1358)):
`deal._wick_high_since_open` and `_wick_low_since_open` start at
`avg_entry_price` on deal-open and are folded forward every tick by
`_update_deal_wick_trackers`. These are the values `_check_tp`
actually compares against ([paper/paper_engine.py:1432-1436](../paper/paper_engine.py#L1432-L1436)):

```python
# paper/paper_engine.py:1432-1437
wick_high = max(deal._wick_high_since_open, price)
wick_hit = (
    getattr(self.config, "use_wick_simulation", True)
    and wick_high >= target_price
)
tick_hit = price >= target_price
```

The since-open tracker is the post-fix path that closed the
"rapid-fire TP" regression where a deal opened mid-candle inherited
the candle's pre-existing high — see [tests/test_trading_engine.py:356-368](../tests/test_trading_engine.py#L356-L368)
for the regression test.

### 2.2 TP firing on wick high

`_check_tp` reads the freshly-derived `target_price` (Part 1.1) on the
same tick it reads the wick high. Both come from local variables
computed two lines apart — there is no caching of `target_price`
across ticks. So when a DCA fills on tick N, on tick N+1 the
comparison is between the **new** lower target and the **current**
since-open wick high.

If `wick_hit and not tick_hit`, the fill is capped at `target_price`
([paper/paper_engine.py:1462](../paper/paper_engine.py#L1462)) — explicitly to avoid simulating
slippage past the TP line. Realised PnL is then computed on
`fill_price` via `deal.calculate_pnl(fill_price)` ([paper/paper_engine.py:1463](../paper/paper_engine.py#L1463)).

### 2.3 Edge cases

1. **DCA + wick-high in the same tick.** Order in `_monitor_open_deals`
   is `_check_tp` → `_check_sl` → `_check_dca` ([paper/paper_engine.py:1381-1395](../paper/paper_engine.py#L1381-L1395)).
   `_update_deal_wick_trackers` runs first
   ([paper/paper_engine.py:1377](../paper/paper_engine.py#L1377)). So if on the same tick the
   price has both wicked above the OLD target *and* fallen back below
   to where DCA wants to fire — the engine fires TP first (using the
   pre-DCA avg) and never reaches the DCA branch. This is consistent
   with operator intent: TP wins over DCA.

2. **Stale target after DCA.** No staleness possible — `target_price`
   is recomputed every tick. The only "staleness" is the within-tick
   snapshot: a DCA that fills on this tick's `_check_dca` doesn't
   update the TP target until the next tick (see 1.2). Conservative
   direction.

3. **Wick high observed BEFORE deal opened.** Cannot trigger because
   `_wick_high_since_open` seeds at `avg_entry_price` and only rises
   on ticks observed *after* deal open. Pinned by
   [tests/test_trading_engine.py:356-368](../tests/test_trading_engine.py#L356-L368).

4. **Wick high observed BEFORE the most recent DCA fill.** The
   tracker is per-deal not per-leg, so a wick high from before the
   DCA fill stays in `_wick_high_since_open` and is compared against
   the **new** lower target on subsequent ticks. This *can* fire TP
   on a wick that pre-dates the DCA but post-dates deal-open. Whether
   that is correct or surprising depends on operator intent: if
   "TP is a level that, once touched after deal open, fires", this
   is correct. If operator intent is "TP is a level that, once
   touched after the *latest* avg-entry change, fires", this is a
   minor bug. Open question — see Appendix B.

## Part 3: Inverse-perpetual math interaction (pt-041)

### 3.1 How TP target price is derived from TP percentage

Linear-style:

```python
# paper/paper_engine.py:1420
target_price = avg * (1 + tp_pct / 100)
```

For a 3 % TP on a long with avg=$60,000, `target_price = $61,800`.
This is the formula a *linear* perpetual (USDT-quoted) position would
use, where `pnl_pct = (exit/entry − 1) × 100`. On an inverse perpetual
the corresponding "+3 % PnL" exit price is *not* `entry × 1.03`.

There is no `is_inverse` flag, no contract-type branch, no per-side
treatment. Both backtest ([backtest/backtest_engine.py:223](../backtest/backtest_engine.py#L223)) and paper
([paper/paper_engine.py:1420](../paper/paper_engine.py#L1420)) carry the same shape.

### 3.2 PnL calculation at TP fire

`PaperDeal.calculate_pnl(current_price)` ([paper/paper_state.py:113-172](../paper/paper_state.py#L113-L172))
uses the **inverse-perpetual** formula post-pt-043 fix:

```python
# paper/paper_state.py:163-166
if self.side == "long":
    pnl_btc = size * (current_price - avg) / current_price * self.leverage
else:
    pnl_btc = size * (avg - current_price) / current_price * self.leverage
```

This was validated against Bitget testnet on 2026-04-28 (LONG and
SHORT, 1× leverage, 0.1 BTC, ~0.06 % match) — see the docstring at
[paper/paper_state.py:113-145](../paper/paper_state.py#L113-L145) and the regression tests at
[tests/test_paper_state.py:103-145](../tests/test_paper_state.py#L103-L145). pt-043 is listed as
`open` in [data/findings_seed.yaml:2244-2252](../data/findings_seed.yaml#L2244-L2252) but the code
docstring asserts the fix is in place; the tracker entry appears
stale.

### 3.3 Symmetric or asymmetric?

**Asymmetric.** The TP target is derived via the linear formula and
PnL is realised via the inverse formula. The asymmetry is the
substance of pt-041.

For a long at TP-fire (`current_price = target_price = avg × (1 + p)`
where `p = tp_pct / 100`):

```
pnl_btc       = size × (avg(1+p) - avg) / (avg(1+p)) × leverage
              = size × p / (1+p) × leverage
margin        = size / leverage
realised_pct  = pnl_btc / margin × 100 = leverage² × p / (1+p) × 100
```

At leverage = 1, `realised_pct = p / (1+p) × 100 = tp_pct / (1 + tp_pct/100)`.

Numerically:

| tp_pct | realised_pct (long, 1×) | absolute shortfall | relative shortfall |
|--------|-------------------------|--------------------|--------------------|
| 1 %    | 0.99010 %              | 0.00990 pp         | 0.99 %             |
| 3 %    | 2.91262 %              | 0.08738 pp         | 2.91 %             |
| 5 %    | 4.76190 %              | 0.23810 pp         | 4.76 %             |
| 10 %   | 9.09091 %              | 0.90909 pp         | 9.09 %             |
| 20 %   | 16.66667 %             | 3.33333 pp         | 16.67 %            |

Shortfall scales ~quadratically with `tp_pct` — matches pt-041's
"Error grows with TP size" description.

For a **short** at TP-fire — *if the engine actually treated shorts
correctly* — the target would be `avg × (1 - p)`. PnL:

```
pnl_btc       = size × (avg - avg(1-p)) / (avg(1-p)) × leverage
              = size × p / (1-p) × leverage
realised_pct  = leverage² × p / (1-p) × 100
```

At leverage=1, 3 %: `realised_pct = 0.03 / 0.97 × 100 = 3.0928 %`. So
shorts would *overshoot* tp_pct symmetrically. **This branch is
unreachable today** because `_check_tp` doesn't honour `deal.side` —
see Part 6.3.

### 3.4 Magnitude of the bug — worked example

**Inputs.** Long, BTC/USD inverse perp, leverage 1×, base order
0.001 BTC at $63,000, one DCA fill of equal size at $61,000, TP
configured at +3 %.

**Step 1 — avg after DCA.** Sizes equal so unweighted average is
exact:

```
avg_entry = (63000 × 0.001 + 61000 × 0.001) / 0.002 = $62,000.000
```

**Step 2 — TP target as the code computes it (linear-style).**

```
target_price = 62000 × 1.03 = $63,860.000
```

**Step 3 — TP target a "true inverse" derivation would compute.**
For an inverse-perp +3 % gain, you need
`(target − avg) / target = 0.03`, i.e.
`target = avg / (1 − 0.03) = 62000 / 0.97 = $63,917.526`.

So today's code TP target is **~$57.50 lower** than the true-inverse
+3 % target on this scenario. The trader cashes out earlier than
intended.

**Step 4 — Realised PnL when the code's target is hit.**
fill_price = $63,860.

```
pnl_btc = 0.002 × (63860 − 62000) / 63860 × 1
        = 0.002 × 1860 / 63860
        = 0.002 × 0.029128…
        = 5.8259e-5  BTC
margin  = 0.002 / 1 = 0.002 BTC
pnl_pct = 5.8259e-5 / 0.002 × 100 = 2.9126 %
```

**Step 5 — Realised PnL if a hypothetical "inverse-aware" target had
been used.** fill_price = $63,917.526.

```
pnl_btc = 0.002 × (63917.526 − 62000) / 63917.526 × 1
        = 0.002 × 1917.526 / 63917.526
        = 0.002 × 0.030000…
        = 6.0000e-5  BTC
pnl_pct = 6.0000e-5 / 0.002 × 100 = 3.0000 %
```

**Step 6 — Linear-math shape (the pre-pt-043 PnL formula, for
reference only).** With the linear PnL formula
`pnl_btc = size × (current − avg) / avg × leverage`, the realised
PnL at the linear-derived target *would* have been exactly +3 %.
That is what pt-043 noted, what the 2026-04-28 fix corrected, and
what creates pt-041 as a side-effect: the target stayed linear, the
PnL went inverse, and the asymmetry remained.

**Conclusion of the worked example.** With the current code, a
3 % TP on this scenario yields ~2.913 % realised PnL, not 3 %. Not
0.67 %. This is the magnitude pt-041 calls out.

## Part 4: The "0.67 % PnL at TP hit" anecdote

### 4.1 Can current code reproduce that scenario?

Walking the most plausible scenarios:

**S1 — Long, no DCA, tp_pct=3 %.** target = avg × 1.03.
realised_pct = 2.913 %. **Not 0.67 %.**

**S2 — Long, 1 DCA at -3.17 %, tp_pct=3 %.** Worked example above.
realised_pct = 2.913 %. **Not 0.67 %.**

**S3 — Long, 5 DCAs deep (very low avg), tp_pct=3 %.** avg drops, but
realised_pct depends only on `tp_pct/(1+tp_pct/100)`, not on avg.
Still 2.913 %. **Not 0.67 %.**

**S4 — Long, no DCA, tp_pct=0.7 %.** target = avg × 1.007.
realised_pct = 0.7 / 1.007 = **0.6951 %.** Closest match.

**S5 — Hypothetical world where TP target *were* fixed at first
entry.** Operator anecdote's stated explanation. With base $63,000,
DCA at $61,000, target_first = 63000 × 1.03 = $64,890. Realised PnL
at $64,890 with avg $62,000 (inverse): (64890−62000)/64890 = **4.45 %.**
*Higher* than 3 %, not lower. Anecdote's stated explanation
contradicts itself even within its own assumptions.

**S6 — Short bot with broken-TP code.** Short at $63,000, "TP at +3 %"
fires at price = $63,000 × 1.03 = $64,890 (wrong direction).
realised inverse PnL for short: (avg − current)/current = (63000 −
64890)/64890 = **−2.91 %.** A 2.91 % *loss*. Not +0.67 %.

**S7 — Pre-pt-043-fix, linear PnL, tp_pct=3 %.** realised_pct = 3.0 %
exactly (formulas symmetric). **Not 0.67 %.**

The only scenario that yields ~0.67 % is **S4** — operator had
configured a tight TP. In that case the anecdote's *symptom* is
correct (the realised PnL really is ~0.7 %) but the *explanation*
("TP fixed on first entry, PnL on avg") is wrong; the realised value
emerges from the linear-vs-inverse asymmetry alone, applied to a
small `tp_pct`.

### 4.2 Bug, misunderstanding, or pt-041 symptom?

| Option | Description | Verdict |
|--------|-------------|---------|
| A | Code freezes TP at first entry; PnL uses avg → mismatch. | **Ruled out.** Code recomputes target every tick from `avg_entry_price` ([paper/paper_engine.py:1419-1420](../paper/paper_engine.py#L1419-L1420)). Pinned by `test_tp_uses_avg_entry`. |
| B | Code does what operator wants; anecdote was a misleading prior explanation. | **Most likely root cause.** The avg-entry-following is in code; the previous explanation contradicts both the code and its own internal math (S5). |
| C | pt-041's linear-vs-inverse asymmetry creates the divergence. | **Real but smaller.** ~2.9 % shortfall at tp_pct=3 %; ~0.7 % shortfall at tp_pct=0.7 %. Plausibly explains the *symptom* of S4 if `tp_pct` was indeed ~0.7 %. |

**Most likely composite verdict: B, with C as a structural overlay.**
Without runtime data (the actual deal's `tp_pct` config and order
history), the investigation cannot definitively distinguish "tp_pct
was 0.7 % and pt-041 produced 0.69 %" from "the prior explanation was
wrong about everything". Recommend the operator check the bot's saved
config or the deal record's `tp_pct` to nail it down — see Appendix B.

## Part 5: Test coverage assessment

### 5.1 Existing tests for TP with DCAs

- `test_tp_uses_avg_entry` ([tests/test_trading_engine.py:76-84](../tests/test_trading_engine.py#L76-L84))
  — pins that target = `avg_entry_price × 1.03` after a DCA. Asserts
  fire/no-fire at target and target-1.
- `test_tp_fires_at_target` / `test_tp_no_fire_below_target` /
  `test_tp_pnl_positive` ([tests/test_trading_engine.py:50-67](../tests/test_trading_engine.py#L50-L67))
  — base-order-only TP tests.
- `TestWickSimulation` class ([tests/test_trading_engine.py:243-321](../tests/test_trading_engine.py#L243-L321))
  — wick-high triggering TP, wick disabled fallback, normal tick path.
- `TestPerDealWickTracking` class ([tests/test_trading_engine.py:324-440](../tests/test_trading_engine.py#L324-L440))
  — pre-existing-wick regression coverage.
- `TestCalculatePnl` and `TestCalculatePnlInversePerpetual`
  ([tests/test_paper_state.py:50-180](../tests/test_paper_state.py#L50-L180)) — inverse PnL
  formula, leverage, long-short asymmetry, Bitget-testnet anchored
  values for both LONG and SHORT.

### 5.2 Coverage gaps

1. **No test asserts realised PnL ≈ tp_pct after TP fires.** The
   pt-041 asymmetry is unobserved by the test suite. A test like:

   ```python
   def test_realised_pnl_approximately_matches_tp_pct():
       e = _engine(tp_pct=3.0)
       d = _deal(80000.0)
       e.state.open_deal(d)
       e._check_tp(d, 80000.0 * 1.03)
       closed = e.state.closed_deals[0]
       # Expectation: realised_pct ≈ 3.0; current code produces 2.913.
       assert abs(closed.pnl_pct - 3.0) < 0.001
   ```

   would currently *fail*, capturing pt-041 as a regression test.

2. **No SHORT-direction TP/SL/DCA test.** `TestSideFromDirection`
   ([tests/test_paper_engine.py:269-285](../tests/test_paper_engine.py#L269-L285)) verifies a short
   bot opens with `side="short"` but does not exercise `_check_tp`,
   `_check_sl`, or `_check_dca` on a short deal. The hardcoded-long
   formulas inside those checks are untested for short bots.

3. **No test for "TP fires at the new (post-DCA) target on the tick
   immediately after the DCA fill"**. `test_tp_uses_avg_entry`
   manually appends an order to `deal.orders` rather than driving
   `_check_dca`. A more realistic test would:
   - drive `_check_dca(d, low_price)` so a DCA fills naturally,
   - then drive `_check_tp(d, new_target_price)` and assert close.

4. **No backtest-level test** that asserts realised PnL ≈ tp_pct
   either, so the same pt-041 shortfall lives in backtest results
   uncaught.

5. **No pt-041 explicit regression**. The finding is HIGH/open in
   [data/findings_seed.yaml:2224-2233](../data/findings_seed.yaml#L2224-L2233) but no test pins the
   current behaviour either way; once the fix lands, no test would
   catch a revert.

## Part 6: Conclusions and recommendations

### 6.1 Summary of findings

1. TP target tracks `avg_entry_price` correctly via property recompute
   on every tick — the DCA-then-TP semantic the operator wanted is
   already in code, pinned by one test.
2. Wick simulation uses the current target value; per-deal since-open
   tracking prevents pre-deal-open wicks from triggering — this is
   already a regression-tested invariant.
3. **TP-derivation uses linear price math, PnL uses inverse-perp
   math** ⇒ realised PnL at TP-fire is `tp_pct / (1 + tp_pct/100)` for
   a long. This is pt-041 (HIGH/open). Magnitude grows ~quadratically
   with `tp_pct`; ≤10 % relative for `tp_pct ≤ 10 %`.
4. **Short bots have hardcoded long-direction TP/SL/DCA price math**
   in `_check_tp` / `_check_sl` / `_check_dca`. Side-aware deal-open
   exists (and is tested), but the exit logic is broken for shorts.
   This is *not* pt-041 — it's a separate, unfiled real-money bug.
5. The "0.67 % at TP-hit" anecdote does not match the current code
   path under any reasonable scenario except `tp_pct ≈ 0.7 %`. The
   prior explanation ("TP on first entry, PnL on avg") is contradicted
   by both the code and its own arithmetic (a long-DCA scenario with
   that bug would yield realised PnL *higher* than `tp_pct`, not
   lower).

### 6.2 Severity for live-trading rollout

- **TP-tracks-avg-entry behaviour**: ✅ correct, already shipped.
  No blocker.
- **pt-041 (HIGH/open)**: not a launch-*blocker* on its own — the
  shortfall is bounded, predictable, and the operator already tracks
  it. But it is a launch-*hygiene* item: every promotion the operator
  pitches as "TP at +X %" actually delivers `X / (1 + X/100)` for
  longs and `X / (1 − X/100)` for shorts (once short bots work). For
  small `X` (≤3 %) the gap is ≤3 % relative; for large `X` (≥10 %)
  the gap is ≥9 %. For BTC inverse perps where 3–5 % TP is typical,
  call it a 3–5 % under-delivery — noticeable but not catastrophic.
- **Short-direction TP/SL/DCA hardcoded long**: launch-blocker for
  short bots specifically. Would silently lose money in production
  the first time a short bot's "TP at +3 %" fires on a 3 %
  *adverse* move. Either ship a short-bot-disabled-in-live gate or
  fix the formulas before short bots are reachable in live mode.
- **Anecdote**: not a code defect by itself. Worth correcting in any
  operator-facing copy / wizard help text so the operator's mental
  model lines up with the code.

### 6.3 Suggested follow-up actions

In priority order:

1. **Verify on the actual production deal**: pull the deal's
   `tp_pct` config and recorded `pnl_pct` for the 0.67 % event from
   `core.deal_store.get_deals` and confirm whether it was a
   `tp_pct≈0.7 %` deal (option B with C overlay) or something
   genuinely surprising. ~0.25 dev-day. **Do this before any code
   change** — without it the rest is speculation.
2. **Fix the short-direction exit logic** (or gate short bots out
   of live mode until fixed). `_check_tp` / `_check_sl` / `_check_dca`
   need to branch on `deal.side` and use `(1 - p)` / `(1 + p)` /
   `(1 + p)` respectively for shorts. Bonus: backtest_engine line
   337 hardcodes `side="long"`, which makes short bots un-backtestable
   today — same fix scope. ~1–1.5 dev-days including a SHORT-direction
   test class mirroring `TestDCA`/`TestFixedStopLoss`/`TestWickSimulation`.
3. **Add a regression test for pt-041**: the failing test in 5.2.1
   above. Even if the fix isn't shipped immediately, the test
   documents the asymmetry as currently-observed-behaviour. ~0.25
   dev-day. Mark `xfail` or `skip(reason="pt-041 open")` so the
   suite stays green until the fix lands.
4. **Decide on pt-041 fix**: two options that both close the
   asymmetry —
   - (a) **Inverse-derived target**: change [paper/paper_engine.py:1420](../paper/paper_engine.py#L1420)
     to `target_price = avg / (1 - tp_pct / 100)` for long,
     `avg / (1 + tp_pct / 100)` for short. Realised PnL becomes
     exactly `tp_pct` × leverage². No PnL formula change. ~1 dev-day
     including backtest mirror, tests, and updating any wizard help
     text that describes TP semantics.
   - (b) **Document the linear convention** and accept the asymmetry:
     update operator-facing copy to say "TP target is set at +X %
     above avg entry; on inverse-perp this realises as X/(1+X/100) %
     PnL". Cheap (~0.25 dd) but does not match the operator's stated
     intent.
   Recommend (a) — it's small, additive, and pre-empts the inevitable
   first user who notices a 5 % TP delivers 4.76 % PnL.
5. **Update findings tracker**: pt-043 is `open` in
   [data/findings_seed.yaml](../data/findings_seed.yaml) but the code docstring at
   [paper/paper_state.py:113-145](../paper/paper_state.py#L113-L145) asserts the fix shipped
   2026-04-28. Either flip the row to `resolved` with a
   `resolution_ref` or add an audit note explaining why it remains
   open. ~0.1 dev-day.
6. **Update operator-facing wizard / docs** ([web/static/app.js](../web/static/app.js)
   has the bot-wizard copy) to describe TP semantics
   accurately — whatever convention is chosen in (4). ~0.5 dev-day.
7. **Optional**: add an integration-style test that exercises
   `_check_dca` → DCA fill → `_check_tp` on the next "tick" with the
   new target, end-to-end. ~0.5 dev-day. Closes coverage gap 5.2.3.

## Appendix A: File:line reference table

**TP target derivation.**
- [paper/paper_engine.py:1419-1420](../paper/paper_engine.py#L1419-L1420) — long-style
  `target_price = avg * (1 + tp_pct / 100)`.
- [backtest/backtest_engine.py:223](../backtest/backtest_engine.py#L223) — same formula in
  backtest.
- [paper/paper_state.py:100-106](../paper/paper_state.py#L100-L106) — `avg_entry_price`
  property (volume-weighted).

**Wick simulation.**
- [paper/paper_engine.py:913-944](../paper/paper_engine.py#L913-L944) — engine-level forming-
  candle cache.
- [paper/paper_engine.py:1330-1358](../paper/paper_engine.py#L1330-L1358) —
  `_update_deal_wick_trackers`.
- [paper/paper_engine.py:1432-1437](../paper/paper_engine.py#L1432-L1437) — TP wick comparison.
- [paper/paper_engine.py:1462](../paper/paper_engine.py#L1462) — fill-price cap at target.
- [paper/paper_state.py:81-93](../paper/paper_state.py#L81-L93) — per-deal trackers.

**DCA placement.**
- [paper/paper_engine.py:1644-1702](../paper/paper_engine.py#L1644-L1702) — `_check_dca`.
- [paper/paper_engine.py:1658-1664](../paper/paper_engine.py#L1658-L1664) — line-1662 dynamic
  threshold the operator already noticed.

**PnL realisation.**
- [paper/paper_state.py:113-172](../paper/paper_state.py#L113-L172) — `calculate_pnl`,
  inverse-perp formula post-pt-043.
- [paper/paper_state.py:163-166](../paper/paper_state.py#L163-L166) — long/short branches.

**Direction handling.**
- [paper/paper_engine.py:1280-1285](../paper/paper_engine.py#L1280-L1285) — `_open_deal` honours
  `config.direction`.
- [paper/paper_engine.py:1397-1702](../paper/paper_engine.py#L1397-L1702) — `_check_tp` /
  `_check_sl` / `_check_dca` ignore `deal.side`.
- [backtest/backtest_engine.py:337](../backtest/backtest_engine.py#L337) — `side="long"`
  hardcoded in backtest deal-open.

**Tests.**
- [tests/test_trading_engine.py:76-84](../tests/test_trading_engine.py#L76-L84) —
  `test_tp_uses_avg_entry` (pins avg-entry-following).
- [tests/test_trading_engine.py:243-440](../tests/test_trading_engine.py#L243-L440) —
  wick-simulation test classes.
- [tests/test_paper_state.py:50-180](../tests/test_paper_state.py#L50-L180) —
  inverse-perp PnL coverage including Bitget-testnet anchors.
- [tests/test_paper_engine.py:269-285](../tests/test_paper_engine.py#L269-L285) — short-side
  open coverage (but not exits).

**Findings.**
- [data/findings_seed.yaml:2224-2233](../data/findings_seed.yaml#L2224-L2233) — pt-041
  (HIGH/open).
- [data/findings_seed.yaml:2244-2252](../data/findings_seed.yaml#L2244-L2252) — pt-043
  (HIGH/open in YAML, fixed-in-code per docstring).

## Appendix B: Open questions for operator

Questions that this read-only investigation cannot answer:

1. **What was the configured `tp_pct` for the 0.67 %-PnL deal?** The
   most likely answer is `tp_pct ≈ 0.7 %` (S4 in Part 4.1). If yes,
   the anecdote's *symptom* is correct (it's pt-041) and the
   *explanation* was wrong. If no — e.g. `tp_pct = 3 %` — there is
   another path producing this number that this investigation hasn't
   located, and runtime instrumentation is needed.
2. **Was the deal a long or a short?** If short, the broken short-
   direction TP logic in `_check_tp` (Part 6.1.4) likely fired on an
   adverse move and the recorded PnL would be *negative*, not 0.67 %
   positive. If positive 0.67 %, the deal was almost certainly a long.
3. **Did the deal have any `_tp_override` from the portal**
   ([paper/paper_engine.py:1409-1413](../paper/paper_engine.py#L1409-L1413))? An override could
   set a different `target_pct` than the bot config — runtime data
   would clarify.
4. **Is pt-043 actually still open as a tracked finding, or is the
   YAML row stale?** Code docstring + tests assert fix shipped; YAML
   says open. Operator should reconcile.
5. **What does the operator want short-direction bots to do during
   the live-trading rollout?** Three options: (a) fix exit logic, (b)
   gate short bots out of live mode until fixed, (c) accept that
   short bots are paper-only for now. Whichever, decide before live.
6. **Is the per-deal wick tracker's "since open" semantic the right
   one** when avg entry changes mid-deal (Part 2.3.4)? Operator
   intent might be "since latest avg-entry change" — that's a
   different semantic. Code-only investigation can't infer the right
   answer.
7. **Industry-standard intent verification**: the investigation
   couldn't verify whether 3Commas / Gainium derive TP linearly or
   inverse-aware (they're closed-source). If the operator has a
   3Commas account, a quick "set 3 % TP, fill, observe realised PnL
   on inverse-BTCUSD" experiment would clarify whether competitors
   ship pt-041's linear shape too (in which case Reverto matching
   today is fine) or whether they're inverse-aware (in which case
   Reverto stands out).
