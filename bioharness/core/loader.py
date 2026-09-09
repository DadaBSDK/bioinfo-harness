"""Load workflow definitions from YAML/JSON."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import Step, Workflow

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def workflow_from_dict(raw: dict[str, Any]) -> Workflow:
    steps = []
    for s in raw.get("steps", []) or []:
        if "capability" not in s:
            raise ValueError(f"step {s.get('id', '?')!r} has no `capability`")
        steps.append(Step(
            id=s.get("id") or s["capability"],
            capability=s["capability"],
            with_=s.get("with", s.get("with_", {})) or {},
            params=s.get("params", {}) or {},
            needs=list(s.get("needs", []) or []),
            when=s.get("when"),
            scatter=s.get("scatter"),
            retries=int(s.get("retries", 0)),
            description=s.get("description", ""),
        ))
    return Workflow(name=raw.get("name", "workflow"),
                    description=raw.get("description", ""),
                    params=raw.get("params", {}) or {},
                    steps=steps)


def load_workflow(path: str | Path) -> Workflow:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such workflow file: {p}")
    text = p.read_text()
    if p.suffix == ".json":
        raw = json.loads(text)
    else:
        if yaml is None:
            raise RuntimeError("PyYAML is required to load YAML workflows (pip install pyyaml)")
        raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: workflow file must contain a mapping")
    return workflow_from_dict(raw)


def find_workflow(name: str, search: list[Path] | None = None) -> Path:
    """Resolve a workflow by path, or by bare name within the search path."""
    direct = Path(name).expanduser()
    if direct.is_file():
        return direct
    for root in (search or [Path("workflows")]):
        for ext in (".yaml", ".yml", ".json"):
            candidate = Path(root).expanduser() / f"{name}{ext}"
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(f"workflow {name!r} not found in {search or ['workflows']}")
