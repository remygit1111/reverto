// web/static/app.js — Reverto portal frontend
// Moved out of an inline <script> in index.html so CSP doesn't need
// 'unsafe-inline' on script-src. All event handlers are wired via
// addEventListener in setupEventListeners() — no onclick="..." attributes.

// ── Theme ─────────────────────────────────────────────────────────────────────
const t0 = localStorage.getItem('reverto-theme') || 'dark';
document.documentElement.setAttribute('data-theme', t0);
function toggleTheme() {
  const n = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', n);
  localStorage.setItem('reverto-theme', n);
}

// ── API Key management ────────────────────────────────────────────────────────
// _pendingAction holds a zero-arg callback that should run after the user
// enters a valid API key. Used when an action (bot start/stop, wizard save)
// hits a 401 or discovers there is no key — we stash the action, show the
// modal, and rerun it from saveApiKey() so in-progress wizard state is not
// lost to a page reload.
let _pendingAction = null;

function getApiKey() {
  return localStorage.getItem('reverto_api_key') || '';
}
function showApiKeyModal() {
  const modal = document.getElementById('api-key-modal');
  document.getElementById('api-key-input').value = getApiKey();
  modal.classList.add('show');
}
function closeApiKeyModal() {
  document.getElementById('api-key-modal').classList.remove('show');
}
function saveApiKey() {
  const key = document.getElementById('api-key-input').value.trim();
  if (!key) { alert('Empty key — not saved'); return; }
  localStorage.setItem('reverto_api_key', key);
  closeApiKeyModal();
  // Resume whatever action was waiting for a valid key. If nothing was
  // pending (first-time visit), we deliberately do NOT reload the page
  // so the user stays where they are.
  if (_pendingAction) {
    const fn = _pendingAction;
    _pendingAction = null;
    fn();
  }
}
function clearApiKey() {
  localStorage.removeItem('reverto_api_key');
  document.getElementById('api-key-input').value = '';
  closeApiKeyModal();
  // Full reset: reload so WebSockets re-connect without a key (and get
  // rejected cleanly) and the UI shows the modal again on next load.
  _pendingAction = null;
  location.reload();
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
function safeText(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function fmtPrice(n) {
  if (!n) return '—';
  return '$' + parseFloat(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPnl(v, decimals = 8) {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : ''; const c = v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu';
  return `<span class="${c}">${s}${v.toFixed(decimals)}</span>`;
}
function fmtPct(v) {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : ''; const c = v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu';
  return `<span class="${c}">${s}${v.toFixed(2)}%</span>`;
}
function timeAgo(iso) {
  if (!iso) return '—';
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function reasonBadge(r) {
  if (!r) return '—';
  const c = r === 'tp' ? 'badge-tp' : r === 'sl' ? 'badge-sl' : 'badge-open';
  return `<span class="badge ${c}">${r.toUpperCase()}</span>`;
}
function logCls(l) {
  if (l.includes('[ERROR]')) return 'err';
  if (l.includes('[WARNING]')) return 'warn';
  if (l.includes('[DEBUG]')) return 'dbg';
  return 'info';
}

// ── State ─────────────────────────────────────────────────────────────────────
let currentSlug = null;
let ws = null;
let detailInterval = null;
let overviewInterval = null;

// ── Navigation ────────────────────────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  $('view-' + name).classList.add('active');
}

function showDTab(name, btn) {
  ['dashboard', 'deals', 'config', 'log'].forEach(n => {
    const el = $('dtab-' + n);
    if (el) { el.classList.toggle('hidden', n !== name); }
  });
  document.querySelectorAll('.detail-subnav .tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (name === 'config' && currentSlug) fetchDetailConfig(currentSlug);
}

// ── Overview ──────────────────────────────────────────────────────────────────
async function fetchOverview() {
  try {
    const d = await fetch('/api/bots').then(r => r.json());
    renderOverview(d);
  } catch (e) {}
}

function renderOverview(d) {
  // Een /api/bots fetch voedt drie views: Overview (summary + status),
  // Bots (grid) en Active Deals (tabel). Splits de render zodat elk
  // view zijn eigen DOM-tak update zonder afhankelijk te zijn van welk
  // view actief is.
  const sum  = d.summary || {};
  const bots = d.bots || [];
  const deals = d.all_open_deals || [];

  // Header price uit eerste running bot
  const runningBot = bots.find(b => b.running && b.current_price);
  if (runningBot) {
    $('hdr-price').textContent = fmtPrice(runningBot.current_price);
    $('hdr-pair').textContent = runningBot.pair || 'BTC/USD';
  }

  renderOverviewSummary(sum, bots);
  renderBotGrid(bots);
  renderActiveDeals(deals);
}

function renderOverviewSummary(sum, bots) {
  const pnl = sum.total_pnl_btc || 0;
  $('ov-pnl').innerHTML = fmtPnl(pnl, 8);
  $('ov-active').textContent = sum.active_bots ?? '—';
  $('ov-total-sub').textContent = `of ${sum.total_bots ?? 0} configured`;
  $('ov-deals').textContent = sum.open_deals ?? '—';

  const running = bots.filter(b => b.running).length;
  const stopped = bots.length - running;
  $('ov-running-count').textContent = running;
  $('ov-stopped-count').textContent = stopped;
}

function renderBotGrid(bots) {
  const grid = $('bot-grid');
  if (!grid) return;
  if (!bots.length) {
    grid.innerHTML = '<div class="empty-config-msg">No bots configured — use ＋ New Bot to add one.</div>';
  } else {
    grid.innerHTML = bots.map(b => renderBotCard(b)).join('');
  }
}

function renderActiveDeals(deals) {
  const tbody = $('all-deals-tbody');
  if (!tbody) return;
  if (!deals.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No open deals across any bot</td></tr>';
  } else {
    tbody.innerHTML = deals.map(deal => `<tr>
      <td><span class="link-like" data-action="open" data-slug="${safeText(deal.bot_slug)}">${safeText(deal.bot_name)}</span></td>
      <td class="muted-cell">${safeText(deal.id)}</td>
      <td>${safeText(deal.symbol || '—')}</td>
      <td>${fmtPrice(deal.entry_price)}</td>
      <td>${fmtPrice(deal.avg_entry_price)}</td>
      <td>${deal.order_count}</td>
      <td>${fmtPnl(deal.pnl_btc)}</td>
      <td>${fmtPct(deal.pnl_pct)}</td>
      <td class="muted-cell">${timeAgo(deal.opened_at)}</td>
    </tr>`).join('');
  }
}

function renderBotCard(b) {
  const running = b.running;
  const pnl = b.total_pnl_btc || 0;
  const pnlSign = pnl >= 0 ? '+' : '';
  const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu';

  const openDealsHtml = (b.open_deals || []).slice(0, 3).map(d => `
    <div class="bot-card-deal-row">
      <span class="deal-id-cell">${safeText(d.id)}</span>
      <span class="muted-cell">${fmtPrice(d.entry_price)}</span>
      <span>${fmtPnl(d.pnl_btc)}</span>
    </div>`).join('');

  const moreDeals = (b.open_deals_count || 0) > 3
    ? `<div class="more-deals-row">+${b.open_deals_count - 3} more deals</div>`
    : '';

  return `
  <div class="bot-card">
    <div class="bot-card-top">
      <span class="bot-card-name">${safeText(b.bot_name || b.slug)}</span>
      <div class="pill ${running ? 'running' : 'stopped'} tab-pill-static">
        <div class="dot"></div>${running ? 'Running' : 'Stopped'}
      </div>
    </div>
    <div class="bot-card-meta">
      ${safeText((b.exchange || '—').toUpperCase())} · ${safeText(b.pair || 'BTC/USD')} · ${safeText((b.mode || 'paper').toUpperCase())}
      ${b.uptime ? '· ⏱ ' + safeText(b.uptime) : ''}
    </div>
    <div class="bot-card-stats">
      <div class="bot-stat">
        <div class="bot-stat-label">Price</div>
        <div class="bot-stat-value">${b.current_price ? fmtPrice(b.current_price) : '—'}</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">Balance</div>
        <div class="bot-stat-value">${b.balance_btc ? b.balance_btc.toFixed(4) : '—'}</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">Win rate</div>
        <div class="bot-stat-value">${b.win_rate || 0}%</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">PnL</div>
        <div class="bot-stat-value ${pnlCls}">${pnlSign}${pnl.toFixed(6)}</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">Open deals</div>
        <div class="bot-stat-value">${b.open_deals_count || 0}</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">Closed</div>
        <div class="bot-stat-value">${b.closed_deals_count || 0}</div>
      </div>
    </div>
    ${openDealsHtml ? `<div class="bot-card-deals">${openDealsHtml}${moreDeals}</div>` : ''}
    <div class="bot-card-footer">
      ${running
        ? `<button class="btn-sm btn-stop"    data-action="stop"    data-slug="${safeText(b.slug)}">■ Stop</button>
           <button class="btn-sm btn-restart" data-action="restart" data-slug="${safeText(b.slug)}">↺ Restart</button>`
        : `<button class="btn-sm btn-start"   data-action="start"   data-slug="${safeText(b.slug)}">▶ Start</button>
           <button class="btn-sm btn-delete"  data-action="delete"  data-slug="${safeText(b.slug)}" data-name="${safeText(b.bot_name || b.slug)}">✕ Delete</button>`
      }
      <button class="btn-sm btn-open" data-action="open" data-slug="${safeText(b.slug)}">Open →</button>
    </div>
  </div>`;
}

// Click delegation — slug komt uit data-slug (escaped via safeText), nooit
// in een onclick-string, dus kan niet uit het attribuut breken.
document.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  const slug = el.dataset.slug;
  if (!slug) return;
  if (action === 'open') openBot(slug);
  else if (action === 'delete') deleteBot(slug, el.dataset.name || slug);
  else if (['start', 'stop', 'restart'].includes(action)) botAction(slug, action, el);
});

// ── Delete bot ───────────────────────────────────────────────────────────────
async function deleteBot(slug, name) {
  if (!confirm(`Delete bot '${name}'? This cannot be undone.`)) return;
  if (!getApiKey()) {
    _pendingAction = () => deleteBot(slug, name);
    showApiKeyModal();
    return;
  }
  const res = await fetch(`/api/bots/${slug}`, {
    method: 'DELETE',
    headers: { 'X-API-Key': getApiKey() },
  });
  if (res.status === 401) {
    _pendingAction = () => deleteBot(slug, name);
    alert('Auth error — check your API key');
    showApiKeyModal();
    return;
  }
  let detail = '';
  try { detail = (await res.json()).detail || ''; } catch (e) {}
  if (!res.ok) {
    alert(`Delete failed: ${detail || res.status}`);
    return;
  }
  // If we're currently inside this bot's detail view, bounce back to Bots.
  if (currentSlug === slug) goBots();
  else fetchOverview();
}

// ── Bot actions ───────────────────────────────────────────────────────────────
function _debounceBotButtons(slug, action, ms = 3000) {
  // Disable every visible button that matches this (slug, action) pair
  // immediately after a click. Cheap client-side guard against double
  // clicks; the backend start_bot() still holds a starting-slot so
  // belt-and-suspenders — even if the re-render after fetchOverview()
  // replaces the button with a fresh one, a second backend call will
  // get "Bot is already starting".
  const sel = `[data-action="${CSS.escape(action)}"][data-slug="${CSS.escape(slug)}"]`;
  const btns = Array.from(document.querySelectorAll(sel));
  btns.forEach(b => { b.disabled = true; });
  setTimeout(() => btns.forEach(b => { b.disabled = false; }), ms);
}

// Busy-label map so Start/Stop/Restart buttons immediately reflect the
// in-flight action. Restored in finally{} so success and error both
// re-enable the button cleanly.
const _BUSY_LABELS = {
  start:   'Starting...',
  stop:    'Stopping...',
  restart: 'Restarting...',
};

async function _withButtonFeedback(btn, action, fn) {
  if (!btn) return await fn();
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = _BUSY_LABELS[action] || 'Working...';
  try {
    return await fn();
  } finally {
    btn.innerHTML = orig;
    btn.disabled = false;
  }
}

async function botAction(slug, action, srcBtn = null) {
  _debounceBotButtons(slug, action);
  // No key yet → queue the action and prompt for one. Once the user
  // enters a valid key in the modal, saveApiKey() re-invokes this.
  if (!getApiKey()) {
    _pendingAction = () => botAction(slug, action);
    showApiKeyModal();
    return;
  }
  await _withButtonFeedback(srcBtn, action, async () => {
    try {
      const res = await fetch(`/api/bots/${slug}/${action}`, {
        method: 'POST',
        headers: { 'X-API-Key': getApiKey() }
      });
      if (res.status === 401) {
        _pendingAction = () => botAction(slug, action);
        alert('Auth error — check your API key');
        showApiKeyModal();
        return;
      }
      const r = await res.json();
      if (!r.ok) alert(`${action} failed: ${r.error}`);
    } catch (e) {
      alert(`${action} failed: ${e.message}`);
    }
  });
  // Refresh after the busy-label has been restored so the next render
  // reflects the real running-state.
  fetchOverview();
  if (currentSlug === slug) fetchDetail(slug);
}

// ── Top-level tab navigation ─────────────────────────────────────────────────
function _setActiveTab(btnId) {
  document.querySelectorAll('#main-nav .tab').forEach(t => t.classList.remove('active'));
  const btn = $(btnId);
  if (btn) btn.classList.add('active');
}

function _resetHeaderForTopLevel() {
  // When leaving the detail view: reset detail-specific header bits
  // and clean up any detail polling / websocket.
  currentSlug = null;
  clearInterval(detailInterval);
  if (ws) { ws.close(); ws = null; }
  $('hdr-context').textContent = 'Multi-Bot Portal';
  $('hdr-context').classList.remove('clickable');
  $('hdr-context').onclick = null;
  $('hdr-pill').classList.add('hidden');
  $('hdr-uptime').textContent = '';
}

function _ensureOverviewPolling() {
  if (!overviewInterval) {
    overviewInterval = setInterval(fetchOverview, 5000);
  }
}

function goOverview() {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-overview-btn');
  showPage('overview');
  fetchOverview();
  _ensureOverviewPolling();
}

function goBots() {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-bots-btn');
  showPage('bots');
  fetchOverview();
  _ensureOverviewPolling();
}

function goDeals() {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-deals-btn');
  showPage('deals');
  fetchOverview();
  _ensureOverviewPolling();
}

function goNewBot() {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-bots-btn');  // new bot lives logically under Bots
  showPage('new-bot');
  nbInit();
}

// ── New bot single-page form ─────────────────────────────────────────────────
// Short inline help shown under the indicator type dropdown so a new
// operator knows what each filter does without leaving the wizard.
const NB_INDICATOR_DESCRIPTIONS = {
  RSI:
    "Measures momentum. Signals when the market is overbought or oversold " +
    "based on recent price changes.",
  EMA_CROSS:
    "Compares two moving averages. A crossover signals a potential trend change.",
  MACD:
    "Tracks trend momentum using the difference between two moving averages " +
    "and a signal line.",
  BOLLINGER:
    "Volatility envelope around a moving average. Signals when price touches " +
    "an extreme band or when the bands squeeze tight.",
  PARABOLIC_SAR:
    "Trailing stop-and-reverse indicator. Signals when price crosses the " +
    "SAR dots, flipping the trend direction.",
  SUPERTREND:
    "Volatility-based trailing stop using ATR. Signals when price crosses " +
    "the supertrend line, flipping the trend direction.",
  MARKET_STRUCTURE:
    "Detects swing highs and lows to identify trend structure (HH/HL or " +
    "LH/LL) and Break of Structure events.",
  SUPPORT_RESISTANCE:
    "Clusters recent swing points into support and resistance levels and " +
    "signals when price approaches or breaks through them.",
  QFL:
    "QFL Base Scanner. Tracks consolidation lows that were rejected fast " +
    "and signals when price returns to or cracks a validated base.",
};

let nbState = null;
// When set, nbSubmit() PUTs to /api/bots/{slug}/config instead of POSTing a
// new bot. editBot() sets this, nbInit() clears it. Cleared again on success.
let nbEditSlug = null;

function nbDefaultState() {
  return {
    name: '', exchange: 'bitget', pair: 'BTC/USD', mode: 'paper', direction: 'long',
    leverage_enabled: false, leverage_size: 1, timeframe: '1h',
    base_unit: 'btc', base_size: 0.001,
    indicators: [],
    tp_target_pct: 3.0, tp_indicator_confirm: '',
    tp_min_pct: null,
    tp_max_age_enabled: false, tp_max_age_hours: 24,
    sl_type: 'fixed', sl_pct: 5.0,
    dca_max_orders: 4, dca_size: 0.001, dca_spacing_pct: 2.5,
    dca_volume_scale: 1.0, dca_step_scale: 1.0,
    schedule_timezone: 'Europe/Amsterdam',
    schedule_windows: [],
    schedule_blackouts: [],
  };
}

const NB_SCHED_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

function nbRenderScheduleWindows() {
  const wrap = $('nb-sched-windows');
  if (!wrap) return;
  if (!nbState.schedule_windows.length) {
    wrap.innerHTML = '<div class="empty-config-msg">No trading windows — bot trades 24/7</div>';
    return;
  }
  wrap.innerHTML = '';
  nbState.schedule_windows.forEach((w, idx) => {
    const row = document.createElement('div');
    row.className = 'sched-window';
    row.dataset.nbSchedIdx = String(idx);

    const days = document.createElement('div');
    days.className = 'sched-window-days';
    NB_SCHED_DAYS.forEach(day => {
      const lbl = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'checkbox-accent';
      cb.checked = (w.days || []).includes(day);
      cb.addEventListener('change', () => {
        const set = new Set(nbState.schedule_windows[idx].days || []);
        if (cb.checked) set.add(day); else set.delete(day);
        nbState.schedule_windows[idx].days = NB_SCHED_DAYS.filter(d => set.has(d));
        nbRecompute();
      });
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(' ' + day));
      days.appendChild(lbl);
    });
    row.appendChild(days);

    const times = document.createElement('div');
    times.className = 'sched-window-times';

    const fromLbl = document.createElement('span');
    fromLbl.textContent = 'From';
    times.appendChild(fromLbl);
    const fromInp = document.createElement('input');
    fromInp.type = 'time';
    fromInp.value = w.from || '09:00';
    fromInp.addEventListener('change', () => {
      nbState.schedule_windows[idx].from = fromInp.value || '00:00';
      nbRecompute();
    });
    times.appendChild(fromInp);

    const toLbl = document.createElement('span');
    toLbl.textContent = 'To';
    times.appendChild(toLbl);
    const toInp = document.createElement('input');
    toInp.type = 'time';
    toInp.value = w.to || '17:00';
    toInp.addEventListener('change', () => {
      nbState.schedule_windows[idx].to = toInp.value || '00:00';
      nbRecompute();
    });
    times.appendChild(toInp);

    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'hbtn hbtn-theme btn-danger sched-window-remove';
    rm.textContent = '✕ Remove';
    rm.addEventListener('click', () => {
      nbState.schedule_windows.splice(idx, 1);
      nbRenderScheduleWindows();
      nbRecompute();
    });
    times.appendChild(rm);

    row.appendChild(times);
    wrap.appendChild(row);
  });
}

function nbInit() {
  nbState = nbDefaultState();
  nbEditSlug = null;
  const btn = $('nb-submit-btn');
  if (btn) btn.textContent = 'Save bot';
  nbApplyStateToForm();
  nbRenderScheduleWindows();
  nbHideError();
  nbRecompute();
  nbApplyMobileCollapse();
}

// On narrow viewports, collapse every wizard section except the first
// so users don't have to scroll past five fully-open sections before
// reaching the Save button. Desktop keeps all sections open.
function nbApplyMobileCollapse() {
  const isNarrow = window.matchMedia('(max-width: 600px)').matches;
  const sections = document.querySelectorAll('#view-new-bot .wizard-section');
  sections.forEach((el, i) => {
    if (isNarrow && i > 0) {
      el.removeAttribute('open');
    } else {
      el.setAttribute('open', '');
    }
  });
}

function nbShowError(msg) {
  const el = $('nb-error');
  el.innerHTML = msg;
  el.classList.remove('hidden');
}
function nbHideError() {
  $('nb-error').classList.add('hidden');
}

function nbReadAll() {
  // Pull all form fields into nbState. Called on every input change and
  // before submit/validation, so the rest of the wizard can read from
  // a single source of truth.
  nbState.name = $('nb-name').value.trim();
  nbState.exchange = $('nb-exchange').value;
  nbState.pair = $('nb-pair').value.trim();
  nbState.mode = $('nb-mode').value;
  nbState.direction = $('nb-direction').value;
  nbState.leverage_enabled = $('nb-leverage-enabled').checked;
  nbState.leverage_size = parseInt($('nb-leverage-size').value, 10);
  nbState.timeframe = $('nb-timeframe').value;

  nbState.base_size = parseFloat($('nb-base-size').value);

  nbState.tp_target_pct = parseFloat($('nb-tp-pct').value);
  nbState.tp_indicator_confirm = $('nb-tp-confirm').value;
  const minRaw = $('nb-tp-min-pct').value;
  nbState.tp_min_pct = minRaw === '' ? null : parseFloat(minRaw);
  nbState.tp_max_age_enabled = $('nb-tp-max-age-enabled').checked;
  nbState.tp_max_age_hours = parseInt($('nb-tp-max-age-hours').value, 10);
  nbState.sl_type = $('nb-sl-type').value;
  nbState.sl_pct = parseFloat($('nb-sl-pct').value);

  // dca_max_orders is the user-facing DCA-only count. Clamp to [0, 49];
  // serializer adds +1 to write the YAML's base+DCA max_orders.
  let _dcaMax = parseInt($('nb-dca-max').value, 10);
  if (isNaN(_dcaMax)) _dcaMax = 0;
  if (_dcaMax < 0) _dcaMax = 0;
  if (_dcaMax > 49) _dcaMax = 49;
  nbState.dca_max_orders = _dcaMax;
  nbState.dca_size = parseFloat($('nb-dca-size').value);
  nbState.dca_spacing_pct = parseFloat($('nb-dca-spacing').value);
  nbState.dca_volume_scale = parseFloat($('nb-dca-volume').value);
  nbState.dca_step_scale = parseFloat($('nb-dca-step').value);

  const tzEl = $('nb-sched-tz');
  if (tzEl) nbState.schedule_timezone = tzEl.value.trim() || 'Europe/Amsterdam';
  const blEl = $('nb-sched-blackouts');
  if (blEl) {
    nbState.schedule_blackouts = blEl.value
      .split('\n')
      .map(s => s.trim())
      .filter(s => s.length > 0);
  }
}

function nbValidateAll() {
  const errors = [];
  if (!nbState.name) errors.push('General: name is required');
  else if (!/^[a-zA-Z0-9 \-_]+$/.test(nbState.name))
    errors.push('General: name may only contain letters, digits, spaces, "-" and "_"');
  else if (nbState.name.length > 100)
    errors.push('General: name max 100 characters');
  if (!nbState.pair) errors.push('General: trading pair is required');

  if (!nbState.base_size || nbState.base_size <= 0)
    errors.push('Entry: base order size must be > 0');

  if (!nbState.tp_target_pct || nbState.tp_target_pct <= 0)
    errors.push('Take Profit: target % must be > 0');
  if (!nbState.sl_pct || nbState.sl_pct <= 0)
    errors.push('Stop Loss: percentage must be > 0');

  if (nbState.dca_max_orders == null || isNaN(nbState.dca_max_orders) ||
      nbState.dca_max_orders < 0 || nbState.dca_max_orders > 49)
    errors.push('DCA: max DCA orders must be between 0 and 49');
  if (!nbState.dca_spacing_pct || nbState.dca_spacing_pct <= 0)
    errors.push('DCA: order spacing must be > 0');

  return errors;
}

function nbRecompute() {
  // Re-read form, refresh DCA preview + review section. Called on every
  // input change so the user sees live updates without having to click
  // through wizard steps.
  if (!nbState) return;
  nbReadAll();
  nbUpdateLeverageUI();
  nbUpdateMinTpHint();
  nbRenderDcaPreview();
  nbRenderReview();
}

function nbUpdateMinTpHint() {
  const hint = $('nb-tp-min-hint');
  if (!hint) return;
  if (nbState.tp_min_pct != null && nbState.tp_min_pct > 0) {
    hint.textContent =
      `Position will only close if price is at least ${nbState.tp_min_pct}% above average entry`;
  } else {
    hint.textContent = '';
  }
}

function nbApplyStateToForm() {
  $('nb-name').value = nbState.name;
  $('nb-exchange').value = nbState.exchange;
  $('nb-pair').value = nbState.pair;
  $('nb-mode').value = nbState.mode;
  $('nb-direction').value = nbState.direction;
  $('nb-leverage-enabled').checked = nbState.leverage_enabled;
  $('nb-leverage-size').value = nbState.leverage_size;
  $('nb-timeframe').value = nbState.timeframe;
  $('nb-base-size').value = nbState.base_size;
  document.querySelectorAll('[data-base-unit]').forEach(b => {
    b.classList.toggle('active', b.dataset.baseUnit === nbState.base_unit);
  });
  $('nb-base-unit-label').textContent = nbState.base_unit === 'btc' ? 'BTC' : '%';
  $('nb-dca-unit-label').textContent = nbState.base_unit === 'btc' ? 'BTC' : '%';

  $('nb-tp-pct').value = nbState.tp_target_pct;
  $('nb-tp-confirm').value = nbState.tp_indicator_confirm;
  $('nb-tp-min-pct').value = nbState.tp_min_pct == null ? '' : nbState.tp_min_pct;
  $('nb-tp-max-age-enabled').checked = nbState.tp_max_age_enabled;
  $('nb-tp-max-age-hours').value = nbState.tp_max_age_hours;
  $('nb-tp-max-age-hours').disabled = !nbState.tp_max_age_enabled;
  $('nb-sl-type').value = nbState.sl_type;
  $('nb-sl-pct').value = nbState.sl_pct;

  $('nb-dca-max').value = nbState.dca_max_orders;
  $('nb-dca-size').value = nbState.dca_size;
  $('nb-dca-spacing').value = nbState.dca_spacing_pct;
  $('nb-dca-volume').value = nbState.dca_volume_scale;
  $('nb-dca-step').value = nbState.dca_step_scale;

  const tzEl = $('nb-sched-tz');
  if (tzEl) tzEl.value = nbState.schedule_timezone || 'Europe/Amsterdam';
  const blEl = $('nb-sched-blackouts');
  if (blEl) blEl.value = (nbState.schedule_blackouts || []).join('\n');

  nbRenderIndicators();
  nbRenderScheduleWindows();
  nbUpdateLeverageUI();
}

function nbToggleBaseUnit(unit) {
  nbState.base_unit = unit;
  document.querySelectorAll('[data-base-unit]').forEach(b => {
    b.classList.toggle('active', b.dataset.baseUnit === unit);
  });
  $('nb-base-unit-label').textContent = unit === 'btc' ? 'BTC' : '%';
  $('nb-dca-unit-label').textContent = unit === 'btc' ? 'BTC' : '%';
}

function nbAddIndicator() {
  nbState.indicators.push({
    type: 'RSI', timeframe: '1h',
    period: 14, threshold: 'below_35',
    // RSI condition/value are derived from threshold ("below_35" → below, 35)
    // but we also keep them on the row for easy editing.
    rsi_condition: 'below', rsi_value: 35,
    fast: 9, slow: 21, signal: 'bullish_cross',
    condition: 'histogram_positive',
    macd_fast: 12, macd_slow: 26, macd_signal: 9,
  });
  nbRenderIndicators();
}
function nbRemoveIndicator(idx) {
  nbState.indicators.splice(idx, 1);
  nbRenderIndicators();
}

function nbRenderIndicators() {
  const list = $('nb-indicators-list');
  if (!list) return;
  if (!nbState.indicators.length) {
    list.innerHTML = '<div class="empty-config-msg">Always enter (no filter)</div>';
    return;
  }
  list.innerHTML = nbState.indicators.map((ind, i) => {
    const typeClass = ind.type === 'RSI' ? 'type-rsi'
                    : ind.type === 'EMA_CROSS' ? 'type-ema'
                    : ind.type === 'MACD' ? 'type-macd'
                    : ind.type === 'BOLLINGER' ? 'type-bollinger'
                    : ind.type === 'PARABOLIC_SAR' ? 'type-psar'
                    : ind.type === 'SUPERTREND' ? 'type-supertrend'
                    : ind.type === 'MARKET_STRUCTURE' ? 'type-ms'
                    : ind.type === 'SUPPORT_RESISTANCE' ? 'type-sr'
                    : ind.type === 'QFL' ? 'type-qfl'
                    : '';
    const title = ind.type === 'EMA_CROSS' ? 'EMA Cross'
                : ind.type === 'BOLLINGER' ? 'Bollinger Bands'
                : ind.type === 'PARABOLIC_SAR' ? 'Parabolic SAR'
                : ind.type === 'SUPERTREND' ? 'Supertrend'
                : ind.type === 'MARKET_STRUCTURE' ? 'Market Structure'
                : ind.type === 'SUPPORT_RESISTANCE' ? 'Support & Resistance'
                : ind.type === 'QFL' ? 'QFL Base Scanner'
                : ind.type;
    return `
      <div class="nb-ind-card ${typeClass}">
        <div class="nb-ind-head">
          <span class="nb-ind-title">${safeText(title)}</span>
          <button type="button" class="nb-ind-close" data-nb-remove="${i}" title="Remove indicator">×</button>
        </div>
        <div class="nb-ind-body">
          <div class="form-row form-row-wide">
            <label>Type</label>
            <select data-nb-ind="${i}" data-nb-field="type">
              <option value="RSI" ${ind.type === 'RSI' ? 'selected' : ''}>RSI</option>
              <option value="EMA_CROSS" ${ind.type === 'EMA_CROSS' ? 'selected' : ''}>EMA Cross</option>
              <option value="MACD" ${ind.type === 'MACD' ? 'selected' : ''}>MACD</option>
              <option value="BOLLINGER" ${ind.type === 'BOLLINGER' ? 'selected' : ''}>Bollinger Bands</option>
              <option value="PARABOLIC_SAR" ${ind.type === 'PARABOLIC_SAR' ? 'selected' : ''}>Parabolic SAR</option>
              <option value="SUPERTREND" ${ind.type === 'SUPERTREND' ? 'selected' : ''}>Supertrend</option>
              <option value="MARKET_STRUCTURE" ${ind.type === 'MARKET_STRUCTURE' ? 'selected' : ''}>Market Structure</option>
              <option value="SUPPORT_RESISTANCE" ${ind.type === 'SUPPORT_RESISTANCE' ? 'selected' : ''}>Support &amp; Resistance</option>
              <option value="QFL" ${ind.type === 'QFL' ? 'selected' : ''}>QFL Base Scanner</option>
            </select>
            <div class="nb-ind-desc">${safeText(NB_INDICATOR_DESCRIPTIONS[ind.type] || '')}</div>
          </div>
          <div class="form-row">
            <label>Timeframe</label>
            <select data-nb-ind="${i}" data-nb-field="timeframe">
              ${['15m', '1h', '4h', '1d'].map(t =>
                `<option value="${t}" ${ind.timeframe === t ? 'selected' : ''}>${t}</option>`
              ).join('')}
            </select>
          </div>
          ${nbIndicatorFieldsHtml(ind, i)}
        </div>
      </div>
    `;
  }).join('');
}

function nbIndicatorFieldsHtml(ind, i) {
  if (ind.type === 'RSI') {
    // Parse threshold back into condition + value so the row always
    // stays in sync even after an edit-load from YAML.
    const parsed = _parseRsiThreshold(ind.threshold);
    const cond = ind.rsi_condition || parsed.condition;
    const val = ind.rsi_value != null ? ind.rsi_value : parsed.value;
    const CONDS = [
      ['cross_above', 'Crosses above X'],
      ['cross_below', 'Crosses below X'],
      ['above', 'Greater than X'],
      ['below', 'Lower than X'],
    ];
    return `
      <div class="form-row">
        <label>Period</label>
        <input type="number" min="5" max="50" value="${ind.period}" data-nb-ind="${i}" data-nb-field="period">
      </div>
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="rsi_condition">
          ${CONDS.map(([v, label]) =>
            `<option value="${v}" ${cond === v ? 'selected' : ''}>${label}</option>`
          ).join('')}
        </select>
      </div>
      <div class="form-row">
        <label>Value (X)</label>
        <input type="number" min="1" max="99" step="1" value="${val}" data-nb-ind="${i}" data-nb-field="rsi_value">
      </div>`;
  }
  if (ind.type === 'EMA_CROSS') {
    return `
      <div class="form-row">
        <label>Fast period</label>
        <input type="number" min="2" value="${ind.fast}" data-nb-ind="${i}" data-nb-field="fast">
      </div>
      <div class="form-row">
        <label>Slow period</label>
        <input type="number" min="2" value="${ind.slow}" data-nb-ind="${i}" data-nb-field="slow">
      </div>
      <div class="form-row">
        <label>Signal</label>
        <select data-nb-ind="${i}" data-nb-field="signal">
          <option value="bullish_cross" ${ind.signal === 'bullish_cross' ? 'selected' : ''}>Bullish</option>
          <option value="bearish_cross" ${ind.signal === 'bearish_cross' ? 'selected' : ''}>Bearish</option>
        </select>
      </div>`;
  }
  if (ind.type === 'QFL') {
    const lb = ind.lookback != null ? ind.lookback : 3;
    const crack = ind.crack_pct != null ? ind.crack_pct : 3.0;
    const bc = ind.base_candles != null ? ind.base_candles : 5;
    const mb = ind.max_bases != null ? ind.max_bases : 5;
    const bp = ind.below_pct != null ? ind.below_pct : 0.0;
    const QFL_CONDS = [
      ['below_base', 'Below base'],
      ['near_base', 'Near base'],
      ['base_retest', 'Base retest'],
    ];
    return `
      <div class="form-row">
        <label>Lookback</label>
        <input type="number" min="1" value="${lb}" data-nb-ind="${i}" data-nb-field="lookback">
      </div>
      <div class="form-row">
        <label>Crack %</label>
        <input type="number" min="0" step="0.1" value="${crack}" data-nb-ind="${i}" data-nb-field="crack_pct">
      </div>
      <div class="form-row">
        <label>Base candles</label>
        <input type="number" min="1" value="${bc}" data-nb-ind="${i}" data-nb-field="base_candles">
      </div>
      <div class="form-row">
        <label>Max bases</label>
        <input type="number" min="1" value="${mb}" data-nb-ind="${i}" data-nb-field="max_bases">
      </div>
      <div class="form-row">
        <label>Below %</label>
        <input type="number" min="0" step="0.1" value="${bp}" data-nb-ind="${i}" data-nb-field="below_pct">
      </div>
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${QFL_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'SUPPORT_RESISTANCE') {
    const lb = ind.lookback != null ? ind.lookback : 3;
    const tol = ind.tolerance_pct != null ? ind.tolerance_pct : 0.5;
    const prox = ind.proximity_pct != null ? ind.proximity_pct : 1.0;
    const SR_CONDS = [
      ['near_support', 'Near support'],
      ['near_resistance', 'Near resistance'],
      ['below_support', 'Below support'],
      ['above_resistance', 'Above resistance'],
    ];
    return `
      <div class="form-row">
        <label>Lookback</label>
        <input type="number" min="1" value="${lb}" data-nb-ind="${i}" data-nb-field="lookback">
      </div>
      <div class="form-row">
        <label>Tolerance %</label>
        <input type="number" min="0" step="0.1" value="${tol}" data-nb-ind="${i}" data-nb-field="tolerance_pct">
      </div>
      <div class="form-row">
        <label>Proximity %</label>
        <input type="number" min="0" step="0.1" value="${prox}" data-nb-ind="${i}" data-nb-field="proximity_pct">
      </div>
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${SR_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'MARKET_STRUCTURE') {
    const lb = ind.lookback != null ? ind.lookback : 3;
    const MS_CONDS = [
      ['bullish_bos', 'Bullish BOS'],
      ['bearish_bos', 'Bearish BOS'],
      ['higher_low', 'Higher Low'],
      ['lower_high', 'Lower High'],
      ['bullish_structure', 'Bullish structure'],
      ['bearish_structure', 'Bearish structure'],
    ];
    return `
      <div class="form-row">
        <label>Lookback</label>
        <input type="number" min="1" value="${lb}" data-nb-ind="${i}" data-nb-field="lookback">
      </div>
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${MS_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'SUPERTREND') {
    const ap = ind.atr_period != null ? ind.atr_period : 10;
    const mult = ind.multiplier != null ? ind.multiplier : 3.0;
    const ST_CONDS = [
      ['bullish', 'Bullish'],
      ['bearish', 'Bearish'],
      ['bullish_flip', 'Bullish flip'],
      ['bearish_flip', 'Bearish flip'],
    ];
    return `
      <div class="form-row">
        <label>ATR Period</label>
        <input type="number" min="2" value="${ap}" data-nb-ind="${i}" data-nb-field="atr_period">
      </div>
      <div class="form-row">
        <label>Multiplier</label>
        <input type="number" min="0.1" step="0.1" value="${mult}" data-nb-ind="${i}" data-nb-field="multiplier">
      </div>
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${ST_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'PARABOLIC_SAR') {
    const iaf = ind.initial_af != null ? ind.initial_af : 0.02;
    const maf = ind.max_af     != null ? ind.max_af     : 0.20;
    const PSAR_CONDS = [
      ['bullish', 'Bullish'],
      ['bearish', 'Bearish'],
      ['bullish_flip', 'Bullish flip'],
      ['bearish_flip', 'Bearish flip'],
    ];
    return `
      <div class="form-row">
        <label>Initial AF</label>
        <input type="number" min="0.001" step="0.01" value="${iaf}" data-nb-ind="${i}" data-nb-field="initial_af">
      </div>
      <div class="form-row">
        <label>Max AF</label>
        <input type="number" min="0.01" step="0.01" value="${maf}" data-nb-ind="${i}" data-nb-field="max_af">
      </div>
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${PSAR_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'BOLLINGER') {
    const mult = ind.multiplier != null ? ind.multiplier : 2.0;
    const BB_CONDS = [
      ['price_below_lower', 'Price below lower band'],
      ['price_above_upper', 'Price above upper band'],
      ['price_below_middle', 'Price below middle'],
      ['price_above_middle', 'Price above middle'],
      ['squeeze', 'Squeeze'],
    ];
    return `
      <div class="form-row">
        <label>Period</label>
        <input type="number" min="5" value="${ind.period || 20}" data-nb-ind="${i}" data-nb-field="period">
      </div>
      <div class="form-row">
        <label>Multiplier</label>
        <input type="number" min="0.1" step="0.1" value="${mult}" data-nb-ind="${i}" data-nb-field="multiplier">
      </div>
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${BB_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'MACD') {
    const mf = ind.macd_fast   != null ? ind.macd_fast   : 12;
    const ms = ind.macd_slow   != null ? ind.macd_slow   : 26;
    const mg = ind.macd_signal != null ? ind.macd_signal : 9;
    return `
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${['histogram_positive', 'histogram_negative', 'bullish_cross', 'bearish_cross'].map(c =>
            `<option value="${c}" ${ind.condition === c ? 'selected' : ''}>${c}</option>`
          ).join('')}
        </select>
      </div>
      <div class="form-row">
        <label>Fast</label>
        <input type="number" min="2" value="${mf}" data-nb-ind="${i}" data-nb-field="macd_fast">
      </div>
      <div class="form-row">
        <label>Slow</label>
        <input type="number" min="2" value="${ms}" data-nb-ind="${i}" data-nb-field="macd_slow">
      </div>
      <div class="form-row">
        <label>Signal</label>
        <input type="number" min="2" value="${mg}" data-nb-ind="${i}" data-nb-field="macd_signal">
      </div>`;
  }
  return '';
}

function _parseRsiThreshold(threshold) {
  // Grammar: below_N, above_N, cross_above_N, cross_below_N.
  // Try the longer prefixes first so "cross_above_30" doesn't get
  // matched by the shorter "above" alternative.
  const m = /^(cross_above|cross_below|above|below)_(\d+)$/.exec(threshold || '');
  if (m) return { condition: m[1], value: parseInt(m[2], 10) };
  return { condition: 'below', value: 35 };
}

function nbUpdateLeverageUI() {
  const enabled = nbState.leverage_enabled;
  const lev = nbState.leverage_size;
  $('nb-leverage-size').disabled = !enabled;
  $('nb-leverage-value').textContent = lev + 'x';
  $('nb-liq-preview').textContent = enabled ? nbCalcLiqPreview() : '—';

  const warnEl = $('nb-leverage-warn');
  if (!enabled || lev <= 1) {
    warnEl.textContent = '';
    warnEl.className = 'form-hint';
  } else if (lev >= 50) {
    warnEl.textContent = '🔴 Extreme leverage — only for experienced traders';
    warnEl.className = 'form-hint warn-red';
  } else if (lev >= 10) {
    warnEl.textContent = '⚠️ High leverage — liquidation risk increases significantly';
    warnEl.className = 'form-hint warn-amber';
  } else {
    warnEl.textContent = '';
    warnEl.className = 'form-hint';
  }
}

function nbCalcLiqPreview() {
  // Rough approximation: liq ≈ entry × (1 ∓ 0.95/leverage).
  // Uses header price from /api/price as a reference; falls back to 80k.
  let price = parseFloat(($('hdr-price').textContent || '').replace(/[$,]/g, ''));
  if (!price || isNaN(price)) price = 80000;
  const lev = nbState.leverage_size;
  const liq = nbState.direction === 'long'
    ? price * (1 - 0.95 / lev)
    : price * (1 + 0.95 / lev);
  return '≈ $' + liq.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function nbRenderDcaPreview() {
  const tbody = $('nb-dca-preview-tbody');
  if (!tbody) return;
  let price = parseFloat(($('hdr-price').textContent || '').replace(/[$,]/g, ''));
  if (!price || isNaN(price)) price = 80000;

  // Walk the order ladder. For each row we track cumulative notional
  // (size * price) so we can compute the volume-weighted average entry
  // after that order, then derive the TP price and the required bounce
  // from THIS ROW's fill price (not the current market) back up to TP.
  const tpPct = nbState.tp_target_pct || 0;
  const rows = [];

  let curPrice = price;                              // row fill price
  let totalSize = nbState.base_size;
  let totalNotional = nbState.base_size * curPrice;
  let avgEntry = curPrice;
  let tpPrice = avgEntry * (1 + tpPct / 100);
  // Base row is always filled at the current price, so by construction
  // required_change = tp_pct. We keep it in the same formula so the
  // logic is uniform across all rows.
  let gainPct = ((tpPrice - curPrice) / curPrice * 100);

  rows.push({
    label: 'Base',
    size: nbState.base_size,
    price: curPrice,
    total: totalSize,
    dropPct: null,
    tpPrice,
    gainPct,
  });

  for (let i = 1; i <= nbState.dca_max_orders; i++) {
    const spacing = nbState.dca_spacing_pct * Math.pow(nbState.dca_step_scale, i - 1);
    curPrice = curPrice * (1 - spacing / 100);
    const size = nbState.dca_size * Math.pow(nbState.dca_volume_scale, i - 1);

    totalSize     += size;
    totalNotional += size * curPrice;
    avgEntry      = totalNotional / totalSize;
    tpPrice       = avgEntry * (1 + tpPct / 100);
    // Bounce required from this DCA fill price up to the new TP.
    // Always positive: on a falling ladder the fill is the lowest
    // print seen so far, avg >= fill, and tp = avg * (1 + tp_pct/100)
    // > avg >= fill, so tp > curPrice by construction.
    gainPct       = ((tpPrice - curPrice) / curPrice * 100);

    const dropPct = ((price - curPrice) / price * 100).toFixed(2);
    rows.push({
      label: `DCA ${i}`,
      size,
      price: curPrice,
      total: totalSize,
      dropPct,
      tpPrice,
      gainPct,
    });
  }

  const unit = nbState.base_unit === 'btc' ? 'BTC' : '%';
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${r.label}</td>
      <td>${r.size.toFixed(4)} ${unit}</td>
      <td>${fmtPrice(r.price)}${r.dropPct != null ? ` <span class="muted-cell">(-${r.dropPct}%)</span>` : ''}</td>
      <td>${r.total.toFixed(4)} ${unit}</td>
      <td>${fmtPrice(r.tpPrice)}</td>
      <td><span class="pos">+${r.gainPct.toFixed(2)}%</span></td>
    </tr>`).join('');
}

function nbCalcTotalSize() {
  let total = nbState.base_size;
  for (let i = 1; i <= nbState.dca_max_orders; i++) {
    total += nbState.dca_size * Math.pow(nbState.dca_volume_scale, i - 1);
  }
  return total;
}

function nbRenderReview() {
  const totalSize = nbCalcTotalSize();
  let warnings = '';
  if (nbState.base_unit === 'btc' && totalSize > 0.1) {
    warnings += `<div class="wizard-warning">⚠️ Total committed ${totalSize.toFixed(4)} BTC exceeds 0.1 BTC limit</div>`;
  }
  if (nbState.base_unit === 'pct' && totalSize > 100) {
    warnings += `<div class="wizard-warning">⚠️ Total committed ${totalSize.toFixed(0)}% exceeds 100%</div>`;
  }

  const indSummary = nbState.indicators.length
    ? nbState.indicators.map(i => `${i.type} (${i.timeframe})`).join(', ')
    : 'none — always enter';
  const unit = nbState.base_unit === 'btc' ? 'BTC' : '%';

  $('nb-review').innerHTML = `
    ${warnings}
    <div class="review-section">
      <div class="review-section-title">General</div>
      <div class="review-row"><span class="review-key">Name</span><span>${safeText(nbState.name) || '—'}</span></div>
      <div class="review-row"><span class="review-key">Exchange</span><span>${safeText(nbState.exchange.toUpperCase())}</span></div>
      <div class="review-row"><span class="review-key">Pair</span><span>${safeText(nbState.pair)}</span></div>
      <div class="review-row"><span class="review-key">Mode</span><span>${safeText(nbState.mode.toUpperCase())}</span></div>
      <div class="review-row"><span class="review-key">Direction</span><span>${safeText(nbState.direction.toUpperCase())}</span></div>
      <div class="review-row"><span class="review-key">Timeframe</span><span>${safeText(nbState.timeframe)}</span></div>
      <div class="review-row"><span class="review-key">Leverage</span><span>${nbState.leverage_enabled ? nbState.leverage_size + 'x' : 'off'}</span></div>
    </div>
    <div class="review-section">
      <div class="review-section-title">Entry</div>
      <div class="review-row"><span class="review-key">Base order</span><span>${nbState.base_size} ${unit}</span></div>
      <div class="review-row"><span class="review-key">Indicators</span><span>${safeText(indSummary)}</span></div>
    </div>
    <div class="review-section">
      <div class="review-section-title">TP / SL</div>
      <div class="review-row"><span class="review-key">Take Profit</span><span>${nbState.tp_target_pct}%</span></div>
      <div class="review-row"><span class="review-key">TP confirmation</span><span>${safeText(nbState.tp_indicator_confirm) || 'none'}</span></div>
      <div class="review-row"><span class="review-key">Max age</span><span>${nbState.tp_max_age_enabled ? nbState.tp_max_age_hours + 'h' : 'none'}</span></div>
      <div class="review-row"><span class="review-key">Stop Loss</span><span>${safeText(nbState.sl_type)} ${nbState.sl_pct}%</span></div>
    </div>
    <div class="review-section">
      <div class="review-section-title">DCA</div>
      <div class="review-row"><span class="review-key">Max DCA orders</span><span>${nbState.dca_max_orders}</span></div>
      <div class="review-row"><span class="review-key">DCA size</span><span>${nbState.dca_size} ${unit}</span></div>
      <div class="review-row"><span class="review-key">Spacing</span><span>${nbState.dca_spacing_pct}%</span></div>
      <div class="review-row"><span class="review-key">Volume scale</span><span>${nbState.dca_volume_scale}</span></div>
      <div class="review-row"><span class="review-key">Step scale</span><span>${nbState.dca_step_scale}</span></div>
      <div class="review-row"><span class="review-key">Total position</span><span>${totalSize.toFixed(4)} ${unit}</span></div>
    </div>
  `;
}

function nbBuildBotConfig() {
  // Build a BotConfig-compatible payload. Pydantic ignores unknown fields
  // (extra='ignore'), so timeframe/direction/etc. are dropped server-side
  // but kept in the wizard form for cosmetic purposes.
  const cfg = {
    name: nbState.name,
    mode: nbState.mode,
    exchange: nbState.exchange,
    pair: nbState.pair,
    contract_type: 'inverse_perpetual',
    leverage: {
      enabled: nbState.leverage_enabled,
      size: nbState.leverage_enabled ? nbState.leverage_size : 1,
    },
    dca: {
      base_order_size: nbState.base_size,
      // Wizard stores DCA-only count; YAML expects base+DCA total.
      max_orders: nbState.dca_max_orders + 1,
      order_spacing_pct: nbState.dca_spacing_pct,
      multiplier: nbState.dca_volume_scale,
    },
    entry: {
      indicators: nbState.indicators.map(i => {
        const out = { type: i.type };
        if (i.type === 'RSI') {
          out.period = i.period;
          out.threshold = i.threshold;
        } else if (i.type === 'EMA_CROSS') {
          out.fast = i.fast;
          out.slow = i.slow;
          out.signal = i.signal;
        } else if (i.type === 'MACD') {
          out.condition = i.condition;
          if (i.macd_fast   != null) out.macd_fast   = i.macd_fast;
          if (i.macd_slow   != null) out.macd_slow   = i.macd_slow;
          if (i.macd_signal != null) out.macd_signal = i.macd_signal;
        } else if (i.type === 'BOLLINGER') {
          out.period = i.period || 20;
          out.multiplier = i.multiplier != null ? i.multiplier : 2.0;
          out.condition = i.condition || 'price_below_lower';
        } else if (i.type === 'PARABOLIC_SAR') {
          out.initial_af = i.initial_af != null ? i.initial_af : 0.02;
          out.max_af = i.max_af != null ? i.max_af : 0.20;
          out.condition = i.condition || 'bullish';
        } else if (i.type === 'SUPERTREND') {
          out.atr_period = i.atr_period != null ? i.atr_period : 10;
          out.multiplier = i.multiplier != null ? i.multiplier : 3.0;
          out.condition = i.condition || 'bullish';
        } else if (i.type === 'MARKET_STRUCTURE') {
          out.lookback = i.lookback != null ? i.lookback : 3;
          out.condition = i.condition || 'bullish_bos';
        } else if (i.type === 'SUPPORT_RESISTANCE') {
          out.lookback = i.lookback != null ? i.lookback : 3;
          out.tolerance_pct = i.tolerance_pct != null ? i.tolerance_pct : 0.5;
          out.proximity_pct = i.proximity_pct != null ? i.proximity_pct : 1.0;
          out.condition = i.condition || 'near_support';
        } else if (i.type === 'QFL') {
          out.lookback = i.lookback != null ? i.lookback : 3;
          out.crack_pct = i.crack_pct != null ? i.crack_pct : 3.0;
          out.base_candles = i.base_candles != null ? i.base_candles : 5;
          out.max_bases = i.max_bases != null ? i.max_bases : 5;
          out.below_pct = i.below_pct != null ? i.below_pct : 0.0;
          out.condition = i.condition || 'below_base';
        }
        return out;
      }),
    },
    take_profit: { target_pct: nbState.tp_target_pct },
    stop_loss: { type: nbState.sl_type, pct: nbState.sl_pct },
  };
  if (nbState.tp_indicator_confirm) {
    cfg.take_profit.indicator_confirm = nbState.tp_indicator_confirm;
  }
  if (nbState.tp_min_pct != null && nbState.tp_min_pct > 0) {
    cfg.take_profit.minimum_tp_pct = nbState.tp_min_pct;
  }
  cfg.schedule = {
    timezone: nbState.schedule_timezone || 'Europe/Amsterdam',
    trading_windows: (nbState.schedule_windows || []).map(w => ({
      days: (w.days || []).slice(),
      from: w.from || '00:00',
      to:   w.to   || '00:00',
    })),
    blackout_dates: (nbState.schedule_blackouts || []).slice(),
  };
  return { bot: cfg };
}

async function nbSubmit() {
  nbReadAll();
  const errors = nbValidateAll();
  if (errors.length) {
    nbShowError(errors.map(e => safeText(e)).join('<br>'));
    return;
  }
  if (!getApiKey()) {
    _pendingAction = () => nbSubmit();
    showApiKeyModal();
    return;
  }
  const body = nbBuildBotConfig();
  const btn = $('nb-submit-btn');
  const wasEdit = nbEditSlug;
  const origLabel = wasEdit ? 'Save changes' : 'Save bot';
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    const url = wasEdit ? `/api/bots/${wasEdit}/config` : '/api/bots';
    const method = wasEdit ? 'PUT' : 'POST';
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-API-Key': getApiKey() },
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      _pendingAction = () => nbSubmit();
      nbShowError('Auth error — check your API key');
      showApiKeyModal();
      return;
    }
    const r = await res.json();
    if (!res.ok) {
      nbShowError(safeText(r.detail || `Save failed (${res.status})`));
      return;
    }
    nbInit();
    goBots();
  } catch (e) {
    nbShowError('Network error: ' + safeText(e.message));
  } finally {
    btn.disabled = false;
    btn.textContent = origLabel;
  }
}

// ── Bot detail ────────────────────────────────────────────────────────────────

function openBot(slug) {
  clearInterval(overviewInterval);
  overviewInterval = null;
  currentSlug = slug;
  _detailConfigCache = null;

  // Detail is a sub-view of Bots — keep the Bots tab active and surface
  // the slug in the header subtext as "Multi-Bot Portal › SLUG".
  _setActiveTab('nav-bots-btn');
  $('hdr-pill').classList.remove('hidden');
  $('hdr-context').innerHTML =
    'Multi-Bot Portal <span class="hdr-sep">›</span> ' +
    '<span class="hdr-slug">' + safeText(slug.toUpperCase()) + '</span>';

  showDTab('dashboard', document.querySelector('.detail-subnav .tab'));

  showPage('detail');
  connectWS(slug);
  fetchDetail(slug);
  detailInterval = setInterval(() => fetchDetail(slug), 5000);
}

async function fetchDetail(slug) {
  try {
    const b = await fetch(`/api/bots/${slug}`).then(r => r.json());

    if (b.current_price) $('hdr-price').textContent = fmtPrice(b.current_price);
    $('hdr-pair').textContent = b.pair || 'BTC/USD';
    $('hdr-uptime').textContent = b.uptime ? '⏱ ' + b.uptime : '';

    const pill = $('status-pill');
    $('status-text').textContent = b.running ? 'Running' : 'Stopped';
    pill.className = 'pill ' + (b.running ? 'running' : 'stopped');

    const rs = $('d-running-status');
    if (rs) {
      rs.textContent = b.running ? 'RUNNING' : 'STOPPED';
      rs.className = 'running-status ' + (b.running ? 'running' : 'stopped');
    }
    const sb = $('d-btn-start');   if (sb) sb.disabled = !!b.running;
    const xb = $('d-btn-stop');    if (xb) xb.disabled = !b.running;
    const rb = $('d-btn-restart'); if (rb) rb.disabled = !b.running;

    $('d-price').textContent = fmtPrice(b.current_price) || '—';
    $('d-pair-sub').textContent = b.pair || 'BTC/USD';
    $('d-balance').textContent = b.balance_btc ? b.balance_btc.toFixed(6) : '—';

    const pnl = b.total_pnl_btc || 0;
    $('d-pnl').innerHTML = fmtPnl(pnl, 8);
    $('d-open-count').textContent = b.open_deals_count ?? '—';
    $('d-winrate').textContent = (b.win_rate ?? 0) + '%';
    $('d-schedule').textContent = b.schedule_open ? 'Open' : 'Closed';
    $('d-schedule').className = 'card-value ' + (b.schedule_open ? 'pos' : 'neu');

    $('d-config').textContent = b.config_file || '—';
    $('d-mode').textContent = (b.mode || '—').toUpperCase();
    $('d-exchange').textContent = (b.exchange || '—').toUpperCase();
    $('d-init').textContent = b.initial_balance_btc ? b.initial_balance_btc.toFixed(4) + ' BTC' : '—';

    const ind = b.indicators || {};
    const ig = $('indicator-grid');
    const indKeys = Object.entries(ind);
    if (!indKeys.length) {
      ig.innerHTML = '<div class="empty-grid">No indicator data yet — waiting for first candle fetch</div>';
    } else {
      ig.innerHTML = indKeys.map(([k, v]) => `
        <div class="indicator-card">
          <div class="indicator-label">${safeText(k.replace(/_/g, ' '))}</div>
          <div class="indicator-value">${typeof v === 'number' ? v.toFixed(4) : safeText(v)}</div>
        </div>`).join('');
    }

    const ob = $('d-open-tbody');
    const od = b.open_deals || [];
    ob.innerHTML = od.length
      ? od.map(d => `<tr>
          <td class="deal-id-cell">${safeText(d.id)}</td>
          <td>${fmtPrice(d.entry_price)}</td>
          <td>${fmtPrice(d.avg_entry_price)}</td>
          <td>${d.order_count}</td>
          <td>${d.total_size?.toFixed(4) || '—'}</td>
          <td>${fmtPnl(d.pnl_btc)}</td>
          <td class="muted-cell">${timeAgo(d.opened_at)}</td>
        </tr>`).join('')
      : '<tr class="empty-row"><td colspan="7">No open deals</td></tr>';

    const cb = $('d-closed-tbody');
    const cd = b.closed_deals || [];
    cb.innerHTML = cd.length
      ? cd.map(d => `<tr>
          <td class="muted-cell">${safeText(d.id)}</td>
          <td>${reasonBadge(d.close_reason)}</td>
          <td>${fmtPrice(d.entry_price)}</td>
          <td>${fmtPrice(d.close_price)}</td>
          <td>${fmtPnl(d.pnl_btc)}</td>
          <td>${fmtPct(d.pnl_pct)}</td>
          <td class="muted-cell">${timeAgo(d.closed_at)}</td>
        </tr>`).join('')
      : '<tr class="empty-row"><td colspan="7">No closed deals</td></tr>';

    renderPerformanceStats(cd);

    $('log-title').textContent = slug + '.log';

  } catch (e) {}
}

// ── Performance stats ────────────────────────────────────────────────────────
function renderPerformanceStats(closedDeals) {
  const grid = $('perf-stats-grid');
  if (!grid) return;
  const cells = [
    ['Profit Factor', 'profit_factor'],
    ['Sharpe Ratio',  'sharpe'],
    ['Sortino Ratio', 'sortino'],
    ['Consistency',   'consistency'],
    ['Max Drawdown',  'max_dd'],
    ['Total Deals',   'total'],
  ];
  const list = Array.isArray(closedDeals) ? closedDeals : [];
  const n = list.length;
  const returns = list.map(d => Number(d.pnl_pct) || 0);
  const wins = returns.filter(r => r > 0);
  const losses = returns.filter(r => r < 0);
  const sum = arr => arr.reduce((a, b) => a + b, 0);
  const mean = arr => arr.length ? sum(arr) / arr.length : 0;
  const std = arr => {
    if (arr.length < 2) return 0;
    const m = mean(arr);
    return Math.sqrt(sum(arr.map(x => (x - m) ** 2)) / arr.length);
  };

  // Per-stat minimum sample size. Each metric only becomes meaningful
  // once we have enough deals — otherwise the cell shows the threshold
  // so the user knows exactly how many more deals are needed.
  let profit_factor;
  if (n < 2) {
    profit_factor = 'Need 2+ deals';
  } else {
    const sumWins = sum(wins);
    const sumLosses = Math.abs(sum(losses));
    profit_factor = losses.length === 0 || sumLosses === 0
      ? '∞'
      : (sumWins / sumLosses).toFixed(2);
  }

  let sharpe;
  if (n < 10) {
    sharpe = 'Need 10+ deals';
  } else {
    const stdAll = std(returns);
    sharpe = stdAll === 0
      ? '—'
      : ((mean(returns) / stdAll) * Math.sqrt(252)).toFixed(2);
  }

  let sortino;
  if (n < 10) {
    sortino = 'Need 10+ deals';
  } else {
    const stdLosses = std(losses);
    sortino = losses.length === 0 || stdLosses === 0
      ? '∞'
      : ((mean(returns) / stdLosses) * Math.sqrt(252)).toFixed(2);
  }

  const consistency = n < 1
    ? 'Need 1+ deals'
    : (wins.length / n * 100).toFixed(1) + '%';

  let max_dd;
  if (n < 1) {
    max_dd = 'Need 1+ deals';
  } else {
    let cum = 0, peak = 0, maxDd = 0;
    returns.forEach(r => {
      cum += r;
      if (cum > peak) peak = cum;
      const dd = peak - cum;
      if (dd > maxDd) maxDd = dd;
    });
    max_dd = '-' + maxDd.toFixed(2) + '%';
  }

  const values = {
    profit_factor,
    sharpe,
    sortino,
    consistency,
    max_dd,
    total: String(n),
  };
  grid.innerHTML = cells.map(([label, key]) => `
    <div class="card">
      <div class="card-label">${safeText(label)}</div>
      <div class="card-value">${safeText(values[key])}</div>
    </div>`).join('');
}

// ── Bot detail: Config tab ───────────────────────────────────────────────────
let _detailConfigCache = null;

async function fetchDetailConfig(slug) {
  const body = $('d-config-body');
  if (!body) return;
  try {
    const res = await fetch(`/api/bots/${slug}/config`);
    if (!res.ok) {
      body.innerHTML = '<div class="cfg-empty">Failed to load config</div>';
      _detailConfigCache = null;
      return;
    }
    const cfg = await res.json();
    _detailConfigCache = cfg;
    renderDetailConfig(cfg);
  } catch (e) {
    body.innerHTML = `<div class="cfg-empty">Network error: ${safeText(e.message)}</div>`;
    _detailConfigCache = null;
  }
}

function renderDetailConfig(cfg) {
  const b = (cfg && cfg.bot) || cfg || {};
  const lev = b.leverage || {};
  const dca = b.dca || {};
  const tp  = b.take_profit || {};
  const sl  = b.stop_loss || {};
  const indicators = (b.entry && b.entry.indicators) || [];
  const sched = b.schedule || null;

  const leverageStr = lev.enabled ? `${lev.size || 1}x` : 'off';
  const indHtml = indicators.length
    ? indicators.map(i => {
        const rows = [];
        rows.push(`<div class="cfg-row"><span class="cfg-key">Type</span><span>${safeText(i.type || '—')}</span></div>`);
        if (i.timeframe) rows.push(`<div class="cfg-row"><span class="cfg-key">Timeframe</span><span>${safeText(i.timeframe)}</span></div>`);
        if (i.type === 'RSI') {
          if (i.period != null) rows.push(`<div class="cfg-row"><span class="cfg-key">Period</span><span>${i.period}</span></div>`);
          if (i.threshold) rows.push(`<div class="cfg-row"><span class="cfg-key">Threshold</span><span>${safeText(i.threshold)}</span></div>`);
        } else if (i.type === 'EMA_CROSS') {
          if (i.fast != null) rows.push(`<div class="cfg-row"><span class="cfg-key">Fast</span><span>${i.fast}</span></div>`);
          if (i.slow != null) rows.push(`<div class="cfg-row"><span class="cfg-key">Slow</span><span>${i.slow}</span></div>`);
          if (i.signal) rows.push(`<div class="cfg-row"><span class="cfg-key">Signal</span><span>${safeText(i.signal)}</span></div>`);
        } else if (i.type === 'MACD') {
          if (i.condition) rows.push(`<div class="cfg-row"><span class="cfg-key">Condition</span><span>${safeText(i.condition)}</span></div>`);
        }
        return `<div class="cfg-indicator">
          <div class="cfg-indicator-head">${safeText(i.type || 'Indicator')}</div>
          ${rows.join('')}
        </div>`;
      }).join('')
    : '<div class="cfg-empty">No indicators — always enter</div>';

  $('d-config-body').innerHTML = `
    <div class="cfg-section">
      <div class="cfg-section-title">General</div>
      <div class="cfg-row"><span class="cfg-key">Name</span><span>${safeText(b.name || '—')}</span></div>
      <div class="cfg-row"><span class="cfg-key">Exchange</span><span>${safeText((b.exchange || '—').toUpperCase())}</span></div>
      <div class="cfg-row"><span class="cfg-key">Pair</span><span>${safeText(b.pair || '—')}</span></div>
      <div class="cfg-row"><span class="cfg-key">Mode</span><span>${safeText((b.mode || '—').toUpperCase())}</span></div>
      <div class="cfg-row"><span class="cfg-key">Leverage</span><span>${safeText(leverageStr)}</span></div>
    </div>

    <div class="cfg-section">
      <div class="cfg-section-title">Entry Conditions</div>
      <div class="cfg-row"><span class="cfg-key">Base order size</span><span>${dca.base_order_size != null ? dca.base_order_size + ' BTC' : '—'}</span></div>
      <div class="cfg-subtitle">Indicators</div>
      ${indHtml}
    </div>

    <div class="cfg-section">
      <div class="cfg-section-title">Take Profit &amp; Stop Loss</div>
      <div class="cfg-row"><span class="cfg-key">TP target</span><span>${tp.target_pct != null ? tp.target_pct + '%' : '—'}</span></div>
      <div class="cfg-row"><span class="cfg-key">TP confirmation</span><span>${safeText(tp.indicator_confirm || 'none')}</span></div>
      <div class="cfg-row"><span class="cfg-key">SL type</span><span>${safeText(sl.type || '—')}</span></div>
      <div class="cfg-row"><span class="cfg-key">SL percentage</span><span>${sl.pct != null ? sl.pct + '%' : '—'}</span></div>
    </div>

    <div class="cfg-section">
      <div class="cfg-section-title">DCA Settings</div>
      <div class="cfg-row"><span class="cfg-key">Max DCA orders</span><span>${dca.max_orders != null ? Math.max(0, dca.max_orders - 1) + ' DCA orders' : '—'}</span></div>
      <div class="cfg-row"><span class="cfg-key">Order spacing</span><span>${dca.order_spacing_pct != null ? dca.order_spacing_pct + '%' : '—'}</span></div>
      <div class="cfg-row"><span class="cfg-key">Multiplier</span><span>${dca.multiplier != null ? dca.multiplier : '—'}</span></div>
      <div class="cfg-row"><span class="cfg-key">Taker fee</span><span>${dca.taker_fee != null ? (dca.taker_fee * 100).toFixed(3) + '%' : '—'}</span></div>
    </div>

    ${renderScheduleSection(sched)}
  `;
}

function renderScheduleSection(sched) {
  if (!sched || (!sched.timezone && !(sched.trading_windows || []).length && !(sched.blackout_dates || []).length)) {
    return `
      <div class="cfg-section">
        <div class="cfg-section-title">Schedule</div>
        <div class="cfg-empty">No schedule configured — bot trades 24/7</div>
      </div>`;
  }
  const dayNames = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' };
  const windows = (sched.trading_windows || []).map(w => {
    const days = (w.days || []).map(d => dayNames[d] || d).join(',');
    const from = w.from || w.from_time || '?';
    const to   = w.to   || w.to_time   || '?';
    return `<div class="cfg-row"><span class="cfg-key">Window</span><span>${safeText(days)} ${safeText(from)}-${safeText(to)}</span></div>`;
  }).join('') || '<div class="cfg-row"><span class="cfg-key">Windows</span><span>none (24/7)</span></div>';
  const blackouts = (sched.blackout_dates || []).length
    ? '<ul class="cfg-blackouts">' +
      (sched.blackout_dates || []).map(d => `<li>${safeText(d)}</li>`).join('') +
      '</ul>'
    : '<span>none</span>';
  return `
    <div class="cfg-section">
      <div class="cfg-section-title">Schedule</div>
      <div class="cfg-row"><span class="cfg-key">Timezone</span><span>${safeText(sched.timezone || '—')}</span></div>
      ${windows}
      <div class="cfg-row"><span class="cfg-key">Blackout dates</span>${blackouts}</div>
    </div>`;
}

// ── Edit flow: load an existing bot into the wizard ─────────────────────────
async function editBot(slug) {
  try {
    const res = await fetch(`/api/bots/${slug}/config`);
    if (!res.ok) { alert('Failed to load config'); return; }
    const cfg = await res.json();
    nbState = nbStateFromConfig(cfg);
    nbEditSlug = slug;
    _resetHeaderForTopLevel();
    _setActiveTab('nav-bots-btn');
    showPage('new-bot');
    nbApplyStateToForm();
    nbHideError();
    nbRecompute();
    // Update submit button label to reflect edit mode
    const btn = $('nb-submit-btn');
    if (btn) btn.textContent = 'Save changes';
  } catch (e) {
    alert('Network error: ' + e.message);
  }
}

function nbStateFromConfig(cfg) {
  const b = (cfg && cfg.bot) || cfg || {};
  const lev = b.leverage || {};
  const dca = b.dca || {};
  const tp  = b.take_profit || {};
  const sl  = b.stop_loss || {};
  const d = nbDefaultState();
  return {
    ...d,
    name:             b.name || '',
    exchange:         b.exchange || d.exchange,
    pair:             b.pair || d.pair,
    mode:             b.mode || d.mode,
    leverage_enabled: !!lev.enabled,
    leverage_size:    lev.size || d.leverage_size,
    base_size:        dca.base_order_size != null ? dca.base_order_size : d.base_size,
    indicators:       ((b.entry && b.entry.indicators) || []).map(i => ({
      type:      i.type || 'RSI',
      timeframe: i.timeframe || '1h',
      period:    i.period != null ? i.period : 14,
      threshold: i.threshold || 'below_35',
      fast:      i.fast != null ? i.fast : 9,
      slow:      i.slow != null ? i.slow : 21,
      signal:    i.signal || 'bullish_cross',
      condition: i.condition || 'histogram_positive',
    })),
    tp_target_pct:        tp.target_pct != null ? tp.target_pct : d.tp_target_pct,
    tp_indicator_confirm: tp.indicator_confirm || '',
    tp_min_pct:           tp.minimum_tp_pct != null ? tp.minimum_tp_pct : null,
    sl_type:              sl.type || d.sl_type,
    sl_pct:               sl.pct != null ? sl.pct : d.sl_pct,
    // YAML stores base+DCA; wizard input is DCA-only, so subtract 1.
    dca_max_orders:       dca.max_orders != null ? Math.max(0, dca.max_orders - 1) : d.dca_max_orders,
    // YAML only stores base_order_size; mirror it as the initial DCA size.
    dca_size:             dca.base_order_size != null ? dca.base_order_size : d.dca_size,
    dca_spacing_pct:      dca.order_spacing_pct != null ? dca.order_spacing_pct : d.dca_spacing_pct,
    dca_volume_scale:     dca.multiplier != null ? dca.multiplier : d.dca_volume_scale,
    schedule_timezone:    (b.schedule && b.schedule.timezone) || d.schedule_timezone,
    schedule_windows:     ((b.schedule && b.schedule.trading_windows) || []).map(w => ({
      days: (w.days || []).slice(),
      from: w.from || w.from_time || '09:00',
      to:   w.to   || w.to_time   || '17:00',
    })),
    schedule_blackouts:   ((b.schedule && b.schedule.blackout_dates) || []).slice(),
  };
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWS(slug) {
  if (ws) ws.close();
  $('log-body').innerHTML = '';
  const key = encodeURIComponent(getApiKey());
  ws = new WebSocket(`ws://${location.host}/ws/logs/${slug}?api_key=${key}`);
  const dot = $('ws-dot');
  const lbl = $('ws-label');
  ws.onopen = () => { dot.className = 'live-dot connected'; lbl.textContent = 'live'; lbl.classList.remove('label-err'); lbl.classList.add('label-ok'); };
  ws.onmessage = e => appendLog(e.data);
  ws.onclose = () => {
    dot.className = 'live-dot error';
    lbl.textContent = 'reconnecting';
    lbl.classList.remove('label-ok'); lbl.classList.add('label-err');
    if (currentSlug === slug) setTimeout(() => connectWS(slug), 3000);
  };
  ws.onerror = () => ws.close();
}

function appendLog(text) {
  if (text === '__ping__') return;
  const out = $('log-body');
  const el = document.createElement('div');
  el.className = 'log-line ' + logCls(text);
  el.textContent = text;
  out.appendChild(el);
  while (out.children.length > 500) out.removeChild(out.firstChild);
  const auto = $('autoscroll');
  if (auto && auto.checked) out.scrollTop = out.scrollHeight;
}
function clearLog() { $('log-body').innerHTML = ''; }

// ── Always-on price fetch ─────────────────────────────────────────────────────
async function fetchPrice() {
  try {
    const d = await fetch('/api/price').then(r => r.json());
    if (d.price) {
      $('hdr-price').textContent = fmtPrice(d.price);
      $('hdr-pair').textContent = d.pair || 'BTC/USD';
    }
  } catch (e) {}
}

// ── Overview log — all bots combined, INFO+ only ──────────────────────────────
let ovWsList = [];

function connectOverviewLogs(slugs) {
  ovWsList.forEach(w => w.close());
  ovWsList = [];

  const dot = $('ov-ws-dot');
  const lbl = $('ov-ws-label');
  let connected = 0;

  const key = encodeURIComponent(getApiKey());
  slugs.forEach(slug => {
    const w = new WebSocket(`ws://${location.host}/ws/logs/${slug}?api_key=${key}`);
    w.onopen = () => {
      connected++;
      dot.className = 'live-dot connected';
      lbl.textContent = 'live';
      lbl.classList.remove('label-err'); lbl.classList.add('label-ok');
    };
    w.onmessage = e => {
      const line = e.data;
      if (line === '__ping__') return;
      if (line.includes('[DEBUG]')) return;
      appendOverviewLog(line, slug);
    };
    w.onclose = () => {
      connected = Math.max(0, connected - 1);
      if (connected === 0) {
        dot.className = 'live-dot error';
        lbl.textContent = 'disconnected';
        lbl.classList.remove('label-ok'); lbl.classList.add('label-err');
      }
    };
    ovWsList.push(w);
  });

  if (!slugs.length) {
    dot.className = 'live-dot';
    lbl.textContent = 'no bots';
    lbl.classList.remove('label-ok', 'label-err');
  }
}

function appendOverviewLog(text, slug) {
  const out = $('ov-log-body');
  const el = document.createElement('div');
  el.className = 'log-line ' + logCls(text);
  el.textContent = text;
  out.appendChild(el);
  while (out.children.length > 300) out.removeChild(out.firstChild);
  out.scrollTop = out.scrollHeight;
}

// ── Portal restart ────────────────────────────────────────────────────────────
async function restartPortal() {
  const btn = $('restart-btn');
  btn.textContent = 'Restarting...';
  btn.disabled = true;

  try {
    await fetch('/api/portal/restart', {
      method: 'POST',
      headers: { 'X-API-Key': getApiKey() }
    });
  } catch (e) {}

  const poll = setInterval(async () => {
    try {
      const r = await fetch('/api/portal/status');
      if (r.ok) {
        clearInterval(poll);
        btn.textContent = 'Restart Dashboard';
        btn.disabled = false;
        location.reload();
      }
    } catch (e) {}
  }, 1000);
}

// ── Event wiring (vervangt alle inline onclick=) ─────────────────────────────
function setupEventListeners() {
  $('api-key-btn').addEventListener('click', showApiKeyModal);
  $('restart-btn').addEventListener('click', restartPortal);
  $('theme-btn').addEventListener('click', toggleTheme);

  $('nav-overview-btn').addEventListener('click', goOverview);
  $('nav-bots-btn').addEventListener('click', goBots);
  $('nav-deals-btn').addEventListener('click', goDeals);

  // Back button inside the bot detail sub-view
  $('detail-back-btn').addEventListener('click', goBots);

  $('new-bot-btn').addEventListener('click', goNewBot);

  document.querySelectorAll('.detail-subnav .tab').forEach(btn => {
    btn.addEventListener('click', () => showDTab(btn.dataset.dtab, btn));
  });

  $('modal-clear-btn').addEventListener('click', clearApiKey);
  $('modal-cancel-btn').addEventListener('click', closeApiKeyModal);
  $('modal-save-btn').addEventListener('click', saveApiKey);

  $('log-clear-btn').addEventListener('click', clearLog);
  $('ov-log-clear-btn').addEventListener('click', () => { $('ov-log-body').innerHTML = ''; });

  // ── Bot detail: Config tab actions ────────────────────────────────────────
  $('d-edit-btn').addEventListener('click', () => {
    if (currentSlug) editBot(currentSlug);
  });
  $('d-delete-btn').addEventListener('click', () => {
    if (!currentSlug) return;
    const name = (_detailConfigCache && _detailConfigCache.bot && _detailConfigCache.bot.name) || currentSlug;
    deleteBot(currentSlug, name);
  });

  // ── New bot form ─────────────────────────────────────────────────────────
  $('nb-submit-btn').addEventListener('click', nbSubmit);
  $('nb-add-indicator-btn').addEventListener('click', nbAddIndicator);

  const addWinBtn = $('nb-sched-add-window');
  if (addWinBtn) {
    addWinBtn.addEventListener('click', () => {
      if (!nbState) return;
      nbState.schedule_windows.push({
        days: ['mon', 'tue', 'wed', 'thu', 'fri'],
        from: '09:00',
        to:   '17:00',
      });
      nbRenderScheduleWindows();
      nbRecompute();
    });
  }

  // Bot detail control buttons
  ['start', 'stop', 'restart'].forEach(action => {
    const btn = $('d-btn-' + action);
    if (btn) {
      btn.addEventListener('click', () => {
        if (currentSlug) botAction(currentSlug, action, btn);
      });
    }
  });

  // Base unit toggle
  document.querySelectorAll('[data-base-unit]').forEach(b => {
    b.addEventListener('click', () => { nbToggleBaseUnit(b.dataset.baseUnit); nbRecompute(); });
  });

  // TP max-age toggle disables the hours input
  $('nb-tp-max-age-enabled').addEventListener('change', e => {
    $('nb-tp-max-age-hours').disabled = !e.target.checked;
    nbRecompute();
  });

  // Live recompute: any input/change inside the wizard refreshes state,
  // DCA preview and review section. Indicator row controls are handled
  // separately below because they need to re-render the row list on type
  // change before we recompute.
  const wizard = document.querySelector('#view-new-bot .wizard');
  if (wizard) {
    wizard.addEventListener('input', e => {
      if (e.target.dataset && e.target.dataset.nbInd != null) return;
      nbRecompute();
    });
    wizard.addEventListener('change', e => {
      if (e.target.dataset && e.target.dataset.nbInd != null) return;
      nbRecompute();
    });
  }

  // Indicator row event delegation (input changes, type switch, remove)
  document.addEventListener('input', e => {
    const t = e.target;
    if (t.dataset && t.dataset.nbInd != null && t.dataset.nbField) {
      const i = parseInt(t.dataset.nbInd, 10);
      const f = t.dataset.nbField;
      if (!nbState || !nbState.indicators[i]) return;
      let v = t.value;
      const intFields = [
        'period', 'fast', 'slow',
        'rsi_value', 'macd_fast', 'macd_slow', 'macd_signal',
        'atr_period', 'lookback',
        'base_candles', 'max_bases',
      ];
      const floatFields = [
        'multiplier', 'initial_af', 'max_af',
        'tolerance_pct', 'proximity_pct',
        'crack_pct', 'below_pct',
      ];
      if (intFields.includes(f)) v = parseInt(v, 10) || 0;
      else if (floatFields.includes(f)) v = parseFloat(v) || 0;
      nbState.indicators[i][f] = v;
      // RSI condition/value are derived back into the threshold field
      // so the serialised payload still matches the "below_35" /
      // "cross_above_30" schema the strategy engine expects.
      if (f === 'rsi_condition' || f === 'rsi_value') {
        const ind = nbState.indicators[i];
        const cond = ind.rsi_condition || 'below';
        const val = Math.min(99, Math.max(1, ind.rsi_value || 35));
        ind.threshold = `${cond}_${val}`;
      }
      if (f === 'type') nbRenderIndicators();
      nbRecompute();
    }
  });
  document.addEventListener('change', e => {
    const t = e.target;
    if (t.dataset && t.dataset.nbInd != null && t.dataset.nbField === 'type') {
      const i = parseInt(t.dataset.nbInd, 10);
      if (nbState && nbState.indicators[i]) {
        nbState.indicators[i].type = t.value;
        nbRenderIndicators();
        nbRecompute();
      }
    }
  });
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-nb-remove]');
    if (t) { nbRemoveIndicator(parseInt(t.dataset.nbRemove, 10)); nbRecompute(); }
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  setupEventListeners();

  if (!getApiKey()) showApiKeyModal();

  fetchOverview();
  fetchPrice();
  overviewInterval = setInterval(fetchOverview, 5000);
  setInterval(fetchPrice, 15000);

  setTimeout(async () => {
    try {
      const d = await fetch('/api/bots').then(r => r.json());
      const slugs = (d.bots || []).map(b => b.slug);
      slugs.push('portal');
      connectOverviewLogs(slugs);
    } catch (e) {}
  }, 1000);
});
