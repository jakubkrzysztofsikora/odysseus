import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import * as Modals from './modalManager.js';

const API_BASE = window.location.origin;
const MODAL_ID = 'evidence-modal';
const ICON_PATH = 'M12 2l7 4v6c0 5-3.5 9-7 10-3.5-1-7-5-7-10V6zM9 12l2 2 4-4';

let _open = false;
let _escHandler = null;
let _lastEntries = [];

function _field(obj, ...names) {
  for (const name of names) {
    if (obj && Object.prototype.hasOwnProperty.call(obj, name)) return obj[name];
  }
  return undefined;
}

function _escape(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function _shortHash(value) {
  const text = String(value || '');
  if (!text) return '';
  if (text.length <= 18) return text;
  return `${text.slice(0, 10)}...${text.slice(-6)}`;
}

function _formatTime(value) {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString([], {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function _normaliseEntries(payload) {
  const source = Array.isArray(payload) ? payload : (payload?.entries || payload?.items || []);
  return source.map((entry) => ({
    sequence: _field(entry, 'sequence', 'Sequence'),
    actor: _field(entry, 'actor', 'Actor'),
    sessionId: _field(entry, 'sessionId', 'SessionId'),
    correlationId: _field(entry, 'correlationId', 'CorrelationId'),
    tool: _field(entry, 'tool', 'Tool'),
    toolArgsHash: _field(entry, 'toolArgsHash', 'ToolArgsHash'),
    dataSourceId: _field(entry, 'dataSourceId', 'DataSourceId'),
    dataClass: _field(entry, 'dataClass', 'DataClass'),
    provider: _field(entry, 'provider', 'Provider'),
    model: _field(entry, 'model', 'Model'),
    timestamp: _field(entry, 'timestamp', 'Timestamp'),
    prevHash: _field(entry, 'prevHash', 'PrevHash'),
    entryHash: _field(entry, 'entryHash', 'EntryHash'),
  }));
}

async function _fetchJson(path, { allowConflict = false } = {}) {
  const res = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin' });
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    data = null;
  }
  if (!res.ok && !(allowConflict && res.status === 409 && data)) {
    const detail = data?.detail || data?.reason || data?.error || res.statusText;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

function _verifyLocalLinks(entries) {
  const ordered = [...entries].sort((a, b) => Number(a.sequence || 0) - Number(b.sequence || 0));
  for (let i = 1; i < ordered.length; i += 1) {
    const prev = String(ordered[i - 1].entryHash || '');
    const currentPrev = String(ordered[i].prevHash || '');
    if (prev && currentPrev && prev !== currentPrev) {
      return {
        valid: false,
        reason: `Local link mismatch at sequence ${ordered[i].sequence}`,
      };
    }
  }
  return { valid: true, reason: '' };
}

function _renderSummary(verdict, entries) {
  const statusEl = document.getElementById('evidence-status');
  const detailsEl = document.getElementById('evidence-summary');
  if (!statusEl || !detailsEl) return;

  const valid = Boolean(_field(verdict, 'valid', 'Valid'));
  const entryCount = _field(verdict, 'entryCount', 'EntryCount') ?? entries.length;
  const verifiedAt = _field(verdict, 'verifiedAt', 'VerifiedAt');
  const reason = _field(verdict, 'reason', 'Reason');
  const brokenAtSequence = _field(verdict, 'brokenAtSequence', 'BrokenAtSequence');
  const local = _verifyLocalLinks(entries);
  const css = valid && local.valid ? 'valid' : 'broken';

  statusEl.className = `evidence-status-pill ${css}`;
  statusEl.textContent = valid && local.valid ? 'Valid' : 'Broken';

  const cards = [
    ['Entries', entryCount],
    ['Verified', _formatTime(verifiedAt) || 'now'],
    ['Local links', local.valid ? 'Valid' : 'Broken'],
  ];
  if (!valid && brokenAtSequence != null) cards.push(['Broken seq', brokenAtSequence]);
  if (reason) cards.push(['Reason', reason]);
  if (!local.valid) cards.push(['Local reason', local.reason]);

  detailsEl.innerHTML = cards.map(([label, value]) => `
    <div class="evidence-summary-card">
      <span>${_escape(label)}</span>
      <strong>${_escape(value)}</strong>
    </div>
  `).join('');
}

function _entryMeta(entry) {
  const bits = [];
  if (entry.tool) bits.push(entry.tool);
  if (entry.provider || entry.model) bits.push([entry.provider, entry.model].filter(Boolean).join(' / '));
  if (entry.dataClass) bits.push(entry.dataClass);
  return bits.join(' | ');
}

function _renderEntries(entries) {
  const listEl = document.getElementById('evidence-list');
  if (!listEl) return;
  if (!entries.length) {
    listEl.innerHTML = '<div class="evidence-empty">No evidence entries yet.</div>';
    return;
  }

  const ordered = [...entries].sort((a, b) => Number(b.sequence || 0) - Number(a.sequence || 0));
  listEl.innerHTML = ordered.map((entry) => {
    const seq = entry.sequence ?? '?';
    const actor = entry.actor || 'unknown';
    const meta = _entryMeta(entry);
    return `
      <article class="evidence-entry">
        <div class="evidence-entry-head">
          <div class="evidence-entry-title">
            <span class="evidence-seq">#${_escape(seq)}</span>
            <strong>${_escape(actor)}</strong>
          </div>
          <time>${_escape(_formatTime(entry.timestamp))}</time>
        </div>
        ${meta ? `<div class="evidence-entry-meta">${_escape(meta)}</div>` : ''}
        <div class="evidence-hash-grid">
          <div><span>Entry</span><code title="${_escape(entry.entryHash)}">${_escape(_shortHash(entry.entryHash))}</code></div>
          <div><span>Previous</span><code title="${_escape(entry.prevHash)}">${_escape(_shortHash(entry.prevHash))}</code></div>
          <div><span>Args</span><code title="${_escape(entry.toolArgsHash)}">${_escape(_shortHash(entry.toolArgsHash))}</code></div>
        </div>
        <div class="evidence-entry-foot">
          ${entry.sessionId ? `<span>session ${_escape(_shortHash(entry.sessionId))}</span>` : ''}
          ${entry.correlationId ? `<span>corr ${_escape(_shortHash(entry.correlationId))}</span>` : ''}
          ${entry.dataSourceId ? `<span>source ${_escape(_shortHash(entry.dataSourceId))}</span>` : ''}
        </div>
      </article>
    `;
  }).join('');
}

function _setLoading() {
  const statusEl = document.getElementById('evidence-status');
  const summaryEl = document.getElementById('evidence-summary');
  const listEl = document.getElementById('evidence-list');
  if (statusEl) {
    statusEl.className = 'evidence-status-pill';
    statusEl.textContent = 'Checking';
  }
  if (summaryEl) summaryEl.innerHTML = '';
  if (listEl) listEl.innerHTML = '<div class="evidence-empty">Loading evidence...</div>';
}

async function _refreshEvidence() {
  _setLoading();
  try {
    const [verdict, rawEntries] = await Promise.all([
      _fetchJson('/api/agentcore/audit-verify', { allowConflict: true }),
      _fetchJson('/api/agentcore/audit-verify/entries'),
    ]);
    _lastEntries = _normaliseEntries(rawEntries);
    _renderSummary(verdict || {}, _lastEntries);
    _renderEntries(_lastEntries);
  } catch (error) {
    const statusEl = document.getElementById('evidence-status');
    const summaryEl = document.getElementById('evidence-summary');
    const listEl = document.getElementById('evidence-list');
    if (statusEl) {
      statusEl.className = 'evidence-status-pill error';
      statusEl.textContent = 'Unavailable';
    }
    if (summaryEl) {
      summaryEl.innerHTML = `
        <div class="evidence-summary-card evidence-summary-error">
          <span>Error</span>
          <strong>${_escape(error?.message || error)}</strong>
        </div>
      `;
    }
    if (listEl) listEl.innerHTML = '<div class="evidence-empty">Evidence could not be loaded.</div>';
  }
}

function _exportJson() {
  const payload = {
    exportedAt: new Date().toISOString(),
    entries: _lastEntries,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `agentcore-evidence-${Date.now()}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function _attachEvents(modal) {
  modal.querySelector('#evidence-close')?.addEventListener('click', closeEvidence);
  modal.querySelector('#evidence-refresh')?.addEventListener('click', _refreshEvidence);
  modal.querySelector('#evidence-export')?.addEventListener('click', _exportJson);
  modal.addEventListener('click', (event) => {
    if (uiModule.isTouchInsideModal()) return;
    if (event.target === modal) closeEvidence();
  });
  _escHandler = (event) => {
    if (event.key === 'Escape') closeEvidence();
  };
  document.addEventListener('keydown', _escHandler);
}

export function openEvidence() {
  if (_open) return;
  _open = true;

  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = MODAL_ID;
  modal.innerHTML = `
    <div class="modal-content evidence-modal-content">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M12 2l7 4v6c0 5-3.5 9-7 10-3.5-1-7-5-7-10V6z"/><path d="M9 12l2 2 4-4"/></svg>Evidence</h4>
        <span id="evidence-status" class="evidence-status-pill">Checking</span>
        <button class="evidence-header-btn" id="evidence-refresh" type="button">Verify</button>
        <button class="evidence-header-btn" id="evidence-export" type="button">Export</button>
        <button class="close-btn" id="evidence-close" type="button">x</button>
      </div>
      <div class="modal-body evidence-body">
        <section id="evidence-summary" class="evidence-summary"></section>
        <section id="evidence-list" class="evidence-list"></section>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(modal, { content, header });

  Modals.register(MODAL_ID, {
    railBtnId: 'rail-evidence',
    sidebarBtnId: 'tool-evidence-btn',
    label: 'Evidence',
    icon: ICON_PATH,
    restoreFn: () => {
      _open = true;
      _refreshEvidence();
    },
    closeFn: closeEvidence,
  });
  Modals.injectMinimizeButton(modal, MODAL_ID);

  _attachEvents(modal);
  _refreshEvidence();
}

export function closeEvidence() {
  if (!_open && !document.getElementById(MODAL_ID)) return;
  _open = false;

  const modal = document.getElementById(MODAL_ID);
  if (modal) {
    const content = modal.querySelector('.modal-content');
    if (content) {
      content.classList.add('modal-closing');
      content.addEventListener('animationend', () => modal.remove(), { once: true });
      setTimeout(() => {
        if (modal.parentElement) modal.remove();
      }, 250);
    } else {
      modal.remove();
    }
  }

  if (_escHandler) {
    document.removeEventListener('keydown', _escHandler);
    _escHandler = null;
  }
  Modals.unregister(MODAL_ID);
}

export function isEvidenceOpen() {
  return _open;
}

const evidenceModule = { openEvidence, closeEvidence, isEvidenceOpen };
export default evidenceModule;
