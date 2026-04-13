// web/static/app.js — Reverto portal frontend
// Verplaatst uit inline <script> in index.html zodat CSP geen 'unsafe-inline'
// nodig heeft op script-src. Alle event handlers worden via addEventListener
// gebonden in setupEventListeners() — geen onclick="..." attributes meer.

// ── Theme ─────────────────────────────────────────────────────────────────────
const t0 = localStorage.getItem('reverto-theme') || 'dark';
document.documentElement.setAttribute('data-theme', t0);
function toggleTheme() {
  const n = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', n);
  localStorage.setItem('reverto-theme', n);
}

// ── API Key management ────────────────────────────────────────────────────────
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
  if (!key) { alert('Lege key — niet opgeslagen'); return; }
  localStorage.setItem('reverto_api_key', key);
  closeApiKeyModal();
  location.reload();
}
function clearApiKey() {
  localStorage.removeItem('reverto_api_key');
  document.getElementById('api-key-input').value = '';
  closeApiKeyModal();
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
  ['dashboard', 'deals', 'log'].forEach(n => {
    const el = $('dtab-' + n);
    if (el) { el.classList.toggle('hidden', n !== name); }
  });
  document.querySelectorAll('.detail-subnav .tab').forEach(t => t.classList.remove('active'));
  if (btn) btn.classList.add('active');
}

// ── Overview ──────────────────────────────────────────────────────────────────
async function fetchOverview() {
  try {
    const d = await fetch('/api/bots').then(r => r.json());
    renderOverview(d);
  } catch (e) {}
}

function renderOverview(d) {
  const sum = d.summary || {};

  const pnl = sum.total_pnl_btc || 0;
  $('ov-pnl').innerHTML = fmtPnl(pnl, 8);
  $('ov-active').textContent = sum.active_bots ?? '—';
  $('ov-total-sub').textContent = `of ${sum.total_bots ?? 0} configured`;
  $('ov-deals').textContent = sum.open_deals ?? '—';

  const runningBot = (d.bots || []).find(b => b.running && b.current_price);
  if (runningBot) {
    $('hdr-price').textContent = fmtPrice(runningBot.current_price);
    $('hdr-pair').textContent = runningBot.pair || 'BTC/USD';
  }

  const grid = $('bot-grid');
  const bots = d.bots || [];
  if (!bots.length) {
    grid.innerHTML = '<div class="empty-config-msg">No bots configured — add a YAML file to config/bots/</div>';
  } else {
    grid.innerHTML = bots.map(b => renderBotCard(b)).join('');
  }

  const tbody = $('all-deals-tbody');
  const deals = d.all_open_deals || [];
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
  else if (['start', 'stop', 'restart'].includes(action)) botAction(slug, action);
});

// ── Bot actions ───────────────────────────────────────────────────────────────
async function botAction(slug, action) {
  const res = await fetch(`/api/bots/${slug}/${action}`, {
    method: 'POST',
    headers: { 'X-API-Key': getApiKey() }
  });
  if (res.status === 401) {
    alert('Auth fout — controleer je API key');
    showApiKeyModal();
    return;
  }
  const r = await res.json();
  if (!r.ok) alert(`${action} failed: ${r.error}`);
  setTimeout(fetchOverview, 1200);
  if (currentSlug === slug) setTimeout(() => fetchDetail(slug), 1500);
}

// ── Bot detail ────────────────────────────────────────────────────────────────
function goOverview() {
  currentSlug = null;
  clearInterval(detailInterval);
  if (ws) { ws.close(); ws = null; }

  $('hdr-context').textContent = 'Multi-Bot Portal';
  $('hdr-context').classList.remove('clickable');
  $('hdr-context').onclick = null;
  $('hdr-pill').classList.add('hidden');
  $('hdr-uptime').textContent = '';
  $('detail-nav-item').classList.add('hidden');

  document.querySelectorAll('#main-nav .tab').forEach(t => t.classList.remove('active'));
  $('main-nav').querySelector('.tab').classList.add('active');

  showPage('overview');
  fetchOverview();
  overviewInterval = setInterval(fetchOverview, 5000);
}

function openBot(slug) {
  clearInterval(overviewInterval);
  currentSlug = slug;

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

  document.querySelectorAll('.detail-subnav .tab').forEach(btn => {
    btn.addEventListener('click', () => showDTab(btn.dataset.dtab, btn));
  });

  $('modal-clear-btn').addEventListener('click', clearApiKey);
  $('modal-cancel-btn').addEventListener('click', closeApiKeyModal);
  $('modal-save-btn').addEventListener('click', saveApiKey);

  $('log-clear-btn').addEventListener('click', clearLog);
  $('ov-log-clear-btn').addEventListener('click', () => { $('ov-log-body').innerHTML = ''; });
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
