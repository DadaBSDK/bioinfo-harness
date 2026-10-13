'use strict';
window.VoxelCharacter = (() => {
  const skins = ['#dca77c', '#a96f50', '#edc7a4', '#774e3a'];
  const hair = ['#352b29', '#523724', '#202b36', '#ad7848'];
  const hash = name => Array.from(name).reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);
  function draw(ctx, { name = 'Scientist', role = 'analyst', x, y, scale = 1.5,
                       angle = -.4, phase = 0, moving = false, working = false }) {
    const seed = hash(name), skin = skins[seed % skins.length];
    const coat = ['runner', 'orchestrator', 'operator'].includes(role)
      ? `hsl(${seed % 360} 38% 49%)` : '#dce9e4';
    const faces = [], cy = Math.cos(angle), sy = Math.sin(angle);
    const project = p => {
      const rx = p[0] * cy + p[2] * sy, rz = -p[0] * sy + p[2] * cy;
      return [x + rx * scale, y + (-p[1] * .94 + rz * .34) * scale, rz * .94 + p[1] * .34];
    };
    function box(cx, bottom, cz, w, h, d, color, tilt = 0, pivot = bottom + h) {
      const vertices = [[-1,0,-1],[1,0,-1],[1,1,-1],[-1,1,-1],[-1,0,1],[1,0,1],[1,1,1],[-1,1,1]]
        .map(([a,b,c]) => {
          const py = bottom + b * h - pivot, pz = cz + c * d / 2;
          return project([cx + a*w/2, pivot + py*Math.cos(tilt) - pz*Math.sin(tilt),
            py*Math.sin(tilt) + pz*Math.cos(tilt)]);
        });
      for (const [ids, shade] of [[[0,1,2,3],.30],[[4,7,6,5],0],[[0,3,7,4],.22],
                                [[1,5,6,2],.12],[[3,2,6,7],-.10],[[0,4,5,1],.4]]) {
        const pts = ids.map(i => vertices[i]);
        faces.push({ pts, color, shade, z: pts.reduce((s,p) => s+p[2],0)/4 });
      }
    }
    const swing = moving ? Math.sin(phase) * .65 : 0;
    box(-2.2, 1, 0, 4, 10, 4, '#405367', swing, 11);
    box(2.2, 1, 0, 4, 10, 4, '#405367', -swing, 11);
    box(-2.2, 0, .6, 4.2, 2.5, 5, '#25323c', swing, 11);
    box(2.2, 0, .6, 4.2, 2.5, 5, '#25323c', -swing, 11);
    box(0, 11, 0, 8, 11, 5, coat);
    for (const side of [-1,1]) {
      const tilt = moving ? -side*swing : working ? -.65 + Math.sin(phase)*.1 : .06*side;
      box(side*6, 12, 0, 4, 9, 4, coat, tilt, 21);
      box(side*6, 10, 0, 4, 3, 4, skin, tilt, 21);
    }
    box(0, 22, 0, 8, 8, 8, skin);
    box(0, 28, 0, 8.3, 2.5, 8.3, hair[seed % hair.length]);
    box(0, 24, -3.8, 8.2, 5, .8, hair[seed % hair.length]);
    box(-2, 25, 4.08, 1.5, 1.3, .2, '#263439');
    box(2, 25, 4.08, 1.5, 1.3, .2, '#263439');
    box(0, 23.4, 4.1, 2, .65, .2, '#925f4b');
    box(0, 18, 2.6, 1.2, 4, .2, '#547e7b');
    box(-2, 17, 2.65, 2, 2, .25, `hsl(${seed % 360} 60% 58%)`);
    if (role === 'curator' || role === 'validator') {
      box(0, 24.7, 4.3, 7.8, 2.3, .4, '#75c8ca');
      box(-2, 25, 4.55, 2, 1, .1, '#d3f8ed');
      box(2, 25, 4.55, 2, 1, .1, '#d3f8ed');
    }
    ctx.save();
    faces.sort((a,b) => a.z-b.z).forEach(({pts,color,shade}) => {
      ctx.beginPath();
      pts.forEach((p,i) => ctx[i ? 'lineTo' : 'moveTo'](Math.round(p[0]), Math.round(p[1])));
      ctx.closePath(); ctx.fillStyle = color; ctx.fill();
      ctx.fillStyle = shade < 0 ? `rgba(255,255,255,${-shade})` : `rgba(0,0,0,${shade})`;
      ctx.fill();
    });
    ctx.restore();
  }
  return { draw };
})();
