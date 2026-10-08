// Force-directed graph in plain SVG (no libraries). Nodes are pie-sliced by domain membership.
import { state, emit, on, domainKeys, domainColor, hasIdentity, isSuperseded } from './state.js';
import { SHAPES, shapePath, primaryLetter } from './shapes.js';

const NS = 'http://www.w3.org/2000/svg';
let svg, viewport, gEdges, gNodes;
let sim = new Map();               // id -> {x, y, vx, vy, fixed}
let view = { k: 1, x: 0, y: 0 };
let elNode = new Map(), elEdge = new Map();

const mk = (tag, attrs = {}, parent) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
};

export function initGraph(svgEl) {
  svg = svgEl;
  const defs = mk('defs', {}, svg);
  for (const [id, cls] of [['arrow', 'arrow'], ['arrow-path', 'arrow on-path']]) {
    const m = mk('marker', { id, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 9, markerHeight: 9, markerUnits: 'userSpaceOnUse', orient: 'auto-start-reverse' }, defs);
    mk('path', { d: 'M0,0 L10,5 L0,10 z', class: cls }, m);
  }
  for (const letter of Object.keys(SHAPES)) {     // clip outlines for multi-domain organizations (scaled to their box)
    const c = mk('clipPath', { id: `clip-shape-${letter}`, clipPathUnits: 'objectBoundingBox' }, defs);
    mk('path', { d: shapePath(letter, 0.5 / SHAPES[letter].k), transform: 'translate(.5,.5)' }, c);
  }
  viewport = mk('g', {}, svg);
  gEdges = mk('g', {}, viewport);
  gNodes = mk('g', {}, viewport);
  bindPanZoom();
  on('filter', applyVisibility);
  on('people', () => render());
  on('path', highlightPath);
  on('select', markSelected);
}

// ------------------------------------------------------------ layout
function degreeMap() {
  const d = new Map();
  for (const e of state.graph.edges) {
    if (isSuperseded(e)) continue;
    d.set(e.source, (d.get(e.source) || 0) + 1);
    d.set(e.target, (d.get(e.target) || 0) + 1);
  }
  return d;
}

function anchors() {
  const doms = domainKeys(), a = {}, R = 330 * Math.max(1, Math.sqrt((state.graph.nodes.length || 1) / 120));
  doms.forEach((d, i) => {
    const t = (i / doms.length) * Math.PI * 2 - Math.PI / 2;
    a[d] = { x: Math.cos(t) * R, y: Math.sin(t) * R };
  });
  return a;
}

export function layout(reset = false) {
  const nodes = state.graph.nodes, A = anchors();
  if (reset) sim = new Map();
  for (const n of nodes) {
    if (!sim.has(n.id)) {
      const a = A[(n.domains || [])[0]] || { x: 0, y: 0 };
      sim.set(n.id, { x: a.x + (Math.random() - .5) * 120, y: a.y + (Math.random() - .5) * 120, vx: 0, vy: 0, fixed: false });
    }
  }
  for (const id of [...sim.keys()]) if (!nodes.find(n => n.id === id)) sim.delete(id);
  const edges = state.graph.edges.filter(e => !isSuperseded(e) && sim.has(e.source) && sim.has(e.target));
  const arr = nodes.map(n => [n, sim.get(n.id)]);
  const iters = arr.length > 1500 ? 220 : 420, C = 90;
  for (let it = 0; it < iters; it++) {
    const alpha = 1 - it / iters;
    // short-range repulsion via a spatial grid (only nearby nodes push each other): fast for thousands of nodes
    const grid = new Map();
    for (const [, p] of arr) {
      const k = `${Math.floor(p.x / C)},${Math.floor(p.y / C)}`;
      if (!grid.has(k)) grid.set(k, []);
      grid.get(k).push(p);
    }
    for (const [, p] of arr) {
      const cx = Math.floor(p.x / C), cy = Math.floor(p.y / C);
      for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) {
        const cell = grid.get(`${cx + i},${cy + j}`);
        if (!cell) continue;
        for (const q of cell) {
          if (q === p) continue;
          let dx = p.x - q.x, dy = p.y - q.y;
          if (dx === 0 && dy === 0) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
          const d2 = Math.max(dx * dx + dy * dy, 25);          // floor: coincident nodes can't explode apart
          if (d2 > C * C) continue;
          const f = 1400 / d2, d = Math.sqrt(d2);
          p.vx += dx / d * f; p.vy += dy / d * f;
        }
      }
    }
    for (const e of edges) {
      const p = sim.get(e.source), q = sim.get(e.target);
      const dx = q.x - p.x, dy = q.y - p.y, d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const f = (d - 130) * 0.04;
      p.vx += dx / d * f; p.vy += dy / d * f; q.vx -= dx / d * f; q.vy -= dy / d * f;
    }
    for (const [n, p] of arr) {
      const doms = (n.domains || []).filter(d => A[d]);
      let ax = 0, ay = 0;
      for (const d of doms) { ax += A[d].x; ay += A[d].y; }
      if (doms.length) { ax /= doms.length; ay /= doms.length; }
      p.vx += (ax - p.x) * 0.012; p.vy += (ay - p.y) * 0.012;
      const v = Math.hypot(p.vx, p.vy);                         // speed limit keeps the layout stable
      if (v > 60) { p.vx *= 60 / v; p.vy *= 60 / v; }
      if (!p.fixed) { p.x += p.vx * alpha; p.y += p.vy * alpha; }
      p.vx *= 0.55; p.vy *= 0.55;
    }
  }
}

// ------------------------------------------------------------ render
function nodeRadius(deg) { return Math.min(9 + deg * 2.2, 20); }
export const isPerson = n => n && n.entity_type === 'PERSON';
let elVia = [];

// With the people layer hidden, organizations that share a person get a dotted "via" link,
// so the entity graph stays connected without drawing every individual.
function renderViaLinks() {
  elVia = [];
  if (state.showPeople) return;
  const byId = new Map(state.graph.nodes.map(n => [n.id, n]));
  for (const v of viaPairs()) {
    const g = mk('g', {}, gEdges);
    const line = mk('line', { class: 'via' + (v.active ? '' : ' inactive'), 'stroke-width': Math.min(1 + v.people.length, 5) }, g);
    const hit = mk('line', { class: 'edge-hit' }, g);
    mk('title', {}, g).textContent = `Shared people (${v.people.length}): ` + v.people.map(id => byId.get(id).label).join(', ') +
      (v.active ? '' : '\n(no current role at both)') + '\nTurn on "Show people" to see each person.';
    hit.addEventListener('click', ev => { ev.stopPropagation(); emit('select', { kind: 'node', id: v.people[0] }); });
    elVia.push({ g, line, hit, ...v });
  }
}

// Organization pairs that share at least one person: [{a, b, people:[ids], active}]
export function viaPairs() {
  const byId = new Map(state.graph.nodes.map(n => [n.id, n]));
  const orgsOf = new Map();
  for (const e of state.graph.edges) {
    if (isSuperseded(e)) continue;
    const [p, o] = isPerson(byId.get(e.source)) ? [e.source, e.target] : isPerson(byId.get(e.target)) ? [e.target, e.source] : [];
    if (!p || isPerson(byId.get(o))) continue;
    if (!orgsOf.has(p)) orgsOf.set(p, new Map());
    const m = orgsOf.get(p);
    m.set(o, (m.get(o) || false) || e.transparency?.active !== false);
  }
  const pairs = new Map();
  for (const [p, m] of orgsOf) {
    const orgs = [...m.keys()].sort();
    for (let i = 0; i < orgs.length; i++) for (let j = i + 1; j < orgs.length; j++) {
      const k = orgs[i] + '|' + orgs[j];
      if (!pairs.has(k)) pairs.set(k, { a: orgs[i], b: orgs[j], people: [], active: false });
      const v = pairs.get(k);
      v.people.push(p);
      v.active ||= m.get(orgs[i]) && m.get(orgs[j]);
    }
  }
  return [...pairs.values()];
}

function slicePath(r, a0, a1) {
  const x0 = Math.cos(a0) * r, y0 = Math.sin(a0) * r, x1 = Math.cos(a1) * r, y1 = Math.sin(a1) * r;
  return `M0,0 L${x0},${y0} A${r},${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${x1},${y1} Z`;
}

export function render() {
  gEdges.textContent = ''; gNodes.textContent = '';
  elNode.clear(); elEdge.clear();
  const deg = degreeMap();
  for (const e of state.graph.edges) {
    const g = mk('g', { 'data-edge': e.edge_id }, gEdges);
    const w = Number(e.integrity?.conflict_weight) || 0;
    const line = mk('line', { class: 'edge', 'stroke-width': (1 + w * 4).toFixed(2), 'marker-end': 'url(#arrow)' }, g);
    if (e.transparency?.active === false) line.classList.add('inactive');
    if (isSuperseded(e)) line.classList.add('superseded');
    const hit = mk('line', { class: 'edge-hit' }, g);
    const title = mk('title', {}, g);
    title.textContent = `${e.relationship_type}${e.human_bridge ? ' — ' + e.human_bridge : ''} (w=${w.toFixed(2)})`;
    hit.addEventListener('click', ev => { ev.stopPropagation(); emit('select', { kind: 'edge', id: e.edge_id }); });
    elEdge.set(e.edge_id, { g, line, hit, e, w });
  }
  renderViaLinks();
  for (const n of state.graph.nodes) {
    const person = isPerson(n);
    const r = person ? 6 : nodeRadius(deg.get(n.id) || 0);
    const g = mk('g', { class: 'node' + (person ? ' person' : ''), 'data-node': n.id, tabindex: 0, role: 'button', 'aria-label': n.label }, gNodes);
    if (!hasIdentity(n)) g.classList.add('noid');
    if (person) {                                   // people: a glowing circle (circles are only ever people)
      mk('circle', { r: r * 2, fill: 'url(#lg-person-halo)', class: 'halo' }, g);
      const doms = (n.domains || []).filter(d => d.startsWith('DOM_'));
      if (!doms.length) mk('circle', { r, fill: 'url(#lg-person-core)' }, g);
      else if (doms.length === 1) mk('circle', { r, fill: domainColor(doms[0]) }, g);
      else doms.forEach((d, i) => {                 // the domains this person bridges, as color slices
        const a0 = -Math.PI / 2 + (i / doms.length) * 2 * Math.PI, a1 = -Math.PI / 2 + ((i + 1) / doms.length) * 2 * Math.PI;
        mk('path', { d: slicePath(r, a0, a1), fill: domainColor(d) }, g);
      });
      if (doms.length) mk('circle', { r, fill: 'url(#lg-person-sheen)', class: 'halo' }, g);   // luminous sheen
      mk('circle', { r: r + 1, class: 'ring' }, g);
    } else {                                        // organizations: the shape of their first domain
      const letter = primaryLetter(n), R = r * SHAPES[letter].k;
      const doms = (n.domains || []).filter(d => d.startsWith('DOM_'));
      if (doms.length <= 1) mk('path', { d: shapePath(letter, r), fill: domainColor(doms[0] || 'X') }, g);
      else {                                        // several domains: color slices inside the first domain's shape
        const cg = mk('g', { 'clip-path': `url(#clip-shape-${letter})` }, g);
        doms.forEach((d, i) => {
          const a0 = -Math.PI / 2 + (i / doms.length) * 2 * Math.PI, a1 = -Math.PI / 2 + ((i + 1) / doms.length) * 2 * Math.PI;
          mk('path', { d: slicePath(R, a0, a1), fill: domainColor(d) }, cg);
        });
      }
      mk('path', { d: shapePath(letter, r + 1), class: 'ring' }, g);
    }
    const label = mk('text', { y: r + 13, 'text-anchor': 'middle' }, g);
    label.textContent = n.label.length > 34 ? n.label.slice(0, 32) + '…' : n.label;
    const title = mk('title', {}, g);
    title.textContent = `${n.label}\n${(n.domains || []).join(', ')}${hasIdentity(n) ? '' : '\n⚠ no canonical identifier'}`;
    bindNodeDrag(g, n.id);
    g.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') emit('select', { kind: 'node', id: n.id }); });
    elNode.set(n.id, { g, r });
  }
  position();
  applyVisibility();
  highlightPath(state.path);
  markSelected(state.selected);
}

function position() {
  for (const [id, { g }] of elNode) {
    const p = sim.get(id);
    if (p) g.setAttribute('transform', `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`);
  }
  for (const [, o] of elEdge) {
    const p = sim.get(o.e.source), q = sim.get(o.e.target);
    if (!p || !q) continue;
    const dx = q.x - p.x, dy = q.y - p.y, d = Math.hypot(dx, dy) || 1;
    const rs = elNode.get(o.e.source)?.r || 10, rt = (elNode.get(o.e.target)?.r || 10) + 4;
    const x1 = p.x + dx / d * rs, y1 = p.y + dy / d * rs, x2 = q.x - dx / d * rt, y2 = q.y - dy / d * rt;
    for (const l of [o.line, o.hit]) { l.setAttribute('x1', x1); l.setAttribute('y1', y1); l.setAttribute('x2', x2); l.setAttribute('y2', y2); }
  }
  for (const v of elVia) {
    const p = sim.get(v.a), q = sim.get(v.b);
    if (!p || !q) continue;
    for (const l of [v.line, v.hit]) { l.setAttribute('x1', p.x); l.setAttribute('y1', p.y); l.setAttribute('x2', q.x); l.setAttribute('y2', q.y); }
  }
  viewport.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`);
}

// ------------------------------------------------------------ filtering / highlighting
export function visibleNodeIds() {
  const act = state.activeDomains;
  const vis = new Set(state.graph.nodes.filter(n => (n.domains || []).some(d => act.has(d)) &&
    (state.showPeople || !isPerson(n))).map(n => n.id));
  if (state.hideIsolated) {
    const linked = new Set();
    for (const e of state.graph.edges) {
      if (isSuperseded(e) || !vis.has(e.source) || !vis.has(e.target)) continue;
      linked.add(e.source); linked.add(e.target);
    }
    for (const id of [...vis]) if (!linked.has(id)) vis.delete(id);
  }
  if (state.focus) {
    const near = neighborhood(state.focus.id, state.focus.hops);
    for (const id of [...vis]) if (!near.has(id)) vis.delete(id);
    if (state.showPeople) for (const id of near) vis.add(id);
    vis.add(state.focus.id);
  }
  if (state.path) state.path.nodes.forEach(id => vis.add(id));     // never hide a highlighted path
  return vis;
}

// Entities within N hops of `id`. Passing through a person doesn't count as a hop, so
// "1 hop" = directly connected companies plus companies that share a person.
// People are kept only when they are the bridge (they sit inside the radius), not every person at
// the outermost companies; otherwise 1 hop would show the whole board of every neighbor.
export function neighborhood(id, hops) {
  const dist = hopDistances(id, hops);
  const byId = new Map(state.graph.nodes.map(n => [n.id, n]));
  const direct = new Set();
  for (const e of state.graph.edges) {
    if (isSuperseded(e)) continue;
    if (e.source === id) direct.add(e.target);
    if (e.target === id) direct.add(e.source);
  }
  return new Set([...dist.keys()].filter(v => v === id || !isPerson(byId.get(v)) || dist.get(v) < hops || direct.has(v)));
}

function hopDistances(id, hops) {
  const byId = new Map(state.graph.nodes.map(n => [n.id, n]));
  const adj = new Map();
  for (const e of state.graph.edges) {
    if (isSuperseded(e)) continue;
    if (!adj.has(e.source)) adj.set(e.source, []);
    if (!adj.has(e.target)) adj.set(e.target, []);
    adj.get(e.source).push(e.target); adj.get(e.target).push(e.source);
  }
  const dist = new Map([[id, 0]]);
  const dq = [id];
  while (dq.length) {
    const u = dq.shift();
    for (const v of adj.get(u) || []) {
      const d = dist.get(u) + (isPerson(byId.get(v)) ? 0 : 1);
      if (d > hops || (dist.has(v) && dist.get(v) <= d)) continue;
      dist.set(v, d);
      isPerson(byId.get(v)) ? dq.unshift(v) : dq.push(v);
    }
  }
  return dist;
}

function applyVisibility() {
  if (!elNode.size) return;
  const vis = visibleNodeIds();
  for (const [id, { g }] of elNode) g.style.display = vis.has(id) ? '' : 'none';
  for (const [, o] of elEdge) o.g.style.display = vis.has(o.e.source) && vis.has(o.e.target) ? '' : 'none';
  for (const v of elVia) v.g.style.display = vis.has(v.a) && vis.has(v.b) ? '' : 'none';
}

function highlightPath(path) {
  state.path = path || null;
  const pn = new Set(path ? path.nodes : []), pe = new Set(path ? path.edgeIds : []);
  for (const [id, { g }] of elNode) {
    g.classList.toggle('on-path', pn.has(id));
    g.classList.toggle('dim', Boolean(path) && !pn.has(id));
  }
  for (const [id, o] of elEdge) {
    o.line.classList.toggle('on-path', pe.has(id));
    o.line.setAttribute('marker-end', pe.has(id) ? 'url(#arrow-path)' : 'url(#arrow)');
    o.g.classList.toggle('dim', Boolean(path) && !pe.has(id));
  }
  for (const v of elVia) v.g.classList.toggle('dim', Boolean(path));
  applyVisibility();
}

function markSelected(sel) {
  for (const [id, { g }] of elNode) g.classList.toggle('selected', sel?.kind === 'node' && sel.id === id);
  for (const [id, o] of elEdge) o.line.classList.toggle('selected', sel?.kind === 'edge' && sel.id === id);
}

// ------------------------------------------------------------ interaction
function toWorld(cx, cy) {
  const r = svg.getBoundingClientRect();
  return { x: (cx - r.left - view.x) / view.k, y: (cy - r.top - view.y) / view.k };
}

function bindNodeDrag(g, id) {
  let start = null;
  g.addEventListener('pointerdown', ev => {
    ev.stopPropagation();
    g.setPointerCapture(ev.pointerId);
    start = { cx: ev.clientX, cy: ev.clientY, moved: false };
  });
  g.addEventListener('pointermove', ev => {
    if (!start) return;
    if (Math.hypot(ev.clientX - start.cx, ev.clientY - start.cy) > 3) start.moved = true;
    if (!start.moved) return;
    const w = toWorld(ev.clientX, ev.clientY), p = sim.get(id);
    p.x = w.x; p.y = w.y; p.fixed = true;
    position();
  });
  g.addEventListener('pointerup', ev => {
    if (start && !start.moved) emit('select', { kind: 'node', id });
    start = null;
    if (g.hasPointerCapture(ev.pointerId)) g.releasePointerCapture(ev.pointerId);
  });
}

function bindPanZoom() {
  let pan = null;
  svg.addEventListener('pointerdown', ev => {
    if (ev.target.classList && ev.target.classList.contains('edge-hit')) return;   // let edge clicks through
    pan = { x: ev.clientX - view.x, y: ev.clientY - view.y, moved: false, sx: ev.clientX, sy: ev.clientY };
    svg.classList.add('panning');
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener('pointermove', ev => {
    if (!pan) return;
    if (Math.hypot(ev.clientX - pan.sx, ev.clientY - pan.sy) > 3) pan.moved = true;
    view.x = ev.clientX - pan.x; view.y = ev.clientY - pan.y;
    position();
  });
  svg.addEventListener('pointerup', ev => {
    if (pan && !pan.moved) emit('select', null);
    pan = null; svg.classList.remove('panning');
    if (svg.hasPointerCapture(ev.pointerId)) svg.releasePointerCapture(ev.pointerId);
  });
  svg.addEventListener('wheel', ev => {
    ev.preventDefault();
    zoomAt(ev.clientX, ev.clientY, Math.exp(-ev.deltaY * 0.0015));
  }, { passive: false });
}

export function zoomAt(cx, cy, f) {
  const r = svg.getBoundingClientRect();
  if (cx == null) { cx = r.left + r.width / 2; cy = r.top + r.height / 2; }
  const k = Math.min(4, Math.max(0.15, view.k * f));
  const w = toWorld(cx, cy);
  view.k = k;
  view.x = cx - r.left - w.x * k; view.y = cy - r.top - w.y * k;
  position();
}

export function fit(ids) {
  const vis = ids || visibleNodeIds();
  const pts = [...vis].map(id => sim.get(id)).filter(Boolean);
  if (!pts.length) return;
  const r = svg.getBoundingClientRect();
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  const minX = Math.min(...xs) - 60, maxX = Math.max(...xs) + 60, minY = Math.min(...ys) - 50, maxY = Math.max(...ys) + 60;
  const k = Math.min(2, Math.max(0.15, Math.min(r.width / (maxX - minX), r.height / (maxY - minY))));
  view.k = k;
  view.x = r.width / 2 - ((minX + maxX) / 2) * k;
  view.y = r.height / 2 - ((minY + maxY) / 2) * k;
  position();
}
