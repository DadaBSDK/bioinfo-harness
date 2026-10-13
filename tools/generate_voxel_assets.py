"""Build original pixel textures and reusable cuboid lab models. Stdlib only.
Run from any directory: python tools/generate_voxel_assets.py
Box schema: [center_x, bottom_y, center_z, width, height, depth, material].
"""
import json
import random
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / 'bioharness/ui/assets/voxel'
PALETTE = {
    'plaster': '#d6dfcc', 'tile': '#afbdb0', 'wood': '#ab8053',
    'metal': '#40565d', 'white': '#e0e8d9', 'dark': '#23383c',
    'screen': '#4cb9af', 'leaf': '#5b924e', 'pot': '#bc7955',
    'blue': '#6a98b0', 'seat': '#778b68', 'rubber': '#283934',
    'amber': '#eab962', 'red': '#cd705b', 'purple': '#a18eb7',
}

def texture(name, color):
    rng = random.Random(name)
    rgb = [int(color[i:i+2], 16) for i in (1,3,5)]
    data = bytearray()
    for y in range(16):
        data.append(0)
        for x in range(16):
            shade = rng.choice([-8,-4,0,0,0,4,8])
            if name == 'wood': shade += -16 if y % 4 == 0 else 0
            if name == 'tile' and (x == 0 or y == 0): shade -= 22
            if name == 'plaster' and (y % 8 == 0 or (x + (8 if y >= 8 else 0)) % 16 == 0): shade -= 12
            if name == 'screen': shade += 24 if (x in (3,4,8,9,12) and 4 < y < 12) else -8
            data.extend(max(0, min(255, c + shade)) for c in rgb)
    def chunk(kind, body):
        return struct.pack('>I',len(body))+kind+body+struct.pack('>I',zlib.crc32(kind+body))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',16,16,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(bytes(data)))+chunk(b'IEND',b'')

MODELS = {}
def model(name):
    boxes=[]; MODELS[name]=boxes
    def add(x,y,z,w,h,d,mat): boxes.append([x,y,z,w,h,d,mat])
    return add

def desk(name='desk_pc'):
    b=model(name)
    b(0,.73,0,1.55,.14,.76,'wood')
    for x in [-.61,.61]:
        for z in [-.25,.25]: b(x,0,z,.1,.73,.1,'metal')
    b(0,.87,-.14,.1,.18,.1,'metal'); b(0,1.03,-.2,.86,.52,.12,'dark')
    b(0,1.08,-.131,.73,.37,.02,'screen'); b(0,.88,.19,.56,.035,.2,'dark')
    b(.56,.87,.16,.16,.2,.16,'white')
    b(-.6,.87,-.16,.18,.07,.25,'purple')
desk()
b=model('bench'); b(0,0,0,1,.82,.74,'white'); b(0,.82,0,1.05,.12,.84,'metal')
for x in [-.25,.25]: b(x,.49,.378,.28,.055,.03,'dark')
b=model('chair'); b(0,0,0,.55,.12,.55,'dark'); b(0,.1,0,.13,.4,.13,'metal'); b(0,.48,0,.65,.13,.58,'seat'); b(0,.6,-.23,.65,.6,.14,'seat')
for name,accent,height in [('sequencer','screen',1.45),('pcr','blue',.85),('incubator','amber',1.25),('freezer','blue',1.9),('gel_dock','purple',.75),('centrifuge','blue',.72),('printer','metal',.6)]:
    b=model(name); b(0,0,0,.94,height,.72,'white'); b(0,.07,.37,.77,height*.55,.025,'dark')
    b(0,.12,.39,.62,height*.43,.018,accent); b(0,height-.2,.372,.68,.08,.03,'metal')
    b(.32,height-.36,.4,.1,.08,.03,'amber'); b(-.1,height,0,.73,.08,.56,'metal')
    if name=='freezer': b(.31,.65,.42,.06,.49,.07,'white')
    if name=='sequencer':
        for x in [-.22,0,.22]: b(x,.17,.414,.07,.32,.02,'screen')
b=model('server_rack'); b(0,0,0,.85,1.85,.78,'dark')
for y in [.15,.46,.77,1.08,1.39]:
    b(0,y,.4,.72,.23,.05,'metal')
    for x in [-.27,-.16]: b(x,y+.09,.44,.055,.06,.025,'screen')
    b(.16,y+.09,.44,.25,.04,.025,'rubber')
b(0,1.83,0,.93,.09,.85,'metal')
b=model('microscope'); b(0,0,0,.5,.08,.5,'metal'); b(-.12,.08,-.1,.12,.7,.14,'white'); b(0,.71,-.1,.35,.12,.18,'white'); b(.11,.6,-.02,.14,.15,.14,'dark'); b(0,.34,.03,.4,.06,.32,'metal'); b(-.13,.8,-.13,.16,.2,.16,'dark')
b=model('plant'); b(0,0,0,.42,.39,.42,'pot'); b(0,.37,0,.14,.45,.14,'wood')
for x,y,z,w in [(-.18,.64,0,.43),(.17,.78,.07,.48),(0,1.01,-.06,.48),(-.14,.94,.14,.36)]: b(x,y,z,w,.34,w,'leaf')
b=model('sofa'); b(0,0,0,1.4,.16,.68,'wood'); b(0,.16,0,1.3,.32,.67,'seat'); b(0,.43,-.27,1.4,.42,.19,'seat')
for x in [-.67,.67]: b(x,.16,0,.15,.51,.7,'seat')
b=model('table'); b(0,.46,0,1,.13,.65,'wood')
for x in [-.38,.38]: b(x,0,0,.1,.46,.4,'metal')
b=model('flask_rack'); b(0,0,0,.66,.06,.3,'metal')
for x,mat in [(-.23,'screen'),(0,'purple'),(.23,'amber')]:
    b(x,.06,0,.16,.2,.18,mat); b(x,.26,0,.07,.16,.08,'white')
b=model('sink'); b(0,0,0,.9,.8,.65,'white'); b(0,.8,0,.94,.08,.69,'metal'); b(0,.89,0,.55,.025,.42,'dark'); b(0,.88,-.21,.07,.35,.07,'metal'); b(0,1.18,-.1,.07,.05,.28,'metal')
b=model('water_cooler'); b(0,0,0,.5,.87,.5,'white'); b(0,.87,0,.34,.45,.34,'blue'); b(0,.53,.26,.12,.1,.03,'screen')
b=model('filing_cabinet'); b(0,0,0,.67,1.2,.6,'metal')
for y in [.07,.44,.81]: b(0,y,.31,.56,.32,.035,'white'); b(0,y+.18,.35,.2,.04,.03,'dark')
b=model('shelving')
for x in [-.5,.5]: b(x,0,0,.08,1.8,.45,'metal')
for y in [0,.55,1.1,1.72]:
    b(0,y,0,1.06,.08,.48,'wood')
    if y<1.7:
        for x,mat in [(-.32,'white'),(-.1,'purple'),(.15,'blue'),(.35,'amber')]: b(x,y+.08,0,.13,.32,.3,mat)
b=model('whiteboard'); b(0,0,0,.1,1,.15,'metal'); b(0,.95,0,1.3,.73,.1,'metal'); b(0,1.01,.06,1.18,.61,.025,'white')
for x,y,w in [(-.25,1.43,.46),(.12,1.3,.68),(-.26,1.17,.44)]: b(x,y,.08,w,.035,.025,'screen')
b=model('clock'); b(0,0,0,.11,1.5,.1,'metal'); b(0,1.42,0,.45,.45,.1,'dark'); b(0,1.46,.06,.36,.36,.02,'white'); b(0,1.61,.08,.03,.14,.02,'dark'); b(.06,1.6,.08,.13,.03,.02,'dark')

if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    for name,color in PALETTE.items(): (OUT/(name+'.png')).write_bytes(texture(name,color))
    (OUT/'models.json').write_text(json.dumps({'version':1,'box_schema':['x','bottom_y','z','width','height','depth','material'],'materials':PALETTE,'models':MODELS},indent=2)+'\n')
    print(f'Generated {len(PALETTE)} textures and {len(MODELS)} models in {OUT}')

def export_glb(boxes):
    positions=[]; normals=[]; uvs=[]; indices=[]
    faces=[((1,0,0),[(.5,-.5,.5),(.5,-.5,-.5),(.5,.5,-.5),(.5,.5,.5)]),
           ((-1,0,0),[(-.5,-.5,-.5),(-.5,-.5,.5),(-.5,.5,.5),(-.5,.5,-.5)]),
           ((0,1,0),[(-.5,.5,.5),(.5,.5,.5),(.5,.5,-.5),(-.5,.5,-.5)]),
           ((0,-1,0),[(-.5,-.5,-.5),(.5,-.5,-.5),(.5,-.5,.5),(-.5,-.5,.5)]),
           ((0,0,1),[(-.5,-.5,.5),(.5,-.5,.5),(.5,.5,.5),(-.5,.5,.5)]),
           ((0,0,-1),[(.5,-.5,-.5),(-.5,-.5,-.5),(-.5,.5,-.5),(.5,.5,-.5)])]
    for normal,vertices in faces:
        offset=len(positions)//3
        positions.extend(c for v in vertices for c in v)
        normals.extend(normal*4);uvs.extend([0,1,1,1,1,0,0,0])
        indices.extend(offset+i for i in [0,1,2,0,2,3])
    blob=bytearray();views=[]
    def add(data):
        blob.extend(b'\x00'*(-len(blob)%4));offset=len(blob);blob.extend(data)
        views.append({'buffer':0,'byteOffset':offset,'byteLength':len(data)})
        return len(views)-1
    arrays=[(positions,'f'),(normals,'f'),(uvs,'f'),(indices,'H')]
    for values,kind in arrays: add(struct.pack('<'+kind*len(values),*values))
    mats=list(dict.fromkeys(b[6] for b in boxes))
    images=[{'bufferView':add(texture(m,PALETTE[m])),'mimeType':'image/png'} for m in mats]
    material_ids={m:i for i,m in enumerate(mats)}
    nodes=[{'name':f'block-{i}','mesh':material_ids[m], 'translation':[x,y+h/2,z],'scale':[w,h,d]}
           for i,(x,y,z,w,h,d,m) in enumerate(boxes)]
    doc={'asset':{'version':'2.0','generator':'Bioinfo Harness voxel asset generator'},
         'scene':0,'scenes':[{'nodes':list(range(len(nodes)))}],'nodes':nodes,
         'meshes':[{'name':m,'primitives':[{'attributes':{'POSITION':0,'NORMAL':1,'TEXCOORD_0':2},'indices':3,'material':i}]} for i,m in enumerate(mats)],
         'materials':[{'name':m,'pbrMetallicRoughness':{'baseColorTexture':{'index':i},'metallicFactor':0,'roughnessFactor':1}} for i,m in enumerate(mats)],
         'samplers':[{'magFilter':9728,'minFilter':9728,'wrapS':10497,'wrapT':10497}],
         'textures':[{'sampler':0,'source':i} for i in range(len(mats))],'images':images,
         'accessors':[{'bufferView':0,'componentType':5126,'count':24,'type':'VEC3','min':[-.5]*3,'max':[.5]*3},
                      {'bufferView':1,'componentType':5126,'count':24,'type':'VEC3'},
                      {'bufferView':2,'componentType':5126,'count':24,'type':'VEC2'},
                      {'bufferView':3,'componentType':5123,'count':36,'type':'SCALAR'}],
         'buffers':[{'byteLength':len(blob)}],'bufferViews':views}
    js=json.dumps(doc,separators=(',',':')).encode();js+=b' '*(-len(js)%4)
    blob+=b'\x00'*(-len(blob)%4)
    return struct.pack('<III',0x46546c67,2,12+8+len(js)+8+len(blob))+struct.pack('<II',len(js),0x4e4f534a)+js+struct.pack('<II',len(blob),0x004e4942)+blob

if __name__=='__main__':
    for name,boxes in MODELS.items(): (OUT/(name+'.glb')).write_bytes(export_glb(boxes))
    print(f'Exported {len(MODELS)} self-contained GLB models')
