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
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No open deals across any bot</td></tr>';
  } else {
    tbody.innerHTML = deals.map(deal => `<tr>
      <td><span class="link-like" data-action="open" data-slug="${safeText(deal.bot_slug)}">${safeText(deal.bot_name)}</span></td>
      <td class="muted-cell">${safeText(deal.id)}</td>
      <td>${safeText(deal.symbol || '—')}</td>
      <td>${fmtPrice(deal.entry_price)}</td>
      <td>${fmtPrice(deal.avg_entry_price)}</td>
      <td>${deal.order_count}</td>
      <td>${fmtPnl(deal.pnl_btc)}</td>
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
  else if (['start', 'stop', 'restart'].includes(action)) botAction(slug, action);
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

async function botAction(slug, action) {
  _debounceBotButtons(slug, action);
  // No key yet → queue the action and prompt for one. Once the user
  // enters a valid key in the modal, saveApiKey() re-invokes this.
  if (!getApiKey()) {
    _pendingAction = () => botAction(slug, action);
    showApiKeyModal();
    return;
  }
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
  setTimeout(fetchOverview, 1200);
  if (currentSlug === slug) setTimeout(() => fetchDetail(slug), 1500);
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
  $('detail-nav-item').classList.add('hidden');
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
let nbState = null;
// When set, nbSubmit() PUTs to /api/bots/{slug}/config instead of POSTing a
// new bot. editBot() sets this, nbInit() clears it. Cleared again on success.
let nbEditSlug = null;

function nbDefaultState() {
  return {
    name: '', exchange: 'bitget', pair: 'BTC/USD', mode: 'paper', direction: 'long',
    leverage_enabled: false, leverage_size: 2, timeframe: '1h',
    base_unit: 'btc', base_size: 0.001,
    indicators: [],
    tp_target_pct: 3.0, tp_indicator_confirm: '',
    tp_max_age_enabled: false, tp_max_age_hours: 24,
    sl_type: 'fixed', sl_pct: 5.0,
    dca_max_orders: 5, dca_size: 0.001, dca_spacing_pct: 2.5,
    dca_volume_scale: 1.0, dca_step_scale: 1.0,
  };
}

function nbInit() {
  nbState = nbDefaultState();
  nbEditSlug = null;
  const btn = $('nb-submit-btn');
  if (btn) btn.textContent = 'Save bot';
  nbApplyStateToForm();
  nbHideError();
  nbRecompute();
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
  nbState.tp_max_age_enabled = $('nb-tp-max-age-enabled').checked;
  nbState.tp_max_age_hours = parseInt($('nb-tp-max-age-hours').value, 10);
  nbState.sl_type = $('nb-sl-type').value;
  nbState.sl_pct = parseFloat($('nb-sl-pct').value);

  nbState.dca_max_orders = parseInt($('nb-dca-max').value, 10);
  nbState.dca_size = parseFloat($('nb-dca-size').value);
  nbState.dca_spacing_pct = parseFloat($('nb-dca-spacing').value);
  nbState.dca_volume_scale = parseFloat($('nb-dca-volume').value);
  nbState.dca_step_scale = parseFloat($('nb-dca-step').value);
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

  if (!nbState.dca_max_orders || nbState.dca_max_orders < 1 || nbState.dca_max_orders > 10)
    errors.push('DCA: max orders must be between 1 and 10');
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
  nbRenderDcaPreview();
  nbRenderReview();
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

  nbRenderIndicators();
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
    fast: 9, slow: 21, signal: 'bullish_cross',
    condition: 'histogram_positive',
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
  list.innerHTML = nbState.indicators.map((ind, i) => `
    <div class="indicator-row">
      <div class="form-row">
        <label>Type</label>
        <select data-nb-ind="${i}" data-nb-field="type">
          <option value="RSI" ${ind.type === 'RSI' ? 'selected' : ''}>RSI</option>
          <option value="EMA_CROSS" ${ind.type === 'EMA_CROSS' ? 'selected' : ''}>EMA Cross</option>
          <option value="MACD" ${ind.type === 'MACD' ? 'selected' : ''}>MACD</option>
        </select>
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
      <button type="button" class="btn-icon-danger" data-nb-remove="${i}">✕</button>
    </div>
  `).join('');
}

function nbIndicatorFieldsHtml(ind, i) {
  if (ind.type === 'RSI') {
    return `
      <div class="form-row">
        <label>Period</label>
        <input type="number" min="2" max="100" value="${ind.period}" data-nb-ind="${i}" data-nb-field="period">
      </div>
      <div class="form-row">
        <label>Threshold</label>
        <select data-nb-ind="${i}" data-nb-field="threshold">
          ${['below_30', 'below_35', 'below_40', 'above_60', 'above_65', 'above_70'].map(t =>
            `<option value="${t}" ${ind.threshold === t ? 'selected' : ''}>${t}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'EMA_CROSS') {
    return `
      <div class="form-row">
        <label>Fast / Slow</label>
        <div class="slider-row">
          <input type="number" min="2" max="200" value="${ind.fast}" data-nb-ind="${i}" data-nb-field="fast">
          <input type="number" min="2" max="200" value="${ind.slow}" data-nb-ind="${i}" data-nb-field="slow">
        </div>
      </div>
      <div class="form-row">
        <label>Signal</label>
        <select data-nb-ind="${i}" data-nb-field="signal">
          <option value="bullish_cross" ${ind.signal === 'bullish_cross' ? 'selected' : ''}>Bullish</option>
          <option value="bearish_cross" ${ind.signal === 'bearish_cross' ? 'selected' : ''}>Bearish</option>
        </select>
      </div>`;
  }
  if (ind.type === 'MACD') {
    return `
      <div class="form-row">
        <label>Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${['histogram_positive', 'histogram_negative', 'bullish_cross', 'bearish_cross'].map(c =>
            `<option value="${c}" ${ind.condition === c ? 'selected' : ''}>${c}</option>`
          ).join('')}
        </select>
      </div>
      <div class="form-row"><label>&nbsp;</label><div></div></div>`;
  }
  return '';
}

function nbUpdateLeverageUI() {
  const enabled = nbState.leverage_enabled;
  $('nb-leverage-size').disabled = !enabled;
  $('nb-leverage-value').textContent = nbState.leverage_size + 'x';
  $('nb-liq-preview').textContent = enabled ? nbCalcLiqPreview() : '—';
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

  const rows = [];
  let total = nbState.base_size;
  let curPrice = price;
  rows.push({ label: 'Base', size: nbState.base_size, price: curPrice, total, dropPct: null });

  for (let i = 1; i < nbState.dca_max_orders; i++) {
    const spacing = nbState.dca_spacing_pct * Math.pow(nbState.dca_step_scale, i - 1);
    curPrice = curPrice * (1 - spacing / 100);
    const size = nbState.dca_size * Math.pow(nbState.dca_volume_scale, i - 1);
    total += size;
    const dropPct = ((price - curPrice) / price * 100).toFixed(2);
    rows.push({ label: `DCA ${i}`, size, price: curPrice, total, dropPct });
  }

  const unit = nbState.base_unit === 'btc' ? 'BTC' : '%';
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${r.label}</td>
      <td>${r.size.toFixed(4)} ${unit}</td>
      <td>${fmtPrice(r.price)}${r.dropPct != null ? ` <span class="muted-cell">(-${r.dropPct}%)</span>` : ''}</td>
      <td>${r.total.toFixed(4)} ${unit}</td>
    </tr>
  `).join('');
}

function nbCalcTotalSize() {
  let total = nbState.base_size;
  for (let i = 1; i < nbState.dca_max_orders; i++) {
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
      <div class="review-row"><span class="review-key">Max orders</span><span>${nbState.dca_max_orders}</span></div>
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
      max_orders: nbState.dca_max_orders,
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

  $('hdr-context').textContent = '← Overview';
  $('hdr-context').classList.add('clickable');
  $('hdr-context').onclick = goOverview;
  $('hdr-pill').classList.remove('hidden');

  document.querySelectorAll('#main-nav .tab').forEach(t => t.classList.remove('active'));
  $('detail-nav-item').classList.remove('hidden');
  $('detail-nav-btn').textContent = slug;
  $('detail-nav-btn').classList.add('active');

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

    $('log-title').textContent = slug + '.log';

  } catch (e) {}
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
      <div class="cfg-row"><span class="cfg-key">Max orders</span><span>${dca.max_orders != null ? dca.max_orders : '—'}</span></div>
      <div class="cfg-row"><span class="cfg-key">Order spacing</span><span>${dca.order_spacing_pct != null ? dca.order_spacing_pct + '%' : '—'}</span></div>
      <div class="cfg-row"><span class="cfg-key">Multiplier</span><span>${dca.multiplier != null ? dca.multiplier : '—'}</span></div>
      <div class="cfg-row"><span class="cfg-key">Taker fee</span><span>${dca.taker_fee != null ? (dca.taker_fee * 100).toFixed(3) + '%' : '—'}</span></div>
    </div>
  `;
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
    sl_type:              sl.type || d.sl_type,
    sl_pct:               sl.pct != null ? sl.pct : d.sl_pct,
    dca_max_orders:       dca.max_orders != null ? dca.max_orders : d.dca_max_orders,
    // YAML only stores base_order_size; mirror it as the initial DCA size.
    dca_size:             dca.base_order_size != null ? dca.base_order_size : d.dca_size,
    dca_spacing_pct:      dca.order_spacing_pct != null ? dca.order_spacing_pct : d.dca_spacing_pct,
    dca_volume_scale:     dca.multiplier != null ? dca.multiplier : d.dca_volume_scale,
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
  btn.textContent = '↺ Restarting...';
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
        btn.textContent = '↺ Restart Dashboard';
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
      if (['period', 'fast', 'slow'].includes(f)) v = parseInt(v, 10) || 0;
      nbState.indicators[i][f] = v;
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
