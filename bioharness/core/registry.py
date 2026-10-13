"""Capability registry: load YAML manifests, probe availability, search.

This is the pluggability seam. Dropping a manifest into `capabilities/`
teaches the harness a new tool -- FastQC, GROMACS, AutoDock Vina, a Scanpy
script -- with no code change anywhere else.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .schemas import Capability, Port, Resources, Runtime

try:  # PyYAML is the only non-stdlib dep, and it is optional at import time.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class RegistryError(RuntimeError):
    pass


def _port(raw: dict[str, Any]) -> Port:
    return Port(
        name=raw["name"],
        type=raw.get("type", "file"),
        multiple=bool(raw.get("multiple", False)),
        optional=bool(raw.get("optional", False)),
        default=raw.get("default"),
        glob=raw.get("glob"),
        description=raw.get("description", ""),
    )


def capability_from_dict(raw: dict[str, Any], source: Path | None = None) -> Capability:
    try:
        runtime = Runtime(raw.get("runtime", {}).get("kind", "binary")
                          if isinstance(raw.get("runtime"), dict)
                          else raw.get("runtime", "binary"))
    except ValueError as exc:
        raise RegistryError(f"{source}: unknown runtime kind: {exc}") from exc

    rt = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
    res = raw.get("resources", {}) or {}
    return Capability(
        id=raw["id"],
        summary=raw.get("summary", ""),
        domain=raw.get("domain", "general"),
        version=str(raw.get("version", "unknown")),
        runtime=runtime,
        command=raw.get("command", ""),
        binary=rt.get("binary") or raw.get("binary"),
        image=rt.get("image") or raw.get("image"),
        conda_env=rt.get("conda_env") or raw.get("conda_env"),
        entrypoint=rt.get("entrypoint") or raw.get("entrypoint"),
        inputs=[_port(p) for p in raw.get("inputs", []) or []],
        outputs=[_port(p) for p in raw.get("outputs", []) or []],
        params=raw.get("params", {}) or {},
        resources=Resources(
            cpu=int(res.get("cpu", 1)),
            mem_gb=float(res.get("mem_gb", 2)),
            gpu=int(res.get("gpu", 0)),
            walltime_min=int(res.get("walltime_min", 60)),
        ),
        requires=list(raw.get("requires", []) or []),
        tags=list(raw.get("tags", []) or []),
        doc_url=raw.get("doc_url", ""),
        citation=raw.get("citation", ""),
    )


class Registry:
    """An in-memory catalogue of everything the harness can run."""

    def __init__(self, capabilities: Iterable[Capability] = ()):
        self._caps: dict[str, Capability] = {c.id: c for c in capabilities}
        self._probe_cache: dict[str, tuple[bool, str]] = {}

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(cls, *paths: str | Path) -> "Registry":
        reg = cls()
        for path in paths:
            reg.load_path(Path(path))
        return reg

    def load_path(self, path: Path) -> int:
        if yaml is None:
            raise RegistryError("PyYAML is required to load capability manifests "
                                "(pip install pyyaml)")
        files: list[Path] = []
        if path.is_dir():
            files = sorted(p for p in path.rglob("*.y*ml"))
        elif path.is_file():
            files = [path]
        else:
            raise RegistryError(f"no such capability path: {path}")

        count = 0
        for f in files:
            docs = [d for d in yaml.safe_load_all(f.read_text()) if d]
            for doc in docs:
                for raw in (doc.get("capabilities", [doc]) if isinstance(doc, dict) else doc):
                    cap = capability_from_dict(raw, source=f)
                    if cap.id in self._caps:
                        raise RegistryError(f"duplicate capability id {cap.id!r} in {f}")
                    self._caps[cap.id] = cap
                    count += 1
        return count

    def register(self, cap: Capability, overwrite: bool = False) -> None:
        if cap.id in self._caps and not overwrite:
            raise RegistryError(f"capability {cap.id!r} already registered")
        self._caps[cap.id] = cap

    # -- access ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._caps)

    def __contains__(self, cap_id: object) -> bool:
        return cap_id in self._caps

    def get(self, cap_id: str) -> Capability:
        try:
            return self._caps[cap_id]
        except KeyError:
            near = self.search(cap_id)[:3]
            hint = f" Did you mean: {', '.join(c.id for c in near)}?" if near else ""
            raise RegistryError(f"unknown capability {cap_id!r}.{hint}") from None

    def all(self) -> list[Capability]:
        return sorted(self._caps.values(), key=lambda c: (c.domain, c.id))

    def domains(self) -> list[str]:
        return sorted({c.domain for c in self._caps.values()})

    def search(self, query: str, domain: str | None = None) -> list[Capability]:
        """Lexical scoring -- good enough to let a planner agent pick tools."""
        terms = [t for t in query.lower().replace("-", " ").split() if t]
        hits: list[tuple[int, Capability]] = []
        for cap in self._caps.values():
            if domain and cap.domain != domain:
                continue
            haystack = " ".join([cap.id, cap.summary, cap.domain, *cap.tags]).lower()
            score = sum(3 if t in cap.id.lower() else (1 if t in haystack else 0) for t in terms)
            score += sum(2 for p in (*cap.inputs, *cap.outputs) if p.type in terms)
            if score:
                hits.append((score, cap))
        return [c for _, c in sorted(hits, key=lambda kv: (-kv[0], kv[1].id))]

    def producing(self, type_: str) -> list[Capability]:
        return [c for c in self._caps.values() if any(p.type == type_ for p in c.outputs)]

    def consuming(self, type_: str) -> list[Capability]:
        return [c for c in self._caps.values() if any(p.type == type_ for p in c.inputs)]

    # -- availability ------------------------------------------------------
    def probe(self, cap_id: str, refresh: bool = False) -> tuple[bool, str]:
        """Is this tool actually installed here? Returns (available, reason)."""
        if not refresh and cap_id in self._probe_cache:
            return self._probe_cache[cap_id]
        cap = self.get(cap_id)
        result = _probe_capability(cap)
        self._probe_cache[cap_id] = result
        return result

    def available(self, refresh: bool = False) -> list[Capability]:
        return [c for c in self.all() if self.probe(c.id, refresh)[0]]

    def with_defaults(self, cap_id: str, params: dict[str, Any]) -> dict[str, Any]:
        merged = dict(self.get(cap_id).params)
        merged.update({k: v for k, v in params.items() if v is not None})
        return merged


def _which(name: str | None) -> bool:
    return bool(name) and shutil.which(name) is not None


def _probe_capability(cap: Capability) -> tuple[bool, str]:
    if cap.runtime is Runtime.BINARY:
        target = cap.binary or (cap.command.split() or [""])[0]
        return (True, f"{target} on PATH") if _which(target) else (False, f"{target!r} not on PATH")

    if cap.runtime is Runtime.PYTHON:
        if not cap.entrypoint:
            return False, "no entrypoint declared"
        # Check the entrypoint module and its `requires`, so lazily imported
        # dependencies are probed too.
        for module in [cap.entrypoint.split(":")[0], *cap.requires]:
            try:
                __import__(module)
            except Exception as exc:  # noqa: BLE001 - probes report, never raise
                return False, f"import {module} failed: {exc}"
        deps = f" (+{', '.join(cap.requires)})" if cap.requires else ""
        return True, f"import {cap.entrypoint.split(':')[0]} ok{deps}"

    if cap.runtime is Runtime.CONDA:
        mgr = next((m for m in ("micromamba", "mamba", "conda") if _which(m)), None)
        if not mgr:
            return False, "no conda/mamba/micromamba on PATH"
        if not cap.conda_env:
            return False, "no conda_env declared"
        try:
            envs = subprocess.run([mgr, "env", "list"], capture_output=True,
                                  text=True, timeout=30).stdout
        except Exception as exc:  # noqa: BLE001
            return False, f"{mgr} env list failed: {exc}"
        ok = any(line.split()[:1] == [cap.conda_env] for line in envs.splitlines() if line.strip())
        return (True, f"{mgr} env {cap.conda_env}") if ok else (False, f"env {cap.conda_env!r} not found")

    if cap.runtime in (Runtime.DOCKER, Runtime.APPTAINER):
        engine = "docker" if cap.runtime is Runtime.DOCKER else "apptainer"
        if not _which(engine):
            return False, f"{engine} not on PATH"
        if not cap.image:
            return False, "no image declared"
        return True, f"{engine} available (image pulled on demand)"

    if cap.runtime is Runtime.AGENT:
        provider = cap.binary or "claude"
        return (True, f"{provider} CLI") if _which(provider) else (False, f"agent CLI {provider!r} not on PATH")

    return False, f"unhandled runtime {cap.runtime}"


DEFAULT_SEARCH_PATH = [
    Path(os.environ.get("BIOHARNESS_CAPABILITIES", "capabilities")),
]
