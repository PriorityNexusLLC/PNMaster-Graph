// Domain A–G filter checkboxes.
import { state, emit, on, domainKeys, domainLetter, domainColor, esc } from './state.js';
import { swatch, SHAPES } from './shapes.js';
import { entityOptions } from './paths.js';
import { visibleNodeIds } from './graph-view.js';

const KEY = 'pn.activeDomains';

export function initFilters() {
  const box = document.getElementById('domain-filters');
  const doms = domainKeys(), meta = state.graph.schema_metadata.domains;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { /* storage unavailable */ }
  state.activeDomains = new Set(Array.isArray(saved) ? saved.filter(d => doms.includes(d)) : doms);
  if (!state.activeDomains.size) state.activeDomains = new Set(doms);

  const counts = Object.fromEntries(doms.map(d => [d, state.graph.nodes.filter(n => (n.domains || []).includes(d)).length]));
  box.innerHTML = doms.map(d => `
    <label class="check" title="${esc(meta[d])}">
      <input type="checkbox" value="${d}" ${state.activeDomains.has(d) ? 'checked' : ''}>
      ${swatch(d)}
      <b>${domainLetter(d)}</b>&nbsp;${esc(meta[d])}
      <span class="dom-count">${counts[d]}</span>
    </label>`).join('');

  const lg = document.getElementById('legend-shapes');
  if (lg) lg.innerHTML = doms.map(d => `<span title="${esc(meta[d])}">${swatch(d)}<b>${domainLetter(d)}</b> ${esc(SHAPES[domainLetter(d)].name.replace('-', ' '))}</span>`).join('')
    + `<span>${swatch('X')}no domain yet</span>`;

  box.addEventListener('change', ev => {
    const d = ev.target.value;
    ev.target.checked ? state.activeDomains.add(d) : state.activeDomains.delete(d);
    persist();
  });
  document.getElementById('dom-all').onclick = () => setAll(true);
  document.getElementById('dom-none').onclick = () => setAll(false);
  document.getElementById('hide-isolated').addEventListener('change', ev => {
    state.hideIsolated = ev.target.checked; emit('filter');
  });
  // focus: one entity's neighborhood
  const fsel = document.getElementById('focus-entity');
  const fill = () => entityOptions(fsel, { placeholder: '— whole map —', value: state.focus ? state.focus.id : '' });
  fill();
  on('graph-loaded', fill);
  const applyFocus = () => {
    const hops = Number(document.querySelector('input[name=focus-hops]:checked').value);
    state.focus = fsel.value ? { id: fsel.value, hops } : null;
    emit('filter');
    emit('focus-changed');
    const hint = document.getElementById('focus-hint');
    if (!state.focus) { hint.textContent = 'Big map? Pick one company or person to see only its neighborhood.'; return; }
    const shown = visibleNodeIds().size;
    hint.textContent = `Showing ${shown.toLocaleString()} entities within ${hops} hop${hops > 1 ? 's' : ''}. ` +
      (hops === 1 ? 'Its direct ties, plus organizations that share a person with it.'
                  : 'Also their neighbors. Big holders (e.g. BlackRock) reach much of the map, so 2 hops can be large.');
  };
  fsel.addEventListener('change', applyFocus);
  document.querySelectorAll('input[name=focus-hops]').forEach(r => r.addEventListener('change', applyFocus));
  document.getElementById('focus-clear').onclick = () => { fsel.value = ''; applyFocus(); };
  on('focus-request', id => { fsel.value = id; applyFocus(); });

  const people = document.getElementById('show-people');
  try { state.showPeople = localStorage.getItem('pn.showPeople') === '1'; } catch { /* ignore */ }
  people.checked = state.showPeople;
  const nPeople = state.graph.nodes.filter(n => n.entity_type === 'PERSON').length;
  document.getElementById('people-count').textContent = nPeople ? `(${nPeople})` : '';
  people.addEventListener('change', () => {
    state.showPeople = people.checked;
    try { localStorage.setItem('pn.showPeople', people.checked ? '1' : '0'); } catch { /* ignore */ }
    emit('people');
  });

  function setAll(on) {
    state.activeDomains = new Set(on ? doms : []);
    box.querySelectorAll('input').forEach(i => { i.checked = on; });
    persist();
  }
  function persist() {
    try { localStorage.setItem(KEY, JSON.stringify([...state.activeDomains])); } catch { /* ignore */ }
    emit('filter');
  }
}
