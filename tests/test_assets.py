"""Sprite atlas integrity.

The floor renderer names sprites as strings. Nothing else checks that those
names exist, so a renamed sprite would silently draw nothing -- these tests
tie the JavaScript and the atlas together.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "bioharness" / "ui"
ATLAS_JSON = UI / "assets" / "atlas.json"
ATLAS_PNG = UI / "assets" / "atlas.png"
APP_JS = UI / "app.js"


@pytest.fixture(scope="module")
def atlas() -> dict:
    return json.loads(ATLAS_JSON.read_text())


def test_atlas_files_exist():
    assert ATLAS_JSON.is_file() and ATLAS_PNG.is_file()
    assert ATLAS_PNG.stat().st_size > 0


def test_atlas_frames_are_well_formed(atlas):
    assert atlas["image"] == "/assets/atlas.png"
    assert len(atlas["frames"]) >= 20
    for name, f in atlas["frames"].items():
        assert {"x", "y", "w", "h"} <= set(f), name
        assert f["w"] > 0 and f["h"] > 0, name


def test_frames_lie_inside_the_atlas_image(atlas):
    from PIL import Image
    with Image.open(ATLAS_PNG) as img:
        w, h = img.size
    for name, f in atlas["frames"].items():
        assert f["x"] + f["w"] <= w and f["y"] + f["h"] <= h, f"{name} overflows the atlas"


def test_frames_do_not_overlap(atlas):
    boxes = [(n, f["x"], f["y"], f["x"] + f["w"], f["y"] + f["h"])
             for n, f in atlas["frames"].items()]
    for i, (n1, ax0, ay0, ax1, ay1) in enumerate(boxes):
        for n2, bx0, by0, bx1, by1 in boxes[i + 1:]:
            overlap = ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1
            assert not overlap, f"{n1} overlaps {n2} in the atlas"


def test_every_sprite_the_ui_asks_for_exists(atlas):
    """Scrape the sprite names out of app.js and check each one is packed."""
    js = APP_JS.read_text()
    frames = set(atlas["frames"])

    referenced: set[str] = set()
    # ROLE_SPRITE / SPRITE_POOL / props lists / explicit fillTiles calls
    referenced |= set(re.findall(r"'(char_[a-z_]+)'", js))
    referenced |= set(re.findall(r"'(floor_[a-z_]+)'", js))
    referenced |= set(re.findall(r"'(wall_[a-z_]+)'", js))
    for props in re.findall(r"props:\s*\[([^\]]*)\]", js):
        referenced |= set(re.findall(r"'([a-z_]+)'", props))

    assert referenced, "found no sprite references in app.js — did the scrape break?"
    missing = sorted(referenced - frames)
    assert not missing, f"app.js references sprites missing from the atlas: {missing}"


def test_characters_and_tiles_are_catalogued(atlas):
    assert len(atlas["characters"]) >= 6
    assert all(t.startswith(("floor_", "wall_")) for t in atlas["tiles"])
    assert "floor_lino" in atlas["tiles"]        # the renderer's fallback tile


def test_tiles_are_square_and_uniform(atlas):
    sizes = {atlas["frames"][t]["w"] for t in atlas["tiles"]}
    assert sizes == {32}, f"tiles must all be 32px, got {sizes}"
    assert all(atlas["frames"][t]["h"] == 32 for t in atlas["tiles"])


def test_ui_assets_are_reachable_by_the_server():
    """The static handler must resolve /assets/* without escaping the ui dir."""
    from bioharness.server import UI_DIR
    target = (UI_DIR / "assets/atlas.png").resolve()
    assert str(target).startswith(str(UI_DIR.resolve()))
    assert target.is_file()
