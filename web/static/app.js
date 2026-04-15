// web/static/app.js — Reverto portal frontend
// Moved out of an inline <script> in index.html so CSP doesn't need
// 'unsafe-inline' on script-src. All event handlers are wired via
// addEventListener in setupEventListeners() — no onclick="..." attributes.

// ── Persisted UI settings (theme, text size, brightness, compact) ────────────
// Applied as early as possible so there is no visual flash between the
// default dark/normal palette and the user's saved preferences.
// Baseline font-size used by body{} in style.css. The text-size slider is
// expressed as the desired body font-size in px; we translate it into a
// uniform `zoom` factor on <html> because the existing stylesheet uses
// absolute px for every font-size declaration, so setting html{font-size}
// alone would be a no-op (nothing cascades from it).
const _TEXTSIZE_BASE = 14;
function _applyTextSizeZoom(px) {
  const v = Math.max(12, Math.min(18, parseInt(px, 10) || _TEXTSIZE_BASE));
  document.documentElement.style.zoom = String(v / _TEXTSIZE_BASE);
}
function applyPersistedSettings() {
  const ts = parseInt(localStorage.getItem('reverto-textsize'), 10);
  if (Number.isFinite(ts) && ts >= 12 && ts <= 18) {
    _applyTextSizeZoom(ts);
  }
  const br = localStorage.getItem('reverto-brightness');
  if (br === 'dimmed' || br === 'normal' || br === 'bright') {
    document.documentElement.dataset.brightness = br;
  } else {
    document.documentElement.dataset.brightness = 'normal';
  }
  const th = localStorage.getItem('reverto-theme');
  if (th === 'light' || th === 'dark') {
    document.documentElement.dataset.theme = th;
  } else {
    document.documentElement.dataset.theme = 'dark';
  }
  const compact = localStorage.getItem('reverto-compact') === '1';
  document.documentElement.classList.toggle('compact', compact);
}
applyPersistedSettings();

// ── API Key management ────────────────────────────────────────────────────────
// The portal now uses session-cookie auth for browser users. The API key is
// kept around purely as an alternative for scripts and CLI tools that don't
// hold a session — set it via Profile → API Key. The SPA itself never sends
// the X-API-Key header anymore.
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
}
function clearApiKey() {
  localStorage.removeItem('reverto_api_key');
  document.getElementById('api-key-input').value = '';
  closeApiKeyModal();
}

// ── Session auth ──────────────────────────────────────────────────────────────
async function checkAuthStatus() {
  try {
    const r = await fetch('/auth/status');
    if (r.status === 401) return false;
    const j = await r.json();
    if (j && j.username) _cachedUsername = j.username;
    return Boolean(j.authenticated);
  } catch (e) { return false; }
}

function _handle401() {
  // Stop background polling so we don't keep hammering protected endpoints
  // after the session has expired.
  if (overviewInterval) { clearInterval(overviewInterval); overviewInterval = null; }
  if (detailInterval)   { clearInterval(detailInterval);   detailInterval   = null; }
  if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  try { disconnectStateWS(); } catch (e) {}
  document.querySelectorAll('.page').forEach(p => {
    p.classList.remove('active');
    p.classList.add('hidden');
  });
  const login = document.getElementById('view-login');
  if (login) {
    login.classList.remove('hidden');
    login.classList.add('active');
  }
  // Strip the chrome (main nav, header buttons, state indicators) so
  // the login screen is the only thing visible. CSS hides everything
  // except the REVERTO logo when body carries .is-login.
  document.body.classList.add('is-login');
}

async function handleLoginSubmit(e) {
  if (e && e.preventDefault) e.preventDefault();
  const u = document.getElementById('login-username').value;
  const p = document.getElementById('login-password').value;
  const err = document.getElementById('login-error');
  err.classList.add('hidden');
  try {
    const r = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p }),
    });
    if (!r.ok) {
      err.textContent = 'Invalid credentials';
      err.classList.remove('hidden');
      return;
    }
    location.reload();
  } catch (e2) {
    err.textContent = 'Login failed';
    err.classList.remove('hidden');
  }
}

async function handleLogout() {
  try { await fetch('/auth/logout', { method: 'POST' }); } catch (e) {}
  location.reload();
}

// ── Profile / Settings / dropdown menu ───────────────────────────────────────
// _cachedUsername is populated from /auth/status so the profile initial
// and the Profile modal username field stay in sync without re-fetching
// on every render.
let _cachedUsername = '';

function refreshProfileInitial() {
  const display = (localStorage.getItem('reverto-display-name') || '').trim();
  const src = display || _cachedUsername || '';
  const ch = (src.charAt(0) || 'A').toUpperCase();
  const el = document.getElementById('profile-initial');
  if (el) el.textContent = ch;
}

function toggleProfileMenu(force) {
  const menu = document.getElementById('profile-menu');
  const btn  = document.getElementById('profile-btn');
  if (!menu || !btn) return;
  const willOpen = force === undefined ? menu.classList.contains('hidden') : force;
  if (willOpen) {
    menu.classList.remove('hidden');
    btn.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
  } else {
    menu.classList.add('hidden');
    btn.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
  }
}

function _installProfileOutsideClickHandler() {
  document.addEventListener('click', (e) => {
    const menu = document.getElementById('profile-menu');
    const btn  = document.getElementById('profile-btn');
    if (!menu || !btn) return;
    if (menu.classList.contains('hidden')) return;
    if (btn.contains(e.target) || menu.contains(e.target)) return;
    toggleProfileMenu(false);
  });
}

async function showProfileModal() {
  toggleProfileMenu(false);
  // Username: prefer cached, otherwise fetch /auth/status.
  if (!_cachedUsername) {
    try {
      const r = await fetch('/auth/status');
      if (r.ok) {
        const j = await r.json();
        if (j && j.username) _cachedUsername = j.username;
      }
    } catch (e) {}
  }
  document.getElementById('profile-username').value = _cachedUsername || '';
  document.getElementById('profile-display-name').value =
    localStorage.getItem('reverto-display-name') || '';
  // Show the API key masked: first 8 chars visible, the rest replaced
  // by a fixed-length run of asterisks. The full key is still kept in
  // localStorage and copied to the clipboard by the Copy button.
  const fullKey = getApiKey();
  const apiInput = document.getElementById('profile-api-key');
  apiInput.value = fullKey
    ? fullKey.slice(0, 8) + '*'.repeat(Math.max(0, fullKey.length - 8))
    : '(not set)';
  apiInput.dataset.fullKey = fullKey || '';
  document.getElementById('profile-api-copy-status').classList.add('hidden');
  // Clear any stale password state.
  document.getElementById('profile-pw-current').value = '';
  document.getElementById('profile-pw-new').value = '';
  document.getElementById('profile-pw-confirm').value = '';
  const err = document.getElementById('profile-pw-error');
  const ok  = document.getElementById('profile-pw-success');
  err.classList.add('hidden'); err.textContent = '';
  ok.classList.add('hidden');
  document.getElementById('profile-modal').classList.add('show');
}
function closeProfileModal() {
  document.getElementById('profile-modal').classList.remove('show');
  document.getElementById('profile-pw-current').value = '';
  document.getElementById('profile-pw-new').value = '';
  document.getElementById('profile-pw-confirm').value = '';
}
async function copyProfileApiKey() {
  const status = document.getElementById('profile-api-copy-status');
  const apiInput = document.getElementById('profile-api-key');
  const fullKey = (apiInput && apiInput.dataset.fullKey) || '';
  if (!fullKey) {
    status.textContent = 'No API key set yet.';
    status.classList.remove('hidden');
    return;
  }
  let copied = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(fullKey);
      copied = true;
    }
  } catch (e) { copied = false; }
  if (!copied) {
    // Clipboard API unavailable (insecure context, no permission). Fall
    // back to a temporary textarea + execCommand("copy") so the button
    // still works on plain http://.
    try {
      const ta = document.createElement('textarea');
      ta.value = fullKey;
      ta.setAttribute('readonly', '');
      ta.style.position = 'absolute';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      copied = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (e) { copied = false; }
  }
  status.textContent = copied ? 'Copied to clipboard.' : 'Copy failed.';
  status.classList.remove('hidden');
}
async function saveProfileModal() {
  const err = document.getElementById('profile-pw-error');
  const ok  = document.getElementById('profile-pw-success');
  err.classList.add('hidden'); err.textContent = '';
  ok.classList.add('hidden');

  // 1) Display name
  const dn = (document.getElementById('profile-display-name').value || '').trim();
  if (dn) localStorage.setItem('reverto-display-name', dn);
  else    localStorage.removeItem('reverto-display-name');
  refreshProfileInitial();

  // 2) Password change (only if any of the three fields is non-empty)
  const cur  = document.getElementById('profile-pw-current').value;
  const neu  = document.getElementById('profile-pw-new').value;
  const conf = document.getElementById('profile-pw-confirm').value;
  if (cur || neu || conf) {
    if (!cur) {
      err.textContent = 'Current password is required';
      err.classList.remove('hidden'); return;
    }
    if (neu.length < 8) {
      err.textContent = 'New password must be at least 8 characters';
      err.classList.remove('hidden'); return;
    }
    if (neu !== conf) {
      err.textContent = 'New passwords do not match';
      err.classList.remove('hidden'); return;
    }
    try {
      const r = await fetch('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: cur, new_password: neu }),
      });
      if (!r.ok) {
        let msg = 'Change failed';
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (e) {}
        err.textContent = msg;
        err.classList.remove('hidden');
        return;
      }
      ok.classList.remove('hidden');
      document.getElementById('profile-pw-current').value = '';
      document.getElementById('profile-pw-new').value = '';
      document.getElementById('profile-pw-confirm').value = '';
      return;
    } catch (e2) {
      err.textContent = 'Network error';
      err.classList.remove('hidden');
      return;
    }
  }

  // 3) Nothing broke → close.
  closeProfileModal();
}

// ── Settings modal ───────────────────────────────────────────────────────────
function _setSegActive(container, attr, value) {
  container.querySelectorAll('.settings-seg-btn').forEach(b => {
    b.classList.toggle('active', b.dataset[attr] === value);
  });
}
function _settingsApplyTextSize(n, persist) {
  const v = Math.max(12, Math.min(18, parseInt(n, 10) || _TEXTSIZE_BASE));
  _applyTextSizeZoom(v);
  const lbl = document.getElementById('settings-textsize-label');
  if (lbl) lbl.textContent = v + 'px';
  const slider = document.getElementById('settings-textsize');
  if (slider) slider.value = String(v);
  if (persist) localStorage.setItem('reverto-textsize', String(v));
}
function _settingsApplyBrightness(b, persist) {
  const v = (b === 'dimmed' || b === 'bright') ? b : 'normal';
  document.documentElement.dataset.brightness = v;
  const segs = document.querySelectorAll('#settings-modal .settings-seg');
  if (segs[0]) _setSegActive(segs[0], 'brightness', v);
  if (persist) localStorage.setItem('reverto-brightness', v);
}
function _settingsApplyTheme(t, persist) {
  const v = (t === 'light') ? 'light' : 'dark';
  document.documentElement.dataset.theme = v;
  const segs = document.querySelectorAll('#settings-modal .settings-seg');
  if (segs[1]) _setSegActive(segs[1], 'theme', v);
  if (persist) localStorage.setItem('reverto-theme', v);
  // Push the new palette into any live chart instance so the operator
  // sees the colour change without having to reopen the chart tab.
  if (typeof _applyChartTheme === 'function') _applyChartTheme();
}
function _settingsApplyCompact(c, persist) {
  const v = Boolean(c);
  document.documentElement.classList.toggle('compact', v);
  const cb = document.getElementById('settings-compact');
  if (cb) cb.checked = v;
  if (persist) localStorage.setItem('reverto-compact', v ? '1' : '0');
}
function _settingsRenderFromState() {
  const ts = parseInt(localStorage.getItem('reverto-textsize'), 10);
  _settingsApplyTextSize(Number.isFinite(ts) ? ts : 14, false);
  _settingsApplyBrightness(localStorage.getItem('reverto-brightness') || 'normal', false);
  _settingsApplyTheme(localStorage.getItem('reverto-theme') || 'dark', false);
  _settingsApplyCompact(localStorage.getItem('reverto-compact') === '1', false);
}
function showSettingsModal() {
  toggleProfileMenu(false);
  _settingsRenderFromState();
  document.getElementById('settings-modal').classList.add('show');
}
function closeSettingsModal() {
  document.getElementById('settings-modal').classList.remove('show');
}
function resetSettingsDefaults() {
  localStorage.removeItem('reverto-textsize');
  localStorage.removeItem('reverto-brightness');
  localStorage.removeItem('reverto-theme');
  localStorage.removeItem('reverto-compact');
  _settingsApplyTextSize(14, false);
  _settingsApplyBrightness('normal', false);
  _settingsApplyTheme('dark', false);
  _settingsApplyCompact(false, false);
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
const _MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function formatDateTime(iso) {
  // Locale-independent formatter → "14 Apr 2026 11:22". Renders in the
  // browser's local timezone (matching the rest of the dashboard).
  if (!iso) return '—';
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return '—';
  const day = String(d.getDate()).padStart(2, '0');
  const mon = _MONTH_ABBR[d.getMonth()];
  const yr  = d.getFullYear();
  const hh  = String(d.getHours()).padStart(2, '0');
  const mm  = String(d.getMinutes()).padStart(2, '0');
  return `${day} ${mon} ${yr} ${hh}:${mm}`;
}
function timeAgo(iso) {
  if (!iso) return '—';
  const s = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function formatDuration(startIso, endIso) {
  // Human-readable diff between two ISO timestamps. Used by the closed
  // deals "Duration" column. Returns "—" if either timestamp is missing
  // or invalid so the table never shows NaN/Infinity.
  if (!startIso || !endIso) return '—';
  const start = new Date(startIso).getTime();
  const end = new Date(endIso).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '—';
  const s = Math.floor((end - start) / 1000);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return m === 0 ? h + 'h' : h + 'h ' + m + 'm';
  }
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  return h === 0 ? d + 'd' : d + 'd ' + h + 'h';
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

// /ws/state — bot-state push channel. The dashboard used to poll
// /api/bots every 5s; now we poll every 30s as a safety net and let
// the WS push handle realtime updates.
let _stateWs = null;
let _stateWsReconnectTimer = null;

function _stateWsConnected(connected) {
  // Reuse the existing .live-dot/.connected/.error convention from
  // the log streaming widgets. The indicator is optional — if index.html
  // doesn't expose #state-ws-dot we silently no-op.
  const dot = document.getElementById('state-ws-dot');
  if (dot) {
    dot.classList.toggle('connected', !!connected);
    dot.classList.toggle('error', !connected);
  }
  const lbl = document.getElementById('state-ws-label');
  if (lbl) {
    lbl.textContent = connected ? 'live' : 'reconnecting...';
    lbl.classList.toggle('label-ok', !!connected);
    lbl.classList.toggle('label-err', !connected);
  }
}

function connectStateWS() {
  if (_stateWs) { try { _stateWs.close(); } catch (e) {} _stateWs = null; }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  _stateWs = new WebSocket(`${proto}//${location.host}/ws/state`);
  _stateWs.onmessage = (e) => {
    if (e.data === '__ping__') return;
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'bot_state') {
        _onWsBotState(msg.slug, msg.data);
      } else if (msg.type === 'summary') {
        _onWsSummary(msg.data);
      }
    } catch (err) { /* malformed frame — ignore */ }
  };
  _stateWs.onopen = () => {
    _stateWsConnected(true);
    // Sync after reconnect so we never miss an update during the gap.
    fetchOverview();
  };
  _stateWs.onclose = () => {
    _stateWsConnected(false);
    if (_stateWsReconnectTimer) clearTimeout(_stateWsReconnectTimer);
    _stateWsReconnectTimer = setTimeout(connectStateWS, 2000);
  };
  _stateWs.onerror = () => { try { _stateWs.close(); } catch (e) {} };
}

function disconnectStateWS() {
  if (_stateWsReconnectTimer) {
    clearTimeout(_stateWsReconnectTimer);
    _stateWsReconnectTimer = null;
  }
  if (_stateWs) {
    try { _stateWs.onclose = null; _stateWs.close(); } catch (e) {}
    _stateWs = null;
  }
  _stateWsConnected(false);
}

function updateBotCard(slug, b) {
  const card = document.querySelector(`.bot-card[data-slug="${CSS.escape(slug)}"]`);
  if (!card) {
    // New bot we don't have a card for yet — fall back to a full
    // re-render so the wholesale renderBotGrid path picks it up.
    fetchOverview();
    return;
  }
  const winRate    = Number(b.win_rate) || 0;
  const balanceBtc = Number(b.balance_btc) || 0;
  const openCount  = Number(b.open_deals_count) || 0;
  const closedCount= Number(b.closed_deals_count) || 0;
  const pnl        = Number(b.total_pnl_btc) || 0;

  const setText = (selector, text) => {
    const el = card.querySelector(selector);
    if (el) el.textContent = text;
  };
  setText('[data-stat="price"]', b.current_price ? fmtPrice(b.current_price) : '—');
  setText('[data-stat="balance"]', balanceBtc ? balanceBtc.toFixed(4) : '—');
  setText('[data-stat="winrate"]', `${winRate.toFixed(0)}%`);
  setText('[data-stat="open"]', String(openCount));
  setText('[data-stat="closed"]', String(closedCount));

  const pnlEl = card.querySelector('[data-stat="pnl"]');
  if (pnlEl) {
    const sign = pnl >= 0 ? '+' : '';
    pnlEl.textContent = `${sign}${pnl.toFixed(6)}`;
    pnlEl.classList.remove('pos', 'neg', 'neu');
    pnlEl.classList.add(pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu');
  }

  const pill = card.querySelector('.pill');
  if (pill) {
    pill.classList.remove('running', 'stopped');
    pill.classList.add(b.running ? 'running' : 'stopped');
    const span = pill.querySelector('span');
    if (span) span.textContent = b.running ? 'Running' : 'Stopped';
  }
}

function _onWsBotState(slug, data) {
  updateBotCard(slug, data);
}

function _onWsSummary(s) {
  if (!s) return;
  const pnlEl = $('ov-pnl');
  if (pnlEl) pnlEl.innerHTML = fmtPnl(s.total_pnl_btc || 0, 8);
  const acEl = $('ov-active');
  if (acEl) acEl.textContent = (s.active_bots ?? '—').toString();
  const totEl = $('ov-total-sub');
  if (totEl) totEl.textContent = `of ${s.total_bots ?? 0} configured`;
  const dlEl = $('ov-deals');
  if (dlEl) dlEl.textContent = (s.open_deals ?? '—').toString();

  // Running / stopped counts — derived from active_bots + total_bots.
  const running = Number(s.active_bots) || 0;
  const total   = Number(s.total_bots) || 0;
  const runEl = $('ov-running-count');
  if (runEl) runEl.textContent = String(running);
  const stopEl = $('ov-stopped-count');
  if (stopEl) stopEl.textContent = String(Math.max(0, total - running));
}

// ── Navigation ────────────────────────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  $('view-' + name).classList.add('active');
}

function showDTab(name, btn) {
  ['chart', 'dashboard', 'deals', 'backtest', 'config', 'log'].forEach(n => {
    const el = $('dtab-' + n);
    if (el) { el.classList.toggle('hidden', n !== name); }
  });
  document.querySelectorAll('.detail-subnav .tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (name === 'config' && currentSlug) fetchDetailConfig(currentSlug);
  if (name === 'chart' && currentSlug) {
    loadChartTab(currentSlug);
  } else {
    teardownChartTab();
  }
}

// ── Overview ──────────────────────────────────────────────────────────────────
async function fetchOverview() {
  try {
    const r = await fetch('/api/bots');
    if (r.status === 401) { _handle401(); return; }
    const d = await r.json();
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

// ── Active Deals column manager ─────────────────────────────────────────────
// Default ordered column set. Each column has a key (used as localStorage
// id), label (shown in cog menu + thead), and a cell renderer that takes
// a deal row and returns an HTML string. Visibility + order live in
// localStorage under "reverto.active_deals_columns".
const ACTIVE_DEALS_COLUMNS = [
  { key: 'bot',        label: 'Bot',
    cell: d => `<td><span class="link-like" data-action="open" data-slug="${safeText(d.bot_slug)}">${safeText(d.bot_name)}</span></td>` },
  { key: 'deal_id',    label: 'Deal ID',
    cell: d => `<td class="muted-cell">${safeText(d.id)}</td>` },
  { key: 'pair',       label: 'Pair',
    cell: d => `<td>${safeText(d.symbol || '—')}</td>` },
  { key: 'entry',      label: 'Entry',
    cell: d => `<td>${fmtPrice(d.entry_price)}</td>` },
  { key: 'avg_entry',  label: 'Avg Entry',
    cell: d => `<td>${fmtPrice(d.avg_entry_price)}</td>` },
  { key: 'orders',     label: 'Orders',
    cell: d => `<td>${d.order_count}</td>` },
  { key: 'pnl_btc',    label: 'PnL BTC',
    cell: d => `<td>${fmtPnl(d.pnl_btc)}</td>` },
  { key: 'pnl_pct',    label: 'PnL %',
    cell: d => `<td>${fmtPct(d.pnl_pct)}</td>` },
  { key: 'started',    label: 'Start Date',
    cell: d => `<td class="muted-cell" title="${safeText(d.opened_at || '')}">${formatDateTime(d.opened_at)}</td>` },
  { key: 'age',        label: 'Age',
    cell: d => `<td class="muted-cell">${timeAgo(d.opened_at)}</td>` },
];
const ACTIVE_DEALS_LS_KEY = 'reverto.active_deals_columns';

// Generic column-config helpers — single source of truth = localStorage.
// Both renderers and cog-menu drag handlers call loadColumns() on every
// invocation, so a freshly dragged order can never be clobbered by a
// poll-driven re-render. The merge logic appends any default column not
// present in the stored array (keeps newly added columns visible by
// default for users with stale localStorage entries).
function loadColumns(key, defaults) {
  const baseline = defaults.map(c => ({ key: c.key, label: c.label, visible: true }));
  let stored = null;
  try {
    const raw = localStorage.getItem(key);
    if (raw) stored = JSON.parse(raw);
  } catch (e) {}
  if (!Array.isArray(stored)) return baseline;

  const known = new Map(baseline.map(c => [c.key, c]));
  const out = [];
  const seen = new Set();
  for (const col of stored) {
    if (!col || typeof col.key !== 'string') continue;
    const def = known.get(col.key);
    if (!def) continue;
    out.push({ key: def.key, label: def.label, visible: col.visible !== false });
    seen.add(col.key);
  }
  for (const d of baseline) {
    if (!seen.has(d.key)) out.push({ ...d });
  }
  return out;
}

function saveColumns(key, cols) {
  try {
    localStorage.setItem(
      key,
      JSON.stringify(cols.map(c => ({ key: c.key, visible: c.visible })))
    );
  } catch (e) {}
}

function getActiveDealsColumns() {
  return loadColumns(ACTIVE_DEALS_LS_KEY, ACTIVE_DEALS_COLUMNS);
}
function saveActiveDealsColumns(cols) {
  saveColumns(ACTIVE_DEALS_LS_KEY, cols);
}

// ── Closed Deals column manager ─────────────────────────────────────────────
// The detail Deals tab's "Closed deals" table is driven by the same loadColumns
// pattern as Active Deals. Columns include Opened/Closed/Duration which the
// previous fixed-table layout did not surface. The cog menu that lets the
// user toggle/reorder these columns is wired up in a separate fix; this
// commit only adds the column array + render path.
const CLOSED_DEALS_COLUMNS = [
  { key: 'deal_id',     label: 'Deal ID',
    cell: d => `<td class="muted-cell">${safeText(d.id)}</td>` },
  { key: 'side',        label: 'Side',
    cell: d => `<td>${safeText((d.side || '—').toUpperCase())}</td>` },
  { key: 'avg_entry',   label: 'Avg Entry',
    cell: d => `<td>${fmtPrice(d.avg_entry_price || d.entry_price)}</td>` },
  { key: 'close_price', label: 'Close Price',
    cell: d => `<td>${fmtPrice(d.close_price)}</td>` },
  { key: 'pnl_btc',     label: 'PnL BTC',
    cell: d => `<td>${fmtPnl(d.pnl_btc)}</td>` },
  { key: 'pnl_pct',     label: 'PnL %',
    cell: d => `<td>${fmtPct(d.pnl_pct)}</td>` },
  { key: 'reason',      label: 'Reason',
    cell: d => `<td>${reasonBadge(d.close_reason)}</td>` },
  { key: 'opened',      label: 'Start Date',
    cell: d => `<td class="muted-cell" title="${safeText(d.opened_at || '')}">${formatDateTime(d.opened_at)}</td>` },
  { key: 'closed',      label: 'Close Date',
    cell: d => `<td class="muted-cell" title="${safeText(d.closed_at || '')}">${formatDateTime(d.closed_at)}</td>` },
  { key: 'duration',    label: 'Duration',
    cell: d => `<td class="muted-cell">${formatDuration(d.opened_at, d.closed_at)}</td>` },
];
const CLOSED_DEALS_LS_KEY = 'reverto.closed_deals_columns';

function getClosedDealsColumns() {
  return loadColumns(CLOSED_DEALS_LS_KEY, CLOSED_DEALS_COLUMNS);
}

function _renderColumnDrivenTable(theadId, tbodyId, lsKey, defaults, rows, emptyMsg, opts) {
  const head = $(theadId);
  const tbody = $(tbodyId);
  if (!tbody) return;
  const cols = loadColumns(lsKey, defaults).filter(c => c.visible);
  const defs = new Map(defaults.map(c => [c.key, c]));
  if (head) {
    head.innerHTML = cols.map((c, i) =>
      `<th draggable="true" data-col-idx="${i}" data-col-key="${safeText(c.key)}">${safeText((defs.get(c.key) || c).label)}</th>`
    ).join('');
    _attachHeaderDragHandlers(head, lsKey, defaults);
  }
  const colSpan = Math.max(1, cols.length);
  if (!rows || !rows.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${colSpan}">${safeText(emptyMsg)}</td></tr>`;
    return;
  }
  // Optional row decoration: Feature 1 (deal timeline) uses this to tag
  // each row with the deal id and a clickable-row class so the tbody
  // delegate handler can find the deal without re-rendering the table.
  const rowAttrs = (opts && typeof opts.rowAttrs === 'function') ? opts.rowAttrs : null;
  tbody.innerHTML = rows.map(row => {
    const cells = cols.map(c => {
      const def = defs.get(c.key);
      return def ? def.cell(row) : '<td></td>';
    }).join('');
    const attrs = rowAttrs ? rowAttrs(row) : '';
    return `<tr${attrs ? ' ' + attrs : ''}>${cells}</tr>`;
  }).join('');
}

function renderDetailClosedDeals(deals) {
  _renderColumnDrivenTable(
    'd-closed-thead-row', 'd-closed-tbody',
    CLOSED_DEALS_LS_KEY, CLOSED_DEALS_COLUMNS,
    deals, 'No closed deals',
    { rowAttrs: d => `data-deal-id="${safeText(d.id)}" class="clickable-row"` },
  );
}

// ── Detail Open Deals column manager ────────────────────────────────────────
// The bot detail view's Open deals table uses its own column set + storage
// key so it can stay independent from the global Active Deals table — the
// detail view doesn't need a "Bot" identifying column since the slug is
// already in the page header.
const DETAIL_OPEN_DEALS_COLUMNS = [
  { key: 'deal_id',   label: 'Deal ID',
    cell: d => `<td class="deal-id-cell">${safeText(d.id)}</td>` },
  { key: 'pair',      label: 'Pair',
    cell: d => `<td>${safeText(d.symbol || '—')}</td>` },
  { key: 'entry',     label: 'Entry',
    cell: d => `<td>${fmtPrice(d.entry_price)}</td>` },
  { key: 'avg_entry', label: 'Avg Entry',
    cell: d => `<td>${fmtPrice(d.avg_entry_price)}</td>` },
  { key: 'orders',    label: 'Orders',
    cell: d => `<td>${d.order_count}</td>` },
  { key: 'pnl_btc',   label: 'PnL BTC',
    cell: d => `<td>${fmtPnl(d.pnl_btc)}</td>` },
  { key: 'pnl_pct',   label: 'PnL %',
    cell: d => `<td>${fmtPct(d.pnl_pct)}</td>` },
  { key: 'started',   label: 'Start Date',
    cell: d => `<td class="muted-cell" title="${safeText(d.opened_at || '')}">${formatDateTime(d.opened_at)}</td>` },
  { key: 'age',       label: 'Age',
    cell: d => `<td class="muted-cell">${timeAgo(d.opened_at)}</td>` },
];
const DETAIL_OPEN_DEALS_LS_KEY = 'reverto.detail_open_deals_columns';

function renderDetailOpenDeals(deals) {
  _renderColumnDrivenTable(
    'd-open-thead-row', 'd-open-tbody',
    DETAIL_OPEN_DEALS_LS_KEY, DETAIL_OPEN_DEALS_COLUMNS,
    deals, 'No open deals',
    { rowAttrs: d => `data-deal-id="${safeText(d.id)}" class="clickable-row"` },
  );
}

function renderActiveDealsHead() {
  const head = $('all-deals-thead-row');
  if (!head) return;
  const cols = getActiveDealsColumns().filter(c => c.visible);
  const defs = new Map(ACTIVE_DEALS_COLUMNS.map(c => [c.key, c]));
  head.innerHTML = cols.map((c, i) =>
    `<th draggable="true" data-col-idx="${i}" data-col-key="${safeText(c.key)}">${safeText((defs.get(c.key) || c).label)}</th>`
  ).join('');
  _attachHeaderDragHandlers(head, ACTIVE_DEALS_LS_KEY, ACTIVE_DEALS_COLUMNS);
}

// Map lsKey → re-render callback so the header drag drop can refresh
// both the table and any open cog menu after persisting a new order.
const _HEADER_RERENDER = new Map();

function _attachHeaderDragHandlers(headEl, lsKey, defaults) {
  // Header drag-and-drop: swaps two visible columns by their VISIBLE
  // index, then maps that back to the underlying column array (which
  // includes hidden columns) before persisting. This keeps hidden
  // columns in their original slots so toggling them back doesn't
  // shuffle into a surprise position.
  const ths = Array.from(headEl.querySelectorAll('th'));
  ths.forEach(th => {
    th.addEventListener('dragstart', e => {
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', th.dataset.colKey || ''); } catch (err) {}
      th.classList.add('dragging');
    });
    th.addEventListener('dragend', () => {
      th.classList.remove('dragging');
      ths.forEach(x => x.classList.remove('drop-before', 'drop-after'));
    });
    th.addEventListener('dragover', e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const half = th.offsetWidth / 2;
      const before = e.offsetX < half;
      ths.forEach(x => x.classList.remove('drop-before', 'drop-after'));
      th.classList.add(before ? 'drop-before' : 'drop-after');
    });
    th.addEventListener('dragleave', () => {
      th.classList.remove('drop-before', 'drop-after');
    });
    th.addEventListener('drop', e => {
      e.preventDefault();
      ths.forEach(x => x.classList.remove('drop-before', 'drop-after'));
      let srcKey = '';
      try { srcKey = e.dataTransfer.getData('text/plain'); } catch (err) {}
      const dstKey = th.dataset.colKey;
      if (!srcKey || !dstKey || srcKey === dstKey) return;
      const cur = loadColumns(lsKey, defaults);
      const a = cur.findIndex(c => c.key === srcKey);
      const b = cur.findIndex(c => c.key === dstKey);
      if (a < 0 || b < 0) return;
      [cur[a], cur[b]] = [cur[b], cur[a]];
      saveColumns(lsKey, cur);
      const cb = _HEADER_RERENDER.get(lsKey);
      if (cb) cb();
    });
  });
}

function renderActiveDeals(deals) {
  const tbody = $('all-deals-tbody');
  if (!tbody) return;
  renderActiveDealsHead();
  const cols = getActiveDealsColumns().filter(c => c.visible);
  const colSpan = Math.max(1, cols.length);
  if (!deals.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${colSpan}">No open deals across any bot</td></tr>`;
    return;
  }
  const defs = new Map(ACTIVE_DEALS_COLUMNS.map(c => [c.key, c]));
  tbody.innerHTML = deals.map(deal => {
    const cells = cols.map(c => {
      const def = defs.get(c.key);
      return def ? def.cell(deal) : '<td></td>';
    }).join('');
    return `<tr>${cells}</tr>`;
  }).join('');
}

// Track every cog-menu instance we set up. The single document-level
// click listener uses this list to close any open menu on outside clicks
// — adding a new cog menu does not need its own outside-click handler.
const _COG_MENUS = [];

function renderCogMenu(menuEl, lsKey, defaults, onChange) {
  const cols = loadColumns(lsKey, defaults);
  menuEl.innerHTML = '';
  cols.forEach((col, idx) => {
    const row = document.createElement('div');
    row.className = 'cog-menu-row';
    row.dataset.colKey = col.key;
    row.dataset.colIdx = String(idx);

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'checkbox-accent';
    cb.checked = col.visible !== false;
    cb.addEventListener('change', () => {
      const cur = loadColumns(lsKey, defaults);
      const target = cur.find(c => c.key === col.key);
      if (target) target.visible = cb.checked;
      saveColumns(lsKey, cur);
      renderCogMenu(menuEl, lsKey, defaults, onChange);
      onChange();
    });

    const lbl = document.createElement('label');
    lbl.textContent = col.label;
    lbl.addEventListener('click', e => {
      e.preventDefault();
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('change'));
    });

    // Up/down arrow buttons to reorder — replaces the HTML5 drag that
    // was hard to trigger in a compact dropdown menu. Disabled at the
    // ends; swap with neighbour on click.
    const up = document.createElement('button');
    up.type = 'button';
    up.className = 'cog-arrow-btn';
    up.textContent = '↑';
    up.disabled = idx === 0;
    up.addEventListener('click', () => {
      if (idx === 0) return;
      const cur = loadColumns(lsKey, defaults);
      [cur[idx - 1], cur[idx]] = [cur[idx], cur[idx - 1]];
      saveColumns(lsKey, cur);
      renderCogMenu(menuEl, lsKey, defaults, onChange);
      onChange();
    });

    const down = document.createElement('button');
    down.type = 'button';
    down.className = 'cog-arrow-btn';
    down.textContent = '↓';
    down.disabled = idx === cols.length - 1;
    down.addEventListener('click', () => {
      const cur = loadColumns(lsKey, defaults);
      if (idx >= cur.length - 1) return;
      [cur[idx + 1], cur[idx]] = [cur[idx], cur[idx + 1]];
      saveColumns(lsKey, cur);
      renderCogMenu(menuEl, lsKey, defaults, onChange);
      onChange();
    });

    row.appendChild(cb);
    row.appendChild(lbl);
    row.appendChild(up);
    row.appendChild(down);

    menuEl.appendChild(row);
  });
}

function setupCogMenu(btnId, menuId, lsKey, defaults, onChange) {
  const btn  = $(btnId);
  const menu = $(menuId);
  if (!btn || !menu) return;
  _COG_MENUS.push({ btn, menu });
  // Header-drag swaps re-use this same onChange so a drop refreshes the
  // table immediately instead of waiting for the next poll.
  _HEADER_RERENDER.set(lsKey, onChange);
  btn.addEventListener('click', e => {
    e.stopPropagation();
    if (menu.classList.contains('hidden')) {
      renderCogMenu(menu, lsKey, defaults, onChange);
      menu.classList.remove('hidden');
    } else {
      menu.classList.add('hidden');
    }
  });
}

function _installCogOutsideClickHandler() {
  // Generic outside-click closer. Iterates every registered cog menu so
  // future cogs do not need to add their own handler.
  document.addEventListener('click', e => {
    for (const { btn, menu } of _COG_MENUS) {
      if (menu.classList.contains('hidden')) continue;
      if (e.target === btn || btn.contains(e.target)) continue;
      if (menu.contains(e.target)) continue;
      menu.classList.add('hidden');
    }
  });
}

function setupActiveDealsCog() {
  setupCogMenu(
    'active-deals-cog', 'active-deals-cog-menu',
    ACTIVE_DEALS_LS_KEY, ACTIVE_DEALS_COLUMNS,
    () => fetchOverview(),
  );
}

function setupDetailOpenDealsCog() {
  setupCogMenu(
    'd-open-deals-cog', 'd-open-deals-cog-menu',
    DETAIL_OPEN_DEALS_LS_KEY, DETAIL_OPEN_DEALS_COLUMNS,
    () => { if (currentSlug) fetchDetail(currentSlug); },
  );
}

function setupDetailClosedDealsCog() {
  setupCogMenu(
    'd-closed-deals-cog', 'd-closed-deals-cog-menu',
    CLOSED_DEALS_LS_KEY, CLOSED_DEALS_COLUMNS,
    () => { if (currentSlug) fetchDetail(currentSlug); },
  );
}

function renderBotCard(b) {
  const running = b.running;
  // Defence-in-depth: every numeric field reaching the HTML template
  // is coerced through Number() so a tampered state.json that smuggled
  // a string past the Pydantic validator (or a future validator bug)
  // can't surface as raw markup. fmtPnl/fmtPrice already coerce too.
  const pnl = Number(b.total_pnl_btc) || 0;
  const pnlSign = pnl >= 0 ? '+' : '';
  const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu';
  const winRate = Number(b.win_rate) || 0;
  const balanceBtc = Number(b.balance_btc) || 0;
  const openCount = Number(b.open_deals_count) || 0;
  const closedCount = Number(b.closed_deals_count) || 0;

  const openDealsHtml = (b.open_deals || []).slice(0, 3).map(d => `
    <div class="bot-card-deal-row">
      <span class="deal-id-cell">${safeText(d.id)}</span>
      <span class="muted-cell">${fmtPrice(d.entry_price)}</span>
      <span>${fmtPnl(d.pnl_btc)}</span>
    </div>`).join('');

  const moreDeals = openCount > 3
    ? `<div class="more-deals-row">+${openCount - 3} more deals</div>`
    : '';

  return `
  <div class="bot-card" data-slug="${safeText(b.slug)}">
    <div class="bot-card-top">
      <span class="bot-card-name">${safeText(b.bot_name || b.slug)}</span>
      <div class="pill ${running ? 'running' : 'stopped'} tab-pill-static">
        <div class="dot"></div><span>${running ? 'Running' : 'Stopped'}</span>
      </div>
    </div>
    <div class="bot-card-meta">
      ${safeText((b.exchange || '—').toUpperCase())} · ${safeText(b.pair || 'BTC/USD')} · ${safeText((b.mode || 'paper').toUpperCase())}
      ${b.uptime ? '· ⏱ ' + safeText(b.uptime) : ''}
    </div>
    <div class="bot-card-stats">
      <div class="bot-stat">
        <div class="bot-stat-label">Price</div>
        <div class="bot-stat-value" data-stat="price">${b.current_price ? fmtPrice(b.current_price) : '—'}</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">Balance</div>
        <div class="bot-stat-value" data-stat="balance">${balanceBtc ? balanceBtc.toFixed(4) : '—'}</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">Win rate</div>
        <div class="bot-stat-value" data-stat="winrate">${winRate.toFixed(0)}%</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">PnL</div>
        <div class="bot-stat-value ${pnlCls}" data-stat="pnl">${pnlSign}${pnl.toFixed(6)}</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">Open deals</div>
        <div class="bot-stat-value" data-stat="open">${openCount}</div>
      </div>
      <div class="bot-stat">
        <div class="bot-stat-label">Closed</div>
        <div class="bot-stat-value" data-stat="closed">${closedCount}</div>
      </div>
    </div>
    ${openDealsHtml ? `<div class="bot-card-deals">${openDealsHtml}${moreDeals}</div>` : ''}
    <div class="bot-card-footer">
      ${running
        ? `<button class="btn-sm btn-stop"    data-action="stop"    data-slug="${safeText(b.slug)}">■ Stop</button>
           <button class="btn-sm btn-restart" data-action="restart" data-slug="${safeText(b.slug)}">↺ Restart</button>`
        : `<button class="btn-sm btn-start"   data-action="start"   data-slug="${safeText(b.slug)}">▶ Start</button>`
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
  const res = await fetch(`/api/bots/${slug}`, { method: 'DELETE' });
  if (res.status === 401) { _handle401(); return; }
  let detail = '';
  try { detail = (await res.json()).detail || ''; } catch (e) {}
  if (!res.ok) {
    alert(`Delete failed: ${detail || res.status}`);
    return;
  }
  // Drop any cached backtest results for the deleted bot so a
  // future bot that reuses the slug starts with a clean slate.
  delete _btResultsBySlug[slug];
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
  await _withButtonFeedback(srcBtn, action, async () => {
    try {
      const res = await fetch(`/api/bots/${slug}/${action}`, { method: 'POST' });
      if (res.status === 401) { _handle401(); return; }
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

async function manualStartDeal(slug, srcBtn = null) {
  const origLabel = srcBtn ? srcBtn.innerHTML : null;
  if (srcBtn) { srcBtn.disabled = true; srcBtn.textContent = 'Starting...'; }
  try {
    const res = await fetch(`/api/bots/${slug}/deal/start`, { method: 'POST' });
    if (res.status === 401) { _handle401(); return; }
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (e) {}
      alert(`Start Deal failed: ${detail || res.status}`);
      return;
    }
  } catch (e) {
    alert(`Start Deal failed: ${e.message}`);
  } finally {
    if (srcBtn) { srcBtn.innerHTML = origLabel; srcBtn.disabled = false; }
    if (currentSlug === slug) fetchDetail(slug);
  }
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
  _chartPendingDeal = null;
  const _ccd = $('chart-clear-deal'); if (_ccd) _ccd.classList.add('hidden');
  teardownChartTab();
  teardownWizardChart();
  if (ws) { ws.close(); ws = null; }
  $('hdr-context').textContent = 'Multi-Bot Portal';
  $('hdr-context').classList.remove('clickable');
  $('hdr-context').onclick = null;
  $('hdr-pill').classList.add('hidden');
  $('hdr-uptime').textContent = '';
}

function _ensureOverviewPolling() {
  if (!overviewInterval) {
    // Polling is now a safety net — /ws/state handles realtime updates.
    overviewInterval = setInterval(fetchOverview, 30000);
  }
}

function _pushHistory(view, hash, extra = {}) {
  try {
    history.pushState({ view, ...extra }, '', hash);
  } catch (e) {}
}

function goOverview(fromPop = false) {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-overview-btn');
  showPage('overview');
  fetchOverview();
  _ensureOverviewPolling();
  if (!fromPop) _pushHistory('overview', '#overview');
}

function goBots(fromPop = false) {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-bots-btn');
  showPage('bots');
  fetchOverview();
  _ensureOverviewPolling();
  if (!fromPop) _pushHistory('bots', '#bots');
}

function goDeals(fromPop = false) {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-deals-btn');
  showPage('deals');
  fetchOverview();
  _ensureOverviewPolling();
  if (!fromPop) _pushHistory('deals', '#deals');
}

function goNewBot() {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-bots-btn');  // new bot lives logically under Bots
  showPage('new-bot');
  nbInit();
  initWizardChart();
  fetchWizardChartData();
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
  ASAP:
    "Opens a deal immediately on the next tick, ignoring all other entry conditions.",
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
    use_wick_simulation: true,
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
  const wickEl = $('nb-use-wick-sim');
  nbState.use_wick_simulation = wickEl ? Boolean(wickEl.checked) : true;

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
  // Live update the wizard chart overlays (indicators + TP/DCA lines)
  // whenever any wizard input changes.
  if (typeof renderWizardOverlays === 'function') renderWizardOverlays();
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
  const wickInput = $('nb-use-wick-sim');
  if (wickInput) wickInput.checked = Boolean(nbState.use_wick_simulation);

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
  // Must trigger a full recompute so the wizard chart picks up the new
  // indicator overlay (e.g. the RSI sub-pane). Without this, the sub-
  // pane only showed up after a later field edit re-triggered recompute.
  nbRecompute();
}
function nbRemoveIndicator(idx) {
  nbState.indicators.splice(idx, 1);
  nbRenderIndicators();
  nbRecompute();
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
                    : ind.type === 'ASAP' ? 'type-asap'
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
              <option value="ASAP" ${ind.type === 'ASAP' ? 'selected' : ''}>ASAP (no filter)</option>
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
    use_wick_simulation: Boolean(nbState.use_wick_simulation),
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
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (res.status === 401) { _handle401(); return; }
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

function openBot(slug, fromPop = false) {
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

  // Explicit Dashboard tab selection — previously used the first
   // .detail-subnav .tab which, after Chart became the first tab, started
   // returning the Chart button, so the nav highlighted the wrong tab.
  showDTab('dashboard', document.querySelector('.detail-subnav .tab[data-dtab="dashboard"]'));

  showPage('detail');
  connectWS(slug);
  fetchDetail(slug);
  // Swap the backtest results pane for whatever was last rendered
  // for this slug (if anything). Without this, results from the
  // previously-open bot bleed through into the new bot's Backtest
  // tab until the operator runs it again.
  btRestoreResultsForSlug(slug);
  detailInterval = setInterval(() => fetchDetail(slug), 5000);
  if (!fromPop) _pushHistory('bot', `#bot/${slug}`, { slug });
}

function _routeFromHash() {
  const h = (window.location.hash || '').replace(/^#/, '');
  if (h.startsWith('bot/')) {
    const slug = h.slice(4);
    if (slug) { openBot(slug, true); return; }
  }
  switch (h) {
    case 'bots':     goBots(true); break;
    case 'deals':    goDeals(true); break;
    case 'overview': goOverview(true); break;
    default:         goOverview(true); break;
  }
}

// Cache of the most recent /api/bots/{slug} payload so row click handlers
// can find the deal object without a second fetch. Keyed by slug so a
// stale detail fetch from a previous bot never hands back the wrong deal.
let _lastDetailState = null;

function findDealByIdInCurrentDetail(id) {
  if (!_lastDetailState || !id) return null;
  const open = _lastDetailState.open_deals || [];
  const closed = _lastDetailState.closed_deals || [];
  return open.find(d => String(d.id) === String(id))
      || closed.find(d => String(d.id) === String(id))
      || null;
}

async function fetchDetail(slug) {
  try {
    const _r = await fetch(`/api/bots/${slug}`);
    if (_r.status === 401) { _handle401(); return; }
    const b = await _r.json();
    _lastDetailState = b;

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
    // Manual deal button — only visible when bot is running AND has
    // no open deals. Mirrors the engine's refusal to open a second deal.
    // The previous `b.open_deals_count || ...` fallback silently flipped
    // to b.open_deals.length when the count was 0, which is fine — but
    // if open_deals_count was missing AND open_deals was undefined the
    // expression yielded 0 regardless, hiding the button when the bot
    // actually was eligible. Compute each input explicitly.
    const mb = $('d-btn-manual-deal');
    if (mb) {
      const running = Boolean(b.running);
      const countField = Number.isFinite(b.open_deals_count) ? b.open_deals_count : null;
      const listLen = Array.isArray(b.open_deals) ? b.open_deals.length : 0;
      const openCnt = countField !== null ? countField : listLen;
      const show = running && openCnt === 0;
      mb.classList.toggle('hidden', !show);
    }

    $('d-price').textContent = fmtPrice(b.current_price) || '—';
    $('d-pair-sub').textContent = b.pair || 'BTC/USD';
    $('d-balance').textContent = b.balance_btc ? b.balance_btc.toFixed(6) : '—';

    const pnl = b.total_pnl_btc || 0;
    $('d-pnl').innerHTML = fmtPnl(pnl, 8);
    $('d-open-count').textContent = b.open_deals_count ?? '—';
    $('d-winrate').textContent = (b.win_rate ?? 0) + '%';
    if (b.has_trading_windows === false) {
      $('d-schedule').textContent = '24/7';
      $('d-schedule').className = 'card-value pos';
    } else {
      $('d-schedule').textContent = b.schedule_open ? 'Open' : 'Closed';
      $('d-schedule').className = 'card-value ' + (b.schedule_open ? 'pos' : 'neu');
    }

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

    renderDetailOpenDeals(b.open_deals || []);

    const cd = b.closed_deals || [];
    renderDetailClosedDeals(cd);

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
    // Boot the wizard chart before the first recompute so the initial
    // render paints overlays + sub-panes for any indicators already in
    // the loaded config. Without this the chart was silently empty in
    // edit mode and RSI/MACD sub-panes never appeared.
    initWizardChart();
    fetchWizardChartData();
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
    use_wick_simulation:  b.use_wick_simulation != null ? Boolean(b.use_wick_simulation) : d.use_wick_simulation,
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
  // Auth via the session cookie only — the backend dropped the legacy
  // ?api_key= query-string fallback because query strings leak into
  // proxy/access logs and browser history. Same-origin WS upgrades
  // automatically forward cookies, so the cookie does the work.
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/logs/${slug}`);
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

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  slugs.forEach(slug => {
    const w = new WebSocket(`${proto}//${location.host}/ws/logs/${slug}`);
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
    await fetch('/api/portal/restart', { method: 'POST' });
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

// ── Live candlestick chart ───────────────────────────────────────────────────
// Lightweight Charts v4 wrapper. The chart tab in the bot detail view shows
// live candles, indicator overlays derived from the bot's configured
// indicators, and (when running) deal entry/TP/SL/DCA price lines. The
// wizard preview is a simpler standalone candlestick chart. Both gracefully
// degrade if window.LightweightCharts is undefined (CDN blocked).

let _chartMain = null, _chartRsi = null, _chartMacd = null;
let _chartCandles = null;
let _chartSeries = {};
let _chartTimeframe = '1h';
let _chartPair = 'BTC/USD';
let _chartRefreshTimer = null;
let _chartBotConfig = null;
let _chartLastDetail = null;
let _chartResizeObs = null;
// Feature: deal timeline markers. When the user clicks a deal row we
// store the target deal here, drive the chart timeframe off its duration
// and re-apply entry/TP/SL price lines + order markers on every refresh
// until the Clear button is pressed.
let _chartPendingDeal = null;
let _chartDealMarkers = [];
let _chartDealPriceLines = [];
// Indicator-driven markers (parabolic SAR + market structure) live here
// so _setCombinedMarkers() can merge them with deal markers in a single
// setMarkers() call — Lightweight Charts replaces the full array on each
// invocation, so any overlay that forgets to merge wipes the other.
let _chartIndicatorMarkers = [];
// Annotations toolbar state. Lightweight Charts v4.1.1 in the standalone
// build does not expose a stable ISeriesPrimitive, so every "drawing" is
// approximated with createPriceLine + series markers — enough to make
// the annotation visible and round-trip it to the DB, without chasing
// private APIs that may change between patch versions.
let _chartActiveTool = 'select';
let _toolFirstPoint = null;
let _measureLines = [];
let _chartAnnotations = [];
// SVG overlay element used to draw text + arrow annotations on top of
// the chart. Lightweight Charts 4.1.1 standalone has no public
// primitive API, and createPriceLine renders a thin horizontal line
// that effectively looks invisible to users. The overlay is an
// `<svg pointer-events:none>` child of #chart-main, redrawn on every
// pan / zoom / resize / annotation mutation.
let _chartAnnotSvg = null;
const _ANNOT_SVG_NS = 'http://www.w3.org/2000/svg';
let _wizardChart = null;
let _wizardCandles = null;
let _wizardRefreshTimer = null;
let _wizardResizeObs = null;
let _wizardTimeframe = '1h';
let _wizardTfHandler = null;
// Cache the most recent candle array so nbRecompute can re-draw indicator
// overlays without re-fetching from the API on every keystroke.
let _wizardCandleCache = null;
// Track overlay series + price lines so we can clear them before each
// re-render. createPriceLine returns a handle that survives setData;
// addLineSeries returns a series we have to removeSeries() ourselves.
let _wizardOverlaySeries = [];
let _wizardOverlayPriceLines = [];
// Sub-charts for RSI / MACD indicators in the wizard preview. Created
// lazily when the user adds the corresponding indicator and destroyed
// when they remove it, so a wizard with no RSI/MACD costs nothing.
let _wizardChartRsi = null;
let _wizardChartMacd = null;
let _wizardSubSeries = {};

function _cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch (e) { return fallback; }
}

function getChartColors() {
  // Resolve the live palette from the active theme. Reads CSS vars so
  // a future palette tweak in style.css automatically flows through to
  // the chart without code changes. The dataset.theme attribute is set
  // by applyPersistedSettings() / _settingsApplyTheme().
  const isDark = document.documentElement.dataset.theme !== 'light';
  return {
    background: _cssVar('--bg',     isDark ? '#0a0e14' : '#f0f2f5'),
    textColor:  _cssVar('--muted',  isDark ? '#4a5568' : '#8a94a6'),
    gridColor:  _cssVar('--border', isDark ? '#1e2736' : '#dde1e9'),
    upColor:    _cssVar('--accent', '#00d4aa'),
    downColor:  _cssVar('--red',    '#ff4d6d'),
  };
}

function _chartLayoutOpts() {
  const c = getChartColors();
  return {
    layout: {
      background: { type: 'solid', color: c.background },
      textColor:  c.textColor,
    },
    grid: {
      vertLines: { color: c.gridColor },
      horzLines: { color: c.gridColor },
    },
    timeScale: { timeVisible: true, secondsVisible: false },
    rightPriceScale: { borderColor: c.gridColor },
    crosshair: { mode: 0 },
  };
}

function _applyChartTheme() {
  // Re-apply the layout options to every live chart instance after a
  // theme switch so the operator sees the new palette without having
  // to reopen the tab. No-ops cleanly when a chart isn't mounted.
  const opts = _chartLayoutOpts();
  for (const chart of [_chartMain, _chartRsi, _chartMacd, _wizardChart, _wizardChartRsi, _wizardChartMacd]) {
    if (!chart) continue;
    try { chart.applyOptions(opts); } catch (e) {}
  }
  // Candlestick series colours live on the series, not the chart, so
  // applyOptions on the chart alone leaves the bodies + wicks at their
  // creation-time colours. Push the up/down palette onto every live
  // candle series too.
  const c = getChartColors();
  const seriesOpts = {
    upColor:        c.upColor,
    downColor:      c.downColor,
    borderUpColor:  c.upColor,
    borderDownColor:c.downColor,
    wickUpColor:    c.upColor,
    wickDownColor:  c.downColor,
  };
  for (const series of [_chartCandles, _wizardCandles]) {
    if (!series) continue;
    try { series.applyOptions(seriesOpts); } catch (e) {}
  }
}

function _chartLibAvailable() { return typeof window.LightweightCharts !== 'undefined'; }

function updateChartTfButtons() {
  document.querySelectorAll('.chart-tf-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tf === _chartTimeframe);
  });
}

function _normalizePair(p) {
  if (!p) return 'BTC/USD';
  return p.indexOf('/') >= 0 ? p : (p.endsWith('USDT') ? p.slice(0, -4) + '/USDT'
        : (p.endsWith('USD') ? p.slice(0, -3) + '/USD' : p));
}

// FastAPI path parameters cannot contain a URL-encoded '/' (%2F) — the
// router still splits on it and a pair like "BTC/USD" breaks the route.
// We send the slash-less form ("BTCUSD") and let the backend's
// _normalize_chart_pair() re-insert the slash server-side.
function _pairForUrl(p) {
  return (p || 'BTCUSD').replace('/', '');
}

// Hover prefetch: when the cursor lingers on the Chart tab button we
// fire-and-forget a /api/chart fetch to warm the backend's per-
// timeframe cache. By the time the user actually clicks the tab the
// real fetchChartData() call hits the hot cache and returns instantly,
// so the chart appears with no perceptible network delay. 200ms
// debounce avoids hammering the exchange on cursor flicks.
let _chartPrefetchTimer = null;
let _chartPrefetchInFlight = false;

async function _prefetchChartForCurrentBot() {
  if (_chartPrefetchInFlight || !currentSlug) return;
  // Pull the bot's pair + timeframe from its config so we warm the
  // cache key the chart tab is actually going to ask for. Falls back
  // to BTCUSD/1h if the config isn't loaded yet.
  let pair = 'BTCUSD';
  let tf = '1h';
  try {
    if (_chartBotConfig && _chartBotConfig.bot) {
      pair = _normalizePair(_chartBotConfig.bot.pair || 'BTC/USD');
      tf = _chartBotConfig.bot.timeframe || '1h';
    } else {
      const r = await fetch(`/api/bots/${currentSlug}/config`);
      if (r.ok) {
        const cfg = await r.json();
        if (cfg && cfg.bot) {
          pair = _normalizePair(cfg.bot.pair || 'BTC/USD');
          tf = cfg.bot.timeframe || '1h';
        }
      }
    }
  } catch (e) { /* keep defaults */ }
  _chartPrefetchInFlight = true;
  try {
    await fetch(`/api/chart/${_pairForUrl(pair)}/${tf}?limit=200`);
  } catch (e) { /* warm-only fetch — backend cache catches the result */ }
  finally {
    _chartPrefetchInFlight = false;
  }
}

function _scheduleChartPrefetch() {
  if (_chartPrefetchTimer) clearTimeout(_chartPrefetchTimer);
  _chartPrefetchTimer = setTimeout(() => {
    _chartPrefetchTimer = null;
    _prefetchChartForCurrentBot();
  }, 200);
}

function _cancelChartPrefetch() {
  if (_chartPrefetchTimer) {
    clearTimeout(_chartPrefetchTimer);
    _chartPrefetchTimer = null;
  }
}

async function loadChartTab(slug) {
  teardownChartTab();
  const fb = $('chart-fallback');
  if (!_chartLibAvailable()) {
    if (fb) fb.classList.remove('hidden');
    return;
  }
  if (fb) fb.classList.add('hidden');
  // Show skeleton immediately so the user sees something is happening
  // before the OHLCV fetch lands. Hidden inside fetchChartData() the
  // moment the candle series gets its first setData() call.
  const sk = $('chart-skeleton');
  if (sk) sk.classList.remove('chart-skeleton-hidden');

  try {
    const r = await fetch(`/api/bots/${slug}/config`);
    if (r.ok) _chartBotConfig = await r.json();
  } catch (e) { _chartBotConfig = null; }

  const inner = (_chartBotConfig && _chartBotConfig.bot) || {};
  // A pending deal (set by showDealOnChart before showDTab navigated here)
  // dictates the timeframe — otherwise use the bot's configured default.
  _chartTimeframe = _chartPendingDeal
    ? _timeframeForDeal(_chartPendingDeal)
    : (inner.timeframe || '1h');
  _chartPair = _normalizePair(inner.pair || 'BTC/USD');
  updateChartTfButtons();
  initCharts();
  await fetchChartData();
  await _loadAnnotations();
  _chartRefreshTimer = setInterval(fetchChartData, 30000);
}

function teardownChartTab() {
  if (_chartRefreshTimer) { clearInterval(_chartRefreshTimer); _chartRefreshTimer = null; }
  if (_chartResizeObs) { try { _chartResizeObs.disconnect(); } catch (e) {} _chartResizeObs = null; }
  try { if (_chartMain) _chartMain.remove(); } catch (e) {}
  try { if (_chartRsi)  _chartRsi.remove();  } catch (e) {}
  try { if (_chartMacd) _chartMacd.remove(); } catch (e) {}
  _chartMain = _chartRsi = _chartMacd = null;
  _chartCandles = null;
  _chartSeries = {};
  // The candle series owned these price-line + marker handles; dropping
  // the refs is enough — they die with the chart instance. _chartPendingDeal
  // deliberately survives so showDealOnChart → showDTab → loadChartTab →
  // teardown → init can still re-apply the deal overlay after init.
  _chartDealPriceLines = [];
  _chartDealMarkers = [];
  _chartIndicatorMarkers = [];
  // Annotations toolbar teardown — leaving the chart tab and coming back
  // must start clean. Handles are owned by the destroyed series, so just
  // drop refs.
  _toolFirstPoint = null;
  _chartActiveTool = 'select';
  _measureLines = [];
  _chartAnnotations = [];
  if (_chartAnnotSvg && _chartAnnotSvg.parentNode) {
    try { _chartAnnotSvg.parentNode.removeChild(_chartAnnotSvg); } catch (e) {}
  }
  _chartAnnotSvg = null;
  document.querySelectorAll('.chart-tool').forEach(b => {
    b.classList.toggle('active', b.dataset.tool === 'select');
  });
  const r = $('chart-rsi'); if (r) r.classList.add('hidden');
  const m = $('chart-macd'); if (m) m.classList.add('hidden');
}

function _indicatorsConfigured() {
  const inner = (_chartBotConfig && _chartBotConfig.bot) || {};
  const entry = inner.entry || {};
  const inds = entry.indicators || [];
  return Array.isArray(inds) ? inds : [];
}

function _hasIndicator(type) {
  return _indicatorsConfigured().some(i => (i.type || '').toUpperCase() === type);
}

function _findIndicator(type) {
  return _indicatorsConfigured().find(i => (i.type || '').toUpperCase() === type) || null;
}

function initCharts() {
  if (!_chartLibAvailable()) return;
  const mainEl = $('chart-main');
  if (!mainEl) return;
  const opts = _chartLayoutOpts();
  _chartMain = LightweightCharts.createChart(mainEl, {
    ...opts,
    width:  mainEl.clientWidth,
    height: mainEl.clientHeight || 500,
  });
  _chartCandles = _chartMain.addCandlestickSeries({
    upColor:        _cssVar('--accent', '#26a69a'),
    downColor:      _cssVar('--red',    '#ef5350'),
    borderUpColor:  _cssVar('--accent', '#26a69a'),
    borderDownColor:_cssVar('--red',    '#ef5350'),
    wickUpColor:    _cssVar('--accent', '#26a69a'),
    wickDownColor:  _cssVar('--red',    '#ef5350'),
  });

  // EMA_CROSS overlay
  if (_hasIndicator('EMA_CROSS')) {
    _chartSeries.emaFast = _chartMain.addLineSeries({ color: _cssVar('--blue', '#5b8dee'), lineWidth: 1 });
    _chartSeries.emaSlow = _chartMain.addLineSeries({ color: _cssVar('--amber', '#ffb347'), lineWidth: 1 });
  }
  if (_hasIndicator('BOLLINGER')) {
    _chartSeries.bbUpper  = _chartMain.addLineSeries({ color: _cssVar('--blue', '#5b8dee'), lineWidth: 1 });
    _chartSeries.bbMiddle = _chartMain.addLineSeries({ color: _cssVar('--muted', '#888'),   lineWidth: 1 });
    _chartSeries.bbLower  = _chartMain.addLineSeries({ color: _cssVar('--blue', '#5b8dee'), lineWidth: 1 });
  }
  if (_hasIndicator('SUPERTREND')) {
    _chartSeries.stBull = _chartMain.addLineSeries({ color: _cssVar('--accent', '#26a69a'), lineWidth: 2 });
    _chartSeries.stBear = _chartMain.addLineSeries({ color: _cssVar('--red',    '#ef5350'), lineWidth: 2 });
  }

  // RSI sub-chart
  if (_hasIndicator('RSI')) {
    const rsiEl = $('chart-rsi');
    rsiEl.classList.remove('hidden');
    _chartRsi = LightweightCharts.createChart(rsiEl, {
      ..._chartLayoutOpts(),
      width:  rsiEl.clientWidth,
      height: rsiEl.clientHeight || 100,
    });
    _chartSeries.rsi = _chartRsi.addLineSeries({ color: _cssVar('--blue', '#5b8dee'), lineWidth: 1 });
    _chartSeries.rsi.createPriceLine({ price: 70, color: _cssVar('--red',  '#ef5350'), lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: '70' });
    _chartSeries.rsi.createPriceLine({ price: 30, color: _cssVar('--accent','#26a69a'), lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: '30' });
  }
  if (_hasIndicator('MACD')) {
    const macdEl = $('chart-macd');
    macdEl.classList.remove('hidden');
    _chartMacd = LightweightCharts.createChart(macdEl, {
      ..._chartLayoutOpts(),
      width:  macdEl.clientWidth,
      height: macdEl.clientHeight || 100,
    });
    _chartSeries.macdHist   = _chartMacd.addHistogramSeries({ color: _cssVar('--muted', '#888') });
    _chartSeries.macdLine   = _chartMacd.addLineSeries({ color: _cssVar('--blue',  '#5b8dee'), lineWidth: 1 });
    _chartSeries.macdSignal = _chartMacd.addLineSeries({ color: _cssVar('--amber', '#ffb347'), lineWidth: 1 });
  }

  // Resize handling
  if (typeof ResizeObserver !== 'undefined') {
    _chartResizeObs = new ResizeObserver(entries => {
      for (const e of entries) {
        const w = e.contentRect.width;
        if (e.target === mainEl && _chartMain) {
          _chartMain.applyOptions({ width: w });
          _renderAnnotations();
        }
        if (_chartRsi  && e.target === $('chart-rsi'))  _chartRsi.applyOptions({ width: w });
        if (_chartMacd && e.target === $('chart-macd')) _chartMacd.applyOptions({ width: w });
      }
    });
    _chartResizeObs.observe(mainEl);
    if (_chartRsi)  _chartResizeObs.observe($('chart-rsi'));
    if (_chartMacd) _chartResizeObs.observe($('chart-macd'));
  }

  // Redraw the SVG annotation overlay whenever the user pans or zooms
  // the chart — without this, existing annotations would stick to
  // stale pixel positions until the next full fetch.
  try {
    _chartMain.timeScale().subscribeVisibleLogicalRangeChange(_renderAnnotations);
  } catch (e) {}

  _installChartToolHandlers();
}

async function fetchChartData() {
  if (!_chartCandles) return;
  let candles;
  try {
    const r = await fetch(`/api/chart/${_pairForUrl(_chartPair)}/${_chartTimeframe}?limit=200`);
    if (!r.ok) return;
    candles = await r.json();
  } catch (e) { return; }
  if (!Array.isArray(candles) || !candles.length) return;

  _chartCandles.setData(candles);
  // First candles in — drop the skeleton placeholder.
  const sk = $('chart-skeleton');
  if (sk) sk.classList.add('chart-skeleton-hidden');

  // Price + change
  const first = candles[0].close;
  const last  = candles[candles.length - 1].close;
  const pct   = first ? ((last - first) / first) * 100 : 0;
  const pe = $('chart-price'); if (pe) pe.textContent = fmtPrice(last);
  const ce = $('chart-change');
  if (ce) {
    const sign = pct >= 0 ? '+' : '';
    ce.textContent = `${sign}${pct.toFixed(2)}%`;
    ce.classList.remove('pos', 'neg');
    ce.classList.add(pct >= 0 ? 'pos' : 'neg');
  }

  _renderIndicatorOverlays(candles);
  await _renderDealOverlays();
  await _applyPendingDealOverlay();
}

function _renderIndicatorOverlays(candles) {
  // EMA_CROSS
  const emaCfg = _findIndicator('EMA_CROSS');
  if (emaCfg && _chartSeries.emaFast) {
    const fast = emaCfg.fast || 9;
    const slow = emaCfg.slow || 21;
    _chartSeries.emaFast.setData(calcEMALine(candles, fast));
    _chartSeries.emaSlow.setData(calcEMALine(candles, slow));
  }
  // BOLLINGER
  const bbCfg = _findIndicator('BOLLINGER');
  if (bbCfg && _chartSeries.bbUpper) {
    const period = bbCfg.period || 20;
    const mult   = bbCfg.multiplier || 2.0;
    const bb = calcBollingerLines(candles, period, mult);
    _chartSeries.bbUpper.setData(bb.upper);
    _chartSeries.bbMiddle.setData(bb.middle);
    _chartSeries.bbLower.setData(bb.lower);
  }
  // SUPERTREND
  const stCfg = _findIndicator('SUPERTREND');
  if (stCfg && _chartSeries.stBull) {
    const atr = stCfg.atr_period || 10;
    const mult = stCfg.multiplier || 3.0;
    const st = calcSupertrendLines(candles, atr, mult);
    _chartSeries.stBull.setData(st.bull);
    _chartSeries.stBear.setData(st.bear);
  }
  // RSI
  const rsiCfg = _findIndicator('RSI');
  if (rsiCfg && _chartSeries.rsi) {
    const period = rsiCfg.period || 14;
    _chartSeries.rsi.setData(calcRSILine(candles, period));
  }
  // MACD
  const macdCfg = _findIndicator('MACD');
  if (macdCfg && _chartSeries.macdHist) {
    const fast   = macdCfg.fast   || 12;
    const slow   = macdCfg.slow   || 26;
    const signal = macdCfg.signal || 9;
    const m = calcMACDLines(candles, fast, slow, signal);
    _chartSeries.macdLine.setData(m.macd);
    _chartSeries.macdSignal.setData(m.signal);
    _chartSeries.macdHist.setData(m.histogram);
  }
  // SUPPORT_RESISTANCE — static price lines
  const srCfg = _findIndicator('SUPPORT_RESISTANCE');
  if (srCfg) {
    const lookback = srCfg.lookback || 3;
    const tol = srCfg.tolerance_pct || 0.5;
    const closes = candles.map(c => c.close);
    const sr = calcSR(closes, lookback, tol);
    sr.support.slice(-3).forEach(lvl => {
      _chartCandles.createPriceLine({ price: lvl, color: _cssVar('--accent', '#26a69a'), lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: 'S' });
    });
    sr.resistance.slice(-3).forEach(lvl => {
      _chartCandles.createPriceLine({ price: lvl, color: _cssVar('--red', '#ef5350'), lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: 'R' });
    });
  }
  // QFL — base price lines
  const qflCfg = _findIndicator('QFL');
  if (qflCfg) {
    const lookback = qflCfg.lookback || 3;
    const crack    = qflCfg.crack_pct || 3.0;
    const baseN    = qflCfg.base_candles || 5;
    const maxBases = qflCfg.max_bases || 5;
    const closes = candles.map(c => c.close);
    const bases  = calcQFL(closes, lookback, crack, baseN, maxBases);
    bases.forEach(b => {
      _chartCandles.createPriceLine({ price: b, color: _cssVar('--blue', '#5b8dee'), lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: 'QFL' });
    });
  }
  // Markers — Parabolic SAR + Market Structure. Stash in module var so
  // _setCombinedMarkers() can merge indicator markers with deal timeline
  // markers (setMarkers replaces the array on every call).
  const markers = [];
  const psCfg = _findIndicator('PARABOLIC_SAR');
  if (psCfg) {
    const ps = calcParabolicSARMarkers(candles, psCfg.initial_af || 0.02, psCfg.max_af || 0.20);
    for (const p of ps) markers.push(p);
  }
  const msCfg = _findIndicator('MARKET_STRUCTURE');
  if (msCfg) {
    const ms = calcMarketStructureMarkers(candles, msCfg.lookback || 3);
    for (const p of ms) markers.push(p);
  }
  _chartIndicatorMarkers = markers;
  _setCombinedMarkers();
}

function _setCombinedMarkers() {
  if (!_chartCandles) return;
  const combined = _chartIndicatorMarkers.concat(_chartDealMarkers);
  combined.sort((a, b) => a.time - b.time);
  try { _chartCandles.setMarkers(combined); } catch (e) {}
}

function _clearDealMarkers() {
  if (_chartCandles && _chartDealPriceLines.length) {
    for (const pl of _chartDealPriceLines) {
      try { _chartCandles.removePriceLine(pl); } catch (e) {}
    }
  }
  _chartDealPriceLines = [];
  _chartDealMarkers = [];
  _setCombinedMarkers();
}

function _dealDurationSeconds(deal) {
  if (!deal || !deal.opened_at) return 0;
  const start = new Date(deal.opened_at).getTime();
  const end = deal.closed_at ? new Date(deal.closed_at).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return 0;
  return Math.floor((end - start) / 1000);
}

function _timeframeForDeal(deal) {
  const s = _dealDurationSeconds(deal);
  if (s < 4 * 3600) return '15m';
  if (s < 24 * 3600) return '1h';
  return '4h';
}

function _isoToUnix(iso) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return null;
  return Math.floor(t / 1000);
}

async function _applyPendingDealOverlay() {
  // Called from fetchChartData() after the candle + indicator overlays
  // finish rendering. Re-applies the deal timeline markers on every
  // auto-refresh as long as _chartPendingDeal stays set.
  if (!_chartPendingDeal || !_chartCandles) return;
  const deal = _chartPendingDeal;

  _clearDealMarkers();

  // Price lines: entry + TP + SL using the bot's configured percentages.
  const inner = (_chartBotConfig && _chartBotConfig.bot) || {};
  const tpPct = Number((inner.take_profit || {}).target_pct) || 0;
  const slPct = Number((inner.stop_loss || {}).pct) || 0;
  const avg = Number(deal.avg_entry_price) || Number(deal.entry_price) || 0;
  const blue   = _cssVar('--blue',   '#5b8dee');
  const accent = _cssVar('--accent', '#26a69a');
  const red    = _cssVar('--red',    '#ef5350');
  const muted  = _cssVar('--muted',  '#888');

  if (avg > 0) {
    try {
      _chartDealPriceLines.push(_chartCandles.createPriceLine({
        price: avg, color: blue, lineStyle: 0, lineWidth: 2,
        axisLabelVisible: true, title: `Entry ${avg.toFixed(2)}`,
      }));
    } catch (e) {}
    if (tpPct > 0) {
      const tp = avg * (1 + tpPct / 100);
      try {
        _chartDealPriceLines.push(_chartCandles.createPriceLine({
          price: tp, color: accent, lineStyle: 2, lineWidth: 2,
          axisLabelVisible: true, title: `TP ${tp.toFixed(2)}`,
        }));
      } catch (e) {}
    }
    if (slPct > 0) {
      const sl = avg * (1 - slPct / 100);
      try {
        _chartDealPriceLines.push(_chartCandles.createPriceLine({
          price: sl, color: red, lineStyle: 2, lineWidth: 2,
          axisLabelVisible: true, title: `SL ${sl.toFixed(2)}`,
        }));
      } catch (e) {}
    }
  }

  // Vertical "lines" are not a thing in Lightweight Charts 4.1.1 — the
  // standard substitute is a series marker at the relevant time with a
  // circle/arrow shape. Fetch the order rows for the deal and build one
  // marker per fill, plus a close marker if the deal closed.
  let orders = [];
  try {
    const r = await fetch(`/api/db/deals/${encodeURIComponent(deal.id)}/orders`);
    if (r.ok) orders = await r.json();
  } catch (e) { orders = []; }

  const markers = [];
  for (const o of (orders || [])) {
    const t = _isoToUnix(o.placed_at);
    if (t == null) continue;
    const isBase = (o.order_type === 'base') || Number(o.order_number) === 1;
    markers.push({
      time: t,
      position: 'belowBar',
      color: isBase ? blue : '#ff9500',
      shape: 'circle',
      text: isBase ? 'BASE' : 'DCA',
    });
  }
  if (deal.closed_at && deal.close_reason) {
    const t = _isoToUnix(deal.closed_at);
    if (t != null) {
      const reason = String(deal.close_reason).toLowerCase();
      let color = muted;
      if (reason === 'tp') color = accent;
      else if (reason === 'sl') color = red;
      markers.push({
        time: t,
        position: 'aboveBar',
        color,
        shape: 'arrowDown',
        text: reason.toUpperCase(),
      });
    }
  }
  _chartDealMarkers = markers;
  _setCombinedMarkers();

  const btn = $('chart-clear-deal');
  if (btn) btn.classList.remove('hidden');
}

async function showDealOnChart(deal) {
  if (!deal) return;
  _chartPendingDeal = deal;
  _chartTimeframe = _timeframeForDeal(deal);
  updateChartTfButtons();
  const chartTab = $('dtab-chart');
  const isChartActive = chartTab && !chartTab.classList.contains('hidden');
  if (!isChartActive) {
    const btn = document.querySelector('.detail-subnav .tab[data-dtab="chart"]');
    showDTab('chart', btn);
  } else {
    await fetchChartData();
  }
}

function clearDealFromChart() {
  _chartPendingDeal = null;
  _clearDealMarkers();
  const btn = $('chart-clear-deal');
  if (btn) btn.classList.add('hidden');
  fetchChartData();
}

// ── Chart annotations (measure / arrow / text / delete) ─────────────────────
// Lightweight Charts 4.1.1 standalone has no stable primitive API, so every
// shape below is approximated with createPriceLine handles and/or series
// markers — enough to make the annotation visible and persist its x/y to
// the DB via /api/db/annotations. The "arrow" is two horizontal price
// lines at y1/y2 with title "→"; "text" is a single price line at y1 with
// the label as its title. Not pixel-perfect, but CSP-safe and zero-deps.

function _setActiveTool(name) {
  _chartActiveTool = name || 'select';
  _toolFirstPoint = null;
  document.querySelectorAll('.chart-tool').forEach(b => {
    b.classList.toggle('active', b.dataset.tool === _chartActiveTool);
  });
  if (_chartActiveTool !== 'measure') _clearMeasureLines();
}

// Transient measure-tool render lives entirely on the SVG overlay.
// Cleared whenever the user switches tools or fires a new measure.
let _measureSession = null;

function _clearMeasureLines() {
  // Drop any legacy createPriceLine handles still hanging around from
  // earlier builds, then null the new SVG-based session and trigger a
  // re-render so the overlay refreshes without the measure shapes.
  if (_chartCandles && _measureLines.length) {
    for (const pl of _measureLines) {
      try { _chartCandles.removePriceLine(pl); } catch (e) {}
    }
  }
  _measureLines = [];
  _measureSession = null;
  _renderAnnotations();
}

function _finishTwoPointTool(tool, p1, p2) {
  if (tool === 'measure') {
    const priceDiff = p2.price - p1.price;
    const pct = p1.price ? (priceDiff / p1.price) * 100 : 0;
    _measureSession = { p1, p2, priceDiff, pct };
    _renderAnnotations();
    return;
  }
  if (tool === 'arrow') {
    _persistAnnotation({
      type: 'arrow',
      x1: p1.time, y1: p1.price,
      x2: p2.time, y2: p2.price,
      color: _cssVar('--blue', '#5b8dee'),
    });
  }
}

function _promptAnnotationText(point) {
  const label = window.prompt('Label?');
  if (!label) return;
  _persistAnnotation({
    type: 'text',
    x1: point.time, y1: point.price,
    label,
    color: _cssVar('--amber', '#ffb347'),
  });
}

async function _persistAnnotation(fields) {
  if (!currentSlug) return;
  // timeframe is a required field on AnnotationBody — the SPA always
  // has _chartTimeframe set by the time the tool fires, but fall
  // through to '1h' just in case a race empties it.
  const body = Object.assign({
    bot_slug: currentSlug,
    timeframe: _chartTimeframe || '1h',
  }, fields);
  let r;
  try {
    r = await fetch('/api/db/annotations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) { return; }
  if (r.status === 401) { _handle401(); return; }
  if (!r.ok) return;
  // Optimistic update: splice the new annotation into the local
  // cache so the SVG overlay updates immediately without waiting
  // for a round-trip to the GET list endpoint.
  try {
    const j = await r.json();
    if (j && Number.isFinite(j.id)) {
      _chartAnnotations.push(Object.assign({}, body, { id: j.id }));
      _renderAnnotations();
      return;
    }
  } catch (e) {}
  // Fallback: full reload if the POST response was malformed.
  await _loadAnnotations();
}

async function _loadAnnotations() {
  _chartAnnotations = [];
  if (!currentSlug) { _renderAnnotations(); return; }
  const tf = _chartTimeframe || '1h';
  try {
    const r = await fetch(
      `/api/db/annotations?bot_slug=${encodeURIComponent(currentSlug)}&timeframe=${encodeURIComponent(tf)}`,
      { credentials: 'same-origin' }
    );
    if (r.status === 401) {
      // Session expired — kick the SPA back to the login view rather
      // than silently showing an empty annotation list.
      _handle401();
      return;
    }
    if (!r.ok) {
      _renderAnnotations();
      return;
    }
    const body = await r.json();
    if (Array.isArray(body)) _chartAnnotations = body;
  } catch (e) { /* keep empty */ }
  _renderAnnotations();
}

function _svgEl(name, attrs) {
  const el = document.createElementNS(_ANNOT_SVG_NS, name);
  if (attrs) {
    for (const k of Object.keys(attrs)) el.setAttribute(k, String(attrs[k]));
  }
  return el;
}

function _ensureAnnotationSvg() {
  const host = document.getElementById('chart-main');
  if (!host || !_chartMain) return null;
  if (_chartAnnotSvg && _chartAnnotSvg.parentNode === host) return _chartAnnotSvg;
  // Lightweight Charts paints onto canvases inside #chart-main as
  // position:absolute siblings. Two siblings with position:absolute and
  // z-index:auto draw in DOM order, but LC may re-mount canvases on
  // resize and end up above a previously-appended SVG. Force z-index:10
  // and inset:0 so the overlay is unambiguously on top of every LC
  // canvas. pointer-events:none keeps the chart click-through working.
  if (!host.style.position) host.style.position = 'relative';
  const svg = _svgEl('svg', {
    class: 'chart-annot-svg',
    xmlns: _ANNOT_SVG_NS,
  });
  svg.style.position = 'absolute';
  svg.style.inset = '0';
  svg.style.width = '100%';
  svg.style.height = '100%';
  svg.style.pointerEvents = 'none';
  svg.style.zIndex = '10';
  svg.style.display = 'block';
  host.appendChild(svg);
  _chartAnnotSvg = svg;
  return svg;
}

function _chartXOfTime(t) {
  if (!_chartMain || t == null) return null;
  try {
    const x = _chartMain.timeScale().timeToCoordinate(Number(t));
    return Number.isFinite(x) ? x : null;
  } catch (e) { return null; }
}
function _chartYOfPrice(p) {
  if (!_chartCandles || p == null) return null;
  try {
    const y = _chartCandles.priceToCoordinate(Number(p));
    return Number.isFinite(y) ? y : null;
  } catch (e) { return null; }
}

function _renderAnnotations() {
  const svg = _ensureAnnotationSvg();
  if (!svg) return;
  // Size the SVG viewBox to match the chart container so pixel
  // coordinates from timeToCoordinate/priceToCoordinate land correctly.
  const host = document.getElementById('chart-main');
  if (host) {
    const w = host.clientWidth, h = host.clientHeight;
    svg.setAttribute('width', String(w));
    svg.setAttribute('height', String(h));
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  }
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  const blue   = _cssVar('--blue',   '#5b8dee');
  const amber  = _cssVar('--amber',  '#ffb347');
  const accent = _cssVar('--accent', '#26a69a');
  const muted  = _cssVar('--muted',  '#888');

  for (const a of (_chartAnnotations || [])) {
    const color = a.color || (a.type === 'text' ? amber : blue);
    const x1 = _chartXOfTime(a.x1);
    const y1 = _chartYOfPrice(a.y1);
    if (x1 == null || y1 == null) continue;

    if (a.type === 'text') {
      const g = _svgEl('g', { 'data-ann-id': a.id });
      g.appendChild(_svgEl('circle', {
        cx: x1, cy: y1, r: 4, fill: color, stroke: '#ffffff', 'stroke-width': 1,
      }));
      const text = _svgEl('text', {
        x: x1 + 8, y: y1 + 4, fill: color,
        'font-family': 'monospace', 'font-size': 11,
      });
      text.textContent = a.label || 'text';
      g.appendChild(text);
      svg.appendChild(g);
    } else if (a.type === 'arrow') {
      const x2 = _chartXOfTime(a.x2);
      const y2 = _chartYOfPrice(a.y2);
      if (x2 == null || y2 == null) continue;
      const g = _svgEl('g', { 'data-ann-id': a.id });
      g.appendChild(_svgEl('line', {
        x1, y1, x2, y2, stroke: color, 'stroke-width': 2,
      }));
      const dx = x2 - x1, dy = y2 - y1;
      const len = Math.sqrt(dx * dx + dy * dy);
      if (len > 0.01) {
        const ux = dx / len, uy = dy / len;
        const bx = x2 - ux * 10, by = y2 - uy * 10;
        const px = -uy * 5, py = ux * 5;
        const points = `${x2},${y2} ${bx + px},${by + py} ${bx - px},${by - py}`;
        g.appendChild(_svgEl('polygon', { points, fill: color }));
      }
      svg.appendChild(g);
    }
  }

  // Transient measure session: solid line from A to B with the percent
  // change as a midpoint label. Persists until the user picks a new
  // tool or measures again (then _clearMeasureLines() drops it).
  if (_measureSession) {
    const { p1, p2, priceDiff, pct } = _measureSession;
    const ax = _chartXOfTime(p1.time);
    const ay = _chartYOfPrice(p1.price);
    const bx = _chartXOfTime(p2.time);
    const by = _chartYOfPrice(p2.price);
    if (ax != null && ay != null && bx != null && by != null) {
      const g = _svgEl('g', { 'data-measure': '1' });
      g.appendChild(_svgEl('line', {
        x1: ax, y1: ay, x2: bx, y2: by,
        stroke: accent, 'stroke-width': 2, 'stroke-dasharray': '4 3',
      }));
      g.appendChild(_svgEl('circle', { cx: ax, cy: ay, r: 4, fill: muted }));
      g.appendChild(_svgEl('circle', { cx: bx, cy: by, r: 4, fill: muted }));
      const mx = (ax + bx) / 2, my = (ay + by) / 2;
      const sign = pct >= 0 ? '+' : '';
      const labelText = `${sign}${pct.toFixed(2)}% / ${sign}${priceDiff.toFixed(2)}`;
      // Label background for legibility against candle wicks.
      const tw = labelText.length * 6.5 + 8;
      g.appendChild(_svgEl('rect', {
        x: mx - tw / 2, y: my - 16, width: tw, height: 14,
        fill: 'rgba(0,0,0,0.7)', rx: 2,
      }));
      const text = _svgEl('text', {
        x: mx, y: my - 5, fill: accent,
        'font-family': 'monospace', 'font-size': 11, 'text-anchor': 'middle',
      });
      text.textContent = labelText;
      g.appendChild(text);
      svg.appendChild(g);
    }
  }
}

async function _deleteAnnotationNear(point) {
  if (!_chartAnnotations.length) return;
  // Crude Euclidean nearest-neighbour in (time, price) space — the two
  // axes have wildly different magnitudes, so normalise both against
  // their respective ranges before comparing.
  let tMin = Infinity, tMax = -Infinity, pMin = Infinity, pMax = -Infinity;
  for (const a of _chartAnnotations) {
    if (a.x1 != null) { tMin = Math.min(tMin, a.x1); tMax = Math.max(tMax, a.x1); }
    if (a.x2 != null) { tMin = Math.min(tMin, a.x2); tMax = Math.max(tMax, a.x2); }
    if (a.y1 != null) { pMin = Math.min(pMin, a.y1); pMax = Math.max(pMax, a.y1); }
    if (a.y2 != null) { pMin = Math.min(pMin, a.y2); pMax = Math.max(pMax, a.y2); }
  }
  const tSpan = Math.max(1, tMax - tMin);
  const pSpan = Math.max(1, pMax - pMin);
  let best = null, bestD = Infinity;
  for (const a of _chartAnnotations) {
    const dt = ((Number(a.x1) || point.time) - point.time) / tSpan;
    const dp = ((Number(a.y1) || point.price) - point.price) / pSpan;
    const d = dt * dt + dp * dp;
    if (d < bestD) { bestD = d; best = a; }
  }
  if (!best) return;
  try {
    const r = await fetch(`/api/db/annotations/${best.id}`, { method: 'DELETE' });
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) return;
    _chartAnnotations = _chartAnnotations.filter(a => a.id !== best.id);
    _renderAnnotations();
  } catch (e) {}
}

async function _clearAllAnnotations(slug, timeframe, onCleared) {
  if (!slug) return;
  if (!window.confirm('Delete all annotations for this chart? This cannot be undone.')) return;
  const url = '/api/db/annotations/all'
    + `?bot_slug=${encodeURIComponent(slug)}`
    + (timeframe ? `&timeframe=${encodeURIComponent(timeframe)}` : '');
  try {
    const r = await fetch(url, { method: 'DELETE' });
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) return;
    if (typeof onCleared === 'function') onCleared();
  } catch (e) {}
}

function _installChartToolHandlers() {
  if (!_chartMain || !_chartCandles) return;
  // Lightweight Charts' subscribeClick only populates `param.time` when
  // the cursor is parked exactly on a candle tick — clicks between
  // candles yield undefined and the handler bailed, so roughly half
  // the clicks felt like no-ops. Derive the time and price from the
  // raw point via the time scale's coordinateToTime() and the candle
  // series' coordinateToPrice() instead, which work anywhere inside
  // the chart area. The subscribed callback is still the right place
  // to receive clicks (LC doesn't expose the canvas DOM to outside
  // listeners) — we just stop trusting param.time.
  const ts = _chartMain.timeScale();
  const handle = (param) => {
    if (_chartActiveTool === 'select') return;
    if (!param || !param.point) return;
    const { x, y } = param.point;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    let t = param.time;
    if (t == null) {
      try { t = ts.coordinateToTime(x); } catch (e) { t = null; }
    }
    if (t == null) return;
    const price = _chartCandles.coordinateToPrice(y);
    if (!Number.isFinite(price)) return;
    const point = { time: Number(t), price: Number(price) };
    if (_chartActiveTool === 'text') {
      _promptAnnotationText(point);
      return;
    }
    if (_chartActiveTool === 'measure' || _chartActiveTool === 'arrow') {
      if (!_toolFirstPoint) {
        _toolFirstPoint = point;
      } else {
        _finishTwoPointTool(_chartActiveTool, _toolFirstPoint, point);
        _toolFirstPoint = null;
      }
      return;
    }
    if (_chartActiveTool === 'delete') {
      _deleteAnnotationNear(point);
    }
  };
  try { _chartMain.subscribeClick(handle); } catch (e) {}
}

async function _renderDealOverlays() {
  if (!currentSlug || !_chartCandles) return;
  let bot;
  try {
    bot = await fetch(`/api/bots/${currentSlug}`).then(r => r.json());
  } catch (e) { return; }
  if (!bot || !bot.running) return;
  const open = bot.open_deals || [];
  if (!open.length) return;

  // YAML config keys are take_profit / stop_loss / dca, not tp/sl —
  // the previous overlay code read from the wrong paths so every line
  // except Entry silently rendered at price 0.
  const inner = (_chartBotConfig && _chartBotConfig.bot) || {};
  const tpPct = Number((inner.take_profit || {}).target_pct) || 0;
  const slCfg = inner.stop_loss || {};
  const slPct = Number(slCfg.pct) || 0;
  const slType = slCfg.type || 'fixed';
  const dcaCfg = inner.dca || {};
  const dcaSpacing = Number(dcaCfg.order_spacing_pct) || 0;
  const dcaMax = Number(dcaCfg.max_orders) || 0;

  const blue   = _cssVar('--blue',   '#5b8dee');
  const accent = _cssVar('--accent', '#26a69a');
  const red    = _cssVar('--red',    '#ef5350');
  const amber  = _cssVar('--amber',  '#ffb347');

  for (const d of open) {
    const avg = Number(d.avg_entry_price) || Number(d.entry_price) || 0;
    if (!avg) continue;

    _chartCandles.createPriceLine({
      price: avg, color: blue, lineStyle: 0, lineWidth: 1,
      axisLabelVisible: true, title: 'Entry',
    });

    if (tpPct > 0) {
      _chartCandles.createPriceLine({
        price: avg * (1 + tpPct / 100),
        color: accent, lineStyle: 2, lineWidth: 1,
        axisLabelVisible: true, title: 'TP',
      });
    }

    if (slPct > 0) {
      // Trailing stops anchor to _peak_price (high-water mark) instead of
      // the average entry. The state file serialises it as _peak_price.
      const slAnchor = (slType === 'trailing' && d._peak_price)
        ? Number(d._peak_price) : avg;
      _chartCandles.createPriceLine({
        price: slAnchor * (1 - slPct / 100),
        color: red, lineStyle: 2, lineWidth: 1,
        axisLabelVisible: true, title: 'SL',
      });
    }

    if (dcaSpacing > 0 && dcaMax > 1) {
      // DCA ladders down from the last placed order — on the first tick
      // that's the base order's fill price. The engine uses the MOST
      // recently placed order as the anchor for each next DCA, so we
      // mirror that: starting point is entry_price (base order), next
      // ladder step = last * (1 - spacing/100). Remaining = total slots
      // (max_orders) minus the number already placed (order_count).
      const placed = Number(d.order_count) || 1;
      const remaining = Math.max(0, dcaMax - placed);
      let last = Number(d.entry_price) || avg;
      for (let i = 0; i < remaining; i++) {
        last = last * (1 - dcaSpacing / 100);
        _chartCandles.createPriceLine({
          price: last, color: amber, lineStyle: 2, lineWidth: 1,
          axisLabelVisible: true, title: `DCA${placed + i}`,
        });
      }
    }
  }
}

// ── Wizard preview chart ─────────────────────────────────────────────────────

function initWizardChart() {
  teardownWizardChart();
  if (!_chartLibAvailable()) return;
  const el = $('wizard-chart');
  if (!el) return;
  // Skeleton up while we wait for the first OHLCV fetch to land —
  // hidden again inside fetchWizardChartData on success.
  const sk = $('wizard-chart-skeleton');
  if (sk) sk.classList.remove('chart-skeleton-hidden');
  _wizardChart = LightweightCharts.createChart(el, {
    ..._chartLayoutOpts(),
    width:  el.clientWidth,
    height: el.clientHeight || 250,
  });
  _wizardCandles = _wizardChart.addCandlestickSeries({
    upColor:        _cssVar('--accent', '#26a69a'),
    downColor:      _cssVar('--red',    '#ef5350'),
    borderUpColor:  _cssVar('--accent', '#26a69a'),
    borderDownColor:_cssVar('--red',    '#ef5350'),
    wickUpColor:    _cssVar('--accent', '#26a69a'),
    wickDownColor:  _cssVar('--red',    '#ef5350'),
  });
  if (typeof ResizeObserver !== 'undefined') {
    _wizardResizeObs = new ResizeObserver(entries => {
      for (const e of entries) {
        const w = e.contentRect.width;
        if (e.target === el && _wizardChart) {
          _wizardChart.applyOptions({ width: w });
          _renderWizardAnnotations();
        }
        if (_wizardChartRsi  && e.target === $('wizard-chart-rsi'))  _wizardChartRsi.applyOptions({ width: w });
        if (_wizardChartMacd && e.target === $('wizard-chart-macd')) _wizardChartMacd.applyOptions({ width: w });
      }
    });
    _wizardResizeObs.observe(el);
  }

  // Track timeframe field changes
  const tfSel = $('nb-timeframe');
  if (tfSel) {
    _wizardTimeframe = tfSel.value || '1h';
    _wizardTfHandler = () => {
      _wizardTimeframe = tfSel.value || '1h';
      fetchWizardChartData();
    };
    tfSel.addEventListener('change', _wizardTfHandler);
  }
  if (_wizardRefreshTimer) clearInterval(_wizardRefreshTimer);
  _wizardRefreshTimer = setInterval(fetchWizardChartData, 30000);

  // Annotation tooling for the wizard chart — same SVG-overlay pattern
  // as the detail chart, just under a dedicated "wizard" pseudo-slug.
  _installWizardChartToolHandlers();
  try {
    _wizardChart.timeScale().subscribeVisibleLogicalRangeChange(_renderWizardAnnotations);
  } catch (e) {}
}

function teardownWizardChart() {
  if (_wizardRefreshTimer) { clearInterval(_wizardRefreshTimer); _wizardRefreshTimer = null; }
  if (_wizardResizeObs) { try { _wizardResizeObs.disconnect(); } catch (e) {} _wizardResizeObs = null; }
  const tfSel = $('nb-timeframe');
  if (tfSel && _wizardTfHandler) tfSel.removeEventListener('change', _wizardTfHandler);
  _wizardTfHandler = null;
  _wizardDestroyRsiChart();
  _wizardDestroyMacdChart();
  if (_wizardAnnotSvg && _wizardAnnotSvg.parentNode) {
    try { _wizardAnnotSvg.parentNode.removeChild(_wizardAnnotSvg); } catch (e) {}
  }
  _wizardAnnotSvg = null;
  _wizardAnnotations = [];
  _wizardActiveTool = 'select';
  _wizardToolFirstPoint = null;
  _wizardMeasureSession = null;
  document.querySelectorAll('.chart-tool[data-wtool]').forEach(b => {
    b.classList.toggle('active', b.dataset.wtool === 'select');
  });
  try { if (_wizardChart) _wizardChart.remove(); } catch (e) {}
  _wizardChart = null;
  _wizardCandles = null;
  _wizardCandleCache = null;
  _wizardOverlaySeries = [];
  _wizardOverlayPriceLines = [];
  _wizardSubSeries = {};
}

async function fetchWizardChartData() {
  if (!_wizardCandles) return;
  const pairInput = $('nb-pair');
  const pair = _normalizePair((pairInput && pairInput.value.trim()) || 'BTC/USD');
  const tf   = _wizardTimeframe || '1h';
  let candles;
  try {
    const r = await fetch(`/api/chart/${_pairForUrl(pair)}/${tf}?limit=200`);
    if (!r.ok) return;
    candles = await r.json();
  } catch (e) { return; }
  if (!Array.isArray(candles) || !candles.length) return;
  _wizardCandles.setData(candles);
  _wizardCandleCache = candles;
  const sk = $('wizard-chart-skeleton');
  if (sk) sk.classList.add('chart-skeleton-hidden');
  const lbl = $('wizard-chart-label'); if (lbl) lbl.textContent = pair;
  const pe  = $('wizard-chart-price'); if (pe)  pe.textContent  = fmtPrice(candles[candles.length - 1].close);
  renderWizardOverlays();
  _loadWizardAnnotations();
}

// ── Wizard chart annotations ───────────────────────────────────────────────
// Brand new bots have no slug, so wizard annotations live under a fixed
// "wizard" pseudo-slug. Same backend endpoints, same SVG-overlay
// rendering — the wizard chart just owns its own state + SVG.
const _WIZARD_ANNOT_SLUG = 'wizard';
let _wizardAnnotations = [];
let _wizardActiveTool = 'select';
let _wizardToolFirstPoint = null;
let _wizardMeasureSession = null;
let _wizardAnnotSvg = null;

function _setActiveWizardTool(name) {
  _wizardActiveTool = name || 'select';
  _wizardToolFirstPoint = null;
  document.querySelectorAll('.chart-tool[data-wtool]').forEach(b => {
    b.classList.toggle('active', b.dataset.wtool === _wizardActiveTool);
  });
  if (_wizardActiveTool !== 'measure') {
    _wizardMeasureSession = null;
    _renderWizardAnnotations();
  }
}

function _ensureWizardAnnotSvg() {
  const host = document.getElementById('wizard-chart');
  if (!host || !_wizardChart) return null;
  if (_wizardAnnotSvg && _wizardAnnotSvg.parentNode === host) return _wizardAnnotSvg;
  if (!host.style.position) host.style.position = 'relative';
  const svg = _svgEl('svg', { class: 'chart-annot-svg', xmlns: _ANNOT_SVG_NS });
  svg.style.position = 'absolute';
  svg.style.inset = '0';
  svg.style.width = '100%';
  svg.style.height = '100%';
  svg.style.pointerEvents = 'none';
  svg.style.zIndex = '10';
  svg.style.display = 'block';
  host.appendChild(svg);
  _wizardAnnotSvg = svg;
  return svg;
}

function _wizardXOfTime(t) {
  if (!_wizardChart || t == null) return null;
  try {
    const x = _wizardChart.timeScale().timeToCoordinate(Number(t));
    return Number.isFinite(x) ? x : null;
  } catch (e) { return null; }
}
function _wizardYOfPrice(p) {
  if (!_wizardCandles || p == null) return null;
  try {
    const y = _wizardCandles.priceToCoordinate(Number(p));
    return Number.isFinite(y) ? y : null;
  } catch (e) { return null; }
}

function _renderWizardAnnotations() {
  const svg = _ensureWizardAnnotSvg();
  if (!svg) return;
  const host = document.getElementById('wizard-chart');
  if (host) {
    const w = host.clientWidth, h = host.clientHeight;
    svg.setAttribute('width', String(w));
    svg.setAttribute('height', String(h));
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  }
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  const blue   = _cssVar('--blue',   '#5b8dee');
  const amber  = _cssVar('--amber',  '#ffb347');
  const accent = _cssVar('--accent', '#26a69a');
  const muted  = _cssVar('--muted',  '#888');

  for (const a of (_wizardAnnotations || [])) {
    const color = a.color || (a.type === 'text' ? amber : blue);
    const x1 = _wizardXOfTime(a.x1);
    const y1 = _wizardYOfPrice(a.y1);
    if (x1 == null || y1 == null) continue;
    if (a.type === 'text') {
      const g = _svgEl('g', { 'data-ann-id': a.id });
      g.appendChild(_svgEl('circle', { cx: x1, cy: y1, r: 4, fill: color, stroke: '#ffffff', 'stroke-width': 1 }));
      const text = _svgEl('text', { x: x1 + 8, y: y1 + 4, fill: color, 'font-family': 'monospace', 'font-size': 11 });
      text.textContent = a.label || 'text';
      g.appendChild(text);
      svg.appendChild(g);
    } else if (a.type === 'arrow') {
      const x2 = _wizardXOfTime(a.x2);
      const y2 = _wizardYOfPrice(a.y2);
      if (x2 == null || y2 == null) continue;
      const g = _svgEl('g', { 'data-ann-id': a.id });
      g.appendChild(_svgEl('line', { x1, y1, x2, y2, stroke: color, 'stroke-width': 2 }));
      const dx = x2 - x1, dy = y2 - y1;
      const len = Math.sqrt(dx * dx + dy * dy);
      if (len > 0.01) {
        const ux = dx / len, uy = dy / len;
        const bx = x2 - ux * 10, by = y2 - uy * 10;
        const px = -uy * 5, py = ux * 5;
        const points = `${x2},${y2} ${bx + px},${by + py} ${bx - px},${by - py}`;
        g.appendChild(_svgEl('polygon', { points, fill: color }));
      }
      svg.appendChild(g);
    }
  }

  if (_wizardMeasureSession) {
    const { p1, p2, priceDiff, pct } = _wizardMeasureSession;
    const ax = _wizardXOfTime(p1.time);
    const ay = _wizardYOfPrice(p1.price);
    const bx = _wizardXOfTime(p2.time);
    const by = _wizardYOfPrice(p2.price);
    if (ax != null && ay != null && bx != null && by != null) {
      const g = _svgEl('g', { 'data-measure': '1' });
      g.appendChild(_svgEl('line', { x1: ax, y1: ay, x2: bx, y2: by, stroke: accent, 'stroke-width': 2, 'stroke-dasharray': '4 3' }));
      g.appendChild(_svgEl('circle', { cx: ax, cy: ay, r: 4, fill: muted }));
      g.appendChild(_svgEl('circle', { cx: bx, cy: by, r: 4, fill: muted }));
      const mx = (ax + bx) / 2, my = (ay + by) / 2;
      const sign = pct >= 0 ? '+' : '';
      const labelText = `${sign}${pct.toFixed(2)}% / ${sign}${priceDiff.toFixed(2)}`;
      const tw = labelText.length * 6.5 + 8;
      g.appendChild(_svgEl('rect', { x: mx - tw / 2, y: my - 16, width: tw, height: 14, fill: 'rgba(0,0,0,0.7)', rx: 2 }));
      const text = _svgEl('text', { x: mx, y: my - 5, fill: accent, 'font-family': 'monospace', 'font-size': 11, 'text-anchor': 'middle' });
      text.textContent = labelText;
      g.appendChild(text);
      svg.appendChild(g);
    }
  }
}

async function _loadWizardAnnotations() {
  _wizardAnnotations = [];
  const tf = _wizardTimeframe || '1h';
  try {
    const r = await fetch(
      `/api/db/annotations?bot_slug=${encodeURIComponent(_WIZARD_ANNOT_SLUG)}&timeframe=${encodeURIComponent(tf)}`,
      { credentials: 'same-origin' }
    );
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) { _renderWizardAnnotations(); return; }
    const body = await r.json();
    if (Array.isArray(body)) _wizardAnnotations = body;
  } catch (e) {}
  _renderWizardAnnotations();
}

async function _persistWizardAnnotation(fields) {
  const body = Object.assign({
    bot_slug: _WIZARD_ANNOT_SLUG,
    timeframe: _wizardTimeframe || '1h',
  }, fields);
  let r;
  try {
    r = await fetch('/api/db/annotations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) { return; }
  if (r.status === 401) { _handle401(); return; }
  if (!r.ok) return;
  try {
    const j = await r.json();
    if (j && Number.isFinite(j.id)) {
      _wizardAnnotations.push(Object.assign({}, body, { id: j.id }));
      _renderWizardAnnotations();
      return;
    }
  } catch (e) {}
  await _loadWizardAnnotations();
}

function _promptWizardAnnotationText(point) {
  const label = window.prompt('Label?');
  if (!label) return;
  _persistWizardAnnotation({
    type: 'text',
    x1: point.time, y1: point.price,
    label,
    color: _cssVar('--amber', '#ffb347'),
  });
}

async function _deleteWizardAnnotationNear(point) {
  if (!_wizardAnnotations.length) return;
  let tMin = Infinity, tMax = -Infinity, pMin = Infinity, pMax = -Infinity;
  for (const a of _wizardAnnotations) {
    if (a.x1 != null) { tMin = Math.min(tMin, a.x1); tMax = Math.max(tMax, a.x1); }
    if (a.x2 != null) { tMin = Math.min(tMin, a.x2); tMax = Math.max(tMax, a.x2); }
    if (a.y1 != null) { pMin = Math.min(pMin, a.y1); pMax = Math.max(pMax, a.y1); }
    if (a.y2 != null) { pMin = Math.min(pMin, a.y2); pMax = Math.max(pMax, a.y2); }
  }
  const tSpan = Math.max(1, tMax - tMin);
  const pSpan = Math.max(1, pMax - pMin);
  let best = null, bestD = Infinity;
  for (const a of _wizardAnnotations) {
    const dt = ((Number(a.x1) || point.time) - point.time) / tSpan;
    const dp = ((Number(a.y1) || point.price) - point.price) / pSpan;
    const d = dt * dt + dp * dp;
    if (d < bestD) { bestD = d; best = a; }
  }
  if (!best) return;
  try {
    const r = await fetch(`/api/db/annotations/${best.id}`, { method: 'DELETE' });
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) return;
    _wizardAnnotations = _wizardAnnotations.filter(a => a.id !== best.id);
    _renderWizardAnnotations();
  } catch (e) {}
}

function _installWizardChartToolHandlers() {
  if (!_wizardChart || !_wizardCandles) return;
  const ts = _wizardChart.timeScale();
  const handle = (param) => {
    if (_wizardActiveTool === 'select') return;
    if (!param || !param.point) return;
    const { x, y } = param.point;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    let t = param.time;
    if (t == null) {
      try { t = ts.coordinateToTime(x); } catch (e) { t = null; }
    }
    if (t == null) return;
    const price = _wizardCandles.coordinateToPrice(y);
    if (!Number.isFinite(price)) return;
    const point = { time: Number(t), price: Number(price) };
    if (_wizardActiveTool === 'text') {
      _promptWizardAnnotationText(point);
      return;
    }
    if (_wizardActiveTool === 'measure' || _wizardActiveTool === 'arrow') {
      if (!_wizardToolFirstPoint) {
        _wizardToolFirstPoint = point;
      } else {
        const p1 = _wizardToolFirstPoint;
        const p2 = point;
        if (_wizardActiveTool === 'measure') {
          const priceDiff = p2.price - p1.price;
          const pct = p1.price ? (priceDiff / p1.price) * 100 : 0;
          _wizardMeasureSession = { p1, p2, priceDiff, pct };
          _renderWizardAnnotations();
        } else {
          _persistWizardAnnotation({
            type: 'arrow',
            x1: p1.time, y1: p1.price,
            x2: p2.time, y2: p2.price,
            color: _cssVar('--blue', '#5b8dee'),
          });
        }
        _wizardToolFirstPoint = null;
      }
      return;
    }
    if (_wizardActiveTool === 'delete') {
      _deleteWizardAnnotationNear(point);
    }
  };
  try { _wizardChart.subscribeClick(handle); } catch (e) {}
}

function _clearWizardOverlays() {
  if (!_wizardChart || !_wizardCandles) return;
  for (const s of _wizardOverlaySeries) {
    try { _wizardChart.removeSeries(s); } catch (e) {}
  }
  _wizardOverlaySeries = [];
  for (const pl of _wizardOverlayPriceLines) {
    try { _wizardCandles.removePriceLine(pl); } catch (e) {}
  }
  _wizardOverlayPriceLines = [];
}

function _wizardEnsureRsiChart() {
  if (_wizardChartRsi) return;
  const el = $('wizard-chart-rsi');
  if (!el || !_chartLibAvailable()) return;
  el.classList.remove('hidden');
  _wizardChartRsi = LightweightCharts.createChart(el, {
    ..._chartLayoutOpts(),
    width:  el.clientWidth,
    height: el.clientHeight || 100,
  });
  _wizardSubSeries.rsi = _wizardChartRsi.addLineSeries({
    color: _cssVar('--blue', '#5b8dee'), lineWidth: 1,
  });
  _wizardSubSeries.rsi.createPriceLine({
    price: 70, color: _cssVar('--red',    '#ef5350'),
    lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: '70',
  });
  _wizardSubSeries.rsi.createPriceLine({
    price: 30, color: _cssVar('--accent', '#26a69a'),
    lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: '30',
  });
  if (_wizardResizeObs) _wizardResizeObs.observe(el);
}

function _wizardDestroyRsiChart() {
  if (!_wizardChartRsi) return;
  try { _wizardChartRsi.remove(); } catch (e) {}
  _wizardChartRsi = null;
  delete _wizardSubSeries.rsi;
  const el = $('wizard-chart-rsi');
  if (el) el.classList.add('hidden');
}

function _wizardEnsureMacdChart() {
  if (_wizardChartMacd) return;
  const el = $('wizard-chart-macd');
  if (!el || !_chartLibAvailable()) return;
  el.classList.remove('hidden');
  _wizardChartMacd = LightweightCharts.createChart(el, {
    ..._chartLayoutOpts(),
    width:  el.clientWidth,
    height: el.clientHeight || 100,
  });
  _wizardSubSeries.macdHist   = _wizardChartMacd.addHistogramSeries({ color: _cssVar('--muted', '#888') });
  _wizardSubSeries.macdLine   = _wizardChartMacd.addLineSeries({ color: _cssVar('--blue',  '#5b8dee'), lineWidth: 1 });
  _wizardSubSeries.macdSignal = _wizardChartMacd.addLineSeries({ color: _cssVar('--amber', '#ffb347'), lineWidth: 1 });
  if (_wizardResizeObs) _wizardResizeObs.observe(el);
}

function _wizardDestroyMacdChart() {
  if (!_wizardChartMacd) return;
  try { _wizardChartMacd.remove(); } catch (e) {}
  _wizardChartMacd = null;
  delete _wizardSubSeries.macdHist;
  delete _wizardSubSeries.macdLine;
  delete _wizardSubSeries.macdSignal;
  const el = $('wizard-chart-macd');
  if (el) el.classList.add('hidden');
}

function _addWizardLineSeries(data, color, lineWidth = 2, lineStyle = 0) {
  if (!_wizardChart || !data || !data.length) return null;
  const s = _wizardChart.addLineSeries({
    color, lineWidth, lineStyle, lastValueVisible: false, priceLineVisible: false,
  });
  s.setData(data);
  _wizardOverlaySeries.push(s);
  return s;
}

function _addWizardPriceLine(price, color, title, lineStyle = 2) {
  if (!_wizardCandles || !Number.isFinite(price)) return;
  const pl = _wizardCandles.createPriceLine({
    price, color, lineWidth: 1, lineStyle, axisLabelVisible: true, title,
  });
  _wizardOverlayPriceLines.push(pl);
}

// Re-draw all wizard chart overlays from the current nbState. Called on
// every nbRecompute() so adding/removing/tweaking an indicator updates
// the preview live, plus on every fresh candle fetch.
function renderWizardOverlays() {
  if (!_wizardChart || !_wizardCandles || !_wizardCandleCache) return;
  if (typeof nbState !== 'object' || !nbState) return;

  _clearWizardOverlays();

  const candles = _wizardCandleCache;
  const closes = candles.map(c => c.close);
  const highs  = candles.map(c => c.high);
  const lows   = candles.map(c => c.low);
  const lastClose = closes[closes.length - 1];

  const accent = _cssVar('--accent', '#26a69a');
  const blue   = _cssVar('--blue',   '#5b8dee');
  const muted  = _cssVar('--muted',  '#888');
  const red    = _cssVar('--red',    '#ef5350');
  const amber  = _cssVar('--amber',  '#ffb347');

  const indicators = Array.isArray(nbState.indicators) ? nbState.indicators : [];

  // Sub-charts: ensure / tear down based on whether the corresponding
  // indicator is currently configured. Each sub-chart is independent so
  // adding RSI alone doesn't drag MACD along.
  const rsiCfg  = indicators.find(i => i && String(i.type).toUpperCase() === 'RSI');
  const macdCfg = indicators.find(i => i && String(i.type).toUpperCase() === 'MACD');
  if (rsiCfg)  _wizardEnsureRsiChart();  else _wizardDestroyRsiChart();
  if (macdCfg) _wizardEnsureMacdChart(); else _wizardDestroyMacdChart();

  if (rsiCfg && _wizardSubSeries.rsi) {
    try {
      const period = Number(rsiCfg.period) || 14;
      _wizardSubSeries.rsi.setData(calcRSILine(candles, period));
    } catch (e) {}
  }
  if (macdCfg && _wizardSubSeries.macdLine) {
    try {
      const fast = Number(macdCfg.macd_fast)   || 12;
      const slow = Number(macdCfg.macd_slow)   || 26;
      const sig  = Number(macdCfg.macd_signal) || 9;
      const m = calcMACDLines(candles, fast, slow, sig);
      if (m) {
        if (m.macd)      _wizardSubSeries.macdLine.setData(m.macd);
        if (m.signal)    _wizardSubSeries.macdSignal.setData(m.signal);
        if (m.histogram) _wizardSubSeries.macdHist.setData(m.histogram);
      }
    } catch (e) {}
  }

  for (const ind of indicators) {
    if (!ind || !ind.type) continue;
    const t = String(ind.type).toUpperCase();
    try {
      if (t === 'EMA_CROSS') {
        const fast = Number(ind.fast) || 9;
        const slow = Number(ind.slow) || 21;
        _addWizardLineSeries(calcEMALine(candles, fast), accent, 2);
        _addWizardLineSeries(calcEMALine(candles, slow), blue,   2);
      } else if (t === 'BOLLINGER') {
        const period = Number(ind.period) || 20;
        const mult   = Number(ind.multiplier) || 2.0;
        if (typeof calcBollingerLines === 'function') {
          const bb = calcBollingerLines(candles, period, mult);
          _addWizardLineSeries(bb.upper,  muted, 1, 2);
          _addWizardLineSeries(bb.middle, blue,  1, 0);
          _addWizardLineSeries(bb.lower,  muted, 1, 2);
        }
      } else if (t === 'SUPERTREND') {
        const atr  = Number(ind.atr_period) || 10;
        const mult = Number(ind.multiplier) || 3.0;
        if (typeof calcSupertrendLines === 'function') {
          const st = calcSupertrendLines(candles, highs, lows, atr, mult);
          if (st && st.bull) _addWizardLineSeries(st.bull, accent, 2);
          if (st && st.bear) _addWizardLineSeries(st.bear, red,    2);
        }
      } else if (t === 'SUPPORT_RESISTANCE') {
        const lookback  = Number(ind.lookback) || 3;
        const tolerance = Number(ind.tolerance_pct) || 0.5;
        if (typeof calcSR === 'function') {
          const sr = calcSR(highs, lows, closes, lookback, tolerance);
          if (sr) {
            for (const lvl of (sr.support || [])) _addWizardPriceLine(lvl, accent, 'S');
            for (const lvl of (sr.resistance || [])) _addWizardPriceLine(lvl, red, 'R');
          }
        }
      }
    } catch (e) { /* keep going if one indicator fails */ }
  }

  // TP / DCA preview lines anchored on the latest close — when the user
  // tweaks tp_target_pct or dca_spacing_pct in the wizard the lines move
  // immediately because nbRecompute calls back here.
  const tpPct = Number(nbState.tp_target_pct) || 0;
  if (tpPct > 0 && Number.isFinite(lastClose)) {
    _addWizardPriceLine(lastClose * (1 + tpPct / 100), accent, 'TP');
  }
  const dcaSpacing = Number(nbState.dca_spacing_pct) || 0;
  const dcaCount   = Number(nbState.dca_max_orders) || 0;
  if (dcaSpacing > 0 && dcaCount > 0 && Number.isFinite(lastClose)) {
    let last = lastClose;
    for (let i = 1; i <= dcaCount; i++) {
      last = last * (1 - dcaSpacing / 100);
      _addWizardPriceLine(last, amber, `DCA${i}`);
    }
  }
}

// ── Indicator helpers (JS ports of strategies/indicators/*.py) ───────────────
// All helpers accept the candles array from /api/chart and return data
// shaped for Lightweight Charts series consumption: {time, value} (or
// {time, value, color} for histograms). The Python implementations use
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

function calcSR(closes, lookback, tolerancePct) {
  // Mirrors strategies/indicators/support_resistance.py. Returns
  // {support: [levels], resistance: [levels]} clustered.
  const highs = [], lows = [];
  for (let i = lookback; i < closes.length - lookback; i++) {
    const pivot = closes[i];
    const left  = closes.slice(i - lookback, i);
    const right = closes.slice(i + 1, i + 1 + lookback);
    if (left.every(x => pivot > x) && right.every(x => pivot > x)) highs.push(pivot);
    else if (left.every(x => pivot < x) && right.every(x => pivot < x)) lows.push(pivot);
  }
  const cluster = (levels) => {
    const out = [];
    for (const lvl of levels) {
      let merged = false;
      for (let i = 0; i < out.length; i++) {
        if (out[i] === 0) continue;
        const diffPct = Math.abs(lvl - out[i]) / out[i] * 100;
        if (diffPct <= tolerancePct) { out[i] = lvl; merged = true; break; }
      }
      if (!merged) out.push(lvl);
    }
    return out;
  };
  return { support: cluster(lows), resistance: cluster(highs) };
}

function calcQFL(closes, lookback, crackPct, baseCandles, maxBases) {
  const bases = [];
  for (let i = lookback; i < closes.length - lookback; i++) {
    const pivot = closes[i];
    const left  = closes.slice(i - lookback, i);
    const right = closes.slice(i + 1, i + 1 + lookback);
    if (left.every(x => pivot < x) && right.every(x => pivot < x)) {
      const end = Math.min(i + 1 + baseCandles, closes.length);
      const window = closes.slice(i + 1, end);
      const rebound = window.length ? Math.max(...window) : pivot;
      if (rebound >= pivot * (1 + crackPct / 100)) bases.push(pivot);
    }
  }
  return bases.slice(-maxBases);
}

function calcParabolicSARMarkers(candles, initialAF, maxAF) {
  // Simplified close-based variant matching parabolic_sar.py. Marker per
  // candle with shape:circle below (bullish) or above (bearish).
  const closes = candles.map(c => c.close);
  if (closes.length < 10) return [];
  let trend, ep, sar, af = initialAF;
  if (closes[1] >= closes[0]) { trend = 1; ep = closes[1]; sar = closes[0]; }
  else                        { trend = -1; ep = closes[1]; sar = closes[0]; }
  const out = [];
  const accent = _cssVar('--accent', '#26a69a');
  const red    = _cssVar('--red',    '#ef5350');
  for (let i = 2; i < closes.length; i++) {
    const price = closes[i];
    const newSar = sar + af * (ep - sar);
    if (trend === 1) {
      if (newSar > price) { trend = -1; sar = ep; ep = price; af = initialAF; }
      else { sar = newSar; if (price > ep) { ep = price; af = Math.min(af + initialAF, maxAF); } }
    } else {
      if (newSar < price) { trend = 1; sar = ep; ep = price; af = initialAF; }
      else { sar = newSar; if (price < ep) { ep = price; af = Math.min(af + initialAF, maxAF); } }
    }
  }
  // Only render the latest marker — full per-candle markers would clutter.
  out.push({
    time: candles[candles.length - 1].time,
    position: trend === 1 ? 'belowBar' : 'aboveBar',
    color: trend === 1 ? accent : red,
    shape: 'circle',
    text: 'SAR',
  });
  return out;
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

// ── Event wiring (vervangt alle inline onclick=) ─────────────────────────────
function setupEventListeners() {
  $('restart-btn').addEventListener('click', restartPortal);

  // Profile dropdown button + menu entries
  $('profile-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleProfileMenu();
  });
  $('profile-menu-profile').addEventListener('click', showProfileModal);
  $('profile-menu-settings').addEventListener('click', showSettingsModal);
  $('profile-menu-logout').addEventListener('click', handleLogout);
  _installProfileOutsideClickHandler();

  // Profile modal
  $('profile-close').addEventListener('click', closeProfileModal);
  $('profile-save').addEventListener('click', saveProfileModal);
  $('profile-api-copy').addEventListener('click', copyProfileApiKey);

  // Settings modal
  $('settings-close').addEventListener('click', closeSettingsModal);
  $('settings-reset').addEventListener('click', resetSettingsDefaults);
  $('settings-textsize').addEventListener('input', (e) => {
    _settingsApplyTextSize(e.target.value, true);
  });
  document.querySelectorAll('#settings-modal .settings-seg-btn[data-brightness]').forEach(b => {
    b.addEventListener('click', () => _settingsApplyBrightness(b.dataset.brightness, true));
  });
  document.querySelectorAll('#settings-modal .settings-seg-btn[data-theme]').forEach(b => {
    b.addEventListener('click', () => _settingsApplyTheme(b.dataset.theme, true));
  });
  $('settings-compact').addEventListener('change', (e) => {
    _settingsApplyCompact(e.target.checked, true);
  });

  // Wrap in arrow fns so the native click event isn't forwarded as the
  // fromPop argument — passing a truthy Event suppressed history.pushState.
  $('nav-overview-btn').addEventListener('click', () => goOverview());
  $('nav-bots-btn').addEventListener('click', () => goBots());
  $('nav-deals-btn').addEventListener('click', () => goDeals());

  // The dedicated detail-back button is gone; users leave the detail
  // view via the main Bots tab or the browser back button (popstate).

  $('new-bot-btn').addEventListener('click', goNewBot);
  const btHistBtn = $('bt-history-btn');
  if (btHistBtn) btHistBtn.addEventListener('click', btOpenHistoryPanel);
  const btHistClose = $('bt-history-close-btn');
  if (btHistClose) btHistClose.addEventListener('click', btCloseHistoryPanel);

  document.querySelectorAll('.detail-subnav .tab').forEach(btn => {
    btn.addEventListener('click', () => showDTab(btn.dataset.dtab, btn));
  });

  // Chart tab hover prefetch — warm the backend cache so the click
  // hits a hot key. Only the chart tab needs this; the others render
  // from the already-cached fetchDetail() payload.
  const chartTabBtn = document.querySelector('.detail-subnav .tab[data-dtab="chart"]');
  if (chartTabBtn) {
    chartTabBtn.addEventListener('mouseenter', _scheduleChartPrefetch);
    chartTabBtn.addEventListener('mouseleave', _cancelChartPrefetch);
  }

  // Chart timeframe selector
  document.querySelectorAll('.chart-tf-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      _chartTimeframe = btn.dataset.tf;
      updateChartTfButtons();
      fetchChartData();
      _loadAnnotations();
    });
  });

  // Chart annotation toolbar — only buttons that declare data-tool.
  // The "Clear all" button has its own dedicated handler below and the
  // wizard toolbar uses data-wtool, so neither leaks into _setActiveTool
  // (which would silently coerce them into 'select').
  document.querySelectorAll('.chart-tool[data-tool]').forEach(btn => {
    btn.addEventListener('click', () => _setActiveTool(btn.dataset.tool));
  });

  const clearAllBtn = $('chart-clear-all');
  if (clearAllBtn) clearAllBtn.addEventListener('click', () => {
    _clearAllAnnotations(currentSlug, _chartTimeframe || '1h', () => {
      _chartAnnotations = [];
      _renderAnnotations();
    });
  });

  // Wizard toolbar — separate data-wtool namespace so it doesn't share
  // active state with the detail chart tools. Each button calls into
  // _setActiveWizardTool which mirrors _setActiveTool for the wizard.
  document.querySelectorAll('.chart-tool[data-wtool]').forEach(btn => {
    btn.addEventListener('click', () => _setActiveWizardTool(btn.dataset.wtool));
  });
  const wizardClearAllBtn = $('wizard-chart-clear-all');
  if (wizardClearAllBtn) wizardClearAllBtn.addEventListener('click', () => {
    _clearAllAnnotations('wizard', _wizardTimeframe || '1h', () => {
      _wizardAnnotations = [];
      _renderWizardAnnotations();
    });
  });

  // Clear deal markers button
  const clearDealBtn = $('chart-clear-deal');
  if (clearDealBtn) clearDealBtn.addEventListener('click', clearDealFromChart);

  // Delegated click handlers on the bot detail deals tables — clicking
  // any row jumps to the chart tab with timeline markers for that deal.
  const _dealRowHandler = (e) => {
    const tr = e.target.closest('tr[data-deal-id]');
    if (!tr) return;
    const deal = findDealByIdInCurrentDetail(tr.dataset.dealId);
    if (deal) showDealOnChart(deal);
  };
  const openTbody = $('d-open-tbody');
  if (openTbody) openTbody.addEventListener('click', _dealRowHandler);
  const closedTbody = $('d-closed-tbody');
  if (closedTbody) closedTbody.addEventListener('click', _dealRowHandler);

  $('modal-clear-btn').addEventListener('click', clearApiKey);
  $('modal-cancel-btn').addEventListener('click', closeApiKeyModal);
  $('modal-save-btn').addEventListener('click', saveApiKey);

  const lf  = $('login-form');   if (lf) lf.addEventListener('submit', handleLoginSubmit);

  setupActiveDealsCog();
  setupDetailOpenDealsCog();
  setupDetailClosedDealsCog();
  _installCogOutsideClickHandler();

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
  const manualBtn = $('d-btn-manual-deal');
  if (manualBtn) {
    manualBtn.addEventListener('click', () => {
      if (currentSlug) manualStartDeal(currentSlug, manualBtn);
    });
  }

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
document.addEventListener('DOMContentLoaded', async () => {
  setupEventListeners();

  // Gate: require a valid session cookie before bringing up the SPA.
  const authed = await checkAuthStatus();
  if (!authed) {
    _handle401();
    return;
  }
  // Authed — make sure the chrome is visible (a previous _handle401
  // call from a stale tab on the same page could have left the
  // .is-login class on body).
  document.body.classList.remove('is-login');

  refreshProfileInitial();

  // No more first-visit API key prompt — session cookie auth covers
  // the SPA. The API key is still settable via Profile → API Key for
  // operators who want to call mutating endpoints from scripts.

  // History API wiring — back/forward in the browser replays pushState
  // events without triggering another push. Initial load parses the
  // hash so reloads preserve view state.
  window.addEventListener('popstate', (e) => {
    const s = e.state;
    if (!s) { _routeFromHash(); return; }
    switch (s.view) {
      case 'bot':      if (s.slug) openBot(s.slug, true); else goOverview(true); break;
      case 'bots':     goBots(true); break;
      case 'deals':    goDeals(true); break;
      case 'overview':
      default:         goOverview(true); break;
    }
  });

  if (window.location.hash) {
    _routeFromHash();
  }

  fetchOverview();
  fetchPrice();
  overviewInterval = setInterval(fetchOverview, 30000);
  setInterval(fetchPrice, 15000);
  connectStateWS();

  setTimeout(async () => {
    try {
      const d = await fetch('/api/bots').then(r => r.json());
      const slugs = (d.bots || []).map(b => b.slug);
      slugs.push('portal');
      connectOverviewLogs(slugs);
    } catch (e) {}
  }, 1000);

  // ── Backtest tab wiring ────────────────────────────────────────────────
  const btRunBtn = $('bt-run-btn');
  if (btRunBtn) btRunBtn.addEventListener('click', btRunFromTab);
  // Default dates — last 30 days
  const btEnd = new Date();
  const btStart = new Date(btEnd.getTime() - 30 * 86400 * 1000);
  const ymd = d => d.toISOString().slice(0, 10);
  if ($('bt-start')) $('bt-start').value = ymd(btStart);
  if ($('bt-end'))   $('bt-end').value   = ymd(btEnd);
  if ($('wbt-start')) $('wbt-start').value = ymd(btStart);
  if ($('wbt-end'))   $('wbt-end').value   = ymd(btEnd);

  const nbBtBtn = $('nb-bt-btn');
  if (nbBtBtn) nbBtBtn.addEventListener('click', btOpenWizardModal);
  const wbtRun = $('wbt-run-btn');
  if (wbtRun) wbtRun.addEventListener('click', btRunFromWizard);
  const wbtClose = $('wbt-close-btn');
  if (wbtClose) wbtClose.addEventListener('click', () => {
    $('wizard-backtest-modal').classList.remove('show');
  });

  // Live "N days of data" hint next to the timeframe selector — refresh
  // on every tf / start / end change so the operator can see the
  // coverage of their chosen range before they run the backtest.
  const refreshTabInfo = () =>
    btUpdateTfWarning('bt-tf', 'bt-start', 'bt-end', 'bt-warning');
  const refreshWizInfo = () =>
    btUpdateTfWarning('wbt-tf', 'wbt-start', 'wbt-end', 'wbt-warning');
  ['bt-tf', 'bt-start', 'bt-end', 'bt-start-time', 'bt-end-time'].forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('change', refreshTabInfo);
  });
  ['wbt-tf', 'wbt-start', 'wbt-end', 'wbt-start-time', 'wbt-end-time'].forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('change', refreshWizInfo);
  });
  refreshTabInfo();
  refreshWizInfo();
});

// ─────────────────────────────────────────────────────────────────────────────
// ── Backtest ────────────────────────────────────────────────────────────────
// Client-side simulator that mirrors backtest/backtest_engine.py for
// entry/DCA/TP/SL/fees. Long-only, inverse perpetual math:
//   pnl_btc = size * (close - avg_entry) / avg_entry * leverage
// Indicators are a simplified subset of the Python engine — RSI, EMA
// cross, MACD histogram and Bollinger lower-band are supported; the
// rest (Supertrend, PSAR, market structure, S/R, QFL) short-circuit to
// "always true" and the entry signal is the logical AND of every
// configured indicator. See _precomputeSignalArrays for details.
// ─────────────────────────────────────────────────────────────────────────────

const BT_DEAL_LS_KEY = 'reverto.bt_deal_columns';
const BT_DEAL_COLUMNS = [
  { key: 'id',        label: '#'        },
  { key: 'opened',    label: 'Opened'   },
  { key: 'closed',    label: 'Closed'   },
  { key: 'duration',  label: 'Duration' },
  { key: 'entry',     label: 'Entry'    },
  { key: 'avg',       label: 'Avg Entry'},
  { key: 'close',     label: 'Close'    },
  { key: 'dcas',      label: 'DCAs'     },
  { key: 'pnl_btc',   label: 'PnL BTC'  },
  { key: 'pnl_pct',   label: 'PnL %'    },
  { key: 'reason',    label: 'Reason'   },
];

class RevertoBacktest {
  constructor(config, candles) {
    this.config = config;
    this.candles = candles;
    this.balance_btc = 0;
    this.initial_balance_btc = 0;
    this.open_deal = null;
    this.closed_deals = [];
    this.equity_curve = [];
    this.fees_paid_btc = 0;
    this._deal_counter = 0;
    this._tp_pct   = (config.take_profit && config.take_profit.target_pct) || 3.0;
    this._sl_pct   = (config.stop_loss   && config.stop_loss.pct)          || 5.0;
    this._sl_type  = (config.stop_loss   && config.stop_loss.type)         || 'fixed';
    this._base_size = (config.dca && config.dca.base_order_size) || 0.001;
    this._max_orders = (config.dca && config.dca.max_orders)      || 1;
    this._spacing = (config.dca && config.dca.order_spacing_pct)  || 2.5;
    this._mult    = (config.dca && config.dca.multiplier)         || 1.0;
    this._taker_fee = (config.dca && config.dca.taker_fee) || 0.0006;
    this._lev = (config.leverage && config.leverage.enabled && config.leverage.size) || 1;
    if (window._BT_DEBUG) {
      console.log('[FEE] taker_fee config:', this._taker_fee,
        'base_size:', this._base_size,
        'raw config.dca.taker_fee:', config.dca && config.dca.taker_fee);
    }
  }

  _calcFee(size) { return size * this._taker_fee; }

  _openDeal(price, time, i) {
    this._deal_counter += 1;
    const fee = this._calcFee(this._base_size);
    this.balance_btc -= fee;
    this.fees_paid_btc += fee;
    if (window._BT_DEBUG) {
      console.log('[FEE] entry fee:', fee, 'size:', this._base_size,
        'deal:', this._deal_counter);
    }
    this.open_deal = {
      id: this._deal_counter,
      opened_at: time,
      opened_idx: i,
      orders: [{ price, size: this._base_size, type: 'base' }],
      dca_count: 0,
      peak_price: 0,
      entry_fee_btc: fee,
      dca_fees_btc: 0,
      exit_fee_btc: 0,
    };
  }

  _avgEntry(deal) {
    let totSize = 0, totVal = 0;
    for (const o of deal.orders) { totSize += o.size; totVal += o.size * o.price; }
    return totVal / totSize;
  }
  _totalSize(deal) {
    let s = 0; for (const o of deal.orders) s += o.size; return s;
  }

  _checkTp(deal, candle) {
    const avg = this._avgEntry(deal);
    const tpPrice = avg * (1 + this._tp_pct / 100);
    if (candle.high >= tpPrice) {
      this._closeDeal(deal, tpPrice, 'tp', candle.time);
      return true;
    }
    return false;
  }

  _checkSl(deal, candle) {
    const avg = this._avgEntry(deal);
    let slPrice;
    if (this._sl_type === 'trailing') {
      if (deal.peak_price === 0) deal.peak_price = candle.open;
      if (candle.high > deal.peak_price) deal.peak_price = candle.high;
      slPrice = deal.peak_price * (1 - this._sl_pct / 100);
    } else {
      slPrice = avg * (1 - this._sl_pct / 100);
    }
    if (candle.low <= slPrice) {
      this._closeDeal(deal, slPrice, 'sl', candle.time);
      return true;
    }
    return false;
  }

  _checkDca(deal, candle) {
    if (this._max_orders <= 1) return;
    if (deal.dca_count >= this._max_orders - 1) return;
    const lastPrice = deal.orders[deal.orders.length - 1].price;
    const nextDca = lastPrice * (1 - this._spacing / 100);
    // Match Python backtest_engine._check_dca exactly: trigger on
    // candle.close (not candle.low), fill at candle.close (not the
    // line), and use dca_count as the multiplier exponent BEFORE
    // incrementing it. The previous JS version triggered on wicks,
    // filled at the line, and over-multiplied size by one step.
    if (candle.close <= nextDca) {
      const dcaSize = this._base_size * Math.pow(this._mult, deal.dca_count);
      const fee = this._calcFee(dcaSize);
      this.balance_btc -= fee;
      this.fees_paid_btc += fee;
      deal.dca_fees_btc += fee;
      if (window._BT_DEBUG) {
        console.log('[FEE] dca fee:', fee, 'size:', dcaSize,
          'deal:', deal.id, 'running dca_fees:', deal.dca_fees_btc);
      }
      deal.orders.push({ price: candle.close, size: dcaSize, type: 'dca' });
      deal.dca_count += 1;
      if (window._BT_DEBUG) {
        console.log('[BT] DCA #', deal.dca_count, 'at', candle.close, 'avg now', this._avgEntry(deal));
      }
    }
  }

  _closeDeal(deal, price, reason, time) {
    const avg = this._avgEntry(deal);
    const size = this._totalSize(deal);
    const grossPnlBtc = size * (price - avg) / avg * this._lev;
    const exitFee = this._calcFee(size);
    deal.exit_fee_btc = exitFee;
    const dealFees = deal.entry_fee_btc + deal.dca_fees_btc + exitFee;
    if (window._BT_DEBUG) {
      console.log('[FEE] exit fee:', exitFee, 'total_size:', size, 'deal:', deal.id);
      console.log('[FEE] deal total fees:', dealFees,
        '= entry', deal.entry_fee_btc, '+ dca', deal.dca_fees_btc, '+ exit', exitFee);
    }
    const netPnlBtc = grossPnlBtc - dealFees;
    const margin = size / this._lev;
    const pnlPct = margin > 0 ? (netPnlBtc / margin) * 100 : 0;
    this.balance_btc += grossPnlBtc - exitFee;
    this.fees_paid_btc += exitFee;
    if (window._BT_DEBUG && this.closed_deals.length < 10) {
      console.log('[BT] close deal #', deal.id, 'reason', reason,
        'price', price, 'gross', grossPnlBtc.toFixed(8),
        'fees', dealFees.toFixed(8), 'net', netPnlBtc.toFixed(8));
    }
    this.closed_deals.push({
      id: deal.id,
      opened_at: deal.opened_at,
      closed_at: time,
      entry_price: deal.orders[0].price,
      avg_entry_price: avg,
      close_price: price,
      dca_count: deal.dca_count,
      orders: deal.orders,
      gross_pnl_btc: grossPnlBtc,
      pnl_btc: netPnlBtc,
      pnl_pct: pnlPct,
      entry_fee_btc: deal.entry_fee_btc,
      dca_fees_btc: deal.dca_fees_btc,
      exit_fee_btc: exitFee,
      total_fees_btc: dealFees,
      reason,
      total_size: size,
    });
    this.open_deal = null;
  }

  _unrealizedPnl(close) {
    if (!this.open_deal) return 0;
    const avg = this._avgEntry(this.open_deal);
    const size = this._totalSize(this.open_deal);
    return size * (close - avg) / avg * this._lev;
  }

  _precomputeSignalArrays() {
    // Build aligned boolean "is the entry signal active at index i"
    // arrays per configured indicator. The caller then ANDs across all
    // indicators per candle. Matches the Python check_entry_signal
    // semantics for RSI / EMA_CROSS / MACD histogram / Bollinger lower
    // band; other indicator types simplify to "always true" — they are
    // intentionally documented as a client-side-backtester limitation.
    const indicators = (this.config.entry && this.config.entry.indicators) || [];
    const n = this.candles.length;
    const result = [];
    if (!indicators.length) {
      return [new Array(n).fill(true)];
    }
    for (const ind of indicators) {
      const arr = new Array(n).fill(false);
      const type = ind.type;
      if (type === 'RSI') {
        const period = ind.period || 14;
        const thr = (ind.threshold || 'below_30').toString();
        const m = thr.match(/^([a-z_]+)_(\d+)/i);
        const cond  = m ? m[1] : 'below';
        const value = m ? parseInt(m[2], 10) : 30;
        const line = calcRSILine(this.candles, period);
        // Build an aligned values array so cross_* can compare i vs i-1
        // without a Map lookup. Indices without an RSI value (warmup)
        // stay NaN and never fire a signal.
        const rsiByIdx = new Array(n).fill(NaN);
        const timeToI = new Map();
        this.candles.forEach((c, i) => timeToI.set(c.time, i));
        for (const p of line) {
          const i = timeToI.get(p.time);
          if (i != null) rsiByIdx[i] = p.value;
        }
        for (let i = 0; i < n; i++) {
          const v = rsiByIdx[i];
          if (!Number.isFinite(v)) continue;
          let hit = false;
          if (cond === 'below') {
            hit = v < value;
          } else if (cond === 'above') {
            hit = v > value;
          } else if (cond === 'cross_above') {
            const prev = rsiByIdx[i - 1];
            hit = Number.isFinite(prev) && prev <= value && v > value;
          } else if (cond === 'cross_below') {
            const prev = rsiByIdx[i - 1];
            hit = Number.isFinite(prev) && prev >= value && v < value;
          }
          arr[i] = hit;
        }
      } else if (type === 'EMA_CROSS') {
        const fast = ind.fast || 12;
        const slow = ind.slow || 26;
        const fastLine = calcEMALine(this.candles, fast);
        const slowLine = calcEMALine(this.candles, slow);
        const fastMap = new Map(fastLine.map(p => [p.time, p.value]));
        const slowMap = new Map(slowLine.map(p => [p.time, p.value]));
        // Cross-only — match the Python check_ema_cross_signal which
        // requires fast just crossed up through slow on this bar.
        // The earlier "also allow currently above" fallback turned the
        // filter into a permanent pass once fast > slow happened once.
        let prevDiff = null;
        for (let i = 0; i < n; i++) {
          const t = this.candles[i].time;
          const f = fastMap.get(t), s = slowMap.get(t);
          if (f == null || s == null) { prevDiff = null; continue; }
          const d = f - s;
          if (prevDiff != null && d > 0 && prevDiff <= 0) arr[i] = true;
          prevDiff = d;
        }
      } else if (type === 'MACD') {
        const fast   = ind.macd_fast   || 12;
        const slow   = ind.macd_slow   || 26;
        const signal = ind.macd_signal || 9;
        const { histogram } = calcMACDLines(this.candles, fast, slow, signal);
        const histMap = new Map(histogram.map(p => [p.time, p.value]));
        for (let i = 0; i < n; i++) {
          const v = histMap.get(this.candles[i].time);
          if (v != null && v > 0) arr[i] = true;
        }
      } else if (type === 'BOLLINGER') {
        const period = ind.period || 20;
        const mult = ind.multiplier != null ? ind.multiplier : 2.0;
        const bb = calcBollingerLines(this.candles, period, mult);
        const lowerMap = new Map(bb.lower.map(p => [p.time, p.value]));
        for (let i = 0; i < n; i++) {
          const v = lowerMap.get(this.candles[i].time);
          if (v != null && this.candles[i].close < v) arr[i] = true;
        }
      } else {
        // SUPERTREND, PARABOLIC_SAR, MARKET_STRUCTURE,
        // SUPPORT_RESISTANCE, QFL — simplified to always-true in the
        // client-side engine. The Python backtester is authoritative
        // for those; the wizard modal shows a note.
        for (let i = 0; i < n; i++) arr[i] = true;
      }
      result.push(arr);
    }
    return result;
  }

  async run(initialBalance, onProgress) {
    this.balance_btc = initialBalance;
    this.initial_balance_btc = initialBalance;
    const total = this.candles.length;
    const sigArrays = this._precomputeSignalArrays();
    // Instrumentation counters — always on. The operator (and future
    // debugging sessions) can spot a broken re-entry loop or an
    // overly-restrictive signal filter at a glance instead of having
    // to enable window._BT_DEBUG and comb through per-deal logs.
    let _btStatOpens = 0;
    let _btStatCloses = 0;
    let _btStatSameCandleReopens = 0;
    let _btStatEntrySignalTrue = 0;
    let _btStatEntryBlocked = 0;
    // One-shot config dump on debug. Surfaces exactly what the engine
    // received so the operator can spot config-shape bugs (wrong key
    // names, missing entry block, etc.) without re-running.
    if (window._BT_DEBUG) {
      const indicators = (this.config.entry && this.config.entry.indicators) || [];
      console.log('[BT] config entry.indicators:', indicators);
      console.log('[BT] tp_pct:', this._tp_pct, 'sl_pct:', this._sl_pct,
        'sl_type:', this._sl_type, 'lev:', this._lev,
        'max_orders:', this._max_orders, 'spacing:', this._spacing,
        'mult:', this._mult, 'base_size:', this._base_size);
      console.log('[BT] sigArrays count:', sigArrays.length,
        'first 5 of sigArrays[0]:', sigArrays[0] && sigArrays[0].slice(0, 5),
        'true count:', sigArrays[0] ? sigArrays[0].filter(Boolean).length : 0,
        '/', total);
    }
    // Warm-up: skip the first 78 candles so indicator arrays are stable
    // (MACD needs 3×26 = 78 bars — matches BacktestEngine's `warmup = 78`
    // exactly so the first eligible entry candle is identical to the
    // Python reference).
    const warmup = Math.min(78, Math.floor(total / 4));
    for (let i = 0; i < total; i++) {
      const candle = this.candles[i];
      const hadOpenDealAtStart = !!this.open_deal;
      if (this.open_deal) {
        const closed = this._checkTp(this.open_deal, candle)
                    || this._checkSl(this.open_deal, candle);
        if (closed) _btStatCloses++;
        if (!closed && this.open_deal) this._checkDca(this.open_deal, candle);
      }
      // Entry check — runs on every candle after warmup where no deal
      // is currently open. Critically, this ALSO fires on the same
      // candle where a deal just closed (hadOpenDealAtStart && !open_deal)
      // so a TP/SL close is immediately followed by a new entry at
      // this.candles[i].close. Python's _process_candle does the same:
      // monitor open deals, then entry-check if none remain. The
      // re-entry path is load-bearing for tight-TP strategies like
      // ASAP with take_profit.target_pct ≤ 1% — without it a 6-year
      // run shrinks from ~hundreds of deals down to a handful.
      if (!this.open_deal && i >= warmup) {
        // Look-ahead guard: Python's BacktestEngine evaluates indicators
        // on candles strictly BEFORE the current driving candle (see
        // _ohlc_up_to: `candles[ptr].timestamp < cur_ts`). Mirror that
        // by reading the signal at index i-1 — the most recently
        // closed bar — instead of i. Without this the JS engine
        // peeks at the current candle's RSI/MACD/EMA values, which
        // shifts the entry timing one bar earlier and inflates the
        // deal count compared to the authoritative Python run.
        const sigIdx = i - 1;
        let entry = sigIdx >= 0;
        if (entry) {
          for (const a of sigArrays) { if (!a[sigIdx]) { entry = false; break; } }
        }
        if (entry) {
          _btStatEntrySignalTrue++;
          if (hadOpenDealAtStart) _btStatSameCandleReopens++;
          if (window._BT_DEBUG && this.closed_deals.length < 10) {
            console.log('[BT] open deal #', this._deal_counter + 1,
              'at candle', i, 'price', candle.close);
          }
          this._openDeal(candle.close, candle.time, i);
          _btStatOpens++;
        } else if (i >= warmup) {
          _btStatEntryBlocked++;
        }
      }
      this.equity_curve.push({
        time: candle.time,
        balance: this.balance_btc + this._unrealizedPnl(candle.close),
      });
      if (i % 50 === 0 && onProgress) {
        const pct = total > 0 ? (i / total) * 100 : 0;
        onProgress(
          pct,
          `Simulating trades… candle ${i.toLocaleString()} / ${total.toLocaleString()}`,
        );
        // Yield to the event loop every 50 candles so the browser can
        // repaint the progress bar and stay responsive on long runs.
        await new Promise(r => setTimeout(r, 0));
      }
    }
    // Leave any still-open deal exactly as-is at end-of-data instead
    // of force-closing it on the final candle. The old behaviour
    // pretended the operator would have sold at the last close,
    // which inflated (or tanked) PnL based on one arbitrary bar.
    // The summary now surfaces the leftover position separately so
    // the UI can warn that it was NOT included in the PnL figure.
    let openDealsAtEnd = 0;
    let openDealsSizeBtc = 0;
    if (this.open_deal) {
      openDealsAtEnd = 1;
      openDealsSizeBtc = this._totalSize(this.open_deal);
    }
    console.info(
      '[BT] run summary: candles=%d warmup=%d opens=%d closes=%d ' +
      'same-candle-reopens=%d entry-signal-true=%d entry-blocked=%d',
      total, warmup, _btStatOpens, _btStatCloses,
      _btStatSameCandleReopens, _btStatEntrySignalTrue, _btStatEntryBlocked,
    );
    if (onProgress) onProgress(100, 'Calculating statistics…');
    return this._buildResults({ openDealsAtEnd, openDealsSizeBtc });
  }

  _buildResults(extras = {}) {
    const deals = this.closed_deals;
    const wins = deals.filter(d => d.pnl_btc > 0).length;
    const losses = deals.filter(d => d.pnl_btc < 0).length;
    const totalPnlBtc = deals.reduce((s, d) => s + d.pnl_btc, 0);
    const totalPnlPct = this.initial_balance_btc > 0
      ? (totalPnlBtc / this.initial_balance_btc) * 100 : 0;
    const winRate = deals.length ? (wins / deals.length) * 100 : 0;
    const durationsHours = deals.map(d => (d.closed_at - d.opened_at) / 3600);
    const avgDurationHours = durationsHours.length
      ? durationsHours.reduce((s, v) => s + v, 0) / durationsHours.length
      : 0;
    const maxDurationHours = durationsHours.length
      ? Math.max(...durationsHours)
      : 0;
    // Max drawdown from the equity curve
    let peak = this.initial_balance_btc, maxDd = 0;
    for (const p of this.equity_curve) {
      if (p.balance > peak) peak = p.balance;
      if (peak > 0) {
        const dd = (peak - p.balance) / peak * 100;
        if (dd > maxDd) maxDd = dd;
      }
    }
    // Buy & hold curve
    const buyHoldCurve = [];
    if (this.candles.length) {
      const p0 = this.candles[0].close;
      for (const c of this.candles) {
        buyHoldCurve.push({
          time: c.time,
          balance: this.initial_balance_btc * (c.close / p0),
        });
      }
    }
    const buyHoldEnd = buyHoldCurve.length ? buyHoldCurve[buyHoldCurve.length - 1].balance : this.initial_balance_btc;
    const buyHoldPnlBtc = buyHoldEnd - this.initial_balance_btc;
    const buyHoldPnlPct = this.initial_balance_btc > 0
      ? (buyHoldPnlBtc / this.initial_balance_btc) * 100 : 0;

    // Profit factor / Sharpe / Sortino
    const grossWin = deals.filter(d => d.pnl_btc > 0).reduce((s, d) => s + d.pnl_btc, 0);
    const grossLoss = Math.abs(deals.filter(d => d.pnl_btc < 0).reduce((s, d) => s + d.pnl_btc, 0));
    const profitFactor = grossLoss > 0 ? (grossWin / grossLoss) : (grossWin > 0 ? Infinity : 0);

    const pctReturns = deals.map(d => d.pnl_pct);
    let sharpe = '—', sortino = '—';
    if (pctReturns.length >= 2) {
      const mean = pctReturns.reduce((s, v) => s + v, 0) / pctReturns.length;
      const variance = pctReturns.reduce((s, v) => s + (v - mean) * (v - mean), 0) / pctReturns.length;
      const sd = Math.sqrt(variance);
      sharpe = sd > 0 ? ((mean / sd) * Math.sqrt(252)).toFixed(2) : '—';
      const losses = pctReturns.filter(v => v < 0);
      if (losses.length === 0) {
        sortino = '∞';
      } else {
        const lMean = 0;
        const lVar = losses.reduce((s, v) => s + (v - lMean) * (v - lMean), 0) / losses.length;
        const lSd = Math.sqrt(lVar);
        sortino = lSd > 0 ? ((mean / lSd) * Math.sqrt(252)).toFixed(2) : '∞';
      }
    }

    // Win / loss streaks
    let maxWinStreak = 0, maxLossStreak = 0, curW = 0, curL = 0;
    for (const d of deals) {
      if (d.pnl_btc > 0) { curW += 1; curL = 0; if (curW > maxWinStreak) maxWinStreak = curW; }
      else if (d.pnl_btc < 0) { curL += 1; curW = 0; if (curL > maxLossStreak) maxLossStreak = curL; }
      else { curW = 0; curL = 0; }
    }
    const bestDeal = deals.reduce((m, d) => (d.pnl_btc > (m ? m.pnl_btc : -Infinity) ? d : m), null);
    const worstDeal = deals.reduce((m, d) => (d.pnl_btc < (m ? m.pnl_btc : Infinity) ? d : m), null);

    // ── Extended ratios ────────────────────────────────────────────────
    // Expectancy, avg W/L, omega, calmar, recovery — all derived from
    // the closed-deals list so they're consistent with the other
    // summary numbers above. Each one has an "undefined if we don't
    // have the inputs" branch so the UI can render an em-dash instead
    // of a divide-by-zero NaN.
    const winBtcs  = deals.filter(d => d.pnl_btc > 0).map(d => d.pnl_btc);
    const lossBtcs = deals.filter(d => d.pnl_btc < 0).map(d => d.pnl_btc);
    const avgWinBtc  = winBtcs.length
      ? winBtcs.reduce((s, v) => s + v, 0) / winBtcs.length : 0;
    const avgLossBtc = lossBtcs.length
      ? Math.abs(lossBtcs.reduce((s, v) => s + v, 0) / lossBtcs.length) : 0;
    const lossRate = deals.length ? (lossBtcs.length / deals.length) : 0;
    const winRateFrac = deals.length ? (winBtcs.length / deals.length) : 0;
    const expectancyBtc = deals.length
      ? winRateFrac * avgWinBtc - lossRate * avgLossBtc
      : 0;
    const avgWinLossRatio = avgLossBtc > 0
      ? avgWinBtc / avgLossBtc
      : (avgWinBtc > 0 ? Infinity : 0);
    const posReturnSum = pctReturns.filter(v => v > 0).reduce((s, v) => s + v, 0);
    const negReturnSum = Math.abs(pctReturns.filter(v => v < 0).reduce((s, v) => s + v, 0));
    const omegaRatio = negReturnSum > 0
      ? posReturnSum / negReturnSum
      : (posReturnSum > 0 ? Infinity : 0);
    // Calmar = annualised return / max drawdown. Annualise the total
    // PnL% by the true backtest span in years so a short run doesn't
    // read as a monster ratio just because there aren't many deals
    // yet.
    let calmarRatio = 0;
    if (this.candles.length >= 2) {
      const spanSec = this.candles[this.candles.length - 1].time - this.candles[0].time;
      const spanYears = spanSec / (365.25 * 86400);
      if (spanYears > 0) {
        const annualPct = totalPnlPct / spanYears;
        if (maxDd > 0) calmarRatio = annualPct / maxDd;
        else calmarRatio = annualPct > 0 ? Infinity : 0;
      }
    }
    // Recovery factor = total PnL / max drawdown (in BTC terms).
    const maxDrawdownBtc = this.initial_balance_btc * maxDd / 100;
    const recoveryFactor = maxDrawdownBtc > 0
      ? totalPnlBtc / maxDrawdownBtc
      : (totalPnlBtc > 0 ? Infinity : 0);

    // Monthly PnL
    const monthlyMap = new Map();
    for (const d of deals) {
      const dt = new Date(d.closed_at * 1000);
      const key = `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, '0')}`;
      monthlyMap.set(key, (monthlyMap.get(key) || 0) + d.pnl_btc);
    }
    const monthlyPnl = Array.from(monthlyMap.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([month, pnl_btc]) => ({
        month, pnl_btc,
        pnl_pct: this.initial_balance_btc > 0 ? (pnl_btc / this.initial_balance_btc) * 100 : 0,
      }));

    // PnL histogram — 1% buckets, clipped to [-20, +20]
    const histMap = new Map();
    for (const d of deals) {
      const b = Math.max(-20, Math.min(20, Math.round(d.pnl_pct)));
      histMap.set(b, (histMap.get(b) || 0) + 1);
    }
    const pnlHistogram = [];
    for (let b = -20; b <= 20; b++) {
      pnlHistogram.push({ bucket_pct: b, count: histMap.get(b) || 0 });
    }

    // DCA price levels — count occurrences of DCA fill prices
    const dcaMap = new Map();
    for (const d of deals) {
      for (const o of d.orders) {
        if (o.type !== 'dca') continue;
        const p = Math.round(o.price);
        dcaMap.set(p, (dcaMap.get(p) || 0) + 1);
      }
    }
    const dcaLevels = Array.from(dcaMap.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 10)
      .map(([price, count]) => ({ price, count }));

    return {
      summary: {
        total_pnl_btc: totalPnlBtc,
        total_pnl_pct: totalPnlPct,
        win_rate: winRate,
        total_deals: deals.length,
        wins, losses,
        avg_duration_hours: avgDurationHours,
        max_duration_hours: maxDurationHours,
        total_fees_btc: deals.reduce((s, d) => s + (d.total_fees_btc || 0), 0),
        max_drawdown_pct: maxDd,
        max_drawdown_btc: maxDrawdownBtc,
        buy_and_hold_pnl_btc: buyHoldPnlBtc,
        buy_and_hold_pnl_pct: buyHoldPnlPct,
        open_deals_at_end: extras.openDealsAtEnd || 0,
        open_deals_size_btc: extras.openDealsSizeBtc || 0,
      },
      ratios: {
        profit_factor: profitFactor,
        sharpe, sortino,
        calmar_ratio: calmarRatio,
        recovery_factor: recoveryFactor,
        expectancy_btc: expectancyBtc,
        avg_win_loss_ratio: avgWinLossRatio,
        omega_ratio: omegaRatio,
        max_consecutive_wins: maxWinStreak,
        max_consecutive_losses: maxLossStreak,
        best_deal_pnl_btc: bestDeal ? bestDeal.pnl_btc : 0,
        worst_deal_pnl_btc: worstDeal ? worstDeal.pnl_btc : 0,
      },
      equity_curve: this.equity_curve,
      buy_hold_curve: buyHoldCurve,
      monthly_pnl: monthlyPnl,
      deals,
      pnl_histogram: pnlHistogram,
      dca_levels: dcaLevels,
    };
  }
}

// ── Backtest UI glue ────────────────────────────────────────────────────────

function btShowError(elId, msg) {
  const el = $(elId);
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('hidden');
}
function btClearError(elId) {
  const el = $(elId);
  if (el) el.classList.add('hidden');
}

// Per-timeframe default candle counts. Picked so the operator always
// gets a meaningful span of historical data even without touching the
// date picker: fine-grained bars cover months, coarse bars cover years.
const BT_TF_DEFAULT_LIMIT = {
  '15m': 10000, '30m': 8000,  '1h':  8760,
  '2h':  8760,  '4h':  8760,  '12h': 5000,
  '1d':  3650,  '3d':  2000,  '1w':  1000,
};
const BT_TF_SECONDS = {
  '15m': 900,   '30m': 1800,  '1h':  3600,
  '2h':  7200,  '4h':  14400, '12h': 43200,
  '1d':  86400, '3d':  259200,'1w':  604800,
};

function btDefaultLimit(tf) { return BT_TF_DEFAULT_LIMIT[tf] || 5000; }

// Upper bound on a single backtest fetch. Matches the backend
// _CANDLES_MAX_BARS cap; exceeding it just wastes a round-trip since
// the server would clamp anyway.
const BT_MAX_CANDLES = 300000;

function btCandleCountForRange(tf, startStr, endStr) {
  const tfSec = BT_TF_SECONDS[tf];
  if (!tfSec || !startStr || !endStr) return 0;
  const sMs = new Date(startStr + 'T00:00:00Z').getTime();
  const eMs = new Date(endStr + 'T23:59:59Z').getTime();
  if (!(eMs > sMs)) return 0;
  return Math.min(BT_MAX_CANDLES, Math.floor((eMs - sMs) / 1000 / tfSec));
}

function btEstimatedSeconds(candleCount) {
  // Fetch: ~0.15s per 1000-candle ccxt page (matches backend sleep).
  // Simulation: ~1ms per candle of JS engine work, clamped loosely.
  const fetchSec = Math.ceil(candleCount / 1000) * 0.15;
  const simSec   = candleCount / 5000;
  return fetchSec + simSec;
}

function btHumanSpan(days) {
  if (days < 1) return 'less than a day';
  if (days < 60) return `${days.toLocaleString()} day${days === 1 ? '' : 's'}`;
  if (days < 730) return `${Math.round(days / 30)} months`;
  return `${(days / 365).toFixed(1)} years`;
}

function btHumanDuration(sec) {
  if (sec < 1) return '<1s';
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec - m * 60);
  return s ? `${m}m ${s}s` : `${m}m`;
}

function btUpdateTfWarning(tfSelectId, startId, endId, warnId) {
  const tf = $(tfSelectId) && $(tfSelectId).value;
  const warn = $(warnId);
  if (!tf || !warn) return;
  const tfSec = BT_TF_SECONDS[tf];
  if (!tfSec) { warn.classList.add('hidden'); warn.textContent = ''; return; }
  const startStr = $(startId) && $(startId).value;
  const endStr = $(endId) && $(endId).value;
  const candleCount = btCandleCountForRange(tf, startStr, endStr)
    || btDefaultLimit(tf);
  const est = btEstimatedSeconds(candleCount);
  if (est > 30) {
    warn.textContent =
      `⚠ This backtest will fetch ~${candleCount.toLocaleString()} ` +
      `candles (est. ${btHumanDuration(est)}). This may take a few minutes.`;
    warn.classList.remove('hidden');
  } else {
    warn.textContent = '';
    warn.classList.add('hidden');
  }
}

async function btFetchCandles(pair, tf, startIso, endIso, limit) {
  const effectiveLimit = limit || btDefaultLimit(tf);
  const params = new URLSearchParams({
    start: startIso, end: endIso, limit: String(effectiveLimit),
  });
  // FastAPI path params don't survive %2F: the router still splits on
  // it and "BTC/USD" routes to pair="BTC" + timeframe="USD/...".
  // Send the slash-less form and let the backend's
  // _normalize_chart_pair() re-insert the slash server-side, exactly
  // like the /api/chart endpoint already does.
  const r = await fetch(`/api/candles/${_pairForUrl(pair)}/${tf}?${params}`);
  if (r.status === 401) { _handle401(); throw new Error('Not authenticated'); }
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.json();
}

function _btComposeIso(dateStr, timeStr, fallback) {
  // Combine a YYYY-MM-DD date picker value and a HH:MM time picker
  // value into a UTC ISO timestamp. Empty time falls back to the
  // caller-supplied default so the operator still gets the full day
  // if they ignore the time field.
  const t = (timeStr && /^\d\d:\d\d/.test(timeStr)) ? timeStr : fallback;
  return new Date(`${dateStr}T${t}:00Z`).toISOString();
}

async function btRunFromTab() {
  btClearError('bt-error');
  const startStr = $('bt-start').value;
  const endStr = $('bt-end').value;
  const startTimeStr = ($('bt-start-time') && $('bt-start-time').value) || '00:00';
  const endTimeStr   = ($('bt-end-time')   && $('bt-end-time').value)   || '23:59';
  const tf = $('bt-tf').value;
  const balance = parseFloat($('bt-balance').value);
  if (!startStr || !endStr) { btShowError('bt-error', 'Pick start and end dates'); return; }
  if (!(balance > 0)) { btShowError('bt-error', 'Balance must be > 0'); return; }

  // Pull the bot config from the detail cache; fall back to a refetch.
  let cfg = null;
  if (_detailConfigCache && _detailConfigCache.bot) cfg = _detailConfigCache.bot;
  if (!cfg && currentSlug) {
    try {
      const r = await fetch(`/api/bots/${currentSlug}/config`);
      if (r.ok) { const j = await r.json(); cfg = j.bot || j; }
    } catch (e) {}
  }
  if (!cfg) { btShowError('bt-error', 'No bot config available'); return; }
  const pair = cfg.pair || 'BTC/USD';

  const startIso = _btComposeIso(startStr, startTimeStr, '00:00');
  const endIso   = _btComposeIso(endStr,   endTimeStr,   '23:59');

  const rangeLimit = btCandleCountForRange(tf, startStr, endStr)
    || btDefaultLimit(tf);
  await btRunPipeline({
    cfg, pair, tf, startIso, endIso, balance, limit: rangeLimit,
    loaderId: 'bt-loader', progressBarId: 'bt-progress-bar',
    statusId: 'bt-status', resultsId: 'bt-results',
    errorId: 'bt-error', mode: 'tab',
  });
}

async function btRunFromWizard() {
  btClearError('wbt-error');
  nbReadAll();
  const body = nbBuildBotConfig();
  const cfg = body.bot;
  const startStr = $('wbt-start').value;
  const endStr = $('wbt-end').value;
  const startTimeStr = ($('wbt-start-time') && $('wbt-start-time').value) || '00:00';
  const endTimeStr   = ($('wbt-end-time')   && $('wbt-end-time').value)   || '23:59';
  const tf = $('wbt-tf').value;
  const balance = parseFloat($('wbt-balance').value);
  if (!startStr || !endStr) { btShowError('wbt-error', 'Pick start and end dates'); return; }
  if (!(balance > 0)) { btShowError('wbt-error', 'Balance must be > 0'); return; }
  const pair = cfg.pair || 'BTC/USD';
  const startIso = _btComposeIso(startStr, startTimeStr, '00:00');
  const endIso   = _btComposeIso(endStr,   endTimeStr,   '23:59');
  const rangeLimit = btCandleCountForRange(tf, startStr, endStr)
    || btDefaultLimit(tf);
  await btRunPipeline({
    cfg, pair, tf, startIso, endIso, balance, limit: rangeLimit,
    loaderId: 'wbt-loader', progressBarId: 'wbt-progress-bar',
    statusId: 'wbt-status', resultsId: 'wbt-results',
    errorId: 'wbt-error', mode: 'wizard',
  });
}

function btOpenWizardModal() {
  $('wizard-backtest-modal').classList.add('show');
  // Reset panels
  $('wbt-loader').classList.add('hidden');
  $('wbt-results').classList.add('hidden');
  btClearError('wbt-error');
}

// Charts held between runs so re-running disposes the previous chart
// cleanly instead of stacking them on top of each other.
let _btEquityChart = null;
let _btMonthlyChart = null;
let _wbtEquityChart = null;

// Per-slug backtest result cache so jumping between bots doesn't
// leak bot A's stats into bot B's Backtest tab. The last run for
// each slug is re-rendered whenever openBot lands on that slug.
const _btResultsBySlug = {};

// Backtest History sub-view state — an in-memory sort handle plus
// the last-fetched rows, so clicking a header just re-renders from
// cache instead of hitting the network again.
const BT_HISTORY_COLUMNS = [
  { key: 'bot_name',            label: 'Bot',       fmt: v => safeText(String(v || '—')) },
  { key: 'created_at',          label: 'Run',       fmt: v => safeText((v || '').slice(0, 16).replace('T', ' ')) },
  { key: 'start_date',          label: 'Start',     fmt: v => safeText((v || '').slice(0, 10)) },
  { key: 'end_date',            label: 'End',       fmt: v => safeText((v || '').slice(0, 10)) },
  { key: 'timeframe',           label: 'TF',        fmt: v => safeText(String(v || '—')) },
  { key: 'total_deals',         label: 'Deals',     fmt: v => String(v ?? 0) },
  { key: 'win_rate',            label: 'Win %',     fmt: v => (v != null ? v.toFixed(1) + '%' : '—') },
  { key: 'total_pnl_btc',       label: 'PnL BTC',   fmt: v => _btColouredBtc(v) },
  { key: 'total_pnl_pct',       label: 'PnL %',     fmt: v => _btColouredPct(v) },
  { key: 'profit_factor',       label: 'PF',        fmt: v => _fmtRatio(v) },
  { key: 'sharpe_ratio',        label: 'Sharpe',    fmt: v => _fmtRatio(v) },
  { key: 'max_drawdown_pct',    label: 'Max DD',    fmt: v => (v != null ? v.toFixed(2) + '%' : '—') },
  { key: 'buy_hold_pnl_pct',    label: 'B&H %',     fmt: v => _btColouredPct(v) },
];
let _btHistoryRows = [];
let _btHistorySortKey = 'created_at';
let _btHistorySortDir = 'desc';

function _btColouredBtc(v) {
  if (v == null) return '—';
  const cls = v >= 0 ? 'bt-history-num-pos' : 'bt-history-num-neg';
  const sign = v >= 0 ? '+' : '';
  return `<span class="${cls}">${sign}${v.toFixed(6)}</span>`;
}
function _btColouredPct(v) {
  if (v == null) return '—';
  const cls = v >= 0 ? 'bt-history-num-pos' : 'bt-history-num-neg';
  const sign = v >= 0 ? '+' : '';
  return `<span class="${cls}">${sign}${v.toFixed(2)}%</span>`;
}

async function btOpenHistoryPanel() {
  $('bot-grid').classList.add('hidden');
  $('bt-history-panel').classList.remove('hidden');
  const body = $('bt-history-body');
  body.innerHTML = '<tr><td colspan="13" class="empty-config-msg">Loading…</td></tr>';
  try {
    const r = await fetch('/api/backtest/runs?limit=200');
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    _btHistoryRows = Array.isArray(j.runs) ? j.runs : [];
  } catch (e) {
    body.innerHTML =
      `<tr><td colspan="13" class="empty-config-msg">Failed to load: ${safeText(e.message || e)}</td></tr>`;
    return;
  }
  _btRenderHistoryTable();
}

function btCloseHistoryPanel() {
  $('bt-history-panel').classList.add('hidden');
  $('bot-grid').classList.remove('hidden');
}

function _btRenderHistoryTable() {
  const head = $('bt-history-head');
  head.innerHTML = BT_HISTORY_COLUMNS.map(col => {
    const dir = col.key === _btHistorySortKey
      ? `<span class="bt-sort-dir">${_btHistorySortDir === 'asc' ? '▲' : '▼'}</span>`
      : '';
    return `<th data-key="${col.key}">${safeText(col.label)}${dir}</th>`;
  }).join('');
  head.querySelectorAll('th').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.key;
      if (_btHistorySortKey === k) {
        _btHistorySortDir = _btHistorySortDir === 'asc' ? 'desc' : 'asc';
      } else {
        _btHistorySortKey = k;
        _btHistorySortDir = 'desc';
      }
      _btRenderHistoryTable();
    });
  });

  const sorted = _btHistoryRows.slice().sort((a, b) => {
    const av = a[_btHistorySortKey];
    const bv = b[_btHistorySortKey];
    const an = typeof av === 'number';
    const bn = typeof bv === 'number';
    let cmp;
    if (an && bn) cmp = av - bv;
    else cmp = String(av ?? '').localeCompare(String(bv ?? ''));
    return _btHistorySortDir === 'asc' ? cmp : -cmp;
  });

  const body = $('bt-history-body');
  if (!sorted.length) {
    body.innerHTML = '<tr><td colspan="13" class="empty-config-msg">No backtest runs yet. Run a backtest on any bot to populate this view.</td></tr>';
    return;
  }
  body.innerHTML = sorted.map(run => {
    const cells = BT_HISTORY_COLUMNS.map(col => `<td>${col.fmt(run[col.key])}</td>`).join('');
    return `<tr data-slug="${safeText(run.bot_slug || '')}">${cells}</tr>`;
  }).join('');
  body.querySelectorAll('tr[data-slug]').forEach(tr => {
    tr.addEventListener('click', () => {
      const slug = tr.dataset.slug;
      if (!slug) return;
      btCloseHistoryPanel();
      openBot(slug);
      // Jump straight into the Backtest tab on the next tick so the
      // detail layout has settled by the time we switch.
      setTimeout(() => {
        const tabBtn = document.querySelector('.detail-subnav .tab[data-dtab="backtest"]');
        if (tabBtn) showDTab('backtest', tabBtn);
      }, 60);
    });
  });
}

function btRestoreResultsForSlug(slug) {
  const resultsEl = $('bt-results');
  if (!resultsEl) return;
  const cached = slug && _btResultsBySlug[slug];
  if (cached) {
    resultsEl.classList.remove('hidden');
    btRenderResults(cached);
  } else {
    resultsEl.classList.add('hidden');
    const grid = $('bt-summary-grid'); if (grid) grid.innerHTML = '';
    const ratios = $('bt-ratios-grid'); if (ratios) ratios.innerHTML = '';
    const note = $('bt-open-deals-note');
    if (note) { note.textContent = ''; note.classList.add('hidden'); }
    if (_btEquityChart) { try { _btEquityChart.remove(); } catch (e) {} _btEquityChart = null; }
    if (_btMonthlyChart) { try { _btMonthlyChart.remove(); } catch (e) {} _btMonthlyChart = null; }
    const eqEl = $('bt-equity-chart'); if (eqEl) eqEl.innerHTML = '';
  }
}

// Flatten res.summary + res.ratios into a single dict so the POST
// body matches the backtest_runs column names save_backtest_run
// expects. Infinity / non-finite values become null so JSON stays
// well-formed.
function _btFlattenForSave(res) {
  const out = {};
  const clean = v => (typeof v === 'number' && Number.isFinite(v) ? v : (v === '∞' || v === Infinity ? null : v));
  for (const [k, v] of Object.entries(res.summary || {})) out[k] = clean(v);
  for (const [k, v] of Object.entries(res.ratios || {})) {
    // Map JS-side camelSuffixed ratio keys onto the SQL column names
    // save_backtest_run uses so the row lines up without a second
    // rename step.
    if (k === 'sharpe') out.sharpe_ratio = clean(v);
    else if (k === 'sortino') out.sortino_ratio = clean(v);
    else out[k] = clean(v);
  }
  return out;
}

async function _btSaveRun(cfg, params, res) {
  if (!cfg || !res || !res.summary) return null;
  try {
    const r = await fetch('/api/backtest/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        slug: cfg.slug || params.slug || '',
        name: cfg.name || params.name || 'Backtest',
        params,
        summary: _btFlattenForSave(res),
      }),
    });
    if (!r.ok) return null;
    const j = await r.json();
    return j.id || null;
  } catch (e) { return null; }
}

function _btFlashSaved(statusId) {
  const el = $(statusId);
  if (!el) return;
  el.textContent = '✓ Saved';
  el.classList.add('bt-status-saved');
  setTimeout(() => el.classList.remove('bt-status-saved'), 2000);
}

async function btRunPipeline(opts) {
  const {
    cfg, pair, tf, startIso, endIso, balance, limit,
    loaderId, progressBarId, statusId, resultsId, errorId, mode,
  } = opts;
  const loader = $(loaderId);
  const results = $(resultsId);
  const bar = $(progressBarId);
  const status = $(statusId);

  results.classList.add('hidden');
  loader.classList.remove('hidden');
  bar.classList.add('bt-progress-bar');
  // Reset width via style attribute is CSP-unfriendly — use a helper
  // class toggled by swapping inline style is not allowed either.
  // Instead, set the bar width via JS by toggling a CSS var on an
  // ancestor. Simpler: recreate the bar element with a fresh 0 width.
  bar.style.width = '0%';   // eslint-disable-line -- CSP allows style *property* writes
  const estSec = btEstimatedSeconds(limit || 0);
  status.textContent =
    `Fetching ~${(limit || 0).toLocaleString()} candles from Bitget ` +
    `(est. ${btHumanDuration(estSec)})…`;

  // Fake-progress the fetch phase: we don't get streamed page events
  // from the backend, so tick the bar forward in small increments while
  // the fetch is in flight. 0–50% belongs to the fetch, 50–100% to
  // the simulation — that split keeps the bar moving on both ends
  // instead of stalling at 0 while the operator waits on Bitget.
  let fetchTicker = 0;
  const fetchInterval = setInterval(() => {
    fetchTicker = Math.min(fetchTicker + 1, 48);
    bar.style.width = fetchTicker + '%';
  }, Math.max(200, (estSec * 1000) / 50));

  try {
    const candles = await btFetchCandles(pair, tf, startIso, endIso, limit);
    clearInterval(fetchInterval);
    bar.style.width = '50%';
    if (!candles || candles.length < 50) {
      throw new Error(`Not enough candles (${candles ? candles.length : 0}) for backtest`);
    }
    status.textContent = `Fetched ${candles.length.toLocaleString()} candles, starting simulation…`;
    const engine = new RevertoBacktest(cfg, candles);
    const result = await engine.run(balance, (pct, msg) => {
      // Remap engine 0–100% into 50–100% so the fetch phase owns
      // the first half of the progress bar and the simulation the
      // second half.
      bar.style.width = (50 + pct / 2) + '%';
      status.textContent = msg;
    });
    bar.style.width = '100%';
    loader.classList.add('hidden');
    results.classList.remove('hidden');
    if (mode === 'tab') {
      if (currentSlug) _btResultsBySlug[currentSlug] = result;
      btRenderResults(result);
    } else {
      btRenderWizardResults(result);
    }
    // Auto-persist every successful run so the Backtest History view
    // has a stable timeline to show. Fire-and-forget — a backend
    // hiccup shouldn't hide the just-computed numbers.
    _btSaveRun(
      { ...cfg, slug: (cfg && cfg.slug) || currentSlug || '' },
      {
        start_date: startIso,
        end_date: endIso,
        timeframe: tf,
        initial_balance_btc: balance,
      },
      result,
    ).then(id => { if (id != null) _btFlashSaved(statusId); });
  } catch (e) {
    clearInterval(fetchInterval);
    loader.classList.add('hidden');
    btShowError(errorId, 'Backtest failed: ' + (e.message || e));
  }
}

function _btCard(label, value, sub) {
  return `<div class="card">
    <div class="card-label">${safeText(label)}</div>
    <div class="card-value">${value}</div>
    <div class="card-sub">${safeText(sub || '')}</div>
  </div>`;
}

function _fmtBtc(v, d = 6) { return (v >= 0 ? '+' : '') + v.toFixed(d) + ' BTC'; }
function _fmtPct(v, d = 2) { return (v >= 0 ? '+' : '') + v.toFixed(d) + '%'; }
function _fmtRatio(v, d = 2) {
  if (v === Infinity) return '∞';
  if (!Number.isFinite(v)) return '—';
  return v.toFixed(d);
}

function btRenderOpenDealsNote(noteId, s) {
  const el = $(noteId);
  if (!el) return;
  if (s.open_deals_at_end > 0) {
    el.textContent =
      `ℹ ${s.open_deals_at_end} deal${s.open_deals_at_end === 1 ? '' : 's'} ` +
      `still open at end of backtest period ` +
      `(${s.open_deals_size_btc.toFixed(6)} BTC, not included in PnL)`;
    el.classList.remove('hidden');
  } else {
    el.textContent = '';
    el.classList.add('hidden');
  }
}

function btRenderResults(res) {
  const s = res.summary, r = res.ratios;
  btRenderOpenDealsNote('bt-open-deals-note', s);
  $('bt-summary-grid').innerHTML = [
    _btCard('Total PnL', _fmtBtc(s.total_pnl_btc), _fmtPct(s.total_pnl_pct)),
    _btCard('Win rate', s.win_rate.toFixed(1) + '%', `${s.wins}W / ${s.losses}L`),
    _btCard('Total deals', String(s.total_deals), 'closed'),
    _btCard('Avg duration', s.avg_duration_hours.toFixed(1) + 'h', 'per deal'),
    _btCard('Max duration', (s.max_duration_hours || 0).toFixed(1) + 'h', 'longest deal'),
    _btCard('Total fees', s.total_fees_btc.toFixed(8) + ' BTC', 'taker'),
    _btCard('Max drawdown', s.max_drawdown_pct.toFixed(2) + '%', 'equity peak'),
    _btCard('Buy & hold', _fmtBtc(s.buy_and_hold_pnl_btc), _fmtPct(s.buy_and_hold_pnl_pct)),
  ].join('');

  $('bt-ratios-grid').innerHTML = [
    _btCard('Profit factor', _fmtRatio(r.profit_factor), 'gross win / loss'),
    _btCard('Sharpe', String(r.sharpe), 'annualised'),
    _btCard('Sortino', String(r.sortino), 'downside'),
    _btCard('Calmar', _fmtRatio(r.calmar_ratio), 'annual / max DD'),
    _btCard('Recovery', _fmtRatio(r.recovery_factor), 'PnL / max DD'),
    _btCard('Expectancy', _fmtBtc(r.expectancy_btc || 0, 8), 'per deal'),
    _btCard('Avg W/L ratio', _fmtRatio(r.avg_win_loss_ratio), 'avg win / avg loss'),
    _btCard('Omega', _fmtRatio(r.omega_ratio), 'upside / downside'),
    _btCard('Win streak', String(r.max_consecutive_wins), 'max'),
    _btCard('Loss streak', String(r.max_consecutive_losses), 'max'),
    _btCard('Best deal', _fmtBtc(r.best_deal_pnl_btc), ''),
    _btCard('Worst deal', _fmtBtc(r.worst_deal_pnl_btc), ''),
  ].join('');

  // Equity curve chart
  const eqEl = $('bt-equity-chart');
  if (_btEquityChart) { try { _btEquityChart.remove(); } catch (e) {} _btEquityChart = null; }
  eqEl.innerHTML = '';
  if (typeof LightweightCharts !== 'undefined') {
    _btEquityChart = LightweightCharts.createChart(eqEl, {
      ..._chartLayoutOpts(),
      width: eqEl.clientWidth || 800,
      height: 300,
    });
    const eqSeries = _btEquityChart.addLineSeries({
      color: _cssVar('--accent', '#26a69a'), lineWidth: 2,
    });
    eqSeries.setData(res.equity_curve.map(p => ({ time: p.time, value: p.balance })));
    const bhSeries = _btEquityChart.addLineSeries({
      color: _cssVar('--muted', '#888'), lineWidth: 1, lineStyle: 2,
    });
    bhSeries.setData(res.buy_hold_curve.map(p => ({ time: p.time, value: p.balance })));
    _btEquityChart.timeScale().fitContent();
  }

  // Monthly PnL histogram
  const mEl = $('bt-monthly-chart');
  if (_btMonthlyChart) { try { _btMonthlyChart.remove(); } catch (e) {} _btMonthlyChart = null; }
  mEl.innerHTML = '';
  if (typeof LightweightCharts !== 'undefined' && res.monthly_pnl.length) {
    _btMonthlyChart = LightweightCharts.createChart(mEl, {
      ..._chartLayoutOpts(),
      width: mEl.clientWidth || 800,
      height: 200,
    });
    const hist = _btMonthlyChart.addHistogramSeries({});
    const green = _cssVar('--accent', '#26a69a');
    const red = _cssVar('--red', '#ef5350');
    hist.setData(res.monthly_pnl.map(m => {
      // Month key to an arbitrary first-of-month UTC time
      const [y, mo] = m.month.split('-').map(Number);
      return {
        time: Math.floor(Date.UTC(y, mo - 1, 1) / 1000),
        value: m.pnl_btc,
        color: m.pnl_btc >= 0 ? green : red,
      };
    }));
    _btMonthlyChart.timeScale().fitContent();
  }

  // Deal table
  btRenderDealTable(res.deals);

  // PnL histogram — pure CSS bars
  const hEl = $('bt-pnl-hist');
  const maxCount = Math.max(1, ...res.pnl_histogram.map(b => b.count));
  hEl.innerHTML = res.pnl_histogram.map(b => {
    const h = (b.count / maxCount) * 100;
    const cls = b.bucket_pct < 0 ? 'bt-pnl-bar bt-pnl-bar-neg' : 'bt-pnl-bar';
    return `<div class="${cls}" data-h="${h.toFixed(1)}" title="${b.bucket_pct}%: ${b.count}"></div>`;
  }).join('');
  // Set heights via inline style (CSP allows direct style property).
  Array.from(hEl.querySelectorAll('.bt-pnl-bar')).forEach(el => {
    el.style.height = el.dataset.h + '%';
  });

  // DCA levels
  const dEl = $('bt-dca-levels');
  if (!res.dca_levels.length) {
    dEl.innerHTML = '<div class="empty-grid">No DCA fills</div>';
  } else {
    dEl.innerHTML = res.dca_levels.map(l =>
      `<div class="bt-dca-row"><span>$${fmtPrice(l.price)}</span><span>×${l.count}</span></div>`
    ).join('');
  }
}

function btRenderWizardResults(res) {
  const s = res.summary, r = res.ratios;
  btRenderOpenDealsNote('wbt-open-deals-note', s);
  $('wbt-summary-grid').innerHTML = [
    _btCard('Total PnL', _fmtBtc(s.total_pnl_btc), _fmtPct(s.total_pnl_pct)),
    _btCard('Win rate', s.win_rate.toFixed(1) + '%', `${s.wins}W / ${s.losses}L`),
    _btCard('Deals', String(s.total_deals), 'closed'),
    _btCard('Max DD', s.max_drawdown_pct.toFixed(2) + '%', ''),
  ].join('');
  const pf = r.profit_factor === Infinity ? '∞' :
             (typeof r.profit_factor === 'number' ? r.profit_factor.toFixed(2) : r.profit_factor);
  $('wbt-ratios-grid').innerHTML = [
    _btCard('Profit factor', pf, ''),
    _btCard('Sharpe', String(r.sharpe), ''),
    _btCard('Best deal', _fmtBtc(r.best_deal_pnl_btc), ''),
    _btCard('Worst deal', _fmtBtc(r.worst_deal_pnl_btc), ''),
  ].join('');

  const eqEl = $('wbt-equity-chart');
  if (_wbtEquityChart) { try { _wbtEquityChart.remove(); } catch (e) {} _wbtEquityChart = null; }
  eqEl.innerHTML = '';
  if (typeof LightweightCharts !== 'undefined') {
    _wbtEquityChart = LightweightCharts.createChart(eqEl, {
      ..._chartLayoutOpts(),
      width: eqEl.clientWidth || 600,
      height: 240,
    });
    const series = _wbtEquityChart.addLineSeries({
      color: _cssVar('--accent', '#26a69a'), lineWidth: 2,
    });
    series.setData(res.equity_curve.map(p => ({ time: p.time, value: p.balance })));
    const bh = _wbtEquityChart.addLineSeries({
      color: _cssVar('--muted', '#888'), lineWidth: 1, lineStyle: 2,
    });
    bh.setData(res.buy_hold_curve.map(p => ({ time: p.time, value: p.balance })));
    _wbtEquityChart.timeScale().fitContent();
  }
}

let _btDealSort = { key: 'id', dir: 'asc' };

function btRenderDealTable(deals) {
  const cols = loadColumns(BT_DEAL_LS_KEY, BT_DEAL_COLUMNS).filter(c => c.visible);
  const thead = $('bt-deals-thead');
  thead.innerHTML = cols.map(c =>
    `<th data-bt-sort="${c.key}">${safeText(c.label)}${_btDealSort.key === c.key ? (_btDealSort.dir === 'asc' ? ' ▲' : ' ▼') : ''}</th>`
  ).join('');
  thead.querySelectorAll('th').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.btSort;
      if (_btDealSort.key === k) _btDealSort.dir = _btDealSort.dir === 'asc' ? 'desc' : 'asc';
      else { _btDealSort.key = k; _btDealSort.dir = 'asc'; }
      btRenderDealTable(deals);
    });
  });
  const sorted = deals.slice();
  const k = _btDealSort.key;
  const sortVal = d => {
    switch (k) {
      case 'id':       return d.id;
      case 'opened':   return d.opened_at;
      case 'closed':   return d.closed_at;
      case 'duration': return d.closed_at - d.opened_at;
      case 'entry':    return d.entry_price;
      case 'avg':      return d.avg_entry_price;
      case 'close':    return d.close_price;
      case 'dcas':     return d.dca_count;
      case 'pnl_btc':  return d.pnl_btc;
      case 'pnl_pct':  return d.pnl_pct;
      case 'reason':   return d.reason;
      default:         return 0;
    }
  };
  sorted.sort((a, b) => {
    const va = sortVal(a), vb = sortVal(b);
    if (va < vb) return _btDealSort.dir === 'asc' ? -1 : 1;
    if (va > vb) return _btDealSort.dir === 'asc' ? 1 : -1;
    return 0;
  });
  const fmtTime = t => new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ');
  const fmtDur = s => {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return `${h}h${m}m`;
  };
  const tbody = $('bt-deals-tbody');
  if (!sorted.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${cols.length}">No deals</td></tr>`;
    return;
  }
  tbody.innerHTML = sorted.map(d => {
    const cells = {
      id:       `<td>${d.id}</td>`,
      opened:   `<td class="muted-cell">${safeText(fmtTime(d.opened_at))}</td>`,
      closed:   `<td class="muted-cell">${safeText(fmtTime(d.closed_at))}</td>`,
      duration: `<td>${safeText(fmtDur(d.closed_at - d.opened_at))}</td>`,
      entry:    `<td>${fmtPrice(d.entry_price)}</td>`,
      avg:      `<td>${fmtPrice(d.avg_entry_price)}</td>`,
      close:    `<td>${fmtPrice(d.close_price)}</td>`,
      dcas:     `<td>${d.dca_count}</td>`,
      pnl_btc:  `<td class="bt-pnl-cell">${d.pnl_btc >= 0 ? '+' : ''}${d.pnl_btc.toFixed(6)}</td>`,
      pnl_pct:  `<td class="bt-pnl-cell">${d.pnl_pct >= 0 ? '+' : ''}${d.pnl_pct.toFixed(2)}%</td>`,
      reason:   `<td class="muted-cell">${safeText(d.reason)}</td>`,
    };
    const cls = d.pnl_btc > 0 ? 'bt-deal-win' : (d.pnl_btc < 0 ? 'bt-deal-loss' : '');
    return `<tr class="${cls}">${cols.map(c => cells[c.key] || '<td></td>').join('')}</tr>`;
  }).join('');
}
