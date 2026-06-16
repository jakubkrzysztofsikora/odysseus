// static/js/workflows.js
import { showToast, showError } from './ui.js';

const API = '';

const state = {
  loaded: false,
  loading: false,
  dirty: false,
  running: false,
  tab: 'editor',
  selectedId: '',
  snapshot: { schemaVersion: 1, version: 0, workflows: [] },
  runs: [],
  artifacts: [],
  models: [],
  defaultModel: '',
  defaultEndpointId: '',
  publicUrls: new Map(),
};

function $(sel, root = document) {
  return root.querySelector(sel);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
}

function newId(prefix) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(16).slice(2, 8)}`;
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  if (!res.ok) {
    const msg = body?.error?.message || body?.detail || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function ensureModal() {
  let modal = $('#workflows-modal');
  if (modal) return modal;
  modal = document.createElement('div');
  modal.id = 'workflows-modal';
  modal.className = 'modal hidden';
  modal.innerHTML = `
    <div class="modal-content workflows-modal-content">
      <div class="modal-header">
        <h4>Workflows</h4>
        <button class="modal-close" id="workflows-close" title="Close" aria-label="Close">x</button>
      </div>
      <div class="modal-body workflows-body" id="workflows-body"></div>
    </div>
  `;
  document.body.appendChild(modal);
  $('#workflows-close', modal).addEventListener('click', closeWorkflows);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeWorkflows();
  });
  return modal;
}

async function loadAll({ force = false } = {}) {
  if (state.loading) return;
  if (state.loaded && !force) return;
  state.loading = true;
  render();
  try {
    const [snap, runs, artifacts, models, def] = await Promise.all([
      api('/api/workflows'),
      api('/api/runs').catch(() => ({ runs: [] })),
      api('/api/artifacts').catch(() => ({ artifacts: [] })),
      api('/api/models').catch(() => ({ items: [] })),
      api('/api/default-chat').catch(() => ({})),
    ]);
    state.snapshot = snap || { schemaVersion: 1, version: 0, workflows: [] };
    state.runs = Array.isArray(runs?.runs) ? runs.runs : [];
    state.artifacts = Array.isArray(artifacts?.artifacts) ? artifacts.artifacts : [];
    state.defaultModel = def?.model || '';
    state.defaultEndpointId = def?.endpoint_id || '';
    state.models = flattenModels(models);
    if (!state.selectedId && state.snapshot.workflows[0]) {
      state.selectedId = state.snapshot.workflows[0].id;
    }
    state.loaded = true;
    state.dirty = false;
  } catch (err) {
    showError(`Workflows failed: ${err.message}`);
  } finally {
    state.loading = false;
    render();
  }
}

function flattenModels(payload) {
  const out = [];
  for (const item of payload?.items || []) {
    if (item?.model_type && item.model_type !== 'llm') continue;
    const endpointId = item.endpoint_id || '';
    const endpointName = item.endpoint_name || 'Endpoint';
    for (const model of [...(item.models || []), ...(item.models_extra || [])]) {
      if (!model) continue;
      out.push({ model, endpointId, endpointName });
    }
  }
  const seen = new Set();
  return out.filter((m) => {
    const key = `${m.endpointId}:${m.model}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function currentWorkflow() {
  return (state.snapshot.workflows || []).find((w) => w.id === state.selectedId) || null;
}

function makeWorkflow() {
  const now = Date.now();
  return {
    id: newId('wf'),
    name: 'New workflow',
    description: '',
    fields: [{
      id: newId('field'),
      key: 'input',
      label: 'Input',
      type: 'textarea',
      required: true,
      placeholder: '',
    }],
    trigger: { kind: 'on_event' },
    steps: [{
      id: newId('step'),
      kind: 'llm',
      model: state.defaultModel || '',
      endpointId: state.defaultEndpointId || '',
      systemPrompt: 'You are a precise assistant. Return a useful result for the operator.',
      userTemplate: '{{inputs.input}}',
      maxTokens: 1500,
    }],
    createdAt: now,
    updatedAt: now,
  };
}

function markDirty() {
  const wf = currentWorkflow();
  if (wf) wf.updatedAt = Date.now();
  state.dirty = true;
  renderSaveState();
}

function renderSaveState() {
  const btn = $('#workflow-save');
  if (btn) {
    btn.disabled = !state.dirty || state.loading || state.running;
    btn.textContent = state.dirty ? 'Save' : 'Saved';
  }
}

function render() {
  const modal = ensureModal();
  const body = $('#workflows-body', modal);
  if (!body) return;
  if (state.loading && !state.loaded) {
    body.innerHTML = '<div class="workflow-loading">Loading workflows...</div>';
    return;
  }

  const wf = currentWorkflow();
  body.innerHTML = `
    <div class="workflow-toolbar">
      <div class="workflow-tabs" role="tablist">
        ${tabButton('editor', 'Editor')}
        ${tabButton('runs', 'Runs')}
        ${tabButton('artifacts', 'Artifacts')}
      </div>
      <div class="workflow-toolbar-actions">
        <button type="button" class="workflow-btn" id="workflow-refresh" title="Refresh">Refresh</button>
        <button type="button" class="workflow-btn workflow-primary" id="workflow-save" title="Save">Save</button>
      </div>
    </div>
    <datalist id="workflow-model-list">
      ${state.models.map((m) => `<option value="${escapeHtml(m.model)}">${escapeHtml(m.endpointName)}</option>`).join('')}
    </datalist>
    ${state.tab === 'editor' ? renderEditor(wf) : ''}
    ${state.tab === 'runs' ? renderRuns() : ''}
    ${state.tab === 'artifacts' ? renderArtifacts() : ''}
  `;
  wireBody(body);
  renderSaveState();
}

function tabButton(id, label) {
  return `<button type="button" class="workflow-tab ${state.tab === id ? 'active' : ''}" data-workflow-tab="${id}">${label}</button>`;
}

function renderEditor(wf) {
  return `
    <div class="workflow-shell">
      <aside class="workflow-list-pane">
        <button type="button" class="workflow-btn workflow-primary workflow-full" id="workflow-new">New Workflow</button>
        <div class="workflow-list">
          ${(state.snapshot.workflows || []).map((item) => `
            <button type="button" class="workflow-list-item ${item.id === state.selectedId ? 'active' : ''}" data-select-workflow="${escapeHtml(item.id)}">
              <span>${escapeHtml(item.name || 'Untitled')}</span>
              <small>${escapeHtml((item.trigger || {}).kind || 'on_event')}</small>
            </button>
          `).join('') || '<div class="workflow-empty">No workflows yet.</div>'}
        </div>
      </aside>
      <section class="workflow-editor-pane">
        ${wf ? renderWorkflowForm(wf) : '<div class="workflow-empty">Create a workflow to begin.</div>'}
      </section>
    </div>
  `;
}

function renderWorkflowForm(wf) {
  const pub = wf.public || {};
  const publicUrl = state.publicUrls.get(wf.id) || (pub.publicId ? `https://ai.jakub.team/f/${pub.publicId}` : '');
  return `
    <div class="workflow-editor-grid">
      <div class="workflow-section workflow-section-main">
        <div class="workflow-form-grid">
          <label>Name<input data-wf-prop="name" value="${escapeHtml(wf.name || '')}" maxlength="80"></label>
          <label>Trigger
            <select data-trigger-kind>
              <option value="on_event" ${(wf.trigger || {}).kind === 'on_event' ? 'selected' : ''}>Public event</option>
              <option value="scheduled" ${(wf.trigger || {}).kind === 'scheduled' ? 'selected' : ''}>Scheduled batch</option>
            </select>
          </label>
          <label class="workflow-span-2">Description<textarea data-wf-prop="description" rows="2">${escapeHtml(wf.description || '')}</textarea></label>
          ${(wf.trigger || {}).kind === 'scheduled' ? `<label>Cron<input data-trigger-cron value="${escapeHtml((wf.trigger || {}).cron || '0 * * * *')}" placeholder="0 * * * *"></label>` : ''}
        </div>
        <div class="workflow-heading-row">
          <h5>Fields</h5>
          <button type="button" class="workflow-icon-btn" id="workflow-add-field" title="Add field">+</button>
        </div>
        <div class="workflow-fields">
          ${(wf.fields || []).map(renderField).join('') || '<div class="workflow-empty">No fields.</div>'}
        </div>
        <div class="workflow-heading-row">
          <h5>Steps</h5>
          <button type="button" class="workflow-icon-btn" id="workflow-add-step" title="Add step">+</button>
        </div>
        <div class="workflow-steps">
          ${(wf.steps || []).map(renderStep).join('') || '<div class="workflow-empty">No steps.</div>'}
        </div>
      </div>
      <aside class="workflow-section workflow-side">
        <div class="workflow-heading-row">
          <h5>Test Run</h5>
          <button type="button" class="workflow-btn workflow-primary" id="workflow-run" ${state.running ? 'disabled' : ''}>${state.running ? 'Running' : 'Run'}</button>
        </div>
        <div class="workflow-test-inputs">
          ${renderTestInputs(wf)}
        </div>
        <div id="workflow-run-result" class="workflow-result"></div>
        <div class="workflow-heading-row workflow-publish-head">
          <h5>Public Form</h5>
          ${pub.enabled ? '<span class="workflow-badge">Published</span>' : '<span class="workflow-badge muted">Private</span>'}
        </div>
        ${publicUrl ? `<input class="workflow-public-url" readonly value="${escapeHtml(publicUrl)}">` : ''}
        <div class="workflow-publish-actions">
          <button type="button" class="workflow-btn workflow-primary" id="workflow-publish">${pub.enabled ? 'Republish' : 'Publish'}</button>
          <button type="button" class="workflow-btn" id="workflow-unpublish" ${pub.enabled ? '' : 'disabled'}>Unpublish</button>
          <button type="button" class="workflow-btn" id="workflow-pull-public">Pull</button>
        </div>
        <button type="button" class="workflow-btn workflow-danger workflow-full" id="workflow-delete">Delete Workflow</button>
      </aside>
    </div>
  `;
}

function renderField(field, idx) {
  const options = Array.isArray(field.options) ? field.options.join(', ') : '';
  return `
    <div class="workflow-field-row" data-field-index="${idx}">
      <input data-field-prop="label" value="${escapeHtml(field.label || '')}" placeholder="Label">
      <input data-field-prop="key" value="${escapeHtml(field.key || '')}" placeholder="key">
      <select data-field-prop="type">
        ${['text', 'textarea', 'date', 'select', 'radio', 'checkbox', 'file'].map((t) => `<option value="${t}" ${field.type === t ? 'selected' : ''}>${t}</option>`).join('')}
      </select>
      <label class="workflow-check"><input type="checkbox" data-field-required ${field.required ? 'checked' : ''}> Required</label>
      <input data-field-prop="optionsText" value="${escapeHtml(options)}" placeholder="Options, comma separated">
      <button type="button" class="workflow-icon-btn" data-remove-field="${idx}" title="Remove field">x</button>
    </div>
  `;
}

function renderStep(step, idx) {
  return `
    <div class="workflow-step-row" data-step-index="${idx}">
      <div class="workflow-step-head">
        <strong>Step ${idx + 1}</strong>
        <button type="button" class="workflow-icon-btn" data-remove-step="${idx}" title="Remove step">x</button>
      </div>
      <div class="workflow-form-grid">
        <label>Model<input data-step-prop="model" list="workflow-model-list" value="${escapeHtml(step.model || '')}" placeholder="${escapeHtml(state.defaultModel || 'model')}"></label>
        <label>Max tokens<input data-step-prop="maxTokens" type="number" min="1" max="8192" value="${escapeHtml(step.maxTokens || 1500)}"></label>
        <label class="workflow-span-2">System prompt<textarea data-step-prop="systemPrompt" rows="3">${escapeHtml(step.systemPrompt || '')}</textarea></label>
        <label class="workflow-span-2">User template<textarea data-step-prop="userTemplate" rows="5">${escapeHtml(step.userTemplate || '')}</textarea></label>
      </div>
    </div>
  `;
}

function renderTestInputs(wf) {
  return (wf.fields || []).map((field) => {
    const key = escapeHtml(field.key || '');
    const label = escapeHtml(field.label || field.key || '');
    if (field.type === 'textarea') {
      return `<label>${label}<textarea data-test-input="${key}" rows="4" placeholder="${escapeHtml(field.placeholder || '')}">${escapeHtml(field.defaultValue || '')}</textarea></label>`;
    }
    if (field.type === 'select' || field.type === 'radio') {
      return `<label>${label}<select data-test-input="${key}"><option value=""></option>${(field.options || []).map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join('')}</select></label>`;
    }
    if (field.type === 'checkbox') {
      return `<fieldset><legend>${label}</legend>${(field.options || []).map((o) => `<label class="workflow-check"><input type="checkbox" data-test-check="${key}" value="${escapeHtml(o)}"> ${escapeHtml(o)}</label>`).join('')}</fieldset>`;
    }
    if (field.type === 'file') {
      return `<label>${label}<textarea data-test-input="${key}" rows="3" placeholder="Paste extracted text for local tests"></textarea></label>`;
    }
    return `<label>${label}<input data-test-input="${key}" type="${field.type === 'date' ? 'date' : 'text'}" value="${escapeHtml(field.defaultValue || '')}" placeholder="${escapeHtml(field.placeholder || '')}"></label>`;
  }).join('') || '<div class="workflow-empty">This workflow has no inputs.</div>';
}

function renderRuns() {
  return `
    <div class="workflow-table-pane">
      <div class="workflow-heading-row">
        <h5>Runs</h5>
        <button type="button" class="workflow-btn" id="workflow-refresh-runs">Refresh</button>
      </div>
      <div class="workflow-run-list">
        ${state.runs.map((run) => `
          <div class="workflow-run-item">
            <div>
              <strong>${escapeHtml(run.workflowName || run.workflowId || 'Workflow')}</strong>
              <span class="workflow-badge ${run.status === 'ok' ? '' : 'muted'}">${escapeHtml(run.status || '')}</span>
            </div>
            <small>${formatTime(run.startedAt)} - ${escapeHtml(run.triggeredBy || '')}</small>
            ${run.error ? `<pre>${escapeHtml(run.error)}</pre>` : ''}
            ${(run.steps || []).map((s, idx) => `<details><summary>Step ${idx + 1}: ${escapeHtml(s.status || '')}</summary><pre>${escapeHtml(s.output || s.error || '')}</pre></details>`).join('')}
          </div>
        `).join('') || '<div class="workflow-empty">No runs yet.</div>'}
      </div>
    </div>
  `;
}

function renderArtifacts() {
  return `
    <div class="workflow-table-pane">
      <div class="workflow-heading-row">
        <h5>Artifacts</h5>
        <button type="button" class="workflow-btn" id="workflow-refresh-artifacts">Refresh</button>
      </div>
      <div class="workflow-artifact-list">
        ${state.artifacts.map((artifact) => `
          <details class="workflow-artifact-item">
            <summary>
              <strong>${escapeHtml(artifact.title || 'Artifact')}</strong>
              <small>${formatTime(artifact.createdAt)}</small>
            </summary>
            <pre>${escapeHtml(artifact.content || '')}</pre>
          </details>
        `).join('') || '<div class="workflow-empty">No artifacts yet.</div>'}
      </div>
    </div>
  `;
}

function formatTime(ms) {
  const n = Number(ms || 0);
  if (!n) return '';
  try { return new Date(n).toLocaleString(); } catch { return ''; }
}

function wireBody(body) {
  body.querySelectorAll('[data-workflow-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.tab = btn.dataset.workflowTab;
      render();
    });
  });
  $('#workflow-refresh', body)?.addEventListener('click', () => refreshAll());
  $('#workflow-refresh-runs', body)?.addEventListener('click', () => refreshRuns());
  $('#workflow-refresh-artifacts', body)?.addEventListener('click', () => refreshArtifacts());
  $('#workflow-save', body)?.addEventListener('click', saveWorkflows);
  $('#workflow-new', body)?.addEventListener('click', addWorkflow);
  $('#workflow-delete', body)?.addEventListener('click', deleteWorkflow);
  $('#workflow-add-field', body)?.addEventListener('click', addField);
  $('#workflow-add-step', body)?.addEventListener('click', addStep);
  $('#workflow-run', body)?.addEventListener('click', runSelectedWorkflow);
  $('#workflow-publish', body)?.addEventListener('click', publishSelected);
  $('#workflow-unpublish', body)?.addEventListener('click', unpublishSelected);
  $('#workflow-pull-public', body)?.addEventListener('click', pullPublic);

  body.querySelectorAll('[data-select-workflow]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.selectedId = btn.dataset.selectWorkflow;
      render();
    });
  });
  body.querySelectorAll('[data-wf-prop]').forEach((input) => {
    input.addEventListener('input', () => {
      const wf = currentWorkflow();
      if (!wf) return;
      wf[input.dataset.wfProp] = input.value;
      markDirty();
    });
  });
  $('[data-trigger-kind]', body)?.addEventListener('change', (e) => {
    const wf = currentWorkflow();
    if (!wf) return;
    wf.trigger = e.target.value === 'scheduled' ? { kind: 'scheduled', cron: '0 * * * *' } : { kind: 'on_event' };
    markDirty();
    render();
  });
  $('[data-trigger-cron]', body)?.addEventListener('input', (e) => {
    const wf = currentWorkflow();
    if (!wf) return;
    wf.trigger = { kind: 'scheduled', cron: e.target.value };
    markDirty();
  });
  body.querySelectorAll('[data-field-index]').forEach((row) => wireFieldRow(row));
  body.querySelectorAll('[data-step-index]').forEach((row) => wireStepRow(row));
  body.querySelectorAll('[data-remove-field]').forEach((btn) => {
    btn.addEventListener('click', () => removeField(Number(btn.dataset.removeField)));
  });
  body.querySelectorAll('[data-remove-step]').forEach((btn) => {
    btn.addEventListener('click', () => removeStep(Number(btn.dataset.removeStep)));
  });
}

function wireFieldRow(row) {
  const idx = Number(row.dataset.fieldIndex);
  row.querySelectorAll('[data-field-prop]').forEach((input) => {
    input.addEventListener('input', () => {
      const wf = currentWorkflow();
      const field = wf?.fields?.[idx];
      if (!field) return;
      const prop = input.dataset.fieldProp;
      if (prop === 'optionsText') {
        field.options = input.value.split(',').map((v) => v.trim()).filter(Boolean);
      } else {
        field[prop] = input.value;
      }
      markDirty();
    });
  });
  row.querySelector('[data-field-required]')?.addEventListener('change', (e) => {
    const wf = currentWorkflow();
    const field = wf?.fields?.[idx];
    if (!field) return;
    field.required = e.target.checked;
    markDirty();
  });
}

function wireStepRow(row) {
  const idx = Number(row.dataset.stepIndex);
  row.querySelectorAll('[data-step-prop]').forEach((input) => {
    input.addEventListener('input', () => {
      const wf = currentWorkflow();
      const step = wf?.steps?.[idx];
      if (!step) return;
      const prop = input.dataset.stepProp;
      if (prop === 'maxTokens') {
        step.maxTokens = Math.max(1, Math.min(8192, Number(input.value || 1500)));
      } else {
        step[prop] = input.value;
      }
      const modelInfo = prop === 'model' ? state.models.find((m) => m.model === input.value) : null;
      if (modelInfo?.endpointId) step.endpointId = modelInfo.endpointId;
      markDirty();
    });
  });
}

async function saveWorkflows() {
  try {
    await api('/api/workflows', {
      method: 'PUT',
      headers: { 'If-Match': String(state.snapshot.version || 0) },
      body: JSON.stringify({ workflows: state.snapshot.workflows || [] }),
    });
    await loadAll({ force: true });
    showToast('Workflows saved');
    return true;
  } catch (err) {
    if (err.status === 409 && err.body) {
      state.snapshot = err.body;
      state.selectedId = state.snapshot.workflows?.[0]?.id || '';
      state.dirty = false;
      render();
      showError('Workflows changed on disk. Reloaded.');
      return false;
    }
    showError(err.message);
    return false;
  }
}

function addWorkflow() {
  const wf = makeWorkflow();
  state.snapshot.workflows = [...(state.snapshot.workflows || []), wf];
  state.selectedId = wf.id;
  markDirty();
  render();
}

function deleteWorkflow() {
  const wf = currentWorkflow();
  if (!wf) return;
  if (!window.confirm(`Delete workflow "${wf.name || wf.id}"?`)) return;
  state.snapshot.workflows = (state.snapshot.workflows || []).filter((item) => item.id !== wf.id);
  state.selectedId = state.snapshot.workflows[0]?.id || '';
  markDirty();
  render();
}

function addField() {
  const wf = currentWorkflow();
  if (!wf) return;
  wf.fields = wf.fields || [];
  wf.fields.push({ id: newId('field'), key: `input_${wf.fields.length + 1}`, label: 'Input', type: 'text', required: false });
  markDirty();
  render();
}

function removeField(idx) {
  const wf = currentWorkflow();
  if (!wf?.fields) return;
  wf.fields.splice(idx, 1);
  markDirty();
  render();
}

function addStep() {
  const wf = currentWorkflow();
  if (!wf) return;
  wf.steps = wf.steps || [];
  if (wf.steps.length >= 4) {
    showError('Max 4 steps');
    return;
  }
  wf.steps.push({
    id: newId('step'),
    kind: 'llm',
    model: state.defaultModel || '',
    endpointId: state.defaultEndpointId || '',
    systemPrompt: 'You are a precise assistant.',
    userTemplate: wf.fields?.[0]?.key ? `{{inputs.${wf.fields[0].key}}}` : '',
    maxTokens: 1500,
  });
  markDirty();
  render();
}

function removeStep(idx) {
  const wf = currentWorkflow();
  if (!wf?.steps) return;
  if (wf.steps.length <= 1) {
    showError('Workflow needs at least one step');
    return;
  }
  wf.steps.splice(idx, 1);
  markDirty();
  render();
}

function collectInputs(root) {
  const inputs = {};
  root.querySelectorAll('[data-test-input]').forEach((el) => {
    inputs[el.dataset.testInput] = el.value;
  });
  root.querySelectorAll('[data-test-check]').forEach((el) => {
    const key = el.dataset.testCheck;
    if (!inputs[key]) inputs[key] = [];
    if (el.checked) inputs[key].push(el.value);
  });
  return inputs;
}

async function runSelectedWorkflow() {
  const wf = currentWorkflow();
  if (!wf) return;
  const inputs = collectInputs(ensureModal());
  if (state.dirty) {
    const saved = await saveWorkflows();
    if (!saved) return;
  }
  const modal = ensureModal();
  const resultEl = $('#workflow-run-result', modal);
  state.running = true;
  render();
  try {
    const data = await api(`/api/workflows/${encodeURIComponent(wf.id)}/run`, {
      method: 'POST',
      body: JSON.stringify({ inputs }),
    });
    await refreshRuns(false);
    await refreshArtifacts(false);
    state.tab = 'runs';
    showToast(data.run?.status === 'ok' ? 'Workflow finished' : 'Workflow failed');
  } catch (err) {
    if (resultEl) resultEl.textContent = err.message;
    showError(err.message);
  } finally {
    state.running = false;
    render();
  }
}

async function publishSelected() {
  const wf = currentWorkflow();
  if (!wf) return;
  try {
    if (state.dirty) {
      const saved = await saveWorkflows();
      if (!saved) return;
    }
    const data = await api('/api/publish', {
      method: 'POST',
      body: JSON.stringify({ workflowId: wf.id }),
    });
    if (data.publicUrl) state.publicUrls.set(wf.id, data.publicUrl);
    await loadAll({ force: true });
    state.selectedId = wf.id;
    showToast('Workflow published');
  } catch (err) {
    showError(err.message);
  }
}

async function unpublishSelected() {
  const wf = currentWorkflow();
  if (!wf) return;
  try {
    await api(`/api/publish/${encodeURIComponent(wf.id)}`, { method: 'DELETE' });
    state.publicUrls.delete(wf.id);
    await loadAll({ force: true });
    state.selectedId = wf.id;
    showToast('Workflow unpublished');
  } catch (err) {
    showError(err.message);
  }
}

async function pullPublic() {
  try {
    const data = await api('/api/workflows/pull-public', { method: 'POST', body: '{}' });
    await refreshRuns(false);
    await refreshArtifacts(false);
    showToast(`Pulled ${data.processed || 0}`);
  } catch (err) {
    showError(err.message);
  }
}

async function refreshAll() {
  await loadAll({ force: true });
  showToast('Workflows refreshed');
}

async function refreshRuns(toast = true) {
  const data = await api('/api/runs').catch(() => ({ runs: [] }));
  state.runs = Array.isArray(data.runs) ? data.runs : [];
  if (toast) showToast('Runs refreshed');
  render();
}

async function refreshArtifacts(toast = true) {
  const data = await api('/api/artifacts').catch(() => ({ artifacts: [] }));
  state.artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
  if (toast) showToast('Artifacts refreshed');
  render();
}

export async function openWorkflows() {
  const modal = ensureModal();
  modal.classList.remove('hidden');
  if (location.pathname !== '/workflows') {
    history.pushState({ modal: 'workflows' }, '', '/workflows');
  }
  await loadAll();
  render();
}

function closeWorkflows() {
  const modal = ensureModal();
  modal.classList.add('hidden');
  if (location.pathname === '/workflows') {
    history.pushState({}, '', '/');
  }
}

function init() {
  ensureModal();
  $('#tool-workflows-btn')?.addEventListener('click', openWorkflows);
  if (location.pathname === '/workflows') {
    setTimeout(openWorkflows, 0);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

window.openWorkflows = openWorkflows;
