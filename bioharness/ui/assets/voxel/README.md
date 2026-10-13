# Original voxel laboratory asset pack

22 reusable models, 15 seamless-style 16×16 pixel material tiles, and portable
self-contained GLB exports. All geometry and textures are original procedural
artwork made for Bioinfo Harness. No Minecraft assets are used.

Regenerate deterministically from the repository root:

```sh
python tools/generate_voxel_assets.py
```

`models.json` is the live scene's source of truth. Every model contains boxes in
metres: `[x, bottom_y, z, width, height, depth, material]`. +Y is up and +Z is the
front. GLBs use the same recipes, with embedded PNG textures, nearest filtering,
shared unit-cube geometry, and opaque rough materials. They can be imported into
Blender, Godot, or any glTF 2.0 viewer independently of this app.

Included models: bench, chair, desk_pc, sequencer, pcr, incubator, freezer,
gel_dock, centrifuge, printer, server_rack, microscope, plant, sofa, table,
flask_rack, sink, water_cooler, filing_cabinet, shelving, whiteboard, clock.

The 3D floor, walls, windows, doorways, lighting, and articulated researchers are
assembled by `ui/lab3d.js`. Character skins are deterministic per researcher name.
Static blocks are batched by material with instanced meshes; characters remain
articulated meshes. The original 2D floor remains available via the 3D Lab toggle.

The bundled renderer is Three.js 0.180.0, from the official npm distribution;
its MIT notice is in `ui/vendor/THREE-LICENSE.txt`. All runtime files are local.

## Immersive scene controls

Drag to orbit, scroll to zoom, or use WASD / arrow keys to move the camera and
Q/E to turn. Select a researcher and use **Follow agent** for a close tracking
view. Room buttons fly to individual workspaces. **Tour** visits every room;
manual orbiting, movement, or Overview cancels the tour. **F** toggles fullscreen
and **Home** returns to the overview. Reduced-motion preferences disable the
opening flight and ambient movement; Pause motion is also available in the UI.

The animated DNA installation, clouds, dust, and equipment lights are generated
in the scene. Actual recent messages between visible researchers produce 3D
message arcs. Distance-based timing and arrival animation are adapted from
Munder Difflin's `MessageEnvelope.ts`:
https://github.com/HarnessMD/munder-difflin/blob/main/src/renderer/src/scene/office/MessageEnvelope.ts
The MIT notice is included in `ui/vendor/MUNDER-LICENSE.txt`. Its artwork is not
included. Message animation never sends messages or simulates backend activity.
