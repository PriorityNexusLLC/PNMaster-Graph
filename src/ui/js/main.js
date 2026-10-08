// Bootstrap: load graph + governance, wire modules, handle reloads after writes.
import { state, emit, on, isSuperseded } from './state.js';
import { api } from './api.js';
import { initGraph, layout, render, fit, zoomAt } from './graph-view.js';
import { initFilters } from './filters.js';
import { initPaths } from './paths.js';
import { initEdgeForm, initNodeForms } from './forms.js';
import { initPanels, renderDetails, renderIssues } from './panels.js';
import { initReview } from './review.js';
import { init3d, set3d, is3d, fit3d, zoom3d, relayout3d, toggleAutoRotate } from './graph3d.js';

function initTheme() {
  const btn = document.getElementById('theme-toggle');
  let saved = null;
  try { saved = localStorage.getItem('pn.theme'); } catch { /* ignore */ }
  if (saved) document.documentElement.dataset.theme = saved;
  btn.addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('pn.theme', next); } catch { /* ignore */ }
  });
}

function stats() {
  const live = state.graph.edges.filter(e => !isSuperseded(e));
  const errs = state.issues.filter(i => i.severity === 'error').length;
  document.getElementById('stats').textContent =
    `${state.graph.nodes.length} entities · ${live.length} verified connections · ${errs} integrity errors · updated ${state.graph.schema_metadata.last_updated || '—'}`;
}

async function load() {
  const [{ graph, governance }, { issues }] = await Promise.all([api.graph(), api.audit()]);
  state.graph = graph; state.gov = governance; state.issues = issues;
}

async function start() {
  initTheme();
  try { await load(); } catch (e) {
    document.querySelector('.graph-wrap').innerHTML = `<p style="padding:24px" class="bad">Couldn't reach the local server (${e.message}). Start it with <code>run.cmd</code>.</p>`;
    return;
  }
  initGraph(document.getElementById('graph'));
  initFilters(); initPaths(); initPanels(); initEdgeForm(); initNodeForms(); initReview();
  layout(); render(); renderIssues(); stats();
  requestAnimationFrame(() => fit());

  init3d();
  const hint = document.querySelector('.graph-hint');
  const hint2d = hint.textContent;
  const btn3d = document.getElementById('mode-3d'), spin = document.getElementById('spin-3d');
  const setMode = on3d => {
    set3d(on3d);
    btn3d.setAttribute('aria-pressed', String(on3d));
    btn3d.textContent = on3d ? '2D' : '3D';
    spin.hidden = !on3d;
    hint.textContent = on3d ? 'Drag to rotate · right-drag (or Shift+drag) to move · scroll to zoom · click a node for details' : hint2d;
    try { localStorage.setItem('pn.view3d', on3d ? '1' : '0'); } catch { /* ignore */ }
  };
  btn3d.onclick = () => setMode(!is3d());
  spin.onclick = () => spin.setAttribute('aria-pressed', String(toggleAutoRotate()));
  try { if (localStorage.getItem('pn.view3d') === '1') setMode(true); } catch { /* ignore */ }

  document.getElementById('zoom-in').onclick = () => is3d() ? zoom3d(1.25) : zoomAt(null, null, 1.25);
  document.getElementById('zoom-out').onclick = () => is3d() ? zoom3d(0.8) : zoomAt(null, null, 0.8);
  document.getElementById('zoom-fit').onclick = () => is3d() ? fit3d() : fit();
  document.getElementById('relayout').onclick = () => is3d() ? relayout3d() : (layout(true), render(), fit());

  on('focus-changed', () => requestAnimationFrame(() => { if (!is3d()) fit(); }));

  // tell the viewer when the graph file changed underneath them (autopilot / update_all)
  let version = null;
  const checkVersion = async () => {
    try {
      const { version: v } = await fetch('/api/version').then(r => r.json());
      if (version === null) { version = v; return; }
      if (v !== version) {
        version = v;
        const t = document.getElementById('toast');
        t.innerHTML = 'New data was added to the graph. <button type="button" class="primary" id="reload-now">Load it</button>';
        t.hidden = false;
        document.getElementById('reload-now').onclick = () => { t.hidden = true; emit('reload', {}); };
      }
    } catch { /* server not reachable: ignore */ }
  };
  checkVersion();
  setInterval(checkVersion, 60000);
  on('reload', async ({ select } = {}) => {
    await load();
    layout(); render(); renderIssues(); stats();
    emit('graph-loaded');
    if (select) emit('select', select); else renderDetails();
  });
}

start();
