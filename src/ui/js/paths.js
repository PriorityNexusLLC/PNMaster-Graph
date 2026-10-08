// Shortest-path search UI: entity -> entity or domain -> domain. The search runs in the Python engine.
import { state, emit, on, nodeById, domainKeys, domainLetter, domainColor, esc, safeUrl, akaOf } from './state.js';
import { swatch } from './shapes.js';
import { api } from './api.js';
import { fit } from './graph-view.js';
import { draftReport } from './panels.js';

let pmode = 'entity';
let lastResult = null;

export function entityOptions(selectEl, { placeholder = 'Choose an entity…', value } = {}) {
  // one A–Z list; people mixed in, marked by color and 👤; other known names shown after "aka"
  // each other name also gets its own A–Z line pointing to the same record ("Margo H. Georgiadis → Mary Margaret …"),
  // so typing either name finds the one host record
  const rows = [];
  for (const n of state.graph.nodes) {
    if (n.provenance && n.provenance.merged_into) continue;
    const aka = akaOf(n);
    const words = [...new Set([n.label, ...aka].flatMap(nameWords))];
    rows.push({ sort: n.label, n, host: true, words, text: esc(n.label) + (aka.length ? ` · aka ${esc(aka.slice(0, 2).join(', '))}` : '') });
    for (const a of aka) rows.push({ sort: a, n, host: false, words, text: `${esc(a)} → ${esc(n.label)}` });
  }
  rows.sort((a, b) => a.sort.localeCompare(b.sort, undefined, { sensitivity: 'base' }));
  const html = list => `<option value="">${list === rows ? placeholder : `${list.length} match${list.length === 1 ? '' : 'es'}: open to pick`}</option>` +
    list.map(({ n, text }) => n.entity_type === 'PERSON'
      ? `<option value="${n.id}" class="person">👤 ${text}</option>`
      : `<option value="${n.id}">${text}</option>`).join('');
  selectEl.innerHTML = html(rows);
  if (value) selectEl.value = value;

  // a search box above the menu: any part of any name, nickname or spelling ("margo", "georgiadis", "margarete")
  let box = selectEl.previousElementSibling;
  if (!box || !box.classList.contains('entity-search')) {
    box = document.createElement('input');
    box.type = 'search';
    box.className = 'entity-search';
    box.placeholder = 'Search any name or nickname…';
    box.setAttribute('aria-label', 'Search entities');
    selectEl.parentNode.insertBefore(box, selectEl);
  }
  box.value = '';
  box.oninput = () => {
    const q = nameWords(box.value, 1);
    if (!q.length) { const v = selectEl.value; selectEl.innerHTML = html(rows); selectEl.value = v; return; }
    // a typed word matches a name word that starts with it ("marg" → Margaret; "g" → Georgiadis), or a close
    // spelling variant that only adds a letter or two ("Margarete" → Margaret, but not "Georgiadis" → Georgia)
    const hit = r => q.every(w => r.words.some(x => x.startsWith(w) || (w.startsWith(x) && x.length >= 4 && w.length - x.length <= 2)));
    const list = rows.filter(r => r.host && hit(r));          // one line per record while searching
    selectEl.innerHTML = html(list);
    if (list.length === 1) { selectEl.value = list[0].n.id; selectEl.dispatchEvent(new Event('change', { bubbles: true })); }
  };
}

// lower-case words with accents removed: "Margarete" -> ["margarete"]
const nameWords = (s, min = 1) => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()
  .split(/[^a-z0-9]+/).filter(w => w.length >= min);

function domainPicks(el, name) {
  el.innerHTML = domainKeys().map(d =>
    `<label><input type="checkbox" name="${name}" value="${d}">${swatch(d)}${domainLetter(d)}</label>`).join('');
}

export function initPaths() {
  const form = document.getElementById('path-form');
  entityOptions(document.getElementById('path-from'));
  entityOptions(document.getElementById('path-to'));
  domainPicks(document.getElementById('path-from-doms'), 'from_dom');
  domainPicks(document.getElementById('path-to-doms'), 'to_dom');

  document.querySelectorAll('[data-pmode]').forEach(b => b.addEventListener('click', () => {
    pmode = b.dataset.pmode;
    document.querySelectorAll('[data-pmode]').forEach(x => x.classList.toggle('active', x === b));
    document.querySelectorAll('[data-pshow]').forEach(x => { x.hidden = x.dataset.pshow !== pmode; });
    document.getElementById('path-from').required = document.getElementById('path-to').required = pmode === 'entity';
  }));

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const q = {
      mode: form.querySelector('input[name=pmode]:checked').value,
      include_inactive: document.getElementById('path-inactive').checked,
      restrict_domains: document.getElementById('path-restrict').checked ? [...state.activeDomains] : null,
      limit: 5,
    };
    if (pmode === 'entity') {
      q.from_ids = [document.getElementById('path-from').value];
      q.to_ids = [document.getElementById('path-to').value];
      if (!q.from_ids[0] || !q.to_ids[0]) return;
    } else {
      q.from_domains = [...form.querySelectorAll('input[name=from_dom]:checked')].map(i => i.value);
      q.to_domains = [...form.querySelectorAll('input[name=to_dom]:checked')].map(i => i.value);
      if (!q.from_domains.length || !q.to_domains.length) { show('<p class="bad small">Pick at least one domain on each side.</p>'); return; }
    }
    show('<p class="muted small">Searching…</p>');
    try { renderResult(await api.path(q), q); } catch (e) { show(`<p class="bad small">${esc(e.message)}</p>`); }
  });
  document.getElementById('path-clear').onclick = () => { show(''); emit('path', null); lastResult = null; };
  on('use-in-path', ({ id, side }) => {
    document.querySelector('[data-pmode=entity]').click();
    document.getElementById(side === 'from' ? 'path-from' : 'path-to').value = id;
  });
}

function show(html) { document.getElementById('path-results').innerHTML = html; }

function label(id) { return esc(nodeById(id)?.label || id); }

function renderResult(r, q) {
  lastResult = r;
  let html = '';
  if (r.spanning_nodes?.length && q.from_domains) {
    html += `<p class="small muted">Already span both sides (0 hops): ${r.spanning_nodes.map(label).join(', ')}</p>`;
  }
  if (!r.paths.length) {
    html += `<p class="small"><b>${esc(r.message || 'No path found.')}</b></p>`;
    if (r.components) {
      const shared = r.source_components.some(c => r.target_components.includes(c));
      html += shared
        ? `<p class="small muted">They are connected, but not through the domains you allowed. Untick "Route only through checked domains" or check more domains.</p>`
        : `<p class="small muted">The ${q.include_inactive ? '' : 'active '}verified edges form ${r.components.length} separate clusters, and the two sides sit in different ones.
           ${q.include_inactive ? '' : 'Try including historical ties, or '}add a verified connection that bridges them.</p>`;
    }
    show(html); emit('path', null); return;
  }
  html += r.paths.map((p, i) => `
    <div class="path-card ${i === 0 ? 'active' : ''}" data-i="${i}">
      <h4>${p.length} hop${p.length > 1 ? 's' : ''} · ${label(p.nodes[0])} → ${label(p.nodes[p.nodes.length - 1])}</h4>
      <div class="small muted">${p.crossings.length} domain crossing${p.crossings.length === 1 ? '' : 's'} · weakest link ${p.min_weight ?? '—'} · mean ${p.mean_weight ?? '—'}</div>
      ${i === 0 ? hopsHtml(p) : ''}
    </div>`).join('');
  if (q.from_ids) html += `<button type="button" id="path-report" title="A shareable report on the starting entity, including this path with its filings">Draft report on this path</button>`;
  show(html);
  document.getElementById('path-report')?.addEventListener('click', ev =>
    draftReport({ subject: q.from_ids[0], path_to: q.to_ids[0] }, ev.target));
  document.querySelectorAll('#path-results .path-card').forEach(c => c.addEventListener('click', () => {
    document.querySelectorAll('#path-results .path-card').forEach(x => {
      x.classList.toggle('active', x === c);
      const p = lastResult.paths[+x.dataset.i];
      const hops = x.querySelector('.hops');
      if (x === c && !hops) x.insertAdjacentHTML('beforeend', hopsHtml(p));
      if (x !== c && hops) hops.remove();
    });
    highlight(lastResult.paths[+c.dataset.i]);
  }));
  highlight(r.paths[0]);
}

function hopsHtml(p) {
  return '<div class="hops">' + `<div class="small"><b>${label(p.nodes[0])}</b></div>` + p.hops.map(h => {
    const url = safeUrl(h.citation_url);
    const src = esc(h.verification_source || 'no source');
    const arrow = h.direction === 'forward' ? '→' : '←';
    const c = h.crossing;
    return `<div class="hop small">
      ${arrow} <b>${esc(h.relationship_type)}</b> ${h.active === false ? '<i>(historical)</i>' : ''} · w=${h.conflict_weight ?? '—'}
      ${h.human_bridge ? `<div class="muted">${esc(h.human_bridge)}</div>` : ''}
      <div class="muted">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${src}</a>` : src} · ${esc(h.edge_id)}</div>
      ${c ? `<span class="cross ${c.full_boundary ? 'full' : ''}" title="${c.full_boundary ? 'The two organizations share no domain' : 'The two organizations share some domains'}">⇢ ${c.full_boundary ? 'crosses' : 'partly crosses'} ${c.leaving.map(domainLetter).join('+') || '·'} → ${c.entering.map(domainLetter).join('+') || '·'}${c.via && c.via.length ? ' via ' + c.via.map(label).join(', ') : ''} · weight ${c.measured_weight}</span>` : ''}
      <div><b>${label(h.to)}</b></div>
    </div>`;
  }).join('') + '</div>';
}

function highlight(p) {
  emit('path', { nodes: p.nodes, edgeIds: p.hops.map(h => h.edge_id) });
  fit(new Set(p.nodes));
}
