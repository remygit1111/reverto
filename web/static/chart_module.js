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
  };

  // ── DOM scaffold ───────────────────────────────────────────────────
  const panel = document.createElement('div');
  panel.className = 'panel panel-chart';

  const header = document.createElement('div');
  header.className = 'panel-header';

  const titleWrap = document.createElement('div');
  titleWrap.className = 'panel-title-wrap';

  const title = document.createElement('span');
  title.className = 'panel-title';

  const subtitle = document.createElement('span');
  subtitle.className = 'panel-subtitle';
  subtitle.style.marginLeft = '8px';

  titleWrap.appendChild(title);
  titleWrap.appendChild(subtitle);

  const toolbar = document.createElement('div');
  toolbar.className = 'panel-annotations-toolbar';
  const toolbarTools = [
    { tool: 'arrow', label: '→', title: 'Arrow (two clicks)' },
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
  header.appendChild(headerRight);

  const body = document.createElement('div');
  body.className = 'panel-body panel-chart-body';
  const canvasHost = document.createElement('div');
  canvasHost.className = 'panel-chart-canvas';
  canvasHost.style.position = 'relative';
  canvasHost.style.width = '100%';
  canvasHost.style.height = '100%';
  body.appendChild(canvasHost);

  const popover = document.createElement('div');
  popover.className = 'panel-settings-popover hidden';
  panel.appendChild(header);
  panel.appendChild(body);
  panel.appendChild(popover);
  container.appendChild(panel);

  // ── Popover UI ────────────────────────────────────────────────────
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

    const tfSel = document.createElement('select');
    tfSel.dataset.field = 'timeframe';
    for (const tf of PANEL_TIMEFRAMES) {
      const o = document.createElement('option');
      o.value = tf;
      o.textContent = tf;
      if (tf === state.timeframe) o.selected = true;
      tfSel.appendChild(o);
    }

    const indBox = document.createElement('div');
    indBox.className = 'panel-ind-grid';
    for (const t of PANEL_INDICATOR_TYPES) {
      const lb = document.createElement('label');
      lb.className = 'panel-ind-item';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.dataset.indicator = t;
      cb.checked = state.indicators.includes(t);
      lb.appendChild(cb);
      const span = document.createElement('span');
      span.textContent = PANEL_INDICATOR_LABELS[t] || t;
      lb.appendChild(span);
      indBox.appendChild(lb);
    }

    const botSel = document.createElement('select');
    botSel.dataset.field = 'bot';
    const noneOpt = document.createElement('option');
    noneOpt.value = '';
    noneOpt.textContent = '— none —';
    botSel.appendChild(noneOpt);
    const bindingHint = document.createElement('div');
    bindingHint.className = 'panel-binding-hint';
    bindingHint.textContent = '';

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
    popover.appendChild(mkRow('Timeframe', tfSel));
    const indRow = document.createElement('div');
    indRow.className = 'form-row form-row-block';
    const indLabel = document.createElement('label');
    indLabel.textContent = 'Indicators';
    indRow.appendChild(indLabel);
    indRow.appendChild(indBox);
    popover.appendChild(indRow);
    popover.appendChild(mkRow('Bind to bot', botSel));
    popover.appendChild(bindingHint);
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
      const newTf = tfSel.value || state.timeframe;
      const newInds = Array.from(indBox.querySelectorAll('input[type=checkbox]'))
        .filter((c) => c.checked)
        .map((c) => c.dataset.indicator);
      const newBot = botSel.value || '';
      const pairChanged = newPair !== state.pair;
      const tfChanged = newTf !== state.timeframe;
      const indsChanged = newInds.join(',') !== state.indicators.join(',');

      // If the user picked a bot, its pair/timeframe wins — auto-sync.
      // /api/bots returns state (includes pair) but not the YAML
      // config's timeframe, so fetch it explicitly. boundBotUserId
      // is stored in layout_json for future use; the backend scopes
      // every request by session cookie so it isn't actually needed
      // for correctness today.
      let forcedPair = newPair, forcedTf = newTf;
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

      const needsReload = pairChanged || tfChanged
        || forcedPair !== state.pair || forcedTf !== state.timeframe;
      state.pair = forcedPair;
      state.timeframe = forcedTf;
      state.indicators = newInds;

      if (needsReload) {
        _updateTitle();
        await _loadCandles();
      } else if (indsChanged) {
        _rebuildIndicatorSeries();
        _renderIndicatorOverlays();
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
    title.textContent = `${state.pair} ${state.timeframe}`;
    if (state.boundBotSlug) {
      subtitle.textContent = `⚡ ${state.boundBotSlug}`;
      subtitle.classList.add('bound');
    } else {
      subtitle.textContent = '';
      subtitle.classList.remove('bound');
    }
  }

  // ── Chart init ────────────────────────────────────────────────────
  function _initChart() {
    if (typeof window.LightweightCharts === 'undefined') return false;
    const LWC = window.LightweightCharts;
    const layoutFn = typeof _chartLayoutOpts === 'function' ? _chartLayoutOpts : null;
    const opts = layoutFn ? layoutFn() : {};
    state._chart = LWC.createChart(canvasHost, {
      ...opts,
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
    _rebuildIndicatorSeries();
    if (typeof ResizeObserver !== 'undefined') {
      state._resizeObs = new ResizeObserver((entries) => {
        for (const e of entries) {
          if (e.target !== canvasHost || !state._chart) continue;
          const w = e.contentRect.width;
          const h = e.contentRect.height || 200;
          state._chart.applyOptions({ width: w, height: h });
          _renderAnnotations();
        }
      });
      state._resizeObs.observe(canvasHost);
    }
    try {
      state._chart.timeScale().subscribeVisibleLogicalRangeChange(_renderAnnotations);
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
    const blue = _cssVar('--blue', '#5b8dee');
    const amber = _cssVar('--amber', '#ffb347');
    for (const a of (state._annotations || [])) {
      const color = a.color || (a.type === 'text' ? amber : blue);
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
      } else if (a.type === 'arrow') {
        const x2 = _xOfTime(a.x2);
        const y2 = _yOfPrice(a.y2);
        if (x2 == null || y2 == null) continue;
        const g = _panelSvg('g', { 'data-ann-id': a.id });
        g.appendChild(_panelSvg('line', { x1, y1, x2, y2, stroke: color, 'stroke-width': 2 }));
        const dx = x2 - x1, dy = y2 - y1;
        const len = Math.sqrt(dx * dx + dy * dy);
        if (len > 0.01) {
          const ux = dx / len, uy = dy / len;
          const bx = x2 - ux * 10, by = y2 - uy * 10;
          const px = -uy * 5, py = ux * 5;
          g.appendChild(_panelSvg('polygon', { points: `${x2},${y2} ${bx + px},${by + py} ${bx - px},${by - py}`, fill: color }));
        }
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
      if (a.x1 != null) { tMin = Math.min(tMin, a.x1); tMax = Math.max(tMax, a.x1); }
      if (a.x2 != null) { tMin = Math.min(tMin, a.x2); tMax = Math.max(tMax, a.x2); }
      if (a.y1 != null) { pMin = Math.min(pMin, a.y1); pMax = Math.max(pMax, a.y1); }
      if (a.y2 != null) { pMin = Math.min(pMin, a.y2); pMax = Math.max(pMax, a.y2); }
    }
    const tSpan = Math.max(1, tMax - tMin);
    const pSpan = Math.max(1, pMax - pMin);
    let best = null, bestD = Infinity;
    for (const a of state._annotations) {
      const dt = ((Number(a.x1) || point.time) - point.time) / tSpan;
      const dp = ((Number(a.y1) || point.price) - point.price) / pSpan;
      const d = dt * dt + dp * dp;
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

  for (const key of ['arrow', 'text', 'delete']) {
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
      if (state._activeTool === 'arrow') {
        if (!state._toolFirstPoint) {
          state._toolFirstPoint = point;
        } else {
          _persistAnnotation({
            type: 'arrow',
            x1: state._toolFirstPoint.time, y1: state._toolFirstPoint.price,
            x2: point.time, y2: point.price,
            color: _cssVar('--blue', '#5b8dee'),
          });
          state._toolFirstPoint = null;
          _setActiveTool('select');
        }
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
      _updateTitle();
      await _loadCandles();
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
      };
    },

    async destroy() {
      if (state._destroyed) return;
      state._destroyed = true;
      if (state._refreshTimer) { clearInterval(state._refreshTimer); state._refreshTimer = null; }
      if (state._resizeObs) { try { state._resizeObs.disconnect(); } catch (e) {} state._resizeObs = null; }
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
// The functions above are still available as plain globals (app.js
// call sites use them that way) — the namespace is additive, giving
// future code an explicit import target. PR 3b grows this with
// createPanelChart, the chart-instance factory consumed by the
// Workspace chart-panel.
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
  PANEL_INDICATOR_TYPES,
  PANEL_INDICATOR_LABELS,
  PANEL_TIMEFRAMES,
});
