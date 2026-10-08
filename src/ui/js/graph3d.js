// 3D view: own force layout + perspective canvas renderer (no libraries, nothing loaded from the internet).
// Each domain A–G gets its own region in space; companies settle near their domains, bridges in between.
import { state, emit, on, domainKeys, isSuperseded, esc } from './state.js';
import { visibleNodeIds, viaPairs, isPerson } from './graph-view.js';
import { traceShape, primaryLetter } from './shapes.js';

let canvas, ctx, tip, active = false, dirty = true, autoRotate = false;
const P = new Map();                         // id -> {x,y,z,vx,vy,vz}
let alpha = 0, links = [], vis = new Set(), projected = [];
const cam = { yaw: 0.6, pitch: -0.25, dist: 1400, tx: 0, ty: 0, tz: 0, panX: 0, panY: 0 };
let hover = null, colors = {};

export function init3d() {
  canvas = document.getElementById('graph3d');
  ctx = canvas.getContext('2d');
  tip = document.getElementById('tip3d');
  bindControls();
  const refresh = () => { if (active) { recompute(); dirty = true; } };
  ['filter', 'path', 'select', 'people', 'graph-loaded'].forEach(ev => on(ev, refresh));
  on('focus-changed', () => { if (active) { recompute(); fit3d(); } });
  new ResizeObserver(() => { dirty = true; }).observe(canvas);
  requestAnimationFrame(loop);
}

export function set3d(on3d) {
  active = on3d;
  document.getElementById('graph').style.display = on3d ? 'none' : '';
  canvas.style.display = on3d ? 'block' : 'none';
  if (on3d) {
    seed();
    recompute();
    fit3d();
  }
  tip.hidden = true;
}
export const is3d = () => active;
export function toggleAutoRotate() { autoRotate = !autoRotate; return autoRotate; }

// ------------------------------------------------------------ layout
function anchors() {
  const doms = domainKeys(), a = {}, R = 520, n = doms.length;
  doms.forEach((d, i) => {                                 // spread domains evenly over a sphere
    const y = 1 - (i + 0.5) / n * 2, r = Math.sqrt(1 - y * y), t = i * 2.399963;
    a[d] = { x: Math.cos(t) * r * R, y: y * R, z: Math.sin(t) * r * R };
  });
  return a;
}

function homeOf(n, A) {
  const ds = (n.domains || []).filter(d => A[d]);
  if (!ds.length) return { x: 0, y: 0, z: 0 };
  const h = { x: 0, y: 0, z: 0 };
  for (const d of ds) { h.x += A[d].x; h.y += A[d].y; h.z += A[d].z; }
  return { x: h.x / ds.length, y: h.y / ds.length, z: h.z / ds.length };
}

function seed() {
  const A = anchors();
  let added = 0;
  for (const n of state.graph.nodes) {
    if (P.has(n.id)) continue;
    const h = homeOf(n, A), j = () => (Math.random() - 0.5) * 160;
    P.set(n.id, { x: h.x + j(), y: h.y + j(), z: h.z + j(), vx: 0, vy: 0, vz: 0 });
    added++;
  }
  for (const id of [...P.keys()]) if (!state.graph.nodes.find(n => n.id === id)) P.delete(id);
  if (added) alpha = 1;
}

function step() {
  const A = anchors(), nodes = state.graph.nodes, C = 70, grid = new Map();
  const key = (x, y, z) => `${Math.floor(x / C)},${Math.floor(y / C)},${Math.floor(z / C)}`;
  for (const n of nodes) {
    const p = P.get(n.id), k = key(p.x, p.y, p.z);
    if (!grid.has(k)) grid.set(k, []);
    grid.get(k).push(p);
  }
  for (const n of nodes) {                                  // short-range repulsion via spatial hash
    const p = P.get(n.id), cx = Math.floor(p.x / C), cy = Math.floor(p.y / C), cz = Math.floor(p.z / C);
    for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) for (let k = -1; k <= 1; k++) {
      const cell = grid.get(`${cx + i},${cy + j},${cz + k}`);
      if (!cell) continue;
      for (const q of cell) {
        if (q === p) continue;
        let dx = p.x - q.x, dy = p.y - q.y, dz = p.z - q.z;
        const d2 = dx * dx + dy * dy + dz * dz + 1;
        if (d2 > C * C) continue;
        const f = 900 / d2;
        p.vx += dx * f * 0.05; p.vy += dy * f * 0.05; p.vz += dz * f * 0.05;
      }
    }
  }
  for (const e of state.graph.edges) {                     // springs along verified connections
    if (isSuperseded(e)) continue;
    const p = P.get(e.source), q = P.get(e.target);
    if (!p || !q) continue;
    const dx = q.x - p.x, dy = q.y - p.y, dz = q.z - p.z, d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.01;
    const f = (d - 70) * 0.012 / d;
    p.vx += dx * f; p.vy += dy * f; p.vz += dz * f; q.vx -= dx * f; q.vy -= dy * f; q.vz -= dz * f;
  }
  for (const n of nodes) {                                  // pull toward the domain's region
    const p = P.get(n.id), h = homeOf(n, A), w = isPerson(n) ? 0.002 : 0.008;
    p.vx += (h.x - p.x) * w; p.vy += (h.y - p.y) * w; p.vz += (h.z - p.z) * w;
    p.x += p.vx * alpha; p.y += p.vy * alpha; p.z += p.vz * alpha;
    p.vx *= 0.6; p.vy *= 0.6; p.vz *= 0.6;
  }
  alpha *= 0.985;
  if (alpha < 0.03) alpha = 0;
}

// ------------------------------------------------------------ what to draw
function recompute() {
  vis = visibleNodeIds();
  const byId = new Map(state.graph.nodes.map(n => [n.id, n]));
  const pathE = new Set(state.path ? state.path.edgeIds : []);
  links = [];
  for (const e of state.graph.edges) {
    if (isSuperseded(e) || !vis.has(e.source) || !vis.has(e.target)) continue;
    if (!state.showPeople && (isPerson(byId.get(e.source)) || isPerson(byId.get(e.target))) && !pathE.has(e.edge_id)) continue;
    links.push({ a: e.source, b: e.target, w: Number(e.integrity?.conflict_weight) || 0.3,
                 inactive: e.transparency?.active === false, path: pathE.has(e.edge_id) });
  }
  if (!state.showPeople) {
    for (const v of viaPairs()) {
      if (vis.has(v.a) && vis.has(v.b)) links.push({ a: v.a, b: v.b, w: Math.min(0.3 + v.people.length * 0.15, 1), via: true, inactive: !v.active, n: v.people.length });
    }
  }
  const css = getComputedStyle(document.documentElement);
  colors = Object.fromEntries(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'X'].map(k => [k, css.getPropertyValue(`--dom-${k}`).trim()]));
  for (const k of ['--edge', '--via', '--path', '--ink', '--bg', '--muted', '--person-glow']) colors[k] = css.getPropertyValue(k).trim();
}

function fit3d() {
  const pts = [...vis].map(id => P.get(id)).filter(Boolean);
  if (!pts.length) return;
  const c = pts.reduce((s, p) => ({ x: s.x + p.x, y: s.y + p.y, z: s.z + p.z }), { x: 0, y: 0, z: 0 });
  cam.tx = c.x / pts.length; cam.ty = c.y / pts.length; cam.tz = c.z / pts.length;
  const r = Math.max(120, ...pts.map(p => Math.hypot(p.x - cam.tx, p.y - cam.ty, p.z - cam.tz)));
  cam.dist = r * 2.6; cam.panX = cam.panY = 0;
  dirty = true;
}
export { fit3d };
export function zoom3d(f) { cam.dist = Math.min(8000, Math.max(80, cam.dist / f)); dirty = true; }
export function relayout3d() { P.clear(); seed(); dirty = true; }

// ------------------------------------------------------------ render
function project(p, W, H) {
  const x0 = p.x - cam.tx, y0 = p.y - cam.ty, z0 = p.z - cam.tz;
  const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
  const x1 = x0 * cy - z0 * sy, z1 = x0 * sy + z0 * cy;
  const y2 = y0 * cp - z1 * sp, z2 = y0 * sp + z1 * cp;
  const zc = z2 + cam.dist;
  if (zc < 20) return null;
  const f = Math.min(W, H) * 0.9 / zc;
  return { sx: W / 2 + cam.panX + x1 * f, sy: H / 2 + cam.panY + y2 * f, f, z: zc };
}

function colorOf(n) {
  const d = (n.domains || []).find(x => x.startsWith('DOM_'));
  return colors[d ? d.slice(4) : 'X'] || '#888';
}

function draw() {
  const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth, H = canvas.clientHeight;
  if (canvas.width !== W * dpr || canvas.height !== H * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const byId = new Map(state.graph.nodes.map(n => [n.id, n]));
  const proj = new Map();
  for (const id of vis) { const p = P.get(id); if (p) { const q = project(p, W, H); if (q) proj.set(id, q); } }
  const depthAlpha = z => Math.max(0.12, Math.min(1, 1.6 - z / (cam.dist * 1.6)));

  for (const l of links) {                                  // links behind nodes
    const a = proj.get(l.a), b = proj.get(l.b);
    if (!a || !b) continue;
    ctx.globalAlpha = l.path ? 1 : depthAlpha((a.z + b.z) / 2) * (l.inactive ? 0.4 : 0.7);
    ctx.strokeStyle = l.path ? colors['--path'] : l.via ? colors['--via'] : colors['--edge'];
    ctx.lineWidth = l.path ? 3 : 0.5 + l.w * 1.5;
    ctx.setLineDash(l.via ? [1.5, 4] : l.inactive ? [4, 4] : []);
    ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
  }
  ctx.setLineDash([]);

  const deg = new Map();
  for (const l of links) { deg.set(l.a, (deg.get(l.a) || 0) + 1); deg.set(l.b, (deg.get(l.b) || 0) + 1); }
  const pathN = new Set(state.path ? state.path.nodes : []);
  const sel = state.selected?.kind === 'node' ? state.selected.id : null;
  projected = [...proj.entries()].map(([id, q]) => {
    const n = byId.get(id);
    const base = isPerson(n) ? 3 : Math.min(4 + Math.sqrt(deg.get(id) || 0) * 2.2, 14);
    return { id, n, ...q, r: Math.max(1.5, base * q.f * 1.3) };
  }).sort((a, b) => b.z - a.z);

  for (const o of projected) {
    ctx.globalAlpha = pathN.size && !pathN.has(o.id) ? 0.25 : depthAlpha(o.z);
    const person = isPerson(o.n), letter = primaryLetter(o.n);
    if (person) {                                            // people: glowing circle (only people are round)
      const R = o.r * 2.2, g = ctx.createRadialGradient(o.sx, o.sy, 0, o.sx, o.sy, R);
      g.addColorStop(0, '#ffffff'); g.addColorStop(0.25, colors['--person-glow']);
      g.addColorStop(0.45, colors['--person-glow'] + '88'); g.addColorStop(1, colors['--person-glow'] + '00');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(o.sx, o.sy, R, 0, Math.PI * 2); ctx.fill();
      const doms = (o.n.domains || []).filter(d => d.startsWith('DOM_'));
      doms.forEach((d, i) => {                               // the domains this person bridges, as color slices
        const a0 = -Math.PI / 2 + (i / doms.length) * 2 * Math.PI, a1 = -Math.PI / 2 + ((i + 1) / doms.length) * 2 * Math.PI;
        ctx.fillStyle = colors[d.slice(4)];
        ctx.beginPath(); ctx.moveTo(o.sx, o.sy); ctx.arc(o.sx, o.sy, o.r, a0, a1); ctx.closePath(); ctx.fill();
      });
      if (doms.length) {                                     // luminous sheen over the colors
        const s = ctx.createRadialGradient(o.sx - o.r * 0.25, o.sy - o.r * 0.3, 0, o.sx, o.sy, o.r);
        s.addColorStop(0, 'rgba(255,255,255,.6)'); s.addColorStop(0.35, 'rgba(255,255,255,.12)'); s.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.fillStyle = s;
        ctx.beginPath(); ctx.arc(o.sx, o.sy, o.r, 0, Math.PI * 2); ctx.fill();
      }
    } else {
      ctx.fillStyle = colorOf(o.n);
      traceShape(ctx, letter, o.sx, o.sy, o.r); ctx.fill();
      const doms = (o.n.domains || []).filter(d => d.startsWith('DOM_'));
      if (doms.length > 1) {                                 // multi-domain: second domain as an outline of the same shape
        ctx.strokeStyle = colors[doms[1].slice(4)]; ctx.lineWidth = Math.max(1, o.r * 0.3); ctx.lineJoin = 'round';
        traceShape(ctx, letter, o.sx, o.sy, o.r * 1.2); ctx.stroke();
      }
    }
    if (o.id === sel || pathN.has(o.id) || o.id === hover?.id) {
      ctx.globalAlpha = 1; ctx.strokeStyle = pathN.has(o.id) ? colors['--path'] : colors['--ink']; ctx.lineWidth = 2.5;
      if (person) { ctx.beginPath(); ctx.arc(o.sx, o.sy, o.r + 3, 0, Math.PI * 2); } else traceShape(ctx, letter, o.sx, o.sy, o.r + 3);
      ctx.stroke();
    }
  }

  // labels: biggest nearby companies, plus anything selected / hovered / on the path
  const labelled = projected.filter(o => !isPerson(o.n)).sort((a, b) => b.r - a.r).slice(0, vis.size < 60 ? 60 : 28);
  const must = projected.filter(o => o.id === sel || o.id === hover?.id || pathN.has(o.id));
  ctx.font = '11px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.lineJoin = 'round';
  const placed = [];
  for (const o of new Set([...must, ...labelled])) {
    const t = o.n.label.length > 30 ? o.n.label.slice(0, 28) + '…' : o.n.label;
    const w = ctx.measureText(t).width, box = [o.sx - w / 2 - 2, o.sy + o.r + 2, o.sx + w / 2 + 2, o.sy + o.r + 15];
    const clash = placed.some(b => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]);
    if (clash && !must.includes(o)) continue;               // keep labels readable: skip ones that would overlap
    placed.push(box);
    ctx.globalAlpha = must.includes(o) ? 1 : depthAlpha(o.z);
    ctx.strokeStyle = colors['--bg']; ctx.lineWidth = 3; ctx.strokeText(t, o.sx, o.sy + o.r + 12);
    ctx.fillStyle = colors['--ink']; ctx.fillText(t, o.sx, o.sy + o.r + 12);
  }
  ctx.globalAlpha = 1;
}

function loop() {
  if (active) {
    if (alpha > 0) { for (let i = 0; i < 4 && alpha > 0; i++) step(); dirty = true; }
    if (autoRotate) { cam.yaw += 0.0025; dirty = true; }
    if (dirty) { draw(); dirty = false; }
  }
  requestAnimationFrame(loop);
}

// ------------------------------------------------------------ interaction
function pick(x, y) {
  let best = null;
  for (let i = projected.length - 1; i >= 0; i--) {        // front-most first
    const o = projected[i];
    if (Math.hypot(o.sx - x, o.sy - y) <= o.r + 4) { best = o; break; }
  }
  return best;
}

function bindControls() {
  let drag = null;
  canvas.addEventListener('contextmenu', e => e.preventDefault());
  canvas.addEventListener('pointerdown', e => {
    drag = { x: e.clientX, y: e.clientY, moved: false, pan: e.button === 2 || e.shiftKey };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointermove', e => {
    const r = canvas.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.pan) { cam.panX += dx; cam.panY += dy; } else {
        cam.yaw += dx * 0.006; cam.pitch = Math.max(-1.5, Math.min(1.5, cam.pitch + dy * 0.006));
      }
      drag.x = e.clientX; drag.y = e.clientY; dirty = true;
      return;
    }
    const h = pick(x, y);
    if (h?.id !== hover?.id) { hover = h; dirty = true; }
    if (h) {
      const doms = (h.n.domains || []).map(d => d.replace('DOM_', '').replace('BRIDGE_HUMAN', 'bridge')).join(', ');
      tip.innerHTML = `<b>${esc(h.n.label)}</b><br><span class="muted">${esc(h.n.entity_type || '')} · ${esc(doms)}</span>`;
      tip.style.left = `${x + 14}px`; tip.style.top = `${y + 10}px`; tip.hidden = false;
    } else tip.hidden = true;
  });
  canvas.addEventListener('pointerup', e => {
    if (drag && !drag.moved) {
      const r = canvas.getBoundingClientRect(), h = pick(e.clientX - r.left, e.clientY - r.top);
      emit('select', h ? { kind: 'node', id: h.id } : null);
    }
    drag = null;
    if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointerleave', () => { hover = null; tip.hidden = true; dirty = true; });
  canvas.addEventListener('wheel', e => { e.preventDefault(); zoom3d(Math.exp(-e.deltaY * 0.0012)); }, { passive: false });
}
