"""Isolated run workspaces + a provenance ledger.

Every run gets a per-run, per-step directory tree that nothing else writes to,
plus an append-only record of exactly what produced every file.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

from .schemas import Artifact, Task, dumps, new_id, sha256_file


class Workspace:
    """Layout:

        runs/<run_id>/
            run.json          manifest: workflow, params, environment
            ledger.jsonl      append-only provenance events
            inputs/           symlinks to declared inputs (never copies)
            steps/<step_id>/  cwd for one task; stdout/stderr/outputs live here
            results/          promoted final outputs
    """

    def __init__(self, root: str | Path, run_id: str | None = None):
        self.root = Path(root).expanduser().resolve()
        self.run_id = run_id or new_id("run")
        self.dir = self.root / self.run_id
        self.ledger_path = self.dir / "ledger.jsonl"

    def create(self, meta: dict[str, Any] | None = None) -> "Workspace":
        for sub in ("steps", "inputs", "results", "logs"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": self.run_id,
            "created": time.time(),
            "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "cwd": os.getcwd(),
            "host": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            **(meta or {}),
        }
        (self.dir / "run.json").write_text(dumps(manifest))
        self.record("run.created", manifest)
        return self

    def step_dir(self, step_id: str) -> Path:
        d = self.dir / "steps" / step_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def stage_input(self, name: str, src: str | Path) -> Path:
        """Link an external input into the run so provenance can hash it.
        Symlink first (sequencing data is large); copy only if linking fails."""
        src = Path(src).expanduser().resolve()
        dest = self.dir / "inputs" / f"{name}__{src.name}"
        if dest.exists() or dest.is_symlink():
            return dest
        try:
            dest.symlink_to(src)
        except OSError:
            shutil.copy2(src, dest) if src.is_file() else shutil.copytree(src, dest)
        return dest

    def promote(self, path: str | Path, as_name: str | None = None) -> Path:
        src = Path(path)
        dest = self.dir / "results" / (as_name or src.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
        self.record("artifact.promoted", {"from": str(src), "to": str(dest)})
        return dest

    def record(self, event: str, payload: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": time.time(), "event": event, "run_id": self.run_id,
                           **{"payload": payload}}, default=str)
        with open(self.ledger_path, "a") as fh:
            fh.write(line + "\n")

    def record_task(self, task: Task) -> None:
        self.record(f"task.{task.status.value}", {
            "task_id": task.id, "step": task.step_id, "capability": task.capability,
            "argv": task.argv, "rc": task.returncode, "duration_s": round(task.duration, 3),
            "error": task.error,
        })

    def ledger(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(l) for l in self.ledger_path.read_text().splitlines() if l.strip()]

    def collect_artifacts(self, task: Task, outputs: dict[str, Any]) -> list[Artifact]:
        arts: list[Artifact] = []
        for name, value in outputs.items():
            for p in (value if isinstance(value, list) else [value]):
                path = Path(p)
                if not path.exists() or not path.is_file():
                    continue
                arts.append(Artifact(
                    id=new_id("art"), run_id=self.run_id, step_id=task.step_id, name=name,
                    path=str(path), sha256=sha256_file(path), bytes=path.stat().st_size,
                ))
        for a in arts:
            self.record("artifact.created", {"name": a.name, "path": a.path,
                                             "sha256": a.sha256, "bytes": a.bytes})
        return arts

    def summary(self) -> dict[str, Any]:
        events = self.ledger()
        tasks = [e for e in events if e["event"].startswith("task.")]
        return {
            "run_id": self.run_id,
            "dir": str(self.dir),
            "events": len(events),
            "tasks_ok": sum(1 for e in tasks if e["event"] == "task.ok"),
            "tasks_failed": sum(1 for e in tasks if e["event"] == "task.failed"),
            "artifacts": sum(1 for e in events if e["event"] == "artifact.created"),
        }

    @staticmethod
    def list_runs(root: str | Path) -> list[dict[str, Any]]:
        root = Path(root).expanduser()
        if not root.exists():
            return []
        out = []
        for d in sorted(root.iterdir(), reverse=True):
            manifest = d / "run.json"
            if manifest.is_file():
                try:
                    out.append(json.loads(manifest.read_text()))
                except json.JSONDecodeError:
                    continue
        return out
