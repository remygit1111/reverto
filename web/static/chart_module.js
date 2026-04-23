// chart_module.js — shared chart logic extracted from app.js.
//
// Status: PR 3a of the Workspace feature. This is a SCOPED
// extraction — only the pure indicator-math helpers that all three
// candle-chart sites (_chartMain, _wizardChart, _btCandleChart)
// invoke live here. The three bespoke chart-instance setups remain
// in app.js; a factory for them is deferred to PR 3b, where the
// Workspace chart-panel will shape the consumer API.
//
// Rationale for the lean scope (per the spec's own rule):
//
//   "Als backtest-equity of monthly ook aanroept: laat kern-functie
//    in app.js, laat module die importeren via window-scope."
//
// The chart primitives _cssVar, getChartColors, _chartLayoutOpts,
// _LWC, _lwcCreateChart, _chartLibAvailable, _applyChartTheme are
// also called by the equity/monthly/wizard-backtest line-chart
// instances, so they stay in app.js. This module's functions only
// touch pure math (no DOM, no chart state) — safe to move without
// touching call sites.
//
// Loading order in index.html: Lightweight Charts (unpkg) →
// chart_module.js → app.js. Script tags at top level share the
// same global scope, so function declarations here land alongside
// the ones in app.js with no import/export ceremony.
//
// Each function is ALSO attached to window.RevertoChart below so
// a future PR 3b chart-panel factory has a namespaced home for
// them without having to re-plumb global lookups.

// pandas-style EWM with adjust=False, which matches the textbook
// recurrence: ema[i] = k*x[i] + (1-k)*ema[i-1], k = 2/(period+1).
function _emaArray(values, period) {
  const out = new Array(values.length).fill(NaN);
  if (!values.length) return out;
  const k = 2 / (period + 1);
  out[0] = values[0];
  for (let i = 1; i < values.length; i++) {
    out[i] = k * values[i] + (1 - k) * out[i - 1];
  }
  return out;
}

function calcEMALine(candles, period) {
  const closes = candles.map(c => c.close);
  const ema = _emaArray(closes, period);
  // Skip warmup — first `period` points are still biased.
  return candles
    .map((c, i) => ({ time: c.time, value: i < period ? NaN : ema[i] }))
    .filter(p => Number.isFinite(p.value));
}

function calcRSILine(candles, period) {
  // Wilder smoothing via ewm(com=period-1) ⇒ alpha = 1/period.
  const closes = candles.map(c => c.close);
  if (closes.length < period + 1) return [];
  const alpha = 1 / period;
  let avgGain = 0, avgLoss = 0;
  // Seed with simple average over the first `period` deltas.
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d >= 0) avgGain += d; else avgLoss += -d;
  }
  avgGain /= period; avgLoss /= period;
  const out = [];
  const rsiAt = (g, l) => {
    if (l === 0) return 100;
    const rs = g / l;
    return 100 - 100 / (1 + rs);
  };
  out.push({ time: candles[period].time, value: rsiAt(avgGain, avgLoss) });
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const gain = d > 0 ? d : 0;
    const loss = d < 0 ? -d : 0;
    avgGain = (1 - alpha) * avgGain + alpha * gain;
    avgLoss = (1 - alpha) * avgLoss + alpha * loss;
    out.push({ time: candles[i].time, value: rsiAt(avgGain, avgLoss) });
  }
  return out;
}

function calcMACDLines(candles, fast, slow, signal) {
  const closes = candles.map(c => c.close);
  const emaFast = _emaArray(closes, fast);
  const emaSlow = _emaArray(closes, slow);
  const macd = closes.map((_, i) => emaFast[i] - emaSlow[i]);
  const signalArr = _emaArray(macd, signal);
  const macdOut = [], sigOut = [], histOut = [];
  // _cssVar lives in app.js (used by the line/bar-chart instances
  // too) and resolves via the shared global scope at call time.
  const accent = _cssVar('--accent', '#26a69a');
  const red    = _cssVar('--red',    '#ef5350');
  for (let i = 0; i < candles.length; i++) {
    if (i < slow) continue;
    macdOut.push({ time: candles[i].time, value: macd[i] });
    sigOut.push({ time: candles[i].time, value: signalArr[i] });
    const h = macd[i] - signalArr[i];
    histOut.push({ time: candles[i].time, value: h, color: h >= 0 ? accent : red });
  }
  return { macd: macdOut, signal: sigOut, histogram: histOut };
}

function calcBollingerLines(candles, period, multiplier) {
  const upper = [], middle = [], lower = [];
  for (let i = period - 1; i < candles.length; i++) {
    const window = candles.slice(i - period + 1, i + 1).map(c => c.close);
    const mean = window.reduce((a, b) => a + b, 0) / period;
    let variance = 0;
    for (const v of window) variance += (v - mean) * (v - mean);
    variance /= period; // population std dev (matches Python pstdev)
    const std = Math.sqrt(variance);
    const t = candles[i].time;
    middle.push({ time: t, value: mean });
    upper.push({  time: t, value: mean + multiplier * std });
    lower.push({  time: t, value: mean - multiplier * std });
  }
  return { upper, middle, lower };
}

function calcSupertrendLines(candles, atrPeriod, multiplier) {
  // Mirrors strategies/indicators/supertrend.py: simple ATR for the first
  // window then Wilder smoothing, with the trend-following band logic.
  const n = candles.length;
  const highs  = candles.map(c => c.high);
  const lows   = candles.map(c => c.low);
  const closes = candles.map(c => c.close);
  if (n < atrPeriod + 1) return { bull: [], bear: [] };
  const tr = new Array(n).fill(0);
  for (let i = 1; i < n; i++) {
    tr[i] = Math.max(
      highs[i] - lows[i],
      Math.abs(highs[i] - closes[i - 1]),
      Math.abs(lows[i] - closes[i - 1]),
    );
  }
  const atr = new Array(n).fill(0);
  let s = 0;
  for (let i = 1; i <= atrPeriod; i++) s += tr[i];
  atr[atrPeriod] = s / atrPeriod;
  for (let i = atrPeriod + 1; i < n; i++) {
    atr[i] = (atr[i - 1] * (atrPeriod - 1) + tr[i]) / atrPeriod;
  }

  const bull = [], bear = [];
  let prevFinalUpper = 0, prevFinalLower = 0, prevTrend = 1;
  for (let i = 0; i < n; i++) {
    if (i < atrPeriod) continue;
    const mid = (highs[i] + lows[i]) / 2;
    const basicUpper = mid + multiplier * atr[i];
    const basicLower = mid - multiplier * atr[i];
    let finalUpper, finalLower, trend;
    if (i === atrPeriod) {
      finalUpper = basicUpper;
      finalLower = basicLower;
      trend = closes[i] > basicUpper ? 1 : -1;
    } else {
      finalUpper = (basicUpper < prevFinalUpper || closes[i - 1] > prevFinalUpper) ? basicUpper : prevFinalUpper;
      finalLower = (basicLower > prevFinalLower || closes[i - 1] < prevFinalLower) ? basicLower : prevFinalLower;
      if (prevTrend === 1) trend = closes[i] < finalLower ? -1 : 1;
      else                 trend = closes[i] > finalUpper ?  1 : -1;
    }
    const stVal = trend === 1 ? finalLower : finalUpper;
    const t = candles[i].time;
    if (trend === 1) {
      bull.push({ time: t, value: stVal });
      bear.push({ time: t, value: NaN });
    } else {
      bear.push({ time: t, value: stVal });
      bull.push({ time: t, value: NaN });
    }
    prevFinalUpper = finalUpper; prevFinalLower = finalLower; prevTrend = trend;
  }
  // LWC drops NaN points → gaps. Filter to keep series clean.
  return {
    bull: bull.filter(p => Number.isFinite(p.value)),
    bear: bear.filter(p => Number.isFinite(p.value)),
  };
}

function calcSR(candles, leftBars, rightBars, volumeThreshold, minTouches) {
  const n = candles.length;
  const vt = volumeThreshold || 0;
  const mt = minTouches || 1;
  const resSeries = new Array(n).fill(null);
  const supSeries = new Array(n).fill(null);
  let volOsc = null;
  if (vt > 0) {
    const vols = candles.map(c => c.volume || 0);
    const ema5 = _emaArray(vols, 5);
    const ema10 = _emaArray(vols, 10);
    volOsc = vols.map((_, i) => ema10[i] !== 0 ? 100 * (ema5[i] - ema10[i]) / ema10[i] : 0);
  }
  let curRes = null, curSup = null;
  let resTouches = 0, supTouches = 0;
  for (let i = 0; i < n; i++) {
    const p = i - rightBars;
    if (p >= leftBars) {
      const ph = candles[p].high != null ? candles[p].high : candles[p].close;
      const pl = candles[p].low != null ? candles[p].low : candles[p].close;
      let leftMaxH = -Infinity, rightMaxH = -Infinity;
      let leftMinL = Infinity, rightMinL = Infinity;
      for (let k = p - leftBars; k < p; k++) {
        const h = candles[k].high != null ? candles[k].high : candles[k].close;
        const l = candles[k].low != null ? candles[k].low : candles[k].close;
        if (h > leftMaxH) leftMaxH = h;
        if (l < leftMinL) leftMinL = l;
      }
      for (let k = p + 1; k <= p + rightBars; k++) {
        const h = candles[k].high != null ? candles[k].high : candles[k].close;
        const l = candles[k].low != null ? candles[k].low : candles[k].close;
        if (h > rightMaxH) rightMaxH = h;
        if (l < rightMinL) rightMinL = l;
      }
      if (ph > leftMaxH && ph > rightMaxH) {
        if (!volOsc || volOsc[p] > vt) { curRes = ph; resTouches = 1; }
      }
      if (pl < leftMinL && pl < rightMinL) {
        if (!volOsc || volOsc[p] > vt) { curSup = pl; supTouches = 1; }
      }
    }
    if (curRes !== null && mt > 1) {
      const hi = candles[i].high != null ? candles[i].high : candles[i].close;
      if (Math.abs(hi - curRes) / curRes < 0.005) resTouches++;
    }
    if (curSup !== null && mt > 1) {
      const lo = candles[i].low != null ? candles[i].low : candles[i].close;
      if (Math.abs(lo - curSup) / curSup < 0.005) supTouches++;
    }
    resSeries[i] = resTouches >= mt ? curRes : null;
    supSeries[i] = supTouches >= mt ? curSup : null;
  }
  if (window._BT_DEBUG) {
    console.log('[S&R DEBUG] resSeries last:', resSeries[n - 1], 'supSeries last:', supSeries[n - 1]);
  }
  return { resSeries, supSeries };
}

function calcQFL(candles, basePeriods, pumpPeriods, pumpPct, baseCrackPct) {
  const n = candles.length;
  const bp = basePeriods || 36;
  const pp = Math.min(pumpPeriods || 8, bp - 1);
  const pumpFrac = (pumpPct || 3.0) / 100;
  const crackFrac = (baseCrackPct || 3.0) / 100;
  const hi = candles.map(c => c.high != null ? c.high : c.close);
  const lo = candles.map(c => c.low != null ? c.low : c.close);
  const baseSeries = new Array(n).fill(null);
  const buyLimitSeries = new Array(n).fill(null);
  const newBaseSeries = new Array(n).fill(false);
  let curBase = null, curHH = null;
  for (let i = 0; i < n; i++) {
    const start = Math.max(0, i - bp + 1);
    let lowestLow = Infinity;
    for (let k = start; k <= i; k++) if (lo[k] < lowestLow) lowestLow = lo[k];
    let newBase = false;
    if (i >= pp + 1) {
      const endPp1 = Math.max(1, i - pp);
      const endPp = i - pp + 1;
      const sPp1 = Math.max(0, endPp1 - bp);
      const sPp = Math.max(0, endPp - bp);
      let llPp1 = Infinity, llPp = Infinity;
      for (let k = sPp1; k < endPp1; k++) if (lo[k] < llPp1) llPp1 = lo[k];
      for (let k = sPp; k < endPp; k++) if (lo[k] < llPp) llPp = lo[k];
      newBase = (llPp1 > llPp) && (llPp === lowestLow);
    }
    const hhStart = Math.max(0, i - pp + 1);
    let offsetHigh = -Infinity;
    for (let k = hhStart; k <= i; k++) if (hi[k] > offsetHigh) offsetHigh = hi[k];
    if (newBase || curHH === null || hi[i] > curHH) curHH = offsetHigh;
    if (newBase) curBase = lowestLow;
    let buyLimit = null;
    if (curBase !== null && curHH !== null && curBase > 0) {
      const pumpOk = (curHH - curBase) / curBase > pumpFrac;
      const crackOk = (curBase - lo[i]) / curBase > crackFrac;
      if (pumpOk && crackOk) buyLimit = curBase * (1 - crackFrac);
    }
    baseSeries[i] = curBase;
    buyLimitSeries[i] = buyLimit;
    newBaseSeries[i] = newBase;
  }
  return { baseSeries, buyLimitSeries, newBaseSeries };
}

function calcParabolicSAR(candles, initialAF, maxAF) {
  const n = candles.length;
  if (n < 10) return { sarValues: [], dirs: [] };
  const hi = candles.map(c => c.high != null ? c.high : c.close);
  const lo = candles.map(c => c.low != null ? c.low : c.close);
  const cl = candles.map(c => c.close);
  const sarValues = new Array(n).fill(null);
  const dirs = new Array(n).fill(0);
  let trend, ep, sar, af = initialAF;
  if (cl[1] >= cl[0]) { trend = 1; ep = hi[1]; sar = lo[0]; }
  else                 { trend = -1; ep = lo[1]; sar = hi[0]; }
  sarValues[0] = sar; dirs[0] = trend;
  sarValues[1] = sar; dirs[1] = trend;
  for (let i = 2; i < n; i++) {
    let newSar = sar + af * (ep - sar);
    if (trend === 1) {
      newSar = Math.min(newSar, lo[i - 1], i >= 3 ? lo[i - 2] : lo[i - 1]);
      if (newSar > lo[i]) { trend = -1; sar = ep; ep = lo[i]; af = initialAF; }
      else { sar = newSar; if (hi[i] > ep) { ep = hi[i]; af = Math.min(af + initialAF, maxAF); } }
    } else {
      newSar = Math.max(newSar, hi[i - 1], i >= 3 ? hi[i - 2] : hi[i - 1]);
      if (newSar < hi[i]) { trend = 1; sar = ep; ep = hi[i]; af = initialAF; }
      else { sar = newSar; if (lo[i] < ep) { ep = lo[i]; af = Math.min(af + initialAF, maxAF); } }
    }
    sarValues[i] = sar;
    dirs[i] = sar < cl[i] ? 1 : -1;
  }
  return { sarValues, dirs };
}

function calcMarketStructureMarkers(candles, lookback) {
  // Surface swing highs/lows as markers — matches market_structure.py
  // _swing_points. Simplified: we don't try to label HH/HL/LH/LL.
  const closes = candles.map(c => c.close);
  const out = [];
  const accent = _cssVar('--accent', '#26a69a');
  const red    = _cssVar('--red',    '#ef5350');
  for (let i = lookback; i < closes.length - lookback; i++) {
    const pivot = closes[i];
    const left  = closes.slice(i - lookback, i);
    const right = closes.slice(i + 1, i + 1 + lookback);
    if (left.every(x => pivot > x) && right.every(x => pivot > x)) {
      out.push({ time: candles[i].time, position: 'aboveBar', color: red, shape: 'arrowDown', text: 'H' });
    } else if (left.every(x => pivot < x) && right.every(x => pivot < x)) {
      out.push({ time: candles[i].time, position: 'belowBar', color: accent, shape: 'arrowUp', text: 'L' });
    }
  }
  return out;
}

// ── Public namespace ──────────────────────────────────────────────────────
// The functions above are still available as plain globals (app.js
// call sites use them that way) — the namespace is additive, giving
// future code an explicit import target. PR 3b will grow this with
// the chart-instance factory once the Workspace chart-panel shapes
// its own consumer API.
window.RevertoChart = Object.assign(window.RevertoChart || {}, {
  calcEMALine,
  calcRSILine,
  calcMACDLines,
  calcBollingerLines,
  calcSupertrendLines,
  calcSR,
  calcQFL,
  calcParabolicSAR,
  calcMarketStructureMarkers,
});
