/* render.js — vanilla JS render logic + theme toggle for the
   static marketing site at https://reverto.bot.

   Loads /data/roadmap.json and /data/changelog.json (written by
   the FastAPI app's snapshot-export hooks; see
   core/marketing_export.py) and renders them into the timeline /
   list containers in roadmap.html and changelog.html.

   Render functions intentionally mirror web/static/app.js
   (_rmRenderPhase + _clRenderEntry) so the marketing site reads
   like the in-app SPA. The marketing copies are read-only — no
   admin UI, no edit modals — and live independently so the
   marketing bundle stays free of any auth / framework code.

   body_html and description_html are emitted by bleach on the
   server (core/markdown_render.py) and are safe to drop into
   innerHTML directly. Adding a client-side sanitiser would
   duplicate the trust boundary without strengthening it. */


/* ── Theme toggle ─────────────────────────────────────────────── */
/* Initial theme application happens in an inline <script> in the
   <head> of every marketing page (before the stylesheet loads) to
   avoid a flash of wrong theme on initial paint. This module only
   handles the runtime toggle: click the button → flip the
   data-theme attribute on <html> → persist to localStorage.

   localStorage key: reverto-theme. Same name as the app uses, but
   marketing.bot and app.reverto.bot are different origins so the
   storage is not shared — naming is for consistency only. */

function toggleTheme() {
  const root = document.documentElement;
  const current = root.dataset.theme || 'dark';
  const next = (current === 'dark') ? 'light' : 'dark';
  root.dataset.theme = next;
  try {
    localStorage.setItem('reverto-theme', next);
  } catch (e) {
    // localStorage unavailable (private mode, sandboxed iframe,
    // disk-quota etc) — toggle still works for the session even
    // without persistence.
    if (typeof console !== 'undefined') {
      console.warn('Theme preference not persisted:', e);
    }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('theme-toggle-btn');
  if (btn) btn.addEventListener('click', toggleTheme);
});


/* ── Roadmap ──────────────────────────────────────────────────── */

const _RM_STATUS_LABELS = {
  pending: 'Pending',
  active: 'Active',
  done: 'Done',
};

function _rmStatusBadge(status) {
  const safe = String(status || '').replace(/[^a-z]/g, '');
  const label = _RM_STATUS_LABELS[safe] || safe || '—';
  const badge = document.createElement('span');
  badge.className = 'roadmap-badge roadmap-badge--' + safe;
  badge.textContent = label;
  return badge;
}

function _rmRenderMetaItem(label, value) {
  if (!value) return null;
  const wrap = document.createElement('div');
  wrap.className = 'roadmap-meta-item';
  const labelEl = document.createElement('span');
  labelEl.className = 'roadmap-meta-item-label';
  labelEl.textContent = label + ' ';
  const valueEl = document.createElement('span');
  valueEl.textContent = value;
  wrap.appendChild(labelEl);
  wrap.appendChild(valueEl);
  return wrap;
}

function _rmRenderPhase(phase) {
  const article = document.createElement('article');
  const safeStatus = String(phase.status || 'pending').replace(/[^a-z]/g, '');
  article.className = 'roadmap-phase roadmap-phase--' + safeStatus;

  const dot = document.createElement('div');
  dot.className = 'roadmap-dot';
  dot.setAttribute('aria-hidden', 'true');
  article.appendChild(dot);

  const header = document.createElement('div');
  header.className = 'roadmap-phase-header';
  const title = document.createElement('h3');
  title.className = 'roadmap-phase-title';
  title.textContent = phase.display_name || phase.phase_key || '—';
  header.appendChild(title);
  header.appendChild(_rmStatusBadge(phase.status));
  article.appendChild(header);

  if (phase.summary) {
    const summary = document.createElement('div');
    summary.className = 'roadmap-phase-summary';
    summary.textContent = phase.summary;
    article.appendChild(summary);
  }

  if (phase.status === 'active' && phase.in_progress_note) {
    const note = document.createElement('div');
    note.className = 'roadmap-progress-note';
    const noteLabel = document.createElement('span');
    noteLabel.className = 'roadmap-progress-note-label';
    noteLabel.textContent = 'Currently working on';
    const noteBody = document.createElement('span');
    noteBody.textContent = phase.in_progress_note;
    note.appendChild(noteLabel);
    note.appendChild(noteBody);
    article.appendChild(note);
  }

  const metaItems = [];
  const effortItem = _rmRenderMetaItem('Effort:', phase.effort_estimate);
  if (effortItem) metaItems.push(effortItem);
  const auditItem = _rmRenderMetaItem('Audit:', phase.audit_checkpoint);
  if (auditItem) metaItems.push(auditItem);
  if (metaItems.length > 0) {
    const meta = document.createElement('div');
    meta.className = 'roadmap-meta';
    metaItems.forEach((m) => meta.appendChild(m));
    article.appendChild(meta);
  }

  if (phase.body_html) {
    const body = document.createElement('div');
    body.className = 'roadmap-phase-body';
    body.hidden = true;
    body.innerHTML = phase.body_html;

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'roadmap-phase-toggle';
    toggle.textContent = 'Read more';
    toggle.addEventListener('click', () => {
      const hidden = body.hidden;
      body.hidden = !hidden;
      toggle.textContent = hidden ? 'Show less' : 'Read more';
    });

    article.appendChild(toggle);
    article.appendChild(body);
  }

  return article;
}

async function loadRoadmap() {
  const statusEl = document.getElementById('roadmap-status');
  const timelineEl = document.getElementById('roadmap-timeline');
  const phasesEl = document.getElementById('roadmap-phases');
  if (!statusEl || !timelineEl || !phasesEl) return;

  try {
    const r = await fetch('/data/roadmap.json', { cache: 'no-cache' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const phases = Array.isArray(data.phases) ? data.phases : [];

    if (phases.length === 0) {
      statusEl.textContent =
        'The roadmap is being prepared — check back soon.';
      return;
    }
    statusEl.hidden = true;
    timelineEl.hidden = false;
    const frag = document.createDocumentFragment();
    phases.forEach((p) => frag.appendChild(_rmRenderPhase(p)));
    phasesEl.appendChild(frag);
  } catch (e) {
    if (typeof console !== 'undefined') console.error('Failed to load roadmap:', e);
    statusEl.textContent = 'Failed to load the roadmap. Please try again later.';
  }
}


/* ── Changelog ────────────────────────────────────────────────── */

const _CL_CATEGORY_LABELS = {
  feature: 'Feature',
  fix: 'Fix',
  improvement: 'Improvement',
  security: 'Security',
};

function _clFormatDate(ts) {
  if (!ts) return '—';
  return String(ts).split(' ')[0];
}

function _clCategoryBadge(category) {
  const safe = String(category || '').replace(/[^a-z]/g, '');
  const label = _CL_CATEGORY_LABELS[safe] || safe || '—';
  const badge = document.createElement('span');
  badge.className = 'cl-badge cl-badge-' + safe;
  badge.textContent = label;
  return badge;
}

function _clRenderEntry(entry) {
  const article = document.createElement('article');
  article.className = 'cl-entry';

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
  body.innerHTML = entry.description_html || '';

  article.appendChild(header);
  article.appendChild(body);
  return article;
}

async function loadChangelog() {
  const statusEl = document.getElementById('changelog-status');
  const listEl = document.getElementById('changelog-list');
  if (!statusEl || !listEl) return;

  try {
    const r = await fetch('/data/changelog.json', { cache: 'no-cache' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];

    if (entries.length === 0) {
      statusEl.textContent = 'No updates yet.';
      return;
    }
    statusEl.hidden = true;
    listEl.hidden = false;
    const frag = document.createDocumentFragment();
    entries.forEach((e) => frag.appendChild(_clRenderEntry(e)));
    listEl.appendChild(frag);
  } catch (e) {
    if (typeof console !== 'undefined') console.error('Failed to load changelog:', e);
    statusEl.textContent = 'Failed to load the changelog. Please try again later.';
  }
}
