'use strict';

const $ = (s, r = document) => r.querySelector(s);
const TABS = ['floor', 'runs', 'hive', 'caps', 'cost'];
const CC_TABS = ['activity', 'workers', 'staff', 'memory', 'tasks', 'graph'];

const state = {
  tab: 'floor', cc: 'activity', runId: null, agent: null,
  runs: [], detail: null, hive: null, caps: null, overview: null, floor: null,
  log: null, logTab: 'stderr', sent: '', sending: false,
  staffOptions: null, hireMsg: '', hiring: false,
  // Kept in state so a repaint never discards a half-filled form.
  hireForm: { name: '', role: '', provider: 'builtin', model: '', autonomy: 'bounded' },
};

const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtSecs = s => s == null ? '—' : s < 60 ? `${(+s).toFixed(1)}s`
  : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
const shortId = id => String(id || '').replace(/^run-/, '').slice(0, 8);
const pill = s => `<span class="pill s-${esc(s)}">${esc(s)}</span>`;
const chip = s => `<span class="chip c-${esc(s)}">${esc(s)}</span>`;
const clock = ts => ts ? new Date(ts * 1000).toLocaleTimeString() : '';
const initials = n => String(n || '?').slice(0, 2).toUpperCase();
// Stable per-name colour so an agent keeps its identity across renders.
const hue = n => { let h = 0; for (const c of String(n)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
const avatarFill = n => `hsl(${hue(n)} 42% 45%)`;
const STATUS_COLOR = { working: 'var(--run)', awaiting: 'var(--accent)',
                       blocked: 'var(--fail)', idle: 'var(--skip)' };

async function api(path, options) {
  const r = await fetch(path, { cache: 'no-store', ...options });
  if (!r.ok && !options) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

// ==========================================================================
// The lab floor -- pixel art on a canvas, labels on an SVG layer above it
// ==========================================================================
const TILE = 32;
const FW = 1152, FH = 672;
const SCALE = 1.5;

const WALL_H = 60;      // height of a room's back wall band
const WT = 12;          // thickness of side and front walls
const DOOR_W = 48;      // clear width of a doorway

// Corridors. Every room's door opens onto one of these two lanes, and the
// lanes are joined by the vertical gaps between the room columns.
const LANE_TOP = 266, LANE_BOTTOM = 534;
const CONNECTORS = [380, 768];
const WALK_SPEED = 78;

const WALL_FACE = '#d8d2be';
const WALL_EDGE = '#333a3f';

const ATLAS = { img: null, frames: null, ready: false };

async function loadAtlas() {
  try {
    const idx = await api('/assets/atlas.json');
    const img = new Image();
    img.src = idx.image;
    await img.decode();
    ATLAS.img = img; ATLAS.frames = idx.frames; ATLAS.ready = true;
  } catch {
    ATLAS.ready = false;
  }
}

// x/y/w/h describe the room's INTERIOR floor. Walls are built around it, and
// `doorX` is the centre of the only opening.
const ROOMS = [
  { id: 'planning', label: 'PLANNING OFFICE', x: 16, y: 72, w: 340, h: 156,
    tile: 'floor_wood', doorSide: 'bottom', doorX: 186, roles: ['pi', 'planner'],
    wall: ['desk_pc', 'desk_pc', 'whiteboard', 'filing_cabinet', 'clock'],
    floorProps: [['table', 0.24, 0.86], ['chair', 0.62, 0.84], ['plant', 0.90, 0.86]] },

  { id: 'seq', label: 'SEQUENCING BAY', x: 404, y: 72, w: 340, h: 156,
    tile: 'floor_tile', counter: true, doorSide: 'bottom', doorX: 574,
    roles: ['curator'],
    wall: ['sequencer', 'pcr', 'incubator', 'freezer', 'gel_dock'],
    floorProps: [['sink', 0.22, 0.86], ['bench', 0.72, 0.86]] },

  { id: 'qc', label: 'QC BENCH', x: 792, y: 72, w: 340, h: 156,
    tile: 'floor_tile', counter: true, doorSide: 'bottom', doorX: 962,
    roles: ['validator'],
    wall: ['microscope', 'centrifuge', 'gel_dock', 'shelving', 'incubator'],
    floorProps: [['flask_rack', 0.24, 0.86], ['bench', 0.74, 0.86]] },

  { id: 'compute', label: 'COMPUTE CLUSTER', x: 16, y: 352, w: 340, h: 148,
    tile: 'floor_grate', doorSide: 'bottom', doorX: 186,
    roles: ['runner', 'orchestrator'],
    wall: ['server_rack', 'server_rack', 'server_rack', 'server_rack', 'server_rack'],
    floorProps: [['desk_pc', 0.24, 0.88], ['printer', 0.58, 0.86], ['filing_cabinet', 0.88, 0.86]] },

  { id: 'structure', label: 'STRUCTURE LAB', x: 404, y: 352, w: 340, h: 148,
    tile: 'floor_lino', counter: true, doorSide: 'bottom', doorX: 574,
    roles: ['analyst'],
    wall: ['fume_hood', 'gel_dock', 'shelving', 'incubator', 'freezer'],
    floorProps: [['microscope', 0.24, 0.88], ['bench', 0.74, 0.88]] },

  { id: 'writeup', label: 'WRITE-UP DESK', x: 792, y: 352, w: 340, h: 148,
    tile: 'floor_wood', doorSide: 'bottom', doorX: 962, roles: ['scribe'],
    wall: ['desk_pc', 'whiteboard', 'filing_cabinet', 'clock', 'printer'],
    floorProps: [['sofa', 0.24, 0.88], ['table', 0.56, 0.86], ['plant', 0.88, 0.86]] },

  // The lobby is a proper enclosed room too; its single door is in the top
  // wall, opening onto the same lower corridor the labs open onto.
  { id: 'lobby', label: 'WAITING LOBBY', x: 16, y: 568, w: 1116, h: 80,
    tile: 'floor_wood', backWall: false, doorSide: 'top', doorX: 574,
    roles: ['operator'], wall: [],
    floorProps: [['water_cooler', 0.06, 0.95], ['sofa', 0.18, 0.95],
                 ['table', 0.29, 0.95], ['plant', 0.40, 0.95],
                 ['sofa', 0.70, 0.95], ['table', 0.80, 0.95],
                 ['plant', 0.94, 0.95]] },
];

const TOP_ROOMS = ['planning', 'seq', 'qc'];
const ROLE_SPRITE = {
  pi: 'char_senior', planner: 'char_tablet', curator: 'char_goggles',
  validator: 'char_pipette', analyst: 'char_curly', scribe: 'char_hijab',
  runner: 'char_hoodie', orchestrator: 'char_hoodie', operator: 'char_casual',
};
const SIT_SPRITE = {
  pi: 'sit_senior', planner: 'sit_tablet', curator: 'sit_goggles',
  validator: 'sit_pipette', analyst: 'sit_curly',
  runner: 'sit_hoodie', orchestrator: 'sit_hoodie',
};
const SPRITE_POOL = ['char_hoodie', 'char_casual', 'char_curly', 'char_tablet'];
const spriteFor = a => ROLE_SPRITE[a.role] || SPRITE_POOL[hue(a.name) % SPRITE_POOL.length];

function roomById(id) { return ROOMS.find(r => r.id === id); }

// Interior only: the walls are not walkable, so a point inside a wall belongs
// to no room and gets pushed out to a corridor.
function roomAt(x, y) {
  return ROOMS.find(r => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h);
}

function slots(room) {
  const n = room.id === 'lobby' ? 6 : 3;
  const step = room.w / n;
  const y = room.y + room.h - (room.id === 'lobby' ? 12 : 24);
  return Array.from({ length: n }, (_, i) => ({ x: room.x + step * (i + 0.5), y }));
}

function seat(agents) {
  const taken = {}, placed = {};
  ROOMS.forEach(r => (taken[r.id] = 0));
  const roomFor = role => ROOMS.find(r => r.roles.includes(role)) || roomById('lobby');
  agents.forEach(a => {
    const r = roomFor(a.role);
    const free = slots(r);
    placed[a.name] = { ...free[Math.min(taken[r.id]++, free.length - 1)],
                       room: r.id, agent: a };
  });
  return placed;
}

// -- doors and routing -----------------------------------------------------
const laneFor = room => (TOP_ROOMS.includes(room.id) ? LANE_TOP : LANE_BOTTOM);
const nearestLane = y => (Math.abs(y - LANE_TOP) < Math.abs(y - LANE_BOTTOM)
                          ? LANE_TOP : LANE_BOTTOM);

/** The two points either side of a room's doorway. */
function doorPoints(room) {
  const x = room.doorX;
  return room.doorSide === 'top'
    ? { inside: { x, y: room.y + 18 }, outside: { x, y: room.y - WT - 16 } }
    : { inside: { x, y: room.y + room.h - 16 },
        outside: { x, y: room.y + room.h + WT + 16 } };
}

/** Keep a destination somewhere a person could actually stand. */
function sanitizeTarget(x, y) {
  const r = roomAt(x, y);
  if (r) return { x, y };
  return { x: Math.max(20, Math.min(FW - 20, x)), y: nearestLane(y) };
}

/**
 * Walk from one point to another using doors and corridors only -- never
 * through a wall. Inside a room the route first slides across to the door,
 * steps out, follows a corridor lane, then enters the target room's door.
 */
function buildRoute(fx, fy, tx, ty) {
  const A = roomAt(fx, fy), B = roomAt(tx, ty);
  if (A === B) return [{ x: tx, y: ty }];

  const pts = [];
  let lane;

  if (A) {
    const d = doorPoints(A);
    pts.push({ x: d.inside.x, y: fy }, d.inside, d.outside);
    lane = laneFor(A);
    pts.push({ x: d.outside.x, y: lane });
  } else {
    lane = nearestLane(fy);
    pts.push({ x: fx, y: lane });
  }

  const target = B ? laneFor(B) : nearestLane(ty);
  if (lane !== target) {
    const cx = CONNECTORS.reduce((a, b) => (Math.abs(b - tx) < Math.abs(a - tx) ? b : a));
    pts.push({ x: cx, y: lane }, { x: cx, y: target });
  }

  if (B) {
    const d = doorPoints(B);
    pts.push({ x: d.outside.x, y: target }, d.outside, d.inside, { x: tx, y: ty });
  } else {
    pts.push({ x: tx, y: target }, { x: tx, y: ty });
  }
  return pts;
}

// -- the world -------------------------------------------------------------
const WORLD = { actors: {}, seen: new Set(), raf: 0, last: 0, hover: null };

function syncWorld() {
  const agents = state.floor?.agents || [];
  const pos = seat(agents);

  for (const a of agents) {
    const home = pos[a.name];
    let act = WORLD.actors[a.name];
    if (!act) {
      act = WORLD.actors[a.name] = { x: home.x, y: home.y, path: [], facing: 1,
                                     wait: 0, moving: false, errand: null };
    }
    act.agent = a;
    act.home = home;
  }
  for (const name of Object.keys(WORLD.actors)) {
    if (!pos[name]) delete WORLD.actors[name];
  }

  // A routed message becomes an errand: the sender walks over, pauses at the
  // recipient's desk, then heads back. The animation is driven by real events.
  const now = Date.now() / 1000;
  for (const t of state.floor?.traffic || []) {
    const key = `${t.from}>${t.to}@${t.ts}`;
    if (WORLD.seen.has(key)) continue;
    WORLD.seen.add(key);
    if (now - t.ts > 45) continue;            // don't replay old history on load
    const from = WORLD.actors[t.from], target = pos[t.to];
    if (!from || !target || t.from === t.to) continue;
    const spot = sanitizeTarget(target.x + 34, target.y);
    from.path = buildRoute(from.x, from.y, spot.x, spot.y);
    from.errand = { to: t.to, topic: t.topic };
    from.wait = 1.8;
  }
}

function stepWorld(dt) {
  for (const act of Object.values(WORLD.actors)) {
    if (act.path.length) {
      const t = act.path[0];
      const dx = t.x - act.x, dy = t.y - act.y;
      const d = Math.hypot(dx, dy);
      if (d < 2.5) {
        act.path.shift();
        if (!act.path.length) act.arrived = performance.now();
      } else {
        act.x += (dx / d) * WALK_SPEED * dt;
        act.y += (dy / d) * WALK_SPEED * dt;
        if (Math.abs(dx) > 0.7) act.facing = dx < 0 ? -1 : 1;
      }
      act.moving = true;
    } else if (act.wait > 0) {
      act.wait -= dt;
      act.moving = false;
    } else {
      act.moving = false;
      act.errand = null;
      const h = act.home;
      if (h && Math.hypot(h.x - act.x, h.y - act.y) > 4) {
        act.path = buildRoute(act.x, act.y, h.x, h.y);   // wander back to station
      }
    }
  }
}

const atHome = act => act.home && !act.moving && act.wait <= 0 &&
                      Math.hypot(act.home.x - act.x, act.home.y - act.y) < 5;

// -- drawing ---------------------------------------------------------------
function frame(name) { return ATLAS.frames?.[name]; }

function shadow(ctx, cx, y, w) {
  ctx.save();
  ctx.fillStyle = 'rgba(0,0,0,0.28)';
  ctx.beginPath();
  ctx.ellipse(cx, y - 1, Math.max(5, w * 0.42), Math.max(2, w * 0.16), 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function drawSprite(ctx, name, cx, bottom, opts = {}) {
  const f = frame(name);
  if (!f) return;
  const scale = opts.scale ?? SCALE;
  const w = Math.round(f.w * scale), h = Math.round(f.h * scale);
  if (opts.shadow !== false) shadow(ctx, cx, bottom, w);
  const x = Math.round(cx - w / 2), y = Math.round(bottom - h);
  if (opts.flip) {
    ctx.save();
    ctx.translate(x + w, y);
    ctx.scale(-1, 1);
    ctx.drawImage(ATLAS.img, f.x, f.y, f.w, f.h, 0, 0, w, h);
    ctx.restore();
  } else {
    ctx.drawImage(ATLAS.img, f.x, f.y, f.w, f.h, x, y, w, h);
  }
}

function tileRect(ctx, name, x, y, w, h) {
  const f = frame(name);
  if (!f) return;
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();
  for (let ty = y; ty < y + h; ty += TILE) {
    for (let tx = x; tx < x + w; tx += TILE) {
      ctx.drawImage(ATLAS.img, f.x, f.y, f.w, f.h, tx, ty, TILE, TILE);
    }
  }
  ctx.restore();
}

/** A solid wall segment: cream face with a dark edge, matching the tile art. */
function wallRect(ctx, x, y, w, h) {
  if (w <= 0 || h <= 0) return;
  ctx.fillStyle = WALL_EDGE;
  ctx.fillRect(x, y, w, h);
  ctx.fillStyle = WALL_FACE;
  ctx.fillRect(x + 1, y + 1, w - 2, h - 2);
}

/** Draws the full enclosure: back band, both sides, and the front wall with
 *  its doorway gap. The gap is simply not painted, so the corridor floor
 *  already laid down shows through it. */
function drawShell(ctx, room) {
  const { x, y, w, h } = room;
  const ox = x - WT, ow = w + WT * 2;

  if (room.backWall !== false) {
    const f = frame('wall_run');
    if (f) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(ox, y - WALL_H, ow, WALL_H);
      ctx.clip();
      for (let i = 0; i * 64 < ow; i++) {
        ctx.drawImage(ATLAS.img, f.x, f.y, f.w, f.h, ox + i * 64, y - WALL_H, 64, WALL_H);
      }
      ctx.restore();
    }
  }

  // Side walls run the full height and tuck into the front wall.
  wallRect(ctx, ox, y, WT, h + WT);
  wallRect(ctx, x + w, y, WT, h + WT);

  const gap0 = room.doorX - DOOR_W / 2, gap1 = room.doorX + DOOR_W / 2;
  const frontY = room.doorSide === 'top' ? y - WT : y + h;

  if (room.doorSide === 'top' && room.backWall === false) {
    wallRect(ctx, ox, frontY, gap0 - ox, WT);
    wallRect(ctx, gap1, frontY, ox + ow - gap1, WT);
  } else if (room.doorSide === 'bottom') {
    wallRect(ctx, ox, frontY, gap0 - ox, WT);
    wallRect(ctx, gap1, frontY, ox + ow - gap1, WT);
  } else {
    wallRect(ctx, ox, y + h, ow, WT);
  }

  // Door jambs, so the opening reads as a doorway rather than a missing wall.
  ctx.fillStyle = WALL_EDGE;
  ctx.fillRect(gap0 - 2, frontY, 3, WT);
  ctx.fillRect(gap1 - 1, frontY, 3, WT);
  ctx.fillStyle = 'rgba(0,0,0,0.25)';
  ctx.fillRect(gap0 + 1, frontY, DOOR_W - 2, 3);   // threshold shadow
}

function drawCounter(ctx, room) {
  // Only the middle section is a straight run -- the supplied end caps are
  // L-shaped corner pieces and read as stray blocks in a straight wall.
  const m = frame('counter_m');
  if (!m) return;
  const sc = 1.15;
  const mw = Math.round(m.w * sc), h = Math.round(m.h * sc);
  const bottom = room.y + 84;
  ctx.save();
  ctx.beginPath();
  ctx.rect(room.x + 2, room.y, room.w - 4, room.h);
  ctx.clip();
  for (let x = room.x + 2; x < room.x + room.w - 2; x += mw) {
    ctx.drawImage(ATLAS.img, m.x, m.y, m.w, m.h, x, bottom - h, mw, h);
  }
  ctx.restore();
}

function paintCanvas() {
  const cv = $('#floorcanvas');
  if (!cv || !ATLAS.ready) return;
  const ctx = cv.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, FW, FH);

  tileRect(ctx, 'floor_lino', 0, 0, FW, FH);
  ctx.fillStyle = 'rgba(0,0,0,0.20)';
  ctx.fillRect(0, 0, FW, FH);

  const actors = Object.values(WORLD.actors);
  const staffed = new Set(actors.map(a => a.home?.room));

  for (const room of ROOMS) {
    ctx.globalAlpha = staffed.has(room.id) ? 1 : 0.62;
    if (room.tile) {
      tileRect(ctx, room.tile, room.x, room.y, room.w, room.h);
      drawShell(ctx, room);
    }
    if (room.counter) drawCounter(ctx, room);

    const wall = room.wall || [];
    const step = room.w / (wall.length + 1);
    wall.forEach((prop, i) => {
      drawSprite(ctx, prop, room.x + step * (i + 1),
                 room.y + (room.counter ? 52 : 54), { shadow: !room.counter });
    });
    (room.floorProps || []).forEach(([prop, fx, fy]) => {
      drawSprite(ctx, prop, room.x + room.w * fx, room.y + room.h * fy);
    });
    ctx.globalAlpha = 1;
  }

  // Highlight the room under the cursor / the selected agent's room.
  const focusRoom = WORLD.hover ? WORLD.actors[WORLD.hover]?.home?.room
                                : WORLD.actors[state.agent]?.home?.room;
  const fr = focusRoom && roomById(focusRoom);
  if (fr && fr.tile) {
    ctx.save();
    ctx.strokeStyle = 'rgba(111,191,149,0.9)';
    ctx.lineWidth = 2;
    ctx.strokeRect(fr.x + 1, fr.y + 1, fr.w - 2, fr.h - 2);
    ctx.restore();
  }

  actors.sort((a, b) => a.y - b.y).forEach(act => {
    const seated = atHome(act) && SIT_SPRITE[act.agent.role];
    const name = seated || spriteFor(act.agent);
    // A walking figure bobs; a seated one does not.
    const bob = act.moving ? Math.round(Math.sin(performance.now() / 90) * 1.2) : 0;
    drawSprite(ctx, name, act.x, act.y + bob,
               { flip: !seated && act.facing < 0 });
  });
}

const CHAR_H = 34 * SCALE;

// -- overlay ---------------------------------------------------------------
// Built once per roster change with coordinates relative to each actor, then
// moved every frame with a transform -- rebuilding this at 60fps would be
// wasteful and would kill the browser's text rendering cache.
function floorOverlay() {
  const f = state.floor;
  if (!f || !f.agents.length) return '';

  // Rooms with a back wall band get their name on the wall; the lobby has no
  // band, so its name goes just inside its own front wall.
  const labels = ROOMS.map(r => (r.backWall === false
    ? `<text class="room-label" x="${r.x + 10}" y="${r.y + 17}">${esc(r.label)}</text>`
    : `<text class="room-label" x="${r.x + 8}" y="${r.y - WALL_H + 15}"
             >${esc(r.label)}</text>`)).join('');

  const people = f.agents.map(a => `
    <g class="station clickable" data-agent="${esc(a.name)}" id="actor-${esc(a.name)}">
      <rect class="hit" x="-16" y="${-CHAR_H}" width="32" height="${CHAR_H + 6}"/>
      <rect class="sel" x="-20" y="${-CHAR_H - 4}" width="40" height="${CHAR_H + 10}" rx="5"/>
      <circle class="pip" cx="-20" cy="${-CHAR_H - 8}" r="3.5"/>
      <text class="pix-status" x="-13" y="${-CHAR_H - 5}"></text>
      <text class="pix-name" x="0" y="13" text-anchor="middle"></text>
    </g>`).join('');

  return `<rect class="floorhit" x="0" y="0" width="${FW}" height="${FH}"/>
          <g class="wires"></g>${labels}${people}`;
}

function paintOverlay() {
  const svg = $('.flooroverlay');
  if (!svg) return;

  for (const [name, act] of Object.entries(WORLD.actors)) {
    const g = svg.querySelector(`#actor-${CSS.escape(name)}`);
    if (!g) continue;
    g.setAttribute('transform', `translate(${act.x.toFixed(1)},${act.y.toFixed(1)})`);
    const c = STATUS_COLOR[act.agent.status] || 'var(--skip)';
    const status = act.errand ? `→ ${act.errand.to}` : act.agent.status;
    g.querySelector('.pix-status').textContent = status;
    g.querySelector('.pix-status').setAttribute('fill', c);
    g.querySelector('.pip').setAttribute('fill', c);
    g.querySelector('.pix-name').textContent =
      act.agent.name + (act.agent.inbox_pending ? ` (${act.agent.inbox_pending})` : '');
    const sel = g.querySelector('.sel');
    const on = state.agent === name || WORLD.hover === name;
    sel.setAttribute('stroke', on ? c : 'transparent');
    g.classList.toggle('on', state.agent === name);
  }

  // Wires are redrawn each frame because their endpoints are moving.
  const wires = svg.querySelector('.wires');
  if (!wires) return;
  const now = Date.now() / 1000;
  wires.innerHTML = (state.floor?.traffic || [])
    .filter(t => now - t.ts < 20 && WORLD.actors[t.from] && WORLD.actors[t.to])
    .slice(-4).map(t => {
      const a = WORLD.actors[t.from], b = WORLD.actors[t.to];
      const ay = a.y - CHAR_H, by = b.y - CHAR_H;
      const d = `M${a.x.toFixed(1)},${ay.toFixed(1)} Q${((a.x + b.x) / 2).toFixed(1)},${
        (Math.min(ay, by) - 44).toFixed(1)} ${b.x.toFixed(1)},${by.toFixed(1)}`;
      return `<path class="wire" d="${d}"/>`;
    }).join('');
}

function renderFloor() {
  const f = state.floor;
  if (!f) return '<div class="empty">connecting…</div>';
  if (!f.agents.length) {
    return `<div class="empty">The floor is empty — no agents enrolled yet.<br><br>
      <code>bioharness mission "call variants in this sample" --roles pi planner curator</code>
      </div>`;
  }
  return `<div class="floorstage">
    <canvas id="floorcanvas" width="${FW}" height="${FH}"></canvas>
    <svg class="flooroverlay" viewBox="0 0 ${FW} ${FH}"
         preserveAspectRatio="none">${floorOverlay()}</svg>
  </div>`;
}

// -- the loop --------------------------------------------------------------
function tickWorld(ts) {
  WORLD.raf = requestAnimationFrame(tickWorld);
  const dt = Math.min(0.05, (ts - WORLD.last) / 1000 || 0);
  WORLD.last = ts;
  if (state.tab !== 'floor' || !ATLAS.ready) return;
  stepWorld(dt);
  paintCanvas();
  paintOverlay();
}

function startWorld() {
  if (!WORLD.raf) WORLD.raf = requestAnimationFrame(tickWorld);
}

// -- pointer ---------------------------------------------------------------
function svgPoint(evt, svg) {
  const pt = svg.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  const ctm = svg.getScreenCTM();
  return ctm ? pt.matrixTransform(ctm.inverse()) : null;
}

function walkTo(name, x, y) {
  const act = WORLD.actors[name];
  if (!act) return;
  // Clicking a wall would otherwise ask someone to stand inside it; snap the
  // destination to the nearest place a person could actually stand.
  const t = sanitizeTarget(Math.max(12, Math.min(FW - 12, x)),
                           Math.max(40, Math.min(FH - 8, y)));
  act.path = buildRoute(act.x, act.y, t.x, t.y);
  act.wait = 2.5;          // linger where sent before drifting back to station
  act.errand = null;
}

document.addEventListener('mousemove', e => {
  const svg = e.target.closest?.('.flooroverlay');
  if (!svg) { WORLD.hover = null; return; }
  const p = svgPoint(e, svg);
  if (!p) return;
  let best = null, bestD = 26;
  for (const [name, act] of Object.entries(WORLD.actors)) {
    const d = Math.hypot(act.x - p.x, (act.y - CHAR_H / 2) - p.y);
    if (d < bestD) { bestD = d; best = name; }
  }
  WORLD.hover = best;
});
// ==========================================================================
// Command center
// ==========================================================================
function ccActivity() {
  const rows = (state.floor?.activity || []).map(e => `<div>
      <span class="t">${clock(e.ts)}</span>
      <span class="e">${esc(e.event)}</span>
      <span class="p">${esc(JSON.stringify(e.payload || {}).slice(0, 90))}</span>
    </div>`).join('');
  return rows ? `<div class="stream">${rows}</div>`
              : '<div class="empty">No events yet.</div>';
}

function ccWorkers() {
  const agents = state.floor?.agents || [];
  if (!agents.length) return '<div class="empty">No agents enrolled.</div>';
  return agents.map(a => `<div class="card"><div class="card-body">
      <div style="display:flex;gap:8px;align-items:center">
        <b>${esc(a.name)}</b> ${chip(a.status)}
        <span style="margin-left:auto" class="mono">${esc(a.role)}</span></div>
      <div class="kv" style="margin-top:6px">
        <span class="k">provider</span> ${esc(a.provider)} ·
        <span class="k">autonomy</span> ${esc(a.autonomy || '—')} ·
        <span class="k">mail</span> ${a.inbox_pending}</div>
      ${a.last_event ? `<div class="kv"><span class="k">last</span>
        ${esc(a.last_event)} at ${clock(a.last_seen)}</div>` : ''}
      ${a.note ? `<div class="msg">${esc(a.note)}</div>` : ''}
    </div></div>`).join('');
}

function ccStaff() {
  const opt = state.staffOptions;
  if (!opt) return '<div class="empty">loading…</div>';
  const agents = state.floor?.agents || [];

  const roster = agents.map(a => `<tr>
      <td class="mono">${esc(a.name)}</td>
      <td class="mono" style="color:var(--muted)">${esc(a.role)}</td>
      <td class="mono">${esc(a.provider)}${a.model ? `<br><span
        style="color:var(--muted)">${esc(a.model)}</span>` : ''}</td>
      <td><button class="mini" data-fire="${esc(a.name)}">dismiss</button></td>
    </tr>`).join('');

  const f = state.hireForm;
  const sel = (v, cur) => (v === cur ? 'selected' : '');
  const provOpts = opt.providers.map(p =>
    `<option value="${esc(p.id)}" ${p.available ? '' : 'disabled'} ${sel(p.id, f.provider)}
      >${esc(p.label)}${p.available ? '' : ' — ' + esc(p.reason)}</option>`).join('');
  const api = opt.providers.find(p => p.id === 'claude-api');

  return `<div class="card"><h3>Roster</h3>
      ${roster ? `<table><tbody>${roster}</tbody></table>`
               : '<div class="empty">nobody hired yet</div>'}</div>

    <div class="card"><h3>Hire an agent</h3><div class="card-body">
      <div class="field"><label for="hname">name</label>
        <input class="search" id="hname" placeholder="e.g. ada" autocomplete="off"
               value="${esc(f.name)}"></div>
      <div class="field"><label for="hrole">role</label>
        <select class="pick wide" id="hrole">${
          opt.roles.map(r => `<option ${sel(r, f.role)}>${esc(r)}</option>`).join('')
          }</select></div>
      <div class="field"><label for="hprov">provider</label>
        <select class="pick wide" id="hprov">${provOpts}</select></div>
      <div class="field"><label for="hmodel">model</label>
        <select class="pick wide" id="hmodel">${
          (api?.models || []).map(m => `<option ${sel(m, f.model)}>${esc(m)}</option>`).join('')
          || '<option value="">n/a</option>'}</select></div>
      <div class="field"><label for="hauto">autonomy</label>
        <select class="pick wide" id="hauto">${
          opt.autonomy.map(a => `<option ${sel(a, f.autonomy)}>${esc(a)}</option>`
            ).join('')}</select></div>
      <button class="send" id="hire" ${state.hiring ? 'disabled' : ''}
        style="margin-top:8px">hire</button>
      ${state.hireMsg ? `<div class="sent">${esc(state.hireMsg)}</div>` : ''}
      ${api && !api.available ? `<div class="sent" style="color:var(--run)">
        Claude API: ${esc(api.reason)}</div>` : ''}
    </div></div>`;
}

function ccMemory() {
  return `<input class="search" id="recall" placeholder="Search agent memory…">
          <div id="recall-out"></div>`;
}

function ccTasks() {
  const tasks = state.detail?.tasks || [];
  if (!tasks.length) return '<div class="empty">No run selected.</div>';
  return `<table><tbody>${tasks.map(t => `<tr class="clickable" data-step="${esc(t.step)}">
      <td>${pill(t.status)}</td><td class="mono">${esc(t.step)}</td>
      <td class="mono">${fmtSecs(t.seconds)}</td></tr>`).join('')}</tbody></table>
    ${state.log ? `<pre class="log">${esc(state.log.text)}</pre>` : ''}`;
}

function ccGraph() {
  const d = state.detail;
  if (!d) return '<div class="empty">No run selected.</div>';
  return renderDag(d.graph || [], d.tasks || []);
}

function ccHeader() {
  const run = state.floor?.current_run;
  return {
    status: run ? pill(run.status) : '',
    sub: run ? `${esc(run.workflow)} · ${esc(shortId(run.run_id))}` : 'no run recorded',
  };
}

function ccBody() {
  return { activity: ccActivity, workers: ccWorkers, staff: ccStaff,
           memory: ccMemory, tasks: ccTasks, graph: ccGraph }[state.cc]();
}

const TOPICS = ['goal', 'plan.request', 'data.validate', 'qc.check', 'analyze', 'report'];

// Built once per tab switch. Everything that ticks is updated in place below,
// so the message box is never destroyed under the user's cursor.
function floorScaffold() {
  return `<div class="floorwrap">
    <div class="stage"><div class="stage-inner" id="stage"></div></div>
    <div class="cc">
      <div class="cc-head">
        <div class="cc-title">COMMAND CENTER <span id="cc-status"></span></div>
        <div class="cc-sub" id="cc-sub"></div>
      </div>
      <div class="cc-tabs" id="cc-tabs"></div>
      <div class="cc-body" id="cc-body"></div>
      <div class="cc-foot">
        <textarea class="queue" id="qmsg" placeholder="Message an agent…"></textarea>
        <div class="queue-row">
          <select class="pick" id="qto"></select>
          <select class="pick" id="qtopic">${
            TOPICS.map(t => `<option>${t}</option>`).join('')}</select>
          <button class="send" id="qsend">send</button>
        </div>
        <div class="sent" id="qsent"></div>
      </div>
    </div>
    <div class="strip" id="strip"></div>
  </div>`;
}

function paintFloor() {
  const head = ccHeader();
  $('#cc-status').innerHTML = head.status;
  $('#cc-sub').innerHTML = head.sub;
  $('#cc-tabs').innerHTML = CC_TABS.map(t =>
    `<button data-cc="${t}" class="${state.cc === t ? 'on' : ''}">${t}</button>`).join('');
  // Build the stage once; after that only the pixels and the overlay change.
  // Recreating the canvas every tick threw away the drawing and flickered.
  syncWorld();
  // Rebuild the overlay only when the roster changes; the animation loop moves
  // the existing groups every frame.
  const overlay = $('.flooroverlay');
  const roster = (state.floor?.agents || []).map(a => a.name).join(',');
  if (!overlay || $('#stage').dataset.roster !== roster) {
    $('#stage').innerHTML = renderFloor();
    $('#stage').dataset.roster = roster;
  }
  paintCanvas();
  paintOverlay();
  $('#strip').innerHTML = workerStripInner();

  // Only repaint the body when nothing inside it is being typed into.
  const body = $('#cc-body');
  if (!body.contains(document.activeElement)) body.innerHTML = ccBody();

  // Rebuild the recipient list only when the roster actually changes, so the
  // dropdown does not reset while it is open.
  const names = (state.floor?.agents || []).map(a => a.name);
  const sel = $('#qto');
  if (sel && sel.dataset.names !== names.join(',')) {
    const keep = sel.value;
    const preferred = state.agent || keep ||
      (state.floor?.agents.find(a => a.role === 'pi') || {}).name || names[0];
    sel.innerHTML = names.map(n =>
      `<option value="${esc(n)}" ${n === preferred ? 'selected' : ''}>${esc(n)}</option>`).join('');
    sel.dataset.names = names.join(',');
  }
  $('#qsend').disabled = state.sending;
  $('#qsent').textContent = state.sent;
}

function workerStripInner() {
  const agents = state.floor?.agents || [];
  return agents.map(a => `
    <div class="wcard ${state.agent === a.name ? 'on' : ''}" data-agent="${esc(a.name)}">
      <div class="av" style="background:${avatarFill(a.name)}">${esc(initials(a.name))}</div>
      <div><div class="nm">${esc(a.name)}</div><div class="rl">${esc(a.role)}</div></div>
      <div style="margin-left:auto">${chip(a.status)}</div>
    </div>`).join('') || '<div class="empty">no agents</div>';
}

// ==========================================================================
// DAG (shared by the runs view and the command center)
// ==========================================================================
function layout(graph) {
  const byId = Object.fromEntries(graph.map(s => [s.id, s]));
  const depth = {};
  const walk = (id, seen = new Set()) => {
    if (depth[id] != null) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const needs = (byId[id]?.needs || []).filter(n => byId[n]);
    depth[id] = needs.length ? Math.max(...needs.map(n => walk(n, seen))) + 1 : 0;
    return depth[id];
  };
  graph.forEach(s => walk(s.id));
  const cols = {};
  graph.forEach(s => (cols[depth[s.id]] ||= []).push(s));
  return cols;
}

function renderDag(graph, tasks, compact = false) {
  if (!graph.length) return '<div class="empty">No graph recorded for this run.</div>';
  const status = {};
  tasks.forEach(t => {
    const base = String(t.step).split('.')[0];
    status[base] = status[base] && status[base] !== t.status ? 'running' : t.status;
  });
  const cols = layout(graph);
  const W = compact ? 130 : 172, H = compact ? 44 : 54;
  const GX = compact ? 40 : 68, GY = 22, PAD = 16;
  const depths = Object.keys(cols).map(Number).sort((a, b) => a - b);
  const pos = {};
  depths.forEach(d => cols[d].forEach((s, i) => {
    pos[s.id] = { x: PAD + d * (W + GX), y: PAD + i * (H + GY) };
  }));
  const width = PAD * 2 + depths.length * W + (depths.length - 1) * GX;
  const height = PAD * 2 + Math.max(...depths.map(d => cols[d].length)) * (H + GY);
  const color = s => ({ ok: 'var(--ok)', failed: 'var(--fail)', running: 'var(--run)',
    blocked: 'var(--block)' }[s] || 'var(--skip)');

  let edges = '', nodes = '';
  graph.forEach(s => (s.needs || []).forEach(n => {
    if (!pos[n]) return;
    const a = pos[n], b = pos[s.id];
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
    edges += `<path class="edge" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
  }));
  graph.forEach(s => {
    const p = pos[s.id], st = status[s.id] || 'pending', c = color(st);
    const shards = tasks.filter(t => String(t.step).split('.')[0] === s.id);
    const label = s.scatter && shards.length > 1 ? `${s.id} ×${shards.length}` : s.id;
    nodes += `<g class="node clickable" data-step="${esc(s.id)}" transform="translate(${p.x},${p.y})">
      <rect width="${W}" height="${H}" fill="var(--panel)" stroke="${c}"/>
      <rect width="4" height="${H}" fill="${c}" rx="2"/>
      <text x="14" y="${compact ? 19 : 21}" fill="var(--ink)" font-weight="500"
            font-size="${compact ? 11 : 12}">${esc(label)}</text>
      <text x="14" y="${compact ? 33 : 38}" class="cap">${esc(s.capability)}</text>
      ${compact ? '' : `<text x="${W - 12}" y="21" text-anchor="end" font-size="10"
        fill="${c}" font-family="var(--mono)">${esc(st)}</text>`}
    </g>`;
  });
  return `<div class="scroll"><svg class="dag" width="${width}" height="${height}"
    viewBox="0 0 ${width} ${height}">${edges}${nodes}</svg></div>`;
}

// ==========================================================================
// Classic views
// ==========================================================================
function viewRuns() {
  const d = state.detail;
  if (!d) return `<div class="empty">No runs yet. Try:<br><br>
    <code>bioharness run wgs-variants --dry-run --set reads=… --set reference=…</code></div>`;
  const m = d.manifest, s = d.summary || {}, tasks = d.tasks || [];
  const rows = tasks.map(t => `<tr class="clickable" data-step="${esc(t.step)}">
      <td>${pill(t.status)}</td><td class="mono">${esc(t.step)}</td>
      <td><code>${esc(t.capability)}</code></td><td class="mono">${fmtSecs(t.seconds)}</td>
      <td><span class="trunc mono" title="${esc(t.command || t.error)}">${
        esc(t.error || t.command || '')}</span></td></tr>`).join('');
  const arts = (d.artifacts || []).map(a => `<tr>
      <td class="mono">${esc(a.name)}</td><td><span class="trunc mono">${esc(a.path)}</span></td>
      <td class="mono">${(a.bytes / 1024).toFixed(1)} kB</td>
      <td class="mono" title="${esc(a.sha256)}">${esc(String(a.sha256).slice(0, 12))}…</td></tr>`).join('');
  return `
  <h2>${esc(m.workflow)} ${m.dry_run ? pill('dry-run') : pill(s.status || 'running')}</h2>
  <div class="sub mono">${esc(m.run_id)} · ${esc(m.created_iso || '')} · ${esc(d.dir)}</div>
  <div class="card"><h3>Pipeline</h3>${renderDag(d.graph || [], tasks)}</div>
  <div class="card"><h3>Steps</h3><div class="scroll"><table>
    <thead><tr><th>Status</th><th>Step</th><th>Capability</th><th>Time</th>
    <th>Command / error</th></tr></thead><tbody>${rows}</tbody></table></div></div>
  ${state.log ? `<div class="card"><h3>${esc(state.log.step)} — logs</h3>
    <div class="tabs">${['stderr', 'stdout'].map(t => `<button data-logtab="${t}"
      class="${state.logTab === t ? 'on' : ''}">${t}</button>`).join('')}</div>
    <pre class="log">${esc(state.log.text || '(empty)')}</pre></div>` : ''}
  <div class="card"><h3>Parameters</h3><div class="card-body kv">
    ${Object.entries(m.params || {}).map(([k, v]) =>
      `<div><span class="k">${esc(k)}</span> = ${esc(JSON.stringify(v))}</div>`).join('')
      || 'none'}</div></div>
  <div class="card"><h3>Artifacts (SHA-256)</h3>${arts ? `<div class="scroll"><table>
    <thead><tr><th>Port</th><th>Path</th><th>Size</th><th>Hash</th></tr></thead>
    <tbody>${arts}</tbody></table></div>` : '<div class="empty">No artifacts recorded.</div>'}</div>
  <details><summary>Provenance ledger (${(d.ledger || []).length} events)</summary>
    <pre class="log">${esc((d.ledger || []).map(e =>
      `${new Date(e.ts * 1000).toISOString()}  ${e.event}  ${JSON.stringify(e.payload)}`
    ).join('\n'))}</pre></details>`;
}

function viewHive() {
  const h = state.hive;
  if (!h || !h.exists) return `<div class="empty">No hive at this path yet.</div>`;
  const agents = (h.agents || []).map(a => `<div class="card">
    <h3>${esc(a.name)} · ${esc(a.role)}</h3><div class="card-body">
      <div class="kv"><span class="k">provider</span> ${esc(a.provider)}
        · <span class="k">autonomy</span> ${esc(a.autonomy || '—')}
        · <span class="k">inbox</span> ${a.inbox_pending}</div>
      ${(a.inbox || []).map(m => `<div class="msg"><b>${esc(m.topic)}</b> from ${esc(m.sender)}<br>
        <span class="mono">${esc(JSON.stringify(m.body).slice(0, 220))}</span></div>`).join('')}
      ${a.memory ? `<details><summary>memory.md</summary>
        <pre class="log">${esc(a.memory)}</pre></details>` : ''}
    </div></div>`).join('');
  const bb = (h.blackboard || []).map(e => `<tr><td class="mono">${esc(e.key)}</td>
      <td class="mono">${esc(e.agent || '')}</td>
      <td><span class="trunc mono">${esc(JSON.stringify(e.value))}</span></td></tr>`).join('');
  const ev = (h.events || []).slice(0, 60).map(e =>
    `${clock(e.ts)}  ${e.event}  ${JSON.stringify(e.payload).slice(0, 150)}`).join('\n');
  return `<h2>Hive</h2><div class="sub mono">${esc(h.root)}</div>
    <div class="card"><h3>Memory palace</h3><div class="card-body">
      <input class="search" id="recall" placeholder="Search agent memory…">
      <div id="recall-out"></div></div></div>
    ${agents || '<div class="empty">No agents enrolled.</div>'}
    <div class="card"><h3>Blackboard</h3>${bb ? `<div class="scroll"><table>
      <thead><tr><th>Key</th><th>Agent</th><th>Value</th></tr></thead>
      <tbody>${bb}</tbody></table></div>` : '<div class="empty">Empty.</div>'}</div>
    <div class="card"><h3>Event log</h3><pre class="log">${esc(ev || '(none)')}</pre></div>`;
}

function viewCaps() {
  const caps = state.caps || [];
  const domains = [...new Set(caps.map(c => c.domain))];
  return `<h2>Capability registry</h2>
    <div class="sub">${caps.filter(c => c.available).length} of ${caps.length}
      available on this machine. Drop a YAML manifest in <code>capabilities/</code> to add one.</div>
    ${domains.map(dom => `<div class="card"><h3>${esc(dom)}</h3><div class="scroll"><table>
      <thead><tr><th></th><th>ID</th><th>Ports</th><th>Runtime</th><th>Summary</th>
      <th>Probe</th></tr></thead><tbody>
      ${caps.filter(c => c.domain === dom).map(c => `<tr>
        <td><span class="dot${c.available ? '' : ' stale'}"></span></td>
        <td class="mono">${esc(c.id)} <span style="color:var(--muted)">${esc(c.version)}</span></td>
        <td class="mono" style="color:var(--muted)">${esc(c.inputs.map(p => p.type).join(', ') || '—')}
          → ${esc(c.outputs.map(p => p.type).join(', ') || '—')}</td>
        <td class="mono">${esc(c.runtime)}</td><td>${esc(c.summary)}</td>
        <td class="mono" style="color:var(--muted)">${esc(c.reason)}</td></tr>`).join('')}
      </tbody></table></div></div>`).join('')}`;
}

function viewCost() {
  const o = state.overview || {}, c = state.hive?.cost || o.cost || {};
  const rows = Object.entries(c.by_agent || {}).map(([a, v]) => `<tr>
      <td class="mono">${esc(a)}</td><td class="mono">${v.calls}</td>
      <td class="mono">${v.tokens_in.toLocaleString()}</td>
      <td class="mono">${v.tokens_out.toLocaleString()}</td>
      <td class="mono">$${v.usd.toFixed(4)}</td></tr>`).join('');
  const st = (n, l) => `<div class="card stat"><div class="n">${n}</div><div class="l">${l}</div></div>`;
  return `<h2>Cost &amp; guardrails</h2>
    <div class="sub">Real token accounting from the hive event log.
      Deterministic agents cost nothing.</div>
    <div class="grid2">
      ${st('$' + (c.total_usd || 0).toFixed(4), 'total spend')}
      ${st((c.total_tokens || 0).toLocaleString(), 'tokens')}
      ${st(o.runs?.total || 0, 'runs recorded')}
      ${st(`${o.capabilities?.available || 0}/${o.capabilities?.total || 0}`, 'capabilities available')}
    </div>
    <div class="card"><h3>By agent</h3>${rows ? `<div class="scroll"><table>
      <thead><tr><th>Agent</th><th>Calls</th><th>Tokens in</th><th>Tokens out</th><th>USD</th>
      </tr></thead><tbody>${rows}</tbody></table></div>`
      : '<div class="empty">No LLM calls charged — the roster has been running deterministically.</div>'}
    </div>`;
}

// ==========================================================================
// Shell
// ==========================================================================
function sidebar() {
  const runs = state.runs.map(r => `
    <div class="run-item ${r.run_id === state.runId ? 'on' : ''}" data-run="${esc(r.run_id)}">
      <span class="pill s-${esc(r.status)}" style="padding:1px 6px">${
        esc(r.status[0].toUpperCase())}</span>
      <span class="wf">${esc(r.workflow)}</span>
      <span class="id">${esc(shortId(r.run_id))}</span></div>`).join('');
  const agents = (state.floor?.agents || state.hive?.agents || []).map(a => `
    <div class="agent-row"><span class="dot"></span>${esc(a.name)}
      <span class="badge">${a.inbox_pending}</span>
      <span class="role">${esc(a.role)}</span></div>`).join('');
  const g = state.detail?.summary?.guardrails;
  const cost = state.overview?.cost || {};
  const budget = g ? `
    <div><span class="k">steps</span><span>${g.used.steps} / ${
      g.used.steps + (g.remaining.steps ?? 0)}</span></div>
    <div><span class="k">spend</span><span>$${(g.used.usd || 0).toFixed(4)}</span></div>
    <div><span class="k">tokens</span><span>${(g.used.tokens || 0).toLocaleString()}</span></div>
    <div><span class="k">elapsed</span><span>${fmtSecs(g.used.elapsed_s)}</span></div>
    <div><span class="k">breakers</span><span>${
      g.open_breakers.length ? g.open_breakers.join(', ') : 'closed'}</span></div>`
    : `<div><span class="k">spend</span><span>$${(cost.total_usd || 0).toFixed(4)}</span></div>
       <div><span class="k">tokens</span><span>${(cost.total_tokens || 0).toLocaleString()}</span></div>`;
  return `<aside>
    <div class="side-h">Runs</div>${runs || '<div class="empty">none</div>'}
    <div class="side-h">Agents</div>${agents || '<div class="empty">none</div>'}
    <div class="side-h">Budget</div><div class="budget">${budget}</div></aside>`;
}

function render() {
  $('#nav').innerHTML = TABS.map(t =>
    `<button data-tab="${t}" class="${state.tab === t ? 'on' : ''}">${t}</button>`).join('');

  if (state.tab === 'floor') {
    if (!$('.floorwrap')) $('#shell').innerHTML = floorScaffold();
    return paintFloor();
  }

  // Classic views rebuild wholesale; skip the repaint while a field in them has
  // focus so a search box is not cleared mid-word.
  const shell = $('#shell');
  if (shell.contains(document.activeElement) &&
      /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
  const view = { runs: viewRuns, hive: viewHive, caps: viewCaps, cost: viewCost }[state.tab];
  shell.innerHTML = `<div class="classic">${sidebar()}<section>${view()}</section></div>`;
}

async function refresh(full = false) {
  try {
    const [runs, overview, floor] = await Promise.all(
      [api('/api/runs'), api('/api/overview'), api('/api/floor')]);
    state.runs = runs; state.overview = overview; state.floor = floor;
    if (!state.runId && runs.length) state.runId = runs[0].run_id;
    if (state.runId) state.detail = await api(`/api/runs/${state.runId}`).catch(() => null);
    if (state.tab === 'hive' || full || !state.hive) state.hive = await api('/api/hive');
    if ((state.tab === 'caps' && !state.caps) || full) state.caps = await api('/api/caps');
    if (!state.staffOptions) {
      state.staffOptions = await api('/api/staff/options');
      state.hireForm.role ||= state.staffOptions.roles[0];
      state.hireForm.model ||= (state.staffOptions.providers
        .find(p => p.id === 'claude-api')?.models || [])[0] || '';
    }
    $('#pulse').className = 'dot';
    $('#tick').textContent = new Date().toLocaleTimeString();
  } catch {
    $('#pulse').className = 'dot stale';
    $('#tick').textContent = 'disconnected';
  }
  render();
}

async function loadLog(step) {
  state.log = { step, text: 'loading…' };
  render();
  const r = await api(`/api/runs/${state.runId}/log?step=${
    encodeURIComponent(step)}&stream=${state.logTab}`);
  state.log = { step, text: r.exists ? r.text : `(no ${state.logTab}.log for ${step})` };
  render();
}

/** Copy whatever is currently in the hire fields into state. */
function syncHireForm() {
  const f = state.hireForm;
  const get = id => $(`#${id}`)?.value;
  f.name = get('hname') ?? f.name;
  f.role = get('hrole') ?? f.role;
  f.provider = get('hprov') ?? f.provider;
  f.model = get('hmodel') ?? f.model;
  f.autonomy = get('hauto') ?? f.autonomy;
}

async function hireAgent() {
  syncHireForm();
  const body = { ...state.hireForm, name: state.hireForm.name.trim() };
  if (!body.name) { state.hireMsg = 'give them a name first'; return render(); }
  state.hiring = true; render();
  const r = await api('/api/staff/hire', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  state.hiring = false;
  state.hireMsg = r.ok ? `hired ${r.agent.name} as ${r.agent.role}` : `refused: ${r.error}`;
  if (r.ok) state.hireForm.name = '';
  await refresh();
  // The roster changed, so the cc body is stale even though it holds focus.
  const b = $('#cc-body'); if (b) b.innerHTML = ccBody();
}

async function fireAgent(name) {
  const r = await api('/api/staff/fire', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  state.hireMsg = r.ok ? `dismissed ${name}` : `refused: ${r.error}`;
  if (state.agent === name) state.agent = null;
  delete WORLD.actors[name];
  await refresh();
  const b = $('#cc-body'); if (b) b.innerHTML = ccBody();
}

async function sendMessage() {
  const text = $('#qmsg')?.value.trim();
  if (!text) return;
  state.sending = true; render();
  const to = $('#qto')?.value, topic = $('#qtopic')?.value || 'goal';
  const body = topic === 'goal' ? { goal: text } : { note: text };
  const r = await api('/api/message', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ to, topic, body }),
  });
  state.sending = false;
  state.sent = r.ok ? `queued ${r.topic} → ${r.to} (${r.id})` : `refused: ${r.error}`;
  const box = $('#qmsg');
  if (r.ok && box) box.value = '';
  await refresh();
}

document.addEventListener('click', e => {
  const t = e.target.closest('[data-tab]');
  if (t) { state.tab = t.dataset.tab; state.log = null;
           $('#shell').innerHTML = ''; return refresh(); }
  const cc = e.target.closest('[data-cc]');
  if (cc) { state.cc = cc.dataset.cc;
            const b = $('#cc-body'); if (b) b.innerHTML = ccBody();
            return render(); }
  const ag = e.target.closest('[data-agent]');
  if (ag) {
    state.agent = state.agent === ag.dataset.agent ? null : ag.dataset.agent;
    state.cc = 'workers';
    const b = $('#cc-body'); if (b) b.innerHTML = ccBody();
    return render();
  }
  if (e.target.classList?.contains('floorhit')) {
    const svg = e.target.ownerSVGElement;
    const p = svg && svgPoint(e, svg);
    if (!p) return;
    // With someone selected, clicking the floor sends them walking there.
    // With nobody selected, it just clears the selection.
    if (state.agent && WORLD.actors[state.agent]) return walkTo(state.agent, p.x, p.y);
    state.agent = null;
    return render();
  }
  const run = e.target.closest('[data-run]');
  if (run) { state.runId = run.dataset.run; state.log = null; return refresh(); }
  const step = e.target.closest('[data-step]');
  if (step) return loadLog(step.dataset.step);
  const lt = e.target.closest('[data-logtab]');
  if (lt && state.log) { state.logTab = lt.dataset.logtab; return loadLog(state.log.step); }
  if (e.target.id === 'qsend') return sendMessage();
  if (e.target.id === 'hire') return hireAgent();
  const fire = e.target.closest('[data-fire]');
  if (fire) return fireAgent(fire.dataset.fire);
});

document.addEventListener('change', e => {
  if (e.target.closest?.('.cc-foot')) return;
  if (['hname', 'hrole', 'hprov', 'hmodel', 'hauto'].includes(e.target.id)) syncHireForm();
});

document.addEventListener('input', async e => {
  if (['hname'].includes(e.target.id)) syncHireForm();
  if (e.target.id !== 'recall') return;
  const q = e.target.value.trim();
  const out = $('#recall-out');
  if (!out) return;
  if (!q) { out.innerHTML = ''; return; }
  const hits = await api('/api/recall?q=' + encodeURIComponent(q));
  out.innerHTML = hits.length ? hits.map(h => `<div class="msg">
      <b>${esc(h.agent)}</b> <span style="color:var(--muted)">[${esc(h.kind)}]</span><br>
      ${esc(h.text.slice(0, 400))}</div>`).join('')
    : '<div class="empty">nothing remembered about that</div>';
});

loadAtlas().then(() => { refresh(true); startWorld(); });
setInterval(() => refresh(), 3000);
