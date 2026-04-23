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

// ── Workspace chart-panel factory ─────────────────────────────────────────
// PR 3b: a self-contained candle-chart instance for use inside a Workspace
// grid panel. The factory owns a closure over the chart's mutable state
// (LWC instance, series handles, markers, price lines, annotations, bound
// bot) and returns a handle with a verb-shaped API so the workspace
// module can drive it without poking at internals.
//
// The main-chart / wizard / backtest paths keep their bespoke init code
// in app.js — they predate this factory and the cost of migrating them
// outweighs the benefit. This factory is consumer-shaped for the
// Workspace's specific needs: per-panel pair/timeframe, toggleable
// indicators with default parameters, panel-scoped annotations, and an
// optional bot-binding that overlays deal markers + TP/SL lines.
//
// Shared with app.js via the global scope: _cssVar, _chartLayoutOpts,
// _LWC, _lwcCreateChart, _chartLibAvailable, _applyChartTheme. Those
// primitives live in app.js (they're used by equity/monthly line charts
// too) and resolve via window at call time, just like the pure-math
// helpers above.

const PANEL_INDICATOR_TYPES = [
  'EMA', 'RSI', 'MACD', 'BOLLINGER', 'SUPERTREND',
  'SUPPORT_RESISTANCE', 'QFL', 'PARABOLIC_SAR', 'MARKET_STRUCTURE',
];

// Default indicator parameters — workspace chart-panels don't expose
// per-indicator config (the consumer spec picked "use existing defaults")
// so we seed the calc* helpers with the same values the bot-config
// wizard defaults to.
const PANEL_INDICATOR_DEFAULTS = {
  EMA: { period: 21 },
  RSI: { period: 14 },
  MACD: { fast: 12, slow: 26, signal: 9 },
  BOLLINGER: { period: 20, multiplier: 2.0 },
  SUPERTREND: { atr_period: 10, multiplier: 3.0 },
  SUPPORT_RESISTANCE: { left_bars: 15, right_bars: 15, volume_threshold: 0, min_touches: 1 },
  QFL: { base_periods: 36, pump_periods: 8, pump_pct: 3.0, base_crack_pct: 3.0 },
  PARABOLIC_SAR: { initial_af: 0.02, max_af: 0.20 },
  MARKET_STRUCTURE: { lookback: 3 },
};

const PANEL_INDICATOR_LABELS = {
  EMA: 'EMA(21)',
  RSI: 'RSI',
  MACD: 'MACD',
  BOLLINGER: 'Bollinger',
  SUPERTREND: 'Supertrend',
  SUPPORT_RESISTANCE: 'S/R',
  QFL: 'QFL',
  PARABOLIC_SAR: 'Parabolic SAR',
  MARKET_STRUCTURE: 'Market structure',
};

// Aligned with the backend _CHART_TIMEFRAMES whitelist in web/app.py —
// 1m/5m are intentionally excluded because Reverto is DCA/swing, not a
// scalping platform, and the /api/chart endpoint 400s for anything
// outside this set. A layout saved with a now-invalid timeframe falls
// back to '1h' on load (see _panelNormalizeTimeframe below).
const PANEL_TIMEFRAMES = ['15m', '30m', '1h', '2h', '4h', '12h', '1d', '3d', '1w'];

// Shared timeframe-to-seconds map used by the scroll-to-load-history
// path. The map-form isn't exposed; the helper ``tfSeconds`` below
// is the public surface so consumers can't mutate the table. Mirrors
// ``web/app.py``'s ``_TF_SECONDS`` but the frontend only needs the
// subset that PANEL_TIMEFRAMES exposes. Unknown timeframes fall back
// to 3600 (1 h) — the loaders treat the computed start-window as
// "roughly one batch ago" so a bad map-hit still produces a fetch
// that asks for too-many candles (clamped by the backend) rather
// than zero.
const _CHART_TF_SECONDS = {
  '15m': 900,
  '30m': 1800,
  '1h':  3600,
  '2h':  7200,
  '4h':  14400,
  '12h': 43200,
  '1d':  86400,
  '3d':  259200,
  '1w':  604800,
};
function tfSeconds(tf) {
  return _CHART_TF_SECONDS[tf] || 3600;
}

// Shared timezone catalogue used by every chart type that offers a
// per-chart timezone dropdown (main bot-chart, wizard chart,
// backtest-candle chart, Workspace chart-panel). IANA names are
// kept as the ``value`` so Intl.DateTimeFormat applies them
// directly; the special ``'local'`` sentinel means "use the
// browser's default" and maps to an ``undefined`` timeZone option
// (which is the HTML Living Standard's "drop the option" behaviour).
// Alphabetic ordering within each continent-group for predictable
// dropdown scanning.
const CHART_TIMEZONES = [
  { value: 'local',              label: 'Local (browser)' },
  { value: 'UTC',                label: 'UTC' },
  { value: 'Europe/Amsterdam',   label: 'Europe/Amsterdam' },
  { value: 'Europe/London',      label: 'Europe/London' },
  { value: 'Europe/Berlin',      label: 'Europe/Berlin' },
  { value: 'Europe/Moscow',      label: 'Europe/Moscow' },
  { value: 'America/New_York',   label: 'America/New_York' },
  { value: 'America/Chicago',    label: 'America/Chicago' },
  { value: 'America/Denver',     label: 'America/Denver' },
  { value: 'America/Los_Angeles',label: 'America/Los_Angeles' },
  { value: 'America/Toronto',    label: 'America/Toronto' },
  { value: 'America/Sao_Paulo',  label: 'America/Sao_Paulo' },
  { value: 'Asia/Tokyo',         label: 'Asia/Tokyo' },
  { value: 'Asia/Shanghai',      label: 'Asia/Shanghai' },
  { value: 'Asia/Hong_Kong',     label: 'Asia/Hong_Kong' },
  { value: 'Asia/Singapore',     label: 'Asia/Singapore' },
  { value: 'Asia/Dubai',         label: 'Asia/Dubai' },
  { value: 'Asia/Kolkata',       label: 'Asia/Kolkata' },
  { value: 'Australia/Sydney',   label: 'Australia/Sydney' },
  { value: 'Pacific/Auckland',   label: 'Pacific/Auckland' },
];
const _CHART_TIMEZONE_VALUES = new Set(CHART_TIMEZONES.map((t) => t.value));

function _normalizeChartTimezone(tz) {
  return _CHART_TIMEZONE_VALUES.has(tz) ? tz : 'local';
}

// Central formatter factory shared between every chart site. Returns
// ``{short, full}``:
//   * ``short(unixSec)`` → HH:MM, used by ``timeScale.tickMarkFormatter``
//     for the x-axis labels.
//   * ``full(unixSec)``  → DD-MM-YYYY HH:MM, used by
//     ``localization.timeFormatter`` for the crosshair tooltip.
// Output shape matches app.js's pre-existing _tzFormatter contract so
// migrating callers don't regress the deal-panel's fmtDateTimeNL
// style. ``'local'`` sentinel drops the ``timeZone`` option so the
// browser's default applies.
function buildTimezoneFormatter(timezone) {
  const tz = _normalizeChartTimezone(timezone);
  const shortOpts = {
    hour: '2-digit', minute: '2-digit', hour12: false,
  };
  const fullOpts = {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  };
  if (tz !== 'local') {
    shortOpts.timeZone = tz;
    fullOpts.timeZone = tz;
  }
  const shortFmt = new Intl.DateTimeFormat('en-GB', shortOpts);
  const fullFmt = new Intl.DateTimeFormat('en-GB', fullOpts);
  return {
    short(unixSec) {
      try {
        const parts = shortFmt.formatToParts(new Date(unixSec * 1000));
        const lookup = {};
        for (const p of parts) lookup[p.type] = p.value;
        const hh = lookup.hour === '24' ? '00' : lookup.hour;
        return `${hh}:${lookup.minute}`;
      } catch (e) {
        return new Date(unixSec * 1000).toISOString().slice(11, 16);
      }
    },
    full(unixSec) {
      try {
        const parts = fullFmt.formatToParts(new Date(unixSec * 1000));
        const lookup = {};
        for (const p of parts) lookup[p.type] = p.value;
        const hh = lookup.hour === '24' ? '00' : lookup.hour;
        return `${lookup.day}-${lookup.month}-${lookup.year} ${hh}:${lookup.minute}`;
      } catch (e) {
        return new Date(unixSec * 1000).toISOString().slice(0, 16).replace('T', ' ');
      }
    },
  };
}

const PANEL_SVG_NS = 'http://www.w3.org/2000/svg';

// FastAPI path params reject %2F, so the backend /api/chart route takes
// the slash-less form; the internal representation keeps the slash so
// display + layout-json stay human-readable.
function _panelPairForUrl(pair) {
  return String(pair || 'BTCUSD').replace('/', '');
}

function _panelNormalizePair(p) {
  if (!p) return 'BTC/USD';
  if (p.indexOf('/') >= 0) return p;
  if (p.endsWith('USDT')) return p.slice(0, -4) + '/USDT';
  if (p.endsWith('USD')) return p.slice(0, -3) + '/USD';
  return p;
}

// A layout persisted before the PANEL_TIMEFRAMES narrowing could hold a
// timeframe that the backend now rejects (1m, 5m were briefly exposed
// in PR 3b's popover but /api/chart always 400d them). Substitute an
// unknown value with the default '1h' so the chart loads instead of
// hitting a guaranteed 400 on every candle fetch.
function _panelNormalizeTimeframe(tf) {
  return PANEL_TIMEFRAMES.includes(tf) ? tf : '1h';
}

function _panelIsoToUnix(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isFinite(t) ? Math.floor(t / 1000) : null;
}

function _panelSvg(name, attrs) {
  const el = document.createElementNS(PANEL_SVG_NS, name);
  if (attrs) {
    for (const k of Object.keys(attrs)) el.setAttribute(k, String(attrs[k]));
  }
  return el;
}

// ── Header-dropdown helpers (PR 5a) ──────────────────────────────────────
// Two factories kept module-local so the per-panel state closure stays
// tight. Each returns a ``{root, menu, updateLabel/updateCount}``
// object; the panel calls ``_wireXxxDropdown`` to hook up state +
// onChange. Keeping build + wire separate lets tests mount the DOM
// without driving real handlers (none yet, but cheap insurance).

function _buildHeaderTfDropdown(state) {
  const root = document.createElement('div');
  root.className = 'panel-tf-dropdown';
  root.dataset.role = 'tf-dropdown';
  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'dropdown-trigger';
  const current = document.createElement('span');
  current.className = 'tf-current';
  current.textContent = state.timeframe;
  const caret = document.createElement('span');
  caret.className = 'dropdown-caret';
  caret.textContent = '▾';
  trigger.appendChild(current);
  trigger.appendChild(caret);
  const menu = document.createElement('div');
  menu.className = 'dropdown-menu hidden';
  menu.setAttribute('role', 'menu');
  for (const tf of PANEL_TIMEFRAMES) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'dropdown-item';
    if (tf === state.timeframe) item.classList.add('active');
    item.dataset.value = tf;
    item.textContent = tf;
    menu.appendChild(item);
  }
  root.appendChild(trigger);
  root.appendChild(menu);
  return {
    root, trigger, menu,
    updateLabel(tf) {
      current.textContent = tf;
      menu.querySelectorAll('.dropdown-item').forEach((b) => {
        b.classList.toggle('active', b.dataset.value === tf);
      });
    },
  };
}

function _buildHeaderIndicatorsDropdown(state) {
  const root = document.createElement('div');
  root.className = 'panel-indicators-dropdown';
  root.dataset.role = 'indicators-dropdown';
  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'dropdown-trigger';
  const count = document.createElement('span');
  count.className = 'indicators-count';
  count.textContent = String(state.indicators.length);
  const label = document.createElement('span');
  label.className = 'indicators-label';
  label.textContent = 'Indicators';
  const caret = document.createElement('span');
  caret.className = 'dropdown-caret';
  caret.textContent = '▾';
  trigger.appendChild(count);
  trigger.appendChild(label);
  trigger.appendChild(caret);
  const menu = document.createElement('div');
  menu.className = 'dropdown-menu hidden';
  menu.setAttribute('role', 'menu');
  for (const t of PANEL_INDICATOR_TYPES) {
    const row = document.createElement('label');
    row.className = 'dropdown-checkbox';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = t;
    cb.checked = state.indicators.includes(t);
    const span = document.createElement('span');
    span.textContent = PANEL_INDICATOR_LABELS[t] || t;
    row.appendChild(cb);
    row.appendChild(span);
    menu.appendChild(row);
  }
  root.appendChild(trigger);
  root.appendChild(menu);
  return {
    root, trigger, menu,
    updateCount(n) { count.textContent = String(n); },
  };
}

// ── Header-dropdown click wiring ──────────────────────────────────────────
// Each dropdown manages its own open/close via state._openDropdown so
// only one can be open at a time. A single document-level click
// listener (attached once per panel on construction) dismisses both.

function _closeAllDropdowns(state) {
  if (!state._openDropdown) return;
  state._openDropdown.menu.classList.add('hidden');
  state._openDropdown = null;
}

function _toggleDropdown(state, dd) {
  if (state._openDropdown === dd) {
    _closeAllDropdowns(state);
    return;
  }
  _closeAllDropdowns(state);
  dd.menu.classList.remove('hidden');
  state._openDropdown = dd;
}

function _wireTfDropdown(dd, state, onPick) {
  dd.trigger.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    _toggleDropdown(state, dd);
  });
  dd.menu.addEventListener('click', async (e) => {
    const btn = e.target.closest('.dropdown-item');
    if (!btn) return;
    e.stopPropagation();
    _closeAllDropdowns(state);
    await onPick(btn.dataset.value);
  });
}

function _wireIndicatorsDropdown(dd, state, onChange) {
  dd.trigger.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    _toggleDropdown(state, dd);
  });
  // Multi-select: keep the menu open while the operator toggles
  // several indicators. e.stopPropagation on the checkbox click
  // prevents the document-level outside-click handler from closing
  // the menu on its own label click.
  dd.menu.addEventListener('click', (e) => {
    e.stopPropagation();
    const cb = e.target.closest('input[type=checkbox]');
    if (!cb) return;
    const t = cb.value;
    const idx = state.indicators.indexOf(t);
    if (cb.checked && idx < 0) state.indicators.push(t);
    else if (!cb.checked && idx >= 0) state.indicators.splice(idx, 1);
    else return;
    onChange();
  });
}

// ── Info-sidebar helpers (PR 5a) ─────────────────────────────────────────

function _fmtSidebarPrice(v) {
  if (v == null || !Number.isFinite(v)) return '—';
  // Match the operator's expectations from the main dashboard: thousands
  // separator, two decimals for BTC-USD-scale values. Keeps the sidebar
  // compact enough for the 160 px minimum width without truncation.
  return Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}

function _fmtSidebarVolume(v) {
  if (v == null || !Number.isFinite(v)) return '—';
  const n = Number(v);
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(2) + 'K';
  return n.toFixed(2);
}

function _buildSidebar(state) {
  const root = document.createElement('div');
  root.className = 'panel-chart-sidebar';

  const priceSection = document.createElement('div');
  priceSection.className = 'sidebar-section sidebar-price';
  const pairLabel = document.createElement('div');
  pairLabel.className = 'sidebar-label';
  pairLabel.textContent = state.pair;
  const priceValue = document.createElement('div');
  priceValue.className = 'sidebar-price-value';
  priceValue.textContent = '—';
  const change = document.createElement('div');
  change.className = 'sidebar-change';
  change.textContent = '—';
  priceSection.appendChild(pairLabel);
  priceSection.appendChild(priceValue);
  priceSection.appendChild(change);

  const rangeSection = document.createElement('div');
  rangeSection.className = 'sidebar-section sidebar-range';
  const mkRow = (labelText) => {
    const row = document.createElement('div');
    row.className = 'sidebar-row';
    const lb = document.createElement('span');
    lb.className = 'sidebar-row-label';
    lb.textContent = labelText;
    const val = document.createElement('span');
    val.className = 'sidebar-row-value';
    val.textContent = '—';
    row.appendChild(lb);
    row.appendChild(val);
    rangeSection.appendChild(row);
    return val;
  };
  const highValue = mkRow('High 24h');
  const lowValue = mkRow('Low 24h');
  const volumeValue = mkRow('Volume 24h');

  root.appendChild(priceSection);
  root.appendChild(rangeSection);

  return {
    root,
    updatePair(pair) { pairLabel.textContent = pair; },
    renderTicker(data) {
      if (!data) {
        priceValue.textContent = '—';
        change.textContent = '—';
        change.classList.remove('sidebar-change-positive', 'sidebar-change-negative');
        highValue.textContent = '—';
        lowValue.textContent = '—';
        volumeValue.textContent = '—';
        return;
      }
      priceValue.textContent = _fmtSidebarPrice(data.price);
      const abs = Number(data.change_24h);
      const pct = Number(data.change_pct_24h);
      if (Number.isFinite(abs) && Number.isFinite(pct)) {
        const sign = abs >= 0 ? '+' : '';
        change.textContent =
          `${sign}${abs.toFixed(1)} (${sign}${pct.toFixed(2)}%)`;
        change.classList.toggle('sidebar-change-positive', abs >= 0);
        change.classList.toggle('sidebar-change-negative', abs < 0);
      } else {
        change.textContent = '—';
        change.classList.remove('sidebar-change-positive', 'sidebar-change-negative');
      }
      highValue.textContent = _fmtSidebarPrice(data.high_24h);
      lowValue.textContent = _fmtSidebarPrice(data.low_24h);
      volumeValue.textContent = _fmtSidebarVolume(data.volume_24h);
    },
  };
}

// ── Theme propagation ────────────────────────────────────────────────────
// Module-scoped registry of live panel-chart instances so app.js's
// ``_applyChartTheme`` can fan the theme-switch out across every
// workspace panel in a single call. Each entry holds the raw LWC
// chart + its candle-series so we can re-apply both layout options
// (background / grid / borders) and series options (up/down body +
// wick colours) — the same split that _applyChartTheme does for the
// main bot-detail chart.

const _activePanelCharts = new Set();

function _applyThemeToPanel(entry) {
  // Defensive against a race where destroy() runs between the
  // _applyChartTheme() dispatch and this call. null chart means the
  // panel is already torn down; leave it be.
  if (!entry || !entry.chart) return;
  try {
    const opts = (typeof _chartLayoutOpts === 'function')
      ? _chartLayoutOpts()
      : null;
    if (opts) entry.chart.applyOptions(opts);
  } catch (e) { /* best-effort — theme change must not explode */ }
  // Candle-series colours live on the series, not the chart, so the
  // applyOptions above leaves wick/body colours at creation-time
  // values. Push the up/down palette onto the series too.
  const colorsFn = (typeof getChartColors === 'function') ? getChartColors : null;
  const c = colorsFn ? colorsFn() : null;
  if (!c || !entry.candleSeries) return;
  try {
    entry.candleSeries.applyOptions({
      upColor: c.upColor,
      downColor: c.downColor,
      borderUpColor: c.upColor,
      borderDownColor: c.downColor,
      wickUpColor: c.upColor,
      wickDownColor: c.downColor,
    });
  } catch (e) { /* best-effort */ }
}

function _applyThemeToAllPanels() {
  for (const entry of _activePanelCharts) {
    _applyThemeToPanel(entry);
  }
}

function createPanelChart(container, config) {
  // Consumer contract (see PR 3b spec):
  //   container: the grid-stack-item-content element. The factory
  //     materialises its own panel chrome inside it — header with
  //     title/subtitle/toolbar/settings/remove, body with the LWC
  //     mount, plus an absolute-positioned settings popover.
  //   config.panelId: unique workspace panel id. Used to scope
  //     annotations under the virtual "workspace-panel-<id>" slug
  //     and to filter live state updates.
  //   config.pair / config.timeframe: initial chart state.
  //   config.indicators: array of indicator type names from
  //     PANEL_INDICATOR_TYPES — rendered on init and toggleable via
  //     the settings popover.
  //   config.boundBotSlug / config.boundBotUserId: optional live
  //     bot-binding. boundBotUserId is required when boundBotSlug
  //     is set so closed-deal lookups hit the correct tenant row.
  //   config.onRemove: fired when the operator clicks the × button.
  //     Workspace wires this to grid.removeWidget() + cleanup.
  //   config.onConfigChange: fired whenever any state mutation
  //     would change getConfig()'s output (indicator toggle, pair
  //     change, binding change). Workspace uses it to re-queue
  //     the layout-save.
  const cfg = config || {};
  const state = {
    panelId: cfg.panelId,
    pair: _panelNormalizePair(cfg.pair || 'BTC/USD'),
    timeframe: _panelNormalizeTimeframe(cfg.timeframe || '1h'),
    indicators: Array.isArray(cfg.indicators)
      ? cfg.indicators.filter((i) => PANEL_INDICATOR_TYPES.includes(i))
      : [],
    // PR 5b → timezone-cleanup: per-panel timezone for axis labels +
    // crosshair tooltip. Stored as an IANA name ('UTC', 'Europe/
    // Amsterdam', …) or the literal 'local' sentinel (use browser
    // default). Legacy ``useUtc: bool`` saved layouts migrate here
    // on load: true → 'UTC', false/absent → 'local'. Keeps the
    // pre-migration operator's setting intact and lets the new
    // dropdown surface the richer choice without a user-visible
    // reset.
    timezone: (() => {
      if (typeof cfg.timezone === 'string') return _normalizeChartTimezone(cfg.timezone);
      if (cfg.useUtc === true) return 'UTC';
      return 'local';
    })(),
    boundBotSlug: cfg.boundBotSlug || null,
    boundBotUserId: cfg.boundBotUserId || null,
    onRemove: typeof cfg.onRemove === 'function' ? cfg.onRemove : null,
    onConfigChange: typeof cfg.onConfigChange === 'function' ? cfg.onConfigChange : null,
    _chart: null,
    _candleSeries: null,
    _candles: [],
    _indicatorSeries: {}, // type -> { main: [series], paneSeries: [series] }
    _indicatorMarkers: [],
    _dealMarkers: [],
    _markersPrimitive: null,
    _dealPriceLines: [],
    _closedDeals: [],
    _lastOpenDeals: [],
    _botState: null,
    _botConfig: null,
    _annotations: [],
    _annotSvg: null,
    _activeTool: 'select',
    _toolFirstPoint: null,
    _resizeObs: null,
    _refreshTimer: null,
    _destroyed: false,
    _bindingMissingHint: false,
    // PR 5a: info-sidebar poll + last-known ticker shape for the
    // re-render-without-fetch path on resize.
    _tickerTimer: null,
    _tickerData: null,
    _openDropdown: null, // tracks the currently-open header dropdown for outside-click dismissal
    // scroll-to-load history state — see _maybeLoadMoreHistory below.
    _loadingMore: false,
    _noMoreData: false,
    _historyAbort: null,
    _rangeUnsub: null,
  };

  // ── DOM scaffold ───────────────────────────────────────────────────
  // PR 5a layout (TradingView-style): pair-title + TF-dropdown +
  // Indicators-dropdown as always-visible controls in the header;
  // subtitle (binding indicator) + annotations toolbar + ⚙ + × on
  // the right. Settings popover now only houses pair + bot-binding
  // (the less-frequently touched config). Body is split into the
  // LWC canvas on the left + info-sidebar on the right.
  const panel = document.createElement('div');
  panel.className = 'panel panel-chart';

  const header = document.createElement('div');
  header.className = 'panel-header';

  const titleWrap = document.createElement('div');
  titleWrap.className = 'panel-title-wrap';

  const title = document.createElement('span');
  title.className = 'panel-title';

  // TF-dropdown — standalone control, builds its own menu lazily.
  const tfDropdown = _buildHeaderTfDropdown(state);
  // Indicators-dropdown — multi-select checkbox list, count shown
  // in the trigger label.
  const indDropdown = _buildHeaderIndicatorsDropdown(state);

  const subtitle = document.createElement('span');
  subtitle.className = 'panel-subtitle';
  subtitle.style.marginLeft = '8px';

  // Title + binding-subtitle stay inside the wrap because both are
  // text that benefits from the wrap's overflow:hidden + ellipsis
  // behaviour on long pair-names. The header dropdowns used to sit
  // here too, but the wrap is only ~18 px tall (flex row with
  // overflow:hidden) — the ~200 px dropdown menus opened correctly
  // but were clipped to the wrap's height. Moving them one level
  // up to the header so the menus can overflow downward freely.
  titleWrap.appendChild(title);
  titleWrap.appendChild(subtitle);

  const toolbar = document.createElement('div');
  toolbar.className = 'panel-annotations-toolbar';
  // PR 5b: three new annotation types alongside the existing arrow
  // + text + delete tools. ``measure`` is a two-click ruler (% +
  // absolute delta label between the endpoints); ``trendline`` is
  // a two-click plain line for sloped support/resistance; ``hline``
  // is a one-click horizontal line spanning the full canvas at the
  // clicked price. All three persist through the same
  // /api/db/annotations endpoint the arrow + text annotations
  // already use — only the ``type`` field differs, and the backend
  // column is just TEXT.
  const toolbarTools = [
    { tool: 'arrow', label: '→', title: 'Arrow (two clicks)' },
    { tool: 'trendline', label: '╱', title: 'Trendline (two clicks)' },
    { tool: 'hline', label: '━', title: 'Horizontal line (one click)' },
    { tool: 'measure', label: 'M', title: 'Measure — %-change + Δ (two clicks)' },
    { tool: 'text', label: 'T', title: 'Text label' },
    { tool: 'delete', label: '×', title: 'Delete nearest annotation' },
    { tool: 'clear-all', label: 'CA', title: 'Clear all annotations' },
  ];
  const toolbarBtns = {};
  for (const t of toolbarTools) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'panel-tool-btn';
    b.dataset.tool = t.tool;
    b.title = t.title;
    b.textContent = t.label;
    toolbar.appendChild(b);
    toolbarBtns[t.tool] = b;
  }

  const settingsBtn = document.createElement('button');
  settingsBtn.type = 'button';
  settingsBtn.className = 'panel-settings-btn';
  settingsBtn.setAttribute('aria-label', 'Panel settings');
  settingsBtn.textContent = '⚙';

  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'panel-remove';
  removeBtn.setAttribute('aria-label', 'Remove panel');
  removeBtn.textContent = '×';

  const headerRight = document.createElement('div');
  headerRight.className = 'panel-header-right';
  headerRight.appendChild(toolbar);
  headerRight.appendChild(settingsBtn);
  headerRight.appendChild(removeBtn);

  header.appendChild(titleWrap);
  header.appendChild(tfDropdown.root);
  header.appendChild(indDropdown.root);
  header.appendChild(headerRight);

  const body = document.createElement('div');
  body.className = 'panel-body panel-chart-body';
  const canvasHost = document.createElement('div');
  canvasHost.className = 'panel-chart-canvas';
  canvasHost.style.position = 'relative';
  body.appendChild(canvasHost);

  // Scroll-to-load overlays, absolute-positioned inside the canvas
  // host. The spinner flashes while a history batch is in-flight;
  // the no-more-data marker replaces it when Bitget returns no
  // older candles (the exchange explicitly is named so operators
  // don't blame Reverto for the limit). Both start hidden.
  const loadingOverlay = document.createElement('div');
  loadingOverlay.className = 'chart-loading-spinner hidden';
  loadingOverlay.setAttribute('data-role', 'loading');
  loadingOverlay.innerHTML =
    '<span class="spinner-icon">⏳</span>' +
    '<span class="spinner-text">Loading history…</span>';
  canvasHost.appendChild(loadingOverlay);

  const noMoreOverlay = document.createElement('div');
  noMoreOverlay.className = 'chart-no-more-data hidden';
  noMoreOverlay.setAttribute('data-role', 'no-more-data');
  noMoreOverlay.innerHTML =
    '<span class="no-more-icon">⚠</span>' +
    '<span class="no-more-text">No more historical data available from Bitget for this timeframe</span>';
  canvasHost.appendChild(noMoreOverlay);

  // Info-sidebar — poll /api/ticker every 5 s and render price +
  // 24h change + high/low/volume. DOM is built once; subsequent
  // refreshes only mutate text nodes + change-class so we don't
  // churn LWC's neighbouring canvas layout.
  const sidebar = _buildSidebar(state);
  body.appendChild(sidebar.root);

  const popover = document.createElement('div');
  popover.className = 'panel-settings-popover hidden';
  panel.appendChild(header);
  panel.appendChild(body);
  panel.appendChild(popover);
  container.appendChild(panel);

  // Wire dropdown handlers now that state + render helpers exist.
  _wireTfDropdown(tfDropdown, state, async (tf) => {
    if (!tf || tf === state.timeframe) return;
    state.timeframe = tf;
    tfDropdown.updateLabel(tf);
    _updateTitle();
    await _loadCandles();
    if (state.onConfigChange) state.onConfigChange();
  });
  _wireIndicatorsDropdown(indDropdown, state, () => {
    _rebuildIndicatorSeries();
    _renderIndicatorOverlays();
    indDropdown.updateCount(state.indicators.length);
    if (state.onConfigChange) state.onConfigChange();
  });

  // Outside-click closes whichever header dropdown is currently open.
  // Anchored on ``panel`` so the listener scope dies with the panel
  // — destroy() removes the DOM, and the handler has nothing left to
  // run against. Uses mousedown so the click that opens the new
  // dropdown doesn't first close via its own click event.
  const _outsideClickCloser = (e) => {
    if (!state._openDropdown) return;
    if (state._openDropdown.root.contains(e.target)) return;
    _closeAllDropdowns(state);
  };
  document.addEventListener('mousedown', _outsideClickCloser);
  state._outsideClickCloser = _outsideClickCloser;

  // ── Popover UI ────────────────────────────────────────────────────
  // PR 5a: timeframe + indicators moved to standalone header
  // dropdowns. The popover is now only pair + bot-binding — the
  // less-frequently touched config. Keeps the settings-dialog
  // shape for future additions (default-indicator params,
  // annotations export, etc.) without bloating the header.
  function _buildPopover() {
    popover.innerHTML = '';
    const mkRow = (labelText, input) => {
      const row = document.createElement('div');
      row.className = 'form-row';
      const lb = document.createElement('label');
      lb.textContent = labelText;
      row.appendChild(lb);
      row.appendChild(input);
      return row;
    };
    const pairInput = document.createElement('input');
    pairInput.type = 'text';
    pairInput.value = state.pair;
    pairInput.dataset.field = 'pair';

    const botSel = document.createElement('select');
    botSel.dataset.field = 'bot';
    const noneOpt = document.createElement('option');
    noneOpt.value = '';
    noneOpt.textContent = '— none —';
    botSel.appendChild(noneOpt);
    const bindingHint = document.createElement('div');
    bindingHint.className = 'panel-binding-hint';
    bindingHint.textContent = '';

    // Timezone dropdown. PR 5b shipped a binary UTC-axis-labels
    // checkbox; this supersedes it with a full 20-entry list
    // (local + UTC + common IANA names). Legacy useUtc config is
    // migrated to timezone at state-init so operators see their
    // previous choice pre-selected.
    const tzSel = document.createElement('select');
    tzSel.dataset.field = 'timezone';
    tzSel.className = 'panel-tz-select';
    for (const tz of CHART_TIMEZONES) {
      const o = document.createElement('option');
      o.value = tz.value;
      o.textContent = tz.label;
      if (tz.value === state.timezone) o.selected = true;
      tzSel.appendChild(o);
    }

    const btnRow = document.createElement('div');
    btnRow.className = 'panel-settings-actions';
    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'hbtn hbtn-theme btn-accent';
    saveBtn.textContent = 'Apply';
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'hbtn hbtn-theme';
    cancelBtn.textContent = 'Close';
    btnRow.appendChild(saveBtn);
    btnRow.appendChild(cancelBtn);

    popover.appendChild(mkRow('Pair', pairInput));
    popover.appendChild(mkRow('Bind to bot', botSel));
    popover.appendChild(bindingHint);
    popover.appendChild(mkRow('Timezone', tzSel));
    popover.appendChild(btnRow);

    // Populate bot list lazily on open; keep reference for later refresh.
    state._popoverBotSel = botSel;
    state._popoverBindingHint = bindingHint;

    cancelBtn.addEventListener('click', (e) => {
      e.preventDefault();
      popover.classList.add('hidden');
    });
    saveBtn.addEventListener('click', async (e) => {
      e.preventDefault();
      const newPair = _panelNormalizePair(pairInput.value.trim() || state.pair);
      const newBot = botSel.value || '';
      const pairChanged = newPair !== state.pair;

      // Binding auto-syncs pair + timeframe to the picked bot's
      // config. /api/bots returns state (includes pair) but not the
      // YAML config's timeframe, so we fetch the YAML explicitly.
      // boundBotUserId is stored in layout_json for future use; the
      // backend scopes every request by session cookie so it isn't
      // needed for correctness today.
      let forcedPair = newPair;
      let forcedTf = state.timeframe;
      let boundUserId = state.boundBotUserId;
      if (newBot && newBot !== state.boundBotSlug) {
        const bot = (state._botList || []).find((b) => b.slug === newBot);
        if (bot && bot.pair) forcedPair = _panelNormalizePair(bot.pair);
        try {
          const cfg = await fetch(`/api/bots/${encodeURIComponent(newBot)}/config`)
            .then((r) => r.ok ? r.json() : null);
          const inner = (cfg && cfg.bot) || {};
          if (inner.pair) forcedPair = _panelNormalizePair(inner.pair);
          if (inner.timeframe) forcedTf = inner.timeframe;
        } catch (e) { /* fall back to state-level pair only */ }
      }

      const tfChanged = forcedTf !== state.timeframe;
      const needsReload = pairChanged || tfChanged
        || forcedPair !== state.pair;
      state.pair = forcedPair;
      state.timeframe = forcedTf;

      if (needsReload) {
        if (tfChanged) tfDropdown.updateLabel(state.timeframe);
        sidebar.updatePair(state.pair);
        _updateTitle();
        await _loadCandles();
        // Pair changed → the /api/ticker cache is keyed on pair so
        // we must refetch rather than keep rendering the stale one.
        _tickerFetch();
      }

      // Timezone — re-apply localization + tick-mark formatter to
      // the LWC instance so axis labels + crosshair timestamps swap
      // without a candle reload. Safe to run on every Apply click;
      // our formatter closes over the live ``state.timezone`` so
      // LWC's reference-equality short-circuit still works when the
      // value is unchanged.
      const newTz = _normalizeChartTimezone(tzSel.value);
      const tzChanged = newTz !== state.timezone;
      if (tzChanged) {
        state.timezone = newTz;
        try {
          state._chart.applyOptions({
            localization: { timeFormatter: _panelTimeFormatter },
            timeScale: { tickMarkFormatter: _panelTickMarkFormatter },
          });
        } catch (e) { /* best-effort; cosmetic only */ }
      }

      if (newBot !== (state.boundBotSlug || '')) {
        // silent: the explicit onConfigChange below covers the save
        // so we don't double-queue on a single Apply click.
        await _applyBinding(newBot || null, newBot ? boundUserId : null, true);
      }

      if (state.onConfigChange) state.onConfigChange();
      popover.classList.add('hidden');
    });
  }

  async function _openPopover() {
    _buildPopover();
    // Fetch bot list each time the popover opens so newly-created bots
    // appear without a page refresh.
    try {
      const r = await fetch('/api/bots');
      if (r.ok) {
        const data = await r.json();
        state._botList = Array.isArray(data.bots) ? data.bots : [];
      }
    } catch (e) { state._botList = state._botList || []; }

    const sel = state._popoverBotSel;
    if (sel) {
      // Clear entries except the sentinel "none" at index 0.
      while (sel.options.length > 1) sel.remove(1);
      for (const b of (state._botList || [])) {
        const o = document.createElement('option');
        o.value = b.slug;
        const pairLabel = b.pair ? ` (${b.pair} ${b.timeframe || ''})` : '';
        o.textContent = (b.bot_name || b.slug) + pairLabel;
        if (state.boundBotSlug && state.boundBotSlug === b.slug) o.selected = true;
        sel.appendChild(o);
      }
      // Binding-lost hint: the previously-bound slug isn't in the
      // current list (bot was deleted since layout was saved).
      const hint = state._popoverBindingHint;
      if (hint) {
        if (state.boundBotSlug
            && !(state._botList || []).some((b) => b.slug === state.boundBotSlug)) {
          hint.textContent = `Previously bound bot "${state.boundBotSlug}" no longer exists — binding will be cleared on Apply.`;
        } else {
          hint.textContent = '';
        }
      }
    }
    popover.classList.remove('hidden');
  }

  settingsBtn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (popover.classList.contains('hidden')) _openPopover();
    else popover.classList.add('hidden');
  });

  removeBtn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (state.onRemove) state.onRemove();
  });

  // ── Header indicators ─────────────────────────────────────────────
  function _updateTitle() {
    // PR 5a: title is now just the pair. The TF moved to its own
    // dropdown next to the title; the binding indicator stays on
    // the subtitle.
    title.textContent = state.pair;
    if (state.boundBotSlug) {
      subtitle.textContent = `⚡ ${state.boundBotSlug}`;
      subtitle.classList.add('bound');
    } else {
      subtitle.textContent = '';
      subtitle.classList.remove('bound');
    }
  }

  // ── Info-sidebar poll ────────────────────────────────────────────
  // 5 s cadence mirrors the server-side 10 s cache so N panels on the
  // same pair cost one upstream fetch per window. fetch() errors map
  // to the sidebar's em-dash state; the chart itself is unaffected.
  async function _tickerFetch() {
    if (state._destroyed) return;
    try {
      const r = await fetch(`/api/ticker/${_panelPairForUrl(state.pair)}`);
      if (!r.ok) throw new Error(`ticker ${r.status}`);
      const data = await r.json();
      if (state._destroyed) return;
      state._tickerData = data;
      sidebar.renderTicker(data);
    } catch (e) {
      state._tickerData = null;
      sidebar.renderTicker(null);
    }
  }

  // ── Timezone formatter ────────────────────────────────────────────
  // Per-panel axis-label formatter. Wraps the module-level
  // ``buildTimezoneFormatter`` helper so every chart site (main,
  // wizard, backtest-candle, workspace panel) uses the same shape.
  // The returned closure reads ``state.timezone`` at call time, so
  // swapping the dropdown selection doesn't require re-building the
  // LWC instance — just an ``applyOptions`` re-hook.
  function _panelTimeFormatter(ts) {
    return buildTimezoneFormatter(state.timezone).full(ts);
  }
  function _panelTickMarkFormatter(ts) {
    return buildTimezoneFormatter(state.timezone).short(ts);
  }

  // ── Chart init ────────────────────────────────────────────────────
  function _initChart() {
    if (typeof window.LightweightCharts === 'undefined') return false;
    const LWC = window.LightweightCharts;
    const layoutFn = typeof _chartLayoutOpts === 'function' ? _chartLayoutOpts : null;
    const opts = layoutFn ? layoutFn() : {};
    state._chart = LWC.createChart(canvasHost, {
      ...opts,
      // Per-panel formatter overrides the global one from
      // _chartLayoutOpts so the timezone dropdown works
      // independently per panel. Shallow-merge: ``localization``
      // and ``timeScale`` are replaced whole (the latter merges
      // with the opts-provided ``timeVisible``/``secondsVisible``
      // only via LWC's own later applyOptions elsewhere — the
      // base values here are fine).
      localization: { timeFormatter: _panelTimeFormatter },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: _panelTickMarkFormatter,
      },
      width: canvasHost.clientWidth || 300,
      height: canvasHost.clientHeight || 200,
    });
    state._candleSeries = state._chart.addSeries(LWC.CandlestickSeries, {
      upColor: _cssVar('--accent', '#26a69a'),
      downColor: _cssVar('--red', '#ef5350'),
      borderUpColor: _cssVar('--accent', '#26a69a'),
      borderDownColor: _cssVar('--red', '#ef5350'),
      wickUpColor: _cssVar('--accent', '#26a69a'),
      wickDownColor: _cssVar('--red', '#ef5350'),
    });
    // Register with the module-scoped active-panels set so
    // ``_applyThemeToAllPanels`` (called from app.js's
    // ``_applyChartTheme`` after a theme switch) can re-apply the
    // layout + candle-series palette without needing a reference to
    // this particular factory instance.
    state._themeRegistryEntry = {
      chart: state._chart,
      candleSeries: state._candleSeries,
    };
    _activePanelCharts.add(state._themeRegistryEntry);
    _rebuildIndicatorSeries();
    if (typeof ResizeObserver !== 'undefined') {
      state._resizeObs = new ResizeObserver((entries) => {
        for (const e of entries) {
          if (e.target === canvasHost && state._chart) {
            const w = e.contentRect.width;
            const h = e.contentRect.height || 200;
            state._chart.applyOptions({ width: w, height: h });
            _renderAnnotations();
          } else if (e.target === panel) {
            // PR 5a: responsive sidebar — hide below 400 px panel
            // width. Toggling a class on ``panel`` lets the CSS own
            // the hide-rule without coupling to every caller. Chart
            // reclaims the full body width because ``.panel-chart-
            // sidebar`` collapses to ``display: none``.
            const narrow = e.contentRect.width < 400;
            panel.classList.toggle('panel-chart-narrow', narrow);
          }
        }
      });
      state._resizeObs.observe(canvasHost);
      state._resizeObs.observe(panel);
    }
    try {
      // Combined handler: render annotations on every range change
      // (they're pixel-positioned, so any pan/zoom needs a redraw)
      // + peek at the range to trigger scroll-to-load when the left
      // edge nears the data's start. Wrapping in one subscribe call
      // is cheaper than two separate ones and keeps the unsubscribe
      // single-purpose on destroy.
      state._rangeUnsub = state._chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        _renderAnnotations();
        if (!range || state._loadingMore || state._noMoreData) return;
        // Left-edge buffer: trigger when the visible range's start
        // is within 20 % of the loaded candles' left side. ``range.from``
        // is a logical index into the series — can go negative when
        // the user has scrolled past the oldest candle (empty space
        // on the left), which still satisfies ``from < 20 %``.
        const threshold = Math.max(1, state._candles.length * 0.20);
        if (range.from < threshold) _maybeLoadMoreHistory();
      });
    } catch (e) {}
    _installChartClickHandler();
    return true;
  }

  function _destroyIndicatorSeries() {
    if (!state._chart) return;
    for (const typ of Object.keys(state._indicatorSeries)) {
      const rec = state._indicatorSeries[typ];
      if (!rec) continue;
      for (const s of (rec.main || [])) {
        try { state._chart.removeSeries(s); } catch (e) {}
      }
      for (const s of (rec.paneSeries || [])) {
        try { state._chart.removeSeries(s); } catch (e) {}
      }
    }
    state._indicatorSeries = {};
  }

  function _rebuildIndicatorSeries() {
    if (!state._chart) return;
    _destroyIndicatorSeries();
    const LWC = window.LightweightCharts;
    // Pane allocation: RSI gets pane 1 if enabled, MACD the next one.
    let nextPane = 1;
    const panes = {};
    if (state.indicators.includes('RSI')) panes.RSI = nextPane++;
    if (state.indicators.includes('MACD')) panes.MACD = nextPane++;

    for (const t of state.indicators) {
      const rec = { main: [], paneSeries: [] };
      if (t === 'BOLLINGER') {
        rec.main.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--blue', '#5b8dee'), lineWidth: 1, priceLineVisible: false, lastValueVisible: false }));
        rec.main.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--muted', '#888'), lineWidth: 1, priceLineVisible: false, lastValueVisible: false }));
        rec.main.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--blue', '#5b8dee'), lineWidth: 1, priceLineVisible: false, lastValueVisible: false }));
      } else if (t === 'SUPERTREND') {
        rec.main.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--accent', '#26a69a'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }));
        rec.main.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--red', '#ef5350'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }));
      } else if (t === 'EMA') {
        rec.main.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--amber', '#ffb347'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }));
      } else if (t === 'RSI') {
        const p = panes.RSI;
        rec.paneSeries.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--blue', '#5b8dee'), lineWidth: 1, priceLineVisible: false, lastValueVisible: true }, p));
      } else if (t === 'MACD') {
        const p = panes.MACD;
        rec.paneSeries.push(state._chart.addSeries(LWC.HistogramSeries, { color: _cssVar('--muted', '#888') }, p));
        rec.paneSeries.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--blue', '#5b8dee'), lineWidth: 1 }, p));
        rec.paneSeries.push(state._chart.addSeries(LWC.LineSeries, { color: _cssVar('--amber', '#ffb347'), lineWidth: 1 }, p));
      }
      // SUPPORT_RESISTANCE / QFL / PARABOLIC_SAR rebuild their series
      // dynamically inside _renderIndicatorOverlays because the series
      // count depends on the detected segments, not the config.
      state._indicatorSeries[t] = rec;
    }
  }

  function _renderIndicatorOverlays() {
    if (!state._chart || !state._candleSeries || !state._candles.length) return;
    const candles = state._candles;
    const markers = [];

    const has = (t) => state.indicators.includes(t);
    const rec = (t) => state._indicatorSeries[t] || { main: [], paneSeries: [] };

    if (has('EMA')) {
      const d = PANEL_INDICATOR_DEFAULTS.EMA;
      const r = rec('EMA');
      if (r.main[0]) r.main[0].setData(calcEMALine(candles, d.period));
    }
    if (has('BOLLINGER')) {
      const d = PANEL_INDICATOR_DEFAULTS.BOLLINGER;
      const bb = calcBollingerLines(candles, d.period, d.multiplier);
      const r = rec('BOLLINGER');
      if (r.main[0]) r.main[0].setData(bb.upper);
      if (r.main[1]) r.main[1].setData(bb.middle);
      if (r.main[2]) r.main[2].setData(bb.lower);
    }
    if (has('SUPERTREND')) {
      const d = PANEL_INDICATOR_DEFAULTS.SUPERTREND;
      const st = calcSupertrendLines(candles, d.atr_period, d.multiplier);
      const r = rec('SUPERTREND');
      if (r.main[0]) r.main[0].setData(st.bull);
      if (r.main[1]) r.main[1].setData(st.bear);
    }
    if (has('RSI')) {
      const d = PANEL_INDICATOR_DEFAULTS.RSI;
      const r = rec('RSI');
      if (r.paneSeries[0]) r.paneSeries[0].setData(calcRSILine(candles, d.period));
    }
    if (has('MACD')) {
      const d = PANEL_INDICATOR_DEFAULTS.MACD;
      const m = calcMACDLines(candles, d.fast, d.slow, d.signal);
      const r = rec('MACD');
      if (r.paneSeries[0]) r.paneSeries[0].setData(m.histogram);
      if (r.paneSeries[1]) r.paneSeries[1].setData(m.macd);
      if (r.paneSeries[2]) r.paneSeries[2].setData(m.signal);
    }
    if (has('SUPPORT_RESISTANCE')) {
      const d = PANEL_INDICATOR_DEFAULTS.SUPPORT_RESISTANCE;
      const sr = calcSR(candles, d.left_bars, d.right_bars, d.volume_threshold, d.min_touches);
      _renderSegmentedLevels(sr.resSeries, '#e53935', 'SUPPORT_RESISTANCE', 'R', 'R');
      _renderSegmentedLevels(sr.supSeries, '#1e88e5', 'SUPPORT_RESISTANCE', 'S', 'S');
    }
    if (has('QFL')) {
      const d = PANEL_INDICATOR_DEFAULTS.QFL;
      const qfl = calcQFL(candles, d.base_periods, d.pump_periods, d.pump_pct, d.base_crack_pct);
      _renderSegmentedLevels(qfl.baseSeries, '#f050a0', 'QFL', null, null);
    }
    if (has('PARABOLIC_SAR')) {
      const d = PANEL_INDICATOR_DEFAULTS.PARABOLIC_SAR;
      const ps = calcParabolicSAR(candles, d.initial_af, d.max_af);
      const r = rec('PARABOLIC_SAR');
      for (const s of (r.main || [])) {
        try { state._chart.removeSeries(s); } catch (e) {}
      }
      r.main = [];
      const bullData = [], bearData = [];
      for (let i = 0; i < candles.length; i++) {
        if (ps.sarValues[i] === null) continue;
        const t = candles[i].time, v = ps.sarValues[i];
        if (ps.dirs[i] === 1) { bullData.push({ time: t, value: v }); }
        else                  { bearData.push({ time: t, value: v }); }
        if (i > 0 && ps.dirs[i] !== 0 && ps.dirs[i - 1] !== 0 && ps.dirs[i] !== ps.dirs[i - 1]) {
          markers.push({
            time: t,
            position: ps.dirs[i] === 1 ? 'belowBar' : 'aboveBar',
            shape: ps.dirs[i] === 1 ? 'arrowUp' : 'arrowDown',
            color: ps.dirs[i] === 1 ? '#26a69a' : '#ef5350',
            size: 1,
          });
        }
      }
      const LWC = window.LightweightCharts;
      const addSeriesWithDots = (data, color) => {
        if (!data.length) return;
        const s = state._chart.addSeries(LWC.LineSeries, {
          color: 'transparent', lineWidth: 0,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        s.setData(data);
        const csm = LWC.createSeriesMarkers;
        const dotMarkers = data.map((p) => ({ time: p.time, position: 'inBar', color, shape: 'circle', size: 1 }));
        if (csm) csm(s, dotMarkers);
        else { try { s.setMarkers(dotMarkers); } catch (e) {} }
        r.main.push(s);
      };
      addSeriesWithDots(bullData, 'rgba(51, 136, 187, 0.6)');
      addSeriesWithDots(bearData, 'rgba(253, 204, 2, 0.6)');
      state._indicatorSeries.PARABOLIC_SAR = r;
    }
    if (has('MARKET_STRUCTURE')) {
      const d = PANEL_INDICATOR_DEFAULTS.MARKET_STRUCTURE;
      const ms = calcMarketStructureMarkers(candles, d.lookback);
      for (const p of ms) markers.push(p);
    }

    state._indicatorMarkers = markers;
    _setCombinedMarkers();
  }

  // Indicator buckets are keyed by type by default, but S/R needs two
  // coexisting buckets (resistance + support) under the same indicator
  // type. ``subKey`` is optional; when present, the actual bucket key
  // becomes `${typeKey}__${subKey}` so each direction owns its own
  // series list and the self-cleanup loop in _renderSegmentedLevels
  // doesn't wipe the sibling direction's lines on re-render.
  function _getIndicatorBucketKeys(typeKey) {
    return Object.keys(state._indicatorSeries).filter(
      (k) => k === typeKey || k.startsWith(typeKey + '__'),
    );
  }

  function _renderSegmentedLevels(series, color, typeKey, subKey, labelStart) {
    const candles = state._candles;
    const LWC = window.LightweightCharts;
    const bucketKey = subKey ? `${typeKey}__${subKey}` : typeKey;
    const rec = state._indicatorSeries[bucketKey] || { main: [], paneSeries: [] };
    for (const s of (rec.main || [])) {
      try { state._chart.removeSeries(s); } catch (e) {}
    }
    rec.main = [];
    const segs = [];
    let segStart = null, segVal = null;
    for (let i = 0; i < candles.length; i++) {
      if (series[i] === null) continue;
      if (segVal === null) { segStart = i; segVal = series[i]; }
      else if (series[i] !== segVal) {
        segs.push({ start: segStart, end: i - 1, value: segVal });
        segStart = i; segVal = series[i];
      }
    }
    if (segVal !== null) segs.push({ start: segStart, end: candles.length - 1, value: segVal });
    for (const seg of segs) {
      const data = [];
      for (let j = seg.start; j <= seg.end; j++) data.push({ time: candles[j].time, value: seg.value });
      const s = state._chart.addSeries(LWC.LineSeries, {
        color, lineWidth: 2, lineStyle: typeKey === 'QFL' ? 2 : 0,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      s.setData(data);
      rec.main.push(s);
      if (labelStart && seg.end === candles.length - 1) {
        s.createPriceLine({
          price: seg.value, color, lineWidth: 0, lineStyle: 0,
          axisLabelVisible: true, title: labelStart,
        });
      }
    }
    state._indicatorSeries[bucketKey] = rec;
  }

  function _setCombinedMarkers() {
    if (!state._candleSeries) return;
    const combined = state._indicatorMarkers.concat(state._dealMarkers);
    combined.sort((a, b) => a.time - b.time);
    try {
      const LWC = window.LightweightCharts;
      const csm = LWC.createSeriesMarkers;
      if (csm) {
        if (state._markersPrimitive) state._markersPrimitive.setMarkers(combined);
        else state._markersPrimitive = csm(state._candleSeries, combined);
      } else {
        state._candleSeries.setMarkers(combined);
      }
    } catch (e) {}
  }

  async function _loadCandles() {
    if (!state._chart || !state._candleSeries) return;
    // Reset scroll-to-load state — a fresh candle set means the
    // left-edge marker is no longer accurate and any abort-in-flight
    // should drop its response on the floor.
    if (state._historyAbort) {
      try { state._historyAbort.abort(); } catch (e) {}
      state._historyAbort = null;
    }
    state._loadingMore = false;
    state._noMoreData = false;
    _toggleOverlay(loadingOverlay, false);
    _toggleOverlay(noMoreOverlay, false);

    let candles = [];
    try {
      const r = await fetch(`/api/chart/${_panelPairForUrl(state.pair)}/${state.timeframe}?limit=200`);
      if (r.ok) candles = await r.json();
    } catch (e) { /* keep last render */ return; }
    if (!Array.isArray(candles) || !candles.length) return;
    state._candles = candles;
    state._candleSeries.setData(candles);
    _renderIndicatorOverlays();
    _renderDealOverlays();
    _loadAnnotations();
  }

  function _toggleOverlay(el, show) {
    if (!el) return;
    el.classList.toggle('hidden', !show);
  }

  // Scroll-to-load-history: when the visible logical range's left
  // edge crosses into the first 20 % of loaded candles, fetch the
  // next-older batch via /api/candles and prepend to the series.
  // Keeps user-driven pan a seamless experience — by the time they
  // hit the true left edge, 500 older candles are usually already
  // in place. Cancelable via AbortController so timeframe/pair
  // switches don't race stale responses onto the next chart.
  async function _maybeLoadMoreHistory() {
    if (state._destroyed) return;
    if (state._loadingMore || state._noMoreData) return;
    if (!state._candles.length) return;
    state._loadingMore = true;
    _toggleOverlay(loadingOverlay, true);

    const batchSize = 500;
    const secPerBar = tfSeconds(state.timeframe);
    const oldest = state._candles[0].time;
    const endMs = oldest * 1000;
    const startMs = (oldest - batchSize * secPerBar) * 1000;
    const endIso = new Date(endMs).toISOString();
    const startIso = new Date(startMs).toISOString();

    const ctrl = new AbortController();
    state._historyAbort = ctrl;
    const url = `/api/candles/${_panelPairForUrl(state.pair)}/${state.timeframe}`
      + `?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`
      + `&limit=${batchSize}`;

    try {
      let r = await fetch(url, { signal: ctrl.signal });
      if (r.status === 429) {
        // Rate limit — wait out the typical per-minute window and
        // retry once. If it 429s again the user's scroll speed is
        // beyond what the backend allows; log + give up without
        // poisoning the no-more-data flag (they can pan again
        // later once the budget resets).
        await new Promise((resolve) => setTimeout(resolve, 2000));
        if (ctrl.signal.aborted) return;
        r = await fetch(url, { signal: ctrl.signal });
      }
      if (!r.ok) {
        console.warn('Scroll-to-load failed:', r.status);
        return;
      }
      const body = await r.json();
      // /api/candles returns {candles: [...], gaps: N} (unlike
      // /api/chart which returns a bare array). Pick the list out
      // defensively in case the backend evolves.
      const batch = Array.isArray(body) ? body
        : (body && Array.isArray(body.candles) ? body.candles : []);
      if (!batch.length) {
        state._noMoreData = true;
        _toggleOverlay(noMoreOverlay, true);
        return;
      }
      // Dedupe on time — the backend's window rounding can overlap
      // with our oldest candle by one bar.
      const prior = batch.filter((c) => c.time < oldest);
      if (!prior.length) {
        state._noMoreData = true;
        _toggleOverlay(noMoreOverlay, true);
        return;
      }
      state._candles = prior.concat(state._candles);
      state._candleSeries.setData(state._candles);
      // Indicator series read from state._candles on each render
      // call, so a full re-render on the larger dataset is the
      // right move (RSI / MACD / EMA all use the oldest-anchored
      // warm-up windows). Annotations + deal markers are stored
      // by timestamp and follow the new range automatically.
      _renderIndicatorOverlays();
      _renderDealOverlays();
      _renderAnnotations();
    } catch (e) {
      if (e && e.name === 'AbortError') return;  // expected on tf/pair switch
      console.warn('Scroll-to-load error:', e);
    } finally {
      if (state._historyAbort === ctrl) state._historyAbort = null;
      state._loadingMore = false;
      _toggleOverlay(loadingOverlay, false);
    }
  }

  // ── Deal overlays (bot-binding) ───────────────────────────────────
  function _clearDealOverlays() {
    if (state._candleSeries) {
      for (const pl of state._dealPriceLines) {
        try { state._candleSeries.removePriceLine(pl); } catch (e) {}
      }
    }
    state._dealPriceLines = [];
    state._dealMarkers = [];
    _setCombinedMarkers();
  }

  function _renderDealOverlays() {
    if (!state._candleSeries) return;
    _clearDealOverlays();
    if (!state.boundBotSlug) return;
    // TP/SL live in the YAML config, not the state snapshot — see
    // the detail-chart path in app.js for the same split (fetches
    // /api/bots/{slug}/config to get inner.take_profit.target_pct).
    const inner = (state._botConfig && state._botConfig.bot) || {};
    const tpPct = Number((inner.take_profit || {}).target_pct) || 0;
    const slCfg = inner.stop_loss || {};
    const slPct = Number(slCfg.pct) || 0;
    const slType = slCfg.type || 'fixed';
    const blue = _cssVar('--blue', '#5b8dee');
    const accent = _cssVar('--accent', '#26a69a');
    const red = _cssVar('--red', '#ef5350');
    const muted = _cssVar('--muted', '#888');
    const markers = [];

    // Open deals — entry markers + TP/SL price lines per deal.
    const open = state._lastOpenDeals || [];
    for (const d of open) {
      const avg = Number(d.avg_entry_price) || Number(d.entry_price) || 0;
      if (!avg) continue;
      try {
        state._dealPriceLines.push(state._candleSeries.createPriceLine({
          price: avg, color: blue, lineStyle: 0, lineWidth: 1,
          axisLabelVisible: true, title: 'Entry',
        }));
      } catch (e) {}
      if (tpPct > 0) {
        try {
          state._dealPriceLines.push(state._candleSeries.createPriceLine({
            price: avg * (1 + tpPct / 100),
            color: accent, lineStyle: 2, lineWidth: 1,
            axisLabelVisible: true, title: 'TP',
          }));
        } catch (e) {}
      }
      if (slPct > 0) {
        const slAnchor = (slType === 'trailing' && d._peak_price) ? Number(d._peak_price) : avg;
        try {
          state._dealPriceLines.push(state._candleSeries.createPriceLine({
            price: slAnchor * (1 - slPct / 100),
            color: red, lineStyle: 2, lineWidth: 1,
            axisLabelVisible: true, title: 'SL',
          }));
        } catch (e) {}
      }
      // Entry + DCA fills as markers when we have order rows.
      for (const o of (d.orders || [])) {
        const t = _panelIsoToUnix(o.placed_at);
        if (t == null) continue;
        const isBase = (o.order_type === 'base') || Number(o.order_number) === 1;
        markers.push({
          time: t, position: 'belowBar',
          color: isBase ? blue : '#ff9500', shape: 'circle',
          text: isBase ? 'BASE' : 'DCA',
        });
      }
    }

    // Closed deals — exit markers (from _closedDeals). We don't draw
    // TP/SL lines for closed deals because they'd stack up visually.
    for (const row of (state._closedDeals || [])) {
      if (!row || !row.closed_at) continue;
      const t = _panelIsoToUnix(row.closed_at);
      if (t == null) continue;
      const reason = String(row.close_reason || '').toLowerCase();
      let color = muted;
      if (reason === 'tp') color = accent;
      else if (reason === 'sl') color = red;
      markers.push({
        time: t, position: 'aboveBar',
        color, shape: 'arrowDown',
        text: reason ? reason.toUpperCase() : 'CLOSE',
      });
      // Entry marker for the closed deal, so the timeline makes sense.
      const entryT = _panelIsoToUnix(row.opened_at);
      const entryPrice = Number(row.avg_entry_price) || Number(row.entry_price) || 0;
      if (entryT != null && entryPrice > 0) {
        markers.push({
          time: entryT, position: 'belowBar',
          color: blue, shape: 'circle', text: 'ENTRY',
        });
      }
    }

    state._dealMarkers = markers;
    _setCombinedMarkers();
  }

  // ── Annotations ───────────────────────────────────────────────────
  function _annotSlug() {
    return `workspace-panel-${state.panelId}`;
  }

  function _ensureAnnotSvg() {
    if (state._annotSvg && state._annotSvg.parentNode === canvasHost) return state._annotSvg;
    const svg = _panelSvg('svg', { xmlns: PANEL_SVG_NS, class: 'panel-annot-svg' });
    svg.style.position = 'absolute';
    svg.style.inset = '0';
    svg.style.width = '100%';
    svg.style.height = '100%';
    svg.style.pointerEvents = 'none';
    svg.style.zIndex = '10';
    svg.style.display = 'block';
    canvasHost.appendChild(svg);
    state._annotSvg = svg;
    return svg;
  }

  function _xOfTime(t) {
    if (!state._chart || t == null) return null;
    try {
      const x = state._chart.timeScale().timeToCoordinate(Number(t));
      return Number.isFinite(x) ? x : null;
    } catch (e) { return null; }
  }
  function _yOfPrice(p) {
    if (!state._candleSeries || p == null) return null;
    try {
      const y = state._candleSeries.priceToCoordinate(Number(p));
      return Number.isFinite(y) ? y : null;
    } catch (e) { return null; }
  }

  function _renderAnnotations() {
    const svg = _ensureAnnotSvg();
    if (!svg) return;
    const w = canvasHost.clientWidth, h = canvasHost.clientHeight;
    svg.setAttribute('width', String(w));
    svg.setAttribute('height', String(h));
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const blue   = _cssVar('--blue',   '#5b8dee');
    const amber  = _cssVar('--amber',  '#ffb347');
    const accent = _cssVar('--accent', '#26a69a');
    const defaultColorFor = (type) => {
      if (type === 'text') return amber;
      if (type === 'hline') return amber;
      if (type === 'measure') return accent;
      return blue;  // arrow, trendline, unknown
    };
    for (const a of (state._annotations || [])) {
      const color = a.color || defaultColorFor(a.type);
      if (a.type === 'hline') {
        // Horizontal line: price-only annotation. Spans the full
        // canvas width at y = _yOfPrice(y1); a small right-side
        // label shows the price so stacked lines stay
        // distinguishable. x1 was stored as the click's timestamp
        // for debuggability but is not consulted here.
        const y = _yOfPrice(a.y1);
        if (y == null) continue;
        const g = _panelSvg('g', { 'data-ann-id': a.id });
        g.appendChild(_panelSvg('line', {
          x1: 0, y1: y, x2: w, y2: y,
          stroke: color, 'stroke-width': 1, 'stroke-dasharray': '4 3',
        }));
        const priceTxt = typeof a.y1 === 'number'
          ? _fmtSidebarPrice(a.y1) : String(a.y1 || '');
        const tw = Math.max(28, priceTxt.length * 7 + 6);
        g.appendChild(_panelSvg('rect', {
          x: w - tw - 4, y: y - 8, width: tw, height: 14,
          fill: 'rgba(0,0,0,0.6)', rx: 2,
        }));
        const t = _panelSvg('text', {
          x: w - 4 - 3, y: y + 3, fill: color,
          'font-family': 'monospace', 'font-size': 10, 'text-anchor': 'end',
        });
        t.textContent = priceTxt;
        g.appendChild(t);
        svg.appendChild(g);
        continue;
      }
      const x1 = _xOfTime(a.x1);
      const y1 = _yOfPrice(a.y1);
      if (x1 == null || y1 == null) continue;
      if (a.type === 'text') {
        const g = _panelSvg('g', { 'data-ann-id': a.id });
        g.appendChild(_panelSvg('circle', { cx: x1, cy: y1, r: 4, fill: color, stroke: '#ffffff', 'stroke-width': 1 }));
        const text = _panelSvg('text', { x: x1 + 8, y: y1 + 4, fill: color, 'font-family': 'monospace', 'font-size': 11 });
        text.textContent = a.label || 'text';
        g.appendChild(text);
        svg.appendChild(g);
      } else if (a.type === 'arrow' || a.type === 'trendline' || a.type === 'measure') {
        const x2 = _xOfTime(a.x2);
        const y2 = _yOfPrice(a.y2);
        if (x2 == null || y2 == null) continue;
        const g = _panelSvg('g', { 'data-ann-id': a.id });
        const lineAttrs = { x1, y1, x2, y2, stroke: color, 'stroke-width': 2 };
        if (a.type === 'measure') lineAttrs['stroke-dasharray'] = '4 3';
        g.appendChild(_panelSvg('line', lineAttrs));
        if (a.type === 'arrow') {
          // Arrowhead — shared with the original PR 3b code path.
          const dx = x2 - x1, dy = y2 - y1;
          const len = Math.sqrt(dx * dx + dy * dy);
          if (len > 0.01) {
            const ux = dx / len, uy = dy / len;
            const bx = x2 - ux * 10, by = y2 - uy * 10;
            const px = -uy * 5, py = ux * 5;
            g.appendChild(_panelSvg('polygon', { points: `${x2},${y2} ${bx + px},${by + py} ${bx - px},${by - py}`, fill: color }));
          }
        } else if (a.type === 'measure') {
          // Measure: endpoint circles + midpoint label carrying the
          // absolute + percentage delta. Matches the main-chart
          // measure session visualisation from app.js.
          g.appendChild(_panelSvg('circle', { cx: x1, cy: y1, r: 3, fill: color }));
          g.appendChild(_panelSvg('circle', { cx: x2, cy: y2, r: 3, fill: color }));
          const p1 = Number(a.y1), p2 = Number(a.y2);
          if (Number.isFinite(p1) && Number.isFinite(p2) && p1 !== 0) {
            const abs = p2 - p1;
            const pct = (abs / p1) * 100;
            const sign = abs >= 0 ? '+' : '';
            const label = `${sign}${pct.toFixed(2)}% / ${sign}${abs.toFixed(2)}`;
            const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
            const tw = label.length * 6.5 + 8;
            g.appendChild(_panelSvg('rect', {
              x: mx - tw / 2, y: my - 16, width: tw, height: 14,
              fill: 'rgba(0,0,0,0.7)', rx: 2,
            }));
            const text = _panelSvg('text', {
              x: mx, y: my - 5, fill: color,
              'font-family': 'monospace', 'font-size': 11, 'text-anchor': 'middle',
            });
            text.textContent = label;
            g.appendChild(text);
          }
        }
        // Trendline has no decoration beyond the line itself.
        svg.appendChild(g);
      }
    }
  }

  async function _loadAnnotations() {
    state._annotations = [];
    try {
      const r = await fetch(
        `/api/db/annotations?bot_slug=${encodeURIComponent(_annotSlug())}&timeframe=${encodeURIComponent(state.timeframe)}`,
        { credentials: 'same-origin' },
      );
      if (r.ok) {
        const body = await r.json();
        if (Array.isArray(body)) state._annotations = body;
      }
    } catch (e) { /* keep empty */ }
    _renderAnnotations();
  }

  async function _persistAnnotation(fields) {
    const body = Object.assign({
      bot_slug: _annotSlug(),
      timeframe: state.timeframe,
    }, fields);
    let r;
    try {
      r = await fetch('/api/db/annotations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (e) { return; }
    if (!r.ok) return;
    try {
      const j = await r.json();
      if (j && Number.isFinite(j.id)) {
        state._annotations.push(Object.assign({}, body, { id: j.id }));
        _renderAnnotations();
        return;
      }
    } catch (e) {}
    await _loadAnnotations();
  }

  async function _deleteAnnotationNear(point) {
    if (!state._annotations.length) return;
    let tMin = Infinity, tMax = -Infinity, pMin = Infinity, pMax = -Infinity;
    for (const a of state._annotations) {
      // Skip hlines from the x-axis span calculation — their x1 is
      // the click timestamp at draw time, which can pollute the
      // tMin/tMax if the user drew a very old or very new hline.
      if (a.type !== 'hline') {
        if (a.x1 != null) { tMin = Math.min(tMin, a.x1); tMax = Math.max(tMax, a.x1); }
        if (a.x2 != null) { tMin = Math.min(tMin, a.x2); tMax = Math.max(tMax, a.x2); }
      }
      if (a.y1 != null) { pMin = Math.min(pMin, a.y1); pMax = Math.max(pMax, a.y1); }
      if (a.y2 != null) { pMin = Math.min(pMin, a.y2); pMax = Math.max(pMax, a.y2); }
    }
    const tSpan = Math.max(1, tMax - tMin);
    const pSpan = Math.max(1, pMax - pMin);
    let best = null, bestD = Infinity;
    for (const a of state._annotations) {
      let d;
      if (a.type === 'hline') {
        // Horizontal line: x is irrelevant (line spans full canvas).
        // Distance is purely the normalised price-delta.
        const dp = ((Number(a.y1) || point.price) - point.price) / pSpan;
        d = dp * dp;
      } else {
        const dt = ((Number(a.x1) || point.time) - point.time) / tSpan;
        const dp = ((Number(a.y1) || point.price) - point.price) / pSpan;
        d = dt * dt + dp * dp;
      }
      if (d < bestD) { bestD = d; best = a; }
    }
    if (!best) return;
    try {
      const r = await fetch(`/api/db/annotations/${best.id}`, { method: 'DELETE' });
      if (r.ok) {
        state._annotations = state._annotations.filter((a) => a.id !== best.id);
        _renderAnnotations();
      }
    } catch (e) {}
  }

  async function _clearAllAnnotations() {
    if (!window.confirm('Delete all annotations for this panel? This cannot be undone.')) return;
    const url = '/api/db/annotations/all'
      + `?bot_slug=${encodeURIComponent(_annotSlug())}`
      + `&timeframe=${encodeURIComponent(state.timeframe)}`;
    try {
      const r = await fetch(url, { method: 'DELETE' });
      if (!r.ok) return;
      state._annotations = [];
      _renderAnnotations();
    } catch (e) {}
  }

  function _setActiveTool(name) {
    state._activeTool = name || 'select';
    state._toolFirstPoint = null;
    for (const k of Object.keys(toolbarBtns)) {
      toolbarBtns[k].classList.toggle('active', k === state._activeTool);
    }
  }

  for (const key of ['arrow', 'trendline', 'hline', 'measure', 'text', 'delete']) {
    toolbarBtns[key].addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (state._activeTool === key) _setActiveTool('select');
      else _setActiveTool(key);
    });
  }
  toolbarBtns['clear-all'].addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    _clearAllAnnotations();
  });

  function _installChartClickHandler() {
    if (!state._chart || !state._candleSeries) return;
    const ts = state._chart.timeScale();
    const handle = (param) => {
      if (state._activeTool === 'select') return;
      if (!param || !param.point) return;
      const { x, y } = param.point;
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      let t = param.time;
      if (t == null) {
        try { t = ts.coordinateToTime(x); } catch (e) { t = null; }
      }
      if (t == null) return;
      const price = state._candleSeries.coordinateToPrice(y);
      if (!Number.isFinite(price)) return;
      const point = { time: Number(t), price: Number(price) };
      if (state._activeTool === 'text') {
        const label = window.prompt('Label?');
        if (label) {
          _persistAnnotation({
            type: 'text', x1: point.time, y1: point.price,
            label, color: _cssVar('--amber', '#ffb347'),
          });
        }
        _setActiveTool('select');
        return;
      }
      // Two-click tools share the same "first click stashes the
      // point, second click persists the annotation" flow. Only the
      // colour + type field change per tool.
      const twoClickSpecs = {
        arrow:     { type: 'arrow',     color: _cssVar('--blue',   '#5b8dee') },
        trendline: { type: 'trendline', color: _cssVar('--blue',   '#5b8dee') },
        measure:   { type: 'measure',   color: _cssVar('--accent', '#26a69a') },
      };
      const twoClick = twoClickSpecs[state._activeTool];
      if (twoClick) {
        if (!state._toolFirstPoint) {
          state._toolFirstPoint = point;
        } else {
          _persistAnnotation({
            type: twoClick.type,
            x1: state._toolFirstPoint.time, y1: state._toolFirstPoint.price,
            x2: point.time, y2: point.price,
            color: twoClick.color,
          });
          state._toolFirstPoint = null;
          _setActiveTool('select');
        }
        return;
      }
      if (state._activeTool === 'hline') {
        // Single-click horizontal line: x values are irrelevant to
        // rendering (the line spans the full canvas), but the
        // backend requires x1 in [0, 2_000_000_000]. Store the
        // click's timestamp so the row remains debuggable — "when
        // did the operator draw this" — without affecting the
        // render.
        _persistAnnotation({
          type: 'hline',
          x1: point.time, y1: point.price,
          color: _cssVar('--amber', '#ffb347'),
        });
        _setActiveTool('select');
        return;
      }
      if (state._activeTool === 'delete') {
        _deleteAnnotationNear(point);
        _setActiveTool('select');
      }
    };
    try { state._chart.subscribeClick(handle); } catch (e) {}
  }

  // Shared by setBinding + init. ``silent`` suppresses onConfigChange so
  // hydrating the handle from a stored layout doesn't feed straight
  // back into _queueWorkspaceSave — the loaded layout is already on
  // disk and re-saving it would be wasted work on every page-load.
  async function _applyBinding(slug, userId, silent) {
    state.boundBotSlug = slug || null;
    state.boundBotUserId = userId || null;
    _updateTitle();
    if (!slug) {
      state._botState = null;
      state._botConfig = null;
      state._lastOpenDeals = [];
      state._closedDeals = [];
      _clearDealOverlays();
      if (!silent && state.onConfigChange) state.onConfigChange();
      return;
    }
    // Three parallel fetches: state (for open_deals), YAML config
    // (for TP/SL percentages — state.json doesn't carry these), and
    // closed-deal history (for exit markers). All three tolerate a
    // 4xx quietly so a lost permission / deleted bot doesn't nuke
    // the chart; the overlay just renders with whatever came back.
    let botState = null, botConfig = null, closed = [];
    try {
      const [bs, cfg, cls] = await Promise.all([
        fetch(`/api/bots/${encodeURIComponent(slug)}`).then((r) => r.ok ? r.json() : null),
        fetch(`/api/bots/${encodeURIComponent(slug)}/config`).then((r) => r.ok ? r.json() : null),
        fetch(`/api/db/deals?bot_slug=${encodeURIComponent(slug)}&status=closed&limit=100`)
          .then((r) => r.ok ? r.json() : []),
      ]);
      botState = bs;
      botConfig = cfg;
      closed = Array.isArray(cls) ? cls.map((x) => x && x.deal ? x.deal : x) : [];
    } catch (e) { /* render with whatever we got */ }
    state._botState = botState;
    state._botConfig = botConfig;
    state._lastOpenDeals = (botState && botState.open_deals) || [];
    state._closedDeals = closed;
    _renderDealOverlays();
    if (!silent && state.onConfigChange) state.onConfigChange();
  }

  // ── Public handle ─────────────────────────────────────────────────
  const api = {
    panelId: state.panelId,
    get element() { return panel; },

    async init() {
      _updateTitle();
      if (!_initChart()) return;
      await _loadCandles();
      if (state.boundBotSlug) {
        // Re-hydrate the binding so deal markers render on page-load.
        // Silent: layout came off disk, no need to round-trip it back.
        await _applyBinding(state.boundBotSlug, state.boundBotUserId, true);
      } else {
        _loadAnnotations();
      }
      // Periodic refresh: candles are paged through the backend's
      // per-timeframe cache so the fetch is cheap, and the 30s cadence
      // matches the main chart tab. Deal overlays refresh via WS on
      // bot_state pushes; this timer is only about candle data.
      state._refreshTimer = setInterval(() => {
        if (!state._destroyed) _loadCandles();
      }, 30000);
      // Info-sidebar poll — 5 s cadence, backend 10 s cache keeps N
      // panels on the same pair at 1 upstream call per window. An
      // immediate fetch on init so the sidebar doesn't sit on "—"
      // for the first 5 s after page-load.
      _tickerFetch();
      state._tickerTimer = setInterval(() => {
        if (!state._destroyed) _tickerFetch();
      }, 5000);
    },

    async setTimeframe(tf) {
      if (!tf || tf === state.timeframe) return;
      state.timeframe = tf;
      _updateTitle();
      await _loadCandles();
      if (state.onConfigChange) state.onConfigChange();
    },

    async setPair(pair) {
      const np = _panelNormalizePair(pair);
      if (!np || np === state.pair) return;
      state.pair = np;
      sidebar.updatePair(state.pair);
      _updateTitle();
      await _loadCandles();
      // Pair change → ticker cache is keyed on pair, old reading is
      // wrong, force an immediate refetch rather than wait for the
      // 5 s poll.
      _tickerFetch();
      if (state.onConfigChange) state.onConfigChange();
    },

    toggleIndicator(name, enabled) {
      if (!PANEL_INDICATOR_TYPES.includes(name)) return;
      const has = state.indicators.includes(name);
      if (enabled && !has) state.indicators.push(name);
      else if (!enabled && has) state.indicators = state.indicators.filter((x) => x !== name);
      else return;
      _rebuildIndicatorSeries();
      _renderIndicatorOverlays();
      if (state.onConfigChange) state.onConfigChange();
    },

    async setBinding(slug, userId) {
      return _applyBinding(slug, userId, /* silent */ false);
    },

    async clearBinding() {
      await _applyBinding(null, null, false);
    },

    handleStateUpdate(payload) {
      if (!state.boundBotSlug) return;
      if (!payload || payload.slug !== state.boundBotSlug) return;
      const data = payload.data || {};
      state._botState = data;
      state._lastOpenDeals = Array.isArray(data.open_deals) ? data.open_deals : [];
      // Closed-deals list is intentionally untouched here — /ws/state
      // only surfaces current open_deals. A deal that just closed is
      // handled lazily: its entry marker drops from the open set on
      // the next _renderDealOverlays, and the exit marker shows up on
      // the next setBinding refresh (or page reload). That matches the
      // v26-16 audit's "eventually consistent" stance for closed-deal
      // history without requiring a DB round-trip on every state push.
      _renderDealOverlays();
    },

    resize() {
      if (!state._chart) return;
      try {
        state._chart.applyOptions({
          width: canvasHost.clientWidth,
          height: canvasHost.clientHeight,
        });
        _renderAnnotations();
      } catch (e) {}
    },

    getConfig() {
      return {
        pair: state.pair,
        timeframe: state.timeframe,
        indicators: state.indicators.slice(),
        boundBotSlug: state.boundBotSlug,
        boundBotUserId: state.boundBotUserId,
        // ``timezone`` supersedes the legacy ``useUtc`` field from
        // PR 5b. Old saved layouts with ``useUtc: true`` migrate on
        // load; writes always produce ``timezone`` going forward.
        timezone: state.timezone,
      };
    },

    async destroy() {
      if (state._destroyed) return;
      state._destroyed = true;
      if (state._refreshTimer) { clearInterval(state._refreshTimer); state._refreshTimer = null; }
      if (state._tickerTimer) { clearInterval(state._tickerTimer); state._tickerTimer = null; }
      if (state._historyAbort) {
        try { state._historyAbort.abort(); } catch (e) {}
        state._historyAbort = null;
      }
      // LWC v5's subscribeVisibleLogicalRangeChange returns the
      // disposer via the subscribe call's return value on the
      // timeScale instance — but v5.1.0 doesn't expose an
      // explicit unsubscribe. The chart.remove() call below tears
      // the timeScale down with it, so cleanup is implicit — we
      // just drop our reference for clarity.
      state._rangeUnsub = null;
      if (state._resizeObs) { try { state._resizeObs.disconnect(); } catch (e) {} state._resizeObs = null; }
      if (state._outsideClickCloser) {
        document.removeEventListener('mousedown', state._outsideClickCloser);
        state._outsideClickCloser = null;
      }
      if (state._themeRegistryEntry) {
        _activePanelCharts.delete(state._themeRegistryEntry);
        // Clear refs on the entry so a mid-flight theme-switch after
        // destroy finds an obvious null rather than the released
        // LWC instance.
        state._themeRegistryEntry.chart = null;
        state._themeRegistryEntry.candleSeries = null;
        state._themeRegistryEntry = null;
      }
      // Cascade annotations: workspace-panel-<id> rows have no other
      // home, so drop them server-side alongside the panel.
      try {
        await fetch(
          `/api/db/annotations/all?bot_slug=${encodeURIComponent(_annotSlug())}`,
          { method: 'DELETE' },
        );
      } catch (e) { /* best-effort: leaving orphan rows is harmless */ }
      try { if (state._chart) state._chart.remove(); } catch (e) {}
      state._chart = null;
      state._candleSeries = null;
      state._indicatorSeries = {};
      state._markersPrimitive = null;
      state._annotSvg = null;
      // Leave the panel DOM in place — the workspace grid owns the
      // grid-stack-item wrapper and removes it via removeWidget().
    },
  };
  return api;
}

// ── Public namespace ──────────────────────────────────────────────────────
// ── Workspace open-deals-panel factory ────────────────────────────────────
// PR 4: a "global" (no bot-binding) panel that lists all currently-open
// deals across the user's bots in a sortable, column-configurable
// table. Each open-deals-panel keeps its own visibleColumns /
// columnOrder / sort inside layout_json — two panels in the same
// workspace show the same data-set but can be configured
// independently.
//
// Column definitions come in via config.columnDefs (app.js passes
// ACTIVE_DEALS_COLUMNS here, so cell renderers + sortValue
// extractors stay in one place). Action-button clicks ride the same
// document-level ``.deal-btn`` delegate that the Active Deals page
// uses — no extra listeners per panel.

const OPEN_DEALS_DEFAULT_VISIBLE = ['bot', 'pair', 'pnl_pct', 'age', 'actions'];
const OPEN_DEALS_REFETCH_DEBOUNCE_MS = 500;

function _odStableSort(rows, sort, colDefsMap) {
  if (!sort || !rows || !rows.length) return rows;
  const { key, dir } = sort;
  const mult = dir === 'asc' ? 1 : -1;
  const colDef = colDefsMap.get(key);
  const extract = colDef && typeof colDef.sortValue === 'function'
    ? colDef.sortValue
    : (r) => (r == null ? null : r[key]);
  const withIdx = rows.map((r, i) => [extract(r), i, r]);
  withIdx.sort((a, b) => {
    const va = a[0], vb = b[0];
    if (va == null && vb == null) return a[1] - b[1];
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'number' && typeof vb === 'number') {
      if (va === vb) return a[1] - b[1];
      return (va - vb) * mult;
    }
    const sa = String(va).toLowerCase();
    const sb = String(vb).toLowerCase();
    if (sa === sb) return a[1] - b[1];
    return (sa < sb ? -1 : 1) * mult;
  });
  return withIdx.map((p) => p[2]);
}

function createOpenDealsPanel(container, config) {
  const cfg = config || {};
  const columnDefs = Array.isArray(cfg.columnDefs) ? cfg.columnDefs : [];
  const allKeys = columnDefs.map((c) => c.key);
  const defsMap = new Map(columnDefs.map((c) => [c.key, c]));

  // Resolve visibleColumns + columnOrder, defaulting when the saved
  // layout is new / missing either. Unknown keys (older layout from
  // a build with a different column set) are filtered out so the
  // table can't crash on a dangling key.
  const visibleInit = Array.isArray(cfg.visibleColumns) && cfg.visibleColumns.length
    ? cfg.visibleColumns.filter((k) => defsMap.has(k))
    : OPEN_DEALS_DEFAULT_VISIBLE.filter((k) => defsMap.has(k));
  const orderInit = Array.isArray(cfg.columnOrder) && cfg.columnOrder.length
    ? [
        // Keep stored order for keys we still know about, then append
        // any columns that were added to the default set since the
        // layout was saved (so new columns surface as hidden-but-
        // orderable after the known tail).
        ...cfg.columnOrder.filter((k) => defsMap.has(k)),
        ...allKeys.filter((k) => !cfg.columnOrder.includes(k)),
      ]
    : allKeys.slice();

  const state = {
    panelId: cfg.panelId,
    visibleColumns: visibleInit.slice(),
    columnOrder: orderInit.slice(),
    sort: cfg.sort && cfg.sort.key && (cfg.sort.dir === 'asc' || cfg.sort.dir === 'desc')
      ? { key: cfg.sort.key, dir: cfg.sort.dir }
      : null,
    onRemove: typeof cfg.onRemove === 'function' ? cfg.onRemove : null,
    onConfigChange: typeof cfg.onConfigChange === 'function' ? cfg.onConfigChange : null,
    _rows: [],
    _destroyed: false,
    _fetchInFlight: false,
    _debounceTimer: null,
    _lastHeaderDragEndAt: 0,
    _popoverOpen: false,
  };

  // ── DOM scaffold ──────────────────────────────────────────────────
  const panel = document.createElement('div');
  panel.className = 'panel panel-open-deals';

  const header = document.createElement('div');
  header.className = 'panel-header';
  const titleWrap = document.createElement('div');
  titleWrap.className = 'panel-title-wrap';
  const title = document.createElement('span');
  title.className = 'panel-title';
  title.textContent = 'Open deals';
  const subtitle = document.createElement('span');
  subtitle.className = 'panel-subtitle';
  subtitle.style.marginLeft = '8px';
  titleWrap.appendChild(title);
  titleWrap.appendChild(subtitle);

  const headerRight = document.createElement('div');
  headerRight.className = 'panel-header-right';
  const settingsBtn = document.createElement('button');
  settingsBtn.type = 'button';
  settingsBtn.className = 'panel-settings-btn';
  settingsBtn.setAttribute('aria-label', 'Panel settings');
  settingsBtn.textContent = '⚙';
  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'panel-remove';
  removeBtn.setAttribute('aria-label', 'Remove panel');
  removeBtn.textContent = '×';
  headerRight.appendChild(settingsBtn);
  headerRight.appendChild(removeBtn);

  header.appendChild(titleWrap);
  header.appendChild(headerRight);

  const body = document.createElement('div');
  body.className = 'panel-body panel-open-deals-body';

  const table = document.createElement('table');
  table.className = 'panel-deals-table';
  const thead = document.createElement('thead');
  const theadRow = document.createElement('tr');
  thead.appendChild(theadRow);
  const tbody = document.createElement('tbody');
  table.appendChild(thead);
  table.appendChild(tbody);
  body.appendChild(table);

  const popover = document.createElement('div');
  popover.className = 'panel-settings-popover hidden';

  panel.appendChild(header);
  panel.appendChild(body);
  panel.appendChild(popover);
  container.appendChild(panel);

  removeBtn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (state.onRemove) state.onRemove();
  });

  // ── Rendering ─────────────────────────────────────────────────────
  function _orderedVisibleKeys() {
    // Column order controls position; visibility is a separate set.
    // Filter the order list down to keys that are (a) still known
    // and (b) currently visible.
    const visible = new Set(state.visibleColumns);
    return state.columnOrder.filter((k) => defsMap.has(k) && visible.has(k));
  }

  function _renderHead() {
    const keys = _orderedVisibleKeys();
    theadRow.innerHTML = keys.map((k) => {
      const def = defsMap.get(k);
      const label = (def && def.label) || '';
      const isSorted = state.sort && state.sort.key === k;
      const arrow = isSorted ? (state.sort.dir === 'asc' ? '▲' : '▼') : '';
      const sortable = label !== '' && typeof def.sortValue === 'function';
      const sortedCls = isSorted ? ` sorted sorted-${state.sort.dir}` : '';
      const sortableCls = sortable ? ' sortable' : '';
      return `<th draggable="true" data-col-key="${_odEscape(k)}"${sortable ? ' data-sortable="1"' : ''} class="${(sortedCls + sortableCls).trim()}">`
        + `<span class="col-label">${_odEscape(label)}</span>`
        + `<span class="col-sort-arrow">${arrow}</span>`
        + `</th>`;
    }).join('');
    _attachHeadHandlers();
  }

  function _renderBody() {
    const keys = _orderedVisibleKeys();
    const colSpan = Math.max(1, keys.length);
    const sorted = _odStableSort(state._rows, state.sort, defsMap);
    if (!sorted.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="${colSpan}">No open deals across any bot</td></tr>`;
      return;
    }
    tbody.innerHTML = sorted.map((row) => {
      const cells = keys.map((k) => {
        const def = defsMap.get(k);
        return def && typeof def.cell === 'function' ? def.cell(row) : '<td></td>';
      }).join('');
      return `<tr>${cells}</tr>`;
    }).join('');
  }

  function _renderSubtitle() {
    const n = state._rows.length;
    subtitle.textContent = `${n} open`;
  }

  function _render() {
    _renderSubtitle();
    _renderHead();
    _renderBody();
  }

  // ── Head event handlers (sort + drag-to-reorder) ─────────────────
  function _attachHeadHandlers() {
    const ths = Array.from(theadRow.querySelectorAll('th'));
    ths.forEach((th) => {
      // Drag-to-reorder — mirrors _attachHeaderDragHandlers in app.js
      // but uses panel-local state (columnOrder) instead of the
      // localStorage-backed loadColumns/saveColumns pair.
      th.addEventListener('dragstart', (e) => {
        e.dataTransfer.effectAllowed = 'move';
        try { e.dataTransfer.setData('text/plain', th.dataset.colKey || ''); } catch (err) {}
        th.classList.add('dragging');
      });
      th.addEventListener('dragend', () => {
        th.classList.remove('dragging');
        ths.forEach((x) => x.classList.remove('drop-before', 'drop-after'));
        state._lastHeaderDragEndAt = Date.now();
      });
      th.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        const half = th.offsetWidth / 2;
        const before = e.offsetX < half;
        ths.forEach((x) => x.classList.remove('drop-before', 'drop-after'));
        th.classList.add(before ? 'drop-before' : 'drop-after');
      });
      th.addEventListener('dragleave', () => {
        th.classList.remove('drop-before', 'drop-after');
      });
      th.addEventListener('drop', (e) => {
        e.preventDefault();
        ths.forEach((x) => x.classList.remove('drop-before', 'drop-after'));
        let srcKey = '';
        try { srcKey = e.dataTransfer.getData('text/plain'); } catch (err) {}
        const dstKey = th.dataset.colKey;
        if (!srcKey || !dstKey || srcKey === dstKey) return;
        const a = state.columnOrder.indexOf(srcKey);
        const b = state.columnOrder.indexOf(dstKey);
        if (a < 0 || b < 0) return;
        // Swap the two keys — matches the app.js helper's semantics
        // so hidden columns keep their original slots instead of
        // shuffling into surprise positions on toggle-back.
        const next = state.columnOrder.slice();
        [next[a], next[b]] = [next[b], next[a]];
        state.columnOrder = next;
        _render();
        if (state.onConfigChange) state.onConfigChange();
      });

      // Click-to-sort — asc → desc → unsorted cycle per column.
      if (th.dataset.sortable !== '1') return;
      th.addEventListener('click', (e) => {
        if (Date.now() - state._lastHeaderDragEndAt < 250) return;
        if (e.defaultPrevented) return;
        const key = th.dataset.colKey;
        if (!key) return;
        let next;
        if (!state.sort || state.sort.key !== key) next = { key, dir: 'asc' };
        else if (state.sort.dir === 'asc')         next = { key, dir: 'desc' };
        else                                        next = null;
        state.sort = next;
        _render();
        if (state.onConfigChange) state.onConfigChange();
      });
    });
  }

  // ── Settings popover ─────────────────────────────────────────────
  function _buildPopover() {
    popover.innerHTML = '';
    const label = document.createElement('div');
    label.className = 'form-row form-row-block';
    const lbl = document.createElement('label');
    lbl.textContent = 'Visible columns';
    label.appendChild(lbl);
    const grid = document.createElement('div');
    grid.className = 'panel-ind-grid';
    // Iterate by column order so the checkboxes follow the actual
    // table order — drag-to-reorder in the header also updates this
    // list on the next open.
    for (const k of state.columnOrder) {
      const def = defsMap.get(k);
      if (!def) continue;
      const item = document.createElement('label');
      item.className = 'panel-ind-item';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.colKey = k;
      cb.checked = state.visibleColumns.includes(k);
      item.appendChild(cb);
      const span = document.createElement('span');
      span.textContent = def.label || '(actions)';
      item.appendChild(span);
      grid.appendChild(item);
    }
    label.appendChild(grid);
    popover.appendChild(label);

    const actions = document.createElement('div');
    actions.className = 'panel-settings-actions';
    const apply = document.createElement('button');
    apply.type = 'button';
    apply.className = 'hbtn hbtn-theme btn-accent';
    apply.textContent = 'Apply';
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'hbtn hbtn-theme';
    close.textContent = 'Close';
    actions.appendChild(apply);
    actions.appendChild(close);
    popover.appendChild(actions);

    close.addEventListener('click', (e) => {
      e.preventDefault();
      popover.classList.add('hidden');
      state._popoverOpen = false;
    });
    apply.addEventListener('click', (e) => {
      e.preventDefault();
      const next = Array.from(grid.querySelectorAll('input[type=checkbox]'))
        .filter((c) => c.checked)
        .map((c) => c.dataset.colKey);
      const changed = next.length !== state.visibleColumns.length
        || next.some((k, i) => state.visibleColumns[i] !== k);
      state.visibleColumns = next;
      _render();
      popover.classList.add('hidden');
      state._popoverOpen = false;
      if (changed && state.onConfigChange) state.onConfigChange();
    });
  }

  settingsBtn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (popover.classList.contains('hidden')) {
      _buildPopover();
      popover.classList.remove('hidden');
      state._popoverOpen = true;
    } else {
      popover.classList.add('hidden');
      state._popoverOpen = false;
    }
  });

  // ── Data fetch + refresh ─────────────────────────────────────────
  async function _refetch() {
    if (state._destroyed || state._fetchInFlight) return;
    state._fetchInFlight = true;
    try {
      const r = await fetch('/api/bots');
      if (!r.ok) return;
      const d = await r.json();
      if (state._destroyed) return;
      state._rows = Array.isArray(d && d.all_open_deals) ? d.all_open_deals : [];
      _render();
    } catch (e) {
      // Network blip — leave the last-rendered rows in place. The
      // next WS push or manual settings-apply will re-kick the
      // fetcher, so we don't need a retry loop here.
    } finally {
      state._fetchInFlight = false;
    }
  }

  function _scheduleRefetch() {
    if (state._destroyed) return;
    if (state._debounceTimer) clearTimeout(state._debounceTimer);
    state._debounceTimer = setTimeout(() => {
      state._debounceTimer = null;
      _refetch();
    }, OPEN_DEALS_REFETCH_DEBOUNCE_MS);
  }

  // ── Public handle ────────────────────────────────────────────────
  const api = {
    panelId: state.panelId,
    get element() { return panel; },

    async init() {
      _render();
      await _refetch();
    },

    handleStateUpdate(_payload) {
      // Any bot_state push means "a deal may have opened/closed on
      // some bot" — the panel shows cross-bot open deals, so we
      // debounce-refetch the authoritative /api/bots view rather
      // than trying to reconcile incrementally from the single-bot
      // payload. 500ms mirrors the debounce we use elsewhere for
      // trade-event bursts; multiple pushes inside the window
      // collapse into one network round-trip.
      _scheduleRefetch();
    },

    resize() {
      // Table reflows via CSS width:100% — nothing for the factory
      // to do on panel resize. Kept for handle-API parity with
      // createPanelChart.
    },

    getConfig() {
      return {
        visibleColumns: state.visibleColumns.slice(),
        columnOrder: state.columnOrder.slice(),
        sort: state.sort ? { key: state.sort.key, dir: state.sort.dir } : null,
      };
    },

    destroy() {
      if (state._destroyed) return;
      state._destroyed = true;
      if (state._debounceTimer) {
        clearTimeout(state._debounceTimer);
        state._debounceTimer = null;
      }
      // No WS-unsubscribe — the workspace module owns the single
      // /ws/state socket and fans out to panels; destroying one
      // panel just stops its handleStateUpdate from scheduling
      // further refetches (guarded by state._destroyed above).
    },
  };
  return api;
}

// Minimal HTML-escape for the small amount of user-controlled text
// the open-deals-panel emits (column labels + keys come from
// columnDefs which are defined in app.js; deal-row cell rendering
// goes through the column cell functions which have their own
// safeText guards). Kept local so chart_module.js doesn't need a
// runtime dependency on app.js's helpers.
function _odEscape(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── Public namespace ──────────────────────────────────────────────────────
// The functions above are still available as plain globals (app.js
// call sites use them that way) — the namespace is additive, giving
// future code an explicit import target. PR 3b grew this with
// createPanelChart, PR 4 adds createOpenDealsPanel.
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
  createPanelChart,
  createOpenDealsPanel,
  // Called by app.js's ``_applyChartTheme`` after a theme switch.
  // Iterates every live panel-chart and re-applies layout + candle
  // colours. Safe to call when no panels exist (no-op). Exposed as
  // an arrow function so the closure captures _applyThemeToAllPanels
  // at namespace-export time and can't accidentally be shadowed by
  // consumers poking at window.RevertoChart after the fact.
  applyThemeToAll: () => _applyThemeToAllPanels(),
  buildTimezoneFormatter,
  tfSeconds,
  PANEL_INDICATOR_TYPES,
  PANEL_INDICATOR_LABELS,
  PANEL_TIMEFRAMES,
  CHART_TIMEZONES,
});
