// 3D adaptation of the distance-timed message arcs in Munder Difflin's
// MessageEnvelope.ts (MIT, Chaitanya Giri). See vendor/MUNDER-LICENSE.txt.
export function messagePose(start, end, elapsed) {
  const duration = Math.min(2, Math.max(.8, Math.hypot(end.x-start.x,end.z-start.z)/7.2));
  const t = Math.min(1, Math.max(0, elapsed/duration));
  const e = t < .5 ? 2*t*t : 1-(-2*t+2)**2/2;
  const burst = Math.max(0, (elapsed-duration)/.34);
  return {x:start.x+(end.x-start.x)*e, z:start.z+(end.z-start.z)*e,
    y:2.2+Math.sin(Math.PI*e)*2.2, burst,
    scale:t<1 ? Math.min(1,elapsed/.14) : 1+burst*3,
    opacity:t<1 ? 1 : Math.max(0,1-burst), done:burst>=1};
}

// New events from the last 15 s, deduplicated across refreshes; the seen-set is capped at 1000.
export function freshTraffic(traffic, seen, now) {
  const fresh=[];
  for(const event of traffic || []) {
    const key=JSON.stringify([event.from,event.to,event.ts,event.topic]);
    if(seen.has(key)) continue;
    seen.add(key);
    if(Number.isFinite(event.ts) && now-event.ts>=0 && now-event.ts<15 && event.from!==event.to) fresh.push(event);
  }
  while(seen.size>1000) seen.delete(seen.values().next().value);
  return fresh;
}
