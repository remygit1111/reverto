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
// Loading order in index.html: Lightweight Charts (vendored at
// /static/vendor/lightweight-charts/, see web/static/vendor/
// README.md) → chart_module.js → app.js. Script tags at top
// level share the same global scope, so function declarations
// here land alongside the ones in app.js with no import/export
// ceremony.
//
// Each function is ALSO attached to window.RevertoChart below so
// a future PR 3b chart-panel factory has a namespaced home for
// them without having to re-plumb global lookups.
//
// Indicator plugin architecture (PR feat/workspace-indicator-plugins):
// the Workspace chart-panel reads INDICATOR_PLUGINS to discover what
// indicators are available. Each plugin owns its own series lifecycle
// (create / render / destroy) + the param schema that feeds the
// indicator-manager modal's edit form. Registering a new plugin at
// runtime is a single call to ``registerIndicatorPlugin``; the
// modal's add-menu picks it up on next open. Main-chart, wizard,
// and backtest-candle still use their pre-existing hardcoded
// indicator dropdowns — this refactor is scoped to Workspace.

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

// ── Indicator-styling helpers ────────────────────────────────────────────
// Shared mappings + utilities used by every plugin's createSeries / render
// so the per-line styling knobs (color + lineStyle + lineWidth + opacity)
// translate to Lightweight Charts options without each plugin duplicating
// the logic.

// Human-readable line-style names ↔ LWC's LineStyle enum values.
// 0 = Solid, 1 = Dotted, 2 = Dashed (v5.1.0).
const LINE_STYLE_MAP = { solid: 0, dashed: 2, dotted: 1 };
function _lwcLineStyle(name) {
  const v = LINE_STYLE_MAP[String(name || '').toLowerCase()];
  return v != null ? v : 0;
}

// Apply an opacity percentage (0-100) to a hex color. Accepts '#RRGGBB',
// '#RGB', or already-alphaed '#RRGGBBAA'; anything else is returned as-is
// so callers can pass raw rgba()/named colors without the helper
// clobbering them. Percentages <= 0 fully transparent; >= 100 returns
// the input unchanged.
function _applyOpacityToColor(color, opacityPct) {
  if (typeof color !== 'string' || !color) return color;
  const pct = Math.max(0, Math.min(100, Number(opacityPct)));
  if (!Number.isFinite(pct)) return color;
  if (pct >= 100 && !/^#[0-9a-fA-F]{8}$/.test(color)) return color;
  let hex = color;
  if (/^#[0-9a-fA-F]{3}$/.test(hex)) {
    // Expand shorthand '#rgb' → '#rrggbb' so alpha appends correctly.
    hex = '#' + hex.slice(1).split('').map((c) => c + c).join('');
  }
  const base = /^#[0-9a-fA-F]{8}$/.test(hex) ? hex.slice(0, 7) : hex;
  if (!/^#[0-9a-fA-F]{6}$/.test(base)) return color;
  const alpha = Math.round(pct * 2.55);
  return base + alpha.toString(16).padStart(2, '0');
}

// Map a style-object entry ({color, lineStyle, lineWidth, opacity}) to
// LineSeries addSeries options. Extras (LWC-specific flags a plugin
// wants to tack on, like priceLineVisible) merge in via the second
// arg so the callers stay terse.
function _seriesOptsFromStyle(style, extra) {
  const s = style || {};
  return Object.assign({
    color: _applyOpacityToColor(s.color, s.opacity),
    lineStyle: _lwcLineStyle(s.lineStyle),
    lineWidth: Number(s.lineWidth) || 1,
    priceLineVisible: false,
    lastValueVisible: false,
  }, extra || {});
}

// ── Indicator plugin architecture ────────────────────────────────────────
// Each indicator is a plugin object conforming to the contract below.
// The Workspace chart-panel holds an array of *instances*, each one
// referencing a plugin by ``type`` and carrying its own ``params`` +
// ``color``. Multiple instances of the same type coexist — three EMAs
// at different periods, two RSIs on separate panes, etc. — without
// any switch-case bookkeeping on the render side.
//
// Contract:
//
//   @typedef {Object} IndicatorParamSchema
//   @property {string} key    — object key on instance.params
//   @property {string} label  — human-readable label in the edit form
//   @property {'int'|'float'} type
//   @property {number} default
//   @property {number=} min
//   @property {number=} max
//   @property {number=} step
//
//   @typedef {Object} IndicatorInstance
//   @property {string} id            — stable id used as dict key ("EMA_1")
//   @property {string} type          — plugin type, uppercase
//   @property {Object} params        — key-value map seeded from plugin defaults
//   @property {string} color         — CSS color used as the instance's primary
//   @property {Array=} _series       — runtime series handles (not serialized)
//   @property {number=} _assignedPane — runtime pane idx for paneType='pane'
//
//   @typedef {Object} IndicatorPlugin
//   @property {string} type
//   @property {string} displayName
//   @property {string} defaultColor
//   @property {'overlay'|'pane'} paneType
//   @property {IndicatorParamSchema[]} params
//   @property {(params: Object) => string} labelTemplate
//   @property {(ctx: {chart, LWC, inst, paneIdx}) => Array} createSeries
//   @property {(ctx: {inst, candles, chart, LWC}) => ({markers?: Array}|null)} render
//   @property {(ctx: {chart, inst}) => void=} destroy — optional override
//
// Panels call createSeries once per rebuild to set up the initial series
// list; render runs on every candle-data change. For plugins whose
// series count depends on computed segments (S/R, QFL, Parabolic SAR),
// createSeries returns an empty array and render manages the full
// add/remove lifecycle against ``inst._series``.

// EMA — single overlay line, one param. One `line` entry in `lines`
// exposes color + style + width + opacity through the Style tab.
const _EMA_PLUGIN = {
  type: 'EMA',
  displayName: 'EMA',
  paneType: 'overlay',
  params: [
    { key: 'period', label: 'Period', type: 'int', default: 21, min: 1, max: 500 },
  ],
  lines: [
    { id: 'line', label: 'Line', defaultColor: '#ffb347',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `EMA(${p.period})`,
  createSeries({ chart, LWC, inst }) {
    return [chart.addSeries(LWC.LineSeries, _seriesOptsFromStyle(inst.styles.line))];
  },
  render({ inst, candles }) {
    if (!inst._series[0]) return null;
    inst._series[0].applyOptions(_seriesOptsFromStyle(inst.styles.line));
    inst._series[0].setData(calcEMALine(candles, inst.params.period));
    return null;
  },
};

// RSI — pane series, one param. Each instance claims its own pane so
// two RSIs on different periods don't stomp on each other's scale.
const _RSI_PLUGIN = {
  type: 'RSI',
  displayName: 'RSI',
  paneType: 'pane',
  params: [
    { key: 'period', label: 'Period', type: 'int', default: 14, min: 2, max: 200 },
  ],
  lines: [
    { id: 'line', label: 'Line', defaultColor: '#5b8dee',
      defaultStyle: 'solid', defaultWidth: 1, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `RSI(${p.period})`,
  createSeries({ chart, LWC, inst, paneIdx }) {
    const opts = _seriesOptsFromStyle(inst.styles.line, { lastValueVisible: true });
    return [chart.addSeries(LWC.LineSeries, opts, paneIdx)];
  },
  render({ inst, candles }) {
    if (!inst._series[0]) return null;
    inst._series[0].applyOptions(_seriesOptsFromStyle(inst.styles.line, { lastValueVisible: true }));
    inst._series[0].setData(calcRSILine(candles, inst.params.period));
    return null;
  },
};

// MACD — histogram + MACD-line + signal-line in its own pane. The
// histogram's positive-bar color follows the style; negative bars
// keep the default red so sign flips remain visually obvious.
const _MACD_PLUGIN = {
  type: 'MACD',
  displayName: 'MACD',
  paneType: 'pane',
  params: [
    { key: 'fast',   label: 'Fast',   type: 'int', default: 12, min: 1 },
    { key: 'slow',   label: 'Slow',   type: 'int', default: 26, min: 1 },
    { key: 'signal', label: 'Signal', type: 'int', default: 9,  min: 1 },
  ],
  lines: [
    { id: 'histogram', label: 'Histogram', defaultColor: '#26a69a',
      defaultStyle: 'solid', defaultWidth: 1, defaultOpacity: 100 },
    { id: 'macd_line', label: 'MACD line', defaultColor: '#5b8dee',
      defaultStyle: 'solid', defaultWidth: 1, defaultOpacity: 100 },
    { id: 'signal_line', label: 'Signal line', defaultColor: '#ffb347',
      defaultStyle: 'solid', defaultWidth: 1, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `MACD(${p.fast},${p.slow},${p.signal})`,
  createSeries({ chart, LWC, inst, paneIdx }) {
    return [
      chart.addSeries(LWC.HistogramSeries, {
        color: _applyOpacityToColor(inst.styles.histogram.color, inst.styles.histogram.opacity),
      }, paneIdx),
      chart.addSeries(LWC.LineSeries, _seriesOptsFromStyle(inst.styles.macd_line), paneIdx),
      chart.addSeries(LWC.LineSeries, _seriesOptsFromStyle(inst.styles.signal_line), paneIdx),
    ];
  },
  render({ inst, candles }) {
    const m = calcMACDLines(candles, inst.params.fast, inst.params.slow, inst.params.signal);
    const histPos = _applyOpacityToColor(inst.styles.histogram.color, inst.styles.histogram.opacity);
    const histNeg = _applyOpacityToColor(_cssVar('--red', '#ef5350'), inst.styles.histogram.opacity);
    // The calcMACDLines helper colours bars from CSS vars at compute
    // time — override per-bar so the style's positive colour drives
    // up-bars and the default red stays on down-bars.
    for (const row of m.histogram) row.color = row.value >= 0 ? histPos : histNeg;
    if (inst._series[0]) inst._series[0].setData(m.histogram);
    if (inst._series[1]) {
      inst._series[1].applyOptions(_seriesOptsFromStyle(inst.styles.macd_line));
      inst._series[1].setData(m.macd);
    }
    if (inst._series[2]) {
      inst._series[2].applyOptions(_seriesOptsFromStyle(inst.styles.signal_line));
      inst._series[2].setData(m.signal);
    }
    return null;
  },
};

// Bollinger — three overlay lines (upper / middle / lower), each with
// independent styling.
const _BOLLINGER_PLUGIN = {
  type: 'BOLLINGER',
  displayName: 'Bollinger Bands',
  paneType: 'overlay',
  params: [
    { key: 'period',     label: 'Period',      type: 'int',   default: 20, min: 2 },
    { key: 'multiplier', label: 'Stddev mult', type: 'float', default: 2.0, min: 0.1, step: 0.1 },
  ],
  lines: [
    { id: 'upper',  label: 'Upper',  defaultColor: '#ef5350',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
    { id: 'middle', label: 'Middle', defaultColor: '#888888',
      defaultStyle: 'dashed', defaultWidth: 1, defaultOpacity: 100 },
    { id: 'lower',  label: 'Lower',  defaultColor: '#1e88e5',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `BB(${p.period},${p.multiplier})`,
  createSeries({ chart, LWC, inst }) {
    return [
      chart.addSeries(LWC.LineSeries, _seriesOptsFromStyle(inst.styles.upper)),
      chart.addSeries(LWC.LineSeries, _seriesOptsFromStyle(inst.styles.middle)),
      chart.addSeries(LWC.LineSeries, _seriesOptsFromStyle(inst.styles.lower)),
    ];
  },
  render({ inst, candles }) {
    const bb = calcBollingerLines(candles, inst.params.period, inst.params.multiplier);
    const order = ['upper', 'middle', 'lower'];
    const data = [bb.upper, bb.middle, bb.lower];
    for (let i = 0; i < 3; i++) {
      if (!inst._series[i]) continue;
      inst._series[i].applyOptions(_seriesOptsFromStyle(inst.styles[order[i]]));
      inst._series[i].setData(data[i]);
    }
    return null;
  },
};

// Supertrend — two overlay lines (bull / bear), each independently
// styled. The trend-following calc fills only one direction at a
// time so the inactive leg stays an empty array.
const _SUPERTREND_PLUGIN = {
  type: 'SUPERTREND',
  displayName: 'Supertrend',
  paneType: 'overlay',
  params: [
    { key: 'atr_period', label: 'ATR period', type: 'int',   default: 10, min: 1 },
    { key: 'multiplier', label: 'Multiplier', type: 'float', default: 3.0, min: 0.1, step: 0.1 },
  ],
  lines: [
    { id: 'bull', label: 'Bull', defaultColor: '#26a69a',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
    { id: 'bear', label: 'Bear', defaultColor: '#ef5350',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `ST(${p.atr_period},${p.multiplier})`,
  createSeries({ chart, LWC, inst }) {
    return [
      chart.addSeries(LWC.LineSeries, _seriesOptsFromStyle(inst.styles.bull)),
      chart.addSeries(LWC.LineSeries, _seriesOptsFromStyle(inst.styles.bear)),
    ];
  },
  render({ inst, candles }) {
    const st = calcSupertrendLines(candles, inst.params.atr_period, inst.params.multiplier);
    if (inst._series[0]) {
      inst._series[0].applyOptions(_seriesOptsFromStyle(inst.styles.bull));
      inst._series[0].setData(st.bull);
    }
    if (inst._series[1]) {
      inst._series[1].applyOptions(_seriesOptsFromStyle(inst.styles.bear));
      inst._series[1].setData(st.bear);
    }
    return null;
  },
};

// Support/Resistance — dynamic per-segment lines. createSeries is a
// no-op; render clears inst._series and rebuilds per segment. Each
// direction has its own styled entry so resistance + support can be
// distinguished without needing two separate instances.
const _SR_PLUGIN = {
  type: 'SUPPORT_RESISTANCE',
  displayName: 'S/R',
  paneType: 'overlay',
  params: [
    { key: 'left_bars',        label: 'Left bars',     type: 'int',   default: 15, min: 1 },
    { key: 'right_bars',       label: 'Right bars',    type: 'int',   default: 15, min: 1 },
    { key: 'volume_threshold', label: 'Vol threshold', type: 'float', default: 0,  min: 0, step: 0.1 },
    { key: 'min_touches',      label: 'Min touches',   type: 'int',   default: 1,  min: 1 },
  ],
  lines: [
    { id: 'resistance', label: 'Resistance', defaultColor: '#ef5350',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
    { id: 'support', label: 'Support', defaultColor: '#1e88e5',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `S/R(${p.left_bars},${p.right_bars})`,
  createSeries() { return []; },
  render({ inst, candles, chart, LWC }) {
    _clearInstanceSeries(chart, inst);
    const sr = calcSR(
      candles, inst.params.left_bars, inst.params.right_bars,
      inst.params.volume_threshold, inst.params.min_touches,
    );
    _addSegmentedLevelSeries(chart, LWC, inst, candles, sr.resSeries, inst.styles.resistance, 'R');
    _addSegmentedLevelSeries(chart, LWC, inst, candles, sr.supSeries, inst.styles.support, 'S');
    return null;
  },
};

// QFL — dynamic-segment base-level line, dashed by default to set it
// apart from S/R on the same chart.
const _QFL_PLUGIN = {
  type: 'QFL',
  displayName: 'QFL',
  paneType: 'overlay',
  params: [
    { key: 'base_periods',   label: 'Base periods',  type: 'int',   default: 36, min: 1 },
    { key: 'pump_periods',   label: 'Pump periods',  type: 'int',   default: 8,  min: 1 },
    { key: 'pump_pct',       label: 'Pump %',        type: 'float', default: 3.0, min: 0, step: 0.1 },
    { key: 'base_crack_pct', label: 'Base crack %',  type: 'float', default: 3.0, min: 0, step: 0.1 },
  ],
  lines: [
    { id: 'base', label: 'Base', defaultColor: '#f050a0',
      defaultStyle: 'dashed', defaultWidth: 2, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `QFL(${p.base_periods},${p.pump_periods})`,
  createSeries() { return []; },
  render({ inst, candles, chart, LWC }) {
    _clearInstanceSeries(chart, inst);
    const qfl = calcQFL(
      candles, inst.params.base_periods, inst.params.pump_periods,
      inst.params.pump_pct, inst.params.base_crack_pct,
    );
    _addSegmentedLevelSeries(chart, LWC, inst, candles, qfl.baseSeries, inst.styles.base, null);
    return null;
  },
};

// Parabolic SAR — dots per direction (via transparent line + marker
// primitives) plus trend-flip arrow markers returned to the caller so
// every instance's arrows merge into the candle-series marker set.
// Both dot colours + the flip-arrow colours follow the styled entries.
const _PARABOLIC_SAR_PLUGIN = {
  type: 'PARABOLIC_SAR',
  displayName: 'Parabolic SAR',
  paneType: 'overlay',
  params: [
    { key: 'initial_af', label: 'Initial AF', type: 'float', default: 0.02, min: 0.001, step: 0.005 },
    { key: 'max_af',     label: 'Max AF',     type: 'float', default: 0.20, min: 0.01,  step: 0.01 },
  ],
  lines: [
    { id: 'bull', label: 'Bull dots', defaultColor: '#3388bb',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
    { id: 'bear', label: 'Bear dots', defaultColor: '#ffb347',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `PSAR(${p.initial_af},${p.max_af})`,
  createSeries() { return []; },
  render({ inst, candles, chart, LWC }) {
    _clearInstanceSeries(chart, inst);
    const ps = calcParabolicSAR(candles, inst.params.initial_af, inst.params.max_af);
    const bullColor = _applyOpacityToColor(inst.styles.bull.color, inst.styles.bull.opacity);
    const bearColor = _applyOpacityToColor(inst.styles.bear.color, inst.styles.bear.opacity);
    const bullData = [], bearData = [], markers = [];
    for (let i = 0; i < candles.length; i++) {
      if (ps.sarValues[i] === null) continue;
      const t = candles[i].time, v = ps.sarValues[i];
      if (ps.dirs[i] === 1) bullData.push({ time: t, value: v });
      else                  bearData.push({ time: t, value: v });
      if (i > 0 && ps.dirs[i] !== 0 && ps.dirs[i - 1] !== 0 && ps.dirs[i] !== ps.dirs[i - 1]) {
        markers.push({
          time: t,
          position: ps.dirs[i] === 1 ? 'belowBar' : 'aboveBar',
          shape: ps.dirs[i] === 1 ? 'arrowUp' : 'arrowDown',
          color: ps.dirs[i] === 1 ? bullColor : bearColor,
          size: 1,
        });
      }
    }
    const addDots = (data, color) => {
      if (!data.length) return;
      const s = chart.addSeries(LWC.LineSeries, {
        color: 'transparent', lineWidth: 0,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      s.setData(data);
      const csm = LWC.createSeriesMarkers;
      const dotMarkers = data.map((p) => ({
        time: p.time, position: 'inBar', color, shape: 'circle', size: 1,
      }));
      if (csm) csm(s, dotMarkers);
      else { try { s.setMarkers(dotMarkers); } catch (e) {} }
      inst._series.push(s);
    };
    addDots(bullData, bullColor);
    addDots(bearData, bearColor);
    return { markers };
  },
};

// Market structure — pure swing-high/low markers, no series at all.
// The single styled `marker` entry tints both up + down arrows so
// two instances on different lookbacks remain distinguishable.
const _MARKET_STRUCTURE_PLUGIN = {
  type: 'MARKET_STRUCTURE',
  displayName: 'Market structure',
  paneType: 'overlay',
  params: [
    { key: 'lookback', label: 'Lookback', type: 'int', default: 3, min: 1 },
  ],
  lines: [
    { id: 'marker', label: 'Markers', defaultColor: '#26a69a',
      defaultStyle: 'solid', defaultWidth: 2, defaultOpacity: 100 },
  ],
  labelTemplate: (p) => `MS(${p.lookback})`,
  createSeries() { return []; },
  render({ inst, candles }) {
    const ms = calcMarketStructureMarkers(candles, inst.params.lookback);
    const color = _applyOpacityToColor(inst.styles.marker.color, inst.styles.marker.opacity);
    for (const m of ms) m.color = color;
    return { markers: ms };
  },
};

// Shared plugin utilities — hoisted to module scope so the plugin
// objects above can use them without capturing a closure over the
// per-panel factory.

// Tear down every series attached to an instance. Price lines go
// with their host series automatically, so this is enough for the
// default destroy path and for dynamic-segment plugins that call it
// from inside render before rebuilding.
function _clearInstanceSeries(chart, inst) {
  if (!chart || !inst || !Array.isArray(inst._series)) {
    if (inst) inst._series = [];
    return;
  }
  for (const s of inst._series) {
    try { chart.removeSeries(s); } catch (e) {}
  }
  inst._series = [];
}

// Segmented-level renderer shared by S/R and QFL. Walks a per-bar
// value-or-null series, groups consecutive equal values into flat
// horizontal segments, and draws each segment as its own LineSeries
// styled from the given style-object ({color, lineStyle, lineWidth,
// opacity}). The last segment (if ``labelStart`` is truthy) gets a
// price-axis label so the operator can read the level without hovering.
function _addSegmentedLevelSeries(chart, LWC, inst, candles, series, style, labelStart) {
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
  const seriesOpts = _seriesOptsFromStyle(style, { crosshairMarkerVisible: false });
  const axisLabelColor = _applyOpacityToColor(style.color, style.opacity);
  for (const seg of segs) {
    const data = [];
    for (let j = seg.start; j <= seg.end; j++) {
      data.push({ time: candles[j].time, value: seg.value });
    }
    const s = chart.addSeries(LWC.LineSeries, seriesOpts);
    s.setData(data);
    inst._series.push(s);
    if (labelStart && seg.end === candles.length - 1) {
      s.createPriceLine({
        price: seg.value, color: axisLabelColor,
        lineWidth: 0, lineStyle: 0,
        axisLabelVisible: true, title: labelStart,
      });
    }
  }
}

const INDICATOR_PLUGINS = {
  EMA: _EMA_PLUGIN,
  RSI: _RSI_PLUGIN,
  MACD: _MACD_PLUGIN,
  BOLLINGER: _BOLLINGER_PLUGIN,
  SUPERTREND: _SUPERTREND_PLUGIN,
  SUPPORT_RESISTANCE: _SR_PLUGIN,
  QFL: _QFL_PLUGIN,
  PARABOLIC_SAR: _PARABOLIC_SAR_PLUGIN,
  MARKET_STRUCTURE: _MARKET_STRUCTURE_PLUGIN,
};

// Registration hook for future plugins loaded from app.js / user code
// without having to patch this file. Overrides a built-in type if a
// collision happens; the indicator-manager modal picks the registry
// up at open time so newly-registered plugins appear in the add-menu
// without a refresh.
//
// Audit r1.1-005: validate the full plugin contract at registration
// time so a missing field surfaces as a clear warning instead of a
// cryptic TypeError deep inside render(). Return true/false so
// callers can tell whether registration succeeded.
function registerIndicatorPlugin(plugin) {
  if (!plugin || typeof plugin !== 'object') {
    console.warn('registerIndicatorPlugin: expected a plugin object');
    return false;
  }
  const required = [
    'type', 'displayName', 'paneType', 'params', 'lines',
    'labelTemplate', 'createSeries', 'render',
  ];
  const missing = required.filter((k) => !(k in plugin));
  const tag = typeof plugin.type === 'string' ? plugin.type : '(no type)';
  if (missing.length) {
    console.warn(
      `registerIndicatorPlugin: plugin ${tag} missing required fields: ${missing.join(', ')}`,
    );
    return false;
  }
  // Type-level checks for the fields most likely to trip up plugin
  // authors. Everything else is duck-typed at render time.
  if (typeof plugin.type !== 'string' || !plugin.type) {
    console.warn('registerIndicatorPlugin: type must be a non-empty string');
    return false;
  }
  if (!Array.isArray(plugin.params)) {
    console.warn(`registerIndicatorPlugin: ${tag} params must be an array`);
    return false;
  }
  if (!Array.isArray(plugin.lines)) {
    console.warn(`registerIndicatorPlugin: ${tag} lines must be an array`);
    return false;
  }
  if (typeof plugin.labelTemplate !== 'function') {
    console.warn(`registerIndicatorPlugin: ${tag} labelTemplate must be a function`);
    return false;
  }
  if (typeof plugin.createSeries !== 'function') {
    console.warn(`registerIndicatorPlugin: ${tag} createSeries must be a function`);
    return false;
  }
  if (typeof plugin.render !== 'function') {
    console.warn(`registerIndicatorPlugin: ${tag} render must be a function`);
    return false;
  }
  INDICATOR_PLUGINS[plugin.type] = plugin;
  return true;
}

// Pull param defaults into a fresh object. Safe to mutate by the
// caller — each instance owns its own copy.
function _defaultParamsFor(type) {
  const plugin = INDICATOR_PLUGINS[type];
  if (!plugin) return {};
  const out = {};
  for (const p of plugin.params) out[p.key] = p.default;
  return out;
}

// Build the default styles-object for a plugin — keyed on line.id with
// {color, lineStyle, lineWidth, opacity} for each entry. Invoked from
// _createIndicatorInstance and from the style migration helper when an
// instance is missing its styles entirely.
function _defaultStylesFor(type) {
  const plugin = INDICATOR_PLUGINS[type];
  if (!plugin || !Array.isArray(plugin.lines)) return {};
  const out = {};
  for (const line of plugin.lines) {
    out[line.id] = {
      color: line.defaultColor,
      lineStyle: line.defaultStyle,
      lineWidth: line.defaultWidth,
      opacity: line.defaultOpacity,
    };
  }
  return out;
}

// Reconcile an instance's styles-object with the plugin's current
// lines list. Three jobs:
//   1. Legacy single-color layouts (``color: "#RRGGBB"``) → fan the
//      color out across every line.id, seeding other style fields
//      from plugin defaults.
//   2. Missing or partial styles → fill gaps with plugin defaults so
//      render doesn't crash on ``style.color`` lookups.
//   3. Stale line.id entries (plugin removed a line post-save) →
//      drop them so the layout doesn't accumulate garbage.
// Mutates ``instance`` in place; the legacy ``color`` field is
// removed after fan-out so getConfig serialises only the new shape.
function _migrateInstanceStyles(instance, plugin) {
  if (!instance || !plugin || !Array.isArray(plugin.lines)) return;
  const legacyColor = typeof instance.color === 'string' && instance.color
    ? instance.color : null;
  if (!instance.styles || typeof instance.styles !== 'object') instance.styles = {};
  for (const line of plugin.lines) {
    const cur = instance.styles[line.id];
    const seed = {
      color: legacyColor || line.defaultColor,
      lineStyle: line.defaultStyle,
      lineWidth: line.defaultWidth,
      opacity: line.defaultOpacity,
    };
    if (!cur || typeof cur !== 'object') {
      instance.styles[line.id] = seed;
      continue;
    }
    if (typeof cur.color !== 'string' || !cur.color) cur.color = seed.color;
    if (typeof cur.lineStyle !== 'string') cur.lineStyle = seed.lineStyle;
    if (typeof cur.lineWidth !== 'number') cur.lineWidth = seed.lineWidth;
    if (typeof cur.opacity !== 'number') cur.opacity = seed.opacity;
  }
  const valid = new Set(plugin.lines.map((l) => l.id));
  for (const k of Object.keys(instance.styles)) {
    if (!valid.has(k)) delete instance.styles[k];
  }
  if (legacyColor) delete instance.color;
}

// Resolve the "primary" color for an instance — the first line's
// color, used by the manager-list dot and anywhere we need a single
// glance-able tint without pulling the whole style-tab up. Falls back
// through instance.color (legacy, pre-migration) → plugin default →
// a muted grey if the plugin has no lines defined.
function _getInstancePrimaryColor(instance, plugin) {
  if (!plugin || !Array.isArray(plugin.lines) || plugin.lines.length === 0) {
    return instance && typeof instance.color === 'string' ? instance.color : '#888';
  }
  const first = plugin.lines[0];
  const style = instance && instance.styles && instance.styles[first.id];
  if (style && typeof style.color === 'string') return style.color;
  if (instance && typeof instance.color === 'string' && instance.color) return instance.color;
  return first.defaultColor;
}

// Mint a new instance with defaults. The id uniquely names the
// instance for state lookups + form-element ids; the uniqueness
// scope is the single panel (two panels can share ids without
// conflict). Suffix counts active instances of the same type plus
// one so deletes-and-adds don't collide.
function _createIndicatorInstance(type, existing, colorHint) {
  const plugin = INDICATOR_PLUGINS[type];
  if (!plugin) return null;
  const siblings = (existing || []).filter((x) => x && x.type === type);
  // Find smallest unused integer suffix instead of count+1 — after a
  // delete + add, count+1 can collide with a surviving higher-suffix
  // instance.
  const used = new Set(siblings.map((s) => {
    const m = /^[A-Z_]+_(\d+)$/.exec(String(s.id || ''));
    return m ? Number(m[1]) : 0;
  }));
  let n = 1;
  while (used.has(n)) n++;
  const inst = {
    id: `${type}_${n}`,
    type,
    params: _defaultParamsFor(type),
    styles: _defaultStylesFor(type),
  };
  // colorHint (rare — came from a pre-plugin migration path) seeds
  // every line's color to match; _migrateInstanceStyles would
  // otherwise handle this for read-from-disk instances.
  if (colorHint && plugin.lines) {
    for (const line of plugin.lines) {
      if (inst.styles[line.id]) inst.styles[line.id].color = colorHint;
    }
  }
  return inst;
}

// Convert whatever landed from disk into the in-memory instance
// array. Three input shapes to tolerate:
//   1. Legacy v1 — string array: ['EMA', 'RSI'] → one default
//      instance per type, skipping unknown types.
//   2. Legacy v2 — single-color instance: [{type, params, color}]
//      → styles object fanned out from the one color.
//   3. Current — per-line-styled instance: [{type, params, styles}]
//      → passed through with defaults backfilling gaps.
// Missing params or style fields re-seed from plugin defaults so
// adding a new param / line to a plugin doesn't break old layouts.
function _migrateIndicators(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const item of raw) {
    if (typeof item === 'string') {
      if (!INDICATOR_PLUGINS[item]) continue;
      const inst = _createIndicatorInstance(item, out, null);
      if (inst) out.push(inst);
      continue;
    }
    if (!item || typeof item !== 'object') continue;
    const type = String(item.type || '').toUpperCase();
    const plugin = INDICATOR_PLUGINS[type];
    if (!plugin) continue;
    const params = Object.assign(_defaultParamsFor(type), item.params || {});
    const id = typeof item.id === 'string' && item.id
      ? item.id
      : (_createIndicatorInstance(type, out, null) || { id: `${type}_1` }).id;
    // Guarantee unique ids — duplicates would collide in the DOM
    // form-element id space and confuse the manager modal.
    let uid = id, bump = 2;
    const taken = new Set(out.map((x) => x.id));
    while (taken.has(uid)) { uid = `${id}_${bump++}`; }
    const inst = {
      id: uid,
      type,
      params,
      styles: (item.styles && typeof item.styles === 'object')
        ? Object.assign({}, item.styles) : {},
    };
    // Preserve legacy ``color`` temporarily so _migrateInstanceStyles
    // can use it as the fan-out seed; the helper deletes the field
    // after fan-out so writes go out clean.
    if (typeof item.color === 'string' && item.color) inst.color = item.color;
    _migrateInstanceStyles(inst, plugin);
    out.push(inst);
  }
  return out;
}

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

// Merge prior pan-loaded history with a fresh candle batch. Audit
// r1.1-003: the Workspace chart-panel's 30 s refresh timer used to
// replace ``state._candles`` wholesale via setData(), wiping any
// older candles the user had scrolled back to load. The main bot-
// chart path (``app.js::fetchChartData``) already does this merge;
// this helper is the factored-out version so both code paths share
// the same logic and it's testable in isolation.
//
// Semantics:
//   * Empty ``newCandles`` → return the prior unchanged (caller
//     decides whether to no-op the render).
//   * Empty ``prior`` → return newCandles as-is (initial load).
//   * Both non-empty → keep prior rows whose ``time`` is strictly
//     less than the newest batch's oldest ``time``, then concat.
//     Overlap on a timestamp is resolved in favour of the fresh
//     batch so a late fill / correction on that bar propagates.
function _mergePriorHistory(prior, newCandles) {
  if (!Array.isArray(newCandles) || newCandles.length === 0) {
    return Array.isArray(prior) ? prior : [];
  }
  if (!Array.isArray(prior) || prior.length === 0) return newCandles;
  const newOldest = newCandles[0].time;
  const priorHistory = prior.filter((c) => c.time < newOldest);
  return priorHistory.concat(newCandles);
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

// PR: plugin-architecture — the multi-select dropdown is replaced by
// a single button that opens the indicator-manager modal. The button
// surfaces the instance count so operators see at a glance how many
// indicators are live on the chart.
function _buildHeaderIndicatorsBtn(state) {
  const root = document.createElement('button');
  root.type = 'button';
  root.className = 'panel-indicators-btn';
  root.dataset.role = 'indicators-btn';
  root.setAttribute('aria-label', 'Manage indicators');
  const count = document.createElement('span');
  count.className = 'indicators-count';
  count.textContent = String(state.indicators.length);
  const label = document.createElement('span');
  label.className = 'indicators-label';
  label.textContent = 'Indicators';
  root.appendChild(count);
  root.appendChild(label);
  return {
    root,
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
  //   config.indicators: array of indicator instances
  //     ({id, type, params, color}) keyed by type in INDICATOR_PLUGINS.
  //     Legacy string-array shape (['EMA', 'RSI']) is accepted and
  //     migrated to instance form via _migrateIndicators.
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
    // PR plugin-architecture: each entry is a full instance object
    // ({id, type, params, color}), not a bare type string. Legacy
    // string-array layouts are migrated on load via _migrateIndicators;
    // getConfig() always emits the new shape so the layout_json self-
    // heals on the next save.
    indicators: _migrateIndicators(cfg.indicators),
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
  // Indicators-button — opens the plugin-architecture manager modal.
  // Count in the button reflects live instance count and updates via
  // ``indBtn.updateCount(n)`` whenever state.indicators mutates.
  const indBtn = _buildHeaderIndicatorsBtn(state);

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
  header.appendChild(indBtn.root);
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
  // Indicators-button opens the plugin-architecture manager modal.
  // The modal is built lazily so the DOM stays light until the
  // operator actually clicks through.
  indBtn.root.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    _openIndicatorsModal();
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

  // ── Indicator-manager modal ──────────────────────────────────────
  // PR plugin-architecture: a full modal replacing the header's
  // legacy multi-select dropdown. Lists live instances with edit +
  // color + remove per row, and an add-select at the bottom sourced
  // from INDICATOR_PLUGINS. Built lazily on first open so the panel
  // DOM stays light. Re-rendered in place on every add/remove/edit
  // so the modal's state matches state.indicators without
  // full-DOM-replacement flicker.

  // One modal node per panel, anchored on ``panel`` so it appears
  // inside the grid cell. z-index is above the popover so stacking
  // with open settings doesn't crash the UX.
  const indModal = document.createElement('div');
  indModal.className = 'panel-indicators-modal hidden';
  indModal.innerHTML =
    '<div class="panel-indicators-modal-backdrop"></div>'
    + '<div class="panel-indicators-modal-dialog" role="dialog" aria-label="Indicators">'
    +   '<div class="panel-indicators-modal-header">'
    +     '<span class="panel-indicators-modal-title">Indicators</span>'
    +     '<button type="button" class="panel-indicators-modal-close" aria-label="Close">×</button>'
    +   '</div>'
    +   '<div class="panel-indicators-modal-body">'
    +     '<div class="panel-indicators-list" data-role="list"></div>'
    +     '<div class="panel-indicators-empty hidden">No indicators yet — add one below.</div>'
    +     '<div class="panel-indicators-add-row">'
    +       '<select class="panel-indicators-add-select" data-role="add-select"></select>'
    +       '<button type="button" class="hbtn hbtn-theme btn-accent" data-role="add-btn">+ Add</button>'
    +     '</div>'
    +   '</div>'
    + '</div>';
  panel.appendChild(indModal);

  const indModalBackdrop = indModal.querySelector('.panel-indicators-modal-backdrop');
  const indModalClose = indModal.querySelector('.panel-indicators-modal-close');
  const indModalList = indModal.querySelector('[data-role="list"]');
  const indModalEmpty = indModal.querySelector('.panel-indicators-empty');
  const indModalAddSelect = indModal.querySelector('[data-role="add-select"]');
  const indModalAddBtn = indModal.querySelector('[data-role="add-btn"]');

  // Which instance is currently expanded for editing, by id. Null
  // means no edit-form is shown. Tracking this lets the re-render
  // preserve the expansion across param tweaks.
  state._editingInstanceId = null;

  function _closeIndicatorsModal() {
    indModal.classList.add('hidden');
    state._editingInstanceId = null;
  }

  // Input widget for one param schema row. Numeric inputs so the
  // browser's spin buttons + validation handle range/step; the form
  // submit reads inputs back at apply time, cast to Number.
  function _buildParamInput(schema, currentValue) {
    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'panel-indicators-param-input';
    input.dataset.key = schema.key;
    input.dataset.paramType = schema.type;
    if (schema.type === 'int') input.step = '1';
    else input.step = String(schema.step || 'any');
    if (schema.min != null) input.min = String(schema.min);
    if (schema.max != null) input.max = String(schema.max);
    input.value = String(currentValue != null ? currentValue : (schema.default != null ? schema.default : 0));
    return input;
  }

  // Tabbed edit form — "Inputs" tab holds the param schema rows, "Style"
  // tab holds per-line styling controls driven by plugin.lines. Apply
  // collects from both tabs in one go, so switching tabs mid-edit
  // doesn't drop user input before the final write. The active tab is
  // cached on state so re-renders (from row-delete elsewhere) don't
  // bounce the operator back to Inputs mid-session.
  function _buildInputsTab(plugin, inst) {
    const container = document.createElement('div');
    container.className = 'panel-indicators-tab-content';
    container.dataset.tab = 'inputs';
    for (const schema of plugin.params) {
      const row = document.createElement('div');
      row.className = 'panel-indicators-param-row';
      const lbl = document.createElement('label');
      lbl.className = 'panel-indicators-param-label';
      lbl.textContent = schema.label;
      row.appendChild(lbl);
      row.appendChild(_buildParamInput(schema, inst.params[schema.key]));
      container.appendChild(row);
    }
    return container;
  }

  function _buildStyleTab(plugin, inst) {
    const container = document.createElement('div');
    container.className = 'panel-indicators-tab-content hidden';
    container.dataset.tab = 'style';
    const lines = Array.isArray(plugin.lines) ? plugin.lines : [];
    if (!lines.length) {
      const empty = document.createElement('div');
      empty.className = 'panel-indicators-empty';
      empty.textContent = 'No styled lines for this indicator.';
      container.appendChild(empty);
      return container;
    }
    for (const line of lines) {
      const style = (inst.styles && inst.styles[line.id]) || {};
      const row = document.createElement('div');
      row.className = 'panel-indicators-line-row';
      row.dataset.lineId = line.id;

      const label = document.createElement('span');
      label.className = 'panel-indicators-line-label';
      label.textContent = line.label;

      const colorInput = document.createElement('input');
      colorInput.type = 'color';
      colorInput.dataset.lineProp = 'color';
      colorInput.value = style.color || line.defaultColor;

      const styleSelect = document.createElement('select');
      styleSelect.dataset.lineProp = 'lineStyle';
      for (const v of ['solid', 'dashed', 'dotted']) {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v.charAt(0).toUpperCase() + v.slice(1);
        if ((style.lineStyle || line.defaultStyle) === v) opt.selected = true;
        styleSelect.appendChild(opt);
      }

      const widthSelect = document.createElement('select');
      widthSelect.dataset.lineProp = 'lineWidth';
      for (const v of [1, 2, 3, 4]) {
        const opt = document.createElement('option');
        opt.value = String(v);
        opt.textContent = String(v);
        if ((Number(style.lineWidth) || line.defaultWidth) === v) opt.selected = true;
        widthSelect.appendChild(opt);
      }

      const opacityInput = document.createElement('input');
      opacityInput.type = 'number';
      opacityInput.min = '0';
      opacityInput.max = '100';
      opacityInput.step = '5';
      opacityInput.dataset.lineProp = 'opacity';
      opacityInput.value = String(
        Number.isFinite(style.opacity) ? style.opacity : line.defaultOpacity,
      );

      const pct = document.createElement('span');
      pct.className = 'panel-indicators-opacity-pct';
      pct.textContent = '%';

      row.append(label, colorInput, styleSelect, widthSelect, opacityInput, pct);
      container.appendChild(row);
    }
    return container;
  }

  function _buildEditForm(inst) {
    const plugin = INDICATOR_PLUGINS[inst.type];
    if (!plugin) return null;
    const form = document.createElement('div');
    form.className = 'panel-indicators-edit-form';
    form.dataset.instanceId = inst.id;

    // Tab buttons.
    const tabs = document.createElement('div');
    tabs.className = 'panel-indicators-tabs';
    const mkTab = (name, label, active) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'panel-indicators-tab' + (active ? ' active' : '');
      b.dataset.tab = name;
      b.textContent = label;
      return b;
    };
    const activeTab = state._editTab || 'inputs';
    const inputsBtn = mkTab('inputs', 'Inputs', activeTab === 'inputs');
    const styleBtn  = mkTab('style',  'Style',  activeTab === 'style');
    tabs.append(inputsBtn, styleBtn);
    form.appendChild(tabs);

    // Tab content panes. Both stay in the DOM so Apply can collect
    // from the hidden pane without rebuilding; the active tab's
    // visibility is toggled via the 'hidden' class.
    const inputsPane = _buildInputsTab(plugin, inst);
    const stylePane  = _buildStyleTab(plugin, inst);
    if (activeTab === 'style') { inputsPane.classList.add('hidden'); stylePane.classList.remove('hidden'); }
    form.appendChild(inputsPane);
    form.appendChild(stylePane);

    const switchTo = (name) => {
      state._editTab = name;
      inputsBtn.classList.toggle('active', name === 'inputs');
      styleBtn.classList.toggle('active', name === 'style');
      inputsPane.classList.toggle('hidden', name !== 'inputs');
      stylePane.classList.toggle('hidden', name !== 'style');
    };
    inputsBtn.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      switchTo('inputs');
    });
    styleBtn.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      switchTo('style');
    });

    const actions = document.createElement('div');
    actions.className = 'panel-indicators-edit-actions';
    const applyBtn = document.createElement('button');
    applyBtn.type = 'button';
    applyBtn.className = 'hbtn hbtn-theme btn-accent';
    applyBtn.dataset.action = 'apply';
    applyBtn.textContent = 'Apply';
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'hbtn hbtn-theme';
    closeBtn.dataset.action = 'cancel';
    closeBtn.textContent = 'Close';
    actions.appendChild(applyBtn);
    actions.appendChild(closeBtn);
    form.appendChild(actions);

    applyBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      // Collect params from inputs tab (still in DOM even when hidden).
      const patch = { params: {}, styles: {} };
      for (const inp of inputsPane.querySelectorAll('input[data-key]')) {
        const key = inp.dataset.key;
        const t = inp.dataset.paramType;
        let v = Number(inp.value);
        if (!Number.isFinite(v)) v = Number(plugin.params.find((p) => p.key === key).default);
        if (t === 'int') v = Math.round(v);
        patch.params[key] = v;
      }
      // Collect per-line styles.
      for (const row of stylePane.querySelectorAll('.panel-indicators-line-row')) {
        const lineId = row.dataset.lineId;
        if (!lineId) continue;
        const color = row.querySelector('[data-line-prop="color"]').value;
        const lineStyle = row.querySelector('[data-line-prop="lineStyle"]').value;
        const lineWidth = parseInt(row.querySelector('[data-line-prop="lineWidth"]').value, 10) || 1;
        const opacityRaw = parseInt(row.querySelector('[data-line-prop="opacity"]').value, 10);
        const opacity = Number.isFinite(opacityRaw)
          ? Math.max(0, Math.min(100, opacityRaw)) : 100;
        patch.styles[lineId] = { color, lineStyle, lineWidth, opacity };
      }
      api.updateIndicator(inst.id, patch);
      state._editingInstanceId = null;
      state._editTab = 'inputs';
      _renderIndModal();
    });
    closeBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      state._editingInstanceId = null;
      state._editTab = 'inputs';
      _renderIndModal();
    });
    return form;
  }

  // Full modal re-render. Cheap — at most ~20 rows in practice — so
  // any mutation (add/remove/color/edit) replays the full list
  // rather than hunting for the single changed row.
  function _renderIndModal() {
    // Refresh add-select against the live registry so a late
    // registerIndicatorPlugin call surfaces without reload.
    indModalAddSelect.innerHTML = '';
    const types = Object.keys(INDICATOR_PLUGINS);
    for (const type of types) {
      const p = INDICATOR_PLUGINS[type];
      const opt = document.createElement('option');
      opt.value = type;
      opt.textContent = p.displayName || type;
      indModalAddSelect.appendChild(opt);
    }

    indModalList.innerHTML = '';
    if (!state.indicators.length) {
      indModalEmpty.classList.remove('hidden');
      return;
    }
    indModalEmpty.classList.add('hidden');
    for (const inst of state.indicators) {
      const plugin = INDICATOR_PLUGINS[inst.type];
      if (!plugin) continue;
      const row = document.createElement('div');
      row.className = 'panel-indicators-row';
      row.dataset.id = inst.id;

      const main = document.createElement('div');
      main.className = 'panel-indicators-row-main';

      // Display-only color swatch — preview of the first line's color.
      // Color editing lives in the Style tab so multi-line indicators
      // don't have two competing entry points for the same state.
      const colorDot = document.createElement('span');
      colorDot.className = 'panel-indicators-row-color';
      colorDot.setAttribute('aria-hidden', 'true');
      colorDot.style.backgroundColor = _getInstancePrimaryColor(inst, plugin);
      main.appendChild(colorDot);

      const label = document.createElement('span');
      label.className = 'panel-indicators-row-label';
      label.textContent = plugin.labelTemplate(inst.params);
      main.appendChild(label);

      const actions = document.createElement('div');
      actions.className = 'panel-indicators-row-actions';
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'panel-indicators-row-edit';
      editBtn.textContent = state._editingInstanceId === inst.id ? 'Hide' : 'Edit';
      editBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        state._editingInstanceId = state._editingInstanceId === inst.id ? null : inst.id;
        _renderIndModal();
      });
      const removeRowBtn = document.createElement('button');
      removeRowBtn.type = 'button';
      removeRowBtn.className = 'panel-indicators-row-remove';
      removeRowBtn.setAttribute('aria-label', 'Remove indicator');
      removeRowBtn.textContent = '×';
      removeRowBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        api.removeIndicator(inst.id);
        if (state._editingInstanceId === inst.id) state._editingInstanceId = null;
        _renderIndModal();
      });
      actions.appendChild(editBtn);
      actions.appendChild(removeRowBtn);
      main.appendChild(actions);

      row.appendChild(main);
      if (state._editingInstanceId === inst.id) {
        const form = _buildEditForm(inst);
        if (form) row.appendChild(form);
      }
      indModalList.appendChild(row);
    }
  }

  function _openIndicatorsModal() {
    _renderIndModal();
    indModal.classList.remove('hidden');
  }

  indModalClose.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    _closeIndicatorsModal();
  });
  indModalBackdrop.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    _closeIndicatorsModal();
  });
  indModalAddBtn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const type = indModalAddSelect.value;
    if (!type) return;
    const newId = api.addIndicator(type);
    // Auto-expand the newly added instance's edit form so the
    // operator can immediately tweak params without hunting for it.
    if (newId) state._editingInstanceId = newId;
    _renderIndModal();
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
      upColor: _cssVar('--chart-up', '#26a69a'),
      downColor: _cssVar('--red', '#ef5350'),
      borderUpColor: _cssVar('--chart-up', '#26a69a'),
      borderDownColor: _cssVar('--red', '#ef5350'),
      wickUpColor: _cssVar('--chart-up', '#26a69a'),
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
    // Audit r1.1-006: anything that throws between the add above
    // and the final ``return true`` below would otherwise leave
    // state._themeRegistryEntry in _activePanelCharts forever —
    // destroy() only runs via state.onRemove so a failed init
    // doesn't clean up after itself. Catch, de-register, signal
    // failure.
    try {
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
    } catch (initErr) {
      _activePanelCharts.delete(state._themeRegistryEntry);
      state._themeRegistryEntry = null;
      console.error('chart_module: _initChart body threw:', initErr);
      return false;
    }
    return true;
  }

  // Plugin-architecture: destroy/rebuild/render iterate the instance
  // array instead of switching on type. Each plugin manages its own
  // series list via ``inst._series``; createSeries is called on
  // rebuild, render is called on every data update. The two are
  // split so dynamic-segment plugins (S/R, QFL, PSAR) can return []
  // from createSeries and own the full add/remove cycle inside render.
  function _destroyIndicatorSeries() {
    if (!state._chart) return;
    for (const inst of state.indicators) {
      const plugin = INDICATOR_PLUGINS[inst.type];
      if (plugin && typeof plugin.destroy === 'function') {
        try { plugin.destroy({ chart: state._chart, inst }); } catch (e) {}
      } else {
        _clearInstanceSeries(state._chart, inst);
      }
    }
  }

  function _rebuildIndicatorSeries() {
    if (!state._chart) return;
    _destroyIndicatorSeries();
    const LWC = window.LightweightCharts;
    // Pane allocation: every paneType='pane' instance claims the next
    // free pane index. Two RSIs therefore get two separate panes and
    // don't stomp on each other's scale. Pane 0 is reserved for the
    // candle series + overlay indicators.
    let nextPane = 1;
    for (const inst of state.indicators) {
      const plugin = INDICATOR_PLUGINS[inst.type];
      if (!plugin) { inst._series = []; continue; }
      const paneIdx = plugin.paneType === 'pane' ? nextPane++ : 0;
      inst._assignedPane = paneIdx;
      try {
        inst._series = plugin.createSeries({
          chart: state._chart, LWC, inst, paneIdx,
        }) || [];
      } catch (e) {
        inst._series = [];
      }
    }
  }

  function _renderIndicatorOverlays() {
    if (!state._chart || !state._candleSeries || !state._candles.length) return;
    const candles = state._candles;
    const LWC = window.LightweightCharts;
    const markers = [];

    for (const inst of state.indicators) {
      const plugin = INDICATOR_PLUGINS[inst.type];
      if (!plugin || !Array.isArray(inst._series)) continue;
      let out = null;
      try {
        out = plugin.render({
          inst, candles,
          chart: state._chart,
          LWC,
        });
      } catch (e) { /* plugin render failure must not break neighbours */ }
      if (out && Array.isArray(out.markers)) {
        for (const m of out.markers) markers.push(m);
      }
    }

    state._indicatorMarkers = markers;
    _setCombinedMarkers();
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
    // Audit r1.1-003: merge prior pan-loaded history with the fresh
    // batch BEFORE setData. Without the merge, the 30 s refresh
    // timer wipes every scrolled-back candle the user paid a
    // scroll-to-load round-trip for. Mirrors app.js::fetchChartData
    // at :6034-6036.
    state._candles = _mergePriorHistory(state._candles, candles);
    state._candleSeries.setData(state._candles);
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

    // Plugin-architecture handle API: add / remove / update instances
    // by id. The modal calls these directly; external callers (app.js
    // hasn't used the old toggleIndicator since PR 5a) can drive the
    // panel programmatically without poking into state.
    addIndicator(type, colorHint) {
      const inst = _createIndicatorInstance(type, state.indicators, colorHint);
      if (!inst) return null;
      state.indicators.push(inst);
      indBtn.updateCount(state.indicators.length);
      _rebuildIndicatorSeries();
      _renderIndicatorOverlays();
      if (state.onConfigChange) state.onConfigChange();
      return inst.id;
    },

    removeIndicator(id) {
      const idx = state.indicators.findIndex((x) => x.id === id);
      if (idx < 0) return false;
      // Clear this instance's LWC series BEFORE splicing it out of
      // state. _destroyIndicatorSeries (called by
      // _rebuildIndicatorSeries) iterates state.indicators and can't
      // reach an instance that's already been removed — its series
      // would otherwise orphan on the chart until the next full redraw
      // (page refresh or timeframe switch). Mirror the same plugin.
      // destroy() / _clearInstanceSeries fallback that the rebuild
      // path applies per-instance.
      const inst = state.indicators[idx];
      if (state._chart && inst) {
        const plugin = INDICATOR_PLUGINS[inst.type];
        if (plugin && typeof plugin.destroy === 'function') {
          try { plugin.destroy({ chart: state._chart, inst }); } catch (e) {}
        } else {
          _clearInstanceSeries(state._chart, inst);
        }
      }
      state.indicators.splice(idx, 1);
      indBtn.updateCount(state.indicators.length);
      _rebuildIndicatorSeries();
      _renderIndicatorOverlays();
      if (state.onConfigChange) state.onConfigChange();
      return true;
    },

    updateIndicator(id, patch) {
      const inst = state.indicators.find((x) => x.id === id);
      if (!inst || !patch || typeof patch !== 'object') return false;
      if (patch.params && typeof patch.params === 'object') {
        inst.params = Object.assign({}, inst.params, patch.params);
      }
      if (patch.styles && typeof patch.styles === 'object') {
        // Merge per-line style patches so callers can submit a partial
        // update (e.g. just one line's color change). Re-run the
        // migration helper afterwards so any missing line entries get
        // re-seeded from plugin defaults.
        const next = Object.assign({}, inst.styles);
        for (const lineId of Object.keys(patch.styles)) {
          next[lineId] = Object.assign({}, next[lineId] || {}, patch.styles[lineId]);
        }
        inst.styles = next;
        _migrateInstanceStyles(inst, INDICATOR_PLUGINS[inst.type]);
      }
      // Param or style changes affect series-creation options (color,
      // lineStyle, etc. bake in at addSeries time), so a full rebuild
      // is needed rather than a render-only path. Cheap in practice —
      // 9 plugins max.
      _rebuildIndicatorSeries();
      _renderIndicatorOverlays();
      if (state.onConfigChange) state.onConfigChange();
      return true;
    },

    getIndicators() { return state.indicators.slice(); },

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
        // Instance array shape. Strip runtime-only fields (_series,
        // _assignedPane) — they'd bloat layout_json and re-seed on
        // next load anyway. Legacy string-array layouts are migrated
        // on load; writes always produce the instance shape so a
        // layout self-heals on first save.
        indicators: state.indicators.map((inst) => {
          // Deep-copy styles so a later mutation can't mutate the
          // saved snapshot. Legacy ``color`` field is dropped on
          // write — _migrateInstanceStyles already fanned it out
          // into styles on load, so there's no value in keeping it.
          const styles = {};
          for (const k of Object.keys(inst.styles || {})) {
            styles[k] = Object.assign({}, inst.styles[k]);
          }
          return {
            id: inst.id,
            type: inst.type,
            params: Object.assign({}, inst.params),
            styles,
          };
        }),
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
      // Clear per-instance series refs so any stray callbacks don't
      // try to push data onto torn-down handles. The chart.remove()
      // above already released the native resources.
      for (const inst of state.indicators) { inst._series = []; }
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
  // Audit r1.1-003: the merge-prior-history helper used by the
  // Workspace chart-panel's refresh path. Exposed for testability
  // — pure function, no side effects. app.js::fetchChartData
  // inlines an equivalent fragment; both paths converge on this
  // helper once the main-chart migration lands (out of scope for
  // this PR).
  mergePriorHistory: _mergePriorHistory,
  // Audit r1.1-004: callers in app.js wrap their raw localStorage
  // reads through this normaliser so a corrupted value (hand-
  // edited storage, downgrade from a future build that added a
  // new IANA entry) collapses to 'local' before it lands in
  // module-level state. Runtime was already safe via the formatter,
  // but the dropdown UI needs a known-good value to highlight the
  // right option.
  normalizeChartTimezone: _normalizeChartTimezone,
  // Plugin architecture. Consumers read INDICATOR_PLUGINS to enumerate
  // built-in + registered plugins; registerIndicatorPlugin lets app.js
  // (or future user code) ship new indicators without touching this
  // file. The indicator-manager modal reads the registry at open
  // time so registrations propagate without a page reload.
  INDICATOR_PLUGINS,
  registerIndicatorPlugin,
  PANEL_TIMEFRAMES,
  CHART_TIMEZONES,
});
