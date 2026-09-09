"""Workflow DAG execution and the mission loop.

The orchestrator is the single committer: agents propose, the orchestrator
executes. Nothing else calls the Executor, so two agents can never race on the
same step directory.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from .executor import ExecutionError, Executor
from .guardrails import Guardrails, GuardrailTripped
from .hive import Hive
from .registry import Registry
from .schemas import Status, Step, Task, Workflow, dumps, new_id
from .template import render_value
from .workspace import Workspace


class WorkflowError(RuntimeError):
    pass


def topo_sort(workflow: Workflow) -> list[Step]:
    """Dependency order, with a clear error on cycles and dangling `needs`."""
    ids = {s.id for s in workflow.steps}
    for s in workflow.steps:
        missing = [n for n in s.needs if n not in ids]
        if missing:
            raise WorkflowError(f"step {s.id!r} needs unknown step(s): {', '.join(missing)}")

    ordered: list[Step] = []
    done: set[str] = set()
    remaining = list(workflow.steps)
    while remaining:
        ready = [s for s in remaining if all(n in done for n in s.needs)]
        if not ready:
            stuck = ", ".join(s.id for s in remaining)
            raise WorkflowError(f"dependency cycle among steps: {stuck}")
        ready.sort(key=lambda s: s.id)
        ordered.extend(ready)
        done.update(s.id for s in ready)
        remaining = [s for s in remaining if s.id not in done]
    return ordered


class Orchestrator:
    def __init__(self, registry: Registry, workspace: Workspace,
                 guardrails: Guardrails | None = None, hive: Hive | None = None):
        self.registry = registry
        self.ws = workspace
        self.rails = guardrails or Guardrails()
        self.hive = hive
        self.executor = Executor(registry, workspace, self.rails)
        self.tasks: list[Task] = []

    # -- execution ---------------------------------------------------------
    def run(self, workflow: Workflow, params: dict[str, Any] | None = None,
            stop_on_failure: bool = True) -> dict[str, Any]:
        merged = {**workflow.params, **(params or {})}
        self.ws.create({
            "workflow": workflow.name, "description": workflow.description,
            "params": merged, "dry_run": self.rails.dry_run,
            # The DAG shape is recorded up front so a reader (the UI, an audit)
            # can draw the graph even if the run dies halfway through.
            "graph": [{"id": s.id, "capability": s.capability, "needs": s.needs,
                       "scatter": s.scatter, "description": s.description}
                      for s in workflow.steps],
        })
        self._event("workflow.started", {"workflow": workflow.name, "run_id": self.ws.run_id,
                                         "steps": len(workflow.steps)})

        context: dict[str, Any] = {"params": merged, "steps": {}, "run_id": self.ws.run_id}
        ordered = topo_sort(workflow)
        results: dict[str, Task] = {}
        started = time.time()

        # In a dry run every step is SKIPPED by design, so skipping must not
        # cascade -- otherwise only the first step would ever be resolved.
        blocking = ({Status.FAILED, Status.BLOCKED} if self.rails.dry_run
                    else {Status.FAILED, Status.BLOCKED, Status.SKIPPED})

        for step in ordered:
            upstream_failed = [n for n in step.needs
                               if results.get(n) and results[n].status in blocking]
            if upstream_failed:
                task = self._skipped(step, f"upstream failed: {', '.join(upstream_failed)}")
                results[step.id] = task
                self.tasks.append(task)
                continue

            if step.when is not None and not self._guard_passes(step.when, context):
                task = self._skipped(step, f"guard `{step.when}` not satisfied")
                results[step.id] = task
                self.tasks.append(task)
                continue

            try:
                bound = self._bind_step(step, context)
            except (WorkflowError, ExecutionError) as exc:
                task = self._failed(step, str(exc))
                results[step.id] = task
                self.tasks.append(task)
                if stop_on_failure:
                    self._event("workflow.halted", {"step": step.id, "error": str(exc)})
                    break
                continue

            for task in bound:
                self.tasks.append(task)
                results[task.step_id] = self._execute(task, step)

            self._record_outputs(step, results, context)

            final = results.get(step.id)
            if stop_on_failure and final and final.status in (Status.FAILED, Status.BLOCKED):
                self._event("workflow.halted", {"step": step.id, "error": final.error})
                break

        summary = self._summarize(workflow, results, time.time() - started)
        (self.ws.dir / "summary.json").write_text(dumps(summary))
        self._event("workflow.finished", summary)
        return summary

    def _bind_step(self, step: Step, context: dict[str, Any]) -> list[Task]:
        """Resolve bindings, expanding a `scatter` port into one task per item."""
        cap = self.registry.get(step.capability)
        try:
            inputs = render_value(step.with_, context)
            params = render_value(step.params, context)
        except KeyError as exc:
            raise WorkflowError(
                f"step {step.id!r}: cannot resolve {exc.args[0]!r}. "
                f"Available params: {sorted(context['params'])}; "
                f"completed steps: {sorted(context['steps'])}") from None

        if not step.scatter:
            return [Task(id=new_id("task"), run_id=self.ws.run_id, step_id=step.id,
                         capability=cap.id, inputs=dict(inputs), params=dict(params))]

        items = inputs.get(step.scatter)
        if not isinstance(items, list):
            items = [items]
        tasks = []
        for i, item in enumerate(items):
            shard = {**inputs, step.scatter: item}
            tasks.append(Task(id=new_id("task"), run_id=self.ws.run_id,
                              step_id=f"{step.id}.{i}", capability=cap.id,
                              inputs=shard, params=dict(params)))
        return tasks

    def _execute(self, task: Task, step: Step) -> Task:
        attempts = step.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                self.executor.run(task)
            except (ExecutionError, GuardrailTripped) as exc:
                task.status, task.error = Status.FAILED, str(exc)
                self.ws.record_task(task)
            if task.status is not Status.FAILED or attempt == attempts:
                break
            self._event("task.retry", {"step": task.step_id, "attempt": attempt,
                                       "error": task.error})
            task.status, task.error, task.returncode = Status.PENDING, "", None
        return task

    def _record_outputs(self, step: Step, results: dict[str, Task],
                        context: dict[str, Any]) -> None:
        shards = [t for sid, t in results.items()
                  if sid == step.id or sid.startswith(f"{step.id}.")]
        if not shards:
            return
        if len(shards) == 1 and shards[0].step_id == step.id:
            entry = {"outputs": shards[0].outputs, "status": shards[0].status.value,
                     "workdir": shards[0].workdir}
        else:
            gathered: dict[str, list[Any]] = {}
            for t in shards:
                for name, value in t.outputs.items():
                    gathered.setdefault(name, []).extend(
                        value if isinstance(value, list) else [value])
            entry = {"outputs": gathered,
                     "status": ("ok" if all(t.status is Status.OK for t in shards) else "failed"),
                     "shards": len(shards)}
            results[step.id] = shards[0]
        context["steps"][step.id] = entry

    def _skipped(self, step: Step, reason: str) -> Task:
        task = Task(id=new_id("task"), run_id=self.ws.run_id, step_id=step.id,
                    capability=step.capability, status=Status.SKIPPED, error=reason)
        self.ws.record_task(task)
        return task

    def _failed(self, step: Step, reason: str) -> Task:
        task = Task(id=new_id("task"), run_id=self.ws.run_id, step_id=step.id,
                    capability=step.capability, status=Status.FAILED, error=reason)
        self.ws.record_task(task)
        return task

    def _guard_passes(self, expr: str, context: dict[str, Any]) -> bool:
        """`when:` supports `a == b`, `a != b`, or plain truthiness of a token.
        Intentionally not eval() -- a workflow file is data, not code."""
        rendered = render_value(expr, context) if "{{" in expr else expr
        if isinstance(rendered, bool):
            return rendered
        text = str(rendered).strip()
        for op in ("==", "!="):
            if op in text:
                left, right = (part.strip().strip("'\"") for part in text.split(op, 1))
                return (left == right) if op == "==" else (left != right)
        return text.lower() not in ("", "false", "none", "0", "no")

    def _summarize(self, workflow: Workflow, results: dict[str, Task],
                   seconds: float) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for t in self.tasks:
            by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
        outputs = {sid: t.outputs for sid, t in results.items() if t.outputs}
        return {
            "run_id": self.ws.run_id,
            "workflow": workflow.name,
            "dir": str(self.ws.dir),
            "seconds": round(seconds, 2),
            "dry_run": self.rails.dry_run,
            "status": ("ok" if all(t.status in (Status.OK, Status.SKIPPED)
                                   for t in self.tasks) else "failed"),
            "by_status": by_status,
            "outputs": outputs,
            "guardrails": self.rails.status(),
            "tasks": [{"step": t.step_id, "capability": t.capability,
                       "status": t.status.value, "seconds": round(t.duration, 2),
                       "params": t.params, "error": t.error,
                       "command": t.rendered} for t in self.tasks],
        }

    def _event(self, kind: str, payload: dict[str, Any]) -> None:
        self.ws.record(kind, payload)
        if self.hive:
            self.hive.event(kind, payload)


class Mission:
    """Drives the agent roster until the hive goes quiet.

    Agents talk by mailbox; this loop routes and ticks them, bounded by the same
    guardrails as tool execution so a chatty agent pair cannot spin forever.
    """

    def __init__(self, hive: Hive, agents: Iterable[Any], guardrails: Guardrails | None = None,
                 max_rounds: int = 25):
        self.hive = hive
        self.agents = list(agents)
        self.rails = guardrails or Guardrails()
        self.max_rounds = max_rounds

    def kickoff(self, to: str, topic: str, **body: Any) -> None:
        self.hive.send("human", to, topic, body)

    def run(self) -> dict[str, Any]:
        self.hive.enroll("human", "operator")
        rounds, handled_total = 0, 0
        while rounds < self.max_rounds:
            rounds += 1
            self.hive.route()
            handled = sum(agent.tick() for agent in self.agents)
            handled_total += handled
            if handled == 0:
                break
            try:
                self.rails.budget.check()
            except GuardrailTripped as exc:
                self.hive.event("mission.halted", {"reason": str(exc), "round": rounds})
                break
        self.hive.route()
        return {"rounds": rounds, "messages_handled": handled_total,
                "inbox": {a.name: a.mailbox.pending() for a in self.agents},
                "blackboard": self.hive.blackboard.keys(),
                "cost": self.hive.cost_ledger()}
