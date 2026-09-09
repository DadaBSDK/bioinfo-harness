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
from collections import deque
from pathlib import Path
from typing import Any

from ..core.guardrails import Autonomy
from ..core.registry import Registry
from ..core.orchestrator import topo_sort
from ..core.schemas import Message, Step, Workflow
from .base import Agent, ProviderUnavailable


# --------------------------------------------------------------------------
# Type-graph planning: works with no LLM at all.
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


class Planner(Agent):
    """You design bioinformatics workflows. You are given a goal and a catalogue
    of installed capabilities. You select the minimum set of steps that reaches
    the goal, wire each step's inputs to earlier steps' outputs, and you never
    invent a capability that is not in the catalogue."""

    role = "planner"
    subscribes = ("plan.request",)
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
                wf = self._linear_workflow(goal or f"{start_type}-to-{goal_type}", chain)

        if wf is None and self.provider is not None:
            wf = self._llm_workflow(goal, domain)

        if wf is None:
            candidates = [c.id for c in self.ctx.registry.search(goal, domain)[:8]]
            return {"ok": False, "reason": "no route found",
                    "goal": goal, "candidates": candidates}

        self.publish(f"plan/{wf['name']}", wf)
        self.remember(f"Planned `{wf['name']}` for goal: {goal}\nSteps: "
                      + " -> ".join(s["capability"] for s in wf["steps"]),
                      heading=f"plan {wf['name']}")
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

            steps.append({"id": cap_id, "capability": cap_id, "with": binding,
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
            # A dependency is only real if it names a step we already accepted.
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
        except Exception:  # noqa: BLE001 - an unusable plan is simply not a plan
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
        # Shape the planner actually wants back, enforced where the provider
        # supports it and used as documentation where it does not.
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
                    continue  # unresolved template; the orchestrator binds it later
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

    # (metric, comparison, default threshold) -- overridable per message.
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
            # Losing the LLM should degrade the analysis, not fail the mission.
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
            except Exception:  # noqa: BLE001 - a stale capability should not break the write-up
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


class PI(Agent):
    """You are the principal investigator. You turn a research goal into a mission:
    plan, curate, run, validate, analyse, write up. You delegate rather than execute,
    and you stop the mission when a QC gate fails rather than pushing through."""

    role = "pi"
    subscribes = ("goal", "mission")
    default_autonomy = Autonomy.BOUNDED

    def handle(self, msg: Message) -> dict[str, Any]:
        goal = msg.body.get("goal", "")
        self.remember(f"Mission requested: {goal}", heading="mission")
        self.send("planner", "plan.request", **msg.body)
        self.publish("mission/current", {"goal": goal, "requested_by": msg.sender})
        return {"ok": True, "delegated_to": "planner", "goal": goal}


ROLES: dict[str, type[Agent]] = {
    "pi": PI, "planner": Planner, "curator": Curator,
    "validator": Validator, "analyst": Analyst, "scribe": Scribe,
}


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
