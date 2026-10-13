"""The static GitHub Pages demo: site assembly and the committed snapshot."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "demo" / "data"


def test_site_injects_the_replay_shim(tmp_path: Path):
    out = tmp_path / "site"
    subprocess.run([sys.executable, str(ROOT / "tools" / "build_demo.py"), "site", str(out)],
                   check=True, capture_output=True)
    html = (out / "index.html").read_text()
    assert html.index('src="demo.js"') < html.index('src="app.js"')
    assert (out / "data" / "floor.json").is_file() and (out / ".nojekyll").is_file()


def test_the_ui_uses_relative_urls_so_it_works_under_a_subpath():
    ui = ROOT / "bioharness" / "ui"
    for f in ("index.html", "app.js", "lab3d.js"):
        text = (ui / f).read_text()
        for prefix in ("'/api/", '"/api/', "`/api/", "'/assets/", '"/assets/', '"/vendor/'):
            assert prefix not in text, f"{f} uses an absolute URL: {prefix}"


def test_the_snapshot_has_no_local_paths():
    for f in DATA.rglob("*.json"):
        text = f.read_text()
        for marker in ("/home/", "/Users/", "/tmp/", "C:\\\\"):
            assert marker not in text, f"{f.relative_to(ROOT)} contains {marker}"


def test_every_brief_in_the_snapshot_has_its_pdf():
    for b in json.loads((DATA / "briefs.json").read_text()):
        assert (DATA / "briefs" / b["pdf"]).is_file()
