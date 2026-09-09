"""Executes one bound Task, whatever runtime it declares.

The executor is the only place in the harness that spawns a process. Everything
else -- planner, agents, orchestrator -- produces Tasks and reads Artifacts.
"""
from __future__ import annotations

import importlib
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .guardrails import GuardrailTripped, Guardrails
from .registry import Registry
from .schemas import Capability, Runtime, Status, Task
from .template import missing_tokens, render, render_value
from .workspace import Workspace


class ExecutionError(RuntimeError):
    pass


class Executor:
    def __init__(self, registry: Registry, workspace: Workspace,
                 guardrails: Guardrails | None = None, env: dict[str, str] | None = None):
        self.registry = registry
        self.ws = workspace
        self.rails = guardrails or Guardrails()
        self.env = {**os.environ, **(env or {})}

    # -- binding -----------------------------------------------------------
    def bind(self, task: Task) -> Task:
        """Resolve the command template against inputs/params and validate ports."""
        cap = self.registry.get(task.capability)
        workdir = Path(task.workdir) if task.workdir else self.ws.step_dir(task.step_id)
        workdir.mkdir(parents=True, exist_ok=True)
        task.workdir = str(workdir)

        params = self.registry.with_defaults(cap.id, task.params)
        inputs = dict(task.inputs)

        for port in cap.inputs:
            if port.name not in inputs or inputs[port.name] is None:
                if port.default is not None:
                    inputs[port.name] = port.default
                elif not port.optional:
                    raise ExecutionError(
                        f"step {task.step_id!r}: capability {cap.id!r} requires input "
                        f"{port.name!r} ({port.type})")
            value = inputs.get(port.name)
            if port.multiple and value is not None and not isinstance(value, list):
                inputs[port.name] = [value]

        context: dict[str, Any] = {
            "inputs": inputs,
            "params": params,
            "outdir": str(workdir),
            "workdir": str(workdir),
            "run_id": self.ws.run_id,
            "step": task.step_id,
            "cpu": cap.resources.cpu,
            "threads": params.get("threads", cap.resources.cpu),
        }

        task.inputs, task.params = inputs, params

        if cap.runtime is Runtime.PYTHON:
            task.rendered = f"python:{cap.entrypoint}"
            task.argv = []
            return task

        missing = missing_tokens(cap.command, context)
        if missing:
            raise ExecutionError(
                f"step {task.step_id!r}: unresolved template values {missing} "
                f"in capability {cap.id!r}")

        task.rendered = render(cap.command, context, quote=True).strip()
        task.argv = self._wrap_runtime(cap, task, shlex.split(task.rendered))
        return task

    def _wrap_runtime(self, cap: Capability, task: Task, argv: list[str]) -> list[str]:
        if cap.runtime is Runtime.BINARY:
            return argv
        if cap.runtime is Runtime.AGENT:
            return argv
        if cap.runtime is Runtime.CONDA:
            mgr = next((m for m in ("micromamba", "mamba", "conda")
                        if _which(m)), "conda")
            return [mgr, "run", "-n", cap.conda_env or "base", *argv]
        if cap.runtime is Runtime.DOCKER:
            mounts = self._mounts(task)
            flags = ["--gpus", "all"] if cap.resources.gpu else []
            if not self.rails.allow_network:
                flags += ["--network", "none"]
            return ["docker", "run", "--rm", *flags,
                    "-u", f"{os.getuid()}:{os.getgid()}",
                    *mounts, "-w", task.workdir, cap.image or "", *argv]
        if cap.runtime is Runtime.APPTAINER:
            binds = []
            for host in self._bind_paths(task):
                binds += ["--bind", f"{host}:{host}"]
            gpu = ["--nv"] if cap.resources.gpu else []
            return ["apptainer", "exec", *gpu, *binds, "--pwd", task.workdir,
                    cap.image or "", *argv]
        raise ExecutionError(f"cannot build argv for runtime {cap.runtime}")

    def _bind_paths(self, task: Task) -> list[str]:
        paths = {str(self.ws.dir), task.workdir}
        for value in task.inputs.values():
            for v in (value if isinstance(value, list) else [value]):
                p = Path(str(v))
                if p.exists():
                    paths.add(str(p.resolve().parent))
        return sorted(paths)

    def _mounts(self, task: Task) -> list[str]:
        out: list[str] = []
        for host in self._bind_paths(task):
            out += ["-v", f"{host}:{host}"]
        return out

    # -- running -----------------------------------------------------------
    def run(self, task: Task) -> Task:
        cap = self.registry.get(task.capability)
        self.bind(task)

        try:
            self.rails.before_task(cap.id)
        except GuardrailTripped as exc:
            task.status, task.error = Status.BLOCKED, str(exc)
            self.ws.record_task(task)
            return task

        if self.rails.dry_run:
            task.status = Status.SKIPPED
            task.error = "dry-run"
            # Predict what this step would emit so downstream templates still
            # resolve -- a dry run is only useful if it validates the whole DAG.
            task.outputs = self.predict(cap, task)
            self.ws.record("task.dry_run", {"step": task.step_id, "capability": cap.id,
                                            "argv": task.argv or task.rendered,
                                            "would_produce": task.outputs})
            return task

        available, reason = self.registry.probe(cap.id)
        if not available:
            task.status, task.error = Status.BLOCKED, f"capability unavailable: {reason}"
            self.rails.after_task(cap.id, ok=False)
            self.ws.record_task(task)
            return task

        workdir = Path(task.workdir)
        stdout_p, stderr_p = workdir / "stdout.log", workdir / "stderr.log"
        task.stdout_path, task.stderr_path = str(stdout_p), str(stderr_p)
        task.status = Status.RUNNING
        task.started = time.time()

        try:
            if cap.runtime is Runtime.PYTHON:
                self._run_python(cap, task, stdout_p)
            else:
                with open(stdout_p, "w") as so, open(stderr_p, "w") as se:
                    proc = subprocess.run(
                        task.argv, cwd=workdir, env=self.env, stdout=so, stderr=se,
                        timeout=cap.resources.walltime_min * 60, check=False)
                task.returncode = proc.returncode
                if proc.returncode != 0:
                    tail = _tail(stderr_p)
                    raise ExecutionError(
                        f"{cap.id} exited {proc.returncode}"
                        + (f"\n--- stderr tail ---\n{tail}" if tail else ""))
            task.status = Status.OK
        except subprocess.TimeoutExpired:
            task.status = Status.FAILED
            task.error = f"timed out after {cap.resources.walltime_min} min"
        except Exception as exc:  # noqa: BLE001 - a failing tool is data, not a crash
            task.status = Status.FAILED
            task.error = str(exc)
        finally:
            task.finished = time.time()

        if task.status is Status.OK:
            task.outputs = self.collect(cap, task)
            self.ws.collect_artifacts(task, task.outputs)

        self.rails.after_task(cap.id, ok=task.status is Status.OK, seconds=task.duration,
                              cpu=cap.resources.cpu, gpu=cap.resources.gpu)
        self.ws.record_task(task)
        return task

    def _run_python(self, cap: Capability, task: Task, stdout_p: Path) -> None:
        module_name, _, func_name = (cap.entrypoint or "").partition(":")
        if not module_name or not func_name:
            raise ExecutionError(f"{cap.id}: entrypoint must be 'module:function'")
        fn = getattr(importlib.import_module(module_name), func_name)
        result = fn(inputs=task.inputs, params=task.params, workdir=Path(task.workdir))
        task.returncode = 0
        if isinstance(result, dict):
            task.outputs.update({k: str(v) for k, v in result.items()})
        stdout_p.write_text(str(result) if result is not None else "")

    def predict(self, cap: Capability, task: Task) -> dict[str, Any]:
        """The paths a step would produce, without running it."""
        workdir = Path(task.workdir)
        context = {"inputs": task.inputs, "params": task.params, "outdir": str(workdir),
                   "step": task.step_id, "run_id": self.ws.run_id}
        out: dict[str, Any] = {}
        for port in cap.outputs:
            pattern = str(render_value(port.glob or port.name, context))
            # A glob is a prediction, not a path; strip wildcards to a plausible name.
            name = pattern.replace("*", f"{task.step_id}").replace("?", "_")
            out[port.name] = [str(workdir / name)] if port.multiple else str(workdir / name)
        return out

    def collect(self, cap: Capability, task: Task) -> dict[str, Any]:
        """Gather declared outputs from the step workspace."""
        workdir = Path(task.workdir)
        found: dict[str, Any] = dict(task.outputs)
        context = {"inputs": task.inputs, "params": task.params,
                   "outdir": str(workdir), "step": task.step_id, "run_id": self.ws.run_id}
        for port in cap.outputs:
            if port.name in found:
                continue
            pattern = render_value(port.glob or port.name, context)
            matches = sorted(str(p) for p in workdir.glob(str(pattern)))
            if not matches:
                if not port.optional:
                    task.error = (task.error + " ; " if task.error else "") + \
                        f"expected output {port.name!r} matching {pattern!r} not produced"
                continue
            found[port.name] = matches if port.multiple else matches[0]
        return found


def _which(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
