// Shared app state + a tiny event bus. Modules subscribe instead of calling each other directly.
const listeners = {};

export const state = {
  graph: null,          // { schema_metadata, nodes, edges }
  gov: null,            // governance.json
  issues: [],           // integrity audit
  activeDomains: new Set(),
  hideIsolated: false,
  selected: null,       // { kind: 'node'|'edge', id }
  path: null,           // { nodes:[], edgeIds:[] } currently highlighted
};

export function on(evt, fn) { (listeners[evt] ||= []).push(fn); }
export function emit(evt, data) { (listeners[evt] || []).forEach(fn => fn(data)); }

export const nodeById = id => state.graph.nodes.find(n => n.id === id);
export const edgeById = id => state.graph.edges.find(e => e.edge_id === id);
export const domainKeys = () => Object.keys(state.graph.schema_metadata.domains).filter(k => k.startsWith('DOM_'));
export const domainLetter = d => d.replace('DOM_', '');
export const domainColor = d => `var(--dom-${d.startsWith('DOM_') ? domainLetter(d) : 'X'})`;
export const isSuperseded = e => Boolean(e.integrity && e.integrity.superseded_by);

// other names sources have used for this entity, minus mere reorderings of its label ("HOLLUB VICKI A")
const nameKey = s => String(s || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean).sort().join(' ');
export function akaOf(node) {
  const seen = new Set([nameKey(node.label)]), out = [];
  const all = [...(node.provenance?.aliases_seen_in_sources || [])]
    .sort((a, b) => (a === a.toUpperCase()) - (b === b.toUpperCase()));   // 'Sylvia M Mathews' before 'MATHEWS SYLVIA M'
  for (const a of all) {
    const k = nameKey(a);
    if (k && !seen.has(k)) { seen.add(k); out.push(a); }
  }
  return out;
}

export function hasIdentity(node) {
  const g = state.gov.identity;
  const ids = node.identity || {};
  return g.accepted_keys.some(k => ids[k] && new RegExp(g.patterns[k]).test(String(ids[k])));
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function safeUrl(u) {
  try { const p = new URL(u); return p.protocol === 'https:' || p.protocol === 'http:' ? p.href : null; } catch { return null; }
}

export function toast(msg, ms = 4000) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, ms);
}
