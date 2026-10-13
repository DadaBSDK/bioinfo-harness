import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../bioharness/ui/app.js', import.meta.url), 'utf8');
function harness(fetch) {
  const fields = { '#qmsg': {value: 'test draft'}, '#qto': {value: 'pi'}, '#qtopic': {value: 'goal'} };
  const context = vm.createContext({fetch, AbortSignal, performance: {now: () => 0},
    document: {querySelector: s => fields[s] || null, addEventListener() {}}});
  vm.runInContext(source.slice(0, source.indexOf('state.paused = window.matchMedia')) +
    '\nrender = () => {}; refresh = async () => {}; globalThis.testState = state;', context);
  return {context, fields};
}
test('HTTP errors reject with the server reason', async () => {
  const {context} = harness(async () => ({ok:false, status:403, json:async () => ({error:'Read only'})}));
  await assert.rejects(vm.runInContext("api('/api/message', {method:'POST'})", context), /Read only/);
});
test('failed message preserves the draft and releases the send button', async () => {
  const {context, fields} = harness(async () => { throw new Error('offline'); });
  await vm.runInContext('sendMessage()', context);
  assert.equal(context.testState.sending, false);
  assert.match(context.testState.sent, /offline/);
  assert.equal(fields['#qmsg'].value, 'test draft');
});
test('failed dismissal preserves selection and the actor', async () => {
  const {context} = harness(async () => { throw new Error('offline'); });
  await vm.runInContext("state.agent = 'pi'; WORLD.actors.pi = {}; fireAgent('pi')", context);
  assert.equal(context.testState.agent, 'pi');
  assert.equal(vm.runInContext("'pi' in WORLD.actors", context), true);
});
test('3D geometry is finite for every facing and walking pose', () => {
  const context = vm.createContext({window:{}});
  vm.runInContext(readFileSync(new URL('../bioharness/ui/voxel.js', import.meta.url),'utf8'),context);
  let points = 0;
  const point = (x,y) => { assert.ok(Number.isFinite(x) && Number.isFinite(y)); points++; };
  const ctx = {save(){},restore(){},beginPath(){},closePath(){},fill(){},moveTo:point,lineTo:point};
  for (const angle of [-Math.PI,-1,0,1,Math.PI]) {
    for (const phase of [0,Math.PI/2,Math.PI]) {
      context.window.VoxelCharacter.draw(ctx,{name:'Iris',role:'curator',x:80,y:150,angle,phase,moving:true});
    }
  }
  assert.ok(points > 1000);
});

test('corridor-to-corridor routes use a connector instead of crossing rooms', () => {
  const {context} = harness();
  const route = vm.runInContext('buildRoute(186, LANE_TOP, 962, LANE_BOTTOM)',context);
  assert.ok(route.some(p => p.x === 380 || p.x === 768));
  for(let i=1;i<route.length;i++) assert.ok(route[i-1].x===route[i].x || route[i-1].y===route[i].y);
});

test('walking clamps to the destination even with a large time step', () => {
  const {context} = harness();
  vm.runInContext("WORLD.actors.test = {x:0,y:0,path:[{x:3,y:0}],wait:0}; stepWorld(.5)",context);
  assert.equal(vm.runInContext('WORLD.actors.test.x',context),3);
  assert.equal(vm.runInContext('WORLD.actors.test.path.length',context),0);
  assert.equal(vm.runInContext('WORLD.actors.test.moving',context),false);
});

test('more than three researchers in a room do not share a station', () => {
  const {context} = harness();
  const xs=vm.runInContext("Object.values(seat(Array.from({length:8},(_,i)=>({name:'a'+i,role:'analyst'})))).map(a=>a.x)",context);
  assert.equal(new Set(xs).size,8);
});

test('changing provider renders that provider’s models only', () => {
  const {context}=harness();
  vm.runInContext("state.staffOptions={roles:['pi'],autonomy:['bounded'],providers:[{id:'builtin',label:'Builtin',available:true,models:[]},{id:'gemini',label:'Gemini',available:true,models:['gemini-test']} ]}; state.hireForm.provider='gemini'; state.hireForm.model='gemini-test';",context);
  assert.match(vm.runInContext('ccStaff()',context),/gemini-test/);
  vm.runInContext("state.hireForm.provider='builtin'",context);
  assert.doesNotMatch(vm.runInContext('ccStaff()',context),/gemini-test/);
});

test('message success does not erase a newer draft typed during the request', async () => {
  let resolve;
  const {context,fields}=harness(()=>new Promise(r=>{resolve=r;}));
  const request=vm.runInContext('sendMessage()',context);
  fields['#qmsg'].value='new unsent draft';context.testState.draft='new unsent draft';
  resolve({ok:true,json:async()=>({ok:true,id:'msg',to:'pi',topic:'goal'})});
  await request;
  assert.equal(fields['#qmsg'].value,'new unsent draft');
  assert.equal(context.testState.draft,'new unsent draft');
});

test('old log responses cannot overwrite a newly selected log',async()=>{
  const requests=[];
  const {context}=harness(()=>new Promise(resolve=>requests.push(resolve)));
  vm.runInContext("state.runId='run-a'",context);
  const first=vm.runInContext("loadLog('first')",context);
  const second=vm.runInContext("loadLog('second')",context);
  requests[1]({ok:true,json:async()=>({exists:true,text:'second log'})});await second;
  requests[0]({ok:true,json:async()=>({exists:true,text:'first log'})});await first;
  assert.equal(context.testState.log.step,'second');
  assert.equal(context.testState.log.text,'second log');
});

test('removing a selected agent clears stale selection and its actor',()=>{
  const {context}=harness();
  vm.runInContext("state.floor={agents:[]};state.agent='gone';WORLD.actors.gone={};syncWorld()",context);
  assert.equal(context.testState.agent,null);
  assert.equal(vm.runInContext('Object.keys(WORLD.actors).length',context),0);
});

test('draft and topic survive reconstruction of the lab scaffold',()=>{
  const {context}=harness();
  context.testState.draft='keep <my draft>';context.testState.topic='peer.ask';
  const html=vm.runInContext('floorScaffold()',context);
  assert.match(html,/keep &lt;my draft&gt;/);
  assert.match(html,/<option selected>peer.ask/);
});

const effects = await import('data:text/javascript;base64,' + Buffer.from(readFileSync(new URL('../bioharness/ui/scene-logic.js', import.meta.url),'utf8')).toString('base64'));
test('message arcs travel, arrive, and finish for short and long distances', () => {
  for(const distance of [0,1,20,50]) {
    const from={x:0,z:0},to={x:distance,z:distance};
    const start=effects.messagePose(from,to,0),end=effects.messagePose(from,to,3);
    assert.equal(start.x,0);assert.equal(end.x,distance);assert.equal(end.z,distance);
    assert.equal(end.done,true);assert.equal(end.opacity,0);
    for(let t=0;t<3;t+=.05) {
      const pose=effects.messagePose(from,to,t);
      assert.ok(pose.y>=2.2 && Number.isFinite(pose.y));
      assert.ok(pose.opacity>=0 && pose.opacity<=1);
    }
  }
});
test('traffic effects reject history, future events, repeats and self messages', () => {
  const seen=new Set(),live={from:'ada',to:'rex',ts:99,topic:'request'};
  const events=[live,{...live,ts:60},{...live,ts:101},{...live,to:'ada'},{...live,ts:NaN}];
  assert.deepEqual(effects.freshTraffic(events,seen,100),[live]);
  assert.deepEqual(effects.freshTraffic(events,seen,101),[]);
  effects.freshTraffic(Array.from({length:2000},(_,i)=>({...live,ts:i})),seen,2001);
  assert.ok(seen.size<=1000);
});
