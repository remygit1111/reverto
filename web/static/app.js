// web/static/app.js — Reverto portal frontend
// Moved out of an inline <script> in index.html so CSP doesn't need
// 'unsafe-inline' on script-src. All event handlers are wired via
// addEventListener in setupEventListeners() — no onclick="..." attributes.

// ── Debug helpers ────────────────────────────────────────────────────────────
// Opt-in debug logging. Set `window._REVERTO_DEBUG = true` from the
// browser console to see _debug() output. Existing console.log calls
// guarded by `window._BT_DEBUG` keep their own gate — this helper is
// for new code that doesn't have domain-specific debug flags yet.
function _debug(...args) {
  if (window._REVERTO_DEBUG === true) {
    console.log('[REVERTO]', ...args);
  }
}

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

// ── CSRF token auto-inject (audit r1-073) ────────────────────────────────────
// Reads the non-HttpOnly ``reverto_csrf`` cookie and echoes it on
// every mutating fetch via the ``X-CSRF-Token`` header. The server
// compares cookie + header; mismatch → 403. Same-origin JS can read
// the cookie (that's the double-submit pattern) but a cross-origin
// attacker can't, so they can't mint a matching header.
function _getCsrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)reverto_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

(function _installCsrfFetchWrapper() {
  const _origFetch = window.fetch;
  const _mutating = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  window.fetch = function (input, init = {}) {
    const method = String((init && init.method) || 'GET').toUpperCase();
    if (_mutating.has(method)) {
      const token = _getCsrfToken();
      if (token) {
        const headers = new Headers(init.headers || {});
        if (!headers.has('X-CSRF-Token')) {
          headers.set('X-CSRF-Token', token);
        }
        init = Object.assign({}, init, { headers });
      }
    }
    return _origFetch.call(this, input, init);
  };
})();

// ── API Key management ────────────────────────────────────────────────────────
// The portal now uses session-cookie auth for browser users. The API key is
// kept around purely as an alternative for scripts and CLI tools that don't
// hold a session — set it via Profile → API Key. The SPA itself never sends
// Legacy — session cookies are now the primary auth mechanism.
// getApiKey reads from localStorage for backwards compatibility with
// operator scripts that use the X-API-Key header. New code should
// NOT write to localStorage; the key is set via the Profile modal
// which now only displays the server-side key without persisting it.
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
  // Legacy: no longer persisted to localStorage. The session cookie
  // handles portal auth; the API key is only for external scripts
  // which should set REVERTO_API_KEY in their own environment.
  closeApiKeyModal();
}
function clearApiKey() {
  localStorage.removeItem('reverto_api_key');
  document.getElementById('api-key-input').value = '';
  closeApiKeyModal();
}

// ── Session auth ──────────────────────────────────────────────────────────────
// Cached user id from /auth/status — lets nav-rendering decide whether to
// reveal admin-only links without a second round-trip. `null` means
// unauthenticated; the SPA route-guard short-circuits before any admin
// element would ever be queried in that state.
let _cachedUserId = null;

async function checkAuthStatus() {
  try {
    const r = await fetch('/auth/status');
    if (r.status === 401) return false;
    const j = await r.json();
    if (j && j.username) _cachedUsername = j.username;
    _cachedUserId = (j && typeof j.user_id === 'number') ? j.user_id : null;
    return Boolean(j.authenticated);
  } catch (e) { return false; }
}

// Reveal / hide every [data-admin-only] element based on the current
// user. Admin == user_id 1 today; this mirrors web/routes/changelog.py
// `_require_admin_user`. Phase-3b role-checks will flip both sides to
// `user.role === 'admin'` in one pass.
function applyAdminVisibility() {
  const isAdmin = _cachedUserId === 1;
  document.querySelectorAll('[data-admin-only]').forEach((el) => {
    if (isAdmin) {
      el.removeAttribute('hidden');
    } else {
      el.setAttribute('hidden', '');
    }
  });
}

function _handle401() {
  // Stop background polling so we don't keep hammering protected endpoints
  // after the session has expired.
  if (overviewInterval) { clearInterval(overviewInterval); overviewInterval = null; }
  if (detailInterval)   { clearInterval(detailInterval);   detailInterval   = null; }
  if (_priceInterval)   { clearInterval(_priceInterval);   _priceInterval   = null; }
  try { localStorage.removeItem('reverto_api_key'); } catch (e) {}
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
  try { localStorage.removeItem('reverto_api_key'); } catch (e) {}
  location.reload();
}

// Emergency stop — one request, server iterates every running bot and
// SIGTERMs it. Confirmation is a blocking native confirm() because
// there's no modal infrastructure for a single-purpose dialog here
// and a stray click on the profile menu should NEVER trip this.
// Global Escape-key handler — closes the top-most visible modal.
//
// Before this there was only a per-modal keydown binding (swModal),
// so every other modal (wizard, API key, settings, deal-edit, ...)
// swallowed Escape silently. This single handler walks the known
// "visible modal" class set, grabs the last one in DOM order
// (top-most), and triggers its close button — or falls back to
// stripping the visibility classes directly.
function handleGlobalEscape(e) {
  if (e.key !== 'Escape') return;
  const modals = Array.from(document.querySelectorAll(
    '.modal.show, .modal-overlay.show, .modal.visible'
  ));
  if (modals.length === 0) return;
  const top = modals[modals.length - 1];
  const closeBtn = top.querySelector(
    '[data-action="close"], .modal-close, .close-btn, [data-close-modal]'
  );
  if (closeBtn) {
    closeBtn.click();
  } else {
    top.classList.remove('show', 'visible');
  }
  e.preventDefault();
}
document.addEventListener('keydown', handleGlobalEscape);

async function handleResetDrawdown(slug) {
  if (!slug) return;
  if (!window.confirm(
    'Reset the drawdown guard for ' + slug + '?\n\n' +
    'The peak value is cleared and the bot resumes new entries on the next tick.'
  )) return;
  try {
    const res = await fetch(
      '/api/bots/' + encodeURIComponent(slug) + '/drawdown/reset',
      { method: 'POST' },
    );
    if (!res.ok) {
      const txt = await res.text();
      alert('Reset failed: ' + (res.status + ' ' + txt.slice(0, 200)));
      return;
    }
    alert('Drawdown guard reset for ' + slug + '.');
    location.reload();
  } catch (e) {
    alert('Reset failed: ' + (e && e.message || e));
  }
}

// Emergency-stop was previously a profile-menu item here; it now
// lives on the Admin → Bot Overview page as a prominent red button
// with a confirmation modal. See _confirmEmergencyStop below.

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
  // PR 5b: hidden username field anchors the password-change form so
  // browser password managers can associate the new password with
  // the logged-in account. Visually-hidden via CSS; not tab-focusable.
  const _pwHiddenUser = document.getElementById('profile-pw-hidden-username');
  if (_pwHiddenUser) _pwHiddenUser.value = _cachedUsername || '';
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
function fmtDateTimeNL(ts) {
  const d = new Date(typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
  if (!Number.isFinite(d.getTime())) return '--';
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `${dd}-${mm}-${d.getFullYear()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}
function fmtDateNL(ts) {
  const d = new Date(typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
  if (!Number.isFinite(d.getTime())) return '--';
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `${dd}-${mm}-${d.getFullYear()}`;
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
let _priceInterval = null;

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
  // Workspace chart-panels subscribe via the same WS broadcaster
  // rather than opening their own socket — see
  // _dispatchWorkspaceBotState for the fan-out rationale.
  _dispatchWorkspaceBotState(slug, data);
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

function showDTab(name, btn, fromPop = false) {
  ['chart', 'dashboard', 'deals', 'backtest', 'config', 'log'].forEach(n => {
    const el = $('dtab-' + n);
    if (el) { el.classList.toggle('hidden', n !== name); }
  });
  document.querySelectorAll('.detail-subnav .tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (currentSlug && !fromPop) {
    _pushHistory('bot', `#bot/${currentSlug}/${name}`, { slug: currentSlug, dtab: name });
  }
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
// id), label (shown in cog menu + thead), a cell renderer that takes a
// deal row and returns an HTML string, and an optional sortValue(row)
// extractor used by the click-to-sort path. The extractor exists
// because the column key is a display-shorthand (e.g. 'pair', 'age')
// that intentionally doesn't match the underlying data field
// (d.symbol, d.opened_at), so rows[key] would return undefined for
// most columns. Columns without a sortValue (like the action-button
// column) are deliberately unsortable.
// Visibility + order live in localStorage under
// "reverto.active_deals_columns".
const _sortTs = (iso) => {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isFinite(t) ? t : null;
};
const ACTIVE_DEALS_COLUMNS = [
  { key: 'bot',        label: 'Bot',
    cell: d => `<td><span class="link-like" data-action="open" data-slug="${safeText(d.bot_slug)}">${safeText(d.bot_name)}</span></td>`,
    sortValue: d => d.bot_name || d.bot_slug || '' },
  { key: 'deal_id',    label: 'Deal ID',
    cell: d => `<td class="muted-cell">${safeText(d.id)}</td>`,
    sortValue: d => d.id || '' },
  { key: 'pair',       label: 'Pair',
    cell: d => `<td>${safeText(d.symbol || '—')}</td>`,
    sortValue: d => d.symbol || '' },
  { key: 'entry',      label: 'Entry',
    cell: d => `<td>${fmtPrice(d.entry_price)}</td>`,
    sortValue: d => (d.entry_price == null ? null : Number(d.entry_price)) },
  { key: 'avg_entry',  label: 'Avg Entry',
    cell: d => `<td>${fmtPrice(d.avg_entry_price)}</td>`,
    sortValue: d => (d.avg_entry_price == null ? null : Number(d.avg_entry_price)) },
  { key: 'orders',     label: 'Orders',
    cell: d => `<td>${d.order_count}</td>`,
    sortValue: d => (d.order_count == null ? null : Number(d.order_count)) },
  { key: 'pnl_btc',    label: 'PnL BTC',
    cell: d => `<td>${fmtPnl(d.pnl_btc)}</td>`,
    sortValue: d => (d.pnl_btc == null ? null : Number(d.pnl_btc)) },
  { key: 'pnl_pct',    label: 'PnL %',
    cell: d => `<td>${fmtPct(d.pnl_pct)}</td>`,
    sortValue: d => (d.pnl_pct == null ? null : Number(d.pnl_pct)) },
  // Start Date + Age share the opened_at timestamp — the two columns
  // render differently (absolute date vs relative "5h ago") but the
  // underlying monotonic-order is the same, so they sort identically.
  { key: 'started',    label: 'Start Date',
    cell: d => `<td class="muted-cell" title="${safeText(d.opened_at || '')}">${formatDateTime(d.opened_at)}</td>`,
    sortValue: d => _sortTs(d.opened_at) },
  { key: 'age',        label: 'Age',
    cell: d => `<td class="muted-cell">${timeAgo(d.opened_at)}</td>`,
    sortValue: d => _sortTs(d.opened_at) },
  { key: 'actions',    label: '',
    cell: d => `<td class="deal-actions-cell">` +
      `<button class="deal-btn deal-btn-edit" data-slug="${safeText(d.bot_slug)}" data-deal="${safeText(d.id)}" title="Edit">✎</button>` +
      `<button class="deal-btn deal-btn-close" data-slug="${safeText(d.bot_slug)}" data-deal="${safeText(d.id)}" title="Close at market">■</button>` +
      `<button class="deal-btn deal-btn-cancel" data-slug="${safeText(d.bot_slug)}" data-deal="${safeText(d.id)}" title="Cancel">✕</button>` +
      `</td>` },
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

// Click-to-sort state lives under `${lsKey}-sort` so each column-driven
// table gets its own independent sort-memory without colliding with the
// column-visibility array stored at `${lsKey}`. Value shape is either
// null (unsorted) or {key, dir} where dir is 'asc' | 'desc'. loadSort
// returns null on any corruption (malformed JSON, unknown dir) so a
// stale entry from an older build can't throw during render.
function loadSort(key) {
  try {
    const raw = localStorage.getItem(`${key}-sort`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    if (!parsed.key || typeof parsed.key !== 'string') return null;
    if (parsed.dir !== 'asc' && parsed.dir !== 'desc') return null;
    return { key: parsed.key, dir: parsed.dir };
  } catch (e) {
    return null;
  }
}

function saveSort(key, sort) {
  try {
    if (sort === null) {
      localStorage.removeItem(`${key}-sort`);
    } else {
      localStorage.setItem(
        `${key}-sort`,
        JSON.stringify({ key: sort.key, dir: sort.dir }),
      );
    }
  } catch (e) {}
}

// Stable sort with null/undefined always sinking to the bottom — that
// way a partially-populated column (e.g. closed_at on deals that are
// still open) doesn't pollute the visible-sorted top of the table in
// either direction. Numeric vs string detection is by typeof on the
// first non-null sample; that's enough for the deal-row shape (which
// is the only caller today) without introducing a per-column
// comparator registry we don't yet need.
//
// ``defs`` is the Map built by _renderColumnDrivenTable (key →
// column-definition). When a column declares ``sortValue(row)`` we
// call that to extract the comparable; otherwise we fall back to
// ``row[key]``. The extractor indirection matters because the column
// key is a display-shorthand (e.g. 'pair', 'age', 'started') that
// doesn't have to match the underlying data field (symbol,
// opened_at). Without sortValue the fallback returns undefined for
// mismatches and the column sorts as a no-op — which is the bug
// this file path fixes.
function _applySortToRows(rows, sort, defs) {
  if (!sort || !rows || !rows.length) return rows;
  const { key, dir } = sort;
  const mult = dir === 'asc' ? 1 : -1;
  const colDef = defs && typeof defs.get === 'function' ? defs.get(key) : null;
  const extract = colDef && typeof colDef.sortValue === 'function'
    ? colDef.sortValue
    : (r) => (r == null ? null : r[key]);
  const withIdx = rows.map((r, i) => [extract(r), i, r]);
  withIdx.sort((a, b) => {
    const va = a[0];
    const vb = b[0];
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
    cell: d => `<td class="muted-cell">${safeText(d.id)}</td>`,
    sortValue: d => d.id || '' },
  { key: 'side',        label: 'Side',
    cell: d => `<td>${safeText((d.side || '—').toUpperCase())}</td>`,
    sortValue: d => d.side || '' },
  { key: 'avg_entry',   label: 'Avg Entry',
    cell: d => `<td>${fmtPrice(d.avg_entry_price || d.entry_price)}</td>`,
    sortValue: d => {
      const v = d.avg_entry_price != null ? d.avg_entry_price : d.entry_price;
      return v == null ? null : Number(v);
    } },
  { key: 'close_price', label: 'Close Price',
    cell: d => `<td>${fmtPrice(d.close_price)}</td>`,
    sortValue: d => (d.close_price == null ? null : Number(d.close_price)) },
  { key: 'pnl_btc',     label: 'PnL BTC',
    cell: d => `<td>${fmtPnl(d.pnl_btc)}</td>`,
    sortValue: d => (d.pnl_btc == null ? null : Number(d.pnl_btc)) },
  { key: 'pnl_pct',     label: 'PnL %',
    cell: d => `<td>${fmtPct(d.pnl_pct)}</td>`,
    sortValue: d => (d.pnl_pct == null ? null : Number(d.pnl_pct)) },
  { key: 'reason',      label: 'Reason',
    cell: d => `<td>${reasonBadge(d.close_reason)}</td>`,
    sortValue: d => d.close_reason || '' },
  { key: 'opened',      label: 'Start Date',
    cell: d => `<td class="muted-cell" title="${safeText(d.opened_at || '')}">${formatDateTime(d.opened_at)}</td>`,
    sortValue: d => _sortTs(d.opened_at) },
  { key: 'closed',      label: 'Close Date',
    cell: d => `<td class="muted-cell" title="${safeText(d.closed_at || '')}">${formatDateTime(d.closed_at)}</td>`,
    sortValue: d => _sortTs(d.closed_at) },
  // Duration = closed_at − opened_at in ms; null when either end is
  // missing so deals that haven't closed yet sink to the bottom via
  // the null-last rule in _applySortToRows.
  { key: 'duration',    label: 'Duration',
    cell: d => `<td class="muted-cell">${formatDuration(d.opened_at, d.closed_at)}</td>`,
    sortValue: d => {
      const a = _sortTs(d.opened_at);
      const b = _sortTs(d.closed_at);
      return (a == null || b == null) ? null : b - a;
    } },
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
  const sort = loadSort(lsKey);
  const sortedRows = _applySortToRows(rows, sort, defs);
  const onResort = (opts && typeof opts.onResort === 'function') ? opts.onResort : null;
  if (head) {
    head.innerHTML = cols.map((c, i) => {
      const isSorted = sort && sort.key === c.key;
      const arrow = isSorted ? (sort.dir === 'asc' ? '▲' : '▼') : '';
      // A column only participates in sort when it has BOTH a
      // non-empty label (so there's a header to click) AND a
      // sortValue extractor (so there's something meaningful to
      // compare). Action-button columns have neither; they remain
      // draggable for reorder but are not sortable.
      const def = defs.get(c.key) || c;
      const label = def.label || '';
      const sortable = label !== '' && typeof def.sortValue === 'function';
      const sortedCls = isSorted ? ` sorted sorted-${sort.dir}` : '';
      const sortableCls = sortable ? ' sortable' : '';
      return `<th draggable="true" data-col-idx="${i}" data-col-key="${safeText(c.key)}"${sortable ? ' data-sortable="1"' : ''} class="${(sortedCls + sortableCls).trim()}">`
        + `<span class="col-label">${safeText(label)}</span>`
        + `<span class="col-sort-arrow">${arrow}</span>`
        + `</th>`;
    }).join('');
    _attachHeaderDragHandlers(head, lsKey, defaults);
    if (onResort) _attachHeaderSortHandlers(head, lsKey, onResort);
  }
  const colSpan = Math.max(1, cols.length);
  if (!sortedRows || !sortedRows.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${colSpan}">${safeText(emptyMsg)}</td></tr>`;
    return;
  }
  // Optional row decoration: Feature 1 (deal timeline) uses this to tag
  // each row with the deal id and a clickable-row class so the tbody
  // delegate handler can find the deal without re-rendering the table.
  const rowAttrs = (opts && typeof opts.rowAttrs === 'function') ? opts.rowAttrs : null;
  tbody.innerHTML = sortedRows.map(row => {
    const cells = cols.map(c => {
      const def = defs.get(c.key);
      return def ? def.cell(row) : '<td></td>';
    }).join('');
    const attrs = rowAttrs ? rowAttrs(row) : '';
    return `<tr${attrs ? ' ' + attrs : ''}>${cells}</tr>`;
  }).join('');
}

function renderDetailClosedDeals(deals) {
  _detailClosedDealsLastRows = Array.isArray(deals) ? deals : [];
  _renderColumnDrivenTable(
    'd-closed-thead-row', 'd-closed-tbody',
    CLOSED_DEALS_LS_KEY, CLOSED_DEALS_COLUMNS,
    _detailClosedDealsLastRows, 'No closed deals',
    {
      rowAttrs: d => `data-deal-id="${safeText(d.id)}" class="clickable-row"`,
      onResort: () => renderDetailClosedDeals(_detailClosedDealsLastRows),
    },
  );
}

// ── Detail Open Deals column manager ────────────────────────────────────────
// The bot detail view's Open deals table uses its own column set + storage
// key so it can stay independent from the global Active Deals table — the
// detail view doesn't need a "Bot" identifying column since the slug is
// already in the page header.
const DETAIL_OPEN_DEALS_COLUMNS = [
  { key: 'deal_id',   label: 'Deal ID',
    cell: d => `<td class="deal-id-cell">${safeText(d.id)}</td>`,
    sortValue: d => d.id || '' },
  { key: 'pair',      label: 'Pair',
    cell: d => `<td>${safeText(d.symbol || '—')}</td>`,
    sortValue: d => d.symbol || '' },
  { key: 'entry',     label: 'Entry',
    cell: d => `<td>${fmtPrice(d.entry_price)}</td>`,
    sortValue: d => (d.entry_price == null ? null : Number(d.entry_price)) },
  { key: 'avg_entry', label: 'Avg Entry',
    cell: d => `<td>${fmtPrice(d.avg_entry_price)}</td>`,
    sortValue: d => (d.avg_entry_price == null ? null : Number(d.avg_entry_price)) },
  { key: 'orders',    label: 'Orders',
    cell: d => `<td>${d.order_count}</td>`,
    sortValue: d => (d.order_count == null ? null : Number(d.order_count)) },
  { key: 'pnl_btc',   label: 'PnL BTC',
    cell: d => `<td>${fmtPnl(d.pnl_btc)}</td>`,
    sortValue: d => (d.pnl_btc == null ? null : Number(d.pnl_btc)) },
  { key: 'pnl_pct',   label: 'PnL %',
    cell: d => `<td>${fmtPct(d.pnl_pct)}</td>`,
    sortValue: d => (d.pnl_pct == null ? null : Number(d.pnl_pct)) },
  { key: 'started',   label: 'Start Date',
    cell: d => `<td class="muted-cell" title="${safeText(d.opened_at || '')}">${formatDateTime(d.opened_at)}</td>`,
    sortValue: d => _sortTs(d.opened_at) },
  { key: 'age',       label: 'Age',
    cell: d => `<td class="muted-cell">${timeAgo(d.opened_at)}</td>`,
    sortValue: d => _sortTs(d.opened_at) },
  { key: 'actions',  label: '',
    cell: d => `<td class="deal-actions-cell">` +
      `<button class="deal-btn deal-btn-edit" data-slug="${safeText(d.bot_slug || currentSlug || '')}" data-deal="${safeText(d.id)}" title="Edit">✎</button>` +
      `<button class="deal-btn deal-btn-close" data-slug="${safeText(d.bot_slug || currentSlug || '')}" data-deal="${safeText(d.id)}" title="Close at market">■</button>` +
      `<button class="deal-btn deal-btn-cancel" data-slug="${safeText(d.bot_slug || currentSlug || '')}" data-deal="${safeText(d.id)}" title="Cancel">✕</button>` +
      `</td>` },
];
const DETAIL_OPEN_DEALS_LS_KEY = 'reverto.detail_open_deals_columns';

function renderDetailOpenDeals(deals) {
  _detailOpenDealsLastRows = Array.isArray(deals) ? deals : [];
  _renderColumnDrivenTable(
    'd-open-thead-row', 'd-open-tbody',
    DETAIL_OPEN_DEALS_LS_KEY, DETAIL_OPEN_DEALS_COLUMNS,
    _detailOpenDealsLastRows, 'No open deals',
    {
      rowAttrs: d => `data-deal-id="${safeText(d.id)}" class="clickable-row"`,
      onResort: () => renderDetailOpenDeals(_detailOpenDealsLastRows),
    },
  );
}

// Map lsKey → re-render callback so the header drag drop can refresh
// both the table and any open cog menu after persisting a new order.
const _HEADER_RERENDER = new Map();

// Drag-vs-click conflict guard. HTML5 drag events fire a trailing
// click on the source element in some browsers (Chrome + Firefox both
// do this for draggable="true" th's when the drop lands back inside
// the same element). Record the dragend timestamp so the sort
// handler below can suppress any click that follows within ~250ms.
let _lastHeaderDragEndAt = 0;

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
      _lastHeaderDragEndAt = Date.now();
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

// Click-to-sort on column-driven tables. Cycles through unsorted →
// asc → desc → unsorted per column. Picking a new column resets the
// previous one to unsorted and starts at asc. Only th's whose
// defaults entry has a non-empty label participate (data-sortable is
// set by _renderColumnDrivenTable's header render) so action-button
// columns stay reorder-only.
function _attachHeaderSortHandlers(headEl, lsKey, onResort) {
  const ths = Array.from(headEl.querySelectorAll('th[data-sortable="1"]'));
  ths.forEach((th) => {
    th.addEventListener('click', (e) => {
      // Suppress the click that trails a drag on the same th — see
      // _lastHeaderDragEndAt above.
      if (Date.now() - _lastHeaderDragEndAt < 250) return;
      if (e.defaultPrevented) return;
      const key = th.dataset.colKey;
      if (!key) return;
      const cur = loadSort(lsKey);
      let next;
      if (!cur || cur.key !== key) next = { key, dir: 'asc' };
      else if (cur.dir === 'asc')  next = { key, dir: 'desc' };
      else                          next = null;
      saveSort(lsKey, next);
      onResort();
    });
  });
}

// Rows caches — each column-driven table keeps the last-rendered rows
// so onResort can re-render the same data in the new order without
// hitting the network. Fetch paths (fetchOverview, fetchDetail) call
// the renderer with fresh rows and overwrite the cache; the handler
// below just needs a stable snapshot while the user clicks around.
let _activeDealsLastRows = [];
let _detailOpenDealsLastRows = [];
let _detailClosedDealsLastRows = [];

function renderActiveDeals(deals) {
  _activeDealsLastRows = Array.isArray(deals) ? deals : [];
  _renderColumnDrivenTable(
    'all-deals-thead-row', 'all-deals-tbody',
    ACTIVE_DEALS_LS_KEY, ACTIVE_DEALS_COLUMNS,
    _activeDealsLastRows, 'No open deals across any bot',
    { onResort: () => renderActiveDeals(_activeDealsLastRows) },
  );
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

  // State badges — drawdown pause and clock-skew pause are operator-
  // visible warning states that deserve a bold marker above the stats
  // grid. Drawdown takes priority because it means the kill-switch
  // actually fired; clock-skew is a softer "orders paused while we
  // resync with the exchange clock".
  const drawdownTriggered = b.drawdown_guard && b.drawdown_guard.triggered;
  const pausedByDrawdown = b.paused_by_drawdown || drawdownTriggered;
  const pausedBySkew = b.paused_by_clock_skew;
  // Source mode from every plausible location — the API today returns
  // it flat as b.mode (read from logs/<slug>.state.json), but legacy
  // code and a hypothetical future nested shape both show up in the
  // wild, so keep this read defensive. See test_api_bots_returns_mode_field.
  const botMode = (
    (b.config && b.config.mode) || b.mode || 'paper'
  ).toString().toLowerCase();
  const isLive = botMode === 'live';
  let stateBadge = '';
  if (pausedByDrawdown) {
    const reason = (b.drawdown_guard && b.drawdown_guard.trigger_reason) || '';
    stateBadge = `<div class="state-badge badge-danger" role="status"
        aria-label="Drawdown guard triggered">
      ⚠ DRAWDOWN PAUSED${reason ? ' — ' + safeText(reason) : ''}
      <button type="button" class="btn-small btn-warning"
              data-action="reset-drawdown" data-slug="${safeText(b.slug)}"
              aria-label="Reset drawdown guard">Reset</button>
    </div>`;
  } else if (pausedBySkew) {
    stateBadge = `<div class="state-badge badge-warning" role="status"
        aria-label="Clock skew detected">
      🕐 CLOCK SKEW — orders paused
    </div>`;
  } else if (isLive && running) {
    // Phase 1: live bots can only run in dry-run mode. The badge keeps
    // that explicit on the overview so operators never confuse a
    // paper bot's "Running" pill with a real-money run.
    stateBadge = `<div class="state-badge badge-dry-run" role="status"
        aria-label="Bot running in dry-run mode">
      🟡 DRY RUN — no real orders placed
    </div>`;
  }

  return `
  <div class="bot-card" data-slug="${safeText(b.slug)}">
    <div class="bot-card-top">
      <span class="bot-card-name">${safeText(b.bot_name || b.slug)}</span>
      <div class="bot-card-top-right">
        <div class="pill ${running ? 'running' : 'stopped'} tab-pill-static">
          <div class="dot"></div><span>${running ? 'Running' : 'Stopped'}</span>
        </div>
        <div class="bot-card-menu-wrap">
          <button class="bot-card-menu-btn" data-action="menu"
                  data-slug="${safeText(b.slug)}" aria-label="Bot actions"
                  aria-haspopup="true" aria-expanded="false">⋮</button>
          <div class="bot-card-menu hidden" data-menu-for="${safeText(b.slug)}">
            <button class="bot-card-menu-item" data-action="edit"
                    data-slug="${safeText(b.slug)}">Edit config</button>
            <button class="bot-card-menu-item" data-action="duplicate"
                    data-slug="${safeText(b.slug)}">Duplicate</button>
            <button class="bot-card-menu-item" data-action="export"
                    data-slug="${safeText(b.slug)}">Export</button>
            <button class="bot-card-menu-item bot-card-menu-danger"
                    data-action="delete" data-slug="${safeText(b.slug)}"
                    data-name="${safeText(b.bot_name || b.slug)}">Delete</button>
          </div>
        </div>
      </div>
    </div>
    ${stateBadge}
    <div class="bot-card-meta">
      ${safeText((b.exchange || '—').toUpperCase())} · ${safeText(b.pair || 'BTC/USD')} · ${safeText(botMode.toUpperCase())}
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
        : (isLive
            ? `<button class="btn-sm btn-warning" data-action="start-dry-run" data-slug="${safeText(b.slug)}"
                       title="Spawn main_live.py --dry-run (no real orders)">▶ Start dry-run</button>`
            : `<button class="btn-sm btn-start"   data-action="start"   data-slug="${safeText(b.slug)}">▶ Start</button>`)
      }
      <button class="btn-sm btn-open" data-action="open" data-slug="${safeText(b.slug)}">Open →</button>
    </div>
  </div>`;
}

// Click delegation — slug komt uit data-slug (escaped via safeText), nooit
// in een onclick-string, dus kan niet uit het attribuut breken.
document.addEventListener('click', e => {
  // Kebab dropdowns: click outside any open menu closes all of them.
  // Runs before the bot-card handler so clicking ⋮ doesn't also open
  // the detail view underneath.
  const menuBtn = e.target.closest('.bot-card-menu-btn');
  const insideMenu = e.target.closest('.bot-card-menu');
  if (!menuBtn && !insideMenu) {
    document.querySelectorAll('.bot-card-menu').forEach(m => m.classList.add('hidden'));
    document.querySelectorAll('.bot-card.menu-open')
      .forEach(c => c.classList.remove('menu-open'));
    document.querySelectorAll('.bot-card-menu-btn[aria-expanded="true"]')
      .forEach(b => b.setAttribute('aria-expanded', 'false'));
  }
  // Bot card click — open detail unless a button was clicked
  const card = e.target.closest('.bot-card');
  if (card && !e.target.closest('[data-action]') && !e.target.closest('.deal-btn')) {
    const slug = card.dataset.slug;
    if (slug) { openBot(slug); return; }
  }
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  const slug = el.dataset.slug;
  if (!slug) return;
  if (action === 'open') openBot(slug);
  else if (action === 'menu') {
    // Toggle the dropdown for this card, closing every other open menu
    // first so only one is visible at a time. The .menu-open class on
    // the card overrides overflow:hidden so the dropdown can escape
    // the card boundary — without it the menu is clipped.
    e.stopPropagation();
    const wrap = el.closest('.bot-card-menu-wrap');
    const menu = wrap && wrap.querySelector('.bot-card-menu');
    if (!menu) return;
    const wasOpen = !menu.classList.contains('hidden');
    document.querySelectorAll('.bot-card-menu').forEach(m => m.classList.add('hidden'));
    document.querySelectorAll('.bot-card.menu-open')
      .forEach(c => c.classList.remove('menu-open'));
    document.querySelectorAll('.bot-card-menu-btn[aria-expanded="true"]')
      .forEach(b => b.setAttribute('aria-expanded', 'false'));
    if (!wasOpen) {
      menu.classList.remove('hidden');
      el.setAttribute('aria-expanded', 'true');
      const card = el.closest('.bot-card');
      if (card) card.classList.add('menu-open');
    }
  }
  else if (action === 'edit') { editBot(slug); }
  else if (action === 'duplicate') { duplicateBot(slug); }
  else if (action === 'export') { exportBot(slug); }
  else if (action === 'delete') deleteBot(slug, el.dataset.name || slug);
  else if (action === 'reset-drawdown') { handleResetDrawdown(slug); e.stopPropagation(); }
  else if (action === 'start-dry-run') {
    // Extra prompt because this launches a LIVE-mode bot (dry-run only
    // under Phase 1, but the runner class is the real one). Mirror the
    // confirmation pattern used for emergency-stop.
    const ok = confirm(
      `Start "${slug}" in DRY-RUN mode?\n\n` +
      `This spawns main_live.py with --dry-run. No real orders will be ` +
      `placed — Phase 1 refuses real execution — but the bot will use ` +
      `the live exchange client for market data.`
    );
    if (ok) botAction(slug, 'start-dry-run', el);
  }
  else if (['start', 'stop', 'restart'].includes(action)) botAction(slug, action, el);
});

// Bot-detail view: the reset button doesn't carry a data-slug (we're
// already scoped to currentSlug). Handle it via a second delegated
// listener that reads currentSlug at click-time.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-action="reset-drawdown-detail"]');
  if (!btn) return;
  const slug = (typeof currentSlug !== 'undefined' && currentSlug) || null;
  if (slug) {
    handleResetDrawdown(slug);
    e.stopPropagation();
  }
});

// Keep the bot-detail drawdown panel in sync with the latest state
// fetched from /api/bots/{slug}. Called from fetchDetail() below.
function updateBotDetailDrawdown(state) {
  const panel = document.getElementById('bot-detail-drawdown-status');
  if (!panel) return;
  const guard = (state && state.drawdown_guard) || null;
  const triggered = guard && guard.triggered;
  const paused = state && state.paused_by_drawdown;
  // Only show the panel if the bot actually writes a drawdown_guard
  // blob — pre-v20 state files or bots with the guard disabled should
  // not see this section at all.
  const hasGuard = guard !== null && guard !== undefined;
  if (!hasGuard) {
    panel.classList.add('hidden');
    return;
  }
  panel.classList.remove('hidden');
  const statusEl = panel.querySelector('.status-value');
  const peakEl   = panel.querySelector('.peak-value');
  const reasonEl = panel.querySelector('.reason-value');
  if (triggered || paused) {
    statusEl.textContent = 'TRIGGERED — new entries paused';
    statusEl.classList.add('status-danger');
  } else {
    statusEl.textContent = 'Active (monitoring)';
    statusEl.classList.remove('status-danger');
  }
  const peak = guard.peak_value;
  peakEl.textContent = (peak !== null && peak !== undefined)
    ? (Number(peak).toFixed(8) + ' BTC')
    : '—';
  reasonEl.textContent = guard.trigger_reason || '—';
}

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
  _dealOvRemoveForSlug(slug);
  // If we're currently inside this bot's detail view, bounce back to Bots.
  if (currentSlug === slug) goBots();
  else fetchOverview();
}

// ── Duplicate / Export / Import ─────────────────────────────────────────────
// Slug shape must match the backend _BOT_SLUG_RE — keep in sync with
// web/app.py:412.
const _BOT_SLUG_RE_CLIENT = /^[A-Za-z0-9_\-]+$/;

async function duplicateBot(slug) {
  const proposed = prompt(
    `Duplicate '${slug}'\n\nNew slug (letters, digits, _ and - only):`,
    `${slug}_copy`,
  );
  if (!proposed) return;
  const newSlug = proposed.trim();
  if (!_BOT_SLUG_RE_CLIENT.test(newSlug)) {
    _dealToast('Invalid slug — use letters, digits, _ and - only', 'toast-warn');
    return;
  }
  try {
    const r = await fetch(`/api/bots/${encodeURIComponent(slug)}/duplicate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_slug: newSlug }),
    });
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      _dealToast(err.detail || 'Duplicate failed', 'toast-warn');
      return;
    }
    _dealToast(`Bot '${newSlug}' created from '${slug}'`);
    fetchOverview();
  } catch (e) {
    _dealToast('Network error', 'toast-warn');
  }
}

function exportBot(slug) {
  // Browser handles the download via Content-Disposition on the response.
  // Using window.location keeps session cookies intact — a fetch+blob
  // dance would work too but adds no value here.
  window.location = `/api/bots/${encodeURIComponent(slug)}/export`;
}

function importBot() {
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.yaml,.yml';
  fileInput.onchange = async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    let yamlText;
    try { yamlText = await file.text(); }
    catch (err) { _dealToast('Could not read file', 'toast-warn'); return; }
    const defaultSlug = file.name.replace(/\.(yaml|yml)$/i, '');
    const proposed = prompt('Import bot as slug:', defaultSlug);
    if (!proposed) return;
    const slug = proposed.trim();
    if (!_BOT_SLUG_RE_CLIENT.test(slug)) {
      _dealToast('Invalid slug — use letters, digits, _ and - only', 'toast-warn');
      return;
    }
    try {
      const r = await fetch(`/api/bots/import?slug=${encodeURIComponent(slug)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-yaml' },
        body: yamlText,
      });
      if (r.status === 401) { _handle401(); return; }
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        _dealToast(err.detail || 'Import failed', 'toast-warn');
        return;
      }
      _dealToast(`Bot '${slug}' imported`);
      fetchOverview();
    } catch (err) {
      _dealToast('Network error', 'toast-warn');
    }
  };
  fileInput.click();
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

// ── Deal management (edit / cancel / close) ─────────────────────────────────

function _dealToast(msg, cls = 'toast-success') {
  const existing = document.querySelector('.deal-toast');
  if (existing) existing.remove();
  const el = document.createElement('div');
  el.className = `deal-toast ${cls}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

let _dealEditState = null;
const _dealOverrideCache = {};
const _DEAL_OV_PREFIX = 'reverto_deal_ov_';

function _dealOvKey(slug, dealId) { return _DEAL_OV_PREFIX + slug + '_' + dealId; }

function _dealOvStore(slug, dealId, payload) {
  const k = _dealOvKey(slug, dealId);
  _dealOverrideCache[k] = payload;
  try { localStorage.setItem(k, JSON.stringify(payload)); } catch (e) {}
}
function _dealOvLoad(slug, dealId) {
  const k = _dealOvKey(slug, dealId);
  if (_dealOverrideCache[k]) return _dealOverrideCache[k];
  try {
    const raw = localStorage.getItem(k);
    if (raw) { const parsed = JSON.parse(raw); _dealOverrideCache[k] = parsed; return parsed; }
  } catch (e) {}
  return null;
}
function _dealOvRemove(slug, dealId) {
  const k = _dealOvKey(slug, dealId);
  delete _dealOverrideCache[k];
  try { localStorage.removeItem(k); } catch (e) {}
}
function _dealOvRemoveForSlug(slug) {
  const prefix = _DEAL_OV_PREFIX + slug + '_';
  const toRemove = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(prefix)) toRemove.push(k);
    }
  } catch (e) {}
  toRemove.forEach(k => { try { localStorage.removeItem(k); } catch (e) {} });
  for (const k of Object.keys(_dealOverrideCache)) {
    if (k.startsWith(prefix)) delete _dealOverrideCache[k];
  }
}

function _deUpdateDcaDisabled() {
  const fields = $('de-dca-fields');
  if (fields) {
    fields.classList.toggle('de-disabled', !$('de-dca-enabled').checked);
  }
}

async function dealOpenEditModal(slug, dealId) {
  _dealEditState = { slug, dealId };
  $('deal-edit-title').textContent = `Edit Deal #${dealId}`;

  // Start from bot config defaults
  const bc = (_detailConfigCache && _detailConfigCache.bot) || {};
  const bcTp = bc.take_profit || {};
  const bcSl = bc.stop_loss || {};
  const bcDca = bc.dca || {};
  let tpEnabled = bcTp.enabled !== false;
  let tpPct = bcTp.target_pct || 3.0;
  let slEnabled = bcSl.type !== 'none';
  let slType = (bcSl.type && bcSl.type !== 'none') ? bcSl.type : 'fixed';
  let slPct = bcSl.pct || 5.0;
  let dcaEnabled = bcDca.enabled !== false;
  let dcaSpacing = bcDca.order_spacing_pct || 2.5;
  let dcaMult = bcDca.multiplier || 1.0;
  let dcaStep = bcDca.step_scale != null ? bcDca.step_scale : 1.0;
  let dcaMax = bcDca.max_orders ? Math.max(0, bcDca.max_orders - 1) : 4;

  // Check local cache first (survives page refresh via localStorage)
  const cached = _dealOvLoad(slug, dealId);
  if (cached) {
    if (cached.tp_enabled != null) tpEnabled = cached.tp_enabled;
    if (cached.tp_target_pct != null) tpPct = cached.tp_target_pct;
    if (cached.sl_enabled != null) slEnabled = cached.sl_enabled;
    if (cached.sl_type) slType = cached.sl_type;
    if (cached.sl_pct != null) slPct = cached.sl_pct;
    if (cached.dca_enabled != null) dcaEnabled = cached.dca_enabled;
    if (cached.dca_spacing_pct != null) dcaSpacing = cached.dca_spacing_pct;
    if (cached.dca_multiplier != null) dcaMult = cached.dca_multiplier;
    if (cached.dca_step_scale != null) dcaStep = cached.dca_step_scale;
    if (cached.dca_max_orders != null) dcaMax = cached.dca_max_orders;
  }

  // Fetch live deal state (includes per-deal overrides from engine)
  try {
    const r = await fetch(`/api/bots/${slug}/deals/${dealId}`);
    if (r.ok) {
      const j = await r.json();
      const d = j.deal || {};
      if (window._BT_DEBUG) console.log('[DEAL_EDIT] GET response:', d);
      const tpOv = d._tp_override;
      if (tpOv) {
        if (tpOv.enabled != null) tpEnabled = tpOv.enabled;
        if (tpOv.target_pct != null) tpPct = tpOv.target_pct;
      }
      const slOv = d._sl_override;
      if (slOv) {
        if (slOv.enabled != null) slEnabled = slOv.enabled;
        if (slOv.type) slType = slOv.type;
        if (slOv.pct != null) slPct = slOv.pct;
      }
      if (d._dca_enabled === false) dcaEnabled = false;
      else if (d._dca_enabled === true) dcaEnabled = true;
    }
  } catch (e) { /* use defaults + cache */ }

  $('de-tp-enabled').checked = tpEnabled;
  $('de-tp-pct').value = tpPct;
  $('de-sl-enabled').checked = slEnabled;
  $('de-sl-type').value = slType;
  $('de-sl-pct').value = slPct;
  $('de-dca-enabled').checked = dcaEnabled;
  $('de-dca-spacing').value = dcaSpacing;
  $('de-dca-mult').value = dcaMult;
  $('de-dca-step').value = dcaStep;
  $('de-dca-max').value = dcaMax;
  _deUpdateDcaDisabled();
  $('deal-edit-modal').classList.add('show');
}

async function dealSaveEdit() {
  if (!_dealEditState) return;
  const { slug, dealId } = _dealEditState;
  const payload = {
    tp_enabled: $('de-tp-enabled').checked,
    tp_target_pct: parseFloat($('de-tp-pct').value),
    sl_enabled: $('de-sl-enabled').checked,
    sl_type: $('de-sl-type').value,
    sl_pct: parseFloat($('de-sl-pct').value),
    dca_enabled: $('de-dca-enabled').checked,
    dca_spacing_pct: parseFloat($('de-dca-spacing').value),
    dca_multiplier: parseFloat($('de-dca-mult').value),
    dca_step_scale: parseFloat($('de-dca-step').value),
    dca_max_orders: parseInt($('de-dca-max').value, 10),
  };
  try {
    const r = await fetch(`/api/bots/${slug}/deals/${dealId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) { _dealToast('Save failed', 'toast-warn'); return; }
    _dealOvStore(slug, dealId, payload);
    _dealToast('Deal settings saved');
  } catch (e) { _dealToast('Network error', 'toast-warn'); }
  $('deal-edit-modal').classList.remove('show');
  _dealEditState = null;
  if (currentSlug) fetchDetail(currentSlug);
}

async function dealAction(slug, dealId, action) {
  const verb = action === 'cancel' ? 'Cancel' : 'Close at market';
  const msg = action === 'cancel'
    ? `Cancel deal #${dealId}? The position remains open on the exchange.`
    : `Close deal #${dealId} at market price?`;
  if (!confirm(msg)) return;
  try {
    const r = await fetch(`/api/bots/${slug}/deals/${dealId}?action=${action}`, { method: 'DELETE' });
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) { _dealToast(`${verb} failed`, 'toast-warn'); return; }
    _dealOvRemove(slug, dealId);
    _dealToast(action === 'cancel' ? 'Deal cancelled' : 'Deal closed');
  } catch (e) { _dealToast('Network error', 'toast-warn'); }
  if (currentSlug) fetchDetail(currentSlug);
  fetchOverview();
}

// Delegate clicks on deal action buttons anywhere in the page
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.deal-btn');
  if (!btn) return;
  e.stopPropagation();
  const slug = btn.dataset.slug;
  const deal = btn.dataset.deal;
  if (!slug || !deal) return;
  if (btn.classList.contains('deal-btn-edit')) dealOpenEditModal(slug, deal);
  else if (btn.classList.contains('deal-btn-cancel')) dealAction(slug, deal, 'cancel');
  else if (btn.classList.contains('deal-btn-close')) dealAction(slug, deal, 'close');
});

let _paramTipPopup = null;
function _dismissParamTip() {
  if (_paramTipPopup) { _paramTipPopup.remove(); _paramTipPopup = null; }
}
function _showParamTip(label, text) {
  _dismissParamTip();
  const popup = document.createElement('div');
  popup.className = 'param-tip-popup';
  popup.textContent = text;
  popup.style.visibility = 'hidden';
  document.body.appendChild(popup);
  _paramTipPopup = popup;
  const rect = label.getBoundingClientRect();
  const pr = popup.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  let left = rect.left;
  if (left + pr.width > vw - 10) left = vw - pr.width - 10;
  if (left < 10) left = 10;
  let top = rect.bottom + window.scrollY + 6;
  if (rect.bottom + pr.height > vh - 10) top = rect.top + window.scrollY - pr.height - 6;
  popup.style.left = left + 'px';
  popup.style.top = top + 'px';
  popup.style.visibility = 'visible';
}
document.addEventListener('click', (e) => {
  const lbl = e.target.closest('.param-label-toggle');
  if (!lbl) { _dismissParamTip(); return; }
  const text = lbl.dataset.hint;
  if (!text) { _dismissParamTip(); return; }
  e.stopPropagation();
  if (_paramTipPopup && _paramTipPopup._srcLabel === lbl) { _dismissParamTip(); return; }
  _showParamTip(lbl, text);
  _paramTipPopup._srcLabel = lbl;
});

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

function goWorkspace(fromPop = false) {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-workspace-btn');
  showPage('workspace');
  // Workspace panels are static placeholders in PR 2 — no live
  // data feed yet. Idle the overview poller so the Workspace tab
  // keeps the network quiet while the operator rearranges panels.
  clearInterval(overviewInterval);
  overviewInterval = null;
  if (!fromPop) _pushHistory('workspace', '#workspace');
  initWorkspace();
}

function goBacktests(fromPop = false) {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-backtests-btn');
  showPage('backtests');
  // Idle the overview poller while the history view is active — the
  // backtest_runs table isn't tied to live ticker data, and the
  // timer would otherwise keep refetching /api/bots on every tick.
  clearInterval(overviewInterval);
  overviewInterval = null;
  btLoadHistory();
  if (!fromPop) _pushHistory('backtests', '#backtests');
}

function goNewBot() {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-bots-btn');  // new bot lives logically under Bots
  showPage('new-bot');
  nbInit();
  initWizardChart();
  fetchWizardChartData();
}

// ── Changelog + Admin SPA tabs ───────────────────────────────────────────────
// Both pages used to be server-rendered escape-hatches (full page-load
// out of the SPA); the SPA-integration refactor turns them into
// first-class tabs that route entirely via showPage() + the JSON API
// endpoints in web/routes/changelog.py.

function goChangelog(fromPop = false) {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-changelog-btn');
  showPage('changelog');
  // The overview poller is pointless while the user is reading
  // release notes; pause it so the network stays quiet.
  clearInterval(overviewInterval);
  overviewInterval = null;
  if (!fromPop) _pushHistory('changelog', '#changelog');
  loadChangelog();
}

function goAdmin(fromPop = false, subRoute = null) {
  _resetHeaderForTopLevel();
  _setActiveTab('nav-admin-btn');
  showPage('admin');
  clearInterval(overviewInterval);
  overviewInterval = null;
  if (!fromPop) {
    const hash = subRoute ? `#admin/${subRoute}` : '#admin';
    _pushHistory('admin', hash, { sub: subRoute });
  }
  _showAdminSubpage(subRoute);
  if (subRoute === 'changelog-manage') loadAdminChangelog();
  if (subRoute === 'bots') loadAdminBotsOverview();
}

function _showAdminSubpage(name) {
  // Default view: the admin-cards index. Sub-pages replace the index
  // in place so back/forward navigation feels the same as switching
  // top-level tabs.
  const index = $('admin-index');
  const subCl = $('admin-changelog-manage');
  const subBots = $('admin-bots-overview');
  if (!index) return;
  const subs = [subCl, subBots].filter(Boolean);
  const showIndex = !(name === 'changelog-manage' || name === 'bots');
  index.classList.toggle('hidden', !showIndex);
  subs.forEach((el) => el.classList.add('hidden'));
  if (name === 'changelog-manage' && subCl) subCl.classList.remove('hidden');
  if (name === 'bots' && subBots) subBots.classList.remove('hidden');
}

// ── Admin — Bot Overview (cross-user) ────────────────────────────────────
// Loads /api/admin/bots and renders per-user groups. Lifecycle buttons
// hit the admin endpoints under /api/admin/bots/{uid}/{slug}/{action}
// so the backend double-logs the action into both audit.log AND the
// target bot's own log.
//
// Fase 2 adds checkboxes + status-filter + a bulk-action bar that
// posts selected bots to /api/admin/bots/bulk/{stop,restart}. The
// backend enforces a 20-target cap; the UI mirrors that ceiling and
// surfaces a hint when the operator exceeds it.

const ADMIN_BULK_MAX = 20;
// Selected (user_id, slug) pairs keyed as "uid/slug" so Set handles
// uniqueness natively. Survives filter changes so a temporarily-
// hidden bot stays selected until the operator deselects or acts.
const _adminBulkSelection = new Set();
// Most recent /api/admin/bots payload — kept so filter/selection
// changes can re-render without refetching.
let _adminBotsCache = null;
// Active status filter — "all" | "running" | "stopped".
let _adminBotsFilter = 'all';

async function loadAdminBotsOverview() {
  const status = $('admin-bots-status');
  const container = $('admin-bots-users');
  if (!container) return;
  if (status) {
    status.textContent = 'Loading…';
    status.classList.remove('hidden');
  }
  container.innerHTML = '';
  try {
    const r = await fetch('/api/admin/bots');
    if (r.status === 403) {
      if (status) status.textContent = 'Admin access required.';
      _adminBotsCache = null;
      _updateBulkBar();
      return;
    }
    if (!r.ok) {
      if (status) {
        status.textContent = 'Could not load bots (' + r.status + ').';
      }
      _adminBotsCache = null;
      _updateBulkBar();
      return;
    }
    const j = await r.json();
    _adminBotsCache = j;
    _renderAdminBotsFromCache();
  } catch (e) {
    if (status) status.textContent = 'Network error loading bots.';
    _adminBotsCache = null;
    _updateBulkBar();
  }
}

function _renderAdminBotsFromCache() {
  const status = $('admin-bots-status');
  const container = $('admin-bots-users');
  if (!container) return;
  container.innerHTML = '';
  const users = (_adminBotsCache && _adminBotsCache.users) || [];
  if (users.length === 0) {
    if (status) {
      status.textContent = 'No bots configured across any user.';
      status.classList.remove('hidden');
    }
    _updateBulkBar();
    return;
  }
  // Apply the status filter per user-group, then drop groups with
  // no surviving bots so empty headers don't litter the page.
  let rendered = 0;
  users.forEach((u) => {
    const filtered = _applyAdminFilter(u.bots || []);
    if (filtered.length === 0) return;
    container.appendChild(
      _renderAdminUserGroup({ ...u, bots: filtered }),
    );
    rendered += 1;
  });
  if (rendered === 0) {
    if (status) {
      status.textContent = 'No bots match the current filter.';
      status.classList.remove('hidden');
    }
  } else if (status) {
    status.classList.add('hidden');
  }
  _updateBulkBar();
}

function _applyAdminFilter(bots) {
  if (_adminBotsFilter === 'running') return bots.filter((b) => b.running);
  if (_adminBotsFilter === 'stopped') return bots.filter((b) => !b.running);
  return bots;
}

function _adminSelectionKey(userId, slug) {
  return Number(userId) + '/' + String(slug);
}

function _renderAdminUserGroup(userEntry) {
  const group = document.createElement('section');
  group.className = 'admin-user-group';

  const headerRow = document.createElement('div');
  headerRow.className = 'admin-user-header-row';

  const header = document.createElement('div');
  header.className = 'admin-user-header';
  header.textContent = String(userEntry.username || 'user');
  const meta = document.createElement('span');
  meta.className = 'admin-user-header-meta';
  meta.textContent = '(user_id=' + Number(userEntry.user_id) + ')';
  header.appendChild(meta);
  headerRow.appendChild(header);

  const bots = Array.isArray(userEntry.bots) ? userEntry.bots : [];
  // One button flips both ways: select-all if any are unselected,
  // deselect-all once every visible bot in the group is selected.
  // Only considers bots that survived the current filter.
  if (bots.length > 0) {
    const allSelected = bots.every((b) => _adminBulkSelection.has(
      _adminSelectionKey(userEntry.user_id, b.slug),
    ));
    const selectAllBtn = document.createElement('button');
    selectAllBtn.type = 'button';
    selectAllBtn.className = 'hbtn hbtn-theme admin-user-select-all';
    selectAllBtn.textContent = allSelected ? 'Deselect all' : 'Select all';
    selectAllBtn.addEventListener('click', () => {
      const shouldSelect = !allSelected;
      bots.forEach((b) => {
        const key = _adminSelectionKey(userEntry.user_id, b.slug);
        if (shouldSelect) _adminBulkSelection.add(key);
        else _adminBulkSelection.delete(key);
      });
      _renderAdminBotsFromCache();
    });
    headerRow.appendChild(selectAllBtn);
  }
  group.appendChild(headerRow);

  const grid = document.createElement('div');
  grid.className = 'admin-bot-grid';
  if (bots.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'admin-bot-empty';
    empty.textContent = 'No bots for this user.';
    grid.appendChild(empty);
  } else {
    bots.forEach((b) => {
      grid.appendChild(_renderAdminBotCard(Number(userEntry.user_id), b));
    });
  }
  group.appendChild(grid);
  return group;
}

function _renderAdminBotCard(userId, bot) {
  const card = document.createElement('div');
  card.className = 'card admin-bot-card';
  const selectionKey = _adminSelectionKey(userId, bot.slug);
  const isSelected = _adminBulkSelection.has(selectionKey);
  if (isSelected) card.classList.add('is-selected');

  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.className = 'admin-bot-checkbox';
  checkbox.checked = isSelected;
  checkbox.setAttribute(
    'aria-label',
    'Select bot ' + (bot.name || bot.slug) + ' for bulk action',
  );
  checkbox.addEventListener('change', () => {
    if (checkbox.checked) {
      _adminBulkSelection.add(selectionKey);
      card.classList.add('is-selected');
    } else {
      _adminBulkSelection.delete(selectionKey);
      card.classList.remove('is-selected');
    }
    _updateBulkBar();
    _refreshSelectAllButtons();
  });
  card.appendChild(checkbox);

  const head = document.createElement('div');
  head.className = 'admin-bot-card-head';
  const name = document.createElement('span');
  name.className = 'admin-bot-card-name';
  name.textContent = bot.name || bot.slug;
  const pill = document.createElement('div');
  pill.className = 'pill ' + (bot.running ? 'running' : 'stopped') + ' tab-pill-static';
  const dot = document.createElement('div');
  dot.className = 'dot';
  const pillLabel = document.createElement('span');
  pillLabel.textContent = bot.running ? 'Running' : 'Stopped';
  pill.appendChild(dot);
  pill.appendChild(pillLabel);
  head.appendChild(name);
  head.appendChild(pill);
  card.appendChild(head);

  const meta = document.createElement('div');
  meta.className = 'admin-bot-card-meta';
  const mode = String(bot.mode || 'paper').toUpperCase();
  const exchange = String(bot.exchange || '—').toUpperCase();
  const pair = String(bot.pair || '—');
  meta.textContent = `${exchange} · ${pair} · ${mode}`;
  card.appendChild(meta);

  const stats = document.createElement('div');
  stats.className = 'admin-bot-card-stats';
  stats.appendChild(_renderAdminStat('Price', bot.current_price
    ? fmtPrice(bot.current_price) : '—'));
  const balance = Number(bot.balance_btc) || 0;
  stats.appendChild(_renderAdminStat(
    'Balance', balance ? balance.toFixed(4) : '—',
  ));
  const pnl = Number(bot.total_pnl_btc) || 0;
  const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu';
  stats.appendChild(_renderAdminStat(
    'PnL', (pnl >= 0 ? '+' : '') + pnl.toFixed(6), pnlCls,
  ));
  const winRate = Number(bot.win_rate) || 0;
  stats.appendChild(_renderAdminStat('Win rate', winRate.toFixed(0) + '%'));
  stats.appendChild(_renderAdminStat(
    'Open', String(Number(bot.open_deals_count) || 0),
  ));
  stats.appendChild(_renderAdminStat(
    'Closed', String(Number(bot.closed_deals_count) || 0),
  ));
  card.appendChild(stats);

  const actions = document.createElement('div');
  actions.className = 'admin-bot-card-actions';
  const isLive = String(bot.mode || '').toLowerCase() === 'live';
  if (bot.running) {
    actions.appendChild(_mkAdminBtn(
      '■ Stop', 'btn-sm btn-stop',
      () => _handleAdminLifecycle(userId, bot.slug, 'stop'),
    ));
    actions.appendChild(_mkAdminBtn(
      '↺ Restart', 'btn-sm btn-restart',
      () => _handleAdminLifecycle(userId, bot.slug, 'restart'),
    ));
  } else if (isLive) {
    actions.appendChild(_mkAdminBtn(
      '▶ Start dry-run', 'btn-sm btn-warning',
      () => _handleAdminLifecycle(userId, bot.slug, 'start-dry-run'),
    ));
  } else {
    actions.appendChild(_mkAdminBtn(
      '▶ Start', 'btn-sm btn-start',
      () => _handleAdminLifecycle(userId, bot.slug, 'start'),
    ));
  }
  card.appendChild(actions);
  return card;
}

function _renderAdminStat(label, value, valueCls = '') {
  const wrap = document.createElement('div');
  const l = document.createElement('div');
  l.className = 'admin-bot-stat-label';
  l.textContent = label;
  const v = document.createElement('div');
  v.className = 'admin-bot-stat-value ' + valueCls;
  v.textContent = value;
  wrap.appendChild(l);
  wrap.appendChild(v);
  return wrap;
}

function _mkAdminBtn(label, cls, onClick) {
  const btn = document.createElement('button');
  btn.className = cls;
  btn.textContent = label;
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    onClick();
  });
  return btn;
}

async function _handleAdminLifecycle(userId, slug, action) {
  // action ∈ {"start", "stop", "restart", "start-dry-run"}
  const url = '/api/admin/bots/' + Number(userId)
    + '/' + encodeURIComponent(slug) + '/' + action;
  try {
    const r = await fetch(url, { method: 'POST' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.ok === false) {
      const detail = (data && (data.detail || data.error)) || r.statusText;
      alert(`Admin ${action} failed for ${slug}: ${detail}`);
      return;
    }
    // Re-fetch the overview so running-state + PIDs reflect reality.
    loadAdminBotsOverview();
  } catch (e) {
    alert(`Admin ${action} request failed: ${(e && e.message) || e}`);
  }
}

// Emergency-stop modal — moved out of the profile dropdown so it
// sits next to the Admin Bot Overview where it belongs. The
// confirmation is a custom modal (not window.confirm) so the
// destructive button styling makes the consequence obvious.

function _openEmergencyStopModal() {
  const modal = $('emergency-stop-modal');
  if (!modal) return;
  modal.classList.add('show');
}

function _closeEmergencyStopModal() {
  const modal = $('emergency-stop-modal');
  if (!modal) return;
  modal.classList.remove('show');
}

async function _confirmEmergencyStop() {
  _closeEmergencyStopModal();
  try {
    const res = await fetch('/api/emergency-stop', { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = (data && (data.detail || data.error)) || res.statusText;
      alert('Emergency stop failed: ' + detail);
      return;
    }
    const stopped = (data.stopped_bots || []).join(', ') || 'none';
    const failed = (data.failed || []).length;
    alert(`Emergency stop complete. Stopped: ${stopped}. Failed: ${failed}.`);
    // Refresh the overview so the running pills flip back to "Stopped".
    loadAdminBotsOverview();
  } catch (e) {
    alert('Emergency stop request failed: ' + (e && e.message || e));
  }
}

// ── Admin Bot Overview — Fase 2 bulk-bar + modals ────────────────────────

function _updateBulkBar() {
  // Runs after every render / selection change so the count +
  // disabled states + over-cap hint stay in sync.
  const count = _adminBulkSelection.size;
  const countEl = $('admin-bots-bulk-count');
  const stopBtn = $('admin-bulk-stop-btn');
  const restartBtn = $('admin-bulk-restart-btn');
  const hint = $('admin-bots-bulk-hint');
  if (countEl) countEl.textContent = `${count} selected`;
  const overCap = count > ADMIN_BULK_MAX;
  const canAct = count > 0 && !overCap;
  if (stopBtn) {
    stopBtn.disabled = !canAct;
    stopBtn.title = overCap
      ? `Bulk operations are limited to ${ADMIN_BULK_MAX} bots at a time.`
      : '';
  }
  if (restartBtn) {
    restartBtn.disabled = !canAct;
    restartBtn.title = stopBtn ? stopBtn.title : '';
  }
  if (hint) {
    if (overCap) hint.removeAttribute('hidden');
    else hint.setAttribute('hidden', '');
  }
}

function _refreshSelectAllButtons() {
  // The "Select all" label flips between "Select all" and
  // "Deselect all" based on whether every visible bot in the group
  // is currently selected. A full re-render of the groups is the
  // simplest way to keep labels + card classes + checkbox state
  // all coherent, and the grids are small enough that DOM churn is
  // not a concern.
  _renderAdminBotsFromCache();
}

function _selectedTargetsInOrder() {
  // Flatten selected (uid, slug) pairs, preserving the order in
  // which they appear in the current cache so the confirmation
  // modal and the request payload stay grouped by user.
  const targets = [];
  if (!_adminBotsCache) return targets;
  for (const u of _adminBotsCache.users || []) {
    for (const b of u.bots || []) {
      const key = _adminSelectionKey(u.user_id, b.slug);
      if (_adminBulkSelection.has(key)) {
        targets.push({
          user_id: Number(u.user_id),
          slug: String(b.slug),
          username: u.username,
        });
      }
    }
  }
  return targets;
}

function _openBulkModal(kind) {
  // kind ∈ {"stop", "restart"}
  const targets = _selectedTargetsInOrder();
  if (targets.length === 0) return;
  if (targets.length > ADMIN_BULK_MAX) {
    alert(
      `Bulk operations are limited to ${ADMIN_BULK_MAX} bots at a time. `
      + `Deselect ${targets.length - ADMIN_BULK_MAX} to continue.`,
    );
    return;
  }
  const modal = $('bulk-' + kind + '-modal');
  const list = $('bulk-' + kind + '-list');
  const countEl = $('bulk-' + kind + '-count');
  const countBtn = $('bulk-' + kind + '-count-btn');
  const moreEl = $('bulk-' + kind + '-more');
  if (!modal || !list || !countEl || !countBtn) return;

  list.innerHTML = '';
  const SHOW = 10;
  targets.slice(0, SHOW).forEach((t) => {
    const li = document.createElement('li');
    li.textContent = (t.username || ('user_' + t.user_id)) + '/' + t.slug;
    list.appendChild(li);
  });
  if (moreEl) {
    if (targets.length > SHOW) {
      moreEl.textContent = `…and ${targets.length - SHOW} more`;
      moreEl.removeAttribute('hidden');
    } else {
      moreEl.setAttribute('hidden', '');
    }
  }
  countEl.textContent = String(targets.length);
  countBtn.textContent = String(targets.length);
  modal.classList.add('show');
}

function _closeBulkModal(kind) {
  const modal = $('bulk-' + kind + '-modal');
  if (modal) modal.classList.remove('show');
}

async function _confirmBulkAction(kind) {
  // kind ∈ {"stop", "restart"}
  _closeBulkModal(kind);
  const targets = _selectedTargetsInOrder();
  if (targets.length === 0) return;
  const payload = {
    bots: targets.map((t) => ({ user_id: t.user_id, slug: t.slug })),
  };
  try {
    const r = await fetch('/api/admin/bots/bulk/' + kind, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = (data && (data.detail || data.error)) || r.statusText;
      alert(`Bulk ${kind} request failed: ${detail}`);
      return;
    }
    const succeeded = Number(data.total_succeeded) || 0;
    const failed = Number(data.total_failed) || 0;
    const verb = kind === 'stop' ? 'Stopped' : 'Restarted';
    if (failed === 0) {
      alert(`${verb} ${succeeded} bot${succeeded === 1 ? '' : 's'} successfully.`);
    } else {
      const firstFail = (data.failed && data.failed[0]) || {};
      alert(
        `${verb} ${succeeded} bot${succeeded === 1 ? '' : 's'}; `
        + `${failed} failed — first error: `
        + `${firstFail.slug || '?'}: ${firstFail.error || 'unknown'}`,
      );
    }
    // Clear selection on any successful request (even partial) —
    // the operator will re-select whatever still needs attention
    // after the refetch shows fresh running-state.
    _adminBulkSelection.clear();
    loadAdminBotsOverview();
  } catch (e) {
    alert(`Bulk ${kind} request failed: ${(e && e.message) || e}`);
  }
}

// ── Workspace — PR 2 skeleton with GridStack ─────────────────────────────
// Modular dashboard: the operator drops panels onto a 12-col grid,
// drags them around by the header, and resizes them from the SE
// handle. Layout persists via PR 1's /api/dashboard/layout endpoint
// (auto-saved, 500ms debounce). PR 3 will introduce a "chart"
// panel type; PR 4 "open_deals". For now only "empty" ships, so the
// grid mechanics can be exercised without coupling to live data.

const WORKSPACE_LAYOUT_VERSION = 1;
const WORKSPACE_SAVE_DEBOUNCE_MS = 500;

let _workspaceGrid = null;               // GridStack instance (single)
let _workspaceInitInFlight = false;      // first-load re-entrancy guard
let _workspaceSaveTimer = null;
let _workspaceSuppressSave = false;      // true during initial render

function _workspaceNewPanelId() {
  // Prefer crypto.randomUUID when available; fall back to a
  // Math.random-based 8-hex chunk so older browsers (or test
  // harnesses without crypto.subtle) still get a stable-looking
  // id. Collision probability is irrelevant at the layout scale
  // we operate at (tens of panels per user).
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return 'panel-' + window.crypto.randomUUID();
  }
  return 'panel-' + Math.random().toString(16).slice(2, 10);
}

async function initWorkspace() {
  // Idempotent: first call bootstraps the GridStack instance and
  // loads the saved layout; later calls (tab-switch back to
  // Workspace) do nothing because GridStack's internal state is
  // already in-DOM and a redundant load would churn the grid.
  if (_workspaceGrid || _workspaceInitInFlight) return;
  if (typeof GridStack === 'undefined') {
    console.error('Workspace: GridStack global not present — CDN load failed?');
    return;
  }
  _workspaceInitInFlight = true;
  try {
    _workspaceGrid = GridStack.init({
      column: 12,
      cellHeight: 80,
      margin: 10,
      float: false,
      animate: true,
      // Header-only drag so future chart interactions (pan/zoom in
      // PR 3) don't double-hit the grid and trigger panel drags
      // when the operator meant to scroll a candle series.
      draggable: { handle: '.panel-header' },
      resizable: { handles: 'se' },
      minRow: 4,
      alwaysShowResizeHandle: 'mobile',
    }, '#workspace-grid');

    _workspaceGrid.on('change', _queueWorkspaceSave);
    _workspaceGrid.on('added', _queueWorkspaceSave);
    _workspaceGrid.on('removed', _queueWorkspaceSave);

    await _loadWorkspaceLayout();
  } finally {
    _workspaceInitInFlight = false;
  }
}

async function _loadWorkspaceLayout() {
  const indicator = $('workspace-save-indicator');
  try {
    const r = await fetch('/api/dashboard/layout');
    if (r.status === 401) {
      // Session expired — send the operator back through login.
      // Matches the silent-redirect pattern used elsewhere in app.js.
      window.location.reload();
      return;
    }
    if (!r.ok) throw new Error('load failed: ' + r.status);
    const data = await r.json();
    const layout = (data && data.layout) || null;
    if (layout && layout.version === WORKSPACE_LAYOUT_VERSION
        && Array.isArray(layout.panels)) {
      _renderWorkspaceFromLayout(layout.panels);
    } else {
      // Empty state — no layout stored yet, or an unknown version.
      // Future-proofing: an incompatible version surfaces as empty
      // so the next save migrates the user to v1 implicitly.
      _showWorkspaceEmptyState(true);
    }
    if (indicator) {
      indicator.textContent = 'Saved';
      indicator.classList.remove('saving', 'error');
    }
  } catch (e) {
    console.warn('Workspace: layout load failed:', e);
    _showWorkspaceEmptyState(true);
    if (indicator) {
      indicator.textContent = 'Load failed';
      indicator.classList.remove('saving');
      indicator.classList.add('error');
    }
  }
}

function _renderWorkspaceFromLayout(panels) {
  if (!_workspaceGrid) return;
  // Initial render — suppress save callbacks so we don't round-trip
  // the exact same layout back to the server on page-load.
  _workspaceSuppressSave = true;
  try {
    _workspaceGrid.removeAll(false);
    panels.forEach((p) => {
      const el = _createPanelElement(
        p.id, p.type || 'empty', p.config || {},
        {
          x: Number(p.x) || 0,
          y: Number(p.y) || 0,
          w: Math.max(1, Number(p.w) || 4),
          h: Math.max(1, Number(p.h) || 3),
        },
      );
      _attachPanelToGrid(el);
    });
  } finally {
    _workspaceSuppressSave = false;
  }
  _showWorkspaceEmptyState(panels.length === 0);
}

function _attachPanelToGrid(el) {
  // GridStack v11 removed the HTMLElement overload of addWidget:
  // panels must already live inside the grid container, then
  // ``makeWidget`` promotes the element and reads its gs-*
  // attributes for positioning and sizing. Placing the element
  // before the call avoids a second reflow that the old addWidget
  // path hid behind its own DOM manipulation.
  _workspaceGrid.el.appendChild(el);
  _workspaceGrid.makeWidget(el);
  // Panel factories defer their own async init until the grid cell
  // has a measurable rect. For chart-panels LWC renders at 0×0
  // otherwise; for open-deals-panels the initial fetch can start
  // straight away but we still funnel through the same hook so
  // every factory has one place to hang async startup off.
  if (el._panelHandle) _initPanel(el);
}

function _showWorkspaceEmptyState(show) {
  const empty = $('workspace-empty-state');
  if (empty) empty.classList.toggle('hidden', !show);
}

function _createPanelElement(panelId, panelType, config, gridAttrs) {
  // GridStack v11 ``makeWidget`` reads size + position from gs-*
  // attributes on the element instead of the old addWidget
  // options bag. ``gridAttrs`` is a plain object:
  //   { x, y, w, h, autoPosition }
  // Every key is optional — an add-panel call passes
  // {w, h, autoPosition: true} and lets GridStack find the slot;
  // a restore call passes the full {x, y, w, h} from storage.
  // ``dataset.panel*`` fields survive the round-trip through
  // save/load and let the renderer re-emit the same panel type
  // next session.
  const wrap = document.createElement('div');
  wrap.className = 'grid-stack-item';
  wrap.dataset.panelId = panelId;
  wrap.dataset.panelType = panelType;
  try {
    wrap.dataset.panelConfig = JSON.stringify(config || {});
  } catch (_) {
    wrap.dataset.panelConfig = '{}';
  }

  const attrs = gridAttrs || {};
  if (typeof attrs.x === 'number') wrap.setAttribute('gs-x', String(attrs.x));
  if (typeof attrs.y === 'number') wrap.setAttribute('gs-y', String(attrs.y));
  if (typeof attrs.w === 'number') wrap.setAttribute('gs-w', String(attrs.w));
  if (typeof attrs.h === 'number') wrap.setAttribute('gs-h', String(attrs.h));
  if (attrs.autoPosition) wrap.setAttribute('gs-auto-position', 'true');

  const content = document.createElement('div');
  content.className = 'grid-stack-item-content';
  wrap.appendChild(content);

  if (panelType === 'chart') {
    // The factory in chart_module.js builds its own header/body
    // inside `content` — including a remove button that needs to
    // call back into the grid. Everything else (LWC instance,
    // indicator series, annotations, binding) is owned by the
    // handle we stash on the wrap element for later lookup by the
    // save / WS-dispatch / destroy paths.
    if (!window.RevertoChart || typeof window.RevertoChart.createPanelChart !== 'function') {
      const fallback = document.createElement('div');
      fallback.className = 'panel';
      fallback.innerHTML = '<div class="panel-header"><span class="panel-title">Chart</span></div>'
        + '<div class="panel-body"><p class="panel-placeholder">chart_module.js not loaded — refresh the page.</p></div>';
      content.appendChild(fallback);
      return wrap;
    }
    const handle = window.RevertoChart.createPanelChart(content, {
      panelId,
      pair: (config && config.pair) || 'BTC/USD',
      timeframe: (config && config.timeframe) || '1h',
      indicators: (config && config.indicators) || [],
      boundBotSlug: (config && config.boundBotSlug) || null,
      boundBotUserId: (config && config.boundBotUserId) || null,
      // Pass both fields through — the factory's state-init
      // normalises ``timezone`` first and falls back to migrating
      // ``useUtc`` when only the legacy field is set. Once saved,
      // getConfig() only emits ``timezone`` so the layout_json
      // self-heals on next write.
      timezone: (config && config.timezone) || null,
      useUtc: !!(config && config.useUtc),
      onRemove: () => _removeWorkspacePanel(wrap),
      onConfigChange: () => {
        // Persist the chart's current config immediately so a later
        // _saveWorkspaceLayout reads fresh dataset state even if the
        // grid's own 'change' event hasn't fired.
        _syncPanelConfig(wrap);
        _queueWorkspaceSave();
      },
    });
    wrap._panelHandle = handle;
    // ``init`` must wait until the element is in the DOM (LWC needs
    // a real layout rect for width/height). We defer attachment to
    // _attachPanelToGrid, which calls _initPanel post-append.
    return wrap;
  }

  if (panelType === 'open_deals') {
    if (!window.RevertoChart || typeof window.RevertoChart.createOpenDealsPanel !== 'function') {
      const fallback = document.createElement('div');
      fallback.className = 'panel';
      fallback.innerHTML = '<div class="panel-header"><span class="panel-title">Open deals</span></div>'
        + '<div class="panel-body"><p class="panel-placeholder">chart_module.js not loaded — refresh the page.</p></div>';
      content.appendChild(fallback);
      return wrap;
    }
    const handle = window.RevertoChart.createOpenDealsPanel(content, {
      panelId,
      // ACTIVE_DEALS_COLUMNS is declared earlier in this file, same
      // global scope — the open-deals panel is deliberately built on
      // the same cell renderers + sortValue extractors so action
      // buttons, pnl formatting, and sort semantics match the
      // Active Deals page row-for-row.
      columnDefs: ACTIVE_DEALS_COLUMNS,
      visibleColumns: (config && Array.isArray(config.visibleColumns))
        ? config.visibleColumns : null,
      columnOrder: (config && Array.isArray(config.columnOrder))
        ? config.columnOrder : null,
      sort: (config && config.sort) || null,
      onRemove: () => _removeWorkspacePanel(wrap),
      onConfigChange: () => {
        _syncPanelConfig(wrap);
        _queueWorkspaceSave();
      },
    });
    wrap._panelHandle = handle;
    return wrap;
  }

  const panel = document.createElement('div');
  panel.className = 'panel';

  const header = document.createElement('div');
  header.className = 'panel-header';
  const title = document.createElement('span');
  title.className = 'panel-title';
  title.textContent = _panelTitleForType(panelType);
  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'panel-remove';
  removeBtn.setAttribute('aria-label', 'Remove panel');
  removeBtn.textContent = '×';
  removeBtn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    _removeWorkspacePanel(wrap);
  });
  header.appendChild(title);
  header.appendChild(removeBtn);

  const body = document.createElement('div');
  body.className = 'panel-body';
  const placeholder = document.createElement('p');
  placeholder.className = 'panel-placeholder';
  placeholder.textContent = _panelPlaceholderForType(panelType);
  body.appendChild(placeholder);

  panel.appendChild(header);
  panel.appendChild(body);
  content.appendChild(panel);
  return wrap;
}

function _removeWorkspacePanel(wrap) {
  if (!_workspaceGrid || !wrap) return;
  // Let the panel's factory release its resources (candle refresh,
  // LWC instance, state-refetch timers, annotation rows …) before
  // the grid removes the DOM node, so nothing keeps firing against
  // a detached container.
  if (wrap._panelHandle && typeof wrap._panelHandle.destroy === 'function') {
    try { wrap._panelHandle.destroy(); } catch (e) {}
    wrap._panelHandle = null;
  }
  _workspaceGrid.removeWidget(wrap);
  if (_workspaceGrid.engine.nodes.length === 0) {
    _showWorkspaceEmptyState(true);
  }
}

function _syncPanelConfig(wrap) {
  if (!wrap || !wrap._panelHandle || typeof wrap._panelHandle.getConfig !== 'function') return;
  try {
    wrap.dataset.panelConfig = JSON.stringify(wrap._panelHandle.getConfig());
  } catch (e) { /* leave previous config on error */ }
}

async function _initPanel(wrap) {
  if (!wrap || !wrap._panelHandle) return;
  try {
    await wrap._panelHandle.init();
  } catch (e) {
    console.warn('Workspace chart panel init failed:', e);
  }
}

function _panelTitleForType(type) {
  if (type === 'chart') return 'Chart';
  if (type === 'open_deals') return 'Open deals';
  return 'Empty panel';
}

function _panelPlaceholderForType(type) {
  // Defensive fallback for unknown panel types in saved layouts
  // (e.g. a future build introduced a type the current build
  // doesn't know how to render). The non-empty branch is no
  // longer reachable from the current panel-type menu, but we
  // keep the guard so loading a newer layout in an older tab
  // still surfaces something instead of a silent blank cell.
  if (type && type !== 'empty') {
    return 'Panel type "' + type + '" not yet implemented on this build.';
  }
  return 'Placeholder.';
}

function _queueWorkspaceSave() {
  if (_workspaceSuppressSave) return;
  // Hide the empty-state pane the moment any panel appears — the
  // 'added' event fires before the operator's cursor even leaves
  // the button, so the hint visibility tracks reality without a
  // full re-render.
  if (_workspaceGrid && _workspaceGrid.engine.nodes.length > 0) {
    _showWorkspaceEmptyState(false);
  }
  const indicator = $('workspace-save-indicator');
  if (indicator) {
    indicator.textContent = 'Saving…';
    indicator.classList.remove('error');
    indicator.classList.add('saving');
  }
  clearTimeout(_workspaceSaveTimer);
  _workspaceSaveTimer = setTimeout(_saveWorkspaceLayout, WORKSPACE_SAVE_DEBOUNCE_MS);
}

async function _saveWorkspaceLayout(isRetry = false) {
  if (!_workspaceGrid) return;
  const indicator = $('workspace-save-indicator');
  const panels = _workspaceGrid.engine.nodes.map((node) => {
    // Panels that own a factory handle carry their own mutable state
    // inside a closure (chart pair/timeframe/indicators/binding,
    // open-deals visible columns + sort, …). Prefer the handle's
    // getConfig() over the dataset cache so the saved layout always
    // reflects whichever control the user touched most recently —
    // even if _syncPanelConfig hasn't run yet for the last mutation.
    let config = {};
    if (node.el._panelHandle && typeof node.el._panelHandle.getConfig === 'function') {
      try { config = node.el._panelHandle.getConfig(); } catch (_) { config = {}; }
    } else {
      try {
        config = JSON.parse(node.el.dataset.panelConfig || '{}');
      } catch (_) {
        config = {};
      }
    }
    return {
      id: node.el.dataset.panelId || _workspaceNewPanelId(),
      type: node.el.dataset.panelType || 'empty',
      x: Number(node.x) || 0,
      y: Number(node.y) || 0,
      w: Number(node.w) || 4,
      h: Number(node.h) || 3,
      config,
    };
  });
  const payload = {
    layout: { version: WORKSPACE_LAYOUT_VERSION, panels },
  };
  try {
    const r = await fetch('/api/dashboard/layout', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (r.status === 400) {
      const data = await r.json().catch(() => ({}));
      const detail = (data && data.detail) || 'layout rejected';
      if (/max size/i.test(detail)) {
        alert(
          'Workspace layout is too complex to save ('
          + detail + '). Remove some panels and try again.',
        );
      }
      if (indicator) {
        indicator.textContent = 'Save failed';
        indicator.classList.remove('saving');
        indicator.classList.add('error');
      }
      return;
    }
    if (r.status === 401) {
      window.location.reload();
      return;
    }
    if (!r.ok) throw new Error('PUT failed: ' + r.status);
    if (indicator) {
      indicator.textContent = 'Saved';
      indicator.classList.remove('saving', 'error');
    }
  } catch (e) {
    console.warn('Workspace: layout save failed:', e);
    if (!isRetry) {
      // One retry after 2s covers the typical flaky-network case
      // without hammering the server. If the second attempt also
      // fails we flip the indicator red and stop — the operator
      // can save again by touching any panel.
      setTimeout(() => _saveWorkspaceLayout(true), 2000);
      return;
    }
    if (indicator) {
      indicator.textContent = 'Save failed';
      indicator.classList.remove('saving');
      indicator.classList.add('error');
    }
  }
}

// _handleWorkspaceAddPanel — dropped. The "+ Add panel" button was a
// placeholder from the PR 2 Workspace skeleton before chart + deals
// panel-types shipped their own dedicated add-buttons. Empty panels
// remain a valid panel-type in saved layouts so the factory in
// ``_createPanelElement`` still handles ``panelType === 'empty'``
// on load; there's just no UI path to create new ones.

function _handleWorkspaceAddChartPanel() {
  // Chart panels default to 8×6 cells — enough room for a readable
  // candlestick chart plus the RSI/MACD panes the factory opens
  // below it when those indicators are enabled. Empty panels are
  // still 4×3; the two defaults are intentional so operators don't
  // have to resize every chart they add.
  _addWorkspacePanel('chart', { w: 8, h: 6 });
}

function _handleWorkspaceAddOpenDealsPanel() {
  // 8×5 gives a comfortable visible row count before the body
  // starts scrolling, matching the chart-panel width so two panels
  // side-by-side in a 12-column grid leave an asymmetric 4-wide
  // slot for a future compact panel type rather than an awkward
  // half-empty fit.
  _addWorkspacePanel('open_deals', { w: 8, h: 5 });
}

function _addWorkspacePanel(type, size) {
  if (!_workspaceGrid) return;
  const panelId = _workspaceNewPanelId();
  const el = _createPanelElement(
    panelId, type, {},
    { w: (size && size.w) || 4, h: (size && size.h) || 3, autoPosition: true },
  );
  _attachPanelToGrid(el);
  _showWorkspaceEmptyState(false);
  // 'added' event fires → _queueWorkspaceSave → persists.
}

// Fan out /ws/state bot-state pushes to every workspace panel that
// cares about state updates. Chart-panels filter by their own
// boundBotSlug; open-deals-panels accept any slug and use the push
// as a debounced "refetch now" trigger. Workspace-grid owns a
// single WS subscription via _stateWs — we don't open one per
// panel, because the /ws/state endpoint is already user-scoped
// (v26-16) and multiplexes all bots into a single stream.
function _dispatchWorkspaceBotState(slug, data) {
  if (!_workspaceGrid) return;
  for (const node of _workspaceGrid.engine.nodes) {
    const h = node.el && node.el._panelHandle;
    if (h && typeof h.handleStateUpdate === 'function') {
      try { h.handleStateUpdate({ slug, data }); } catch (e) {}
    }
  }
}

// ── Changelog — public listing ───────────────────────────────────────────
// Renders /api/changelog inside the Changelog tab. description_html is
// rendered and bleach-sanitised server-side (core.markdown_render) so
// we drop it straight into innerHTML — adding a client-side sanitiser
// would duplicate the trust boundary without strengthening it.

const _CL_CATEGORY_LABELS = {
  feature: 'Feature',
  fix: 'Fix',
  improvement: 'Improvement',
  security: 'Security',
};

function _clFormatDate(ts) {
  if (!ts) return '—';
  // Backend emits "YYYY-MM-DD HH:MM:SS"; the user surface only shows
  // the date half, matching "when was this feature added" rather than
  // exact publish-click time.
  return String(ts).split(' ')[0];
}

function _clCategoryBadge(category) {
  const safe = String(category || '').replace(/[^a-z]/g, '');
  const label = _CL_CATEGORY_LABELS[safe] || safe || '—';
  const badge = document.createElement('span');
  badge.className = `cl-badge cl-badge-${safe}`;
  badge.textContent = label;
  return badge;
}

function _clRenderEntry(entry) {
  const article = document.createElement('article');
  article.className = 'card cl-entry';

  const header = document.createElement('div');
  header.className = 'cl-entry-header';
  const title = document.createElement('h2');
  title.className = 'cl-entry-title';
  title.textContent = entry.title || '';
  const meta = document.createElement('div');
  meta.className = 'cl-entry-meta';
  meta.appendChild(_clCategoryBadge(entry.category));
  const date = document.createElement('span');
  date.className = 'cl-entry-date';
  date.textContent = _clFormatDate(entry.published_at);
  meta.appendChild(date);
  header.appendChild(title);
  header.appendChild(meta);

  const body = document.createElement('div');
  body.className = 'cl-entry-body';
  // innerHTML is safe here: description_html is emitted by bleach on
  // the server (tags/attrs allow-list enforced). See
  // core/markdown_render.py.
  body.innerHTML = entry.description_html || '';

  article.appendChild(header);
  article.appendChild(body);
  return article;
}

async function loadChangelog() {
  const statusEl = $('cl-status');
  const listEl = $('cl-entries');
  if (!statusEl || !listEl) return;
  listEl.innerHTML = '';
  statusEl.classList.remove('hidden');
  statusEl.textContent = 'Loading…';
  try {
    const r = await fetch('/api/changelog');
    if (r.status === 401) { _handle401(); return; }
    if (!r.ok) throw new Error(`status ${r.status}`);
    const data = await r.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];
    if (entries.length === 0) {
      statusEl.textContent = 'No updates yet.';
      return;
    }
    statusEl.classList.add('hidden');
    const frag = document.createDocumentFragment();
    entries.forEach((e) => frag.appendChild(_clRenderEntry(e)));
    listEl.appendChild(frag);
  } catch (e) {
    statusEl.textContent = 'Failed to load changelog.';
  }
}

// ── Admin — changelog CRUD ───────────────────────────────────────────────
// Lists every entry (drafts + published) in a table and wires the
// modal editor for create/edit plus inline publish/unpublish/delete
// actions. Every write goes via the /api/admin/changelog/* surface;
// the server handles validation + audit logging.

// Tracks which entry the modal is currently editing. null = create
// mode, integer = edit mode. Reset on modal close.
let _clEditingId = null;

async function loadAdminChangelog() {
  const tbody = $('admin-cl-tbody');
  if (!tbody) return;
  tbody.innerHTML =
    '<tr><td colspan="5" class="cl-empty-cell">Loading…</td></tr>';
  try {
    const r = await fetch('/api/admin/changelog');
    if (r.status === 401) { _handle401(); return; }
    if (r.status === 403) {
      tbody.innerHTML =
        '<tr><td colspan="5" class="cl-empty-cell">' +
        'Admin access required.' +
        '</td></tr>';
      return;
    }
    if (!r.ok) throw new Error(`status ${r.status}`);
    const data = await r.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];
    if (entries.length === 0) {
      tbody.innerHTML =
        '<tr><td colspan="5" class="cl-empty-cell">' +
        'No entries yet. Click “+ New entry” to add the first one.' +
        '</td></tr>';
      return;
    }
    tbody.innerHTML = '';
    entries.forEach((e) => tbody.appendChild(_clRenderAdminRow(e)));
  } catch (e) {
    tbody.innerHTML =
      '<tr><td colspan="5" class="cl-empty-cell">' +
      'Failed to load entries.' +
      '</td></tr>';
  }
}

function _clRenderAdminRow(entry) {
  const tr = document.createElement('tr');

  const tdTitle = document.createElement('td');
  tdTitle.textContent = entry.title || '';
  tr.appendChild(tdTitle);

  const tdCat = document.createElement('td');
  tdCat.appendChild(_clCategoryBadge(entry.category));
  tr.appendChild(tdCat);

  const tdStatus = document.createElement('td');
  const status = document.createElement('span');
  status.className = 'cl-status ' +
    (entry.is_published ? 'cl-status-published' : 'cl-status-draft');
  status.textContent = entry.is_published ? 'Published' : 'Draft';
  tdStatus.appendChild(status);
  tr.appendChild(tdStatus);

  const tdCreated = document.createElement('td');
  tdCreated.textContent = _clFormatDate(entry.created_at);
  tr.appendChild(tdCreated);

  const tdActions = document.createElement('td');
  tdActions.className = 'cl-actions';

  const editBtn = document.createElement('button');
  editBtn.type = 'button';
  editBtn.className = 'hbtn hbtn-theme';
  editBtn.textContent = 'Edit';
  editBtn.addEventListener('click', () => openClEditModal(entry));
  tdActions.appendChild(editBtn);

  const pubBtn = document.createElement('button');
  pubBtn.type = 'button';
  if (entry.is_published) {
    pubBtn.className = 'hbtn hbtn-theme';
    pubBtn.textContent = 'Unpublish';
    pubBtn.addEventListener('click', () => _clPublishAction(entry.id, false));
  } else {
    pubBtn.className = 'hbtn hbtn-theme btn-accent';
    pubBtn.textContent = 'Publish';
    pubBtn.addEventListener('click', () => _clPublishAction(entry.id, true));
  }
  tdActions.appendChild(pubBtn);

  const delBtn = document.createElement('button');
  delBtn.type = 'button';
  delBtn.className = 'hbtn hbtn-theme btn-danger';
  delBtn.textContent = 'Delete';
  delBtn.addEventListener('click', () => _clDeleteAction(entry.id));
  tdActions.appendChild(delBtn);

  tr.appendChild(tdActions);
  return tr;
}

function openClEditModal(entry) {
  const modal = $('cl-edit-modal');
  if (!modal) return;
  _clEditingId = entry ? entry.id : null;
  $('cl-modal-title').textContent =
    entry ? `Edit entry #${entry.id}` : 'New changelog entry';
  $('cl-modal-title-input').value = entry ? (entry.title || '') : '';
  $('cl-modal-category').value = entry ? (entry.category || 'feature') : 'feature';
  // The API ships raw markdown in ``description`` on admin-shape
  // entries so the edit form can round-trip it unmodified.
  $('cl-modal-description').value = entry ? (entry.description || '') : '';
  const err = $('cl-modal-error');
  err.classList.add('hidden');
  err.textContent = '';
  modal.classList.add('show');
  setTimeout(() => $('cl-modal-title-input').focus(), 30);
}

function closeClEditModal() {
  const modal = $('cl-edit-modal');
  if (!modal) return;
  modal.classList.remove('show');
  _clEditingId = null;
}

function _clShowModalError(msg) {
  const err = $('cl-modal-error');
  if (!err) return;
  err.textContent = msg;
  err.classList.remove('hidden');
}

function _clCollectModalPayload() {
  return {
    title: ($('cl-modal-title-input').value || '').trim(),
    description: ($('cl-modal-description').value || '').trim(),
    category: $('cl-modal-category').value,
  };
}

function _clValidateModalPayload(p) {
  if (!p.title) return 'Title is required.';
  if (!p.description) return 'Description is required.';
  if (!['feature', 'fix', 'improvement', 'security'].includes(p.category)) {
    return 'Invalid category.';
  }
  return null;
}

async function _clSaveModal(publish) {
  const payload = _clCollectModalPayload();
  const err = _clValidateModalPayload(payload);
  if (err) { _clShowModalError(err); return; }

  try {
    let savedId;
    if (_clEditingId === null) {
      // Create
      const r = await fetch('/api/admin/changelog', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        _clShowModalError(await _clErrorMessage(r));
        return;
      }
      const body = await r.json();
      savedId = body.id;
    } else {
      // Update
      const r = await fetch(`/api/admin/changelog/${_clEditingId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        _clShowModalError(await _clErrorMessage(r));
        return;
      }
      savedId = _clEditingId;
    }

    if (publish) {
      const pr = await fetch(
        `/api/admin/changelog/${savedId}/publish`,
        { method: 'POST' },
      );
      if (!pr.ok) {
        _clShowModalError(await _clErrorMessage(pr));
        return;
      }
    }

    closeClEditModal();
    loadAdminChangelog();
  } catch (e) {
    _clShowModalError('Network error — please try again.');
  }
}

async function _clErrorMessage(response) {
  try {
    const j = await response.json();
    if (j && j.detail) return String(j.detail);
  } catch (e) { /* fall through */ }
  return `Request failed (status ${response.status}).`;
}

async function _clPublishAction(entryId, publish) {
  const path = publish
    ? `/api/admin/changelog/${entryId}/publish`
    : `/api/admin/changelog/${entryId}/unpublish`;
  try {
    const r = await fetch(path, { method: 'POST' });
    if (!r.ok) {
      // Non-fatal: reload to reflect current server state anyway.
      console.warn('publish action failed:', r.status);
    }
  } catch (e) {
    console.warn('publish action errored:', e);
  }
  loadAdminChangelog();
}

async function _clDeleteAction(entryId) {
  if (!window.confirm('Delete this entry? This cannot be undone.')) return;
  try {
    const r = await fetch(`/api/admin/changelog/${entryId}`, {
      method: 'DELETE',
    });
    if (!r.ok && r.status !== 204) {
      console.warn('delete failed:', r.status);
    }
  } catch (e) {
    console.warn('delete errored:', e);
  }
  loadAdminChangelog();
}

// ── New bot single-page form ─────────────────────────────────────────────────
// Short inline help shown under the indicator type dropdown so a new
// operator knows what each filter does without leaving the wizard.
const NB_INDICATOR_DESCRIPTIONS = {
  RSI:
    "Measures momentum. Signals when the market is overbought or oversold " +
    "based on recent price changes.",
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
    indicatorGroups: [{ id: 1, name: 'Group 1', indicators: [] }],
    tp_enabled: true,
    tp_price_enabled: true,
    tp_target_pct: 3.0,
    tp_min_pct: null,
    tp_max_age_enabled: false, tp_max_age_hours: 24,
    tpIndicatorGroups: [],
    sl_enabled: true,
    sl_type: 'fixed', sl_pct: 5.0,
    use_wick_simulation: true,
    dca_enabled: true,
    dca_max_orders: 4, dca_size: 0.001, dca_spacing_pct: 2.5,
    dca_volume_scale: 1.0, dca_step_scale: 1.0,
    sched_enabled: false,
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
  // Cancel button is edit-mode-only; keep it hidden on fresh entry.
  const cancelBtn = $('nb-cancel-btn');
  if (cancelBtn) cancelBtn.classList.add('hidden');
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
  el.textContent = '';
  if (Array.isArray(msg)) {
    msg.forEach((m, i) => {
      if (i > 0) el.appendChild(document.createElement('br'));
      el.appendChild(document.createTextNode(m));
    });
  } else {
    el.textContent = msg;
  }
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

  nbState.tp_enabled = $('nb-tp-enabled') ? $('nb-tp-enabled').checked : true;
  nbState.tp_price_enabled = $('nb-tp-price-enabled') ? $('nb-tp-price-enabled').checked : true;
  nbState.tp_target_pct = parseFloat($('nb-tp-pct').value);
  const minRaw = $('nb-tp-min-pct').value;
  nbState.tp_min_pct = minRaw === '' ? null : parseFloat(minRaw);
  nbState.tp_max_age_enabled = $('nb-tp-max-age-enabled').checked;
  nbState.tp_max_age_hours = parseInt($('nb-tp-max-age-hours').value, 10);
  nbState.sl_enabled = $('nb-sl-enabled') ? $('nb-sl-enabled').checked : true;
  nbState.sl_type = $('nb-sl-type').value;
  nbState.sl_pct = parseFloat($('nb-sl-pct').value);
  const wickEl = $('nb-use-wick-sim');
  nbState.use_wick_simulation = wickEl ? Boolean(wickEl.checked) : true;
  nbState.dca_enabled = $('nb-dca-enabled') ? $('nb-dca-enabled').checked : true;
  nbState.sched_enabled = $('nb-sched-enabled') ? $('nb-sched-enabled').checked : false;

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
  if (nbState.sl_enabled && (!nbState.sl_pct || nbState.sl_pct <= 0))
    errors.push('Stop Loss: percentage must be > 0 when Stop Loss is enabled');

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

  if ($('nb-tp-enabled')) $('nb-tp-enabled').checked = nbState.tp_enabled;
  const tpPriceChk = $('nb-tp-price-enabled');
  if (tpPriceChk) tpPriceChk.checked = nbState.tp_price_enabled;
  $('nb-tp-pct').value = nbState.tp_target_pct;
  $('nb-tp-min-pct').value = nbState.tp_min_pct == null ? '' : nbState.tp_min_pct;
  const tpPctRow = $('nb-tp-pct-row'), tpMinRow = $('nb-tp-min-row');
  if (tpPctRow) tpPctRow.style.display = nbState.tp_price_enabled ? '' : 'none';
  if (tpMinRow) tpMinRow.style.display = nbState.tp_price_enabled ? '' : 'none';
  $('nb-tp-max-age-enabled').checked = nbState.tp_max_age_enabled;
  $('nb-tp-max-age-hours').value = nbState.tp_max_age_hours;
  $('nb-tp-max-age-hours').disabled = !nbState.tp_max_age_enabled;
  nbRenderTpIndicators();
  if ($('nb-sl-enabled')) $('nb-sl-enabled').checked = nbState.sl_enabled;
  $('nb-sl-type').value = nbState.sl_type;
  $('nb-sl-pct').value = nbState.sl_pct;
  const wickInput = $('nb-use-wick-sim');
  if (wickInput) wickInput.checked = Boolean(nbState.use_wick_simulation);

  if ($('nb-dca-enabled')) $('nb-dca-enabled').checked = nbState.dca_enabled;
  $('nb-dca-max').value = nbState.dca_max_orders;
  $('nb-dca-size').value = nbState.dca_size;
  $('nb-dca-spacing').value = nbState.dca_spacing_pct;
  $('nb-dca-volume').value = nbState.dca_volume_scale;
  $('nb-dca-step').value = nbState.dca_step_scale;

  if ($('nb-sched-enabled')) $('nb-sched-enabled').checked = nbState.sched_enabled;
  const tzEl = $('nb-sched-tz');
  if (tzEl) tzEl.value = nbState.schedule_timezone || 'Europe/Amsterdam';
  const blEl = $('nb-sched-blackouts');
  if (blEl) blEl.value = (nbState.schedule_blackouts || []).join('\n');

  nbRenderIndicators();
  nbRenderScheduleWindows();
  nbUpdateLeverageUI();
  nbUpdateToggleStates();
}

function nbToggleBaseUnit(unit) {
  nbState.base_unit = unit;
  document.querySelectorAll('[data-base-unit]').forEach(b => {
    b.classList.toggle('active', b.dataset.baseUnit === unit);
  });
  $('nb-base-unit-label').textContent = unit === 'btc' ? 'BTC' : '%';
  $('nb-dca-unit-label').textContent = unit === 'btc' ? 'BTC' : '%';
}

function _nbDefaultIndicator() {
  return {
    type: 'RSI', timeframe: '1h',
    period: 14, threshold: 'below_35',
    rsi_condition: 'below', rsi_value: 35,
    fast: 9, slow: 21, signal: 'bullish_cross',
    condition: 'histogram_positive',
    macd_fast: 12, macd_slow: 26, macd_signal: 9,
  };
}
function nbAddIndicator(groupId) {
  const groups = nbState.indicatorGroups || [];
  if (groupId != null) {
    const g = groups.find(g => g.id === groupId);
    if (g) g.indicators.push(_nbDefaultIndicator());
  } else if (groups.length) {
    groups[0].indicators.push(_nbDefaultIndicator());
  }
  nbRenderIndicators();
  nbRecompute();
}
function nbRemoveIndicator(groupId, idx) {
  // Every indicator in the wizard lives in a group since the v17
  // indicator-groups refactor — the original flat `nbState.indicators`
  // path no longer exists. The else-branch below used to reference
  // that stale path and would crash with a TypeError on any call that
  // omitted groupId. Keeping the else intact for the no-op case so
  // stray callers don't break either.
  if (groupId == null) return;
  const g = nbState.indicatorGroups.find(g => g.id === groupId);
  if (g) g.indicators.splice(idx, 1);
  nbRenderIndicators();
  nbRecompute();
}
function nbAddGroup() {
  const maxId = nbState.indicatorGroups.reduce((m, g) => Math.max(m, g.id), 0);
  nbState.indicatorGroups.push({ id: maxId + 1, name: `Group ${maxId + 1}`, indicators: [] });
  nbRenderIndicators();
  nbRecompute();
}
function nbRemoveGroup(groupId) {
  nbState.indicatorGroups = nbState.indicatorGroups.filter(g => g.id !== groupId);
  nbRenderIndicators();
  nbRecompute();
}
function nbAddTpGroup() {
  const groups = nbState.tpIndicatorGroups || [];
  const maxId = groups.reduce((m, g) => Math.max(m, g.id), 0);
  groups.push({ id: maxId + 1, name: `TP Group ${maxId + 1}`, indicators: [] });
  nbState.tpIndicatorGroups = groups;
  nbRenderTpIndicators();
  nbRecompute();
}
function nbRemoveTpGroup(gid) {
  nbState.tpIndicatorGroups = (nbState.tpIndicatorGroups || []).filter(g => g.id !== gid);
  nbRenderTpIndicators();
  nbRecompute();
}
function nbAddTpIndicator(gid) {
  const g = (nbState.tpIndicatorGroups || []).find(g => g.id === gid);
  if (g) g.indicators.push(_nbDefaultIndicator());
  nbRenderTpIndicators();
  nbRecompute();
}
function nbRemoveTpIndicator(gid, idx) {
  const g = (nbState.tpIndicatorGroups || []).find(g => g.id === gid);
  if (g) g.indicators.splice(idx, 1);
  nbRenderTpIndicators();
  nbRecompute();
}
function nbRenderTpIndicators() {
  const list = $('nb-tp-indicators-list');
  if (!list) return;
  const groups = nbState.tpIndicatorGroups || [];
  if (!groups.length) {
    list.innerHTML = '<div class="empty-config-msg">No indicator TP (price target only)</div>'
      + '<button type="button" class="btn-add-group" data-nb-action="add-tp-group">+ Add TP indicator group</button>';
    return;
  }
  const html = groups.map((g, gi) => {
    const cards = g.indicators.map((ind, ii) => {
      const dp = `tp:${g.id}:${ii}`;
      return _nbIndCardHtml(ind, dp);
    }).join('');
    return (gi > 0 ? '<div class="group-or-divider">OR</div>' : '')
      + `<div class="indicator-group" data-group-id="tp:${g.id}">
          <div class="indicator-group-header">
            <input class="indicator-group-name" value="${safeText(g.name)}"
              placeholder="TP Group ${gi + 1}" data-nb-tp-gname="${g.id}">
            ${groups.length > 1 ? `<button type="button" class="nb-ind-close" data-nb-action="remove-tp-group" data-nb-gid="${g.id}">\u00d7</button>` : ''}
          </div>
          ${cards || '<div class="empty-config-msg" style="margin:8px 0;font-size:11px">No indicators — add one below</div>'}
          <button type="button" class="btn-add-group" data-nb-action="add-tp-indicator" data-nb-gid="${g.id}">+ Add indicator</button>
        </div>`;
  }).join('');
  list.innerHTML = html + '<button type="button" class="btn-add-group" data-nb-action="add-tp-group">+ Add TP indicator group</button>';
}

function _nbIndCardHtml(ind, dataPrefix) {
  const typeClass = ind.type === 'RSI' ? 'type-rsi'
                  : ind.type === 'MACD' ? 'type-macd'
                  : ind.type === 'BOLLINGER' ? 'type-bollinger'
                  : ind.type === 'PARABOLIC_SAR' ? 'type-psar'
                  : ind.type === 'SUPERTREND' ? 'type-supertrend'
                  : ind.type === 'MARKET_STRUCTURE' ? 'type-ms'
                  : ind.type === 'SUPPORT_RESISTANCE' ? 'type-sr'
                  : ind.type === 'QFL' ? 'type-qfl'
                  : ind.type === 'ASAP' ? 'type-asap'
                  : '';
  const title = ind.type === 'BOLLINGER' ? 'Bollinger Bands'
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
        <button type="button" class="nb-ind-close" data-nb-grm="${dataPrefix}" title="Remove indicator" aria-label="Remove indicator">\u00d7</button>
      </div>
      <div class="nb-ind-body">
        <div class="form-row form-row-wide">
          <label>Type</label>
          <select data-nb-gind="${dataPrefix}" data-nb-field="type">
            <option value="RSI" ${ind.type === 'RSI' ? 'selected' : ''}>RSI</option>
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
          <select data-nb-gind="${dataPrefix}" data-nb-field="timeframe">
            ${['15m', '1h', '4h', '1d'].map(t =>
              `<option value="${t}" ${ind.timeframe === t ? 'selected' : ''}>${t}</option>`
            ).join('')}
          </select>
        </div>
        ${nbIndicatorFieldsHtml(ind, dataPrefix)}
      </div>
    </div>`;
}

function nbRenderIndicators() {
  const list = $('nb-indicators-list');
  if (!list) return;
  const groups = nbState.indicatorGroups || [];
  if (!groups.length) {
    list.innerHTML = '<div class="empty-config-msg">No indicator groups configured</div>'
      + '<button type="button" class="btn-add-group" data-nb-action="add-group">+ Add indicator group</button>';
    return;
  }
  const html = groups.map((g, gi) => {
    const cards = g.indicators.map((ind, ii) => {
      const dp = `${g.id}:${ii}`;
      return _nbIndCardHtml(ind, dp);
    }).join('');
    return (gi > 0 ? '<div class="group-or-divider">OR</div>' : '')
      + `<div class="indicator-group" data-group-id="${g.id}">
          <div class="indicator-group-header">
            <input class="indicator-group-name" value="${safeText(g.name)}"
              placeholder="Group ${gi + 1}" data-nb-gname="${g.id}">
            ${groups.length > 1 ? `<button type="button" class="nb-ind-close" data-nb-gremove="${g.id}" title="Remove group" aria-label="Remove group">\u00d7</button>` : ''}
          </div>
          ${cards || '<div class="empty-config-msg" style="margin:8px 0;font-size:11px">No indicators — add one below</div>'}
          <button type="button" class="btn-add-group" data-nb-action="add-indicator" data-nb-gid="${g.id}">+ Add indicator</button>
        </div>`;
  }).join('');
  list.innerHTML = html + '<button type="button" class="btn-add-group" data-nb-action="add-group">+ Add group</button>';
}

function nbIndicatorFieldsHtml(ind, i) {
  if (ind.type === 'RSI') {
    const parsed = _parseRsiThreshold(ind.threshold);
    const cond = ind.rsi_condition || parsed.condition;
    const val = ind.rsi_value != null ? ind.rsi_value : parsed.value;
    const ps = ind.price_source || 'close';
    const CONDS = [
      ['cross_above', 'Crosses above X'],
      ['cross_below', 'Crosses below X'],
      ['above', 'Greater than X'],
      ['below', 'Lower than X'],
      ['rsi_cross_above_50', 'Centerline cross up (50)'],
      ['rsi_cross_below_50', 'Centerline cross down (50)'],
      ['rsi_bullish_divergence', 'Bullish divergence'],
      ['rsi_bearish_divergence', 'Bearish divergence'],
    ];
    const SOURCES = ['close','open','high','low','hl2','hlc3','ohlc4'];
    return `
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Number of candles used to calculate RSI (default 14)">Period</label>
        <input type="number" min="5" max="50" value="${ind.period}" data-nb-ind="${i}" data-nb-field="period">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Price data used for RSI calculation. Close is standard. High/Low can detect wicks, Open reflects gap behavior.">Source</label>
        <select data-nb-ind="${i}" data-nb-field="price_source">
          ${SOURCES.map(s => `<option value="${s}" ${ps === s ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Signal trigger: oversold/overbought threshold, crossing, centerline cross, or divergence">Condition</label>
        <select data-nb-ind="${i}" data-nb-field="rsi_condition">
          ${CONDS.map(([v, label]) =>
            `<option value="${v}" ${cond === v ? 'selected' : ''}>${label}</option>`
          ).join('')}
        </select>
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="RSI value that triggers the condition">Value (X)</label>
        <input type="number" min="1" max="99" step="1" value="${val}" data-nb-ind="${i}" data-nb-field="rsi_value">
      </div>`;
  }
  if (ind.type === 'QFL') {
    const basePer = ind.base_periods != null ? ind.base_periods : 36;
    const pumpPer = ind.pump_periods != null ? ind.pump_periods : 8;
    const pfb = ind.pump_from_base_pct != null ? ind.pump_from_base_pct : 3.0;
    const bcp = ind.base_crack_pct != null ? ind.base_crack_pct : 3.0;
    const QFL_CONDS = [
      ['below_base', 'Below base'],
      ['near_base', 'Near base'],
      ['base_retest', 'Base retest'],
    ];
    return `
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Number of candles to look for a low that may form a new base">Base periods</label>
        <input type="number" min="1" value="${basePer}" data-nb-ind="${i}" data-nb-field="base_periods">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Number of candles price must stay above the low to confirm a new base">Pump periods</label>
        <input type="number" min="1" value="${pumpPer}" data-nb-ind="${i}" data-nb-field="pump_periods">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Minimum % pump from base required before a crack signal is valid">Pump from base %</label>
        <input type="number" min="0.1" step="0.1" value="${pfb}" data-nb-ind="${i}" data-nb-field="pump_from_base_pct">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="% below base that triggers the buy signal. Buy limit = base x (1 - crack%)">Base crack %</label>
        <input type="number" min="0.1" step="0.1" value="${bcp}" data-nb-ind="${i}" data-nb-field="base_crack_pct">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Below base = buy limit active. Near base = price approaching. Retest = price returning to base.">Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${QFL_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'SUPPORT_RESISTANCE') {
    const lbars = ind.left_bars != null ? ind.left_bars : 15;
    const rbars = ind.right_bars != null ? ind.right_bars : 15;
    const prox = ind.proximity_pct != null ? ind.proximity_pct : 1.0;
    const volThr = ind.volume_threshold != null ? ind.volume_threshold : 0;
    const minT = ind.min_touches != null ? ind.min_touches : 1;
    const val = ind.value || 'resistance';
    const SR_CONDS = [
      ['price_crossing_up', 'Price crossing up'],
      ['price_crossing_down', 'Price crossing down'],
      ['price_greater_than', 'Price greater than'],
      ['price_lower_than', 'Price lower than'],
      ['near_support', 'Near support'],
      ['near_resistance', 'Near resistance'],
    ];
    return `
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Candles to the left required to confirm a pivot point">Left bars</label>
        <input type="number" min="1" value="${lbars}" data-nb-ind="${i}" data-nb-field="left_bars">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Candles to the right required to confirm a pivot point. Higher = slower but more reliable.">Right bars</label>
        <input type="number" min="1" value="${rbars}" data-nb-ind="${i}" data-nb-field="right_bars">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Apply condition to Support or Resistance level">Level</label>
        <select data-nb-ind="${i}" data-nb-field="value">
          <option value="support" ${val === 'support' ? 'selected' : ''}>Support</option>
          <option value="resistance" ${val === 'resistance' ? 'selected' : ''}>Resistance</option>
        </select>
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Price is considered &#39;near&#39; a level within this percentage">Proximity %</label>
        <input type="number" min="0" step="0.1" value="${prox}" data-nb-ind="${i}" data-nb-field="proximity_pct">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Minimum volume momentum for a pivot to be valid. Uses EMA(5)/EMA(10) oscillator. 0 = disabled.">Volume threshold</label>
        <input type="number" min="0" step="1" value="${volThr}" data-nb-ind="${i}" data-nb-field="volume_threshold">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Number of times price must test a level before it becomes active. Higher = stronger levels only.">Min touches</label>
        <input type="number" min="1" max="10" value="${minT}" data-nb-ind="${i}" data-nb-field="min_touches">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="When to trigger: crossing through level, proximity, or relative position">Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${SR_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'MARKET_STRUCTURE') {
    const lb = ind.lookback != null ? ind.lookback : 3;
    const val = ind.value || 'bullish';
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
        <label class="param-label-toggle" data-hint="Number of candles to detect swing highs and lows">Lookback</label>
        <input type="number" min="1" value="${lb}" data-nb-ind="${i}" data-nb-field="lookback">
      </div>
      <div class="form-row">
        <label>Bias</label>
        <select data-nb-ind="${i}" data-nb-field="value">
          <option value="bullish" ${val === 'bullish' ? 'selected' : ''}>Bullish</option>
          <option value="bearish" ${val === 'bearish' ? 'selected' : ''}>Bearish</option>
        </select>
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="BOS = Break of Structure. Higher low = bullish continuation.">Condition</label>
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
      ['bullish', 'Uptrend'],
      ['bearish', 'Downtrend'],
      ['from_down_to_up', 'Flip down → up'],
      ['from_up_to_down', 'Flip up → down'],
    ];
    return `
      <div class="form-row">
        <label class="param-label-toggle" data-hint="ATR period used to calculate volatility (default 10)">ATR Period</label>
        <input type="number" min="2" value="${ap}" data-nb-ind="${i}" data-nb-field="atr_period">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="ATR multiplier for band distance (default 3.0)">Multiplier</label>
        <input type="number" min="0.1" step="0.1" value="${mult}" data-nb-ind="${i}" data-nb-field="multiplier">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Bullish: price above supertrend line. Bearish: price below.">Condition</label>
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
      ['bullish', 'Bullish (price above SAR)'],
      ['bearish', 'Bearish (price below SAR)'],
      ['bullish_flip', 'Bullish flip (trend reversal up)'],
      ['bearish_flip', 'Bearish flip (trend reversal down)'],
    ];
    return `
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Starting acceleration factor — how fast SAR moves (default 0.02)">Initial AF</label>
        <input type="number" min="0.001" step="0.01" value="${iaf}" data-nb-ind="${i}" data-nb-field="initial_af">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Maximum acceleration factor cap (default 0.20)">Max AF</label>
        <input type="number" min="0.01" step="0.01" value="${maf}" data-nb-ind="${i}" data-nb-field="max_af">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Bullish: price above SAR dots. Bearish: below. Flip: trend just reversed direction.">Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${PSAR_CONDS.map(([v, l]) =>
            `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
      </div>`;
  }
  if (ind.type === 'BOLLINGER') {
    const mult = ind.multiplier != null ? ind.multiplier : 2.0;
    const maT = ind.ma_type || 'SMA';
    const val = ind.value || 'lower';
    const sqThr = ind.squeeze_threshold != null ? ind.squeeze_threshold : 0.02;
    const BB_CONDS = [
      ['price_crossing_up', 'Price crossing up'],
      ['price_crossing_down', 'Price crossing down'],
      ['price_greater_than', 'Price greater than band'],
      ['price_lower_than', 'Price lower than band'],
      ['price_below_lower', 'Price below lower band'],
      ['price_above_upper', 'Price above upper band'],
      ['squeeze', 'Squeeze'],
      ['percent_b_below_0', '%B below 0 (under lower)'],
      ['percent_b_above_1', '%B above 1 (over upper)'],
      ['percent_b_below_20', '%B below 0.2 (near lower)'],
      ['percent_b_above_80', '%B above 0.8 (near upper)'],
    ];
    return `
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Number of candles for the moving average base (default 20)">Period</label>
        <input type="number" min="5" value="${ind.period || 20}" data-nb-ind="${i}" data-nb-field="period">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Standard deviation multiplier for band width (default 2.0)">Multiplier</label>
        <input type="number" min="0.1" step="0.1" value="${mult}" data-nb-ind="${i}" data-nb-field="multiplier">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Moving average type for the middle band. SMA is standard. EMA reacts faster to recent prices.">MA type</label>
        <select data-nb-ind="${i}" data-nb-field="ma_type">
          ${['SMA','EMA','WMA'].map(t => `<option value="${t}" ${maT === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Which band to apply the condition to. Upper = overbought zone, Lower = oversold zone, Middle = trend filter.">Band</label>
        <select data-nb-ind="${i}" data-nb-field="value">
          ${['lower','upper','middle'].map(v => `<option value="${v}" ${val === v ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Squeeze threshold: bands narrower than this % of middle band indicate low volatility before a breakout.">Squeeze threshold</label>
        <input type="number" min="0.001" step="0.005" value="${sqThr}" data-nb-ind="${i}" data-nb-field="squeeze_threshold">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Trigger when price crosses or sits relative to a band. %B conditions measure where price sits within the bands (0=lower, 1=upper).">Condition</label>
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
    const omt = ind.oscillator_ma_type || 'EMA';
    const smt = ind.signal_ma_type || 'EMA';
    const MACD_CONDS = [
      ['histogram_positive', 'Histogram positive'],
      ['histogram_negative', 'Histogram negative'],
      ['macd_above_signal', 'MACD above signal'],
      ['macd_below_signal', 'MACD below signal'],
      ['macd_cross_above_zero', 'MACD cross above zero'],
      ['macd_cross_below_zero', 'MACD cross below zero'],
    ];
    return `
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Histogram positive = bullish momentum, negative = bearish. Zero cross = stronger trend signal.">Condition</label>
        <select data-nb-ind="${i}" data-nb-field="condition">
          ${MACD_CONDS.map(([v, l]) => `<option value="${v}" ${ind.condition === v ? 'selected' : ''}>${l}</option>`).join('')}
        </select>
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Fast EMA period (default 12)">Fast</label>
        <input type="number" min="2" value="${mf}" data-nb-ind="${i}" data-nb-field="macd_fast">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Slow EMA period (default 26)">Slow</label>
        <input type="number" min="2" value="${ms}" data-nb-ind="${i}" data-nb-field="macd_slow">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="Signal line EMA period (default 9)">Signal</label>
        <input type="number" min="2" value="${mg}" data-nb-ind="${i}" data-nb-field="macd_signal">
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="MA type for the MACD line itself. EMA is standard.">Oscillator MA</label>
        <select data-nb-ind="${i}" data-nb-field="oscillator_ma_type">
          ${['EMA','SMA'].map(t => `<option value="${t}" ${omt === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
      </div>
      <div class="form-row">
        <label class="param-label-toggle" data-hint="MA type for the signal line. EMA is standard. Changing this affects crossover sensitivity.">Signal MA</label>
        <select data-nb-ind="${i}" data-nb-field="signal_ma_type">
          ${['EMA','SMA'].map(t => `<option value="${t}" ${smt === t ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
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

function nbUpdateToggleStates() {
  const pairs = [
    { check: 'nb-tp-enabled',    body: 'nb-tp-body',    label: 'nb-tp-toggle-label',    state: 'tp_enabled' },
    { check: 'nb-sl-enabled',    body: 'nb-sl-body',    label: 'nb-sl-toggle-label',    state: 'sl_enabled' },
    { check: 'nb-dca-enabled',   body: 'nb-dca-body',   label: 'nb-dca-toggle-label',   state: 'dca_enabled' },
    { check: 'nb-sched-enabled', body: 'nb-sched-body', label: 'nb-sched-toggle-label', state: 'sched_enabled' },
  ];
  for (const p of pairs) {
    const chk = $(p.check);
    if (!chk) continue;
    const on = chk.checked;
    nbState[p.state] = on;
    const sect = chk.closest('.wizard-section');
    if (sect) sect.classList.toggle('sect-disabled', !on);
    const lbl = $(p.label);
    if (lbl) lbl.textContent = on ? 'enabled' : 'disabled';
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

  const allGroupInds = (nbState.indicatorGroups || []).flatMap(g => g.indicators || []);
  const indSummary = allGroupInds.length
    ? (nbState.indicatorGroups || []).map(g =>
        (g.indicators || []).map(i => i.type).join('+') || 'empty'
      ).join(' OR ')
    : 'none — always enter';
  const unit = nbState.base_unit === 'btc' ? 'BTC' : '%';

  $('nb-review').innerHTML = `
    ${warnings}
    <div id="nb-review-profile" class="review-summary" aria-live="polite">
      <div class="review-summary-placeholder muted">Analysing configuration…</div>
    </div>
    <div id="nb-review-warnings" class="review-warnings hidden" aria-live="polite">
      <h4>Configuration notes</h4>
      <ul></ul>
    </div>
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
      <div class="review-row"><span class="review-key">Take Profit</span><span>${nbState.tp_enabled ? nbState.tp_target_pct + '%' : 'disabled'}${nbState.tp_price_enabled ? '' : ' (indicator only)'}</span></div>
      <div class="review-row"><span class="review-key">TP indicators</span><span>${(nbState.tpIndicatorGroups || []).flatMap(g => g.indicators || []).length ? (nbState.tpIndicatorGroups || []).map(g => (g.indicators || []).map(i => i.type).join('+')).join(' OR ') : 'none'}</span></div>
      <div class="review-row"><span class="review-key">Max age</span><span>${nbState.tp_max_age_enabled ? nbState.tp_max_age_hours + 'h' : 'none'}</span></div>
      <div class="review-row"><span class="review-key">Stop Loss</span><span>${nbState.sl_enabled ? safeText(nbState.sl_type) + ' ' + nbState.sl_pct + '%' : 'disabled'}</span></div>
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
  nbScheduleValidation();
}


// ── Backend-backed config validation (advisory) ───────────────────────────────
// Debounced so rapid input changes in the wizard don't hammer the endpoint.
// The backend is the single source of truth for warning thresholds; mirroring
// the math in JS would guarantee drift between the two.

let _nbValidateTimer = null;
let _nbValidateSeq = 0;

function nbScheduleValidation() {
  if (_nbValidateTimer) clearTimeout(_nbValidateTimer);
  _nbValidateTimer = setTimeout(nbRunValidation, 300);
}

async function nbRunValidation() {
  _nbValidateTimer = null;
  const seq = ++_nbValidateSeq;

  let payload;
  try {
    payload = nbBuildBotConfig();
  } catch (e) {
    _nbRenderValidationError('Invalid form state — fix the inputs above.');
    return;
  }

  try {
    const res = await fetch('/api/bots/validate-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(payload),
    });
    // Drop stale responses — the user may have typed again since this
    // request left. Without the seq guard the last-rendered warnings
    // aren't guaranteed to match the last submitted payload.
    if (seq !== _nbValidateSeq) return;
    if (res.status === 401) { _handle401(); return; }
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (e) {}
      _nbRenderValidationError(detail || `HTTP ${res.status}`);
      return;
    }
    const data = await res.json();
    _nbRenderValidation(data);
  } catch (e) {
    if (seq !== _nbValidateSeq) return;
    _nbRenderValidationError(e.message || 'Network error');
  }
}

function _nbRenderValidation(data) {
  const s = data.summary || {};
  const profileEl = $('nb-review-profile');
  if (profileEl) {
    const bos = Number(s.base_order_size) || 0;
    const worst = Number(s.worst_case_dca) || 0;
    const worstMult = Number(s.worst_case_multiple) || 0;
    const cum = Number(s.cumulative_position) || 0;
    const cumMult = Number(s.cumulative_multiple) || 0;
    const modeClass = s.mode === 'live' ? 'mode-live' : 'mode-paper';
    profileEl.innerHTML = `
      <div class="summary-row">
        <span class="label">Mode</span>
        <span class="value ${modeClass}">${safeText((s.mode || '—').toUpperCase())}</span>
      </div>
      <div class="summary-row">
        <span class="label">Base order size</span>
        <span class="value">${bos.toFixed(8)} BTC</span>
      </div>
      <div class="summary-row">
        <span class="label">Worst-case DCA order</span>
        <span class="value">${worst.toFixed(8)} BTC
          <span class="multiple">(${worstMult.toFixed(1)}× base)</span>
        </span>
      </div>
      <div class="summary-row">
        <span class="label">Cumulative position</span>
        <span class="value">${cum.toFixed(8)} BTC
          <span class="multiple">(${cumMult.toFixed(1)}× base)</span>
        </span>
      </div>
    `;
  }

  const warnEl = $('nb-review-warnings');
  if (!warnEl) return;
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  if (warnings.length === 0) {
    warnEl.classList.add('hidden');
    return;
  }
  warnEl.classList.remove('hidden');
  const ul = warnEl.querySelector('ul');
  if (ul) {
    ul.innerHTML = warnings.map(w => {
      const icon = w.level === 'high' ? '⚠️' : 'ℹ️';
      const cls = w.level === 'high' ? 'warning-high' : 'warning-medium';
      return `<li class="${cls}"><strong>${icon}</strong> ${safeText(w.message || '')}</li>`;
    }).join('');
  }
}

function _nbRenderValidationError(msg) {
  const profileEl = $('nb-review-profile');
  if (profileEl) {
    profileEl.innerHTML =
      `<div class="review-summary-placeholder muted">Config analysis unavailable: ${safeText(msg)}</div>`;
  }
  const warnEl = $('nb-review-warnings');
  if (warnEl) warnEl.classList.add('hidden');
}

function _nbSerializeIndicator(i) {
  const out = { type: i.type };
  if (i.timeframe && i.timeframe !== '1h') out.timeframe = i.timeframe;
  if (i.type === 'RSI') {
    out.period = i.period; out.threshold = i.threshold;
    if (i.price_source && i.price_source !== 'close') out.price_source = i.price_source;
  } else if (i.type === 'MACD') {
    out.condition = i.condition;
    if (i.macd_fast != null) out.macd_fast = i.macd_fast;
    if (i.macd_slow != null) out.macd_slow = i.macd_slow;
    if (i.macd_signal != null) out.macd_signal = i.macd_signal;
    if (i.oscillator_ma_type) out.oscillator_ma_type = i.oscillator_ma_type;
    if (i.signal_ma_type) out.signal_ma_type = i.signal_ma_type;
  } else if (i.type === 'BOLLINGER') {
    out.period = i.period || 20; out.multiplier = i.multiplier != null ? i.multiplier : 2.0;
    out.condition = i.condition || 'price_below_lower';
    if (i.ma_type) out.ma_type = i.ma_type; if (i.value) out.value = i.value;
    if (i.squeeze_threshold != null) out.squeeze_threshold = i.squeeze_threshold;
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
    if (i.value) out.value = i.value;
  } else if (i.type === 'SUPPORT_RESISTANCE') {
    out.left_bars = i.left_bars != null ? i.left_bars : 15;
    out.right_bars = i.right_bars != null ? i.right_bars : 15;
    out.proximity_pct = i.proximity_pct != null ? i.proximity_pct : 1.0;
    out.condition = i.condition || 'price_crossing_down';
    if (i.value) out.value = i.value;
    if (i.volume_threshold) out.volume_threshold = i.volume_threshold;
    if (i.min_touches != null && i.min_touches > 1) out.min_touches = i.min_touches;
  } else if (i.type === 'QFL') {
    out.condition = i.condition || 'below_base';
    out.base_periods = i.base_periods != null ? i.base_periods : 36;
    out.pump_periods = i.pump_periods != null ? i.pump_periods : 8;
    out.pump_from_base_pct = i.pump_from_base_pct != null ? i.pump_from_base_pct : 3.0;
    out.base_crack_pct = i.base_crack_pct != null ? i.base_crack_pct : 3.0;
  }
  return out;
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
      enabled: nbState.dca_enabled,
      base_order_size: nbState.base_size,
      max_orders: nbState.dca_enabled ? nbState.dca_max_orders + 1 : 1,
      order_spacing_pct: nbState.dca_spacing_pct,
      multiplier: nbState.dca_volume_scale,
      step_scale: nbState.dca_step_scale,
    },
    entry: {
      indicators: [],
      indicator_groups: (nbState.indicatorGroups || []).map(g => ({
        id: g.id,
        name: g.name || '',
        indicators: (g.indicators || []).map(_nbSerializeIndicator),
      })),
    },
    take_profit: {
      enabled: nbState.tp_enabled,
      target_pct: nbState.tp_target_pct,
      price_enabled: nbState.tp_price_enabled,
      indicator_groups: (nbState.tpIndicatorGroups || []).map(g => ({
        id: g.id, name: g.name || '',
        indicators: (g.indicators || []).map(_nbSerializeIndicator),
      })),
    },
    stop_loss: { type: nbState.sl_enabled ? nbState.sl_type : 'none', pct: nbState.sl_pct },
    use_wick_simulation: Boolean(nbState.use_wick_simulation),
  };
  if (nbState.tp_min_pct != null && nbState.tp_min_pct > 0) {
    cfg.take_profit.minimum_tp_pct = nbState.tp_min_pct;
  }
  cfg.schedule = {
    enabled: nbState.sched_enabled,
    timezone: nbState.schedule_timezone || 'Europe/Amsterdam',
    trading_windows: nbState.sched_enabled ? (nbState.schedule_windows || []).map(w => ({
      days: (w.days || []).slice(),
      from: w.from || '00:00',
      to:   w.to   || '00:00',
    })) : [],
    blackout_dates: nbState.sched_enabled ? (nbState.schedule_blackouts || []).slice() : [],
  };
  return { bot: cfg };
}

async function nbSubmit() {
  nbReadAll();
  const errors = nbValidateAll();
  if (errors.length) {
    nbShowError(errors);
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
      nbShowError(r.detail || `Save failed (${res.status})`);
      return;
    }
    const returnSlug = wasEdit ? nbEditSlug : (r.slug || null);
    nbInit();
    if (returnSlug) {
      openBot(returnSlug);
    } else {
      goBots();
    }
  } catch (e) {
    nbShowError('Network error: ' + (e.message || e));
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
  // Clear any pending deal from a previous bot view — auto-select
  // will kick in again in loadChartTab if the new bot has open deals.
  _chartPendingDeal = null;

  // Detail is a sub-view of Bots — keep the Bots tab active and
  // surface the bot slug + meta in the detail-context-bar (above the
  // sub-nav). Slug is shown immediately; meta is filled in by
  // fetchDetail() once the API response arrives.
  _setActiveTab('nav-bots-btn');
  $('hdr-pill').classList.remove('hidden');
  const _name = $('bot-name-display');
  if (_name) _name.textContent = slug;
  const _meta = $('bot-meta-display');
  if (_meta) _meta.textContent = '';

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
  if (!fromPop) _pushHistory('bot', `#bot/${slug}/dashboard`, { slug, dtab: 'dashboard' });
}

function _routeFromHash() {
  const h = (window.location.hash || '').replace(/^#/, '');
  if (h.startsWith('bot/')) {
    const parts = h.slice(4).split('/');
    const slug = parts[0];
    const dtab = parts[1] || 'dashboard';
    if (slug) {
      openBot(slug, true);
      setTimeout(() => {
        const tabBtn = document.querySelector(`.detail-subnav .tab[data-dtab="${dtab}"]`);
        if (tabBtn) showDTab(dtab, tabBtn, true);
      }, 60);
      return;
    }
  }
  if (h.startsWith('admin/')) {
    const sub = h.slice('admin/'.length);
    goAdmin(true, sub || null);
    return;
  }
  switch (h) {
    case 'bots':      goBots(true); break;
    case 'deals':     goDeals(true); break;
    case 'workspace': goWorkspace(true); break;
    case 'backtests': goBacktests(true); break;
    case 'changelog': goChangelog(true); break;
    case 'admin':     goAdmin(true); break;
    case 'overview':  goOverview(true); break;
    default:          goOverview(true); break;
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

    updateBotDetailDrawdown(b);

    if (b.current_price) $('hdr-price').textContent = fmtPrice(b.current_price);
    $('hdr-pair').textContent = b.pair || 'BTC/USD';
    $('hdr-uptime').textContent = b.uptime ? '⏱ ' + b.uptime : '';

    // Detail-context-bar: prefer bot_name (operator-set) over slug
    // for the prominent identity, and compose a compact meta string
    // from whatever fields the state response surfaces. Each part is
    // optional — missing fields are skipped so we never render a
    // dangling separator like "Paper · ".
    const _name = $('bot-name-display');
    if (_name) _name.textContent = b.bot_name || slug;
    const _meta = $('bot-meta-display');
    if (_meta) {
      const metaParts = [];
      if (b.mode)     metaParts.push(b.mode.charAt(0).toUpperCase() + b.mode.slice(1));
      if (b.pair)     metaParts.push(b.pair);
      if (b.exchange && b.exchange !== '—') {
        metaParts.push(b.exchange.charAt(0).toUpperCase() + b.exchange.slice(1));
      }
      _meta.textContent = metaParts.join(' · ');
    }

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
    ['Profit Factor', 'profit_factor', 'Ratio of gross profit to gross loss. Above 1.5 is good, above 2.0 is excellent.'],
    ['Sharpe Ratio',  'sharpe',        'Risk-adjusted return. Measures excess return per unit of volatility. Above 1.0 is acceptable, above 2.0 is good.'],
    ['Sortino Ratio', 'sortino',       'Like Sharpe but only penalizes downside volatility. More relevant for trading strategies.'],
    ['Consistency',   'consistency',    'Percentage of trades closed in profit. High win rate alone is not enough — check Profit Factor too.'],
    ['Max Drawdown',  'max_dd',        'Largest peak-to-trough decline in portfolio value. Lower is better — indicates worst-case loss scenario.'],
    ['Total Deals',   'total',         'Total number of completed deals in the selected period.'],
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
  grid.innerHTML = cells.map(([label, key, hint]) => `
    <div class="card">
      <div class="card-label param-label-toggle" ${hint ? `data-hint="${safeText(hint)}"` : ''}>${safeText(label)}</div>
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
  const sched = b.schedule || null;
  const { groups: entryGroups, flat: entryFlat } = _getAllIndicators(b);

  const leverageStr = lev.enabled ? `${lev.size || 1}x` : 'off';
  const _cfgRenderInd = (i) => {
    const parts = [safeText(i.type || '?')];
    if (i.timeframe) parts.push(safeText(i.timeframe));
    if (i.condition) parts.push(safeText(i.condition));
    else if (i.threshold) parts.push(safeText(i.threshold));
    if (i.value) parts.push(safeText(i.value));
    return parts.join(' — ');
  };
  const _cfgRenderGroups = (groups) => groups.map((g, gi) => {
    const inds = (g.indicators || []).map(_cfgRenderInd).join(', ');
    return `<div class="config-group"><span class="config-group-name">${safeText(g.name || 'Group ' + (gi + 1))}</span>: ${inds || 'empty'}</div>`
      + (gi < groups.length - 1 ? '<div class="config-or">OR</div>' : '');
  }).join('');
  const indHtml = entryGroups.length
    ? _cfgRenderGroups(entryGroups)
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
      <div class="cfg-row"><span class="cfg-key">Price TP</span><span>${tp.price_enabled !== false ? 'Enabled' : 'Disabled'}</span></div>
      ${(tp.indicator_groups || []).length ? '<div class="cfg-subtitle">TP Indicator Groups</div>' + _cfgRenderGroups(tp.indicator_groups) : ''}
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
    // Surface the Cancel button — users who open an existing bot for
    // editing need a way back without persisting wip changes. The
    // button is kept hidden in the new-bot flow because there's no
    // "previous state" to return to.
    const cancelBtn = $('nb-cancel-btn');
    if (cancelBtn) cancelBtn.classList.remove('hidden');
  } catch (e) {
    alert('Network error: ' + e.message);
  }
}

function _nbDeserializeInd(i) {
  return {
    type: i.type || 'RSI', timeframe: i.timeframe || '1h',
    period: i.period != null ? i.period : 14, threshold: i.threshold || 'below_35',
    rsi_condition: undefined, rsi_value: undefined,
    fast: i.fast != null ? i.fast : 9, slow: i.slow != null ? i.slow : 21,
    signal: i.signal || 'bullish_cross', condition: i.condition || 'histogram_positive',
    macd_fast: i.macd_fast, macd_slow: i.macd_slow, macd_signal: i.macd_signal,
    oscillator_ma_type: i.oscillator_ma_type, signal_ma_type: i.signal_ma_type,
    multiplier: i.multiplier, ma_type: i.ma_type, value: i.value,
    initial_af: i.initial_af, max_af: i.max_af, atr_period: i.atr_period,
    lookback: i.lookback, price_source: i.price_source,
    left_bars: i.left_bars, right_bars: i.right_bars,
    proximity_pct: i.proximity_pct, volume_threshold: i.volume_threshold,
    min_touches: i.min_touches, squeeze_threshold: i.squeeze_threshold,
    base_periods: i.base_periods, pump_periods: i.pump_periods,
    pump_from_base_pct: i.pump_from_base_pct, base_crack_pct: i.base_crack_pct,
    ...i,
  };
}
function _nbDeserializeGroups(entry) {
  const e = entry || {};
  const groups = e.indicator_groups || [];
  if (groups.length) {
    return groups.map(g => ({
      id: g.id || 1, name: g.name || '',
      indicators: (g.indicators || []).map(_nbDeserializeInd),
    }));
  }
  const flat = e.indicators || [];
  if (flat.length) {
    return [{ id: 1, name: 'Group 1', indicators: flat.map(_nbDeserializeInd) }];
  }
  return [{ id: 1, name: 'Group 1', indicators: [] }];
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
    indicators:       [],
    indicatorGroups:  _nbDeserializeGroups(b.entry),
    tp_enabled:           tp.enabled !== false,
    tp_price_enabled:     tp.price_enabled !== false,
    tp_target_pct:        tp.target_pct != null ? tp.target_pct : d.tp_target_pct,
    tp_min_pct:           tp.minimum_tp_pct != null ? tp.minimum_tp_pct : null,
    tpIndicatorGroups:    _nbDeserializeGroups({ indicator_groups: tp.indicator_groups }),
    sl_enabled:           sl.type !== 'none',
    sl_type:              (sl.type && sl.type !== 'none') ? sl.type : d.sl_type,
    sl_pct:               sl.pct != null ? sl.pct : d.sl_pct,
    use_wick_simulation:  b.use_wick_simulation != null ? Boolean(b.use_wick_simulation) : d.use_wick_simulation,
    dca_enabled:          dca.enabled !== false,
    dca_max_orders:       dca.max_orders != null ? Math.max(0, dca.max_orders - 1) : d.dca_max_orders,
    dca_size:             dca.base_order_size != null ? dca.base_order_size : d.dca_size,
    dca_spacing_pct:      dca.order_spacing_pct != null ? dca.order_spacing_pct : d.dca_spacing_pct,
    dca_volume_scale:     dca.multiplier != null ? dca.multiplier : d.dca_volume_scale,
    sched_enabled:        (b.schedule && b.schedule.enabled) || false,
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

// ── Log-level filter (UI-only, client-side tekstpatroon) ──────────────────────
// _logLevelFilter is een globale preference: geldt voor ALLE bots, blijft
// bewaard bij tab-switch en bij CLEAR. Twee standen:
//   'all'         — toon elke regel (default)
//   'warn-error'  — alleen regels met [WARNING]/[ERROR]/[CRITICAL] prefix
// Pure tekstmatching op de levelname die logging.basicConfig meestuurt;
// geen wijziging aan de WS-payload of backend.
let _logLevelFilter = 'all';

function _lineMatchesFilter(text) {
  if (_logLevelFilter === 'all') return true;
  if (_logLevelFilter === 'warn-error') {
    return (
      text.includes('[WARNING]') ||
      text.includes('[ERROR]') ||
      text.includes('[CRITICAL]')
    );
  }
  return true;
}

function _applyLogLevelFilter() {
  const body = $('log-body');
  if (!body) return;
  for (const line of body.children) {
    const text = line.textContent || '';
    line.style.display = _lineMatchesFilter(text) ? '' : 'none';
  }
}

function appendLog(text) {
  if (text === '__ping__') return;
  const out = $('log-body');
  const el = document.createElement('div');
  el.className = 'log-line ' + logCls(text);
  el.textContent = text;
  out.appendChild(el);
  // Apply filter to the freshly-added line so streaming WARNING+ERROR
  // stays strict even while new INFO lines arrive in the background.
  if (!_lineMatchesFilter(text)) el.style.display = 'none';
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

// ── Live candlestick chart ───────────────────────────────────────────────────
// Lightweight Charts v4 wrapper. The chart tab in the bot detail view shows
// live candles, indicator overlays derived from the bot's configured
// indicators, and (when running) deal entry/TP/SL/DCA price lines. The
// wizard preview is a simpler standalone candlestick chart. Both gracefully
// degrade if window.LightweightCharts is undefined (CDN blocked).

let _chartMain = null;
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
let _srLineSeries = [];
let _psarLineSeries = [];
let _qflLineSeries = [];
// Indicator-driven markers (parabolic SAR + market structure) live here
// so _setCombinedMarkers() can merge them with deal markers in a single
// setMarkers() call — Lightweight Charts replaces the full array on each
// invocation, so any overlay that forgets to merge wipes the other.
let _chartIndicatorMarkers = [];
let _candleMarkersPrimitive = null;
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
let _wizardSrLineSeries = [];
let _wizardPsarSeries = [];
let _wizardQflSeries = [];
// Sub-charts for RSI / MACD indicators in the wizard preview. Created
// lazily when the user adds the corresponding indicator and destroyed
// when they remove it, so a wizard with no RSI/MACD costs nothing.
let _wizardRsiThresholdLine = null;
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

// Per-chart timezone state. Each chart-type keeps its own
// localStorage key + module-level variable + formatter. The central
// ``window.RevertoChart.buildTimezoneFormatter`` helper produces the
// ``{short, full}`` pair used by LWC's ``timeScale.tickMarkFormatter``
// (axis labels) and ``localization.timeFormatter`` (crosshair tooltip).
//
// Legacy fallback: a pre-migration operator may still have the old
// ``reverto_timezone`` key hanging around. Check that once on boot
// so their previous choice persists into the new per-chart scheme,
// then write the new key so the next boot reads from there.
const _MAIN_CHART_TZ_LS_KEY = 'reverto.main_chart_timezone';

// Audit r1.1-004: pipe every LS-read through the chart module's
// normaliser so a corrupted storage value (hand-edit, stale entry
// from a prior build) collapses to 'local' before it ever reaches
// module state or the dropdown UI. Helper is a no-op at runtime
// when chart_module.js exports a normaliser (the common path);
// when the module fails to load (CDN blocked, network error) the
// identity fallback at least lets the rest of the app boot.
function _normalizeTzFromLS(raw) {
  const fn = window.RevertoChart && window.RevertoChart.normalizeChartTimezone;
  return fn ? fn(raw) : raw;
}

function _loadMainChartTz() {
  const v = localStorage.getItem(_MAIN_CHART_TZ_LS_KEY);
  if (v) return _normalizeTzFromLS(v);
  const legacy = localStorage.getItem('reverto_timezone');
  if (legacy) {
    // Migrate once + drop the old key. Keeps legacy behaviour
    // (pre-upgrade user had 'UTC' as default) but re-homes storage
    // under the per-chart namespace the rest of the PR uses. Run
    // the legacy value through the normaliser too — if an old
    // build wrote a value that's no longer in the allowlist
    // (rare but possible across IANA catalogue rewrites) we
    // quietly drop it rather than carrying the garbage forward.
    const normalized = _normalizeTzFromLS(legacy);
    localStorage.setItem(_MAIN_CHART_TZ_LS_KEY, normalized);
    localStorage.removeItem('reverto_timezone');
    return normalized;
  }
  return 'local';
}
function _saveMainChartTz(tz) {
  try { localStorage.setItem(_MAIN_CHART_TZ_LS_KEY, tz); } catch (e) {}
}

let _chartTimezone = _loadMainChartTz();

function _buildChartTzFormatter(tz) {
  // Thin wrapper around window.RevertoChart.buildTimezoneFormatter so
  // callers have a stable global name + a safe fallback if the chart
  // module failed to load (e.g. CDN blocked). The fallback formatter
  // mirrors the pre-helper shape so the chart axis never renders a
  // literal "undefined".
  if (window.RevertoChart && typeof window.RevertoChart.buildTimezoneFormatter === 'function') {
    return window.RevertoChart.buildTimezoneFormatter(tz);
  }
  return {
    short: (s) => new Date(s * 1000).toISOString().slice(11, 16),
    full: (s) => new Date(s * 1000).toISOString().slice(0, 16).replace('T', ' '),
  };
}

function _tzFormatter(ts) {
  // Back-compat shim: the default ``_chartLayoutOpts`` passes this
  // as ``localization.timeFormatter``, so the backtest-equity chart
  // + any other site that uses ``_chartLayoutOpts()`` without its
  // own dropdown falls back to the main-chart timezone. Charts
  // with their own dropdown override via applyOptions.
  return _buildChartTzFormatter(_chartTimezone).full(ts);
}

// Wizard + backtest-candle chart timezone state. Same per-chart
// pattern as the main-chart — own localStorage key, own module-
// level variable, own dropdown. No legacy migration because
// neither was wired to a timezone before this PR.
const _WIZARD_CHART_TZ_LS_KEY   = 'reverto.wizard_chart_timezone';
const _BT_CANDLE_TZ_LS_KEY      = 'reverto.backtest_candle_timezone';

// Audit r1.1-004: same normalisation for the wizard + backtest
// sites as the main-chart path above. Corrupt / unknown LS values
// collapse to 'local' before they land in module-scope state.
let _wizardChartTimezone   = _normalizeTzFromLS(localStorage.getItem(_WIZARD_CHART_TZ_LS_KEY) || 'local');
let _btCandleChartTimezone = _normalizeTzFromLS(localStorage.getItem(_BT_CANDLE_TZ_LS_KEY)   || 'local');

function _populateTzDropdown(sel, current) {
  // Shared population helper. Called by the main-chart + wizard +
  // backtest setup paths with their own select elements so the
  // 20-entry IANA list only lives in chart_module.js.
  const tzList = (window.RevertoChart && window.RevertoChart.CHART_TIMEZONES) || [];
  if (!tzList.length) return;
  sel.innerHTML = '';
  for (const tz of tzList) {
    const o = document.createElement('option');
    o.value = tz.value;
    o.textContent = tz.label;
    sel.appendChild(o);
  }
  sel.value = current;
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
    localization: { timeFormatter: _tzFormatter },
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
  for (const chart of [_chartMain, _wizardChart]) {
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
  // Workspace chart-panels keep their own live set of LWC instances
  // inside chart_module.js. The helper below iterates every active
  // panel and re-applies the same layout + candle-series palette
  // that the loops above applied to the main/wizard charts.
  if (window.RevertoChart
      && typeof window.RevertoChart.applyThemeToAll === 'function') {
    try { window.RevertoChart.applyThemeToAll(); } catch (e) {}
  }
}

function _chartLibAvailable() { return typeof window.LightweightCharts !== 'undefined'; }
const _LWC = () => window.LightweightCharts || {};
const _lwcCreateChart = (el, opts) => _LWC().createChart(el, opts);

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
  // Auto-select the most recent open deal if no explicit pending
  // deal was set (e.g. via a click on a deal row in the Deals tab).
  // This makes "open bot → chart tab" show the active position
  // with timeline markers by default, instead of a bare candle
  // chart that requires an extra click through Deals to see.
  if (!_chartPendingDeal) {
    const autoDeal = _mostRecentOpenDealForSlug(slug);
    if (autoDeal) {
      _chartPendingDeal = autoDeal;
    }
  }
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
  // Abort any in-flight history fetch before the new tab's setup
  // runs — otherwise a late response would try to prepend onto
  // a chart-instance that no longer exists, and _chartCandles is
  // null by then (a setData() call would throw).
  if (_chartHistoryAbort) {
    try { _chartHistoryAbort.abort(); } catch (e) {}
    _chartHistoryAbort = null;
  }
  _chartCandlesArr = [];
  _chartLoadingMore = false;
  _chartNoMoreData = false;
  _chartMainToggleOverlay('chart-main-loading', false);
  _chartMainToggleOverlay('chart-main-no-more', false);
  try { if (_chartMain) _chartMain.remove(); } catch (e) {}
  _chartMain = null;
  _chartCandles = null;
  _chartSeries = {};
  // The candle series owned these price-line + marker handles; dropping
  // the refs is enough — they die with the chart instance. _chartPendingDeal
  // deliberately survives so showDealOnChart → showDTab → loadChartTab →
  // teardown → init can still re-apply the deal overlay after init.
  _chartDealPriceLines = [];
  _srLineSeries = [];
  _psarLineSeries = [];
  _qflLineSeries = [];
  _chartDealMarkers = [];
  _chartIndicatorMarkers = [];
  _candleMarkersPrimitive = null;
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
}

function _indicatorsConfigured() {
  const inner = (_chartBotConfig && _chartBotConfig.bot) || {};
  const entry = inner.entry || {};
  const groups = entry.indicator_groups || [];
  const flat = groups.flatMap(g => g.indicators || []);
  if (flat.length) return flat;
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
  // PR timezone-per-chart: override the shared formatter bundle
  // with the main-chart's own so the dropdown at #chart-tz drives
  // both axis ticks and crosshair tooltip.
  const _mainFmt = _buildChartTzFormatter(_chartTimezone);
  _chartMain = _lwcCreateChart(mainEl, {
    ...opts,
    localization: { timeFormatter: _mainFmt.full },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: _mainFmt.short,
    },
    width:  mainEl.clientWidth,
    height: mainEl.clientHeight || 500,
  });
  _chartCandles = _chartMain.addSeries(_LWC().CandlestickSeries, {
    upColor:        _cssVar('--accent', '#26a69a'),
    downColor:      _cssVar('--red',    '#ef5350'),
    borderUpColor:  _cssVar('--accent', '#26a69a'),
    borderDownColor:_cssVar('--red',    '#ef5350'),
    wickUpColor:    _cssVar('--accent', '#26a69a'),
    wickDownColor:  _cssVar('--red',    '#ef5350'),
  });

  if (_hasIndicator('BOLLINGER')) {
    _chartSeries.bbUpper  = _chartMain.addSeries(_LWC().LineSeries, { color: _cssVar('--blue', '#5b8dee'), lineWidth: 1 });
    _chartSeries.bbMiddle = _chartMain.addSeries(_LWC().LineSeries, { color: _cssVar('--muted', '#888'),   lineWidth: 1 });
    _chartSeries.bbLower  = _chartMain.addSeries(_LWC().LineSeries, { color: _cssVar('--blue', '#5b8dee'), lineWidth: 1 });
  }
  if (_hasIndicator('SUPERTREND')) {
    _chartSeries.stBull = _chartMain.addSeries(_LWC().LineSeries, { color: _cssVar('--accent', '#26a69a'), lineWidth: 2 });
    _chartSeries.stBear = _chartMain.addSeries(_LWC().LineSeries, { color: _cssVar('--red',    '#ef5350'), lineWidth: 2 });
  }

  // RSI pane (pane 1 on main chart)
  if (_hasIndicator('RSI')) {
    _chartSeries.rsi = _chartMain.addSeries(_LWC().LineSeries, {
      color: _cssVar('--blue', '#5b8dee'), lineWidth: 1,
      priceLineVisible: false, lastValueVisible: true,
    }, 1);
    const rsiCfgVal = _findIndicator('RSI');
    const rsiParsed = _parseRsiThreshold(rsiCfgVal?.threshold);
    const rsiTv = rsiCfgVal?.rsi_value != null ? rsiCfgVal.rsi_value : rsiParsed.value;
    if (rsiTv) {
      _chartSeries.rsi.createPriceLine({ price: rsiTv, color: _cssVar('--accent', '#26a69a'), lineStyle: 0, lineWidth: 1, axisLabelVisible: true, title: String(rsiTv) });
    }
  }
  // MACD pane (pane 2 on main chart, or 1 if no RSI)
  if (_hasIndicator('MACD')) {
    const macdPane = _hasIndicator('RSI') ? 2 : 1;
    _chartSeries.macdHist   = _chartMain.addSeries(_LWC().HistogramSeries, { color: _cssVar('--muted', '#888') }, macdPane);
    _chartSeries.macdLine   = _chartMain.addSeries(_LWC().LineSeries, { color: _cssVar('--blue',  '#5b8dee'), lineWidth: 1 }, macdPane);
    _chartSeries.macdSignal = _chartMain.addSeries(_LWC().LineSeries, { color: _cssVar('--amber', '#ffb347'), lineWidth: 1 }, macdPane);
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
      }
    });
    _chartResizeObs.observe(mainEl);
  }

  // Redraw the SVG annotation overlay whenever the user pans or
  // zooms the chart — without this, existing annotations would
  // stick to stale pixel positions until the next full fetch.
  // Same subscribe-call also triggers the scroll-to-load-history
  // path when the visible range's left edge nears the data's
  // oldest candle.
  try {
    _chartMain.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      _renderAnnotations();
      if (!range || _chartLoadingMore || _chartNoMoreData) return;
      const n = _chartCandlesArr.length;
      if (!n) return;
      const threshold = Math.max(1, n * 0.20);
      if (range.from < threshold) _loadMainChartMoreHistory();
    });
  } catch (e) {}

  _installChartToolHandlers();
}

// Scroll-to-load-history state for the main bot-chart. Module-level
// so fetchChartData() can reset and re-populate the full candle
// array, and so the subscribeVisibleLogicalRangeChange handler
// above can inspect the array's length without reaching into
// LWC's series.
let _chartCandlesArr = [];
let _chartLoadingMore = false;
let _chartNoMoreData = false;
let _chartHistoryAbort = null;

function _chartMainToggleOverlay(id, show) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle('hidden', !show);
}

async function _loadMainChartMoreHistory() {
  if (_chartLoadingMore || _chartNoMoreData) return;
  if (!_chartCandlesArr.length || !_chartCandles) return;
  _chartLoadingMore = true;
  _chartMainToggleOverlay('chart-main-loading', true);

  const batchSize = 500;
  const tfSeconds = (window.RevertoChart && window.RevertoChart.tfSeconds)
    ? window.RevertoChart.tfSeconds : (() => 3600);
  const secPerBar = tfSeconds(_chartTimeframe);
  const oldest = _chartCandlesArr[0].time;
  const endIso = new Date(oldest * 1000).toISOString();
  const startIso = new Date((oldest - batchSize * secPerBar) * 1000).toISOString();

  const ctrl = new AbortController();
  _chartHistoryAbort = ctrl;
  const url = `/api/candles/${_pairForUrl(_chartPair)}/${_chartTimeframe}`
    + `?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`
    + `&limit=${batchSize}`;

  try {
    let r = await fetch(url, { signal: ctrl.signal });
    if (r.status === 429) {
      // Wait past the typical 1-minute slowapi window, then retry.
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (ctrl.signal.aborted) return;
      r = await fetch(url, { signal: ctrl.signal });
    }
    if (!r.ok) {
      console.warn('Main-chart scroll-to-load failed:', r.status);
      return;
    }
    const body = await r.json();
    const batch = Array.isArray(body) ? body
      : (body && Array.isArray(body.candles) ? body.candles : []);
    if (!batch.length) {
      _chartNoMoreData = true;
      _chartMainToggleOverlay('chart-main-no-more', true);
      return;
    }
    const prior = batch.filter((c) => c.time < oldest);
    if (!prior.length) {
      _chartNoMoreData = true;
      _chartMainToggleOverlay('chart-main-no-more', true);
      return;
    }
    _chartCandlesArr = prior.concat(_chartCandlesArr);
    _chartCandles.setData(_chartCandlesArr);
    // Full re-render of indicator overlays on the extended dataset
    // — the existing _renderIndicatorOverlays takes the candles
    // array as argument. Deal overlays + pending-deal markers
    // follow their timestamps automatically.
    _renderIndicatorOverlays(_chartCandlesArr);
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    console.warn('Main-chart scroll-to-load error:', e);
  } finally {
    if (_chartHistoryAbort === ctrl) _chartHistoryAbort = null;
    _chartLoadingMore = false;
    _chartMainToggleOverlay('chart-main-loading', false);
  }
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

  // Scroll-to-load preserves user-loaded history across the 30 s
  // polling refresh. If the stored array carries candles older
  // than the new batch's first timestamp, merge them back so the
  // user's pan-back progress survives the refresh — otherwise
  // they'd lose everything they scrolled to every half-minute.
  // Initial load (array empty) / timeframe-switch (array cleared
  // by teardownChartTab) both follow the plain replace-path.
  const newOldest = candles[0].time;
  const priorHistory = _chartCandlesArr.filter((c) => c.time < newOldest);
  _chartCandlesArr = priorHistory.concat(candles);
  _chartCandles.setData(_chartCandlesArr);
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
  // SUPPORT_RESISTANCE — fixnan stepped lines
  for (const s of _srLineSeries) {
    try { if (_chartMain) _chartMain.removeSeries(s); } catch (e) {}
  }
  _srLineSeries = [];
  const srCfg = _findIndicator('SUPPORT_RESISTANCE');
  if (srCfg && _chartMain) {
    const sr = calcSR(candles, srCfg.left_bars || 15, srCfg.right_bars || 15, srCfg.volume_threshold || 0, srCfg.min_touches || 1);
    const renderSegments = (series, color, label) => {
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
        const s = _chartMain.addSeries(_LWC().LineSeries, {
          color, lineWidth: 2, lineStyle: 0,
          priceLineVisible: false, lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        s.setData(data);
        _srLineSeries.push(s);
        if (seg.end === candles.length - 1) {
          s.createPriceLine({
            price: seg.value, color, lineWidth: 0, lineStyle: 0,
            axisLabelVisible: true, title: label,
          });
        }
      }
    };
    renderSegments(sr.resSeries, '#e53935', 'R');
    renderSegments(sr.supSeries, '#1e88e5', 'S');
  }
  // QFL — base segments + buy limit
  for (const s of _qflLineSeries) {
    try { if (_chartMain) _chartMain.removeSeries(s); } catch (e) {}
  }
  _qflLineSeries = [];
  const qflCfg = _findIndicator('QFL');
  if (qflCfg && _chartMain) {
    const qfl = calcQFL(candles, qflCfg.base_periods || 36, qflCfg.pump_periods || 8,
      qflCfg.pump_from_base_pct || 3.0, qflCfg.base_crack_pct || 3.0);
    const segs = [];
    let segStart = null, segVal = null;
    for (let i = 0; i < candles.length; i++) {
      if (qfl.baseSeries[i] === null) continue;
      if (segVal === null) { segStart = i; segVal = qfl.baseSeries[i]; }
      else if (qfl.baseSeries[i] !== segVal) {
        segs.push({ start: segStart, end: i - 1, value: segVal });
        segStart = i; segVal = qfl.baseSeries[i];
      }
    }
    if (segVal !== null) segs.push({ start: segStart, end: candles.length - 1, value: segVal });
    for (const seg of segs) {
      const data = [];
      for (let j = seg.start; j <= seg.end; j++) data.push({ time: candles[j].time, value: seg.value });
      const s = _chartMain.addSeries(_LWC().LineSeries, {
        color: '#f050a0', lineWidth: 1, lineStyle: 2,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      s.setData(data);
      _qflLineSeries.push(s);
    }
  }
  // PARABOLIC_SAR — LineSeries dots at exact SAR price + flip arrows
  for (const s of _psarLineSeries) {
    try { if (_chartMain) _chartMain.removeSeries(s); } catch (e) {}
  }
  _psarLineSeries = [];
  const markers = [];
  const psCfg = _findIndicator('PARABOLIC_SAR');
  if (psCfg && _chartMain) {
    const ps = calcParabolicSAR(candles, psCfg.initial_af || 0.02, psCfg.max_af || 0.20);
    const bullData = [], bearData = [];
    for (let i = 0; i < candles.length; i++) {
      if (ps.sarValues[i] === null) continue;
      const t = candles[i].time, v = ps.sarValues[i];
      if (ps.dirs[i] === 1) {
        bullData.push({ time: t, value: v });
        bearData.push({ time: t, value: NaN });
      } else {
        bearData.push({ time: t, value: v });
        bullData.push({ time: t, value: NaN });
      }
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
    const addSarSeries = (data, color) => {
      const filtered = data.filter(p => Number.isFinite(p.value));
      if (!filtered.length) return;
      const s = _chartMain.addSeries(_LWC().LineSeries, {
        color: 'transparent', lineWidth: 0,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      s.setData(filtered);
      const csm = _LWC().createSeriesMarkers;
      if (csm) csm(s, filtered.map(p => ({ time: p.time, position: 'inBar', color, shape: 'circle', size: 1 })));
      else try { s.setMarkers(filtered.map(p => ({ time: p.time, position: 'inBar', color, shape: 'circle', size: 1 }))); } catch (e) {}
      _psarLineSeries.push(s);
    };
    addSarSeries(bullData, 'rgba(51, 136, 187, 0.6)');
    addSarSeries(bearData, 'rgba(253, 204, 2, 0.6)');
  }
  // Market Structure markers
  const msCfg = _findIndicator('MARKET_STRUCTURE');
  if (msCfg) {
    const ms = calcMarketStructureMarkers(candles, msCfg.lookback || 3);
    for (const p of ms) markers.push(p);
  }
  // Entry markers — green arrow on first candle where ALL indicators agree
  const allInds = _indicatorsConfigured();
  if (allInds.length > 0) {
    const entryMarkers = _calcEntryMarkers(candles, allInds);
    for (const m of entryMarkers) markers.push(m);
  }
  _chartIndicatorMarkers = markers;
  _setCombinedMarkers();
}

function _calcEntryMarkers(candles, indicators) {
  const n = candles.length;
  if (n < 2 || !indicators.length) return [];
  const signals = [];
  for (const ind of indicators) {
    const arr = new Array(n).fill(false);
    const type = (ind.type || '').toUpperCase();
    if (type === 'RSI') {
      const period = ind.period || 14;
      const thr = (ind.threshold || 'below_35').toString();
      const m = thr.match(/^([a-z_]+)_(\d+)/i);
      const cond = m ? m[1] : 'below', val = m ? parseInt(m[2], 10) : 35;
      const line = calcRSILine(candles, period);
      const rsi = new Array(n).fill(NaN);
      const tMap = new Map(); candles.forEach((c, i) => tMap.set(c.time, i));
      for (const p of line) { const i = tMap.get(p.time); if (i != null) rsi[i] = p.value; }
      for (let i = 0; i < n; i++) {
        const v = rsi[i]; if (!Number.isFinite(v)) continue;
        if (cond === 'below' && v < val) arr[i] = true;
        else if (cond === 'above' && v > val) arr[i] = true;
        else if (cond === 'cross_above') { const p = rsi[i - 1]; if (Number.isFinite(p) && p <= val && v > val) arr[i] = true; }
        else if (cond === 'cross_below') { const p = rsi[i - 1]; if (Number.isFinite(p) && p >= val && v < val) arr[i] = true; }
      }
    } else if (type === 'MACD') {
      const macd = calcMACDLines(candles, ind.macd_fast || 12, ind.macd_slow || 26, ind.macd_signal || 9);
      const hMap = new Map(macd.histogram.map(p => [p.time, p.value]));
      const mMap = new Map(macd.macd.map(p => [p.time, p.value]));
      const sMap = new Map(macd.signal.map(p => [p.time, p.value]));
      const cond = ind.condition || 'histogram_positive';
      for (let i = 0; i < n; i++) {
        const t = candles[i].time, h = hMap.get(t), ml = mMap.get(t), sl = sMap.get(t);
        if (cond === 'histogram_positive' && h > 0) arr[i] = true;
        else if (cond === 'histogram_negative' && h < 0) arr[i] = true;
        else if (cond === 'macd_above_signal' && ml != null && sl != null && ml > sl) arr[i] = true;
        else if (cond === 'macd_below_signal' && ml != null && sl != null && ml < sl) arr[i] = true;
      }
    } else if (type === 'BOLLINGER') {
      const bb = calcBollingerLines(candles, ind.period || 20, ind.multiplier || 2.0);
      const loMap = new Map(bb.lower.map(p => [p.time, p.value]));
      const upMap = new Map(bb.upper.map(p => [p.time, p.value]));
      const cond = ind.condition || 'price_below_lower';
      for (let i = 0; i < n; i++) {
        const t = candles[i].time, c = candles[i].close;
        const lo = loMap.get(t), up = upMap.get(t);
        if (cond === 'price_below_lower' && lo != null && c < lo) arr[i] = true;
        else if (cond === 'price_above_upper' && up != null && c > up) arr[i] = true;
      }
    } else if (type === 'SUPERTREND') {
      const st = calcSupertrendLines(candles, ind.atr_period || 10, ind.multiplier || 3.0);
      if (st) {
        const bMap = new Map((st.bull || []).map(p => [p.time, p.value]));
        const cond = ind.condition || 'bullish';
        for (let i = 0; i < n; i++) {
          const t = candles[i].time;
          if (cond === 'bullish' && bMap.has(t)) arr[i] = true;
          else if (cond === 'bearish' && !bMap.has(t)) arr[i] = true;
        }
      }
    } else if (type === 'SUPPORT_RESISTANCE') {
      const sr = calcSR(candles, ind.left_bars || 15, ind.right_bars || 15, ind.volume_threshold || 0, ind.min_touches || 1);
      const cond = ind.condition || 'price_crossing_down';
      const val = ind.value || 'resistance';
      for (let i = 1; i < n; i++) {
        const c = candles[i].close, p = candles[i - 1].close;
        const lv = val === 'support' ? sr.supSeries[i] : sr.resSeries[i];
        if (lv === null) continue;
        if (cond === 'price_crossing_up' && p < lv && lv <= c) arr[i] = true;
        else if (cond === 'price_crossing_down' && p > lv && lv >= c) arr[i] = true;
        else if (cond === 'near_support' && sr.supSeries[i] != null) { if (Math.abs(c - sr.supSeries[i]) / sr.supSeries[i] * 100 <= (ind.proximity_pct || 1)) arr[i] = true; }
        else if (cond === 'near_resistance' && sr.resSeries[i] != null) { if (Math.abs(c - sr.resSeries[i]) / sr.resSeries[i] * 100 <= (ind.proximity_pct || 1)) arr[i] = true; }
      }
    } else if (type === 'QFL') {
      const qfl = calcQFL(candles, ind.base_periods || 36, ind.pump_periods || 8, ind.pump_from_base_pct || 3.0, ind.base_crack_pct || 3.0);
      const cond = ind.condition || 'below_base';
      for (let i = 0; i < n; i++) {
        if (cond === 'below_base' && qfl.buyLimitSeries[i] !== null) arr[i] = true;
        else if (cond === 'near_base' && qfl.baseSeries[i] !== null && Math.abs(candles[i].close - qfl.baseSeries[i]) / qfl.baseSeries[i] < 0.005) arr[i] = true;
      }
    } else if (type === 'PARABOLIC_SAR' || type === 'MARKET_STRUCTURE' || type === 'ASAP') {
      for (let i = 0; i < n; i++) arr[i] = true;
    } else {
      for (let i = 0; i < n; i++) arr[i] = true;
    }
    signals.push(arr);
  }
  const out = [];
  let wasEntry = false;
  for (let i = 1; i < n; i++) {
    const allTrue = signals.every(s => s[i]);
    if (allTrue && !wasEntry) {
      out.push({
        time: candles[i].time,
        position: 'belowBar',
        color: '#26a69a',
        shape: 'arrowUp',
        size: 1,
      });
    }
    wasEntry = allTrue;
  }
  return out;
}

function _setCombinedMarkers() {
  if (!_chartCandles) return;
  const combined = _chartIndicatorMarkers.concat(_chartDealMarkers);
  combined.sort((a, b) => a.time - b.time);
  try {
    const csm = _LWC().createSeriesMarkers;
    if (csm) {
      if (_candleMarkersPrimitive) _candleMarkersPrimitive.setMarkers(combined);
      else _candleMarkersPrimitive = csm(_chartCandles, combined);
    } else {
      _chartCandles.setMarkers(combined);
    }
  } catch (e) {}
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

// Picks the most recent open deal from the current detail state.
// Returns null if no open deals, no detail state yet, or state
// is for a different slug than the one we're rendering.
// Used by loadChartTab to auto-select a deal for the markers
// when the user opens the chart tab without having clicked a
// specific deal first.
function _mostRecentOpenDealForSlug(slug) {
  if (!_lastDetailState || !slug) return null;
  if (_lastDetailState.slug && _lastDetailState.slug !== slug) {
    return null;
  }
  const openDeals = _lastDetailState.open_deals || [];
  if (openDeals.length === 0) return null;
  const sorted = [...openDeals].sort((a, b) => {
    const aTime = a.opened_at || a.id || '';
    const bTime = b.opened_at || b.id || '';
    return bTime.localeCompare(aTime);
  });
  return sorted[0];
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
  const _wizardFmt = _buildChartTzFormatter(_wizardChartTimezone);
  _wizardChart = _lwcCreateChart(el, {
    ..._chartLayoutOpts(),
    localization: { timeFormatter: _wizardFmt.full },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: _wizardFmt.short,
    },
    width:  el.clientWidth,
    height: el.clientHeight || 250,
  });
  // Populate + wire the wizard timezone dropdown once per init —
  // teardown nulls _wizardChart so a re-init re-runs this block
  // with the fresh instance.
  const _wzTzSel = document.getElementById('wizard-chart-tz');
  if (_wzTzSel) {
    _populateTzDropdown(_wzTzSel, _wizardChartTimezone);
    _wzTzSel.onchange = () => {
      _wizardChartTimezone = _wzTzSel.value;
      try { localStorage.setItem(_WIZARD_CHART_TZ_LS_KEY, _wizardChartTimezone); } catch (e) {}
      if (_wizardChart) {
        const f = _buildChartTzFormatter(_wizardChartTimezone);
        _wizardChart.applyOptions({
          localization: { timeFormatter: f.full },
          timeScale: { tickMarkFormatter: f.short },
        });
      }
    };
  }
  _wizardCandles = _wizardChart.addSeries(_LWC().CandlestickSeries, {
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
  _wizardDestroyRsiPane();
  _wizardDestroyMacdPane();
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
  _wizardSrLineSeries = [];
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
  for (const s of _wizardSrLineSeries) {
    try { _wizardChart.removeSeries(s); } catch (e) {}
  }
  _wizardSrLineSeries = [];
  for (const s of _wizardPsarSeries) {
    try { _wizardChart.removeSeries(s); } catch (e) {}
  }
  _wizardPsarSeries = [];
  for (const s of _wizardQflSeries) {
    try { _wizardChart.removeSeries(s); } catch (e) {}
  }
  _wizardQflSeries = [];
}

function _wizardEnsureRsiPane() {
  if (_wizardSubSeries.rsi || !_wizardChart) return;
  _wizardSubSeries.rsi = _wizardChart.addSeries(_LWC().LineSeries, {
    color: _cssVar('--blue', '#5b8dee'), lineWidth: 1,
    priceLineVisible: false, lastValueVisible: true,
  }, 1);
}

function _wizardDestroyRsiPane() {
  if (!_wizardSubSeries.rsi || !_wizardChart) return;
  try { _wizardChart.removeSeries(_wizardSubSeries.rsi); } catch (e) {}
  _wizardRsiThresholdLine = null;
  delete _wizardSubSeries.rsi;
}

function _wizardEnsureMacdPane() {
  if (_wizardSubSeries.macdLine || !_wizardChart) return;
  const pane = _wizardSubSeries.rsi ? 2 : 1;
  _wizardSubSeries.macdHist   = _wizardChart.addSeries(_LWC().HistogramSeries, { color: _cssVar('--muted', '#888') }, pane);
  _wizardSubSeries.macdLine   = _wizardChart.addSeries(_LWC().LineSeries, { color: _cssVar('--blue',  '#5b8dee'), lineWidth: 1 }, pane);
  _wizardSubSeries.macdSignal = _wizardChart.addSeries(_LWC().LineSeries, { color: _cssVar('--amber', '#ffb347'), lineWidth: 1 }, pane);
}

function _wizardDestroyMacdPane() {
  if (!_wizardSubSeries.macdLine || !_wizardChart) return;
  try { _wizardChart.removeSeries(_wizardSubSeries.macdHist); } catch (e) {}
  try { _wizardChart.removeSeries(_wizardSubSeries.macdLine); } catch (e) {}
  try { _wizardChart.removeSeries(_wizardSubSeries.macdSignal); } catch (e) {}
  delete _wizardSubSeries.macdHist;
  delete _wizardSubSeries.macdLine;
  delete _wizardSubSeries.macdSignal;
}

function _addWizardLineSeries(data, color, lineWidth = 2, lineStyle = 0) {
  if (!_wizardChart || !data || !data.length) return null;
  const s = _wizardChart.addSeries(_LWC().LineSeries, {
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

  // v17 moved the flat `nbState.indicators` array into per-group
  // `nbState.indicatorGroups[].indicators`. This render function still
  // read the old (now-undefined) path, so every add-indicator click
  // silently rendered nothing on the wizard chart. Flatten both the
  // entry groups AND the TP indicator groups so the operator can see
  // every configured trigger while configuring. Sub-panes (RSI/MACD)
  // are idempotent — the ensure-helpers short-circuit on the second
  // indicator of the same type.
  const entryGroups = Array.isArray(nbState.indicatorGroups) ? nbState.indicatorGroups : [];
  const tpGroups    = Array.isArray(nbState.tpIndicatorGroups) ? nbState.tpIndicatorGroups : [];
  const indicators = [
    ...entryGroups.flatMap(g => (g && g.indicators) || []),
    ...tpGroups.flatMap(g => (g && g.indicators) || []),
  ];

  // Sub-charts: ensure / tear down based on whether the corresponding
  // indicator is currently configured. Each sub-chart is independent so
  // adding RSI alone doesn't drag MACD along.
  const rsiCfg  = indicators.find(i => i && String(i.type).toUpperCase() === 'RSI');
  const macdCfg = indicators.find(i => i && String(i.type).toUpperCase() === 'MACD');
  if (rsiCfg)  _wizardEnsureRsiPane();  else _wizardDestroyRsiPane();
  if (macdCfg) _wizardEnsureMacdPane(); else _wizardDestroyMacdPane();

  if (rsiCfg && _wizardSubSeries.rsi) {
    try {
      const period = Number(rsiCfg.period) || 14;
      _wizardSubSeries.rsi.setData(calcRSILine(candles, period));
      if (_wizardRsiThresholdLine) {
        try { _wizardSubSeries.rsi.removePriceLine(_wizardRsiThresholdLine); } catch (e) {}
      }
      const p = _parseRsiThreshold(rsiCfg.threshold);
      const tv = rsiCfg.rsi_value != null ? Number(rsiCfg.rsi_value) : p.value;
      if (tv) {
        _wizardRsiThresholdLine = _wizardSubSeries.rsi.createPriceLine({
          price: tv, color: _cssVar('--accent', '#26a69a'),
          lineStyle: 0, lineWidth: 1, axisLabelVisible: true, title: String(tv),
        });
      } else {
        _wizardRsiThresholdLine = null;
      }
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
      if (t === 'BOLLINGER') {
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
      } else if (t === 'PARABOLIC_SAR') {
        if (typeof calcParabolicSAR === 'function' && _wizardChart) {
          const ps = calcParabolicSAR(candles, Number(ind.initial_af) || 0.02, Number(ind.max_af) || 0.20);
          const bullD = [], bearD = [];
          for (let i = 0; i < candles.length; i++) {
            if (ps.sarValues[i] === null) continue;
            const pt = { time: candles[i].time, value: ps.sarValues[i] };
            if (ps.dirs[i] === 1) bullD.push(pt); else bearD.push(pt);
          }
          const addWizPsar = (data, color) => {
            if (!data.length) return;
            const s = _wizardChart.addSeries(_LWC().LineSeries, {
              color: 'transparent', lineWidth: 0,
              priceLineVisible: false, lastValueVisible: false,
              crosshairMarkerVisible: false,
            });
            s.setData(data);
            const csm = _LWC().createSeriesMarkers;
            if (csm) csm(s, data.map(p => ({ time: p.time, position: 'inBar', color, shape: 'circle', size: 1 })));
            else try { s.setMarkers(data.map(p => ({ time: p.time, position: 'inBar', color, shape: 'circle', size: 1 }))); } catch (e) {}
            _wizardPsarSeries.push(s);
          };
          addWizPsar(bullD, 'rgba(51, 136, 187, 0.6)');
          addWizPsar(bearD, 'rgba(253, 204, 2, 0.6)');
        }
      } else if (t === 'SUPPORT_RESISTANCE') {
        if (typeof calcSR === 'function' && _wizardCandleCache && _wizardChart) {
          const wCandles = _wizardCandleCache;
          const sr = calcSR(wCandles, Number(ind.left_bars) || 15, Number(ind.right_bars) || 15, Number(ind.volume_threshold) || 0, Number(ind.min_touches) || 1);
          if (sr) {
            const wizSegments = (series, color, label) => {
              const segs = [];
              let segStart = null, segVal = null;
              for (let i = 0; i < wCandles.length; i++) {
                if (series[i] === null) continue;
                if (segVal === null) { segStart = i; segVal = series[i]; }
                else if (series[i] !== segVal) {
                  segs.push({ start: segStart, end: i - 1, value: segVal });
                  segStart = i; segVal = series[i];
                }
              }
              if (segVal !== null) segs.push({ start: segStart, end: wCandles.length - 1, value: segVal });
              for (const seg of segs) {
                const data = [];
                for (let j = seg.start; j <= seg.end; j++) data.push({ time: wCandles[j].time, value: seg.value });
                const ws = _wizardChart.addSeries(_LWC().LineSeries, {
                  color, lineWidth: 2, lineStyle: 0,
                  priceLineVisible: false, lastValueVisible: false,
                  title: label, crosshairMarkerVisible: false,
                });
                ws.setData(data);
                _wizardSrLineSeries.push(ws);
              }
            };
            wizSegments(sr.resSeries, '#e53935', 'R');
            wizSegments(sr.supSeries, '#1e88e5', 'S');
          }
        }
      } else if (t === 'QFL') {
        if (typeof calcQFL === 'function' && _wizardChart) {
          const wCandles = _wizardCandleCache;
          const qfl = calcQFL(wCandles, Number(ind.base_periods) || 36, Number(ind.pump_periods) || 8,
            Number(ind.pump_from_base_pct) || 3.0, Number(ind.base_crack_pct) || 3.0);
          const segs = [];
          let segStart = null, segVal = null;
          for (let i = 0; i < wCandles.length; i++) {
            if (qfl.baseSeries[i] === null) continue;
            if (segVal === null) { segStart = i; segVal = qfl.baseSeries[i]; }
            else if (qfl.baseSeries[i] !== segVal) {
              segs.push({ start: segStart, end: i - 1, value: segVal });
              segStart = i; segVal = qfl.baseSeries[i];
            }
          }
          if (segVal !== null) segs.push({ start: segStart, end: wCandles.length - 1, value: segVal });
          for (const seg of segs) {
            const data = [];
            for (let j = seg.start; j <= seg.end; j++) data.push({ time: wCandles[j].time, value: seg.value });
            const ws = _wizardChart.addSeries(_LWC().LineSeries, {
              color: '#f050a0', lineWidth: 1, lineStyle: 2,
              priceLineVisible: false, lastValueVisible: false,
              crosshairMarkerVisible: false,
            });
            ws.setData(data);
            _wizardQflSeries.push(ws);
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
// Indicator-math helpers (_emaArray + 9 calc* functions) moved to
// web/static/chart_module.js as part of the PR 3a Workspace-feature
// refactor. They keep the same top-level function names so every
// call site here stays unchanged; the script loads before app.js
// so runtime resolution is trivial. See chart_module.js for the
// full namespace + rationale.

// ── Event wiring (vervangt alle inline onclick=) ─────────────────────────────
function setupEventListeners() {
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
  // PR 5b: Enter-key submit inside the wrapped <form>. Routes
  // through saveProfileModal so the form-path and the explicit
  // Save-button path are identical. preventDefault stops the
  // browser's default form submit (which would navigate away and
  // break the SPA).
  const _profilePwForm = $('profile-pw-form');
  if (_profilePwForm) {
    _profilePwForm.addEventListener('submit', (e) => {
      e.preventDefault();
      saveProfileModal();
    });
  }

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
  const navWsBtn = $('nav-workspace-btn');
  if (navWsBtn) navWsBtn.addEventListener('click', () => goWorkspace());
  const wsAddChart = $('workspace-add-chart');
  if (wsAddChart) wsAddChart.addEventListener('click', _handleWorkspaceAddChartPanel);
  const wsAddDeals = $('workspace-add-deals');
  if (wsAddDeals) wsAddDeals.addEventListener('click', _handleWorkspaceAddOpenDealsPanel);
  const navClBtn = $('nav-changelog-btn');
  if (navClBtn) navClBtn.addEventListener('click', () => goChangelog());
  const navAdminBtn = $('nav-admin-btn');
  if (navAdminBtn) navAdminBtn.addEventListener('click', () => goAdmin());
  const adminCard = $('admin-card-changelog');
  if (adminCard) adminCard.addEventListener('click', (e) => {
    e.preventDefault();
    goAdmin(false, 'changelog-manage');
  });
  const adminBotsCard = $('admin-card-bots');
  if (adminBotsCard) adminBotsCard.addEventListener('click', (e) => {
    e.preventDefault();
    goAdmin(false, 'bots');
  });
  // Admin sub-page back-link — the changelog subpage carries id
  // "admin-back-link" (legacy), the new bots subpage uses
  // class="admin-back-link". One handler per element, no duplicates.
  const backTargets = new Set([
    ...document.querySelectorAll('.admin-back-link'),
  ]);
  const legacyBack = $('admin-back-link');
  if (legacyBack) backTargets.add(legacyBack);
  backTargets.forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      goAdmin();
    });
  });
  const emergencyOpenBtn = $('admin-emergency-stop-btn');
  if (emergencyOpenBtn) {
    emergencyOpenBtn.addEventListener('click', _openEmergencyStopModal);
  }
  const emergencyCancel = $('emergency-stop-cancel');
  if (emergencyCancel) {
    emergencyCancel.addEventListener('click', _closeEmergencyStopModal);
  }
  const emergencyConfirm = $('emergency-stop-confirm');
  if (emergencyConfirm) {
    emergencyConfirm.addEventListener('click', _confirmEmergencyStop);
  }
  // Status filter on the admin bot-overview. Changing the radio
  // re-filters the cached payload (no network round-trip) so the
  // selection stays intact across filter flips.
  document.querySelectorAll('input[name="admin-bots-filter"]').forEach((el) => {
    el.addEventListener('change', (e) => {
      const val = e.target && e.target.value;
      if (val === 'all' || val === 'running' || val === 'stopped') {
        _adminBotsFilter = val;
        _renderAdminBotsFromCache();
      }
    });
  });
  // Bulk action buttons + their confirmation modals.
  const bulkStopBtn = $('admin-bulk-stop-btn');
  if (bulkStopBtn) {
    bulkStopBtn.addEventListener('click', () => _openBulkModal('stop'));
  }
  const bulkRestartBtn = $('admin-bulk-restart-btn');
  if (bulkRestartBtn) {
    bulkRestartBtn.addEventListener('click', () => _openBulkModal('restart'));
  }
  const bulkStopCancel = $('bulk-stop-cancel');
  if (bulkStopCancel) {
    bulkStopCancel.addEventListener('click', () => _closeBulkModal('stop'));
  }
  const bulkStopConfirm = $('bulk-stop-confirm');
  if (bulkStopConfirm) {
    bulkStopConfirm.addEventListener('click', () => _confirmBulkAction('stop'));
  }
  const bulkRestartCancel = $('bulk-restart-cancel');
  if (bulkRestartCancel) {
    bulkRestartCancel.addEventListener('click', () => _closeBulkModal('restart'));
  }
  const bulkRestartConfirm = $('bulk-restart-confirm');
  if (bulkRestartConfirm) {
    bulkRestartConfirm.addEventListener('click', () => _confirmBulkAction('restart'));
  }
  const clNewBtn = $('admin-cl-new-btn');
  if (clNewBtn) clNewBtn.addEventListener('click', () => openClEditModal(null));
  const clCancel = $('cl-modal-cancel');
  if (clCancel) clCancel.addEventListener('click', closeClEditModal);
  const clDraft = $('cl-modal-save-draft');
  if (clDraft) clDraft.addEventListener('click', () => _clSaveModal(false));
  const clPublish = $('cl-modal-save-publish');
  if (clPublish) clPublish.addEventListener('click', () => _clSaveModal(true));

  // The dedicated detail-back button is gone; users leave the detail
  // view via the main Bots tab or the browser back button (popstate).

  $('new-bot-btn').addEventListener('click', goNewBot);
  const importBtn = $('import-bot-btn');
  if (importBtn) importBtn.addEventListener('click', importBot);
  const deCancel = $('deal-edit-cancel-btn');
  if (deCancel) deCancel.addEventListener('click', () => {
    $('deal-edit-modal').classList.remove('show');
    _dealEditState = null;
  });
  const deSave = $('deal-edit-save-btn');
  if (deSave) deSave.addEventListener('click', dealSaveEdit);
  const deDcaChk = $('de-dca-enabled');
  if (deDcaChk) deDcaChk.addEventListener('change', _deUpdateDcaDisabled);
  const navBtBtn = $('nav-backtests-btn');
  if (navBtBtn) navBtBtn.addEventListener('click', () => goBacktests());
  const histGearBtn = $('bt-history-gear-btn');
  if (histGearBtn) histGearBtn.addEventListener('click', () => {
    const menu = $('bt-history-gear-menu');
    if (menu) menu.classList.toggle('hidden');
  });

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
  const tzSel = document.getElementById('chart-tz');
  if (tzSel) {
    // Populate options from the central catalogue so adding a new
    // IANA zone is a one-line change in chart_module.js. Clearing
    // first removes the placeholder option the HTML ships with.
    const tzList = (window.RevertoChart && window.RevertoChart.CHART_TIMEZONES) || [];
    if (tzList.length) {
      tzSel.innerHTML = '';
      for (const tz of tzList) {
        const o = document.createElement('option');
        o.value = tz.value;
        o.textContent = tz.label;
        tzSel.appendChild(o);
      }
    }
    tzSel.value = _chartTimezone;
    tzSel.addEventListener('change', () => {
      _chartTimezone = tzSel.value;
      _saveMainChartTz(_chartTimezone);
      if (_chartMain) {
        const fmt = _buildChartTzFormatter(_chartTimezone);
        _chartMain.applyOptions({
          localization: { timeFormatter: fmt.full },
          timeScale: { tickMarkFormatter: fmt.short },
        });
      }
    });
  }

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
    // Skip clicks on deal action buttons (close/cancel/edit). Those have
    // their own document-level handler on regel 1652 and should NOT also
    // trigger row-navigation to the chart. stopPropagation() in the
    // button handler is too late — both handlers attach to different
    // DOM nodes (tbody vs document) and tbody fires earlier in the
    // bubble path, so we need this explicit opt-out here.
    if (e.target.closest('.deal-btn')) return;
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

  // Log-level filter — re-filter existing DOM so historical lines
  // hide/show immediately on dropdown change, not only new lines.
  const _logLevelSelect = $('log-level-filter');
  if (_logLevelSelect) {
    _logLevelSelect.addEventListener('change', (e) => {
      _logLevelFilter = e.target.value;
      _applyLogLevelFilter();
    });
  }

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

  // Cancel in edit-mode: discard in-memory wizard state and navigate
  // back to the bot-detail config tab. The YAML on disk is NEVER
  // touched — we haven't called /api/bots/{slug}/config PUT yet, so
  // there's nothing to roll back; we simply forget the user's edits.
  $('nb-cancel-btn').addEventListener('click', () => {
    const slug = nbEditSlug;
    nbInit();  // resets state + hides Cancel + relabels submit btn
    if (slug) {
      openBot(slug);
    } else {
      showPage('bots');
    }
  });

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
  const tpPriceToggle = $('nb-tp-price-enabled');
  if (tpPriceToggle) tpPriceToggle.addEventListener('change', e => {
    nbState.tp_price_enabled = e.target.checked;
    const pr = $('nb-tp-pct-row'), mr = $('nb-tp-min-row');
    if (pr) pr.style.display = e.target.checked ? '' : 'none';
    if (mr) mr.style.display = e.target.checked ? '' : 'none';
    nbRecompute();
  });
  ['nb-tp-enabled', 'nb-sl-enabled', 'nb-dca-enabled', 'nb-sched-enabled'].forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('change', () => { nbUpdateToggleStates(); nbRecompute(); });
  });
  // Prevent toggle label clicks from toggling the <details> parent
  document.querySelectorAll('.wizard-toggle').forEach(lbl => {
    lbl.addEventListener('click', e => e.stopPropagation());
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

  function _nbResolveGind(key) {
    if (!nbState || !key) return null;
    const parts = key.split(':');
    if (parts[0] === 'tp') {
      const [, gid, idx] = parts.map((v, i) => i === 0 ? v : Number(v));
      const g = (nbState.tpIndicatorGroups || []).find(g => g.id === gid);
      return g && g.indicators[idx] ? g.indicators[idx] : null;
    }
    const [gid, idx] = parts.map(Number);
    const g = (nbState.indicatorGroups || []).find(g => g.id === gid);
    return g && g.indicators[idx] ? g.indicators[idx] : null;
  }

  // Indicator row event delegation (input changes, type switch, remove)
  document.addEventListener('input', e => {
    const t = e.target;
    const gk = t.dataset?.nbGind || t.dataset?.nbInd;
    if (gk != null && t.dataset.nbField) {
      const ind = _nbResolveGind(gk);
      if (!ind) return;
      const f = t.dataset.nbField;
      let v = t.value;
      const intFields = [
        'period', 'fast', 'slow',
        'rsi_value', 'macd_fast', 'macd_slow', 'macd_signal',
        'atr_period', 'lookback', 'min_touches',
        'base_periods', 'pump_periods',
      ];
      const floatFields = [
        'multiplier', 'initial_af', 'max_af',
        'proximity_pct', 'volume_threshold', 'squeeze_threshold',
        'pump_from_base_pct', 'base_crack_pct',
      ];
      if (intFields.includes(f)) v = parseInt(v, 10) || 0;
      else if (floatFields.includes(f)) v = parseFloat(v) || 0;
      ind[f] = v;
      if (f === 'rsi_condition' || f === 'rsi_value') {
        const cond = ind.rsi_condition || 'below';
        const val = Math.min(99, Math.max(1, ind.rsi_value || 35));
        ind.threshold = `${cond}_${val}`;
      }
      if (f === 'type') {
        if (gk.startsWith('tp:')) nbRenderTpIndicators();
        else nbRenderIndicators();
      }
      nbRecompute();
    }
    if (t.dataset?.nbGname) {
      const g = (nbState.indicatorGroups || []).find(g => g.id === Number(t.dataset.nbGname));
      if (g) g.name = t.value;
    }
    if (t.dataset?.nbTpGname) {
      const g = (nbState.tpIndicatorGroups || []).find(g => g.id === Number(t.dataset.nbTpGname));
      if (g) g.name = t.value;
    }
  });
  document.addEventListener('change', e => {
    const t = e.target;
    const gk = t.dataset?.nbGind || t.dataset?.nbInd;
    if (gk != null && t.dataset.nbField === 'type') {
      const ind = _nbResolveGind(gk);
      if (ind) {
        ind.type = t.value;
        if (gk.startsWith('tp:')) nbRenderTpIndicators();
        else nbRenderIndicators();
        nbRecompute();
      }
    }
  });
  document.addEventListener('click', e => {
    const ab = e.target.closest('[data-nb-action]');
    if (ab) {
      const act = ab.dataset.nbAction;
      if (act === 'add-group') { nbAddGroup(); return; }
      if (act === 'add-indicator') { nbAddIndicator(parseInt(ab.dataset.nbGid)); return; }
      if (act === 'add-tp-group') { nbAddTpGroup(); return; }
      if (act === 'add-tp-indicator') { nbAddTpIndicator(parseInt(ab.dataset.nbGid)); return; }
      if (act === 'remove-tp-group') { nbRemoveTpGroup(parseInt(ab.dataset.nbGid)); return; }
    }
    const rm = e.target.closest('[data-nb-grm]');
    if (rm) {
      const parts = rm.dataset.nbGrm.split(':');
      if (parts[0] === 'tp') {
        nbRemoveTpIndicator(Number(parts[1]), Number(parts[2]));
      } else {
        nbRemoveIndicator(Number(parts[0]), Number(parts[1]));
      }
      return;
    }
    const grm = e.target.closest('[data-nb-gremove]');
    if (grm) { nbRemoveGroup(Number(grm.dataset.nbGremove)); return; }
    const t = e.target.closest('[data-nb-remove]');
    if (t) { nbRemoveIndicator(null, parseInt(t.dataset.nbRemove, 10)); }
  });

  // Number-input scroll-blocker: when a wheel event lands on a
  // <input type="number"> that is NOT focused, block the browser's
  // wheel-changes-value default and let the page scroll instead.
  // Focused number-inputs keep the native wheel-step behaviour
  // intact for operators who deliberately use it.
  document.addEventListener('wheel', (e) => {
    const t = e.target;
    if (t && t.matches && t.matches('input[type="number"]') &&
        document.activeElement !== t) {
      e.preventDefault();
      window.scrollBy(0, e.deltaY);
    }
  }, { passive: false });
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  setupEventListeners();

  // Gate: require a valid session cookie before bringing up the SPA.
  const authed = await checkAuthStatus();
  if (!authed) {
    _handle401();
    // Anti-flash gate (paired with the visibility:hidden rule in
    // index.html): reveal body AFTER _handle401() has stripped the
    // protected chrome and shown the login view. Doing it before
    // would re-introduce the flash we're trying to prevent.
    document.body.classList.add('auth-checked');
    return;
  }
  // Authed — make sure the chrome is visible (a previous _handle401
  // call from a stale tab on the same page could have left the
  // .is-login class on body).
  document.body.classList.remove('is-login');

  // Admin-only nav items (e.g. the "Admin" tab) stay hidden in HTML
  // via the `hidden` attribute and only reveal once the auth check
  // confirms user_id=1. Running this before the SPA renders avoids
  // a flash of the Admin tab on a regular user's reload.
  applyAdminVisibility();

  // Anti-flash gate: reveal body once the chrome + admin-visibility
  // are correct for this user. Idempotent — if the 3s safety
  // timeout in index.html already added the class, this is a no-op.
  document.body.classList.add('auth-checked');

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
      case 'bot':
        if (s.slug) {
          openBot(s.slug, true);
          if (s.dtab) setTimeout(() => {
            const tb = document.querySelector(`.detail-subnav .tab[data-dtab="${s.dtab}"]`);
            if (tb) showDTab(s.dtab, tb, true);
          }, 60);
        } else { goOverview(true); }
        break;
      case 'bots':     goBots(true); break;
      case 'deals':    goDeals(true); break;
      case 'workspace': goWorkspace(true); break;
      case 'backtests': goBacktests(true); break;
      case 'changelog': goChangelog(true); break;
      case 'admin':    goAdmin(true, s.sub || null); break;
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
  _priceInterval = setInterval(fetchPrice, 15000);
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
  document.addEventListener('click', e => {
    // Backtest deal rows → chart highlight.
    const row = e.target.closest('.bt-deal-row');
    if (row) {
      const idx = parseInt(row.dataset.btDeal);
      if (isNaN(idx)) return;
      btShowDealOnChart(idx);
      return;
    }
    // Backtest deal-table pagination — delegated so buttons can be
    // re-rendered on every table refresh without stacking listeners.
    const pageBtn = e.target.closest('.bt-page-btn');
    if (pageBtn && !pageBtn.disabled && _btPageDeals) {
      const dir = pageBtn.dataset.btPage;
      if (dir === 'prev' && _btDealPage > 0) _btDealPage--;
      else if (dir === 'next' && _btDealPage < _btPageTotalPages - 1) _btDealPage++;
      btRenderDealTable(_btPageDeals);
    }
  });
  const btSweepBtn = $('bt-sweep-btn');
  if (btSweepBtn) btSweepBtn.addEventListener('click', swOpenModal);
  const swCloseBtn = $('sw-close-btn');
  const _swClose = () => $('sweep-modal').classList.remove('show');
  if (swCloseBtn) swCloseBtn.addEventListener('click', _swClose);
  const swModal = $('sweep-modal');
  if (swModal) swModal.addEventListener('click', (e) => {
    if (e.target === swModal) _swClose();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && swModal && swModal.classList.contains('show')) _swClose();
  });
  document.querySelectorAll('.sweep-result-tabs .sweep-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.sweep-result-tabs .sweep-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const v = tab.dataset.swView;
      const tbl = $('sw-view-table');
      const cht = $('sw-view-chart');
      if (tbl) tbl.classList.toggle('hidden', v !== 'table');
      if (cht) cht.classList.toggle('hidden', v !== 'chart');
    });
  });
  const swRunBtn = $('sw-run-btn');
  if (swRunBtn) swRunBtn.addEventListener('click', swRunSweep);
  const swStopBtn = $('sw-stop-btn');
  if (swStopBtn) swStopBtn.addEventListener('click', () => { _swStopped = true; });
  // Sweep tab switching
  document.querySelectorAll('.sweep-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.sweep-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.sweep-pane').forEach(p => p.classList.add('hidden'));
      tab.classList.add('active');
      const pane = $('sweep-pane-' + tab.dataset.sweepTab);
      if (pane) pane.classList.remove('hidden');
    });
  });
  // Sweep estimate live update + schedule mode toggles
  document.querySelectorAll('#sweep-modal input, #sweep-modal select').forEach(el => {
    el.addEventListener('input', swUpdateEstimate);
    el.addEventListener('change', swUpdateEstimate);
  });
  // DCA param selector: hide step dropdown for max_orders (always step=1)
  const swDcaParam = $('sw-dca-param');
  if (swDcaParam) swDcaParam.addEventListener('change', () => {
    const stepRow = $('sw-dca-step') && $('sw-dca-step').closest('.form-row');
    if (stepRow) stepRow.classList.toggle('hidden', swDcaParam.value === 'max_orders');
  });
  // Schedule sweep: show/hide day checkboxes and hour range on toggle
  const swDaysChk = $('sw-sched-days-enabled');
  if (swDaysChk) swDaysChk.addEventListener('change', () => {
    const p = $('sw-day-picks'); if (p) p.classList.toggle('hidden', !swDaysChk.checked);
    swUpdateEstimate();
  });
  const swHoursChk = $('sw-sched-hours-enabled');
  if (swHoursChk) swHoursChk.addEventListener('change', () => {
    const p = $('sw-hour-range'); if (p) p.classList.toggle('hidden', !swHoursChk.checked);
    swUpdateEstimate();
  });
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
    this._entryFilter = config.entryFilter || null;
    this._entryFilterUtc = config.entryFilterUtc !== false;
    this._tp_enabled = !config.take_profit || config.take_profit.enabled !== false;
    this._tp_pct   = (config.take_profit && config.take_profit.target_pct) || 3.0;
    this._tp_price_enabled = !config.take_profit || config.take_profit.price_enabled !== false;
    this._tp_ind_groups = (config.take_profit?.indicator_groups || []).filter(g => g.indicators?.length);
    this.config = config;
    this._sl_pct   = (config.stop_loss   && config.stop_loss.pct)          || 5.0;
    this._sl_type  = (config.stop_loss   && config.stop_loss.type)         || 'fixed';
    this._dca_enabled = !config.dca || config.dca.enabled !== false;
    this._base_size = (config.dca && config.dca.base_order_size) || 0.001;
    this._max_orders = (config.dca && config.dca.max_orders)      || 1;
    this._spacing = (config.dca && config.dca.order_spacing_pct)  || 2.5;
    this._step_scale = (config.dca && config.dca.step_scale != null) ? config.dca.step_scale : 1.0;
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
      entry_trigger: this._lastEntryTrigger || null,
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

  _checkTp(deal, candle, candleIdx) {
    if (!this._tp_enabled) return false;
    const avg = this._avgEntry(deal);
    const tpPrice = avg * (1 + this._tp_pct / 100);
    if (this._tp_price_enabled && candle.high >= tpPrice) {
      deal.exit_trigger = 'price_tp';
      this._closeDeal(deal, tpPrice, 'tp', candle.time);
      return true;
    }
    if (this._tp_ind_groups.length && candleIdx != null) {
      for (const g of this._tp_ind_groups) {
        let allTrue = true;
        for (const ind of g.indicators) {
          if (!this._evalTpIndicator(ind, candleIdx)) { allTrue = false; break; }
        }
        if (allTrue) {
          deal.exit_trigger = 'indicator_tp';
          this._closeDeal(deal, candle.close, 'tp', candle.time);
          return true;
        }
      }
    }
    return false;
  }

  _evalTpIndicator(ind, i) {
    const t = (ind.type || '').toUpperCase();
    const c = this.candles;
    if (i < 1) return false;
    try {
      if (t === 'RSI') {
        const line = calcRSILine(c, ind.period || 14);
        const tMap = new Map(line.map(p => [p.time, p.value]));
        const v = tMap.get(c[i].time);
        if (v == null) return false;
        const thr = (ind.threshold || 'above_70').toString();
        const m = thr.match(/^([a-z_]+)_(\d+)/i);
        const cond = m ? m[1] : 'above', val = m ? parseInt(m[2], 10) : 70;
        if (cond === 'above') return v > val;
        if (cond === 'below') return v < val;
        return false;
      }
      if (t === 'MACD') {
        const md = calcMACDLines(c, ind.macd_fast || 12, ind.macd_slow || 26, ind.macd_signal || 9);
        const hMap = new Map(md.histogram.map(p => [p.time, p.value]));
        const h = hMap.get(c[i].time);
        const cond = ind.condition || 'histogram_positive';
        if (cond === 'histogram_positive') return h != null && h > 0;
        if (cond === 'histogram_negative') return h != null && h < 0;
        return false;
      }
    } catch (e) {}
    return false;
  }

  _checkSl(deal, candle) {
    if (this._sl_type === 'none') return false;
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
    if (!this._dca_enabled) return;
    if (this._max_orders <= 1) return;
    if (deal.dca_count >= this._max_orders - 1) return;
    const lastPrice = deal.orders[deal.orders.length - 1].price;
    const step = this._spacing * Math.pow(this._step_scale, deal.dca_count);
    const nextDca = lastPrice * (1 - step / 100);
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
      entry_trigger: deal.entry_trigger || null,
      exit_trigger: deal.exit_trigger || (reason === 'tp' ? 'price_tp' : reason === 'sl' ? 'price_sl' : reason),
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
    // semantics for RSI / MACD histogram / Bollinger lower
    // band; other indicator types simplify to "always true" — they are
    // intentionally documented as a client-side-backtester limitation.
    const entry = this.config.entry || {};
    const groups = (entry.indicator_groups || []).filter(g => g.indicators && g.indicators.length);
    const flatInds = entry.indicators || [];
    const allGroups = groups.length ? groups.map(g => g.indicators)
      : flatInds.length ? [flatInds] : [];
    const n = this.candles.length;
    if (!allGroups.length) return [new Array(n).fill(true)];
    const groupSignals = [];
    for (const groupInds of allGroups) {
      const gArrays = [];
      for (const ind of groupInds) {
      const arr = new Array(n).fill(false);
      const type = ind.type;
      if (type === 'RSI') {
        const period = ind.period || 14;
        const thr = (ind.threshold || 'below_35').toString();
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
      } else if (type === 'MACD') {
        const fast   = ind.macd_fast   || 12;
        const slow   = ind.macd_slow   || 26;
        const signal = ind.macd_signal || 9;
        const cond   = ind.condition || 'histogram_positive';
        const macdData = calcMACDLines(this.candles, fast, slow, signal);
        const histMap = new Map(macdData.histogram.map(p => [p.time, p.value]));
        const macdMap = new Map(macdData.macd.map(p => [p.time, p.value]));
        const sigMap  = new Map(macdData.signal.map(p => [p.time, p.value]));
        for (let i = 0; i < n; i++) {
          const t = this.candles[i].time;
          const h = histMap.get(t), m = macdMap.get(t), s = sigMap.get(t);
          if (cond === 'histogram_positive' && h != null && h > 0) arr[i] = true;
          else if (cond === 'histogram_negative' && h != null && h < 0) arr[i] = true;
          else if (cond === 'macd_above_signal' && m != null && s != null && m > s) arr[i] = true;
          else if (cond === 'macd_below_signal' && m != null && s != null && m < s) arr[i] = true;
        }
      } else if (type === 'BOLLINGER') {
        const period = ind.period || 20;
        const mult = ind.multiplier != null ? ind.multiplier : 2.0;
        const cond = ind.condition || 'price_below_lower';
        const bb = calcBollingerLines(this.candles, period, mult);
        const upperMap  = new Map(bb.upper.map(p => [p.time, p.value]));
        const middleMap = new Map(bb.middle.map(p => [p.time, p.value]));
        const lowerMap  = new Map(bb.lower.map(p => [p.time, p.value]));
        for (let i = 0; i < n; i++) {
          const t = this.candles[i].time, c = this.candles[i].close;
          const lo = lowerMap.get(t), mid = middleMap.get(t), up = upperMap.get(t);
          if (cond === 'price_below_lower'  && lo != null && c < lo) arr[i] = true;
          else if (cond === 'price_above_upper'  && up != null && c > up) arr[i] = true;
          else if (cond === 'price_above_middle' && mid != null && c > mid) arr[i] = true;
          else if (cond === 'price_below_middle' && mid != null && c < mid) arr[i] = true;
          else if (cond === 'squeeze' && lo != null && up != null && mid != null && mid > 0) {
            if ((up - lo) / mid < 0.02) arr[i] = true;
          }
        }
      } else if (type === 'PARABOLIC_SAR') {
        const iaf = ind.initial_af != null ? ind.initial_af : 0.02;
        const maf = ind.max_af != null ? ind.max_af : 0.20;
        const cond = ind.condition || 'bullish';
        const hi = this.candles.map(c => c.high != null ? c.high : c.close);
        const lo = this.candles.map(c => c.low != null ? c.low : c.close);
        const cl = this.candles.map(c => c.close);
        if (cl.length >= 10) {
          let trend, ep, sar, af = iaf;
          if (cl[1] >= cl[0]) { trend = 1; ep = hi[1]; sar = lo[0]; }
          else { trend = -1; ep = lo[1]; sar = hi[0]; }
          for (let i = 2; i < n; i++) {
            const prevTrend = trend;
            let newSar = sar + af * (ep - sar);
            if (trend === 1) {
              newSar = Math.min(newSar, lo[i - 1], i >= 3 ? lo[i - 2] : lo[i - 1]);
              if (newSar > lo[i]) { trend = -1; sar = ep; ep = lo[i]; af = iaf; }
              else { sar = newSar; if (hi[i] > ep) { ep = hi[i]; af = Math.min(af + iaf, maf); } }
            } else {
              newSar = Math.max(newSar, hi[i - 1], i >= 3 ? hi[i - 2] : hi[i - 1]);
              if (newSar < hi[i]) { trend = 1; sar = ep; ep = hi[i]; af = iaf; }
              else { sar = newSar; if (lo[i] < ep) { ep = lo[i]; af = Math.min(af + iaf, maf); } }
            }
            if ((cond === 'bullish' || cond === 'price_greater_than') && trend === 1) arr[i] = true;
            else if ((cond === 'bearish' || cond === 'price_lower_than') && trend === -1) arr[i] = true;
            else if ((cond === 'bullish_flip' || cond === 'price_crossing_up') && prevTrend === -1 && trend === 1) arr[i] = true;
            else if ((cond === 'bearish_flip' || cond === 'price_crossing_down') && prevTrend === 1 && trend === -1) arr[i] = true;
          }
        }
      } else if (type === 'SUPERTREND') {
        const atrP = ind.atr_period != null ? ind.atr_period : 10;
        const mult = ind.multiplier != null ? ind.multiplier : 3.0;
        const cond = ind.condition || 'bullish';
        const highs = this.candles.map(c => c.high), lows = this.candles.map(c => c.low);
        const closes = this.candles.map(c => c.close);
        if (n > atrP + 1) {
          const tr = new Array(n).fill(0);
          for (let i = 1; i < n; i++) tr[i] = Math.max(highs[i]-lows[i], Math.abs(highs[i]-closes[i-1]), Math.abs(lows[i]-closes[i-1]));
          const atr = new Array(n).fill(0);
          let s = 0; for (let i = 1; i <= atrP; i++) s += tr[i]; atr[atrP] = s / atrP;
          for (let i = atrP+1; i < n; i++) atr[i] = (atr[i-1]*(atrP-1)+tr[i])/atrP;
          let pFU = 0, pFL = 0, pT = 1;
          for (let i = atrP; i < n; i++) {
            const mid = (highs[i]+lows[i])/2;
            const bU = mid + mult*atr[i], bL = mid - mult*atr[i];
            let fU, fL, trend;
            if (i === atrP) { fU = bU; fL = bL; trend = closes[i] > bU ? 1 : -1; }
            else {
              fU = (bU < pFU || closes[i-1] > pFU) ? bU : pFU;
              fL = (bL > pFL || closes[i-1] < pFL) ? bL : pFL;
              trend = pT === 1 ? (closes[i] < fL ? -1 : 1) : (closes[i] > fU ? 1 : -1);
            }
            if (cond === 'bullish' && trend === 1) arr[i] = true;
            else if (cond === 'bearish' && trend === -1) arr[i] = true;
            else if ((cond === 'bullish_flip' || cond === 'from_down_to_up') && pT === -1 && trend === 1) arr[i] = true;
            else if ((cond === 'bearish_flip' || cond === 'from_up_to_down') && pT === 1 && trend === -1) arr[i] = true;
            pFU = fU; pFL = fL; pT = trend;
          }
        }
      } else if (type === 'MARKET_STRUCTURE') {
        const lb = ind.lookback != null ? ind.lookback : 3;
        const cond = ind.condition || 'bullish_bos';
        const closes = this.candles.map(c => c.close);
        const swHi = [], swLo = [];
        for (let i = lb; i < n - lb; i++) {
          const p = closes[i];
          const left = closes.slice(i-lb, i), right = closes.slice(i+1, i+1+lb);
          if (left.every(x => p > x) && right.every(x => p > x)) swHi.push({ i, v: p });
          else if (left.every(x => p < x) && right.every(x => p < x)) swLo.push({ i, v: p });
        }
        for (let i = 0; i < n; i++) {
          const lastHi = swHi.filter(s => s.i < i);
          const lastLo = swLo.filter(s => s.i < i);
          if (cond === 'bullish_bos' && lastHi.length && closes[i] > lastHi[lastHi.length-1].v) arr[i] = true;
          else if (cond === 'bearish_bos' && lastLo.length && closes[i] < lastLo[lastLo.length-1].v) arr[i] = true;
          else if (cond === 'higher_low' && lastLo.length >= 2 && lastLo[lastLo.length-1].v > lastLo[lastLo.length-2].v) arr[i] = true;
          else if (cond === 'lower_high' && lastHi.length >= 2 && lastHi[lastHi.length-1].v < lastHi[lastHi.length-2].v) arr[i] = true;
        }
      } else if (type === 'SUPPORT_RESISTANCE') {
        const lb = ind.left_bars || 15;
        const rb = ind.right_bars || 15;
        const proxPct = ind.proximity_pct != null ? ind.proximity_pct : 1.0;
        const cond = ind.condition || 'price_crossing_down';
        const val = ind.value || 'resistance';
        const sr = calcSR(this.candles, lb, rb, ind.volume_threshold || 0, ind.min_touches || 1);
        const closes = this.candles.map(c => c.close);
        for (let i = 1; i < n; i++) {
          const c = closes[i], p = closes[i - 1];
          const res = sr.resSeries[i], sup = sr.supSeries[i];
          const lv = val === 'support' ? sup : res;
          if (lv === null) continue;
          if (cond === 'price_crossing_up' && p < lv && lv <= c) arr[i] = true;
          else if (cond === 'price_crossing_down' && p > lv && lv >= c) arr[i] = true;
          else if (cond === 'price_greater_than' && c > lv) arr[i] = true;
          else if (cond === 'price_lower_than' && c < lv) arr[i] = true;
          else if (cond === 'near_support' && sup !== null && Math.abs(c - sup) / sup * 100 <= proxPct) arr[i] = true;
          else if (cond === 'near_resistance' && res !== null && Math.abs(c - res) / res * 100 <= proxPct) arr[i] = true;
          else if (cond === 'below_support' && sup !== null && c < sup) arr[i] = true;
          else if (cond === 'above_resistance' && res !== null && c > res) arr[i] = true;
        }
      } else if (type === 'QFL') {
        const cond = ind.condition || 'below_base';
        const qfl = calcQFL(this.candles, ind.base_periods || 36, ind.pump_periods || 8,
          ind.pump_from_base_pct || 3.0, ind.base_crack_pct || 3.0);
        const closes = this.candles.map(c => c.close);
        for (let i = 0; i < n; i++) {
          const base = qfl.baseSeries[i];
          const bl = qfl.buyLimitSeries[i];
          if (cond === 'below_base' && bl !== null) arr[i] = true;
          else if (cond === 'near_base' && base !== null && Math.abs(closes[i] - base) / base < 0.005) arr[i] = true;
          else if (cond === 'base_retest' && base !== null && closes[i] >= base * 0.998 && closes[i] <= base * 1.002) arr[i] = true;
        }
      } else {
        for (let i = 0; i < n; i++) arr[i] = true;
      }
      gArrays.push(arr);
    }
    const groupAnd = new Array(n).fill(false);
    for (let i = 0; i < n; i++) groupAnd[i] = gArrays.every(a => a[i]);
    groupSignals.push(groupAnd);
    }
    const combined = new Array(n).fill(false);
    for (let i = 0; i < n; i++) combined[i] = groupSignals.some(g => g[i]);
    this._groupSignals = groupSignals;
    this._groupMeta = (groups.length ? groups : flatInds.length ? [{ id: 1, name: 'Group 1', indicators: flatInds }] : []);
    return [combined];
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
        const closed = this._checkTp(this.open_deal, candle, i)
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
        if (entry && this._entryFilter) {
          const ef = this._entryFilter;
          const ed = new Date(candle.time * 1000);
          const eDay  = this._entryFilterUtc ? ed.getUTCDay()   : ed.getDay();
          const eHour = this._entryFilterUtc ? ed.getUTCHours() : ed.getHours();
          if (ef.days && !ef.days.includes(eDay)) entry = false;
          if (ef.hours && !ef.hours.includes(eHour)) entry = false;
        }
        if (entry) {
          _btStatEntrySignalTrue++;
          if (hadOpenDealAtStart) _btStatSameCandleReopens++;
          this._lastEntryTrigger = { group_name: 'Entry', indicators: [] };
          if (this._groupSignals && this._groupMeta) {
            const sigIdx = i - 1;
            for (let gi = 0; gi < this._groupSignals.length; gi++) {
              if (this._groupSignals[gi][sigIdx]) {
                const gm = this._groupMeta[gi];
                this._lastEntryTrigger = {
                  group_name: gm?.name || `Group ${gi + 1}`,
                  indicators: (gm?.indicators || []).map(x => x.type),
                };
                break;
              }
            }
          }
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
    const avgDcaOrders = deals.length
      ? deals.reduce((s, d) => s + (d.dca_count || 0), 0) / deals.length
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
      sharpe = sd > 1e-6 ? ((mean / sd) * Math.sqrt(252)).toFixed(2) : '—';
      const losses = pctReturns.filter(v => v < 0);
      if (losses.length === 0) {
        sortino = '∞';
      } else {
        const lMean = 0;
        const lVar = losses.reduce((s, v) => s + (v - lMean) * (v - lMean), 0) / losses.length;
        const lSd = Math.sqrt(lVar);
        sortino = lSd > 1e-6 ? ((mean / lSd) * Math.sqrt(252)).toFixed(2) : '∞';
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

    // DCA usage breakdown:
    //  (A) per-level trigger counts — hoe vaak werd DCA #1 gebruikt,
    //      DCA #2, etc. Order binnen deal.orders (sorted op tijd)
    //      bepaalt de level-sequentie.
    //  (B) per-deal depth histogram — verdeling van deals over
    //      dca_count (0 = geen DCA gedaan, N = N DCA-fills).
    const dcaLevelCounts = new Map();
    for (const d of deals) {
      let level = 0;
      for (const o of d.orders) {
        if (o.type !== 'dca') continue;
        level += 1;
        dcaLevelCounts.set(level, (dcaLevelCounts.get(level) || 0) + 1);
      }
    }
    const totalDcaTriggers = Array.from(dcaLevelCounts.values())
      .reduce((s, c) => s + c, 0);
    const dcaLevelBreakdown = Array.from(dcaLevelCounts.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([level, count]) => ({
        level,
        count,
        pct: totalDcaTriggers > 0 ? (count / totalDcaTriggers) * 100 : 0,
      }));

    const dcaDepthMap = new Map();
    for (const d of deals) {
      const depth = d.dca_count || 0;
      dcaDepthMap.set(depth, (dcaDepthMap.get(depth) || 0) + 1);
    }
    const totalDeals = deals.length;
    const dcaDepthHistogram = Array.from(dcaDepthMap.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([depth, count]) => ({
        depth,
        count,
        pct: totalDeals > 0 ? (count / totalDeals) * 100 : 0,
      }));

    return {
      summary: {
        total_pnl_btc: totalPnlBtc,
        total_pnl_pct: totalPnlPct,
        win_rate: winRate,
        total_deals: deals.length,
        wins, losses,
        avg_duration_hours: avgDurationHours,
        max_duration_hours: maxDurationHours,
        avg_dca_orders: avgDcaOrders,
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
      dca_level_breakdown: dcaLevelBreakdown,
      dca_depth_histogram: dcaDepthHistogram,
      total_dca_triggers: totalDcaTriggers,
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

// Human-readable duration for a deal lifespan expressed in hours.
// Under 24h: "Xh Ym"; 24h or more: "Xd Yh Zm" so a 36.033h deal
// reads as "1d 12h 2m" instead of "36h". Shared by the summary
// cards (avg/max) and the deals-list Duration column.
function btFormatDuration(hours) {
  if (hours == null || !Number.isFinite(hours) || hours < 0) return '—';
  if (hours < 24) {
    const h = Math.floor(hours);
    const m = Math.round((hours - h) * 60);
    return `${h}h ${m}m`;
  }
  const days = Math.floor(hours / 24);
  const remH = Math.floor(hours % 24);
  const mins = Math.round((hours % 1) * 60);
  return `${days}d ${remH}h ${mins}m`;
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
  const data = await r.json();
  if (Array.isArray(data)) return data;
  if (data.gaps && data.gaps > 0) {
    console.warn(`[CANDLES] ${data.gaps} data gap(s) detected in candle data`);
  }
  return data.candles || data;
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

// ── Parameter sweep ──────────────────────────────────────────────────────────

function _swCountIter(min, max, step) {
  if (step <= 0 || max < min) return 0;
  return Math.max(0, Math.floor((max - min) / step) + 1);
}

function swUpdateEstimate() {
  let total = 0;
  if ($('sw-tp-enabled') && $('sw-tp-enabled').checked) {
    const n = _swCountIter(
      parseFloat($('sw-tp-min').value), parseFloat($('sw-tp-max').value),
      parseFloat($('sw-tp-step').value));
    total += n;
    const p = $('sw-tp-preview'); if (p) p.textContent = `${n} iterations`;
  } else { const p = $('sw-tp-preview'); if (p) p.textContent = ''; }
  if ($('sw-sl-enabled') && $('sw-sl-enabled').checked) {
    const n = _swCountIter(
      parseFloat($('sw-sl-min').value), parseFloat($('sw-sl-max').value),
      parseFloat($('sw-sl-step').value));
    total += n;
    const p = $('sw-sl-preview'); if (p) p.textContent = `${n} iterations`;
  } else { const p = $('sw-sl-preview'); if (p) p.textContent = ''; }
  if ($('sw-dca-enabled') && $('sw-dca-enabled').checked) {
    const n = _swCountIter(
      parseFloat($('sw-dca-min').value), parseFloat($('sw-dca-max').value),
      parseFloat($('sw-dca-step').value));
    total += n;
    const p = $('sw-dca-preview'); if (p) p.textContent = `${n} iterations`;
  } else { const p = $('sw-dca-preview'); if (p) p.textContent = ''; }
  // Schedule iterations
  let schedIter = 1;
  const daysOn = $('sw-sched-days-enabled') && $('sw-sched-days-enabled').checked;
  const hoursOn = $('sw-sched-hours-enabled') && $('sw-sched-hours-enabled').checked;
  if (daysOn) {
    const checked = document.querySelectorAll('.sw-day-chk:checked');
    const n = checked.length;
    schedIter *= Math.max(1, n);
    const names = Array.from(checked).map(c => _SW_DAY_NAMES[parseInt(c.value, 10)].slice(0, 3));
    const p = $('sw-sched-days-preview');
    if (p) p.textContent = `${n} iteration${n !== 1 ? 's' : ''} (${names.join(', ')})`;
  } else { const p = $('sw-sched-days-preview'); if (p) p.textContent = ''; }
  if (hoursOn) {
    const from = Math.max(0, Math.min(23, parseInt($('sw-hour-from').value, 10) || 0));
    const to   = Math.max(from, Math.min(23, parseInt($('sw-hour-to').value, 10) || 23));
    const n = to - from + 1;
    schedIter *= n;
    const p = $('sw-sched-hours-preview');
    if (p) p.textContent = `${n} iteration${n !== 1 ? 's' : ''} (${String(from).padStart(2,'0')}:00 - ${String(to).padStart(2,'0')}:00)`;
  } else { const p = $('sw-sched-hours-preview'); if (p) p.textContent = ''; }
  if (schedIter > 1) total = total > 0 ? total * schedIter : schedIter;
  if (total === 0) total = 1;
  const candles = btCandleCountForRange(
    $('bt-tf') && $('bt-tf').value || '1h',
    $('bt-start') && $('bt-start').value || '',
    $('bt-end') && $('bt-end').value || '',
  ) || 8760;
  const estSec = Math.round(total * candles / 50000);
  const est = $('sw-estimate');
  if (est) est.textContent = `Total iterations: ${total} · est. ~${btHumanDuration(estSec)}`;
  const warn = $('sw-warn');
  if (warn) {
    if (estSec > 300) {
      warn.textContent = `⚠ This sweep may take ${btHumanDuration(estSec)}`;
      warn.classList.remove('hidden');
    } else {
      warn.classList.add('hidden');
    }
  }
}

const _SW_DAY_NAMES = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];

function _swGenerateScheduleConfigs(baseCfg) {
  const configs = [];
  const useUtc = ($('sw-sched-tz') && $('sw-sched-tz').value) === 'UTC';
  const daysOn  = $('sw-sched-days-enabled')  && $('sw-sched-days-enabled').checked;
  const hoursOn = $('sw-sched-hours-enabled') && $('sw-sched-hours-enabled').checked;

  const daysSets = [];
  if (daysOn) {
    document.querySelectorAll('.sw-day-chk:checked').forEach(c => {
      const d = parseInt(c.value, 10);
      daysSets.push({ days: [d], label: _SW_DAY_NAMES[d].slice(0, 3) });
    });
    if (!daysSets.length) daysSets.push({ days: null, label: null });
  } else {
    daysSets.push({ days: null, label: null });
  }

  const hoursSets = [];
  if (hoursOn) {
    const from = Math.max(0, Math.min(23, parseInt($('sw-hour-from').value, 10) || 0));
    const to   = Math.max(from, Math.min(23, parseInt($('sw-hour-to').value, 10) || 23));
    for (let h = from; h <= to; h++) {
      hoursSets.push({ hours: [h], label: String(h).padStart(2, '0') + ':00' });
    }
  } else {
    hoursSets.push({ hours: null, label: null });
  }

  for (const ds of daysSets) {
    for (const hs of hoursSets) {
      const parts = [ds.label, hs.label].filter(Boolean);
      const label = parts.length ? parts.join(' ') : 'Base config';
      const c = JSON.parse(JSON.stringify(baseCfg));
      const ef = {};
      if (ds.days) ef.days = ds.days;
      if (hs.hours) ef.hours = hs.hours;
      if (ef.days || ef.hours) {
        c.entryFilter = ef;
        c.entryFilterUtc = useUtc;
      }
      configs.push({ label, config: c });
    }
  }
  return configs;
}

function _swGenerateConfigs(baseCfg) {
  const configs = [];
  if ($('sw-tp-enabled') && $('sw-tp-enabled').checked) {
    const min = parseFloat($('sw-tp-min').value);
    const max = parseFloat($('sw-tp-max').value);
    const step = parseFloat($('sw-tp-step').value);
    for (let v = min; v <= max + 1e-9; v += step) {
      const c = JSON.parse(JSON.stringify(baseCfg));
      c.take_profit.target_pct = Math.round(v * 100) / 100;
      configs.push({ label: `TP: ${c.take_profit.target_pct}%`, config: c });
    }
  }
  if ($('sw-sl-enabled') && $('sw-sl-enabled').checked) {
    const min = parseFloat($('sw-sl-min').value);
    const max = parseFloat($('sw-sl-max').value);
    const step = parseFloat($('sw-sl-step').value);
    const slType = $('sw-sl-type').value;
    for (let v = min; v <= max + 1e-9; v += step) {
      const c = JSON.parse(JSON.stringify(baseCfg));
      c.stop_loss.type = slType;
      c.stop_loss.pct = Math.round(v * 100) / 100;
      configs.push({ label: `SL: ${c.stop_loss.pct}`, config: c });
    }
  }
  if ($('sw-dca-enabled') && $('sw-dca-enabled').checked) {
    const param = $('sw-dca-param').value;
    const min = parseFloat($('sw-dca-min').value);
    const max = parseFloat($('sw-dca-max').value);
    const step = param === 'max_orders' ? 1 : parseFloat($('sw-dca-step').value);
    const labelMap = {
      order_spacing_pct: 'Spacing', multiplier: 'VolScale',
      step_scale: 'StepScale', max_orders: 'MaxDCA',
    };
    for (let v = min; v <= max + 1e-9; v += step) {
      const c = JSON.parse(JSON.stringify(baseCfg));
      const val = param === 'max_orders' ? Math.round(v) : Math.round(v * 100) / 100;
      c.dca[param] = val;
      if (param === 'max_orders') c.dca.max_orders = val + 1;
      configs.push({ label: `${labelMap[param] || param}: ${val}`, config: c });
    }
  }
  // Schedule sweep: if enabled, cross-product param configs × schedule configs.
  // If no param sweep is active, schedule configs run against the base config.
  const schedEnabled = ($('sw-sched-days-enabled') && $('sw-sched-days-enabled').checked)
                    || ($('sw-sched-hours-enabled') && $('sw-sched-hours-enabled').checked);
  if (schedEnabled) {
    const schedConfigs = _swGenerateScheduleConfigs(baseCfg);
    if (configs.length) {
      const cross = [];
      for (const pc of configs) {
        for (const sc of schedConfigs) {
          const merged = JSON.parse(JSON.stringify(pc.config));
          if (sc.config.entryFilter) {
            merged.entryFilter = sc.config.entryFilter;
            merged.entryFilterUtc = sc.config.entryFilterUtc;
          }
          cross.push({ label: `${pc.label} | ${sc.label}`, config: merged });
        }
      }
      return cross;
    }
    return schedConfigs;
  }
  if (!configs.length) {
    configs.push({ label: 'Base config', config: JSON.parse(JSON.stringify(baseCfg)) });
  }
  return configs;
}

function swOpenModal() {
  $('sw-results').classList.add('hidden');
  $('sw-loader').classList.add('hidden');
  swUpdateEstimate();
  $('sweep-modal').classList.add('show');
}

const SW_RESULT_COLS = [
  { key: 'label',         label: 'Parameter',  fmt: v => safeText(String(v)) },
  { key: 'candles_used',  label: 'Candles',    fmt: v => v != null ? v.toLocaleString() : '—' },
  { key: 'total_deals',   label: 'Deals',      fmt: v => String(v ?? 0) },
  { key: 'win_rate',      label: 'Win %',      fmt: v => v != null ? v.toFixed(1) + '%' : '—' },
  { key: 'total_pnl_btc', label: 'PnL BTC',    fmt: v => _btColouredBtc(v) },
  { key: 'total_pnl_pct', label: 'PnL %',      fmt: v => _btColouredPct(v) },
  { key: 'profit_factor', label: 'PF',          fmt: v => _fmtRatio(v) },
  { key: 'sharpe',        label: 'Sharpe',      fmt: v => v != null && v !== '—' ? String(v) : '—' },
  { key: 'max_dd',        label: 'Max DD',      fmt: v => v != null ? v.toFixed(2) + '%' : '—' },
  { key: 'avg_dur',       label: 'Avg Dur',     fmt: v => btFormatDuration(v) },
];
let _swStopped = false;
let _swIsRunning = false;
let _swSortKey = 'profit_factor';
let _swSortDir = 'desc';
let _swRows = [];

async function swRunSweep() {
  if (_swIsRunning) return;
  _swIsRunning = true;
  const startStr = $('bt-start').value;
  const endStr   = $('bt-end').value;
  const tf       = $('bt-tf').value;
  const balance  = parseFloat($('bt-balance').value);
  if (!startStr || !endStr) { alert('Pick start and end dates'); return; }

  let cfg = null;
  if (_detailConfigCache && _detailConfigCache.bot) cfg = JSON.parse(JSON.stringify(_detailConfigCache.bot));
  if (!cfg && currentSlug) {
    try {
      const r = await fetch(`/api/bots/${currentSlug}/config`);
      if (r.ok) { const j = await r.json(); cfg = JSON.parse(JSON.stringify(j.bot || j)); }
    } catch (e) {}
  }
  if (!cfg) { alert('No bot config available'); return; }

  const configs = _swGenerateConfigs(cfg);
  const pair = cfg.pair || 'BTC/USD';
  const startTimeStr = ($('bt-start-time') && $('bt-start-time').value) || '00:00';
  const endTimeStr   = ($('bt-end-time')   && $('bt-end-time').value)   || '23:59';
  const startIso = _btComposeIso(startStr, startTimeStr, '00:00');
  const endIso   = _btComposeIso(endStr,   endTimeStr,   '23:59');
  const limit    = btCandleCountForRange(tf, startStr, endStr) || btDefaultLimit(tf);

  $('sw-results').classList.add('hidden');
  $('sw-loader').classList.remove('hidden');
  $('sw-progress-bar').style.width = '0%';
  $('sw-status').textContent = 'Fetching candles…';
  _swStopped = false;
  $('sw-run-btn').classList.add('hidden');
  $('sw-stop-btn').classList.remove('hidden');

  const t0 = performance.now();
  let candles;
  try {
    candles = await btFetchCandles(pair, tf, startIso, endIso, limit);
  } catch (e) {
    $('sw-status').textContent = 'Failed to fetch candles: ' + (e.message || e);
    $('sw-run-btn').classList.remove('hidden'); $('sw-stop-btn').classList.add('hidden');
    _swIsRunning = false; return;
  }
  if (!candles || candles.length < 50) {
    $('sw-status').textContent = `Not enough candles (${candles ? candles.length : 0})`;
    $('sw-run-btn').classList.remove('hidden'); $('sw-stop-btn').classList.add('hidden');
    _swIsRunning = false; return;
  }

  _swRows = [];
  for (let i = 0; i < configs.length; i++) {
    if (_swStopped) {
      $('sw-status').textContent = `Sweep stopped at iteration ${i}/${configs.length}`;
      break;
    }
    const { label, config: c } = configs[i];
    const pct = Math.round(((i + 1) / configs.length) * 100);
    $('sw-progress-bar').style.width = pct + '%';
    $('sw-status').textContent = `Running iteration ${i + 1}/${configs.length} (${label})…`;
    const engine = new RevertoBacktest(c, candles);
    const result = await engine.run(balance);
    const s = result.summary;
    const r = result.ratios;
    // Diagnostic: gross win/loss breakdown for sweep analysis
    const deals = result.deals || [];
    const winDeals = deals.filter(d => d.pnl_btc > 0);
    const lossDeals = deals.filter(d => d.pnl_btc < 0);
    const grossWinSum = winDeals.reduce((a, d) => a + d.pnl_btc, 0);
    const grossLossSum = Math.abs(lossDeals.reduce((a, d) => a + d.pnl_btc, 0));
    const avgWinSize = winDeals.length ? winDeals.reduce((a, d) => a + d.total_size, 0) / winDeals.length : 0;
    const avgLossSize = lossDeals.length ? lossDeals.reduce((a, d) => a + d.total_size, 0) / lossDeals.length : 0;
    if (window._BT_DEBUG) console.log(`[SWEEP] ${label} | deals=${s.total_deals} win=${winDeals.length} loss=${lossDeals.length} | ` +
      `winRate=${s.win_rate.toFixed(1)}% | PnL=${s.total_pnl_btc.toFixed(8)} | ` +
      `grossWin=${grossWinSum.toFixed(8)} grossLoss=${grossLossSum.toFixed(8)} | ` +
      `PF=${typeof r.profit_factor === 'number' ? r.profit_factor.toFixed(3) : r.profit_factor} | ` +
      `avgWinSize=${avgWinSize.toFixed(6)} avgLossSize=${avgLossSize.toFixed(6)} | ` +
      `fees=${s.total_fees_btc.toFixed(8)} maxDD=${s.max_drawdown_pct.toFixed(2)}%`);
    _swRows.push({
      label,
      config: c,
      total_deals:   s.total_deals,
      win_rate:      s.win_rate,
      total_pnl_btc: s.total_pnl_btc,
      total_pnl_pct: s.total_pnl_pct,
      profit_factor: typeof r.profit_factor === 'number' ? r.profit_factor : null,
      sharpe:        r.sharpe,
      max_dd:        s.max_drawdown_pct,
      avg_dur:       s.avg_duration_hours,
      candles_used:  candles.length,
      _result:       result,
    });
    await new Promise(r => setTimeout(r, 0));
  }

  const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
  $('sw-loader').classList.add('hidden');
  $('sw-run-btn').classList.remove('hidden');
  $('sw-stop-btn').classList.add('hidden');
  _swStopped = false;
  _swIsRunning = false;
  $('sw-results-header').textContent =
    `Sweep complete — ${configs.length} iterations in ${elapsed}s`;
  $('sw-results').classList.remove('hidden');
  // Reset to Table tab active
  document.querySelectorAll('.sweep-result-tabs .sweep-tab').forEach(t => t.classList.remove('active'));
  const tableTab = document.querySelector('.sweep-result-tabs .sweep-tab[data-sw-view="table"]');
  if (tableTab) tableTab.classList.add('active');
  const swTbl = $('sw-view-table'); if (swTbl) swTbl.classList.remove('hidden');
  const swCht = $('sw-view-chart'); if (swCht) swCht.classList.add('hidden');
  _swRenderResultsTable();
  _swRenderChart();

  // Auto-save the best run (highest PF, or highest PnL if PF is Infinity)
  if (_swRows.length) {
    const best = _swRows.slice().sort((a, b) => {
      const apf = a.profit_factor != null && isFinite(a.profit_factor) ? a.profit_factor : -1;
      const bpf = b.profit_factor != null && isFinite(b.profit_factor) ? b.profit_factor : -1;
      if (apf !== bpf) return bpf - apf;
      return (b.total_pnl_btc || 0) - (a.total_pnl_btc || 0);
    })[0];
    if (best && best._result) {
      const summary = _btFlattenForSave(best._result);
      summary.sweep_param = best.label;
      summary.source = 'sweep';
      _btSaveRun(
        { ...cfg, slug: (cfg && cfg.slug) || currentSlug || '' },
        { start_date: startIso, end_date: endIso, timeframe: tf, initial_balance_btc: balance },
        { summary: best._result.summary, ratios: best._result.ratios },
      ).then(id => { if (id != null) $('sw-status').textContent = `✓ Best run saved (${best.label})`; });
    }
  }
}

function _swRenderResultsTable() {
  const head = $('sw-results-head');
  head.innerHTML = SW_RESULT_COLS.map(col => {
    const dir = col.key === _swSortKey
      ? `<span class="bt-sort-dir">${_swSortDir === 'asc' ? '▲' : '▼'}</span>` : '';
    return `<th data-key="${col.key}">${safeText(col.label)}${dir}</th>`;
  }).join('') + '<th></th>';
  head.querySelectorAll('th[data-key]').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.key;
      if (_swSortKey === k) _swSortDir = _swSortDir === 'asc' ? 'desc' : 'asc';
      else { _swSortKey = k; _swSortDir = 'desc'; }
      _swRenderResultsTable();
    });
  });

  const sorted = _swRows.slice().sort((a, b) => {
    const av = a[_swSortKey], bv = b[_swSortKey];
    let cmp = 0;
    if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv;
    else cmp = String(av ?? '').localeCompare(String(bv ?? ''));
    return _swSortDir === 'asc' ? cmp : -cmp;
  });

  let bestIdx = -1, worstIdx = -1;
  if (sorted.length >= 2) {
    let bestPf = -Infinity, worstPf = Infinity;
    sorted.forEach((r, i) => {
      const pf = r.profit_factor;
      if (pf != null && pf > bestPf) { bestPf = pf; bestIdx = i; }
      if (pf != null && pf < worstPf) { worstPf = pf; worstIdx = i; }
    });
  }

  const body = $('sw-results-body');
  body.innerHTML = sorted.map((row, i) => {
    const cls = i === bestIdx ? ' class="sw-best"' : (i === worstIdx ? ' class="sw-worst"' : '');
    const cells = SW_RESULT_COLS.map(col => `<td>${col.fmt(row[col.key])}</td>`).join('');
    return `<tr${cls} data-sw-idx="${i}">${cells}<td><button class="deal-btn deal-btn-edit sw-run-full" title="Run full backtest">▶</button></td></tr>`;
  }).join('');

  body.querySelectorAll('.sw-run-full').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const tr = btn.closest('tr');
      const idx = parseInt(tr.dataset.swIdx, 10);
      const row = sorted[idx];
      if (!row) return;
      $('sweep-modal').classList.remove('show');
      if (row._result && currentSlug) {
        _btResultsSet(currentSlug, row._result);
        btRestoreResultsForSlug(currentSlug);
        const resultsEl = $('bt-results');
        if (resultsEl) resultsEl.classList.remove('hidden');
      }
    });
  });
}

function _swRenderChart() {
  const area = $('sw-chart-area');
  if (!area || !_swRows.length) return;

  const dayRe = /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$/;
  const hourRe = /^\d{2}:00$/;
  const dayHourRe = /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{2}):00$/;
  const isDayOnly  = _swRows.every(r => dayRe.test(r.label));
  const isHourOnly = _swRows.every(r => hourRe.test(r.label));
  const isDayHour  = _swRows.length > 7 && _swRows.some(r => dayHourRe.test(r.label));

  const html = [];
  if (isDayHour) {
    html.push(_swAggBarH('Avg PnL BTC per day', _swRows, dayHourRe, 1));
    html.push(_swAggBarV('Avg PnL BTC per hour', _swRows, dayHourRe, 2));
  } else if (isDayOnly) {
    html.push(_swBarH('PnL BTC per day', _swRows));
  } else if (isHourOnly) {
    html.push(_swBarV('PnL BTC per hour', _swRows));
  } else {
    html.push(_swBarV('PnL BTC per iteration', _swRows));
    html.push(_swPfSection(_swRows));
  }
  area.innerHTML = html.join('');
}

function _swPfSection(rows) {
  const infCount = rows.filter(r => r.profit_factor === Infinity || r.profit_factor === null).length;
  const finiteRows = rows.filter(r => typeof r.profit_factor === 'number' && Number.isFinite(r.profit_factor));
  const total = rows.length;
  const title = 'Profit Factor per iteration';

  if (infCount === total) {
    return `<div class="sw-chart-row"><div class="sw-chart-title">${title}</div>` +
      `<div class="sweep-estimate">All ${total} iterations achieved 100% win rate — Profit Factor: ∞</div></div>`;
  }

  if (infCount / total > 0.75) {
    const finiteList = finiteRows.map(r => {
      const wins = Math.round((r.win_rate || 0) / 100 * (r.total_deals || 0));
      const losses = (r.total_deals || 0) - wins;
      return `<tr><td>${safeText(r.label)}</td><td>${_fmtRatio(r.profit_factor)}</td><td>${wins}W / ${losses}L</td><td>${(r.total_pnl_btc || 0).toFixed(8)}</td></tr>`;
    }).join('');
    return `<div class="sw-chart-row"><div class="sw-chart-title">${title}</div>` +
      `<div class="sweep-estimate">${infCount}/${total} iterations achieved 100% win rate (PF = ∞)</div>` +
      (finiteRows.length ? `<div class="sw-chart-title sw-mt8">Finite results</div>` +
        `<table class="bt-history-table sw-pf-table"><thead><tr><th>Parameter</th><th>PF</th><th>W/L</th><th>PnL BTC</th></tr></thead>` +
        `<tbody>${finiteList}</tbody></table>` : '') +
      `</div>`;
  }

  return _swBarV(title, rows, 'profit_factor');
}

function _swSafe(v, cap) {
  if (typeof v !== 'number') return 0;
  if (!Number.isFinite(v)) return cap != null ? cap : 0;
  return cap != null ? Math.min(v, cap) : v;
}

function _swLabelStep(n) {
  if (n > 80) return 10;
  if (n > 40) return 5;
  if (n > 20) return 4;
  if (n > 10) return 2;
  return 1;
}

function _swShortLabel(lbl) {
  return lbl.replace(/:00$/, '').replace(/%$/, '');
}

function _swTooltip(r) {
  const pnl = _swSafe(r.total_pnl_btc);
  const fv = (pnl >= 0 ? '+' : '') + pnl.toFixed(8) + ' BTC';
  const deals = r.total_deals ?? 0;
  const wr = r.win_rate != null ? r.win_rate.toFixed(1) + '%' : '—';
  return `${r.label} | PnL: ${fv} | Deals: ${deals} | Win: ${wr}`;
}

// SVG-based bar charts — CSP-safe because SVG rect width/height are
// element attributes, not inline styles, so no style-src violation.
const _SW_SVG_NS = 'http://www.w3.org/2000/svg';
const _SW_VBAR_H = 180;
const _SW_HBAR_ROW_H = 28;

function _swBestIdx(vals) {
  let bi = 0;
  for (let i = 1; i < vals.length; i++) if (vals[i] > vals[bi]) bi = i;
  return bi;
}

function _swBarV(title, rows, key = 'total_pnl_btc') {
  let cap = null;
  if (key === 'profit_factor') {
    const finiteVals = rows.map(r => r[key]).filter(v => typeof v === 'number' && Number.isFinite(v));
    const maxFinite = finiteVals.length ? Math.max(...finiteVals) : 1;
    cap = Math.max(maxFinite * 1.5, 2);
  }
  const vals = rows.map(r => _swSafe(r[key], cap));
  const best = _swBestIdx(vals);
  const n = rows.length;
  const gap = 2, barW = Math.max(4, Math.floor((600 - gap * n) / n));
  const svgW = n * (barW + gap);
  const step = _swLabelStep(n);
  const lblH = 14;
  const maxPos = Math.max(0, ...vals);
  const maxNeg = Math.max(0, ...vals.map(v => Math.abs(Math.min(0, v))));
  const hasNeg = maxNeg > 0;
  const totalRange = Math.max(1e-12, maxPos + maxNeg);
  const baseY = hasNeg ? Math.round(maxPos / totalRange * _SW_VBAR_H) : _SW_VBAR_H;
  let rects = '', labels = '', baseline = '';
  if (hasNeg) {
    baseline = `<line x1="0" y1="${baseY}" x2="${svgW}" y2="${baseY}" class="sw-svg-baseline"/>`;
  }
  rows.forEach((r, i) => {
    const v = vals[i];
    const h = Math.max(2, Math.round(Math.abs(v) / totalRange * _SW_VBAR_H));
    const x = i * (barW + gap);
    const y = v >= 0 ? baseY - h : baseY;
    const cls = i === best ? 'sw-svg-best-bar' : (v < 0 ? 'sw-svg-neg-bar' : 'sw-svg-bar');
    rects += `<rect x="${x}" y="${y}" width="${barW}" height="${h}" class="${cls}" rx="2"><title>${safeText(_swTooltip(r))}</title></rect>`;
    if (i % step === 0) {
      labels += `<text x="${x + barW / 2}" y="${_SW_VBAR_H + lblH}" text-anchor="middle" class="sw-svg-label">${safeText(_swShortLabel(r.label))}</text>`;
    }
  });
  return `<div class="sw-chart-row"><div class="sw-chart-title">${safeText(title)}</div>` +
    `<svg class="sw-svg-chart" viewBox="0 0 ${svgW} ${_SW_VBAR_H + lblH + 4}" preserveAspectRatio="none" xmlns="${_SW_SVG_NS}">` +
    `${baseline}${rects}${labels}</svg></div>`;
}

function _swBarH(title, rows) {
  const vals = rows.map(r => _swSafe(r.total_pnl_btc));
  const mx = Math.max(1e-12, ...vals.map(v => Math.abs(v)));
  const best = _swBestIdx(vals);
  const n = rows.length;
  const rowH = _SW_HBAR_ROW_H, gap = 3, labelW = 40, valW = 120, barArea = 400;
  const svgH = n * (rowH + gap);
  let elems = '';
  rows.forEach((r, i) => {
    const v = vals[i];
    const w = Math.max(2, Math.round(Math.abs(v) / mx * barArea));
    const y = i * (rowH + gap);
    const cls = i === best ? 'sw-svg-best-bar' : 'sw-svg-bar';
    const fv = (v >= 0 ? '+' : '') + v.toFixed(8);
    elems += `<text x="${labelW - 4}" y="${y + rowH / 2 + 4}" text-anchor="end" class="sw-svg-label">${safeText(_swShortLabel(r.label))}</text>`;
    elems += `<rect x="${labelW}" y="${y}" width="${w}" height="${rowH}" class="${cls}" rx="3"><title>${safeText(_swTooltip(r))}</title></rect>`;
    elems += `<text x="${labelW + w + 6}" y="${y + rowH / 2 + 4}" class="sw-svg-value">${fv}</text>`;
  });
  return `<div class="sw-chart-row"><div class="sw-chart-title">${safeText(title)}</div>` +
    `<svg class="sw-svg-chart sw-svg-hchart" viewBox="0 0 ${labelW + barArea + valW} ${svgH}" xmlns="${_SW_SVG_NS}">` +
    `${elems}</svg></div>`;
}

function _swAggBarH(title, rows, re, grpIdx) {
  const agg = {};
  const dayOrder = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  for (const r of rows) {
    const m = r.label.match(re); if (!m) continue;
    const k = m[grpIdx];
    if (!agg[k]) agg[k] = { sum: 0, n: 0 };
    agg[k].sum += _swSafe(r.total_pnl_btc); agg[k].n++;
  }
  const sorted = dayOrder.filter(d => agg[d]);
  const aggRows = sorted.map(k => ({ label: k, total_pnl_btc: agg[k].sum / agg[k].n }));
  if (window._BT_DEBUG) console.log('[SW_CHART] _swAggBarH', title, { groups: sorted, aggRows });
  return _swBarH(title, aggRows);
}

function _swAggBarV(title, rows, re, grpIdx) {
  const agg = {};
  for (const r of rows) {
    const m = r.label.match(re); if (!m) continue;
    const k = String(parseInt(m[grpIdx], 10)).padStart(2, '0') + ':00';
    if (!agg[k]) agg[k] = { sum: 0, n: 0 };
    agg[k].sum += _swSafe(r.total_pnl_btc); agg[k].n++;
  }
  const sorted = Object.keys(agg).sort();
  const aggRows = sorted.map(k => ({ label: k, total_pnl_btc: agg[k].sum / agg[k].n }));
  if (window._BT_DEBUG) console.log('[SW_CHART] _swAggBarV', title, { groups: sorted.length, sample: aggRows.slice(0, 3) });
  return _swBarV(title, aggRows);
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
// Capped at 20 entries via LRU eviction tracked in _btResultsOrder.
const _btResultsBySlug = {};
const _btResultsOrder = [];
const _BT_RESULTS_MAX = 20;

function _btResultsSet(slug, result) {
  if (_btResultsBySlug[slug]) {
    const idx = _btResultsOrder.indexOf(slug);
    if (idx !== -1) _btResultsOrder.splice(idx, 1);
  }
  _btResultsBySlug[slug] = result;
  _btResultsOrder.push(slug);
  while (_btResultsOrder.length > _BT_RESULTS_MAX) {
    const old = _btResultsOrder.shift();
    delete _btResultsBySlug[old];
  }
}

// Backtest History sub-view state — an in-memory sort handle plus
// the last-fetched rows, so clicking a header just re-renders from
// cache instead of hitting the network again.
const BT_HISTORY_COLUMNS = [
  { key: 'bot_name',            label: 'Bot',        fmt: v => safeText(String(v || '—')),                      on: true },
  { key: 'created_at',          label: 'Run',        fmt: v => safeText((v || '').slice(0, 16).replace('T', ' ')), on: true },
  { key: 'start_date',          label: 'Start',      fmt: v => safeText((v || '').slice(0, 10)),                on: true },
  { key: 'end_date',            label: 'End',        fmt: v => safeText((v || '').slice(0, 10)),                on: true },
  { key: 'timeframe',           label: 'TF',         fmt: v => safeText(String(v || '—')),                      on: true },
  { key: 'total_deals',         label: 'Deals',      fmt: v => String(v ?? 0),                                  on: true },
  { key: 'win_rate',            label: 'Win %',      fmt: v => (v != null ? v.toFixed(1) + '%' : '—'),          on: true },
  { key: 'total_pnl_btc',       label: 'PnL BTC',    fmt: v => _btColouredBtc(v),                               on: true },
  { key: 'total_pnl_pct',       label: 'PnL %',      fmt: v => _btColouredPct(v),                               on: true },
  { key: 'profit_factor',       label: 'PF',         fmt: v => _fmtRatio(v),                                    on: true },
  { key: 'sharpe_ratio',        label: 'Sharpe',     fmt: v => _fmtRatio(v),                                    on: true },
  { key: 'sortino_ratio',       label: 'Sortino',    fmt: v => _fmtRatio(v),                                    on: false },
  { key: 'calmar_ratio',        label: 'Calmar',     fmt: v => _fmtRatio(v),                                    on: false },
  { key: 'recovery_factor',     label: 'Recovery',   fmt: v => _fmtRatio(v),                                    on: false },
  { key: 'expectancy_btc',      label: 'Expect BTC', fmt: v => (v != null ? v.toFixed(8) : '—'),                on: false },
  { key: 'avg_win_loss_ratio',  label: 'Avg W/L',    fmt: v => _fmtRatio(v),                                    on: false },
  { key: 'omega_ratio',         label: 'Omega',      fmt: v => _fmtRatio(v),                                    on: false },
  { key: 'max_drawdown_pct',    label: 'Max DD',     fmt: v => (v != null ? v.toFixed(2) + '%' : '—'),          on: true },
  { key: 'avg_duration_hours',  label: 'Avg Dur',    fmt: v => btFormatDuration(v),                             on: false },
  { key: 'total_fees_btc',      label: 'Fees BTC',   fmt: v => (v != null ? v.toFixed(8) : '—'),                on: false },
  { key: 'buy_hold_pnl_pct',    label: 'B&H %',      fmt: v => _btColouredPct(v),                               on: true },
];
const _BT_HIST_COL_LS = 'reverto.bt_history_cols';

function _btHistVisibleCols() {
  try {
    const raw = localStorage.getItem(_BT_HIST_COL_LS);
    if (raw) { const saved = JSON.parse(raw); return BT_HISTORY_COLUMNS.filter(c => saved.includes(c.key)); }
  } catch (e) {}
  return BT_HISTORY_COLUMNS.filter(c => c.on);
}

function _btHistSaveColPref(keys) {
  try { localStorage.setItem(_BT_HIST_COL_LS, JSON.stringify(keys)); } catch (e) {}
}

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

async function btLoadHistory() {
  const body = $('bt-history-body');
  if (!body) return;
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

function _btRenderHistoryTable() {
  const visCols = _btHistVisibleCols();
  const colSpan = Math.max(1, visCols.length + 1);

  // Gear toggle for column visibility
  const gearEl = $('bt-history-gear-menu');
  if (gearEl) {
    const allOn = visCols.length === BT_HISTORY_COLUMNS.length;
    const toggleLabel = allOn ? 'Deselect All' : 'Select All';
    const checks = BT_HISTORY_COLUMNS.map(col => {
      const checked = visCols.some(v => v.key === col.key) ? ' checked' : '';
      return `<label><input type="checkbox" data-hist-col="${col.key}"${checked}> ${safeText(col.label)}</label>`;
    }).join('');
    gearEl.innerHTML =
      `<div class="bt-hist-col-actions"><button id="bt-hist-toggle-all">${toggleLabel}</button></div>` +
      `<div class="bt-hist-col-grid">${checks}</div>`;
    const saveAndRerender = () => {
      const keys = [];
      gearEl.querySelectorAll('input[data-hist-col]:checked').forEach(c => keys.push(c.dataset.histCol));
      _btHistSaveColPref(keys);
      _btRenderHistoryTable();
    };
    gearEl.querySelectorAll('input[data-hist-col]').forEach(chk => {
      chk.addEventListener('change', saveAndRerender);
    });
    const toggleBtn = $('bt-hist-toggle-all');
    if (toggleBtn) toggleBtn.addEventListener('click', () => {
      const nowAll = gearEl.querySelectorAll('input[data-hist-col]:checked').length === BT_HISTORY_COLUMNS.length;
      gearEl.querySelectorAll('input[data-hist-col]').forEach(c => { c.checked = !nowAll; });
      saveAndRerender();
    });
  }

  const head = $('bt-history-head');
  head.innerHTML = `<th><input type="checkbox" id="bt-hist-sel-all"></th>` +
    visCols.map(col => {
      const dir = col.key === _btHistorySortKey
        ? `<span class="bt-sort-dir">${_btHistorySortDir === 'asc' ? '▲' : '▼'}</span>` : '';
      return `<th data-key="${col.key}">${safeText(col.label)}${dir}</th>`;
    }).join('');
  head.querySelectorAll('th[data-key]').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.key;
      if (_btHistorySortKey === k) _btHistorySortDir = _btHistorySortDir === 'asc' ? 'desc' : 'asc';
      else { _btHistorySortKey = k; _btHistorySortDir = 'desc'; }
      _btRenderHistoryTable();
    });
  });

  const sorted = _btHistoryRows.slice().sort((a, b) => {
    const av = a[_btHistorySortKey], bv = b[_btHistorySortKey];
    let cmp;
    if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv;
    else cmp = String(av ?? '').localeCompare(String(bv ?? ''));
    return _btHistorySortDir === 'asc' ? cmp : -cmp;
  });

  const body = $('bt-history-body');
  if (!sorted.length) {
    body.innerHTML = `<tr><td colspan="${colSpan}" class="empty-config-msg">No backtest runs yet.</td></tr>`;
    return;
  }
  body.innerHTML = sorted.map(run => {
    const chk = `<td><input type="checkbox" class="bt-hist-chk" data-run-id="${run.id}"></td>`;
    const cells = visCols.map(col => `<td>${col.fmt(run[col.key])}</td>`).join('');
    return `<tr data-slug="${safeText(run.bot_slug || '')}">${chk}${cells}</tr>`;
  }).join('');
  body.querySelectorAll('tr[data-slug]').forEach(tr => {
    tr.addEventListener('click', (e) => {
      if (e.target.closest('.bt-hist-chk') || e.target.classList.contains('bt-hist-chk')) return;
      const slug = tr.dataset.slug;
      if (!slug) return;
      openBot(slug);
      setTimeout(() => {
        const tabBtn = document.querySelector('.detail-subnav .tab[data-dtab="backtest"]');
        if (tabBtn) showDTab('backtest', tabBtn);
      }, 60);
    });
  });
  // Select-all checkbox + bulk delete button
  const _updateBulkBtn = () => {
    const checked = body.querySelectorAll('.bt-hist-chk:checked');
    const btn = $('bt-hist-bulk-del');
    if (btn) {
      btn.textContent = `Delete selected (${checked.length})`;
      btn.classList.toggle('hidden', checked.length === 0);
    }
  };
  const selAll = $('bt-hist-sel-all');
  if (selAll) selAll.addEventListener('change', () => {
    body.querySelectorAll('.bt-hist-chk').forEach(c => { c.checked = selAll.checked; });
    _updateBulkBtn();
  });
  body.querySelectorAll('.bt-hist-chk').forEach(c => c.addEventListener('change', _updateBulkBtn));
  const bulkBtn = $('bt-hist-bulk-del');
  if (bulkBtn) bulkBtn.addEventListener('click', async () => {
    const ids = Array.from(body.querySelectorAll('.bt-hist-chk:checked')).map(c => c.dataset.runId);
    if (!ids.length) return;
    if (!confirm(`Delete ${ids.length} backtest run${ids.length > 1 ? 's' : ''}?`)) return;
    let ok = 0;
    for (const id of ids) {
      try {
        const r = await fetch(`/api/backtest/runs/${id}`, { method: 'DELETE' });
        if (r.ok) ok++;
      } catch (e) {}
    }
    _btHistoryRows = _btHistoryRows.filter(r => !ids.includes(String(r.id)));
    _btRenderHistoryTable();
    _dealToast(`Deleted ${ok} backtest run${ok > 1 ? 's' : ''}`);
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
    ['bt-sec-returns', 'bt-sec-activity', 'bt-sec-risk', 'bt-sec-ratios'].forEach(id => {
      const el = $(id);
      if (el) el.innerHTML = '';
    });
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
  const slug = cfg.slug || params.slug || currentSlug
    || (cfg.name || 'backtest').toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 64)
    || 'backtest';
  try {
    const r = await fetch('/api/backtest/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        slug,
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
    const tfSec = { '15m': 900, '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400, '12h': 43200, '1d': 86400 }[tf] || 3600;
    let gapCount = 0;
    for (let ci = 1; ci < candles.length; ci++) {
      if (candles[ci].time - candles[ci - 1].time > tfSec * 2) gapCount++;
    }
    status.textContent = `Fetched ${candles.length.toLocaleString()} candles${gapCount ? ` (${gapCount} gap${gapCount > 1 ? 's' : ''})` : ''}, starting simulation…`;
    const engine = new RevertoBacktest(cfg, candles);
    _btLastCandles = candles;
    _btLastConfig = cfg;
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
      if (currentSlug) _btResultsSet(currentSlug, result);
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

const BT_CARD_TIPS = {
  'Buy & hold':
    'What you would have earned by simply buying BTC at the start of the backtest period and holding until the end, without any active trading. Use this as a benchmark\u2009—\u2009if your bot underperforms buy & hold, a passive strategy may be better.',
  'Max drawdown':
    'The largest peak-to-trough decline in your account balance during the backtest. A drawdown of 5% means your balance dropped 5% from its highest point before recovering. Lower is better\u2009—\u2009high drawdowns indicate higher risk.',
  'Profit factor':
    'Gross profit divided by gross loss. A Profit Factor above 1.0 means you made more than you lost. Above 1.5 is good, above 2.0 is excellent. Below 1.0 means the strategy lost money overall.',
  'Sharpe':
    'Measures return per unit of total risk (volatility). Calculated as average return divided by standard deviation of returns, annualised. Above 1.0 is acceptable, above 2.0 is good, above 3.0 is excellent. Higher is better. Shows "\u2014" when variance is near zero (e.g. all trades have identical returns).',
  'Sortino':
    'Similar to Sharpe, but only penalises downside volatility (losses), not upside volatility (gains). Better suited for trading strategies where winning variance is welcome. Above 1.0 is good, above 2.0 is excellent. Shows "\u221e" when there are zero losing trades.',
  'Calmar':
    'Annual return divided by maximum drawdown. Shows how much return you get per unit of drawdown risk. Above 1.0 means your annual return exceeds your worst drawdown. Higher is better.',
  'Recovery':
    'Total net profit divided by maximum drawdown in BTC. Shows how many times over the strategy recovered from its worst loss. A Recovery Factor of 3 means the strategy earned 3x its worst drawdown. Higher is better.',
  'Expectancy':
    'The average expected profit per trade, calculated as (Win Rate \u00d7 Avg Win) \u2212 (Loss Rate \u00d7 Avg Loss). A positive expectancy means the strategy has a statistical edge. This is the most important metric for long-term profitability.',
  'Avg W/L ratio':
    'Average winning trade divided by average losing trade. A ratio of 2.0 means your average win is twice your average loss. Combined with win rate, this determines your overall expectancy. Higher is better.',
  'Omega':
    'The ratio of the sum of all gains to the sum of all losses. Unlike Sharpe, it considers the full distribution of returns, not just mean and variance. Above 1.0 means more total gains than losses. Higher is better.',
};

function _btCard(label, value, sub) {
  const tip = BT_CARD_TIPS[label];
  const labelHtml = tip
    ? `<span class="bt-tip-label" data-bt-tip="${safeText(label)}">${safeText(label)}</span>`
    : safeText(label);
  return `<div class="card">
    <div class="card-label">${labelHtml}</div>
    <div class="card-value">${value}</div>
    <div class="card-sub">${safeText(sub || '')}</div>
  </div>`;
}

function _btDismissTip() {
  const existing = document.querySelector('.bt-tip-popup');
  if (existing) existing.remove();
}

document.addEventListener('click', (e) => {
  const icon = e.target.closest('.bt-tip-label');
  if (!icon) { _btDismissTip(); return; }
  e.stopPropagation();
  const label = icon.dataset.btTip;
  const text = BT_CARD_TIPS[label];
  if (!text) return;
  const existing = document.querySelector('.bt-tip-popup');
  if (existing && existing.dataset.btTip === label) { existing.remove(); return; }
  _btDismissTip();
  const popup = document.createElement('div');
  popup.className = 'bt-tip-popup';
  popup.dataset.btTip = label;
  popup.textContent = text;
  icon.closest('.card-label').appendChild(popup);
});

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
  const btDeals = res.deals || [];
  const btCandles = _btLastCandles;
  const btConfig = _btLastConfig;
  _btDealPage = 0;
  btCleanupChart();
  _btLastCandles = btCandles;
  _btLastDeals = btDeals;
  _btLastConfig = btConfig;
  const chartEl = $('bt-chart-container');
  if (chartEl && _btLastCandles?.length && _btLastDeals.length) {
    chartEl.style.display = 'block';
    btInitChart(_btLastCandles, _btLastDeals);
  }
  btRenderOpenDealsNote('bt-open-deals-note', s);

  // ── Returns ──────────────────────────────────────────────────────
  $('bt-sec-returns').innerHTML = [
    _btCard('Total PnL', _fmtBtc(s.total_pnl_btc), 'net of fees'),
    _btCard('Total PnL %', _fmtPct(s.total_pnl_pct), 'vs initial'),
    _btCard('Win rate', s.win_rate.toFixed(1) + '%', `${s.wins}W / ${s.losses}L`),
    _btCard('Buy & hold', _fmtBtc(s.buy_and_hold_pnl_btc), _fmtPct(s.buy_and_hold_pnl_pct)),
    _btCard('Winning deals', String(s.wins || 0), 'closed at TP'),
    _btCard('Losing deals',  String(s.losses || 0), 'closed at SL'),
  ].join('');

  // ── Activity ─────────────────────────────────────────────────────
  const activityCards = [
    _btCard('Total deals', String(s.total_deals || 0), 'closed'),
    _btCard('Avg duration', btFormatDuration(s.avg_duration_hours), 'per deal'),
    _btCard('Max duration', btFormatDuration(s.max_duration_hours || 0), 'longest deal'),
    _btCard('Total fees', (s.total_fees_btc || 0).toFixed(8) + ' BTC', 'taker'),
    _btCard('Avg DCA orders', (s.avg_dca_orders || 0).toFixed(2), 'per deal'),
  ];
  if ((s.open_deals_at_end || 0) > 0) {
    activityCards.push(
      _btCard(
        'Open at end',
        String(s.open_deals_at_end),
        (s.open_deals_size_btc || 0).toFixed(6) + ' BTC',
      ),
    );
  }
  $('bt-sec-activity').innerHTML = activityCards.join('');

  // ── Risk ─────────────────────────────────────────────────────────
  $('bt-sec-risk').innerHTML = [
    _btCard('Max drawdown', (s.max_drawdown_pct || 0).toFixed(2) + '%', 'equity peak'),
    _btCard('Win streak', String(r.max_consecutive_wins || 0), 'max'),
    _btCard('Loss streak', String(r.max_consecutive_losses || 0), 'max'),
    _btCard('Best deal', _fmtBtc(r.best_deal_pnl_btc || 0), ''),
    _btCard('Worst deal', _fmtBtc(r.worst_deal_pnl_btc || 0), ''),
  ].join('');

  // ── Ratios ───────────────────────────────────────────────────────
  $('bt-sec-ratios').innerHTML = [
    _btCard('Profit factor', _fmtRatio(r.profit_factor), 'gross win / loss'),
    _btCard('Sharpe', String(r.sharpe), 'annualised'),
    _btCard('Sortino', String(r.sortino), 'downside'),
    _btCard('Calmar', _fmtRatio(r.calmar_ratio), 'annual / max DD'),
    _btCard('Recovery', _fmtRatio(r.recovery_factor), 'PnL / max DD'),
    _btCard('Expectancy', _fmtBtc(r.expectancy_btc || 0, 8), 'per deal'),
    _btCard('Avg W/L ratio', _fmtRatio(r.avg_win_loss_ratio), 'avg win / avg loss'),
    _btCard('Omega', _fmtRatio(r.omega_ratio), 'upside / downside'),
  ].join('');

  // Equity curve chart
  const eqEl = $('bt-equity-chart');
  if (_btEquityChart) { try { _btEquityChart.remove(); } catch (e) {} _btEquityChart = null; }
  eqEl.innerHTML = '';
  if (typeof LightweightCharts !== 'undefined') {
    _btEquityChart = _lwcCreateChart(eqEl, {
      ..._chartLayoutOpts(),
      width: eqEl.clientWidth || 800,
      height: 300,
    });
    const eqSeries = _btEquityChart.addSeries(_LWC().LineSeries, {
      color: _cssVar('--accent', '#26a69a'), lineWidth: 2,
    });
    eqSeries.setData(res.equity_curve.map(p => ({ time: p.time, value: p.balance })));
    const bhSeries = _btEquityChart.addSeries(_LWC().LineSeries, {
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
    _btMonthlyChart = _lwcCreateChart(mEl, {
      ..._chartLayoutOpts(),
      width: mEl.clientWidth || 800,
      height: 200,
    });
    const hist = _btMonthlyChart.addSeries(_LWC().HistogramSeries, {});
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

  // DCA usage — two-part widget:
  //   (A) Per-level breakdown: hoe vaak elke DCA-level triggerde
  //   (B) Per-deal depth histogram: hoe diep gaan deals meestal
  const dEl = $('bt-dca-levels');
  const breakdown = res.dca_level_breakdown || [];
  const depthHist = res.dca_depth_histogram || [];
  const totalTriggers = res.total_dca_triggers || 0;

  if (breakdown.length === 0) {
    dEl.innerHTML = '<div class="empty-grid">No DCA fills</div>';
  } else {
    const breakdownRows = breakdown.map(b =>
      `<div class="bt-dca-row">
         <span>DCA ${b.level}</span>
         <span class="bt-dca-count">${b.count}× (${b.pct.toFixed(1)}%)</span>
       </div>`
    ).join('');

    const depthRows = depthHist.map(h => {
      const label = h.depth === 0
        ? 'No DCA'
        : `${h.depth} DCA${h.depth === 1 ? '' : 's'}`;
      return `<div class="bt-dca-row">
         <span>${label}</span>
         <span class="bt-dca-count">${h.count} deal${h.count === 1 ? '' : 's'} (${h.pct.toFixed(1)}%)</span>
       </div>`;
    }).join('');

    dEl.innerHTML = `
      <div class="bt-dca-section">
        <div class="bt-dca-section-title">DCA-level gebruik (totaal ${totalTriggers})</div>
        ${breakdownRows}
      </div>
      <div class="bt-dca-section">
        <div class="bt-dca-section-title">Deal-diepte verdeling</div>
        ${depthRows}
      </div>
    `;
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
    _wbtEquityChart = _lwcCreateChart(eqEl, {
      ..._chartLayoutOpts(),
      width: eqEl.clientWidth || 600,
      height: 240,
    });
    const series = _wbtEquityChart.addSeries(_LWC().LineSeries, {
      color: _cssVar('--accent', '#26a69a'), lineWidth: 2,
    });
    series.setData(res.equity_curve.map(p => ({ time: p.time, value: p.balance })));
    const bh = _wbtEquityChart.addSeries(_LWC().LineSeries, {
      color: _cssVar('--muted', '#888'), lineWidth: 1, lineStyle: 2,
    });
    bh.setData(res.buy_hold_curve.map(p => ({ time: p.time, value: p.balance })));
    _wbtEquityChart.timeScale().fitContent();
  }
}

let _btDealSort = { key: 'id', dir: 'asc' };
let _btDealPage = 0;
const _BT_DEALS_PER_PAGE = 25;

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
  const fmtTime = t => fmtDateTimeNL(t);
  const fmtDur = s => btFormatDuration(s / 3600);
  const tbody = $('bt-deals-tbody');
  if (!sorted.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${cols.length}">No deals</td></tr>`;
    const pg = $('bt-deals-pagination'); if (pg) pg.innerHTML = '';
    return;
  }
  const totalPages = Math.ceil(sorted.length / _BT_DEALS_PER_PAGE);
  _btDealPage = Math.min(_btDealPage, totalPages - 1);
  const pageDeals = sorted.slice(_btDealPage * _BT_DEALS_PER_PAGE, (_btDealPage + 1) * _BT_DEALS_PER_PAGE);
  tbody.innerHTML = pageDeals.map((d, i) => {
    const globalIdx = _btDealPage * _BT_DEALS_PER_PAGE + i;
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
    return `<tr class="${cls} bt-deal-row" data-bt-deal="${globalIdx}" style="cursor:pointer">${cols.map(c => cells[c.key] || '<td></td>').join('')}</tr>`;
  }).join('');
  const pg = $('bt-deals-pagination');
  if (pg && totalPages > 1) {
    // Stash the current deals array on the container so the single
    // document-level click handler (installed once in setupEventListeners)
    // knows which list to re-render on prev/next. Per-render
    // addEventListener would stack a new listener on every button each
    // redraw, leaking memory and firing the callback N times after N
    // renders. Event delegation avoids both.
    _btPageDeals = deals;
    _btPageTotalPages = totalPages;
    pg.innerHTML = `<button class="bt-page-btn" data-bt-page="prev" ${_btDealPage === 0 ? 'disabled' : ''}>&#9668; Prev</button>`
      + `<span>Page ${_btDealPage + 1} / ${totalPages}</span>`
      + `<button class="bt-page-btn" data-bt-page="next" ${_btDealPage >= totalPages - 1 ? 'disabled' : ''}>Next &#9658;</button>`;
  } else if (pg) { pg.innerHTML = ''; }
}

// Delegated pagination state — populated by btRenderDealTable() and
// consumed by the document-level click handler below.
let _btPageDeals = null;
let _btPageTotalPages = 0;

function _getAllIndicators(config) {
  const entry = config?.entry ?? {};
  const groups = entry.indicator_groups || [];
  if (groups.length) {
    return { groups, flat: groups.flatMap(g => g.indicators || []) };
  }
  const inds = entry.indicators || [];
  if (inds.length) {
    return { groups: [{ id: 1, name: 'Group 1', indicators: inds }], flat: inds };
  }
  return { groups: [], flat: [] };
}

// ── Backtest chart + deal click ──────────────────────────────────────────────
let _btCandleChart = null;
let _btCandleSeries = null;
let _btLastDeals = null;
let _btLastCandles = null;
let _btLastConfig = null;
let _btOverlaySeries = [];

let _btMarkersPrimitive = null;
let _btDealLines = [];

function btInitChart(candles, deals) {
  _btLastCandles = candles;
  _btLastDeals = deals;
  const el = $('bt-chart-container');
  if (!el || !_chartLibAvailable()) return;
  if (_btCandleChart) { try { _btCandleChart.remove(); } catch (e) {} }
  _btMarkersPrimitive = null;
  _btDealLines = [];
  const _btFmt = _buildChartTzFormatter(_btCandleChartTimezone);
  _btCandleChart = _lwcCreateChart(el, {
    ..._chartLayoutOpts(),
    localization: { timeFormatter: _btFmt.full },
    timeScale: {
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: _btFmt.short,
    },
    width: el.clientWidth || 800,
    height: 500,
  });
  // Show + populate the backtest-candle timezone toolbar next to
  // the chart. The toolbar is hidden by default (display:none in
  // HTML) so it doesn't flash while the backtest is still running.
  const _btTbar = $('bt-candle-toolbar');
  if (_btTbar) _btTbar.style.display = '';
  const _btTzSel = $('bt-candle-tz');
  if (_btTzSel) {
    _populateTzDropdown(_btTzSel, _btCandleChartTimezone);
    _btTzSel.onchange = () => {
      _btCandleChartTimezone = _btTzSel.value;
      try { localStorage.setItem(_BT_CANDLE_TZ_LS_KEY, _btCandleChartTimezone); } catch (e) {}
      if (_btCandleChart) {
        const f = _buildChartTzFormatter(_btCandleChartTimezone);
        _btCandleChart.applyOptions({
          localization: { timeFormatter: f.full },
          timeScale: { tickMarkFormatter: f.short },
        });
      }
    };
  }
  _btCandleSeries = _btCandleChart.addSeries(_LWC().CandlestickSeries, {
    upColor: _cssVar('--accent', '#26a69a'),
    downColor: _cssVar('--red', '#ef5350'),
    borderUpColor: _cssVar('--accent', '#26a69a'),
    borderDownColor: _cssVar('--red', '#ef5350'),
    wickUpColor: _cssVar('--accent', '#26a69a'),
    wickDownColor: _cssVar('--red', '#ef5350'),
  });
  _btCandleSeries.setData(candles);
  _btRenderOverlays(candles);
  _btCandleChart.timeScale().fitContent();
}

// Debounced wrapper around _btRenderOverlays — calcSR / calcRSI / calcMACD
// are synchronous and scale linearly with candle count (18k+ bars stall
// ~100ms). Callers that fire overlay rebuilds in bursts (timeframe
// switches, chart resize) should use this wrapper so only the last
// invocation lands; one-shot callers (btInitChart) can still hit the
// underlying function directly.
let _btOverlayDebounceTimer = null;
function _btRenderOverlaysDebounced(candles) {
  if (_btOverlayDebounceTimer) clearTimeout(_btOverlayDebounceTimer);
  _btOverlayDebounceTimer = setTimeout(() => {
    _btOverlayDebounceTimer = null;
    _btRenderOverlays(candles);
  }, 150);
}

function _btRenderOverlays(candles) {
  for (const s of _btOverlaySeries) {
    try { _btCandleChart.removeSeries(s); } catch (e) {}
  }
  _btOverlaySeries = [];
  if (!_btCandleChart || !_btLastConfig) return;
  const { flat: allInds } = _getAllIndicators(_btLastConfig);
  if (!allInds.length) return;
  const _addSeries = (data, color, lw, ls) => {
    if (!data?.length) return;
    const s = _btCandleChart.addSeries(_LWC().LineSeries, {
      color, lineWidth: lw || 1, lineStyle: ls || 0,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });
    s.setData(data);
    _btOverlaySeries.push(s);
  };
  for (const ind of allInds) {
    const t = (ind.type || '').toUpperCase();
    try {
      if (t === 'BOLLINGER') {
        const bb = calcBollingerLines(candles, ind.period || 20, ind.multiplier || 2.0);
        _addSeries(bb.upper, '#5b8dee', 1, 2);
        _addSeries(bb.middle, '#888', 1, 0);
        _addSeries(bb.lower, '#5b8dee', 1, 2);
      } else if (t === 'SUPERTREND') {
        const hi = candles.map(c => c.high), lo = candles.map(c => c.low);
        const st = calcSupertrendLines(candles, ind.atr_period || 10, ind.multiplier || 3.0);
        if (st?.bull) _addSeries(st.bull, '#26a69a', 2);
        if (st?.bear) _addSeries(st.bear, '#ef5350', 2);
      } else if (t === 'SUPPORT_RESISTANCE') {
        const sr = calcSR(candles, ind.left_bars || 15, ind.right_bars || 15,
          ind.volume_threshold || 0, ind.min_touches || 1);
        const buildSegs = (series, color) => {
          const segs = []; let ss = null, sv = null;
          for (let i = 0; i < candles.length; i++) {
            if (series[i] === null) continue;
            if (sv === null) { ss = i; sv = series[i]; }
            else if (series[i] !== sv) { segs.push({ s: ss, e: i - 1, v: sv }); ss = i; sv = series[i]; }
          }
          if (sv !== null) segs.push({ s: ss, e: candles.length - 1, v: sv });
          for (const seg of segs) {
            const data = [];
            for (let j = seg.s; j <= seg.e; j++) data.push({ time: candles[j].time, value: seg.v });
            _addSeries(data, color, 2, 0);
          }
        };
        buildSegs(sr.resSeries, '#e53935');
        buildSegs(sr.supSeries, '#1e88e5');
      }
    } catch (e) {}
  }
  // RSI sub-pane
  const rsiInd = allInds.find(i => (i.type || '').toUpperCase() === 'RSI');
  const macdInd = allInds.find(i => (i.type || '').toUpperCase() === 'MACD');
  if (rsiInd) {
    try {
      const rsiLine = calcRSILine(candles, rsiInd.period || 14);
      if (rsiLine.length) {
        const s = _btCandleChart.addSeries(_LWC().LineSeries, {
          color: '#2962FF', lineWidth: 1,
          priceLineVisible: false, lastValueVisible: true,
        }, 1);
        s.setData(rsiLine);
        const parsed = _parseRsiThreshold(rsiInd.threshold);
        const tv = rsiInd.rsi_value != null ? rsiInd.rsi_value : parsed.value;
        if (tv) s.createPriceLine({ price: tv, color: '#26a69a', lineStyle: 0, lineWidth: 1, axisLabelVisible: true, title: String(tv) });
        _btOverlaySeries.push(s);
      }
    } catch (e) {}
  }
  // MACD sub-pane
  if (macdInd) {
    try {
      const m = calcMACDLines(candles, macdInd.macd_fast || 12, macdInd.macd_slow || 26, macdInd.macd_signal || 9);
      const pane = rsiInd ? 2 : 1;
      if (m.histogram.length) {
        const hs = _btCandleChart.addSeries(_LWC().HistogramSeries, { color: '#888' }, pane);
        hs.setData(m.histogram);
        _btOverlaySeries.push(hs);
        const ms = _btCandleChart.addSeries(_LWC().LineSeries, { color: '#5b8dee', lineWidth: 1 }, pane);
        ms.setData(m.macd);
        _btOverlaySeries.push(ms);
        const ss = _btCandleChart.addSeries(_LWC().LineSeries, { color: '#ffb347', lineWidth: 1 }, pane);
        ss.setData(m.signal);
        _btOverlaySeries.push(ss);
      }
    } catch (e) {}
  }
}

function _btClearDealOverlay() {
  for (const pl of _btDealLines) {
    try { _btCandleSeries.removePriceLine(pl); } catch (e) {}
  }
  _btDealLines = [];
  if (_btMarkersPrimitive) {
    try { _btMarkersPrimitive.setMarkers([]); } catch (e) {}
  }
}

function btShowDealOnChart(dealIdx) {
  if (!_btLastDeals || !_btLastCandles || !_btCandleChart) return;
  const d = _btLastDeals[dealIdx];
  if (!d) return;
  document.querySelectorAll('.bt-deal-row').forEach(r => r.classList.remove('selected'));
  const row = document.querySelector(`[data-bt-deal="${dealIdx}"]`);
  if (row) row.classList.add('selected');
  _btClearDealOverlay();
  const et = d.opened_at;
  const xt = d.closed_at;
  const ep = d.entry_price || (d.orders?.[0]?.price || 0);
  const xp = d.close_price || 0;
  const markers = [];
  if (et) markers.push({ time: et, position: 'belowBar', shape: 'arrowUp', color: '#26a69a', size: 1 });
  if (d.orders) {
    for (let oi = 1; oi < d.orders.length; oi++) {
      const o = d.orders[oi];
      if (o.time || et) markers.push({
        time: o.time || et, position: 'belowBar', shape: 'arrowUp', color: '#5b8dee', size: 1 });
    }
  }
  if (xt) markers.push({
    time: xt, position: 'aboveBar', shape: 'arrowDown',
    color: d.reason === 'sl' ? '#ef5350' : '#26a69a', size: 1 });
  markers.sort((a, b) => a.time - b.time);
  const csm = _LWC().createSeriesMarkers;
  if (csm) {
    if (!_btMarkersPrimitive) _btMarkersPrimitive = csm(_btCandleSeries, markers);
    else _btMarkersPrimitive.setMarkers(markers);
  } else { try { _btCandleSeries.setMarkers(markers); } catch (e) {} }
  if (ep) _btDealLines.push(_btCandleSeries.createPriceLine({
    price: ep, color: '#5b8dee', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'Entry' }));
  if (xp && d.reason === 'tp') _btDealLines.push(_btCandleSeries.createPriceLine({
    price: xp, color: '#26a69a', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TP' }));
  if (xp && d.reason === 'sl') _btDealLines.push(_btCandleSeries.createPriceLine({
    price: xp, color: '#ef5350', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'SL' }));
  const durSec = xt && et ? xt - et : 0;
  const durH = Math.floor(durSec / 3600);
  const durM = Math.floor((durSec % 3600) / 60);
  const durStr = durSec > 0 ? `${durH}h ${durM}m` : '--';
  let triggerHtml = '';
  if (d.entry_trigger) {
    const t = d.entry_trigger;
    const inds = t.indicators?.join(' + ') || '';
    triggerHtml += `<div class="deal-trigger-row"><span class="trigger-label">Entry:</span>`
      + `<span class="trigger-value accent">${safeText(t.group_name || '')}${inds ? ' — ' + safeText(inds) : ''}</span></div>`;
  }
  const exitLabels = { price_tp: 'TP price hit', price_sl: 'Stop loss', timeout: 'Timeout' };
  const exitCls = { price_tp: 'green', price_sl: 'red', timeout: 'muted' };
  const ext = d.exit_trigger || d.reason;
  const extLabel = exitLabels[ext] || (ext === 'tp' ? 'TP price hit' : ext === 'sl' ? 'Stop loss' : safeText(ext || '--'));
  const extClass = exitCls[ext] || (ext === 'tp' || ext === 'price_tp' ? 'green' : ext === 'sl' || ext === 'price_sl' ? 'red' : 'muted');
  triggerHtml += `<div class="deal-trigger-row"><span class="trigger-label">Exit:</span><span class="trigger-value ${extClass}">${extLabel}</span></div>`;
  const info = $('bt-deal-info');
  if (info) {
    info.innerHTML = `<strong>Deal #${safeText(String(d.id))}</strong>`
      + ` &bull; ${fmtDateTimeNL(et)}<br>`
      + `Entry: ${fmtPrice(ep)} &rarr; Exit: ${fmtPrice(xp)}<br>`
      + `PnL: ${d.pnl_pct >= 0 ? '+' : ''}${d.pnl_pct?.toFixed(2) || '--'}%`
      + ` &bull; Duration: ${durStr}`
      + triggerHtml;
    info.classList.add('visible');
  }
  if (et) {
    const pad = 10;
    const entryIdx = _btLastCandles.findIndex(c => c.time >= et);
    const exitIdx = xt ? _btLastCandles.findIndex(c => c.time >= xt) : entryIdx + 20;
    const from = Math.max(0, (entryIdx >= 0 ? entryIdx : 0) - pad);
    const to = Math.min(_btLastCandles.length - 1, (exitIdx >= 0 ? exitIdx : entryIdx + 20) + pad);
    _btCandleChart.timeScale().setVisibleLogicalRange({ from, to });
  }
}

function btCleanupChart() {
  if (_btCandleChart) { try { _btCandleChart.remove(); } catch (e) {} }
  _btCandleChart = null; _btCandleSeries = null;
  _btLastDeals = null; _btLastCandles = null;
  _btOverlaySeries = []; _btMarkersPrimitive = null; _btDealLines = [];
  const info = $('bt-deal-info');
  if (info) { info.classList.remove('visible'); info.innerHTML = ''; }
  const el = $('bt-chart-container');
  if (el) el.style.display = 'none';
}
