# Sprite assets

`atlas.png` + `atlas.json` are built from the sheets in `assets/raw/` by:

```bash
python tools/slice_sprites.py
```

The slicer keys out the flat magenta background, crops each sprite as a
connected blob, downsamples it to a small native resolution and re-quantises
the palette so the result is genuine low-resolution pixel art rather than a
shrunk painting. Everything is then packed into a single atlas.

## Provenance

The source sheets were generated with Google's Nano Banana (Gemini image
model) from prompts written for this project, and are original to it. No
third-party sprite pack is bundled or redistributed here.

To swap in different artwork, drop replacement sheets into `assets/raw/`,
update the `SHEETS` table at the top of `tools/slice_sprites.py` (names are
listed in reading order; `None` discards a cell), and re-run the slicer. The
renderer reads `atlas.json`, so nothing in the UI needs to change as long as
the sprite names stay the same — `tests/test_assets.py` enforces that.
