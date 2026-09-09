'use strict';

const $ = (s, r = document) => r.querySelector(s);
const TABS = ['floor', 'runs', 'hive', 'caps', 'cost'];
const CC_TABS = ['activity', 'workers', 'memory', 'tasks', 'graph'];

const state = {
  tab: 'floor', cc: 'activity', runId: null, agent: null,
  runs: [], detail: null, hive: null, caps: null, overview: null, floor: null,
  log: null, logTab: 'stderr', sent: '', sending: false,
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
const COLS = 24, ROWS = 16;
const FW = COLS * TILE, FH = ROWS * TILE;

const ATLAS = { img: null, frames: null, ready: false };

async function loadAtlas() {
  try {
    const idx = await api('/assets/atlas.json');
    const img = new Image();
    img.src = idx.image;
    await img.decode();
    ATLAS.img = img; ATLAS.frames = idx.frames; ATLAS.ready = true;
    paintCanvas();          // the first tick may have run before this resolved
  } catch {
    ATLAS.ready = false;      // fall back to the flat vector floor
  }
}

// Rooms in tile coordinates. `floor` and `props` decide what the room looks
// like; `roles` decides who works there.
const ZONES = [
  { id: 'planning',  label: 'PLANNING OFFICE', x: 1,  y: 1,  w: 7, h: 5,
    floor: 'floor_wood',  props: ['desk_pc', 'desk_pc', 'whiteboard'], roles: ['pi', 'planner'] },
  { id: 'seq',       label: 'SEQUENCING BAY',  x: 9,  y: 1,  w: 7, h: 5,
    floor: 'floor_tile',  props: ['sequencer', 'sequencer', 'bench'],  roles: ['curator'] },
  { id: 'qc',        label: 'QC BENCH',        x: 17, y: 1,  w: 6, h: 5,
    floor: 'floor_tile',  props: ['microscope', 'centrifuge'],         roles: ['validator'] },
  { id: 'compute',   label: 'COMPUTE CLUSTER', x: 1,  y: 7,  w: 7, h: 5,
    floor: 'floor_grate', props: ['server_rack', 'server_rack', 'server_rack', 'server_rack'],
    roles: ['runner', 'orchestrator'] },
  { id: 'structure', label: 'STRUCTURE LAB',   x: 9,  y: 7,  w: 7, h: 5,
    floor: 'floor_lino',  props: ['fume_hood', 'flask_rack', 'microscope'], roles: ['analyst'] },
  { id: 'writeup',   label: 'WRITE-UP DESK',   x: 17, y: 7,  w: 6, h: 5,
    floor: 'floor_wood',  props: ['desk_pc', 'whiteboard'],            roles: ['scribe'] },
  { id: 'reception', label: 'RECEPTION',       x: 1,  y: 13, w: 22, h: 3,
    floor: 'floor_lino',  props: ['door', 'plant', 'chair', 'plant'],  roles: ['operator'] },
];

// Which sprite plays which role. Anyone unrecognised still gets a body.
const ROLE_SPRITE = {
  pi: 'char_senior', planner: 'char_tablet', curator: 'char_goggles',
  validator: 'char_pipette', analyst: 'char_curly', scribe: 'char_hijab',
  runner: 'char_hoodie', orchestrator: 'char_hoodie', operator: 'char_casual',
};
const SPRITE_POOL = ['char_hoodie', 'char_casual', 'char_curly', 'char_tablet'];
const spriteFor = a => ROLE_SPRITE[a.role] || SPRITE_POOL[hue(a.name) % SPRITE_POOL.length];

function slots(zone) {
  const n = zone.id === 'reception' ? 6 : 3;
  const step = (zone.w * TILE) / n;
  // Stand one row up from the back of the room: the last row is left free for
  // the name plate, which otherwise falls off the bottom of the canvas.
  const y = (zone.y + zone.h - 2) * TILE;
  return Array.from({ length: n }, (_, i) =>
    ({ x: zone.x * TILE + step * (i + 0.5), y }));
}

function seat(agents) {
  const taken = {}, placed = {};
  ZONES.forEach(z => (taken[z.id] = 0));
  const zoneFor = role => ZONES.find(z => z.roles.includes(role)) ||
                          ZONES.find(z => z.id === 'reception');
  agents.forEach(a => {
    const z = zoneFor(a.role);
    const free = slots(z);
    placed[a.name] = { ...free[Math.min(taken[z.id]++, free.length - 1)],
                       zone: z.id, agent: a };
  });
  return placed;
}

function drawSprite(ctx, name, cx, bottom, scale = 1) {
  const f = ATLAS.frames[name];
  if (!f) return;
  const w = f.w * scale, h = f.h * scale;
  ctx.drawImage(ATLAS.img, f.x, f.y, f.w, f.h,
                Math.round(cx - w / 2), Math.round(bottom - h), w, h);
}

function fillTiles(ctx, name, x, y, w, h) {
  const f = ATLAS.frames[name] || ATLAS.frames.floor_lino;
  if (!f) return;
  for (let ty = 0; ty < h; ty++) {
    for (let tx = 0; tx < w; tx++) {
      ctx.drawImage(ATLAS.img, f.x, f.y, f.w, f.h,
                    (x + tx) * TILE, (y + ty) * TILE, TILE, TILE);
    }
  }
}

function paintCanvas() {
  const cv = $('#floorcanvas');
  if (!cv || !ATLAS.ready) return;
  const ctx = cv.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, FW, FH);

  const agents = state.floor?.agents || [];
  const pos = seat(agents);
  const staffed = new Set(Object.values(pos).map(p => p.zone));

  for (const z of ZONES) {
    ctx.globalAlpha = staffed.has(z.id) ? 1 : 0.55;
    fillTiles(ctx, 'wall_skirting', z.x, z.y, z.w, 1);          // back wall
    fillTiles(ctx, z.floor, z.x, z.y + 1, z.w, z.h - 1);        // floor

    // Equipment stands against the back wall.
    const props = z.props || [];
    const step = (z.w * TILE) / (props.length + 1);
    props.forEach((prop, i) => {
      drawSprite(ctx, prop, z.x * TILE + step * (i + 1), (z.y + 2) * TILE);
    });
    ctx.globalAlpha = 1;
  }

  // Painter's algorithm: lower on screen draws later, so nearer people overlap.
  agents.slice().sort((a, b) => pos[a.name].y - pos[b.name].y).forEach(a => {
    const p = pos[a.name];
    drawSprite(ctx, spriteFor(a), p.x, p.y + TILE);
  });
}

function floorOverlay() {
  const f = state.floor;
  if (!f || !f.agents.length) return '';
  const pos = seat(f.agents);
  const staffed = new Set(Object.values(pos).map(p => p.zone));

  const labels = ZONES.map(z => `
    <text class="zone-label" x="${z.x * TILE + 6}" y="${z.y * TILE + 13}"
          opacity="${staffed.has(z.id) ? 0.95 : 0.5}">${esc(z.label)}</text>
    ${staffed.has(z.id) ? '' : `<text class="zone-label" x="${(z.x + z.w) * TILE - 6}"
      y="${z.y * TILE + 13}" text-anchor="end" opacity=".45">unstaffed</text>`}`).join('');

  const now = Date.now() / 1000;
  const wires = (f.traffic || []).filter(t => now - t.ts < 30 && pos[t.from] && pos[t.to])
    .slice(-6).map(t => {
      const a = pos[t.from], b = pos[t.to];
      const ax = a.x, ay = a.y + TILE - 40, bx = b.x, by = b.y + TILE - 40;
      const d = `M${ax},${ay} Q${(ax + bx) / 2},${Math.min(ay, by) - 40} ${bx},${by}`;
      return `<path class="wire" d="${d}"/>
        <circle r="3" fill="var(--accent)">
          <animateMotion dur="1.6s" repeatCount="indefinite" path="${d}"/>
        </circle>`;
    }).join('');

  // The sprite's feet are at p.y + TILE and it is CHAR_H tall, so the chip
  // hangs just above its head and the plate just below its feet.
  const CHAR_H = 34;
  const people = f.agents.map(a => {
    const p = pos[a.name], c = STATUS_COLOR[a.status] || 'var(--skip)';
    const on = state.agent === a.name;
    const head = p.y + TILE - CHAR_H;
    const feet = p.y + TILE;
    return `<g class="station clickable" data-agent="${esc(a.name)}">
      ${on ? `<rect x="${p.x - 20}" y="${head - 3}" width="40" height="${CHAR_H + 6}"
               rx="5" fill="none" stroke="${c}" stroke-width="1.5" opacity=".9"/>` : ''}
      <rect x="${p.x - 26}" y="${head - 16}" width="52" height="13" rx="3"
            fill="var(--panel)" stroke="${c}" stroke-width="1" opacity=".95"/>
      <text x="${p.x}" y="${head - 6}" text-anchor="middle" font-size="8"
            font-family="var(--mono)" fill="${c}">${esc(a.status)}</text>
      <rect x="${p.x - 30}" y="${feet + 1}" width="60" height="19" rx="3"
            fill="var(--panel)" opacity=".85"/>
      <text class="avatar-name" x="${p.x}" y="${feet + 9}" text-anchor="middle"
            >${esc(a.name)}</text>
      <text class="avatar-role" x="${p.x}" y="${feet + 17}" text-anchor="middle"
            >${esc(a.role)}${a.inbox_pending ? ` · ${a.inbox_pending}` : ''}</text>
    </g>`;
  }).join('');

  return labels + wires + people;
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
    <svg class="flooroverlay" viewBox="0 0 ${FW} ${FH}" preserveAspectRatio="none">${floorOverlay()}</svg>
  </div>`;
}

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
  return { activity: ccActivity, workers: ccWorkers, memory: ccMemory,
           tasks: ccTasks, graph: ccGraph }[state.cc]();
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
  const overlay = $('.flooroverlay');
  if (!overlay) $('#stage').innerHTML = renderFloor();
  else overlay.innerHTML = floorOverlay();
  paintCanvas();
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
  const run = e.target.closest('[data-run]');
  if (run) { state.runId = run.dataset.run; state.log = null; return refresh(); }
  const step = e.target.closest('[data-step]');
  if (step) return loadLog(step.dataset.step);
  const lt = e.target.closest('[data-logtab]');
  if (lt && state.log) { state.logTab = lt.dataset.logtab; return loadLog(state.log.step); }
  if (e.target.id === 'qsend') return sendMessage();
});

document.addEventListener('input', async e => {
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

loadAtlas().then(() => refresh(true));
setInterval(() => refresh(), 3000);
