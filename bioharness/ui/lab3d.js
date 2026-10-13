import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { messagePose, freshTraffic } from './scene-logic.js';

// World positions reuse the 2D floor's routing map, at 32 pixels per metre.
const UNIT = 32, WIDTH = 1152, DEPTH = 672;
const world = (x, z) => [(x - WIDTH / 2) / UNIT, (z - DEPTH / 2) / UNIT];
const hash = name => Array.from(name).reduce((h,c) => (h*31+c.charCodeAt(0))>>>0, 7);
const statusColors = { working: '#a6cf73', idle: '#aebaaa', awaiting: '#f3c877', blocked: '#ee8d7b' };
let assets;
let assetRequest;
async function loadAssets() {
  if (assets) return assets;
  if (!assetRequest) assetRequest = fetch('/assets/voxel/models.json', {signal: AbortSignal.timeout(15000)})
    .then(r => { if (!r.ok) throw new Error('Voxel assets could not be loaded'); return r.json(); })
    .then(data => (assets = data)).finally(() => { assetRequest = null; });
  return assetRequest;
}
let active = null;
const HORIZON = '#e9efec', ZENITH = '#9fbfd3';
const MOVE_KEYS = new Set(['w','a','s','d','arrowup','arrowdown','arrowleft','arrowright','q','e']);
const HOME = { position: [23,24,33], target: [0,0,1.4] };
const ease = t => t<.5 ? 4*t*t*t : 1-Math.pow(-2*t+2,3)/2;

class LabScene {
  constructor(host, bridge) {
    this.host = host; this.bridge = bridge; this.disposed = false;
    this.people = new Map(); this.labels = []; this.walls = []; this.materials = new Map();
    this.textures = []; this.staticBoxes = new Map(); this.movers = [];
    this.packets = []; this.trafficSeen = new Set(); this.tour = false; this.tourIndex = 0;
    this.leds = []; this.motion = []; this.keys = new Set(); this.tween = null; this.follow = null;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(HORIZON);
    this.scene.fog = new THREE.Fog(HORIZON, 70, 190);
    this.camera = new THREE.PerspectiveCamera(36, 1.6, .1, 400);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.domElement.setAttribute('aria-label', 'Interactive 3D voxel laboratory. Drag to orbit, scroll to zoom, W A S D to walk, F for fullscreen.');
    this.renderer.domElement.tabIndex = 0;
    host.prepend(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true; this.controls.dampingFactor = .1;
    this.controls.minDistance = 3.5; this.controls.maxDistance = 95;
    this.controls.maxPolarAngle = Math.PI*.485; this.controls.minPolarAngle = .02;
    this.controls.zoomToCursor = true;
    // Any manual drag cancels a camera flight, so the user is never fought.
    this.controls.addEventListener('start', () => { this.tween = null; this.follow = null; this.stopTour(); });
    this.controls.screenSpacePanning = true;
    this.camera.position.set(...HOME.position); this.controls.target.set(...HOME.target);
    this.scene.add(new THREE.HemisphereLight('#f4f8ff','#6d7f78',1.75));
    const sun = new THREE.DirectionalLight('#ffeed8',2.6);
    sun.position.set(-18,35,20); sun.castShadow = true;
    Object.assign(sun.shadow.camera,{left:-28,right:28,top:25,bottom:-25,near:1,far:90});
    sun.shadow.mapSize.set(2048,2048); sun.shadow.bias = -.0005;
    sun.shadow.normalBias = .035;
    this.scene.add(sun);
    this.sky();
    this.geometry = new THREE.BoxGeometry(1,1,1);
    this.ray = new THREE.Raycaster(); this.pointer = new THREE.Vector2();
    this.labelLayer = host.querySelector('.scene-labels');
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.handlers = [];
    this.pointers = new Set(); this.gesture = false;
    this.on('pointerdown', e => {
      if (!this.pointers.size) { this.down = [e.clientX,e.clientY]; this.gesture = false; }
      this.pointers.add(e.pointerId);
      if (this.pointers.size > 1) this.gesture = true;
    });
    this.on('pointermove', e => {
      if (this.down && this.pointers.size && Math.hypot(e.clientX-this.down[0],e.clientY-this.down[1])>5) this.gesture=true;
    });
    this.on('pointerup', e => {
      if (this.pointers.has(e.pointerId) && this.pointers.size === 1 && !this.gesture) this.pick(e);
      this.pointers.delete(e.pointerId); if (!this.pointers.size) this.down = null;
    });
    this.on('pointercancel', e => {this.pointers.delete(e.pointerId);this.gesture=true;this.down=null;});
    this.on('keydown', e => {
      const key = e.key.toLowerCase();
      if (MOVE_KEYS.has(key)) { this.keys.add(key); this.stopTour(); this.follow = null; this.tween = null; e.preventDefault(); }
      if (key === 'f') { this.fullscreen(); e.preventDefault(); }
      if (e.key === 'Home') { this.reset(); e.preventDefault(); }
      if (e.key === '+' || e.key === '=') { this.zoom(1.2); e.preventDefault(); }
      if (e.key === '-') { this.zoom(1/1.2); e.preventDefault(); }
      if (e.key === 'Escape') { this.stopTour(); this.follow = null; this.bridge.select(null); }
    });
    this.on('keyup', e => this.keys.delete(e.key.toLowerCase()));
    this.on('blur', () => this.keys.clear());
    this.on('webglcontextlost', e => {
      e.preventDefault(); this.bridge.fail('3D graphics were interrupted. Switch to 3D again to retry.');
    });
    this.resize();
  }
  on(type, fn) { this.renderer.domElement.addEventListener(type,fn); this.handlers.push([type,fn]); }
  material(key) {
    if (this.materials.has(key)) return this.materials.get(key);
    let mat;
    if (assets.materials[key]) {
      const texture = new THREE.TextureLoader().load(`/assets/voxel/${key}.png`, undefined, undefined, () => {
        if (this.disposed) return;
        mat.map = null; mat.color.set(assets.materials[key]); mat.needsUpdate = true;
        this.bridge.notice?.('Some textures could not load. Solid voxel materials are being used.');
      });
      texture.magFilter = THREE.NearestFilter; texture.minFilter = THREE.NearestFilter;
      texture.colorSpace = THREE.SRGBColorSpace;
      this.textures.push(texture);
      mat = new THREE.MeshLambertMaterial({map:texture});
      if (key === 'screen') { mat.emissive.set('#4cb9af'); mat.emissiveIntensity = .55; this.glow = mat; }
    } else mat = new THREE.MeshLambertMaterial({color:key});
    // Ceiling strips and doorway runners read as light sources, not paint.
    if (key === '#fff0c2' || key === '#e4d5a4') { mat.emissive.set(key); mat.emissiveIntensity = .9; }
    this.materials.set(key,mat);
    return mat;
  }
  box(parent, x,y,z,w,h,d,material) {
    const mesh = new THREE.Mesh(this.geometry,this.material(material));
    mesh.position.set(x,y+h/2,z); mesh.scale.set(w,h,d);
    mesh.castShadow = true; mesh.receiveShadow = true; parent.add(mesh);
    return mesh;
  }
  staticBox(x,y,z,w,h,d,material) {
    if (!this.staticBoxes.has(material)) this.staticBoxes.set(material,[]);
    this.staticBoxes.get(material).push([x,y,z,w,h,d]);
  }
  model(name,x,y,z,scale=1) {
    const boxes = assets.models[name];
    if (!boxes) throw new Error(`Missing voxel model: ${name}`);
    for (const [bx,by,bz,w,h,d,mat] of boxes) {
      const args = [x+bx*scale,y+by*scale,z+bz*scale,w*scale,h*scale,d*scale];
      if (['screen','amber'].includes(mat) && w*h < .01) this.leds.push([...args, mat]);
      else this.staticBox(...args,mat);
    }
  }
  sky() {
    // A gradient sky dome, so the floating lab sits in open air rather than on a flat fill.
    const dome = new THREE.Mesh(new THREE.SphereGeometry(300,32,16), new THREE.ShaderMaterial({
      side: THREE.BackSide, depthWrite: false, fog: false,
      uniforms: { top: {value: new THREE.Color(ZENITH)}, bottom: {value: new THREE.Color(HORIZON)} },
      vertexShader: 'varying vec3 v; void main(){ v = normalize(position); gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.); }',
      fragmentShader: 'uniform vec3 top; uniform vec3 bottom; varying vec3 v; void main(){ gl_FragColor = vec4(mix(bottom, top, smoothstep(-.05,.55,v.y)),1.); }',
    }));
    dome.renderOrder = -1; this.scene.add(dome);
  }
  ambience(rooms) {
    // Warm pools of light in each room, without the cost of extra shadow maps.
    for (const room of rooms) {
      const [cx,cz] = world(room.x+room.w/2, room.y+room.h/2);
      const lamp = new THREE.PointLight(room.id==='compute' ? '#9fd6ff' : '#ffd9a0', 9, 11, 1.6);
      lamp.position.set(cx, 3, cz); this.scene.add(lamp);
    }
    // Indicator LEDs blink on their own rhythm.
    if (this.leds.length) {
      const mesh = new THREE.InstancedMesh(this.geometry, new THREE.MeshBasicMaterial({color:'#ffffff'}), this.leds.length);
      const dummy = new THREE.Object3D();
      this.ledColors = this.leds.map(([x,y,z,w,h,d,mat],i) => {
        dummy.position.set(x,y+h/2,z); dummy.scale.set(w*1.05,h*1.05,d*1.3); dummy.updateMatrix(); mesh.setMatrixAt(i,dummy.matrix);
        const color = new THREE.Color(mat==='amber' ? '#ffc35a' : (i%5===0 ? '#ff7a5c' : '#5dffd2'));
        mesh.setColorAt(i,color); return {color, rate: .6+(i*7%11)/6, offset: i*1.37};
      });
      this.ledMesh = mesh; this.scene.add(mesh);
    }
    // Dust motes drift through the light.
    const count = 420, positions = new Float32Array(count*3);
    for (let i=0; i<count; i++) {
      positions[i*3] = (Math.random()-.5)*36; positions[i*3+1] = .3+Math.random()*3.4; positions[i*3+2] = (Math.random()-.5)*21;
    }
    const dustGeometry = new THREE.BufferGeometry();
    dustGeometry.setAttribute('position', new THREE.BufferAttribute(positions,3));
    this.dust = new THREE.Points(dustGeometry, new THREE.PointsMaterial({
      color:'#fff6dc', size:.06, transparent:true, opacity:.55, depthWrite:false }));
    this.dustBase = positions.slice(); this.scene.add(this.dust);
    // Voxel clouds drift below the floating foundation.
    const cloudMat = new THREE.MeshLambertMaterial({color:'#ffffff', transparent:true, opacity:.7});
    this.clouds = [];
    for (let i=0; i<14; i++) {
      const cloud = new THREE.Group(), r = n => ((hash('c'+i+n)%1000)/1000);
      for (let j=0; j<4+i%3; j++) {
        const m = new THREE.Mesh(this.geometry, cloudMat);
        m.scale.set(2+r(j)*4, .8+r(j+9)*1.1, 1.6+r(j+3)*3); m.position.set(j*1.6-3, r(j+5)*.8, (r(j+7)-.5)*2);
        cloud.add(m);
      }
      const angle = i/14*Math.PI*2;
      cloud.position.set(Math.cos(angle)*(18+r(1)*34), -9-r(2)*10, Math.sin(angle)*(14+r(4)*28));
      cloud.userData.speed = .15+r(3)*.35; this.clouds.push(cloud); this.scene.add(cloud);
    }
  }
  label(text,x,y,z,className='room-tag',select=null) {
    const element = document.createElement(select ? 'button' : 'div');
    element.className = className; element.textContent = text;
    if (select) { element.type='button'; element.addEventListener('click', () => this.bridge.select(select)); }
    this.labelLayer.append(element);
    const label = {element,kind:className,agent:select,point:new THREE.Vector3(x,y,z)};
    this.labels.push(label); return label;
  }
  build(rooms) {
    this.rooms = rooms;
    const destinations = this.host.querySelector('.scene-destinations');
    for (const room of rooms) {
      const button = document.createElement('button');
      button.textContent = room.label.replace(' OFFICE','').replace(' BAY','');
      button.type = 'button'; button.title = `Explore ${room.label.toLowerCase()}`;
      button.addEventListener('click', () => { this.stopTour(); this.visit(room); });
      destinations?.append(button);
    }
    // A floating foundation with visible layered earth and an inset perimeter.
    this.staticBox(0,-1.15,0,37.3,.52,22.1,'dark');
    this.staticBox(0,-.64,0,37.6,.4,22.4,'metal');
    this.staticBox(0,-.25,0,37.6,.25,22.4,'tile');
    for (let x=-18; x<19; x++) for (let z=-10; z<11; z++)
      this.staticBox(x,.001,z,.98,.04,.98,((x+z)%2===0) ? '#8c9c91' : '#829389');
    for (const room of rooms) {
      const [cx,cz] = world(room.x+room.w/2, room.y+room.h/2);
      const w=room.w/UNIT, d=room.h/UNIT;
      const back=cz-d/2, front=cz+d/2;
      const floor=room.tile==='floor_wood' ? 'wood' : room.id==='compute' ? 'metal' : 'tile';
      this.staticBox(cx,.04,cz,w,.1,d,floor);
      // Tiled surfaces have individually inset squares; wood is laid as long planks.
      const step=room.tile==='floor_wood' ? 1.05 : .75;
      for (let x=-w/2+.05; x<w/2-.05; x+=step) for (let z=-d/2+.05; z<d/2-.05; z+=.75) {
        const tw=Math.min(step-.035,w/2-x-.03), td=Math.min(.715,d/2-z-.03);
        if(tw>0&&td>0) this.staticBox(cx+x+tw/2,.145,cz+z+td/2,tw,.035,td,floor);
      }
      const wall = new THREE.Group(); this.scene.add(wall); this.walls.push(wall);
      if (room.id !== 'lobby') {
        this.box(wall,cx,.15,back-.12,w+.3,1.55,.24,'plaster');
        this.box(wall,cx,1.7,back-.12,w+.35,.1,.29,'white');
        this.box(wall,cx,.57,back+.012,w,.1,.025,room.accent);
        // Windows and strip lighting on the back wall.
        for (const fx of [-.3,.3]) {
          this.box(wall,cx+w*fx,.85,back+.02,1.55,.63,.04,'metal');
          this.box(wall,cx+w*fx,.91,back+.05,1.43,.51,.025,'blue');
          this.box(wall,cx+w*fx,1.59,back+.12,1.65,.06,.12,'#fff0c2');
        }
      }
      for (const side of [-1,1]) this.box(wall,cx+side*(w/2+.1),.15,cz,.2,.66,d,'plaster');
      // Low front walls keep the diorama readable. Door gaps match routing exactly.
      const door=(room.doorX-WIDTH/2)/UNIT, gap=48/UNIT;
      const fy=room.doorSide==='top'?back:front;
      const left=cx-w/2, right=cx+w/2;
      this.box(wall,(left+door-gap/2)/2,.15,fy,(door-gap/2)-left,.38,.2,'plaster');
      this.box(wall,(door+gap/2+right)/2,.15,fy,right-(door+gap/2),.38,.2,'plaster');
      this.staticBox(door,.16,fy,gap,.035,.5,room.accent);
      for (const dx of [-gap/2,gap/2]) this.box(wall,door+dx,.15,fy,.13,.75,.22,'metal');
      this.label(room.label,cx,2.1,back-.2);
      if(room.counter) {
        for(let x=cx-w/2+.7;x<cx+w/2-.4;x+=1.02) this.model('bench',x,.18,back+1.72);
      }
      (room.wall||[]).forEach((name,i,list) => {
        const x=cx-w/2+w*(i+1)/(list.length+1);
        const tabletop = room.counter && ['pcr','gel_dock','centrifuge','microscope','printer'].includes(name);
        this.model(name,x,tabletop?1.13:.18,back+(tabletop?1.72:.8),name==='plant'?.85:1);
      });
      for (const [name,fx,fy] of [...(room.mid||[]),...(room.floorProps||[])]) {
        this.model(name,cx-w/2+w*fx,.18,cz-d/2+d*fy,room.id==='lobby'?1.05:.85);
      }
      if (['planning','structure','writeup'].includes(room.id)) {
        for(const fx of [-.24,.24]) this.model('chair',cx+w*fx,.18,back+2.65,.8);
      }
    }
    this.ambience(rooms);
    // A voxel DNA installation, hovering above the lobby.
    const lobby = rooms.find(r => r.id === 'lobby');
    if (lobby) {
      const [hx,hz] = world(lobby.x+lobby.w*.78,lobby.y+lobby.h*.5);
      this.helix = new THREE.Group(); this.helix.position.set(hx,.8,hz);
      const cyan = new THREE.MeshBasicMaterial({color:'#55eedc',transparent:true,opacity:.85});
      const gold = new THREE.MeshBasicMaterial({color:'#ffc96c',transparent:true,opacity:.85});
      for(let i=0;i<24;i++) {
        const angle=i*.45, y=i*.13;
        for(const side of [-1,1]) {
          const cube=new THREE.Mesh(this.geometry,side===1?cyan:gold);
          cube.position.set(Math.cos(angle)*.65*side,y,Math.sin(angle)*.65*side);cube.scale.setScalar(.16);this.helix.add(cube);
        }
        if(i%2===0) {
          const rung=new THREE.Mesh(this.geometry,cyan);rung.position.y=y;rung.scale.set(1.3,.035,.035);rung.rotation.y=-angle;this.helix.add(rung);
        }
      }
      this.scene.add(this.helix);
      this.staticBox(hx,.18,hz,1.8,.25,1.8,'dark');
      this.staticBox(hx,.44,hz,1.6,.03,1.6,'screen');
    }
    // Environmental details: lights embedded along the perimeter and paths.
    for(let x=-17;x<=17;x+=3.4) this.staticBox(x,.04,10.83,.65,.035,.1,'#e4d5a4');
    const dummy = new THREE.Object3D();
    for(const [material,boxes] of this.staticBoxes) {
      const mesh=new THREE.InstancedMesh(this.geometry,this.material(material),boxes.length);
      boxes.forEach(([x,y,z,w,h,d],i) => {
        dummy.position.set(x,y+h/2,z);dummy.scale.set(w,h,d);dummy.updateMatrix();mesh.setMatrixAt(i,dummy.matrix);
      });
      mesh.castShadow=true;mesh.receiveShadow=true;mesh.computeBoundingSphere();this.scene.add(mesh);
    }
    this.staticBoxes.clear();
    // Raycast target uses the exact floor bounds. Intersections outside are ignored.
    this.ground = new THREE.Mesh(new THREE.PlaneGeometry(WIDTH/UNIT,DEPTH/UNIT),new THREE.MeshBasicMaterial({visible:false}));
    this.ground.rotation.x=-Math.PI/2;this.ground.position.y=.18;this.scene.add(this.ground);
    this.marker = new THREE.Group();
    for(const side of [-1,1]) {
      this.box(this.marker,side*.47,0,0,.035,.025,1,'#e4e899');
      this.box(this.marker,0,0,side*.47,1,.025,.035,'#e4e899');
    }
    this.scene.add(this.marker);this.marker.visible=false;
    this.ready=true;
    this.host.querySelector('.scene-loading')?.remove();
    // Fly in from high above the lab, unless the user prefers reduced motion.
    if (!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      this.camera.position.set(-30,70,62); this.controls.target.set(0,-2,0); this.controls.update();
      this.fly(HOME.position, HOME.target, 2600);
    }
  }
  character(agent) {
    const seed=hash(agent.name), root=new THREE.Group(), body=new THREE.Group();root.add(body);
    const skin=['#d7a47c','#a87351','#ebc7a2','#805943'][seed%4];
    const hair=['#352f2b','#62412a','#283640','#b28250'][seed%4];
    const color=new THREE.Color().setHSL((seed%360)/360,.35,.45).getStyle();
    const coat=['runner','orchestrator','operator'].includes(agent.role)?color:'white';
    const limb=(x,y,w,h,d,mat) => {
      const pivot=new THREE.Group();pivot.position.set(x,y,0);body.add(pivot);
      this.box(pivot,0,-h,0,w,h,d,mat);return pivot;
    };
    const legs=[limb(-.12,.65,.22,.59,.24,'#485a72'),limb(.12,.65,.22,.59,.24,'#485a72')];
    legs.forEach(p => this.box(p,0,-.65,.035,.23,.13,.31,'dark'));
    this.box(body,0,.66,0,.5,.61,.31,coat);
    const arms=[limb(-.37,1.24,.23,.49,.28,coat),limb(.37,1.24,.23,.49,.28,coat)];
    arms.forEach(p=>this.box(p,0,-.6,0,.23,.15,.28,skin));
    this.box(body,0,1.29,0,.5,.49,.48,skin);
    this.box(body,0,1.7,0,.52,.14,.5,hair);
    this.box(body,0,1.43,-.22,.51,.33,.06,hair);
    for (const x of [-.13,.13]) this.box(body,x,1.51,.245,.07,.065,.02,'dark');
    this.box(body,0,1.37,.247,.13,.025,.02,'#9b6955');
    this.box(body,-.14,1.05,.16,.105,.13,.015,color);
    this.box(body,0,.83,.16,.04,.38,.015,'metal');
    if(['curator','validator'].includes(agent.role)) {
      this.box(body,0,1.49,.265,.48,.13,.04,'screen');
      for(const x of [-.13,.13]) this.box(body,x,1.52,.29,.13,.04,.01,'#d5f1e0');
    }
    root.traverse(obj=>{obj.userData.agent=agent.name;});
    this.scene.add(root);
    const label=this.label(agent.name,0,2,0,'agent-tag',agent.name);
    return {root,body,arms,legs,label,role:agent.role,heading:0};
  }
  update(actors,state,time) {
    if(!this.ready||this.disposed) return;
    const dt=Math.min(.1,(time-(this.last??time))/1000); this.last=time;
    const names=new Set(actors.map(a=>a.agent.name));
    for(const [name,person] of this.people) if(!names.has(name)) {
      this.removePerson(person); this.people.delete(name);
    }
    for(const act of actors) {
      const a=act.agent;
      let person=this.people.get(a.name);
      if(person&&person.role!==a.role) {this.removePerson(person);this.people.delete(a.name);person=null;}
      if(!person) { person=this.character(a);this.people.set(a.name,person); }
      const [x,z]=world(act.x,act.y);
      person.root.position.set(x,.2,z);
      const next=act.path[0];
      // Turn toward the next waypoint smoothly instead of snapping.
      if(next && act.moving) person.heading=Math.atan2(next.x-act.x,next.y-act.y);
      const turn=Math.atan2(Math.sin(person.heading-person.root.rotation.y),Math.cos(person.heading-person.root.rotation.y));
      person.root.rotation.y+=turn*Math.min(1,dt*10);
      const phase=state.paused?0:time/130+hash(a.name)%20;
      const stride=act.moving&&!state.paused?Math.sin(phase)*.65:0;
      person.legs[0].rotation.x=stride;person.legs[1].rotation.x=-stride;
      person.arms[0].rotation.x=-stride;person.arms[1].rotation.x=stride;
      if(a.status==='working'&&!state.paused&&!act.moving) person.arms[1].rotation.x=-.7+Math.sin(phase)*.12;
      person.body.position.y=act.moving&&!state.paused?Math.abs(Math.sin(phase))*.04:0;
      // Idle researchers breathe and shift their weight.
      const breath=!act.moving&&!state.paused?Math.sin(time/700+hash(a.name)):0;
      person.body.scale.y=1+breath*.012; person.body.rotation.z=breath*.015;
      person.label.point.set(x,2.28,z);
      const el=person.label.element;
      const caption = `${a.name} · ${a.status || 'idle'}`;
      if (el.textContent !== caption) el.textContent = caption;
      el.style.setProperty('--agent-status',statusColors[a.status]||statusColors.idle);
      el.classList.toggle('selected',state.agent===a.name);
      el.setAttribute('aria-pressed',String(state.agent===a.name));
      const bubble = act.bubble;
      if (bubble && bubble.until > time) {
        if (!person.speech) person.speech = this.label('',x,2.9,z,'scene-speech');
        const text = bubble.lines.join(' ');
        if (person.speech.element.textContent !== text) person.speech.element.textContent = text;
        person.speech.point.set(x,2.9,z);
      } else if (person.speech) {
        person.speech.element.remove();this.labels=this.labels.filter(l=>l!==person.speech);person.speech=null;
      }
    }
    const selected=this.people.get(state.agent);
    this.marker.visible=!!selected;
    if(selected) {
      this.marker.position.copy(selected.root.position);
      const pulse=state.paused?1:1+Math.sin(time/260)*.08; this.marker.scale.set(pulse,1,pulse);
    }
    if(this.follow&&!this.people.has(this.follow)) this.follow=null;
    this.walls.forEach(w=>w.visible=!state.cutaway);
    this.labelLayer.hidden=!state.labels;
    for(const event of freshTraffic(state.floor?.traffic,this.trafficSeen,Date.now()/1000)) {
      const sender=this.people.get(event.from), recipient=this.people.get(event.to);
      if(!sender || !recipient || state.paused || this.packets.length>=24) continue;
      const material = new THREE.MeshBasicMaterial({color:/result|reply|done/.test(event.topic||'')?'#ffd077':'#55e8d7',transparent:true});
      const mesh = new THREE.Mesh(this.geometry,material); this.scene.add(mesh);
      this.packets.push({mesh,start:sender.root.position.clone(),end:recipient.root.position.clone(),elapsed:0});
    }
    for(const packet of [...this.packets]) {
      if(!state.paused) packet.elapsed+=dt;
      const pose=messagePose(packet.start,packet.end,packet.elapsed);
      packet.mesh.position.set(pose.x,pose.y,pose.z);
      packet.mesh.scale.set(.35*pose.scale,.24*pose.scale,.12*pose.scale);
      packet.mesh.rotation.y+=state.paused?0:dt*2;
      packet.mesh.material.opacity=pose.opacity;
      if(pose.done) {this.scene.remove(packet.mesh);packet.mesh.material.dispose();this.packets.splice(this.packets.indexOf(packet),1);}
    }
    if(this.tour && !state.paused && !this.tween && time>this.nextStop) {
      this.visit(this.rooms[this.tourIndex++ % this.rooms.length]); this.nextStop=time+6500;
    }
    this.animateWorld(time,dt,state.paused);
    this.moveCamera(time,dt);
    this.controls.update();
    this.camera.updateMatrixWorld();
    const width=this.host.clientWidth,height=this.host.clientHeight;
    for(const label of this.labels) {
      const p=label.point.clone().project(this.camera);
      const distance=this.camera.position.distanceTo(label.point);
      const readable = label.kind === 'agent-tag' ? distance < 30 || label.agent === state.agent
        : label.kind === 'room-tag' ? distance > 6 : true;
      const visible=readable&&p.z>=-1&&p.z<=1&&Math.abs(p.x)<1&&Math.abs(p.y)<1;
      // Nearer labels sit on top, and far ones recede with the scene.
      label.element.style.zIndex=String(1000-Math.round(distance*10));
      label.element.style.opacity=label.kind==='room-tag'?String(THREE.MathUtils.clamp(1.4-distance/80,.45,1)):'';
      label.element.style.display=visible?'':'none';
      label.element.style.transform=`translate(${(p.x*.5+.5)*width}px,${(-p.y*.5+.5)*height}px) translate(-50%,-100%)`;
    }
    this.renderer.render(this.scene,this.camera);
  }
  removePerson(person) {
    this.scene.remove(person.root);person.label.element.remove();
    person.speech?.element.remove();
    this.labels=this.labels.filter(l=>l!==person.label&&l!==person.speech);
  }
  pick(event) {
    if(event.button!==0||!this.down||Math.hypot(event.clientX-this.down[0],event.clientY-this.down[1])>5||!this.ready)return;
    const rect=this.renderer.domElement.getBoundingClientRect();
    this.pointer.set((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1);
    this.ray.setFromCamera(this.pointer,this.camera);
    const people=this.ray.intersectObjects([...this.people.values()].map(p=>p.root),true);
    if(people.length) return this.bridge.select(people[0].object.userData.agent);
    const hit=this.ray.intersectObject(this.ground)[0];
    if(hit) this.bridge.walk(hit.point.x*UNIT+WIDTH/2,hit.point.z*UNIT+DEPTH/2);
  }
  animateWorld(time,dt,paused) {
    if(paused) return;
    if(this.helix) this.helix.rotation.y+=dt*.25;
    if(this.glow) this.glow.emissiveIntensity=.5+Math.sin(time/900)*.08+Math.sin(time/97)*.02;
    if(this.ledMesh) {
      const off=new THREE.Color('#1d2a2c');
      this.ledColors.forEach(({color,rate,offset},i) => {
        const on=Math.sin(time/1000*rate*3+offset)>-.3;
        this.ledMesh.setColorAt(i,on?color:off);
      });
      this.ledMesh.instanceColor.needsUpdate=true;
    }
    if(this.dust) {
      const p=this.dust.geometry.attributes.position, base=this.dustBase, t=time/1000;
      for(let i=0;i<p.count;i++) {
        p.array[i*3]=base[i*3]+Math.sin(t*.21+i)*.35;
        p.array[i*3+1]=base[i*3+1]+Math.sin(t*.33+i*1.7)*.25;
        p.array[i*3+2]=base[i*3+2]+Math.cos(t*.17+i*.6)*.35;
      }
      p.needsUpdate=true;
    }
    for(const cloud of this.clouds||[]) {
      cloud.position.x+=cloud.userData.speed*dt;
      if(cloud.position.x>60) cloud.position.x=-60;
    }
  }
  moveCamera(time,dt) {
    if(this.tween) {
      const {from,to,start,duration}=this.tween, k=ease(Math.min(1,(time-(start??(this.tween.start=time)))/duration));
      this.camera.position.lerpVectors(from.position,to.position,k);
      this.controls.target.lerpVectors(from.target,to.target,k);
      if(k>=1) this.tween=null;
      return;
    }
    // WASD walks the camera across the floor, relative to where it looks.
    if(this.keys.size) {
      const forward=new THREE.Vector3().subVectors(this.controls.target,this.camera.position).setY(0).normalize();
      const right=new THREE.Vector3().crossVectors(forward,this.camera.up).normalize();
      const move=new THREE.Vector3(), k=this.keys;
      if(k.has('w')||k.has('arrowup')) move.add(forward);
      if(k.has('s')||k.has('arrowdown')) move.sub(forward);
      if(k.has('d')||k.has('arrowright')) move.add(right);
      if(k.has('a')||k.has('arrowleft')) move.sub(right);
      const speed=Math.max(4,this.camera.position.distanceTo(this.controls.target)*.6)*dt;
      move.normalize().multiplyScalar(speed);
      const target=this.controls.target.clone().add(move);
      target.x=THREE.MathUtils.clamp(target.x,-20,20); target.z=THREE.MathUtils.clamp(target.z,-12,12);
      move.subVectors(target,this.controls.target);
      this.controls.target.add(move); this.camera.position.add(move);
      const spin=(k.has('e')?1:0)-(k.has('q')?1:0);
      if(spin) {
        const offset=this.camera.position.clone().sub(this.controls.target).applyAxisAngle(this.camera.up,-spin*dt*1.4);
        this.camera.position.copy(this.controls.target).add(offset);
      }
    }
    // Follow mode: the camera rides along behind the chosen researcher.
    const person=this.follow&&this.people.get(this.follow);
    if(person) {
      const goal=person.root.position.clone().setY(1.1);
      const step=goal.sub(this.controls.target).multiplyScalar(Math.min(1,dt*4));
      this.controls.target.add(step); this.camera.position.add(step);
    }
  }
  fly(position,target,duration=1100) {
    if(window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      this.camera.position.set(...position);this.controls.target.set(...target);this.tween=null;return;
    }
    this.tween={from:{position:this.camera.position.clone(),target:this.controls.target.clone()},
      to:{position:new THREE.Vector3(...position),target:new THREE.Vector3(...target)},start:null,duration};
  }
  resize() {
    if(this.disposed)return;
    const width=this.host.clientWidth||800,height=this.host.clientHeight||500;
    this.camera.aspect=width/height;
    // Narrow viewports widen the lens so the whole lab still fits on reset.
    this.camera.fov=THREE.MathUtils.clamp(36*1.6/this.camera.aspect,36,62);
    this.camera.updateProjectionMatrix();this.renderer.setSize(width,height,false);
  }
  stopTour() {
    this.tour=false;
    this.host.querySelector('[data-camera="tour"]')?.setAttribute('aria-pressed','false');
  }
  toggleTour() {
    if(this.tour) {this.stopTour();this.tween=null;return;}
    this.tour=true; this.follow=null; this.nextStop=0;
    this.host.querySelector('[data-camera="tour"]')?.setAttribute('aria-pressed','true');
  }
  visit(room) {
    if(!room) return;
    this.follow=null;
    const [x,z]=world(room.x+room.w/2,room.y+room.h/2);
    this.fly([x+4,8,z+11],[x,.6,z],1800);
    const caption=this.host.querySelector('.scene-location');
    if(caption) caption.textContent=room.label.toLowerCase();
  }
  reset() {
    this.stopTour(); this.follow=null;
    this.host.querySelector('.scene-location').textContent='Explore your lab';
    this.fly(HOME.position,HOME.target);
  }
  top() {
    this.stopTour(); this.follow=null;
    this.host.querySelector('.scene-location').textContent='Floor plan';
    this.fly([0,46,.01],[0,0,0]);
  }
  zoom(factor) {
    this.stopTour(); this.tween=null;
    const offset=this.camera.position.clone().sub(this.controls.target);
    offset.setLength(THREE.MathUtils.clamp(offset.length()/factor,this.controls.minDistance,this.controls.maxDistance));
    this.camera.position.copy(this.controls.target).add(offset);
  }
  focus(name) {
    this.stopTour();
    const person=this.people.get(name);if(!person)return;
    // Swoop down to a low, over-the-shoulder view and keep following.
    const target=person.root.position.clone().setY(1.1);
    const back=new THREE.Vector3(0,0,1).applyAxisAngle(new THREE.Vector3(0,1,0),person.root.rotation.y+Math.PI);
    const position=target.clone().addScaledVector(back,6.5).add(new THREE.Vector3(1.5,3.2,0));
    this.fly(position.toArray(),target.toArray(),1300); this.follow=name;
  }
  fullscreen() {
    if(document.fullscreenElement) document.exitFullscreen?.();
    else this.host.requestFullscreen?.().catch(() => this.bridge.notice?.('Fullscreen is not available in this browser.'));
    this.renderer.domElement.focus();
  }
  dispose() {
    if(this.disposed)return;this.disposed=true;
    this.resizeObserver.disconnect();this.controls.dispose();
    this.handlers.forEach(([type,fn])=>this.renderer.domElement.removeEventListener(type,fn));
    const geometries=new Set(),materials=new Set(this.materials.values());
    this.scene.traverse(obj=>{if(obj.geometry)geometries.add(obj.geometry);if(obj.isInstancedMesh)obj.dispose();if(obj.material&&!Array.isArray(obj.material))materials.add(obj.material);});
    geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());this.textures.forEach(t=>t.dispose());
    this.renderer.dispose();this.renderer.forceContextLoss();this.renderer.domElement.remove();this.labelLayer.replaceChildren();
  }
}

window.Lab3D = {
  async mount(host,bridge) {
    if(active?.host===host)return;
    this.dispose();
    let instance;
    try {
      instance=new LabScene(host,bridge);active=instance;
      await loadAssets();
      if(instance.disposed||!host.isConnected)return;
      instance.build(bridge.rooms);
    } catch(error) {
      if (instance?.disposed || (instance && active !== instance) || !host.isConnected) return;
      instance?.dispose();if(active===instance)active=null;
      bridge.fail(`3D unavailable: ${error.message}. The 2D lab is still available.`);
    }
  },
  update(actors,state,time) {active?.update(actors,state,time);},
  action(action,name) {
    if(action==='reset')active?.reset();
    if(action==='top')active?.top();
    if(action==='in')active?.zoom(1.25);
    if(action==='out')active?.zoom(.8);
    if(action==='focus')active?.focus(name);
    if(action==='tour')active?.toggleTour();
    if(action==='full')active?.fullscreen();
  },
  dispose() {active?.dispose();active=null;},
};
window.dispatchEvent(new Event('lab3d-ready'));
