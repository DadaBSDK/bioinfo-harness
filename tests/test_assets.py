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
    for prefix in ("char_", "sit_", "floor_", "wall_", "counter_"):
        referenced |= set(re.findall(rf"'({prefix}[a-z_]+)'", js))
    for key in ("wall", "floorProps"):
        for block in re.findall(rf"{key}:\s*\[(.*?)\],?\s*\}}", js, re.S):
            referenced |= set(re.findall(r"'([a-z_]+)'", block))

    assert referenced, "found no sprite references in app.js — did the scrape break?"
    missing = sorted(referenced - frames)
    assert not missing, f"app.js references sprites missing from the atlas: {missing}"


def test_every_seated_sprite_has_a_standing_counterpart(atlas):
    """The renderer falls back to standing when an agent is walking, so a
    seated sprite without its standing twin would pop between two people."""
    frames = set(atlas["frames"])
    for name in (n for n in frames if n.startswith("sit_")):
        assert name.replace("sit_", "char_") in frames, f"{name} has no standing sprite"


def test_characters_tiles_and_walls_are_catalogued(atlas):
    assert len(atlas["characters"]) >= 6
    assert "floor_lino" in atlas["tiles"]        # the renderer's fallback tile
    assert "wall_run" in atlas["walls"] and "wall_door" in atlas["walls"]
    # Wall pieces are drawn as a band, never as a 32px tile.
    assert not set(atlas["walls"]) & set(atlas["tiles"])


def test_tiles_are_square_and_uniform(atlas):
    """Repeating floor tiles must be exactly 32px or they will not tile."""
    for t in atlas["tiles"]:
        f = atlas["frames"][t]
        assert (f["w"], f["h"]) == (32, 32), f"{t} is {f['w']}x{f['h']}, not 32x32"


def test_wall_pieces_share_one_size(atlas):
    """drawWall tiles the run and swaps in the door, so both must match."""
    sizes = {(atlas["frames"][w]["w"], atlas["frames"][w]["h"]) for w in atlas["walls"]}
    assert len(sizes) == 1, f"wall pieces disagree on size: {sizes}"


def test_ui_assets_are_reachable_by_the_server():
    """The static handler must resolve /assets/* without escaping the ui dir."""
    from bioharness.server import UI_DIR
    target = (UI_DIR / "assets/atlas.png").resolve()
    assert str(target).startswith(str(UI_DIR.resolve()))
    assert target.is_file()


# -- floor geometry --------------------------------------------------------
def _node_available() -> bool:
    import shutil
    return shutil.which("node") is not None


@pytest.mark.skipif(not _node_available(), reason="node is not installed")
def test_no_route_ever_crosses_a_wall():
    """Characters must enter and leave rooms through doorways only."""
    import subprocess
    proc = subprocess.run(["node", str(ROOT / "tests" / "route_check.mjs")],
                          capture_output=True, text=True, timeout=60)
    report = json.loads(proc.stdout or "{}")
    assert report.get("crossings") == 0, (
        f"routes cut through walls: {report.get('examples')}")
    assert report.get("samples", 0) > 5000, "the sampler did not actually run"
    assert proc.returncode == 0, proc.stdout


@pytest.mark.skipif(not _node_available(), reason="node is not installed")
def test_every_room_has_exactly_one_reachable_door():
    import subprocess
    proc = subprocess.run(["node", str(ROOT / "tests" / "route_check.mjs")],
                          capture_output=True, text=True, timeout=60)
    report = json.loads(proc.stdout or "{}")
    assert report.get("doorless") == [], f"rooms with no door: {report['doorless']}"
    assert report.get("misplaced") == [], (
        f"doorways outside their own wall: {report['misplaced']}")
    assert report.get("rooms", 0) >= 7
