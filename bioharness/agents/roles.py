"""The built-in agent roster.

A wet-lab group, roughly: a PI who decides what happens, a data curator who
refuses to let bad inputs in, a planner who designs the pipeline, a runner who
executes it, a validator who enforces QC gates, an analyst who interprets the
numbers, and a scribe who writes the Methods section.

Every one of them works for NGS and for structural simulation alike, because
none of them hardcodes a tool -- they reason over the capability registry.
"""
from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

from ..core.guardrails import Autonomy
from ..core.registry import Registry
from ..core.orchestrator import Orchestrator, topo_sort
from ..core.schemas import Message, Step, Workflow
from ..core.workspace import Workspace
from .base import Agent, ProviderUnavailable


# --------------------------------------------------------------------------
# Type-graph planning (no LLM required).
# --------------------------------------------------------------------------
def plan_by_types(registry: Registry, start_type: str, goal_type: str,
                  domain: str | None = None, max_depth: int = 8) -> list[str]:
    """Shortest chain of capabilities carrying `start_type` to `goal_type`.

    fastq -> bam -> vcf falls out of this for NGS; pdb -> topology -> trajectory
    falls out for MD. The graph is whatever manifests are installed.
    """
    frontier: deque[tuple[str, list[str]]] = deque([(start_type, [])])
    seen = {start_type}
    while frontier:
        current, chain = frontier.popleft()
        if len(chain) >= max_depth:
            continue
        for cap in registry.consuming(current):
            if domain and cap.domain not in (domain, "general"):
                continue
            if cap.id in chain:
                continue
            for out in cap.outputs:
                if out.type == goal_type:
                    return [*chain, cap.id]
                if out.type not in seen:
                    seen.add(out.type)
                    frontier.append((out.type, [*chain, cap.id]))
    return []


def source_for(registry: Registry, data_type: str,
               domain: str | None = None) -> str | None:
    """A capability that produces `data_type` from parameters alone.

    Used to start a plan with a download step when the input data is not on disk.
    """
    for cap in registry.all():
        if any(not port.optional for port in cap.inputs):
            continue
        if domain and cap.domain not in (domain, "general"):
            continue
        if any(out.type == data_type for out in cap.outputs):
            return cap.id
    return None


# key=value parameters in an order, e.g. `accession=SRR000001`. Quoted values
# may contain spaces.
ORDER_PARAM = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]+\"|'[^']+'|\S+)")


def params_in(text: str) -> dict[str, Any]:
    """key=value parameters written in the order text."""
    out: dict[str, Any] = {}
    for name, value in ORDER_PARAM.findall(text or ""):
        out[name] = value.strip("\"'")
    return out


def repair_plan(registry: Registry, wf: dict[str, Any]) -> dict[str, Any]:
    """Fix common model mistakes in a plan, checked against the registry.

    Mainly: a capability parameter (e.g. `accession`) bound as though it were an
    input port.
    """
    for step in wf.get("steps", []) or []:
        try:
            cap = registry.get(step.get("capability", ""))
        except Exception:  # noqa: BLE001 - unknown capability, handled later
            continue
        ports = {port.name for port in cap.inputs}
        declared = set(cap.params or {})
        moved = {}
        for name in list((step.get("with") or {})):
            if name not in ports and name in declared:
                moved[name] = step["with"].pop(name)
        if moved:
            step.setdefault("params", {}).update(moved)
    return wf


def file_params(registry: Registry, wf: dict[str, Any]) -> set[str]:
    """Workflow parameters that really do name a file.

    Read off the capability's input ports rather than off the template, so a
    plan that mislabels a parameter cannot turn it into a path.
    """
    out: set[str] = set()
    for step in wf.get("steps", []) or []:
        try:
            cap = registry.get(step.get("capability", ""))
        except Exception:  # noqa: BLE001
            continue
        ports = {port.name for port in cap.inputs}
        for name, binding in (step.get("with") or {}).items():
            if name in ports:
                out.update(PARAM_REF.findall(str(binding)))
    return out


# Words in a goal meaning the input data must be downloaded first.
FETCH_WORDS = ("fetch", "download", "pull", "retrieve", "accession", "online",
               "from ena", "from sra", "public")


class Planner(Agent):
    """You design bioinformatics workflows. You are given a goal and a catalogue
    of installed capabilities. You select the minimum set of steps that reaches
    the goal, wire each step's inputs to earlier steps' outputs, and you never
    invent a capability that is not in the catalogue."""

    role = "planner"
    # Also accept `goal`, so a human can message the planner directly.
    subscribes = ("plan.request", "goal")
    default_autonomy = Autonomy.SUGGEST

    def handle(self, msg: Message) -> dict[str, Any]:
        goal = msg.body.get("goal", "")
        start_type = msg.body.get("start_type")
        goal_type = msg.body.get("goal_type")
        domain = msg.body.get("domain")

        wf = None
        if start_type and goal_type:
            chain = plan_by_types(self.ctx.registry, start_type, goal_type, domain)
            if chain:
                # A fetch request must start the chain with a fetch capability.
                if any(word in goal.lower() for word in FETCH_WORDS):
                    src = source_for(self.ctx.registry, start_type, domain)
                    if src and src not in chain:
                        chain = [src, *chain]
                wf = self._linear_workflow(goal or f"{start_type}-to-{goal_type}", chain)

        if wf is None and self.provider is not None:
            wf = self._llm_workflow(goal, domain)
            if wf:
                wf = repair_plan(self.ctx.registry, wf)

        if wf is None:
            candidates = [c.id for c in self.ctx.registry.search(goal, domain)[:8]]
            return {"ok": False, "reason": "no route found",
                    "goal": goal, "candidates": candidates}

        self.publish(f"plan/{wf['name']}", wf)
        self.remember(f"Planned `{wf['name']}` for goal: {goal}\nSteps: "
                      + " -> ".join(s["capability"] for s in wf["steps"]),
                      heading=f"plan {wf['name']}")

        # Ask the curator to check the plan's inputs before it is queued.
        chain = " -> ".join(s["capability"] for s in wf["steps"])
        self.ask_peer("curator",
                      f"I plan to run: {chain}. Which of these inputs would you "
                      f"refuse to let through, and what should each be bound to?",
                      workflow=wf["name"], steps=chain,
                      unbound_params=sorted(wf.get("params", {})))
        return {"ok": True, "workflow": wf}

    def _linear_workflow(self, name: str, chain: list[str]) -> dict[str, Any]:
        """Wire a capability chain into a runnable workflow.

        The port carrying data from the previous step is bound to that step's
        output; every *other* required port becomes a run parameter, so the
        emitted workflow is complete rather than half-bound.
        """
        steps: list[dict[str, Any]] = []
        params: dict[str, Any] = {}
        prev: str | None = None
        prev_types: dict[str, str] = {}

        for cap_id in chain:
            cap = self.ctx.registry.get(cap_id)
            required = [p for p in cap.inputs if not p.optional]
            carried = next((p for p in required if p.type in prev_types), None) if prev else None

            binding: dict[str, Any] = {}
            for port in required:
                if port is carried and prev:
                    binding[port.name] = f"{{{{ steps.{prev}.outputs.{prev_types[port.type]} }}}}"
                else:
                    binding[port.name] = f"{{{{ params.{port.name} }}}}"
                    params.setdefault(port.name, None)

            # Parameters with no usable default (accession, URL) become workflow
            # parameters so the operator is asked for them.
            step_params: dict[str, Any] = {}
            for name, default in (cap.params or {}).items():
                if default in ("", None):
                    step_params[name] = f"{{{{ params.{name} }}}}"
                    params.setdefault(name, None)

            steps.append({"id": cap_id, "capability": cap_id, "with": binding,
                          "params": step_params,
                          "needs": [prev] if prev else [],
                          "description": cap.summary})
            prev = cap_id
            prev_types = {p.type: p.name for p in cap.outputs}

        return {"name": _slug(name), "description": f"Auto-planned: {name}",
                "params": params, "steps": steps}

    def _first_output(self, cap_id: str | None) -> str:
        if not cap_id:
            return "out"
        outs = self.ctx.registry.get(cap_id).outputs
        return outs[0].name if outs else "out"

    def _validate_workflow(self, raw: Any) -> dict[str, Any] | None:
        """Clean and check a model-authored workflow before it is published.

        Small models get the shape roughly right but the wiring wrong: `needs`
        comes back holding template strings or port names instead of step ids,
        and capabilities are occasionally invented. Drop what cannot be true,
        then require that what survives is a runnable DAG.
        """
        if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
            return None

        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, step in enumerate(raw["steps"]):
            if not isinstance(step, dict):
                continue
            cap = step.get("capability")
            if cap not in self.ctx.registry:
                continue                       # invented tool
            sid = str(step.get("id") or f"step{i + 1}")
            if sid in seen:
                sid = f"{sid}_{i}"
            # Keep only dependencies on steps already accepted.
            needs = [n for n in (step.get("needs") or []) if n in seen]
            cleaned.append({"id": sid, "capability": cap,
                            "with": step.get("with") or {},
                            "params": step.get("params") or {},
                            "needs": needs,
                            "description": self.ctx.registry.get(cap).summary})
            seen.add(sid)

        if not cleaned:
            return None

        wf = {"name": str(raw.get("name") or "planned-workflow"),
              "description": str(raw.get("description") or ""),
              "params": raw.get("params") or {}, "steps": cleaned}
        try:
            topo_sort(workflow_from_dict(wf))
        except Exception:  # noqa: BLE001 - unusable plan
            return None
        return wf

    def _llm_workflow(self, goal: str, domain: str | None) -> dict[str, Any] | None:
        catalogue = "\n".join(
            f"- {c.id} [{c.domain}] {c.summary} | in: "
            f"{', '.join(f'{p.name}:{p.type}' for p in c.inputs)} | out: "
            f"{', '.join(f'{p.name}:{p.type}' for p in c.outputs)}"
            for c in self.ctx.registry.all()
            if not domain or c.domain in (domain, "general"))
        schema = ('{"name": str, "description": str, "steps": '
                  '[{"id": str, "capability": str, "with": {port: value}, "needs": [str]}]}')
        # JSON schema for the plan; enforced where the provider supports it.
        wf_schema = {
            "type": "object",
            "required": ["name", "steps"],
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "steps": {"type": "array", "items": {
                    "type": "object",
                    "required": ["id", "capability"],
                    "properties": {
                        "id": {"type": "string"},
                        "capability": {"type": "string"},
                        "with": {"type": "object"},
                        "needs": {"type": "array", "items": {"type": "string"}},
                    }}},
            },
        }
        try:
            raw = self.think_json(
                f"Goal: {goal}\n\nProduce a workflow matching this schema:\n{schema}\n"
                f"`needs` must contain ONLY ids of earlier steps in this same list. "
                f"Use only capabilities from the catalogue. Reference earlier outputs "
                f"as {{{{ steps.<id>.outputs.<port> }}}} and run parameters as "
                f"{{{{ params.<name> }}}}.",
                context=f"# Installed capabilities\n{catalogue}",
                schema=wf_schema)
        except (ProviderUnavailable, ValueError):
            return None
        wf = self._validate_workflow(raw)
        if wf is None:
            self.hive.event("plan.rejected",
                            {"agent": self.name, "goal": goal,
                             "reason": "model workflow failed validation"})
        return wf


class Curator(Agent):
    """You are the data steward. You verify that every declared input exists, is
    non-empty, and matches the port type it is bound to. You block the run rather
    than let a pipeline burn compute on a missing reference genome."""

    role = "curator"
    subscribes = ("data.validate",)
    default_autonomy = Autonomy.BOUNDED

    EXTENSIONS: dict[str, tuple[str, ...]] = {
        "fastq": (".fastq", ".fq", ".fastq.gz", ".fq.gz"),
        "bam": (".bam",), "sam": (".sam",), "cram": (".cram",),
        "vcf": (".vcf", ".vcf.gz", ".bcf"),
        "fasta": (".fa", ".fasta", ".fna", ".fa.gz", ".fasta.gz"),
        "pdb": (".pdb", ".cif", ".mmcif", ".pdbqt"),
        "trajectory": (".xtc", ".trr", ".dcd", ".nc"),
        "topology": (".top", ".psf", ".prmtop", ".parm7"),
        "table": (".csv", ".tsv", ".txt", ".h5ad", ".parquet"),
    }

    def handle(self, msg: Message) -> dict[str, Any]:
        bindings: dict[str, Any] = msg.body.get("inputs", {}) or {}
        types: dict[str, str] = msg.body.get("types", {}) or {}
        problems: list[str] = []
        checked: list[str] = []

        for name, value in bindings.items():
            for item in (value if isinstance(value, list) else [value]):
                if not isinstance(item, (str, Path)) or "{{" in str(item):
                    continue  # unresolved template, bound by the orchestrator
                path = Path(str(item)).expanduser()
                if not path.exists():
                    problems.append(f"{name}: missing path {path}")
                    continue
                if path.is_file() and path.stat().st_size == 0:
                    problems.append(f"{name}: empty file {path}")
                expected = types.get(name)
                exts = self.EXTENSIONS.get(expected or "", ())
                if exts and not str(path).lower().endswith(exts):
                    problems.append(
                        f"{name}: {path.name} does not look like {expected} "
                        f"(expected one of {', '.join(exts)})")
                checked.append(str(path))

        ok = not problems
        self.publish("curation/last", {"ok": ok, "problems": problems, "checked": checked})
        if not ok:
            self.remember("Blocked a run:\n" + "\n".join(f"- {p}" for p in problems),
                          heading="curation block")
        return {"ok": ok, "problems": problems, "checked": len(checked)}


class Validator(Agent):
    """You enforce QC gates. You compare measured metrics against thresholds and
    return a pass/fail verdict with the specific metrics that failed. You do not
    soften a failure because the run was expensive."""

    role = "validator"
    subscribes = ("qc.check",)
    default_autonomy = Autonomy.BOUNDED

    # (metric, comparison, default threshold); overridable per message.
    GATES: dict[str, tuple[str, float]] = {
        "mean_quality": (">=", 28.0),
        "percent_duplicates": ("<=", 30.0),
        "percent_mapped": (">=", 85.0),
        "coverage_mean": (">=", 20.0),
        "rmsd_nm": ("<=", 0.5),
        "final_energy_kj": ("<=", 0.0),
    }

    def handle(self, msg: Message) -> dict[str, Any]:
        metrics: dict[str, float] = msg.body.get("metrics", {}) or {}
        gates = {**self.GATES, **(msg.body.get("gates", {}) or {})}
        failures, passes = [], []
        for name, value in metrics.items():
            gate = gates.get(name)
            if not gate:
                continue
            op, threshold = (gate if isinstance(gate, (list, tuple)) else (">=", gate))
            ok = value >= threshold if op == ">=" else value <= threshold
            (passes if ok else failures).append(
                {"metric": name, "value": value, "op": op, "threshold": threshold})
        verdict = "pass" if not failures else "fail"
        self.publish("qc/last", {"verdict": verdict, "failures": failures,
                                 "passes": passes, "step": msg.body.get("step")})
        if failures:
            # Gate failures are sent to the analyst for interpretation.
            self.ask_peer("analyst",
                          "These QC gates failed. Which technical artefact most "
                          "likely explains them, and is it worth a rerun?",
                          failures=failures, step=msg.body.get("step"))
        return {"verdict": verdict, "failures": failures, "checked": len(passes) + len(failures)}


class Analyst(Agent):
    """You interpret bioinformatics results. You state what the numbers show,
    what they do not show, and the single most likely artefact that could explain
    the result. You never claim significance the data does not support."""

    role = "analyst"
    subscribes = ("analyze",)
    default_autonomy = Autonomy.SUGGEST

    def handle(self, msg: Message) -> dict[str, Any]:
        artifacts = msg.body.get("artifacts", {}) or {}
        metrics = msg.body.get("metrics", {}) or {}
        digest = _digest_artifacts(artifacts)
        if self.provider is None:
            return {"ok": True, "mode": "descriptive",
                    "summary": _descriptive_summary(artifacts, metrics), "digest": digest}
        try:
            text = self.think(
                "Interpret these results. Give: (1) what the data show, (2) the main "
                "caveat, (3) the single most likely technical artefact.",
                context=json.dumps({"metrics": metrics, "artifacts": digest},
                                   indent=2, default=str)[:12000])
        except ProviderUnavailable as exc:
            # Without an LLM, fall back to a metrics-only analysis.
            return {"ok": True, "mode": "descriptive", "provider_error": str(exc),
                    "summary": _descriptive_summary(artifacts, metrics), "digest": digest}
        self.remember(text[:2000], heading=f"analysis {msg.body.get('run_id', '')}")
        return {"ok": True, "mode": "llm", "summary": text}


class Scribe(Agent):
    """You write the Methods section. You state tool names, versions and the exact
    parameters actually used, cite each tool, and write nothing that the provenance
    ledger does not support."""

    role = "scribe"
    subscribes = ("report",)
    default_autonomy = Autonomy.SUGGEST

    def handle(self, msg: Message) -> dict[str, Any]:
        tasks = msg.body.get("tasks", []) or []
        run_id = msg.body.get("run_id", "")
        methods = self.methods_section(tasks)
        out = Path(msg.body.get("outdir", self.workdir)) / f"methods-{run_id or 'run'}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(methods)
        self.publish(f"report/{run_id}", {"methods_path": str(out)})
        return {"ok": True, "methods_path": str(out), "chars": len(methods)}

    def methods_section(self, tasks: list[dict[str, Any]]) -> str:
        lines = ["# Methods", ""]
        citations: list[str] = []
        for t in tasks:
            try:
                cap = self.ctx.registry.get(t.get("capability", ""))
            except Exception:  # noqa: BLE001 - stale capability
                continue
            params = ", ".join(f"{k}={v}" for k, v in sorted((t.get("params") or {}).items()))
            lines.append(
                f"- **{cap.id}** (v{cap.version}) — {cap.summary or 'no summary'}"
                + (f" Parameters: {params}." if params else ""))
            if cap.citation and cap.citation not in citations:
                citations.append(cap.citation)
        if citations:
            lines += ["", "## References", ""] + [f"{i}. {c}" for i, c in enumerate(citations, 1)]
        lines += ["", "_Generated from the run provenance ledger by the Bioinfo Harness scribe._"]
        return "\n".join(lines) + "\n"


class Runner(Agent):
    """You execute pipelines. You run exactly the workflow you were handed, you
    never edit it to make it work, and you stop at the first failure rather than
    pressing on with broken inputs. What you did is recorded in the ledger."""

    role = "runner"
    subscribes = ("run.request",)
    # Runners start bounded: they need an explicit scope or full autonomy to
    # execute.
    default_autonomy = Autonomy.BOUNDED

    def _demanded_params(self, raw: dict[str, Any]) -> set[str]:
        """Parameters that capabilities declare with no usable default.

        These must be supplied by the operator before the run can start.
        """
        wanted: set[str] = set()
        for step in raw.get("steps", []) or []:
            try:
                cap = self.ctx.registry.get(step.get("capability", ""))
            except Exception:  # noqa: BLE001 - unknown capability, handled later
                continue
            given = set(step.get("params", {}) or {})
            for name, default in (cap.params or {}).items():
                if default in ("", None) and name not in given:
                    wanted.add(name)
        return wanted

    def handle(self, msg: Message) -> dict[str, Any]:
        raw = msg.body.get("workflow") or {}
        if not raw or not raw.get("steps"):
            return {"ok": False, "reason": "no workflow to run"}

        wf = workflow_from_dict(raw)
        params = {**(raw.get("params") or {}), **(msg.body.get("params") or {})}
        # Missing = declared-but-empty, plus parameters referenced by steps but never
        # declared. Workflow-level parameters apply to every step that declares them.
        steps = raw.get("steps", []) or []
        for step in steps:
            try:
                cap = self.ctx.registry.get(step.get("capability", ""))
            except Exception:  # noqa: BLE001
                continue
            for name in (cap.params or {}):
                if name in params and params[name] not in (None, ""):
                    step.setdefault("params", {}).setdefault(name, params[name])

        supplied = {k for k, v in params.items() if v not in (None, "")}
        unbound = sorted(
            {k for k, v in params.items() if v is None}
            | (_referenced_params(raw) - supplied)
            | (self._demanded_params(raw) - supplied))
        if unbound:
            # Refuse to run with missing parameters.
            self.publish(f"run/blocked/{wf.name}",
                         {"reason": "unbound parameters", "needs": unbound})
            return {"ok": False, "reason": "unbound parameters", "needs": unbound,
                    "workflow": wf.name}

        runs = self.ctx.runs or (self.workdir / "runs")
        orch = Orchestrator(self.ctx.registry, Workspace(runs),
                            self.ctx.guardrails, self.hive)
        summary = orch.run(wf, params, actor=self.actor())

        blocked = [t for t in summary.get("tasks", []) if t.get("status") == "blocked"]
        self.publish(f"run/{summary['run_id']}",
                     {"workflow": wf.name, "status": summary["status"],
                      "dir": summary["dir"], "blocked": [t["error"] for t in blocked]})
        self.remember(f"Ran `{wf.name}` ({summary['status']}) as {summary['run_id']}: "
                      + " -> ".join(t["step"] for t in summary.get("tasks", [])),
                      heading=f"run {summary['run_id']}")
        return {"ok": summary["status"] == "ok", "run_id": summary["run_id"],
                "status": summary["status"], "dir": summary["dir"],
                "seconds": summary.get("seconds"),
                "refused": [t["error"] for t in blocked]}


class PI(Agent):
    """You are the principal investigator. You turn a research goal into a mission:
    plan, curate, run, validate, analyse, write up. You delegate rather than execute,
    and you stop the mission when a QC gate fails rather than pushing through."""

    role = "pi"
    subscribes = ("goal", "mission", "plan.request.result", "data.validate.result",
                  "run.request.result")
    default_autonomy = Autonomy.BOUNDED

    # Work topic for each role. The PI may only dispatch these topics.
    DISPATCH_TOPICS = {
        "planner": "plan.request", "curator": "data.validate",
        "validator": "qc.check", "analyst": "analyze", "scribe": "report",
        "runner": "run.request",
    }
    # Inputs each role needs before its work topic can be sent.
    DISPATCH_NEEDS = {"curator": "inputs", "validator": "metrics",
                      "analyst": "artifacts", "scribe": "tasks",
                      "runner": "workflow"}

    DISPATCH_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "orders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "instruction": {"type": "string"},
                    },
                    "required": ["role", "instruction"],
                },
            },
        },
        "required": ["orders"],
    }

    def act_on_directive(self, order: str, msg: Message) -> dict[str, Any] | None:
        """Turn an operator order into a mission and dispatch work.

        Uses `ask_idle=False` because every agent already returns a verdict on the
        order.
        """
        if not order:
            return None
        return self._dispatch(order, {**msg.body, "goal": order}, msg.sender,
                              ask_idle=False)

    def handle(self, msg: Message) -> dict[str, Any]:
        if msg.topic == "plan.request.result":
            return self._on_plan(msg)
        if msg.topic == "data.validate.result":
            return self._on_curation(msg)
        if msg.topic == "run.request.result":
            return self._on_run(msg)

        goal = msg.body.get("goal") or msg.body.get("question") or msg.body.get("note", "")
        return self._dispatch(goal, msg.body, msg.sender)

    def _dispatch(self, goal: str, body: dict[str, Any], asked_by: str,
                  ask_idle: bool = True) -> dict[str, Any]:
        # key=value pairs in the order become mission parameters.
        given = {**params_in(goal), **(body.get("params") or {})}
        if given:
            body = {**body, "params": given}
            self.publish("mission/params", given)
        msg = Message(id="", sender=asked_by, recipient=self.name,
                      topic="goal", body=body)
        self.remember(f"Mission requested: {goal}", heading="mission")

        staffed = {a["role"]: a["name"] for a in self.hive.roster()
                   if a["name"] != self.name and a.get("role") in self.DISPATCH_TOPICS}
        orders, reasoning = self._decide(goal, sorted(staffed), msg.body)

        dispatched: list[dict[str, str]] = []
        seen: set[str] = set()
        for order in orders:
            role = str(order.get("role", "")).strip().lower()
            instruction = str(order.get("instruction", "")).strip()
            if role in seen or role not in staffed or not instruction:
                if role and role not in staffed:
                    self.hive.event("agent.no_peer",
                                    {"agent": self.name, "wanted": role})
                continue
            seen.add(role)
            who = staffed[role]
            topic = self._task_for(role, msg.body)
            if topic == self.PEER_ASK and not ask_idle:
                # Nothing to do and already replied: stay silent.
                continue
            if topic == self.PEER_ASK:
                # No inputs yet: ask for an opinion instead of dispatching work.
                self.ask_peer(who, instruction, goal=goal,
                              domain=msg.body.get("domain"))
            else:
                self.send(who, topic, **{**msg.body, "goal": goal,
                                         "instruction": instruction})
            dispatched.append({"to": who, "role": role, "topic": topic})

        self.publish("mission/current", {"goal": goal, "requested_by": msg.sender,
                                         "reasoning": reasoning,
                                         "dispatched": dispatched})
        if reasoning:
            self.remember(reasoning[:1500], heading="how I split this up")
        return {"ok": bool(dispatched), "goal": goal, "reasoning": reasoning,
                "dispatched": dispatched,
                "reason": "" if dispatched else "nobody on the roster to task"}

    def _task_for(self, role: str, body: dict[str, Any]) -> str:
        """The topic that gives `role` real work, given what the mission holds.

        A validator handed `qc.check` with no metrics returns a meaningless
        pass, so a role whose inputs do not exist yet is asked a question
        instead of being given a job.
        """
        needed = self.DISPATCH_NEEDS.get(role)
        if needed and not body.get(needed):
            return self.PEER_ASK
        return self.DISPATCH_TOPICS[role]

    def _decide(self, goal: str, roles: list[str],
                body: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
        """Work out who does what. Falls back to the standard split.

        The deterministic split is not a degraded mode: it is what a PI does
        with any goal -- plan it, and ask everyone else what they need.
        """
        default = [{"role": r,
                    "instruction": (f"Plan the pipeline for: {goal}" if r == "planner"
                                    else f"Mission goal: {goal}. How would you "
                                         f"approach your part of it, what do you "
                                         f"need from the others, and is there "
                                         f"anything here that would make you stop "
                                         f"the run?")}
                   for r in (["planner"] if "planner" in roles else [])
                   + [r for r in roles if r != "planner"]]
        if self.provider is None or not roles:
            return default, ""
        try:
            decision = self.think_json(
                f"The lab has been asked: {goal}\n\n"
                f"These roles are staffed: {', '.join(roles)}. Give each one an "
                f"order in your own words -- what you want from them for this "
                f"goal specifically. Task the planner first if it is staffed. "
                f"Use each role at most once, and only roles from that list.",
                context=json.dumps({k: v for k, v in body.items() if v}, default=str)[:4000],
                fallback={"orders": default},
                schema=self.DISPATCH_SCHEMA)
        except (ProviderUnavailable, ValueError) as exc:
            self.hive.event("agent.fell_back",
                            {"agent": self.name, "reason": str(exc)[:200]})
            return default, ""
        orders = decision.get("orders") if isinstance(decision, dict) else None
        if not isinstance(orders, list) or not orders:
            return default, ""
        return orders, str(decision.get("reasoning", ""))

    def _on_plan(self, msg: Message) -> dict[str, Any]:
        """The planner has answered. Carry the plan to whoever can use it now."""
        wf = msg.body.get("workflow") or {}
        if not msg.body.get("ok") or not wf:
            reason = msg.body.get("reason", "no workflow")
            self.remember(f"No route found: {reason}", heading="plan failed")
            for role in ("curator", "analyst"):
                who = self.hive.whois(role)
                if who:
                    self.ask_peer(who, f"The planner could not route this goal "
                                       f"({reason}). What capability are we missing?",
                                  candidates=msg.body.get("candidates", []))
            return {"ok": False, "reason": reason}

        steps = wf.get("steps", []) or []
        self.publish("mission/plan", {"workflow": wf.get("name"),
                                      "steps": [s.get("capability") for s in steps]})

        # Missing parameters: ask the operator.
        given = self.hive.blackboard.get("mission/params") or {}
        if given:
            wf["params"] = {**(wf.get("params") or {}), **given}
        unbound = sorted(k for k, v in (wf.get("params") or {}).items()
                         if v in (None, ""))
        if unbound:
            self.needs_from_operator(unbound, wf)
            return {"ok": False, "reason": "waiting on the operator",
                    "workflow": wf.get("name"), "needs": unbound,
                    "steps": [s.get("capability") for s in steps]}
        self.remember("Plan accepted: "
                      + " -> ".join(s.get("capability", "?") for s in steps),
                      heading=f"plan {wf.get('name', '')}")
        # The scribe drafts Methods from the plan; the ledger fills in details later.
        scribe = self.hive.whois("scribe")
        if scribe:
            self.send(scribe, "report", tasks=steps, run_id=wf.get("name", ""),
                      instruction="Draft Methods for the planned pipeline.")

        # The curator validates inputs before the run is dispatched.
        self.publish("mission/workflow", wf)
        curator = self.hive.whois("curator")
        if curator:
            # Only parameters bound to input ports are file paths.
            files = file_params(self.ctx.registry, wf)
            self.send(curator, "data.validate",
                      inputs={k: v for k, v in (wf.get("params") or {}).items()
                              if k in files and v},
                      types={}, workflow=wf.get("name"),
                      instruction="Check the input files for the plan before it runs.")
            next_step = f"{curator} is checking the inputs"
        else:
            next_step = self._dispatch_run(wf, "no curator on the roster")
        return {"ok": True, "workflow": wf.get("name"),
                "steps": [s.get("capability") for s in steps],
                "handed_to": scribe or "(no scribe)", "next": next_step}

    def needs_from_operator(self, needs: list[str], wf: dict[str, Any]) -> None:
        """One precise question to the human, instead of a conversation."""
        chain = " -> ".join(s.get("capability", "?") for s in wf.get("steps", []))
        self.publish("mission/held", {"workflow": wf.get("name"), "needs": needs})
        self.send(self.OPERATOR, "needs",
                  workflow=wf.get("name"), needs=needs, steps=chain,
                  line=(f"I can run {chain}, but I need you to supply "
                        f"{', '.join(needs)}. Send them straight back as an "
                        f"order: bioharness order \"run {wf.get('name')} with "
                        + " ".join(f"{n}=<value>" for n in needs) + "\""))
        self.remember(f"Held {wf.get('name')}: waiting on {', '.join(needs)}",
                      heading="held")

    def _on_run(self, msg: Message) -> dict[str, Any]:
        """The runner has reported. A refusal for want of inputs goes straight
        to the operator rather than round the room again."""
        if msg.body.get("ok"):
            return {"ok": True, "run_id": msg.body.get("run_id"),
                    "status": msg.body.get("status"), "dir": msg.body.get("dir")}
        needs = msg.body.get("needs") or []
        if needs:
            self.needs_from_operator(
                needs, self.hive.blackboard.get("mission/workflow") or {})
        return {"ok": False, "reason": msg.body.get("reason", "the run did not start"),
                "needs": needs, "refused": msg.body.get("refused") or []}

    def _on_curation(self, msg: Message) -> dict[str, Any]:
        """The curator has ruled on the inputs. Only a pass reaches the runner."""
        wf = self.hive.blackboard.get("mission/workflow") or {}
        problems = msg.body.get("problems") or []
        if not msg.body.get("ok"):
            self.remember("Run held: " + "; ".join(str(p) for p in problems[:5]),
                          heading="curation block")
            self.publish("mission/held", {"workflow": wf.get("name"),
                                          "problems": problems})
            return {"ok": False, "reason": "curator blocked the inputs",
                    "problems": problems}
        return {"ok": True, "next": self._dispatch_run(wf, "inputs cleared")}

    def _dispatch_run(self, wf: dict[str, Any], why: str) -> str:
        runner = self.hive.whois("runner")
        if not runner or not wf:
            self.hive.event("agent.no_peer", {"agent": self.name, "wanted": "runner"})
            return "nobody is hired to run it"
        self.send(runner, "run.request", workflow=wf, why=why,
                  params=self.hive.blackboard.get("mission/params") or {},
                  instruction="Execute this workflow as planned.")
        return f"handed to {runner} to execute ({why})"


ROLES: dict[str, type[Agent]] = {
    "pi": PI, "planner": Planner, "curator": Curator, "runner": Runner,
    "validator": Validator, "analyst": Analyst, "scribe": Scribe,
}


PARAM_REF = re.compile(r"\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)")


def _referenced_params(raw: dict[str, Any]) -> set[str]:
    """Every `params.<name>` a workflow's bindings mention."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(PARAM_REF.findall(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(raw.get("steps", []))
    return found


def workflow_from_dict(raw: dict[str, Any]) -> Workflow:
    return Workflow(
        name=raw.get("name", "workflow"),
        description=raw.get("description", ""),
        params=raw.get("params", {}) or {},
        steps=[Step(id=s["id"], capability=s["capability"],
                    with_=s.get("with", s.get("with_", {})) or {},
                    params=s.get("params", {}) or {},
                    needs=list(s.get("needs", []) or []),
                    when=s.get("when"), scatter=s.get("scatter"),
                    retries=int(s.get("retries", 0)),
                    description=s.get("description", ""))
               for s in raw.get("steps", []) or []],
    )


def _slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())[:60] or "workflow"


def _digest_artifacts(artifacts: dict[str, Any], head_bytes: int = 2000) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in artifacts.items():
        for item in (value if isinstance(value, list) else [value]):
            p = Path(str(item))
            if not p.is_file():
                continue
            entry: dict[str, Any] = {"path": str(p), "bytes": p.stat().st_size}
            if p.suffix.lower() in {".txt", ".csv", ".tsv", ".json", ".log", ".md", ".vcf"}:
                try:
                    entry["head"] = p.read_text(errors="replace")[:head_bytes]
                except OSError:
                    pass
            out.setdefault(name, []).append(entry)
    return out


def _descriptive_summary(artifacts: dict[str, Any], metrics: dict[str, Any]) -> str:
    lines = [f"{len(artifacts)} output port(s) produced."]
    for name, value in artifacts.items():
        items = value if isinstance(value, list) else [value]
        lines.append(f"- {name}: {len(items)} file(s)")
    if metrics:
        lines.append("Metrics: " + ", ".join(f"{k}={v}" for k, v in metrics.items()))
    lines.append("No LLM provider configured; this is a descriptive summary only.")
    return "\n".join(lines)
