// Right-hand panels: tabs, node/edge details, integrity issues, event log.
import { state, emit, on, nodeById, edgeById, hasIdentity, isSuperseded, domainLetter, domainColor, esc, safeUrl, akaOf } from './state.js';
import { swatch } from './shapes.js';
import { api } from './api.js';

export function initPanels() {
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => emit('tab', t.dataset.tab)));
  on('tab', name => {
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('[data-tabbody]').forEach(b => { b.hidden = b.dataset.tabbody !== name; });
    if (name === 'log') { renderLog(); renderChain(); }
    if (name === 'issues') renderChain();
    if (name === 'balance') renderBalance();
  });
  on('select', sel => {
    state.selected = sel;
    renderDetails();
    if (sel) emit('tab', 'details');
  });
}

const chip = d => `<span class="chip">${swatch(d)}${esc(domainLetter(d))}</span>`;
const link = (u, text) => { const s = safeUrl(u); return s ? `<a href="${esc(s)}" target="_blank" rel="noopener noreferrer">${esc(text || s)}</a>` : esc(text || u || '—'); };
const nodeLink = id => `<button type="button" class="link" data-goto="${esc(id)}">${esc(nodeById(id)?.label || id)}</button>`;

function edgeCard(e, from) {
  const t = e.transparency || {}, i = e.integrity || {};
  const other = e.source === from ? e.target : e.source;
  const dir = from ? (e.source === from ? '→' : '←') : '';
  return `<div class="edge-card">
    <h4>${dir} ${esc(e.relationship_type)} ${from ? nodeLink(other) : ''}
      ${t.active === false ? '<span class="chip">historical</span>' : ''}${isSuperseded(e) ? `<span class="chip">superseded by ${esc(i.superseded_by)}</span>` : ''}</h4>
    ${e.human_bridge ? `<div>${esc(e.human_bridge)}</div>` : ''}
    <dl class="kv">
      <dt>Edge</dt><dd>${esc(e.edge_id)}</dd>
      <dt>Source</dt><dd>${esc(t.verification_source || '—')}${t.source_reference ? ` · <code>${esc(t.source_reference)}</code>` : ''}</dd>
      <dt>Citation</dt><dd>${t.citation_url ? link(t.citation_url) : '<span class="bad">none recorded</span>'}</dd>
      <dt>Document date</dt><dd>${esc(t.document_date || '—')}${t.date_verified ? ` · verified ${esc(t.date_verified)}` : ''}</dd>
      ${t.tenure_start || t.tenure_end ? `<dt>Tenure</dt><dd>${esc(t.tenure_start || '?')} → ${esc(t.tenure_end || 'present')}</dd>` : ''}
      <dt>Control</dt><dd>${esc(i.control_type || '—')}${i.control_class ? ` (${esc(i.control_class)})` : ''}</dd>
      <dt>Conflict weight</dt><dd><b>${i.conflict_weight ?? '—'}</b></dd>
      ${i.verification_hash ? `<dt>Hash</dt><dd><code>${esc(i.verification_hash.slice(0, 16))}…</code></dd>` : ''}
      ${i.supersedes ? `<dt>Supersedes</dt><dd>${esc(i.supersedes)}</dd>` : ''}
      ${i.supersede_reason ? `<dt>Superseded because</dt><dd>${esc(i.supersede_reason)}</dd>` : ''}
    </dl></div>`;
}

export function renderDetails() {
  const box = document.getElementById('details'), sel = state.selected;
  if (!sel) { box.innerHTML = '<p class="muted">Select a node or an edge on the graph.</p>'; return; }
  if (sel.kind === 'edge') {
    const e = edgeById(sel.id);
    if (!e) return;
    box.innerHTML = `<h2>Connection</h2><p>${nodeLink(e.source)} → ${nodeLink(e.target)}</p>${edgeCard(e)}${issuesFor(e.edge_id)}`;
  } else {
    const n = nodeById(sel.id);
    if (!n) return;
    const ids = n.identity || {};
    const accepted = state.gov.identity.accepted_keys;
    const edges = state.graph.edges.filter(e => e.source === n.id || e.target === n.id)
      .sort((a, b) => (b.integrity?.conflict_weight || 0) - (a.integrity?.conflict_weight || 0));
    box.innerHTML = `
      <h2>${esc(n.entity_type || 'Entity')}</h2>
      <h3 style="margin:0 0 6px">${esc(n.label)}</h3>
      ${akaOf(n).length ? `<div class="aka">Also known as: ${akaOf(n).map(esc).join(' · ')}</div>` : ''}
      <div>${(n.domains || []).map(chip).join('')}</div>
      <dl class="kv">
        <dt>Node id</dt><dd><code>${esc(n.id)}</code></dd>
        ${Object.entries(ids).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)} ${accepted.includes(k) ? '<span class="ok" title="canonical identifier">✓</span>' : ''}</dd>`).join('')}
      </dl>
      ${hasIdentity(n) ? '' : `<p class="bad small">No canonical identifier (${accepted.join(', ')}). It can't be connected until one is added.
         <button type="button" class="link" id="fix-id">Add identifier →</button></p>`}
      <div class="stamp small" id="stamp"></div>
      ${(n.leadership || []).length ? `<div class="lbl">Leadership (as recorded)</div><ul class="small">${n.leadership.map(l => `<li>${esc(l)}</li>`).join('')}</ul>` : ''}
      <div class="row">
        <button type="button" id="focus-here">Focus here</button>
        <button type="button" data-use="from">Path from here</button>
        <button type="button" data-use="to">Path to here</button>
        <button type="button" class="primary" id="connect-here">Connect…</button>
        <button type="button" id="draft-report" title="Draft a shareable report: findings with filing links, limits, right of reply, fingerprint">Draft report</button>
      </div>
      <div class="lbl">${edges.length} connection${edges.length === 1 ? '' : 's'}</div>
      ${edges.map(e => edgeCard(e, n.id)).join('') || '<p class="muted small">No verified connections yet.</p>'}
      ${issuesFor(n.id)}`;
    box.querySelectorAll('[data-use]').forEach(b => b.addEventListener('click', () => emit('use-in-path', { id: n.id, side: b.dataset.use })));
    box.querySelector('#connect-here').addEventListener('click', () => emit('connect-from', n.id));
    box.querySelector('#focus-here').addEventListener('click', () => emit('focus-request', n.id));
    box.querySelector('#draft-report').addEventListener('click', ev => draftReport({ subject: n.id }, ev.target));
    showStamp(n.id);
    box.querySelector('#fix-id')?.addEventListener('click', () => emit('fix-identity', n.id));
  }
  box.querySelectorAll('[data-goto]').forEach(b => b.addEventListener('click', () => emit('select', { kind: 'node', id: b.dataset.goto })));
}

function issuesFor(id) {
  const list = state.issues.filter(i => i.id === id);
  if (!list.length) return '';
  return '<div class="lbl">Integrity notes</div>' + list.map(i =>
    `<div class="issue ${i.severity}"><b>${esc(i.core)}</b> · ${esc(i.message)}</div>`).join('');
}

export function renderIssues() {
  const box = document.getElementById('issues');
  const errs = state.issues.filter(i => i.severity === 'error').length;
  document.getElementById('issue-count').textContent = errs || '';
  const by = core => state.issues.filter(i => i.core === core);
  box.innerHTML = ['Identity', 'Transparency', 'Integrity'].map(core => {
    const list = by(core);
    return `<h3>${core} <span class="muted small">(${list.length})</span></h3>` + (list.length ? list.map(i => {
      const name = i.kind === 'node' ? (nodeById(i.id)?.label || i.id) : i.id;
      return `<div class="issue ${i.severity}" data-kind="${i.kind}" data-id="${esc(i.id)}">
        <b>${esc(i.severity)}</b> · ${esc(name)}<div>${esc(i.message)}</div></div>`;
    }).join('') : '<p class="ok small">✓ no issues</p>');
  }).join('');
  box.querySelectorAll('.issue').forEach(el => el.addEventListener('click', () =>
    emit('select', { kind: el.dataset.kind, id: el.dataset.id })));
}

async function renderLog() {
  const box = document.getElementById('log');
  try {
    const { events } = await api.log();
    box.innerHTML = events.reverse().map(ev => {
      let body = '';
      if (ev.event === 'path_query') {
        body = `${ev.paths_found} path(s)` + (ev.best_path ? `: ${ev.best_path.map(id => esc(nodeById(id)?.label || id)).join(' → ')}` : '') +
          (ev.boundary_crossings?.length ? `<br>${ev.boundary_crossings.length} boundary crossing(s): ` +
            ev.boundary_crossings.map(c => `${c.leaving.map(domainLetter).join('+') || '·'}→${c.entering.map(domainLetter).join('+') || '·'} (${c.measured_weight})`).join(', ') : '');
      } else if (ev.event === 'edge_recorded') {
        body = `${esc(ev.edge.edge_id)} ${esc(ev.edge.source)} → ${esc(ev.edge.target)} (${esc(ev.edge.relationship_type)})` + (ev.superseded ? ` · superseded ${esc(ev.superseded)}` : '');
      } else if (ev.event === 'node_recorded') {
        body = `${esc(ev.node.id)} ${esc(ev.node.label)}`;
      } else if (ev.event === 'identity_added') {
        body = `${esc(ev.node_id)}: ${esc(JSON.stringify(ev.identity))}`;
      } else if (ev.event === 'rejected') {
        body = `${esc(ev.endpoint)}: ` + (ev.errors || []).map(e => esc(`${e.field}: ${e.message}`)).join('; ');
      } else if (ev.event === 'aliases_added') {
        body = `${esc(nodeById(ev.node_id)?.label || ev.node_id)} also known as ${esc((ev.aliases || []).join(', '))}`;
      } else if (ev.event === 'node_corrected') {
        body = `${esc(ev.node_id)}: ${esc(ev.was?.entity_type)} → ${esc(ev.now?.entity_type)} · ${esc(ev.basis)}`;
      } else if (ev.event === 'node_consolidated') {
        body = `${esc(ev.node_id)} folded into ${esc(ev.into)} · ${esc(ev.reason)}`;
      } else if (ev.event === 'graph_saved') {
        body = `${ev.nodes} entities · ${ev.edges} connections · file fingerprint <code>${esc((ev.sha256 || '').slice(0, 16))}…</code>`;
      } else if (ev.event === 'chain_start') {
        body = `${ev.sealed_lines} earlier entries sealed under fingerprint <code>${esc((ev.sealed_sha256 || '').slice(0, 16))}…</code>`;
      }
      const fp = ev.hash ? ` · <code title="this entry's fingerprint; it includes the one before it">${esc(ev.hash.slice(0, 10))}</code>` : '';
      return `<div class="log-ev"><div class="t">${esc(ev.ts)} · <b>${esc(ev.event)}</b>${fp}</div>${body}</div>`;
    }).join('') || '<p class="muted small">No events yet.</p>';
  } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
}

// The dated "public records check" stamp for an entity (official enforcement records reviewed, and when).
let stampsCache = null;
async function showStamp(id) {
  const el = document.getElementById('stamp');
  if (!el) return;
  try { stampsCache = stampsCache || await api.stamps(); } catch { return; }
  const s = stampsCache.entities?.[id];
  if (!s) { el.innerHTML = '<span class="muted">Public records check: not reviewed yet</span>'; return; }
  const tone = s.confirmed ? 'bad' : s.possible ? 'warn-text' : 'ok';
  el.innerHTML = `<b>Public records check</b> · reviewed ${esc(s.reviewed)} · <span class="${tone}">${esc(s.result)}</span>`
    + `<div class="muted">${esc(s.sources.join(' · '))}${s.possible && !s.confirmed ? ' · confirm or dismiss in Obsidian: HOOT/Enforcement confirmations' : ''}</div>`
    + (s.confirmed || []).map(f => `<div>${esc(f.source)}, ${esc(f.date)}: <a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">“${esc(f.title)}”</a> (${esc(f.kind)})</div>`).join('');
}
on('reload', () => { stampsCache = null; });

// Draft a report on the server, then open it in a new tab (print / save as PDF / download for Substack).
export async function draftReport(body, btn) {
  const win = window.open('', '_blank');           // opened now, while the click still counts, so it isn't blocked
  const was = btn?.textContent;
  if (btn) { btn.disabled = true; btn.textContent = 'Drafting…'; }
  try {
    const r = await api.report(body);
    if (win) win.location = r.html_url; else location.href = r.html_url;
    emit('report-drafted', r);
  } catch (e) {
    if (win) win.close();
    alert(`Couldn't draft the report: ${e.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = was; }
  }
}

async function renderChain() {
  const boxes = document.querySelectorAll('[data-chain]');
  try {
    const c = await api.chain();
    const g = c.graph || {};
    const html = c.ok
      ? `<b class="ok">✓ Audit log intact</b>: ${c.entries.toLocaleString()} entries, every fingerprint re-checked
         (${c.sealed_legacy_lines.toLocaleString()} early entries sealed together; ${c.chained.toLocaleString()} chained one by one).
         ${c.anchors?.checked ? `${c.anchors.checked} outside anchor${c.anchors.checked === 1 ? '' : 's'} match.` : 'No outside anchor yet.'}
         <div class="${g.status === 'changed_outside' ? 'bad' : 'muted'} small">Graph file: ${esc(g.message || '')}</div>`
      : `<b class="bad">✗ Audit log altered</b>: ${c.break_count} problem${c.break_count === 1 ? '' : 's'} found.
         <ul class="small">${c.breaks.slice(0, 10).map(b => `<li>${b.line ? `line ${b.line}: ` : ''}${esc(b.problem)}</li>`).join('')}</ul>
         <div class="${g.status === 'changed_outside' ? 'bad' : 'muted'} small">Graph file: ${esc(g.message || '')}</div>`;
    boxes.forEach(b => { b.innerHTML = html; b.classList.toggle('broken', !c.ok || g.status === 'changed_outside'); });
  } catch (e) { boxes.forEach(b => { b.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }); }
}

async function renderBalance() {
  const box = document.getElementById('balance');
  box.innerHTML = '<p class="muted small">Measuring…</p>';
  let r;
  try { r = await api.balance(); } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return; }
  const go = (id, text) => `<button type="button" class="link" data-goto="${esc(id)}">${esc(text)}</button>`;
  const ind = r.independence, con = r.concentration, sp = r.single_points, ten = r.tenure;
  box.innerHTML = `
    <h3>1. Board independence <span class="muted small">(${ind.flags.length} of ${ind.boards_scored} boards)</span></h3>
    <div class="bal-std">${esc(ind.standard)}. Flagged when half or fewer of the recorded directors are free of a filed tie to the company. ${esc(ind.note)}</div>
    ${ind.flags.map(f => `<div class="issue warn"><b>${f.tied} of ${f.directors} tied</b> · ${go(f.org, f.label)}
      <ul class="small">${f.ties.map(t => `<li>${go(t.person, t.label)}: ${esc(t.why.join('; '))}</li>`).join('')}</ul></div>`).join('')
      || '<p class="ok small">✓ every scored board has a majority with no filed tie</p>'}
    <h3>2. Concentration by domain</h3>
    <div class="bal-std">${esc(con.standard)}.</div>
    <div class="bal-std"><b>Orgs</b>: organizations in this domain that have at least one recorded owner, director or officer ·
      <b>Blocks</b>: recorded ownership stakes of 5% or more (each holder–company pair counts once) ·
      <b>HHI</b>: how concentrated those blocks are among holders, 0–10,000; over 1,800 = highly concentrated (counts blocks, not market share) ·
      <b>Top-4 reach</b>: share of the domain's orgs that the four most-connected holders own a block of or hold a board/officer seat at ·
      <b>Largest holders</b>: most blocks in the domain (count in brackets)</div>
    <table class="bal-table"><tr><th>Domain</th><th>Orgs</th><th>Blocks</th><th>HHI</th><th>Top-4 reach</th><th>Largest holders</th></tr>
    ${con.domains.map(d => `<tr><td><b>${esc(d.domain.slice(-1))}</b></td><td>${d.organizations}</td><td>${d.ownership_blocks}</td>
      <td class="${d.flag ? 'bad' : ''}">${d.hhi ?? '—'}${d.flag ? ' high' : ''}</td><td>${Math.round(d.top4_reach_share * 100)}%</td>
      <td>${d.top_holders.slice(0, 3).map(h => `${go(h.id, h.label)} (${h.blocks})`).join(', ')}</td></tr>`).join('')}</table>
    <h3>3. Single points of failure <span class="muted small">(${sp.count || 0})</span></h3>
    <div class="bal-std">In a network of ${(sp.network_size || 0).toLocaleString()} connected entities, ${esc(sp.meaning || '')}.</div>
    ${(sp.flags || []).slice(0, 25).map(f => `<div class="small">${go(f.id, f.label)}: <b>${f.cut_off}</b> entities depend on it</div>`).join('') || '<p class="muted small">none</p>'}
    <h3>4. Long tenure <span class="muted small">(${ten.flags.length})</span></h3>
    <div class="bal-std">${esc(ten.standard)}.<br>${esc(ten.method)}.</div>
    ${ten.flags.slice(0, 40).map(f => `<div class="small">${go(f.person, f.label)} at ${go(f.org, f.org_label)}: at least <b>${f.years_at_least}</b> years
      <span class="muted">(${esc(f.first_filing)} → ${esc(f.latest_filing)})</span></div>`).join('') || '<p class="muted small">none on record</p>'}
    <h3>5. Over-boarding <span class="muted small">(${(r.overboarding?.flags || []).length})</span></h3>
    <div class="bal-std">${esc(r.overboarding?.standard || '')}. Also: ${esc(r.overboarding?.ceo_standard || '')}.<br>${esc(r.overboarding?.note || '')}.</div>
    ${(r.overboarding?.flags || []).map(f => `<div class="small">${go(f.person, f.label)}: ${esc(f.why)}</div>`).join('') || '<p class="muted small">none on record</p>'}
    <h3>6. Pattern signals <span class="muted small">(official records combined)</span></h3>
    <div class="bal-std">Connections no single document states, found by combining official records on the map: former senators on boards ·
      a senator's committee overseeing a sector where they hold a filed role · lobbyists who disclosed working for a senator or committee,
      lobbying in that committee's sector · companies sharing a director with a company that has a confirmed public enforcement record.</div>
    <div id="pattern-list" class="small muted">Loading…</div>
    <p class="muted small">Change any standard's value in <code>src/engine/gates.py</code> (BALANCE_STANDARDS). Report copy: Obsidian → HOOT/Reports/Balance check.</p>`;
  box.querySelectorAll('[data-goto]').forEach(b => b.addEventListener('click', () => emit('select', { kind: 'node', id: b.dataset.goto })));
  try {
    const p = await api.patterns();
    const el = document.getElementById('pattern-list');
    el.classList.remove('muted');
    el.innerHTML = (p.pending_identity_confirmations ? `<p class="warn-text">${p.pending_identity_confirmations} senator ↔ SEC record pair(s) await your confirmation in Obsidian (HOOT/Identity confirmations); signals 1–2 appear once confirmed.</p>` : '')
      + (p.signals.map(s => `<div class="issue"><b>${esc(s.type)}</b><div>${esc(s.text)}</div>
          <div class="muted">${s.evidence.map(e => e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">${esc(e.source || 'record')}${e.date ? ' ' + esc(e.date) : ''}</a>` : esc(e.source || '')).join(' · ')}
          · ${s.subjects.map(id => go(id, nodeById(id)?.label || id)).join(', ')}</div></div>`).join('') || '<p class="muted">No pattern signals yet.</p>');
    el.querySelectorAll('[data-goto]').forEach(b => b.addEventListener('click', () => emit('select', { kind: 'node', id: b.dataset.goto })));
  } catch (e) { document.getElementById('pattern-list').textContent = e.message; }
}
