// Review tab: approve staged HOOT batches row by row. Domains and weights are the reviewer's call;
// suggestions are shown and labelled, never applied silently.
import { state, emit, on, domainColor, esc, safeUrl, toast } from './state.js';
import { swatch } from './shapes.js';

const get = url => fetch(url).then(r => r.json());
let batch = null;

export function initReview() {
  on('tab', name => { if (name === 'review') loadList(); });
}

async function loadList() {
  const box = document.getElementById('review');
  const { batches } = await get('/api/staging');
  if (!batches.length) {
    box.innerHTML = '<p class="muted small">No staged batches. Create one with<br><code>python src\\ingest\\hoot_bridge.py ENT_MCDONALDS</code></p>';
    return;
  }
  box.innerHTML = `<label>Batch<select id="rv-batch">${batches.map(b =>
    `<option value="${esc(b.batch_id)}">${esc(b.batch_id)} · ${esc(b.centers.join(', '))} · ${b.edges} edges${b.applied ? ' · applied ' + b.applied + '×' : ''}</option>`).join('')}</select></label>
    <div id="rv-body"></div>`;
  const sel = document.getElementById('rv-batch');
  sel.addEventListener('change', () => loadBatch(sel.value));
  loadBatch(sel.value);
}

const domBoxes = (key, chosen, suggested) => Object.keys(state.graph.schema_metadata.domains).map(d =>
  `<label title="${esc(state.graph.schema_metadata.domains[d])}${suggested.includes(d) ? ' (suggested)' : ''}">
     <input type="checkbox" data-node="${esc(key)}" value="${d}" ${chosen.includes(d) ? 'checked' : ''}>
     ${swatch(d)}${esc(d.replace('DOM_', '').replace('BRIDGE_HUMAN', 'Bridge'))}${suggested.includes(d) ? '*' : ''}</label>`).join('');

async function loadBatch(id) {
  batch = await get('/api/staging/' + encodeURIComponent(id));
  const body = document.getElementById('rv-body');
  const nodes = Object.fromEntries(batch.nodes.map(n => [n.key, n]));
  const applied = new Set((batch.applied || []).flatMap(a => a.edges_keys || []));
  const edges = [...batch.edges].sort((a, b) => a.source_label.localeCompare(b.source_label) || a.target_label.localeCompare(b.target_label));
  const newNodes = batch.nodes.filter(n => n.status === 'new');

  body.innerHTML = `
    <p class="small muted">${esc(batch.instructions)}</p>
    ${batch.corroborations.length ? `<h3>Corroborates existing edges (${batch.corroborations.length})</h3>` + batch.corroborations.map(c =>
      `<div class="edge-card small"><b>${esc(c.master_edge)}</b> · ${esc(c.person)}: ${c.evidence.map(e => `${esc(e.role)} at ${esc(e.org)} (<a href="${esc(safeUrl(e.filing) || '#')}" target="_blank" rel="noopener noreferrer">${esc(e.date)}</a>)`).join(' and ')}</div>`).join('') : ''}
    <h3>New entities (${newNodes.length})</h3>
    <p class="small muted">Tick the domains for each entity you'll use. <b>*</b> = suggested: for people, from the companies they file for; for companies, from your own earlier classification (Connections.xlsx) when it lists them. Otherwise classifying companies is your call.</p>
    ${newNodes.map(n => `<div class="edge-card small" data-nodecard="${esc(n.key)}">
        <b>${esc(n.label)}</b> <span class="chip">${esc(n.entity_type)}</span>
        <div class="muted">${Object.entries(n.identity).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(' · ')}${n.info && n.info.industry ? ' · ' + esc(n.info.industry) : ''}</div>
        ${n.suggestion_basis ? `<div class="muted">* suggested from ${esc(n.suggestion_basis)}</div>` : ''}
        <div class="chip-picks">${domBoxes(n.key, n.suggested_domains || [], n.suggested_domains || [])}</div></div>`).join('')}
    <h3>Connections (${edges.length})</h3>
    <div class="row small">
      <button type="button" class="link" id="rv-all">Select all</button>
      <button type="button" class="link" id="rv-active">Select active only</button>
      <button type="button" class="link" id="rv-none">Clear</button>
      <button type="button" id="rv-suggest">Use suggested weights for selected</button>
    </div>
    ${edges.map(e => {
      const t = e.transparency, s = e.supplement, w = s.evidence_window || {};
      const url = safeUrl(t.citation_url);
      return `<div class="edge-card small" data-edgecard="${esc(e.key)}">
        <label class="check"><input type="checkbox" data-approve="${esc(e.key)}">
          <span><b>${esc(e.source_label)}</b> ${esc(e.relationship_type)}${s.role_title ? ' (' + esc(s.role_title) + ')' : ''} <b>${esc(e.target_label)}</b>
          ${s.percent_of_class != null ? ' · ' + esc(s.percent_of_class) + '%' : ''}</span></label>
        <div class="muted">${t.active ? '<span class="ok">active</span>' : '<span class="bad">historical</span>'} · ${esc(s.activity_basis)}</div>
        <div class="muted">${esc(t.verification_source)} <code>${esc(t.source_reference)}</code> · ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">filing ${esc(t.document_date)}</a>` : ''} · ${w.filings_seen || 0} filings ${esc(w.first_filing || '')}→${esc(w.last_filing || '')}</div>
        <div class="weight-row"><span class="muted">${esc(e.integrity.control_type)} (${esc(e.integrity.control_class)}) · suggested ${e.integrity.suggested_conflict_weight}</span>
          <input type="number" min="0" max="1" step="0.01" data-weight="${esc(e.key)}" placeholder="weight" aria-label="Conflict weight"></div>
      </div>`;
    }).join('')}
    <ul class="errors" id="rv-errors"></ul>
    <button type="button" class="primary" id="rv-apply">Validate &amp; add approved rows</button>
    <div id="rv-result" class="small"></div>`;

  const boxes = () => [...body.querySelectorAll('[data-approve]')];
  body.querySelector('#rv-all').onclick = () => boxes().forEach(b => { b.checked = true; });
  body.querySelector('#rv-none').onclick = () => boxes().forEach(b => { b.checked = false; });
  body.querySelector('#rv-active').onclick = () => boxes().forEach(b => {
    b.checked = batch.edges.find(e => e.key === b.dataset.approve).transparency.active;
  });
  body.querySelector('#rv-suggest').onclick = () => boxes().filter(b => b.checked).forEach(b => {
    const e = batch.edges.find(x => x.key === b.dataset.approve);
    body.querySelector(`[data-weight="${CSS.escape(e.key)}"]`).value = e.integrity.suggested_conflict_weight.toFixed(2);
  });
  body.querySelector('#rv-apply').onclick = () => apply(body, nodes);
  void applied;
}

async function apply(body, nodes) {
  const errs = [];
  const decisions = { nodes: {}, edges: {} };
  const used = new Set();
  for (const b of body.querySelectorAll('[data-approve]:checked')) {
    const e = batch.edges.find(x => x.key === b.dataset.approve);
    const wEl = body.querySelector(`[data-weight="${CSS.escape(e.key)}"]`);
    const w = wEl.value === '' ? NaN : Number(wEl.value);
    if (Number.isNaN(w) || w < 0 || w > 1) { errs.push(`${e.source_label} → ${e.target_label}: set a conflict weight 0.00–1.00`); wEl.classList.add('invalid'); continue; }
    wEl.classList.remove('invalid');
    decisions.edges[e.key] = { approve: true, conflict_weight: Math.round(w * 100) / 100 };
    used.add(e.source_key); used.add(e.target_key);
  }
  for (const k of used) {
    if (nodes[k].status !== 'new') continue;
    const doms = [...body.querySelectorAll(`[data-node="${CSS.escape(k)}"]:checked`)].map(i => i.value);
    if (!doms.length) errs.push(`${nodes[k].label}: tick at least one domain`);
    decisions.nodes[k] = { domains: doms };
  }
  const list = body.querySelector('#rv-errors');
  list.innerHTML = errs.map(m => `<li>${esc(m)}</li>`).join('');
  if (errs.length || !Object.keys(decisions.edges).length) {
    if (!errs.length) list.innerHTML = '<li>Select at least one connection.</li>';
    return;
  }
  const res = await fetch(`/api/staging/${encodeURIComponent(batch.batch_id)}/apply`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(decisions) }).then(r => r.json());
  body.querySelector('#rv-result').innerHTML =
    `<p class="ok">Added ${res.nodes_added?.length || 0} entities and ${res.edges_added?.length || 0} connections.</p>` +
    (res.rejected?.length ? `<p class="bad">Rejected ${res.rejected.length}:</p><ul class="errors">` +
      res.rejected.map(r => `<li>${esc(r.row)}: ${esc(r.errors.map(e => e.message).join('; '))}</li>`).join('') + '</ul>' : '');
  toast(`Review applied: +${res.edges_added?.length || 0} connections`);
  emit('reload', {});
}
