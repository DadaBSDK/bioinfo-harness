"""Build the static GitHub Pages demo.

    python tools/build_demo.py record          # record a session into demo/data
    python tools/build_demo.py site _site      # assemble the static site

`record` hires a lab on a local Ollama model, gives it one order, lets it work
in dry-run mode, adds a dry run of the shipped wgs-variants workflow, and
snapshots every dashboard endpoint to JSON. Host names, user names and local
paths are scrubbed from the snapshot. It needs Ollama; `site` does not, and is
what the Pages workflow runs.
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bioharness.cli import attend, main as cli_main
from bioharness.core.brief import gather, render
from bioharness.server import Dashboard

DATA = ROOT / "demo" / "data"
LAB_NAME = "Kim Lab"
ROSTER = [("hugo", "pi"), ("ada", "planner"), ("iris", "curator"), ("rex", "runner"),
          ("vera", "validator"), ("nova", "analyst"), ("quill", "scribe")]
ORDER = ("Fetch Illumina reads for ENA run SRR2584863 and call variants against "
         "the E. coli REL606 reference. accession=SRR2584863 "
         "url=https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/017/985/"
         "GCF_000017985.1_ASM1798v1/GCF_000017985.1_ASM1798v1_genomic.fna.gz")
WORKFLOW_PARAMS = ["reads=data/SRR2584863_1.fastq.gz", "reference=refs/REL606.fna",
                   "genome_build=Escherichia_coli_B_str_REL606", "threads=4"]


def scrubber(work: Path):
    """Replace local paths, the host name and the user name with neutral ones."""
    pairs = [(str(work / "runs"), "~/lab/runs"), (str(work / "hive"), "~/lab/hive"),
             (str(work), "~/lab"), (str(ROOT), "~/bioinfo-harness"),
             (str(Path.home()), "~"), (socket.gethostname(), "lab-workstation"),
             (getpass.getuser(), "researcher")]
    pairs.sort(key=lambda p: -len(p[0]))

    def scrub(value: Any) -> Any:
        text = json.dumps(value)
        for old, new in pairs:
            text = text.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
        return json.loads(text)
    return scrub


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, default=str))


def snapshot(work: Path, out: Path) -> None:
    """Write every GET endpoint the dashboard uses, as the server would answer."""
    dash = Dashboard(work / "runs", work / "hive", [ROOT / "capabilities"])
    scrub = scrubber(work)
    if out.exists():
        shutil.rmtree(out)
    put = lambda rel, value: write(out / rel, scrub(value))

    put("overview.json", dash.overview())
    put("runs.json", dash.run_list())
    put("floor.json", dash.floor_state())
    put("hive.json", dash.hive_state())
    put("conversation.json", dash.conversation(limit=1000))
    put("caps.json", dash.caps())
    put("staff-options.json", dash.staff_options())
    put("briefs.json", dash.briefs())
    for run in dash.run_list():
        detail = dash.run_detail(run["run_id"])
        put(f"runs/{run['run_id']}.json", detail)
        for task in detail.get("tasks", []):
            step = task.get("step") or task.get("id")
            for stream in ("stdout", "stderr"):
                put(f"logs/{run['run_id']}/{step}.{stream}.json",
                    dash.run_log(run["run_id"], step, stream))
    db = sqlite3.connect(work / "hive" / "palace" / "index.sqlite")
    db.row_factory = sqlite3.Row
    put("notes.json", [dict(r) for r in db.execute("SELECT * FROM notes ORDER BY ts DESC")])
    hive = dash.hive()
    for b in dash.briefs():
        render(scrub(gather(hive, b["thread"])), out / "briefs")
    put("meta.json", {"recorded": time.time(), "lab_name": hive.lab_name()})


def record(model: str, seconds: float, out: Path) -> None:
    work = Path(tempfile.mkdtemp(prefix="bioharness-demo-"))
    dash = Dashboard(work / "runs", work / "hive", [ROOT / "capabilities"])
    dash.hive().set_lab_name(LAB_NAME)
    for name, role in ROSTER:
        got = dash.hire(name, role, "ollama", model,
                        "full" if role == "runner" else "bounded")
        if not got.get("ok"):
            sys.exit(f"could not hire {name}: {got.get('error')}")
    cli_main(["--hive", str(work / "hive"), "--runs", str(work / "runs"),
              "run", "wgs-variants", "--dry-run",
              *[a for p in WORKFLOW_PARAMS for a in ("--set", p)]])
    dash.send_message("*", "directive", {"order": ORDER})

    stop = threading.Event()
    loop = threading.Thread(target=attend, args=(str(work / "hive"), [str(ROOT / "capabilities")]),
                            kwargs=dict(stop=stop, quiet=True, interval=0.5,
                                        runs=str(work / "runs"), dry_run=True,
                                        brief_after=8, social_every=20, seed=7))
    loop.start()
    print(f"recording for {seconds:.0f}s in {work}")
    time.sleep(seconds)
    stop.set()
    loop.join()
    snapshot(work, out)
    shutil.rmtree(work, ignore_errors=True)
    print(f"snapshot written to {out}")


def site(out: Path) -> None:
    """Copy the dashboard UI, the replay shim and the snapshot into `out`."""
    if not (DATA / "meta.json").is_file():
        sys.exit("no snapshot in demo/data; run `python tools/build_demo.py record` first")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "bioharness" / "ui", out)
    shutil.copytree(DATA, out / "data")
    for f in ("demo.js", "demo.css"):
        shutil.copy(ROOT / "demo" / f, out / f)
    index = out / "index.html"
    html = index.read_text()
    html = html.replace('<link rel="stylesheet" href="workspace.css">',
                        '<link rel="stylesheet" href="workspace.css">\n'
                        '<link rel="stylesheet" href="demo.css">', 1)
    html = html.replace('<script src="app.js"></script>',
                        '<script src="demo.js"></script>\n<script src="app.js"></script>', 1)
    if "demo.js" not in html or "demo.css" not in html:
        sys.exit("index.html changed shape; update the injection in build_demo.py")
    index.write_text(html)
    (out / ".nojekyll").write_text("")
    print(f"site written to {out}")


def cli() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record a demo session (needs Ollama)")
    r.add_argument("--model", default="qwen2.5:3b")
    r.add_argument("--seconds", type=float, default=240.0)
    r.add_argument("--out", type=Path, default=DATA)
    s = sub.add_parser("site", help="assemble the static site")
    s.add_argument("out", type=Path)
    args = p.parse_args()
    if args.cmd == "record":
        record(args.model, args.seconds, args.out)
    else:
        site(args.out)


if __name__ == "__main__":
    cli()
