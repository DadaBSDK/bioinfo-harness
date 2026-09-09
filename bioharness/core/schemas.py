"""Core data model for the Bioinfo Harness.

Everything the harness knows about the world is one of these:
a Capability (a tool it can run), a Workflow (a DAG of Steps),
a Task (one Step bound to concrete inputs), or a Message (agent chatter).
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


class Runtime(str, Enum):
    """How a capability's command is actually executed."""

    BINARY = "binary"        # already on PATH
    CONDA = "conda"          # conda/micromamba env
    DOCKER = "docker"        # container image
    APPTAINER = "apptainer"  # HPC-friendly container
    PYTHON = "python"        # in-process python entrypoint
    AGENT = "agent"          # delegate to an LLM agent CLI


class Status(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass
class Port:
    """An input or output slot on a capability."""

    name: str
    type: str = "file"          # fastq | bam | vcf | pdb | trajectory | table | dir | scalar ...
    multiple: bool = False
    optional: bool = False
    default: Any = None
    glob: str | None = None     # outputs: how to collect from the workspace
    description: str = ""


@dataclass
class Resources:
    cpu: int = 1
    mem_gb: float = 2.0
    gpu: int = 0
    walltime_min: int = 60


@dataclass
class Capability:
    """A registered tool. NGS aligner, MD engine, docking program, python function --
    the harness does not care which, only that it declares its ports and runtime."""

    id: str
    summary: str = ""
    domain: str = "general"          # ngs | structural | singlecell | cheminfo | general
    version: str = "unknown"
    runtime: Runtime = Runtime.BINARY
    command: str = ""                # templated; {{ inputs.x }}, {{ params.y }}, {{ outdir }}
    binary: str | None = None
    image: str | None = None
    conda_env: str | None = None
    entrypoint: str | None = None    # PYTHON runtime: "module.path:function"
    inputs: list[Port] = field(default_factory=list)
    outputs: list[Port] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    resources: Resources = field(default_factory=Resources)
    requires: list[str] = field(default_factory=list)  # python modules that must import
    tags: list[str] = field(default_factory=list)
    doc_url: str = ""
    citation: str = ""               # so the scribe can write a Methods section

    def port(self, name: str) -> Port | None:
        for p in (*self.inputs, *self.outputs):
            if p.name == name:
                return p
        return None


@dataclass
class Step:
    """A node in a workflow DAG."""

    id: str
    capability: str
    with_: dict[str, Any] = field(default_factory=dict)   # input bindings (templated)
    params: dict[str, Any] = field(default_factory=dict)
    needs: list[str] = field(default_factory=list)
    when: str | None = None        # optional guard expression over the context
    scatter: str | None = None     # input name to fan out over
    retries: int = 0
    description: str = ""


@dataclass
class Workflow:
    name: str
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)

    def step(self, sid: str) -> Step | None:
        return next((s for s in self.steps if s.id == sid), None)


@dataclass
class Task:
    """A Step bound to concrete values, ready to execute."""

    id: str
    run_id: str
    step_id: str
    capability: str
    inputs: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    workdir: str = ""
    status: Status = Status.PENDING
    argv: list[str] = field(default_factory=list)
    rendered: str = ""
    returncode: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    started: float | None = None
    finished: float | None = None
    error: str = ""

    @property
    def duration(self) -> float:
        if self.started and self.finished:
            return self.finished - self.started
        return 0.0


@dataclass
class Message:
    """Agent-to-agent traffic. Transport is pluggable; the envelope is not."""

    id: str
    sender: str
    recipient: str          # agent name, or "*" to broadcast
    topic: str
    body: dict[str, Any] = field(default_factory=dict)
    thread: str | None = None
    ts: float = field(default_factory=time.time)


@dataclass
class Artifact:
    id: str
    run_id: str
    step_id: str
    name: str
    path: str
    type: str = "file"
    sha256: str = ""
    bytes: int = 0


def to_jsonable(obj: Any) -> Any:
    """asdict() but Enum-safe, for provenance writing."""
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def dumps(obj: Any, indent: int = 2) -> str:
    return json.dumps(to_jsonable(obj), indent=indent, default=str)
