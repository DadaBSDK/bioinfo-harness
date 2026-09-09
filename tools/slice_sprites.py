"""Turn generated sprite sheets into a packed atlas the dashboard can draw.

The sheets come back from an image model as large, chunky-pixel artwork on a
flat magenta field. This does four things:

  1. keys the magenta out to transparency,
  2. finds each sprite as a connected blob and crops it,
  3. downsamples to a small native size and re-flattens the palette, so the
     result is actual low-resolution pixel art rather than a shrunk painting,
  4. packs everything into one atlas.png + atlas.json.

Run:  python tools/slice_sprites.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

RAW = Path("assets/raw")
OUT = Path("bioharness/ui/assets")

# Sprites are named in reading order (rows top to bottom, left to right).
# `None` discards a cell -- the generator produced several near-duplicates.
SHEETS: dict[str, dict] = {
    "characters.png": {
        "names": ["char_senior", "char_tablet", "char_goggles", "char_pipette",
                  "char_hoodie", "char_curly", "char_hijab", "char_casual"],
        "height": 34,          # native px; drawn at 2x on the floor
        "kind": "sprite",
    },
    "props.png": {
        "names": ["bench", "desk_pc", "server_rack", "sequencer",
                  "centrifuge", "microscope", None, "fume_hood",
                  None, None, None, "flask_rack",
                  "chair", "whiteboard", "plant", "door"],
        "max": 30,
        "kind": "sprite",
    },
    "tiles.png": {
        "names": ["floor_lino", "floor_tile", "floor_grate",
                  "floor_wood", None, "wall_skirting",
                  "wall_plain", None, None],
        "size": 32,
        "kind": "tile",
    },
}

MAGENTA_TOLERANCE = 90
MIN_AREA = 400          # drops the stray sparkle artefacts the model adds
JOIN_RADIUS = 5         # dilation used only for grouping, never for output


def key_magenta(img: Image.Image) -> Image.Image:
    """Flat magenta field -> alpha 0. Magenta is used because it appears
    nowhere in the artwork itself.

    The generator antialiases the sprite edges against the background, leaving
    a ring of half-magenta pixels. Keying alone leaves that ring behind as a
    pink halo, so anything magenta-dominant is dropped too: a pixel counts as
    background when it is much redder and bluer than it is green.
    """
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    dist = np.abs(r - 255) + np.abs(g - 0) + np.abs(b - 255)
    flat = (dist < MAGENTA_TOLERANCE * 3) & (r > 150) & (b > 150) & (g < 150)
    fringe = (r - g > 45) & (b - g > 45) & (r > 110) & (b > 110)
    bg = flat | fringe
    out = np.dstack([rgb.astype(np.uint8), np.where(bg, 0, 255).astype(np.uint8)])
    return Image.fromarray(out, "RGBA")


def erode_alpha(img: Image.Image, px: int = 3) -> Image.Image:
    """Pull the alpha edge in by one pixel, removing any surviving halo."""
    alpha = img.getchannel("A").filter(ImageFilter.MinFilter(px))
    out = img.copy()
    out.putalpha(alpha)
    return out


def components(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of connected non-transparent regions (4-connected BFS)."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    boxes = []
    for y in range(h):
        row = np.flatnonzero(mask[y] & ~seen[y])
        for x in row:
            if seen[y, x]:
                continue
            x0 = x1 = x
            y0 = y1 = y
            stack = [(y, x)]
            seen[y, x] = True
            area = 0
            while stack:
                cy, cx = stack.pop()
                area += 1
                x0, x1 = min(x0, cx), max(x1, cx)
                y0, y1 = min(y0, cy), max(y1, cy)
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if area >= MIN_AREA:
                boxes.append((x0, y0, x1 + 1, y1 + 1))
    return boxes


def reading_order(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Group boxes into rows by vertical overlap, then order each row by x."""
    if not boxes:
        return []
    heights = [b[3] - b[1] for b in boxes]
    tol = max(20, int(np.median(heights) * 0.5))
    rows: list[list] = []
    for b in sorted(boxes, key=lambda b: b[1]):
        cy = (b[1] + b[3]) / 2
        for row in rows:
            ref = (row[0][1] + row[0][3]) / 2
            if abs(cy - ref) < tol:
                row.append(b)
                break
        else:
            rows.append([b])
    return [b for row in rows for b in sorted(row, key=lambda b: b[0])]


def flatten(img: Image.Image, colors: int = 24) -> Image.Image:
    """Re-quantise after downsampling so edges read as flat pixel art again."""
    alpha = img.getchannel("A").point(lambda a: 255 if a > 128 else 0)
    quant = img.convert("RGB").quantize(colors=colors, method=Image.MEDIANCUT)
    out = quant.convert("RGBA")
    out.putalpha(alpha)
    return out


def extract(path: Path, spec: dict) -> dict[str, Image.Image]:
    img = key_magenta(Image.open(path))
    mask_img = img.getchannel("A").point(lambda a: 255 if a > 100 else 0)
    grouped = mask_img.filter(ImageFilter.MaxFilter(JOIN_RADIUS))
    boxes = reading_order(components(np.asarray(grouped) > 0))

    names = spec["names"]
    if len(boxes) != len(names):
        print(f"  ! {path.name}: found {len(boxes)} sprites, expected {len(names)}")

    sprites: dict[str, Image.Image] = {}
    for name, box in zip(names, boxes):
        if name is None:
            continue
        crop = img.crop(box)
        crop = crop.crop(crop.getchannel("A").getbbox())     # tighten
        if spec["kind"] == "tile":
            # Tiles are solid squares: shave the outer ring rather than erode,
            # so the texture stays edge to edge and still tiles seamlessly.
            inset = 5
            if crop.width > inset * 3 and crop.height > inset * 3:
                crop = crop.crop((inset, inset, crop.width - inset, crop.height - inset))
            target = (spec["size"], spec["size"])
        elif "height" in spec:
            scale = spec["height"] / crop.height
            target = (max(1, round(crop.width * scale)), spec["height"])
        else:
            scale = spec["max"] / max(crop.width, crop.height)
            target = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
        if spec["kind"] == "sprite":
            crop = erode_alpha(crop)
        sprites[name] = flatten(crop.resize(target, Image.BOX))
    return sprites


def pack(sprites: dict[str, Image.Image], width: int = 256) -> tuple[Image.Image, dict]:
    """Shelf-pack into one atlas; the index is what the renderer reads."""
    pad, x, y, shelf = 1, 1, 1, 0
    frames: dict[str, dict] = {}
    for name, spr in sorted(sprites.items(), key=lambda kv: -kv[1].height):
        if x + spr.width + pad > width:
            x, y, shelf = 1, y + shelf + pad, 0
        frames[name] = {"x": x, "y": y, "w": spr.width, "h": spr.height}
        x += spr.width + pad
        shelf = max(shelf, spr.height)
    height = y + shelf + pad
    atlas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for name, spr in sprites.items():
        f = frames[name]
        atlas.paste(spr, (f["x"], f["y"]))
    return atlas, frames


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    all_sprites: dict[str, Image.Image] = {}
    for filename, spec in SHEETS.items():
        path = RAW / filename
        if not path.exists():
            print(f"  ! missing {path}")
            continue
        found = extract(path, spec)
        print(f"  {filename}: {len(found)} sprites -> {', '.join(sorted(found))}")
        all_sprites.update(found)

    atlas, frames = pack(all_sprites)
    atlas.save(OUT / "atlas.png")
    (OUT / "atlas.json").write_text(json.dumps({
        "image": "/assets/atlas.png",
        "frames": frames,
        "tiles": [n for n in frames if n.startswith(("floor_", "wall_"))],
        "characters": [n for n in frames if n.startswith("char_")],
    }, indent=2, sort_keys=True))
    print(f"\natlas {atlas.size} with {len(frames)} frames -> {OUT / 'atlas.png'}")


if __name__ == "__main__":
    main()
