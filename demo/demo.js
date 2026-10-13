(() => {
  const STEP = 3.2;
  const HOLD = 25;
  const WORKING_WINDOW = 25;
  const NAME_KEY = 'bioharness-demo-lab-name';
  const realFetch = window.fetch.bind(window);

  const base = {};
  const local = [];
  let timeline = [];
  let events = [];
  let start = Date.now() / 1000, loop = 0;
  let labName = null;

  const loaded = (async () => {
    const names = ['floor', 'hive', 'conversation', 'overview', 'runs', 'caps',
                   'staff-options', 'briefs', 'notes', 'meta'];
    const got = await Promise.all(names.map(n =>
      realFetch(`data/${n}.json`).then(r => r.json())));
    names.forEach((n, i) => { base[n] = got[i]; });
    timeline = [...base.conversation].sort((a, b) => a.ts - b.ts);
    events = [...base.hive.events].sort((a, b) => a.ts - b.ts);
    try { labName = localStorage.getItem(NAME_KEY); } catch { labName = null; }
    labName = labName || base.meta.lab_name || base.floor.lab_name;
  })();

  const now = () => Date.now() / 1000;

  function cursor() {
    const t = now();
    if (t - start > timeline.length * STEP + HOLD) { start = t; loop += 1; }
    return Math.min(timeline.length, Math.floor((t - start) / STEP) + 1);
  }

  function replayed() {
    const n = cursor();
    return timeline.slice(0, n).map((m, i) => ({ ...m, id: `${m.id}~${loop}`, ts: start + i * STEP }));
  }

  function messages() {
    return [...replayed(), ...local].sort((a, b) => a.ts - b.ts);
  }

  function visibleEvents() {
    const n = cursor();
    if (!n) return [];
    const cutoff = timeline[n - 1].ts;
    const shown = timeline.slice(0, n);
    return events.filter(e => e.ts <= cutoff).map(e => {
      let i = -1;
      while (i + 1 < shown.length && shown[i + 1].ts <= e.ts) i += 1;
      return { ...e, ts: i < 0 ? start - 1 : start + i * STEP };
    });
  }

  function recipients(m, names) {
    return m.recipient === '*' ? names.filter(n => n !== m.sender && n !== 'human') : [m.recipient];
  }

  function floor() {
    const msgs = messages();
    const names = base.floor.agents.map(a => a.name);
    const t = now();
    const agents = base.floor.agents.map(a => {
      const spoke = msgs.filter(m => m.sender === a.name && t - m.ts < WORKING_WINDOW);
      const addressed = msgs.some(m => t - m.ts < 5 && recipients(m, names).includes(a.name));
      const status = a.role === 'operator' ? 'idle'
        : addressed && !spoke.some(m => t - m.ts < 5) ? 'awaiting'
        : spoke.length ? 'working' : 'idle';
      const last = [...msgs].reverse().find(m => m.sender === a.name);
      return { ...a, status, inbox_pending: status === 'awaiting' ? 1 : 0,
               last_event: last ? 'message.routed' : a.last_event,
               last_seen: last ? last.ts : a.last_seen };
    });
    const traffic = msgs.flatMap(m => recipients(m, names).map(to =>
      ({ from: m.sender, to, topic: m.topic, ts: m.ts }))).slice(-25);
    const chatter = msgs.slice(-25).map(({ body, ...c }) => c);
    return { ...base.floor, lab_name: labName, agents, traffic, chatter,
             activity: visibleEvents().reverse().slice(0, 80) };
  }

  function recall(q, limit) {
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) return [];
    return base.notes.filter(n => terms.every(t => n.text.toLowerCase().includes(t))).slice(0, limit);
  }

  const json = (value, status = 200) => new Response(JSON.stringify(value), {
    status, headers: { 'Content-Type': 'application/json' } });

  async function get(url) {
    const path = url.pathname.replace(/^.*?\/?api\//, '');
    const q = url.searchParams;
    if (path === 'floor') return json(floor());
    if (path === 'conversation') return json(messages().slice(-(+q.get('limit') || 200)));
    if (path === 'hive') return json({ ...base.hive, events: visibleEvents().reverse().slice(0, 250) });
    if (path === 'overview') return json(base.overview);
    if (path === 'runs') return json(base.runs);
    if (path === 'caps') return json(base.caps);
    if (path === 'staff/options') return json(base['staff-options']);
    if (path === 'briefs') return json(base.briefs);
    if (path === 'recall') return json(recall(q.get('q') || '', +q.get('limit') || 20));
    const log = path.match(/^runs\/([\w.-]+)\/log$/);
    if (log) {
      const r = await realFetch(`data/logs/${log[1]}/${q.get('step')}.${q.get('stream') || 'stderr'}.json`);
      return r.ok ? json(await r.json()) : json({ exists: false, text: '' });
    }
    const run = path.match(/^runs\/([\w.-]+)$/);
    if (run) {
      const r = await realFetch(`data/runs/${run[1]}.json`);
      return r.ok ? json(await r.json()) : json({ error: `no such run: ${run[1]}` }, 404);
    }
    return json({ error: `no such endpoint: ${path}` }, 404);
  }

  function post(path, payload) {
    if (path === 'lab') {
      const name = String(payload.name || '').trim().replace(/\s+/g, ' ');
      if (!name || name.length > 60) return json({ ok: false, error: 'lab name must be 1-60 characters' }, 400);
      labName = name;
      try { localStorage.setItem(NAME_KEY, name); } catch { }
      return json({ ok: true, name });
    }
    if (path === 'message') {
      const body = payload.body || {};
      const id = `demo-${local.length + 1}`;
      const say = body.order || body.goal || body.question || body.line || body.note || '';
      local.push({ id, sender: 'human', recipient: payload.to, sender_role: 'operator',
                   topic: payload.topic, ts: now(), say, delivered: true, body });
      const who = payload.to === '*'
        ? (base.floor.agents.find(a => a.role === 'pi') || base.floor.agents[0]).name
        : payload.to;
      setTimeout(() => local.push({
        id: `${id}-reply`, sender: who, recipient: 'human', topic: 'peer.reply', ts: now(),
        sender_role: (base.floor.agents.find(a => a.name === who) || {}).role || '',
        say: 'This lab is a recording replayed in your browser, so I cannot act on that. '
           + 'Install Bioinfo Harness and run `bioharness ui --attend` to work with a live lab.',
        delivered: true, body: {} }), 2500);
      return json({ ok: true, id, to: payload.to, topic: payload.topic, thread: id });
    }
    return json({ ok: false, error: 'Hiring and dismissing are disabled in this demo.' }, 400);
  }

  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (!/\/api\//.test(url.pathname)) return realFetch(input, options);
    await loaded;
    if ((options.method || 'GET').toUpperCase() === 'POST') {
      let payload = {};
      try { payload = JSON.parse(options.body || '{}'); } catch { }
      return post(url.pathname.replace(/^.*?\/?api\//, ''), payload);
    }
    return get(url);
  };

  document.addEventListener('click', e => {
    const a = e.target.closest('a[href^="api/brief/"]');
    if (!a) return;
    e.preventDefault();
    const thread = decodeURIComponent(a.getAttribute('href').slice('api/brief/'.length));
    window.open(`data/briefs/brief-${thread}.pdf`, '_blank', 'noopener');
  }, true);

  document.addEventListener('DOMContentLoaded', () => {
    const bar = document.createElement('div');
    bar.className = 'demo-banner';
    bar.setAttribute('role', 'note');
    bar.innerHTML = `<span><b>Live demo</b> · a recorded lab session, replayed in your browser.</span>
      <a href="https://github.com/DadaBSDK/bioinfo-harness" target="_blank" rel="noopener">GitHub</a>
      <a href="https://github.com/DadaBSDK/bioinfo-harness#install" target="_blank" rel="noopener">Install</a>
      <button type="button" aria-label="Dismiss">×</button>`;
    bar.querySelector('button').addEventListener('click', () => bar.remove());
    document.body.append(bar);
  });
})();
