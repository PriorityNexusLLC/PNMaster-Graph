// One shape per domain, so the map reads without color (colorblind-safe). People are glowing circles;
// circles are reserved for people, so no organization is ever drawn round.
import { domainColor } from './state.js';

export const SHAPES = {
  A: { name: 'triangle', n: 3, rot: -90, k: 1.3 },
  B: { name: 'square', n: 4, rot: 45, k: 1.2 },
  C: { name: 'diamond', n: 4, rot: 0, k: 1.3 },
  D: { name: 'pentagon', n: 5, rot: -90, k: 1.15 },
  E: { name: 'hexagon', n: 6, rot: 0, k: 1.1 },
  F: { name: 'down-triangle', n: 3, rot: 90, k: 1.3 },
  G: { name: 'star', star: true, n: 5, rot: -90, k: 1.35 },
  X: { name: 'octagon', n: 8, rot: 22.5, k: 1.05 },
};

export const primaryLetter = n => {
  const d = (n?.domains || []).find(x => x.startsWith('DOM_'));
  return d ? d.slice(4) : 'X';
};

function points(letter, r) {
  const s = SHAPES[letter] || SHAPES.X, R = r * s.k, pts = [];
  const count = s.star ? s.n * 2 : s.n;
  for (let i = 0; i < count; i++) {
    const rad = s.star && i % 2 ? R * 0.45 : R;
    const a = (s.rot + (360 / count) * i) * Math.PI / 180;
    pts.push([Math.cos(a) * rad, Math.sin(a) * rad]);
  }
  return pts;
}

// SVG path centred on 0,0
export function shapePath(letter, r) {
  return points(letter, r).map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`).join('') + 'Z';
}

// canvas: trace the shape (caller fills/strokes)
export function traceShape(ctx, letter, x, y, r) {
  ctx.beginPath();
  points(letter, r).forEach(([px, py], i) => (i ? ctx.lineTo(x + px, y + py) : ctx.moveTo(x + px, y + py)));
  ctx.closePath();
}

// small inline icon for menus, filters and chips: the domain's shape in its color
export function swatch(d) {
  if (d === 'BRIDGE_HUMAN') return personSwatch();
  const letter = d && d.startsWith('DOM_') ? d.slice(4) : 'X';
  return `<svg class="swatch shape" viewBox="-7 -7 14 14" aria-hidden="true"><path d="${shapePath(letter, 5)}" fill="${domainColor(d || 'X')}"/></svg>`;
}

export function personSwatch() {
  return `<svg class="swatch shape" viewBox="-7 -7 14 14" aria-hidden="true"><circle r="6.5" fill="url(#lg-person-halo)"/><circle r="3.6" fill="url(#lg-person-core)"/></svg>`;
}
