// "Add Verified Connection", "Add Verified Entity" and "Add identifier" forms.
// Client-side checks mirror governance.json for instant feedback; the server re-validates everything.
import { state, emit, on, nodeById, hasIdentity, isSuperseded, domainKeys, domainLetter, domainColor, esc, toast } from './state.js';
import { swatch } from './shapes.js';
import { api } from './api.js';
import { entityOptions } from './paths.js';

const $ = (form, name) => form.elements.namedItem(name);
const today = () => new Date().toISOString().slice(0, 10);

function lei_ok(lei) {
  let s = ''; for (const c of lei) s += parseInt(c, 36).toString();
  let r = 0; for (const ch of s) r = (r * 10 + Number(ch)) % 97;
  return r === 1;
}

function idProblem(key, value) {
  const g = state.gov.identity;
  if (!new RegExp(g.patterns[key]).test(value)) return `not a valid ${key}`;
  if (key === 'gleif_lei' && !lei_ok(value)) return 'fails the ISO 17442 check digits';
  return null;
}

function normId(key, v) {
  v = String(v || '').trim();
  if (key === 'sec_cik' && /^\d+$/.test(v)) v = v.padStart(10, '0');
  if (key === 'gleif_lei' || key === 'canonical_id') v = v.toUpperCase();
  if (key === 'doi') v = v.replace(/^https?:\/\/(dx\.)?doi\.org\//i, '').toLowerCase();
  return v;
}

// field-name mapping: server error field -> form control name
const FIELD = {
  'transparency.verification_source': 'verification_source', 'transparency.source_reference': 'source_reference',
  'transparency.citation_url': 'citation_url', 'transparency.document_date': 'document_date',
  'transparency.tenure_start': 'tenure_start', 'transparency.tenure_end': 'tenure_end', 'transparency.active': 'active',
  'integrity.control_type': 'control_type', 'integrity.control_class': 'control_class',
  'integrity.conflict_weight': 'conflict_weight', 'supersedes.reason': 'supersede_reason', 'supersedes.edge_id': 'do_supersede',
};

function showErrors(form, listEl, errs) {
  form.querySelectorAll('.field-err').forEach(e => e.remove());
  form.querySelectorAll('.invalid').forEach(e => e.classList.remove('invalid'));
  const general = [];
  for (const { field, message } of errs) {
    const name = FIELD[field] || field.split('.').pop();
    let el = $(form, name);
    if (el && el.length && !el.tagName) el = el[0];               // radio group
    if (el && el.tagName) {
      el.classList.add('invalid');
      const host = el.closest('.radio-cards, .chip-picks') || el.closest('label') || el.parentElement;
      host.insertAdjacentHTML('beforeend', `<span class="field-err">${esc(message)}</span>`);
    } else general.push(`${field}: ${message}`);
  }
  listEl.innerHTML = general.map(m => `<li>${esc(m)}</li>`).join('');
}

// ================================================================ edge form
export function initEdgeForm() {
  const form = document.getElementById('edge-form');
  const errList = document.getElementById('edge-errors');
  const st = state.gov.transparency.source_types;

  $(form, 'verification_source').innerHTML = '<option value="">Choose a primary source…</option>' +
    Object.entries(st).map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join('');
  document.getElementById('control-classes').innerHTML = Object.entries(state.gov.integrity.control_classes).map(([k, v]) =>
    `<label><input type="radio" name="control_class" value="${k}"><span><b>${k}</b><small>${esc(v)}</small></span></label>`).join('');

  const fillLists = () => {
    entityOptions($(form, 'source'), { value: $(form, 'source').value });
    entityOptions($(form, 'target'), { value: $(form, 'target').value });
    const uniq = arr => [...new Set(arr.filter(Boolean))].sort();
    document.getElementById('rel-types').innerHTML = uniq(state.graph.edges.map(e => e.relationship_type)).map(v => `<option value="${esc(v)}">`).join('');
    document.getElementById('control-types').innerHTML = uniq(state.graph.edges.map(e => e.integrity?.control_type)).map(v => `<option value="${esc(v)}">`).join('');
  };
  fillLists();
  on('graph-loaded', fillLists);

  const refLabel = document.getElementById('ref-label');
  $(form, 'verification_source').addEventListener('change', () => {
    const r = st[$(form, 'verification_source').value];
    refLabel.textContent = '● ' + (r ? r.reference_label : 'Source reference');
    $(form, 'source_reference').placeholder = r ? r.reference_label : '';
    $(form, 'citation_url').placeholder = r && r.url_hosts.length ? `https://${r.url_hosts[r.url_hosts.length - 1]}/…` : 'https://';
  });

  const range = $(form, 'conflict_weight_range'), num = $(form, 'conflict_weight'), out = document.getElementById('w-out');
  range.addEventListener('input', () => { num.value = Number(range.value).toFixed(2); out.textContent = num.value; });
  num.addEventListener('input', () => { range.value = num.value; out.textContent = num.value; });
  $(form, 'document_date').max = today();

  const dupCheck = () => {
    const s = $(form, 'source').value, t = $(form, 'target').value, r = $(form, 'relationship_type').value.trim().toUpperCase();
    const dup = state.graph.edges.find(e => !isSuperseded(e) && e.source === s && e.target === t && e.relationship_type === r);
    const box = document.getElementById('supersede-box');
    box.hidden = !dup;
    box.dataset.edge = dup ? dup.edge_id : '';
    if (dup) document.getElementById('supersede-msg').innerHTML =
      `<b>${esc(dup.edge_id)}</b> already records ${esc(r)} between these entities (source: ${esc(dup.transparency?.verification_source || '—')}). ` +
      'Saving a new one requires superseding it explicitly. The old edge stays in the file, marked superseded.';
  };
  ['source', 'target', 'relationship_type'].forEach(n => $(form, n).addEventListener('input', dupCheck));
  ['source', 'target'].forEach(n => $(form, n).addEventListener('change', () => {
    const node = nodeById($(form, n).value);
    const hint = form.querySelector('[data-hint=endpoints]');
    const missing = ['source', 'target'].map(k => nodeById($(form, k).value)).filter(x => x && !hasIdentity(x));
    hint.innerHTML = missing.length
      ? `<span class="bad">${missing.map(m => esc(m.label)).join(' and ')} ${missing.length > 1 ? 'have' : 'has'} no canonical identifier yet.</span>
         <button type="button" class="link" data-fix="${missing[0].id}">Add one →</button>`
      : 'Both entities must already have a canonical identifier (sec_cik, gleif_lei, doi or canonical_id).';
    hint.querySelector('[data-fix]')?.addEventListener('click', e => emit('fix-identity', e.target.dataset.fix));
    void node;
  }));

  on('connect-from', id => {
    $(form, 'source').value = id;
    $(form, 'source').dispatchEvent(new Event('change'));
    emit('tab', 'add');
  });

  form.addEventListener('reset', () => setTimeout(() => {
    showErrors(form, errList, []); out.textContent = '0.50';
    document.getElementById('supersede-box').hidden = true;
  }));

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const { payload, errors } = collectEdge(form);
    showErrors(form, errList, errors);
    if (errors.length) { form.querySelector('.invalid')?.focus(); return; }
    const btn = document.getElementById('edge-submit');
    btn.disabled = true;
    try {
      const { edge } = await api.addEdge(payload);
      toast(`Saved ${edge.edge_id} · hash ${edge.integrity.verification_hash.slice(0, 12)}…`);
      form.reset();
      emit('reload', { select: { kind: 'edge', id: edge.edge_id } });
    } catch (e) {
      showErrors(form, errList, e.errors.length ? e.errors : [{ field: 'request', message: e.message }]);
    } finally { btn.disabled = false; }
  });
}

function collectEdge(form) {
  const g = state.gov, errs = [];
  const v = n => ($(form, n).value || '').trim();
  const src = v('source'), tgt = v('target');
  for (const [f, id] of [['source', src], ['target', tgt]]) {
    const n = nodeById(id);
    if (!n) errs.push({ field: f, message: 'pick an existing entity' });
    else if (g.integrity.require_endpoint_identity && !hasIdentity(n)) errs.push({ field: f, message: 'has no valid canonical identifier yet' });
  }
  if (src && src === tgt) errs.push({ field: 'target', message: 'source and target must differ' });
  const tok = new RegExp(g.integrity.token_pattern);
  const rtype = v('relationship_type').toUpperCase().replace(/\s+/g, '_');
  if (!tok.test(rtype)) errs.push({ field: 'relationship_type', message: 'UPPER_SNAKE_CASE, e.g. BOARD_INTERLOCK' });

  const stype = v('verification_source'), rules = g.transparency.source_types[stype];
  if (!rules) errs.push({ field: 'transparency.verification_source', message: 'choose a primary source type' });
  let ref = v('source_reference').replace(/\s+/g, ' ');
  if (['DEF 14A', '13F-HR', 'NIH RePORTER Grant ID'].includes(stype)) ref = ref.toUpperCase().replace(/ /g, '');
  if (rules && !ref) errs.push({ field: 'transparency.source_reference', message: `required: ${rules.reference_label}` });
  else if (rules && !new RegExp(rules.reference_pattern).test(ref)) errs.push({ field: 'transparency.source_reference', message: `doesn't look like a ${rules.reference_label}` });
  const url = v('citation_url');
  let host = null;
  try { const u = new URL(url); if (u.protocol !== 'https:') throw 0; host = u.hostname; } catch { errs.push({ field: 'transparency.citation_url', message: 'a full https:// link to the primary document' }); }
  if (host && rules && rules.url_hosts.length && !rules.url_hosts.some(h => host === h || host.endsWith('.' + h)))
    errs.push({ field: 'transparency.citation_url', message: `a ${stype} citation should link to ${rules.url_hosts.join(' or ')}` });
  const dd = v('document_date');
  if (!dd) errs.push({ field: 'transparency.document_date', message: 'required' });
  else if (dd > today()) errs.push({ field: 'transparency.document_date', message: "can't be in the future" });
  const ts = v('tenure_start'), te = v('tenure_end'), active = $(form, 'active').checked;
  if (ts && te && ts > te) errs.push({ field: 'transparency.tenure_end', message: 'ends before it starts' });
  if (active && te && te < today()) errs.push({ field: 'transparency.active', message: 'marked active but tenure ended' });

  const ctype = v('control_type').toUpperCase().replace(/\s+/g, '_');
  if (!tok.test(ctype)) errs.push({ field: 'integrity.control_type', message: 'UPPER_SNAKE_CASE, e.g. FIDUCIARY_GOVERNANCE' });
  const cclass = form.querySelector('input[name=control_class]:checked')?.value;
  if (!cclass) errs.push({ field: 'integrity.control_class', message: 'choose hard or soft' });
  const w = Number(v('conflict_weight'));
  if (v('conflict_weight') === '' || Number.isNaN(w) || w < g.integrity.conflict_weight_min || w > g.integrity.conflict_weight_max)
    errs.push({ field: 'integrity.conflict_weight', message: '0.00–1.00' });
  else if (Math.round(w * 100) / 100 !== w) errs.push({ field: 'integrity.conflict_weight', message: 'at most 2 decimals' });

  const box = document.getElementById('supersede-box');
  let supersedes = null;
  if (!box.hidden) {
    if (!$(form, 'do_supersede').checked) errs.push({ field: 'supersedes.edge_id', message: `tick to supersede ${box.dataset.edge}, or change the relationship` });
    else {
      const reason = v('supersede_reason');
      if (reason.length < 10) errs.push({ field: 'supersedes.reason', message: 'explain the new evidence (10+ characters)' });
      supersedes = { edge_id: box.dataset.edge, reason };
    }
  }
  const payload = {
    source: src, target: tgt, relationship_type: rtype, human_bridge: v('human_bridge'),
    transparency: { verification_source: stype, source_reference: ref, citation_url: url, document_date: dd, active,
                    tenure_start: ts || null, tenure_end: te || null },
    integrity: { control_type: ctype, control_class: cclass, conflict_weight: w },
  };
  if (supersedes) payload.supersedes = supersedes;
  return { payload, errors: errs };
}

// ================================================================ entity + identifier forms
export function initNodeForms() {
  const form = document.getElementById('node-form'), errList = document.getElementById('node-errors');
  const keys = state.gov.identity.accepted_keys;
  document.getElementById('node-doms').innerHTML = domainKeys().map(d =>
    `<label title="${esc(state.graph.schema_metadata.domains[d])}"><input type="checkbox" name="domains" value="${d}">${swatch(d)}${domainLetter(d)}</label>`).join('');
  const ph = { sec_cik: '0000936468', gleif_lei: '20-character LEI', doi: '10.xxxx/…', canonical_id: 'US-GOV-AGENCY' };
  document.getElementById('node-ids').innerHTML = keys.map(k => `<span>${k}</span><input name="id_${k}" placeholder="${ph[k] || ''}">`).join('');
  const fillTypes = () => {
    document.getElementById('entity-types').innerHTML = [...new Set(state.graph.nodes.map(n => n.entity_type))].sort().map(t => `<option value="${esc(t)}">`).join('');
  };
  fillTypes();
  on('graph-loaded', fillTypes);

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const errs = [];
    const label = $(form, 'label').value.trim();
    if (!label) errs.push({ field: 'label', message: 'required' });
    const etype = $(form, 'entity_type').value.trim().toUpperCase().replace(/\s+/g, '_');
    if (!new RegExp(state.gov.integrity.token_pattern).test(etype)) errs.push({ field: 'entity_type', message: 'UPPER_SNAKE_CASE, e.g. CORPORATION' });
    const domains = [...form.querySelectorAll('input[name=domains]:checked')].map(i => i.value);
    if (!domains.length) errs.push({ field: 'domains', message: 'pick at least one domain' });
    const identity = {};
    for (const k of keys) {
      const val = normId(k, $(form, `id_${k}`).value);
      if (!val) continue;
      const p = idProblem(k, val);
      if (p) errs.push({ field: `id_${k}`, message: p });
      const owner = state.graph.nodes.find(n => String((n.identity || {})[k] || '') === val);
      if (owner) errs.push({ field: `id_${k}`, message: `already belongs to ${owner.label}` });
      identity[k] = val;
    }
    if (!Object.keys(identity).length) errs.push({ field: 'identity', message: 'enter at least one canonical identifier' });
    const fixed = errs.map(e => ({ ...e, field: e.field.startsWith('identity.') ? 'id_' + e.field.split('.')[1] : e.field }));
    showErrors(form, errList, fixed);
    if (errs.length) return;
    try {
      const { node } = await api.addNode({ label, entity_type: etype, domains, identity,
        leadership: $(form, 'leadership').value.split('\n') });
      toast(`Saved ${node.id}`);
      form.reset();
      emit('reload', { select: { kind: 'node', id: node.id } });
    } catch (e) {
      showErrors(form, errList, e.errors.map(x => ({ ...x, field: x.field.startsWith('identity.') ? 'id_' + x.field.split('.')[1] : x.field })));
    }
  });

  const iform = document.getElementById('ident-form'), ierr = document.getElementById('ident-errors');
  $(iform, 'key').innerHTML = keys.map(k => `<option>${k}</option>`).join('');
  const fillNodes = () => entityOptions($(iform, 'node_id'), { value: $(iform, 'node_id').value });
  fillNodes();
  on('graph-loaded', fillNodes);
  on('fix-identity', id => { $(iform, 'node_id').value = id; emit('tab', 'entity'); $(iform, 'value').focus(); });

  iform.addEventListener('submit', async ev => {
    ev.preventDefault();
    const key = $(iform, 'key').value, val = normId(key, $(iform, 'value').value), nid = $(iform, 'node_id').value;
    const errs = [];
    if (!nid) errs.push({ field: 'node_id', message: 'choose an entity' });
    if (!val) errs.push({ field: 'value', message: 'required' });
    else { const p = idProblem(key, val); if (p) errs.push({ field: 'value', message: p }); }
    const existing = nid && (nodeById(nid).identity || {})[key];
    if (existing && existing !== val) errs.push({ field: 'value', message: `${key} is already ${existing}; identifiers can't be overwritten` });
    showErrors(iform, ierr, errs);
    if (errs.length) return;
    try {
      await api.addIdentity({ node_id: nid, identity: { [key]: val }, evidence_url: $(iform, 'evidence_url').value });
      toast(`Added ${key} to ${nodeById(nid).label}`);
      iform.reset();
      emit('reload', { select: { kind: 'node', id: nid } });
    } catch (e) {
      showErrors(iform, ierr, e.errors.map(x => ({ ...x, field: 'value' })));
    }
  });
}
