import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'bioharness', 'ui', 'app.js'), 'utf8');

const from = src.indexOf('const TILE = 32;');
const to = src.indexOf('// -- the world');
if (from < 0 || to < 0) {
  console.log(JSON.stringify({ error: 'could not locate the geometry block in app.js' }));
  process.exit(2);
}

const ctx = vm.createContext({ hue: () => 0, api: async () => ({}), Image: class {} });
vm.runInContext(
  src.slice(from, to) +
  '\n;Object.assign(globalThis, { ROOMS, buildRoute, WT, DOOR_W, WALL_H });',
  ctx);
const { ROOMS, buildRoute, WT, DOOR_W, WALL_H } = ctx;

function wallRects(r) {
  const ox = r.x - WT, ow = r.w + WT * 2, out = [];
  out.push([ox, r.y, WT, r.h + WT]);
  out.push([r.x + r.w, r.y, WT, r.h + WT]);
  const g0 = r.doorX - DOOR_W / 2, g1 = r.doorX + DOOR_W / 2;
  const fy = r.doorSide === 'top' ? r.y - WT : r.y + r.h;
  out.push([ox, fy, g0 - ox, WT]);
  out.push([g1, fy, ox + ow - g1, WT]);
  if (r.backWall !== false) out.push([ox, r.y - WALL_H, ow, WALL_H]);
  return out;
}

const WALLS = ROOMS.flatMap(wallRects);
const inWall = (x, y) =>
  WALLS.some(([wx, wy, ww, wh]) => x >= wx && x <= wx + ww && y >= wy && y <= wy + wh);

const bad = [];
let samples = 0;
for (const A of ROOMS) {
  for (const B of ROOMS) {
    if (A === B) continue;
    let cx = A.x + A.w / 2, cy = A.y + A.h - 24;
    const tx = B.x + B.w / 2, ty = B.y + B.h - 24;
    for (const p of buildRoute(cx, cy, tx, ty)) {
      const steps = Math.max(1, Math.ceil(Math.hypot(p.x - cx, p.y - cy) / 2));
      for (let i = 1; i <= steps; i++) {
        const sx = cx + (p.x - cx) * i / steps;
        const sy = cy + (p.y - cy) * i / steps;
        samples++;
        if (inWall(sx, sy)) {
          bad.push(`${A.id}->${B.id} @ ${sx.toFixed(0)},${sy.toFixed(0)}`);
          i = steps;
        }
      }
      cx = p.x; cy = p.y;
    }
  }
}

const doorless = ROOMS.filter(r => typeof r.doorX !== 'number' || !r.doorSide);
const misplaced = ROOMS.filter(r =>
  r.doorX - DOOR_W / 2 < r.x - WT || r.doorX + DOOR_W / 2 > r.x + r.w + WT);

console.log(JSON.stringify({
  rooms: ROOMS.length, samples, crossings: bad.length,
  examples: bad.slice(0, 5),
  doorless: doorless.map(r => r.id), misplaced: misplaced.map(r => r.id),
}));
process.exit(bad.length || doorless.length || misplaced.length ? 1 : 0);
