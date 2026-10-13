"""End-to-end tests that need no bioinformatics tools installed.

Every test builds its own capabilities out of coreutils, so the suite proves
the harness mechanics (binding, DAG order, scatter, provenance, guardrails,
hive routing) rather than proving GROMACS works.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bioharness.agents.base import AgentContext
from bioharness.agents.roles import Curator, Validator, plan_by_types
from bioharness.core.guardrails import (Autonomy, Budget, CircuitBreaker, Guardrails,
                                        GuardrailTripped, Intervention)
from bioharness.core.hive import Hive
from bioharness.core.loader import workflow_from_dict
from bioharness.core.orchestrator import Orchestrator, WorkflowError, topo_sort
from bioharness.core.registry import Registry, capability_from_dict
from bioharness.core.schemas import Status
from bioharness.core.template import render, render_value
from bioharness.core.workspace import Workspace

# -- fixtures --------------------------------------------------------------
ECHO = {
    "id": "make_text", "domain": "test", "summary": "write a file",
    "runtime": {"kind": "binary", "binary": "sh"},
    "command": "sh -c 'echo {{ params.message }} > {{ outdir }}/out.txt'",
    "inputs": [], "outputs": [{"name": "text", "type": "text", "glob": "out.txt"}],
    "params": {"message": "hello"},
}
UPPER = {
    "id": "upper", "domain": "test", "summary": "uppercase a file",
    "runtime": {"kind": "binary", "binary": "sh"},
    "command": "sh -c 'tr a-z A-Z < {{ inputs.text }} > {{ outdir }}/upper.txt'",
    "inputs": [{"name": "text", "type": "text"}],
    "outputs": [{"name": "loud", "type": "shout", "glob": "upper.txt"}],
}
BROKEN = {
    "id": "broken", "domain": "test", "summary": "always fails",
    "runtime": {"kind": "binary", "binary": "sh"},
    "command": "sh -c 'exit 3'",
    "inputs": [], "outputs": [],
}


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    for raw in (ECHO, UPPER, BROKEN):
        reg.register(capability_from_dict(raw))
    return reg


@pytest.fixture
def orch(registry: Registry, tmp_path: Path) -> Orchestrator:
    return Orchestrator(registry, Workspace(tmp_path / "runs"), Guardrails())


# -- templating ------------------------------------------------------------
def test_render_substitutes_dotted_paths():
    assert render("{{ a.b }}/x", {"a": {"b": "v"}}) == "v/x"


def test_render_value_preserves_scalar_type():
    assert render_value("{{ params.n }}", {"params": {"n": 8}}) == 8
    assert render_value("n={{ params.n }}", {"params": {"n": 8}}) == "n=8"


def test_render_joins_list_inputs():
    assert render("{{ inputs.reads }}", {"inputs": {"reads": ["a.fq", "b.fq"]}}) == "a.fq b.fq"


# -- registry --------------------------------------------------------------
def test_registry_loads_shipped_manifests():
    reg = Registry.load(Path(__file__).resolve().parents[1] / "capabilities")
    assert len(reg) >= 10
    assert {"ngs", "structural", "general"} <= set(reg.domains())
    assert reg.get("bwa_mem").resources.cpu == 8


def test_registry_search_ranks_id_matches_first(registry: Registry):
    assert registry.search("upper")[0].id == "upper"


def test_registry_rejects_duplicate_ids(registry: Registry):
    with pytest.raises(Exception):
        registry.register(capability_from_dict(ECHO))


def test_probe_reports_missing_binary():
    reg = Registry()
    reg.register(capability_from_dict(
        {**ECHO, "id": "nope", "runtime": {"kind": "binary", "binary": "definitely-not-a-tool"}}))
    ok, reason = reg.probe("nope")
    assert not ok and "not on PATH" in reason


# -- type-graph planning ---------------------------------------------------
def test_plan_by_types_finds_chain(registry: Registry):
    assert plan_by_types(registry, "text", "shout") == ["upper"]


def test_plan_by_types_returns_empty_when_unreachable(registry: Registry):
    assert plan_by_types(registry, "text", "vcf") == []


def test_plan_by_types_on_real_ngs_manifests():
    reg = Registry.load(Path(__file__).resolve().parents[1] / "capabilities")
    chain = plan_by_types(reg, "fastq", "vcf", domain="ngs")
    assert chain and chain[-1] == "bcftools_call"


# -- DAG -------------------------------------------------------------------
def test_topo_sort_orders_dependencies():
    wf = workflow_from_dict({"name": "w", "steps": [
        {"id": "b", "capability": "upper", "needs": ["a"]},
        {"id": "a", "capability": "make_text"}]})
    assert [s.id for s in topo_sort(wf)] == ["a", "b"]


def test_topo_sort_detects_cycle():
    wf = workflow_from_dict({"name": "w", "steps": [
        {"id": "a", "capability": "upper", "needs": ["b"]},
        {"id": "b", "capability": "upper", "needs": ["a"]}]})
    with pytest.raises(WorkflowError, match="cycle"):
        topo_sort(wf)


def test_topo_sort_detects_dangling_dependency():
    wf = workflow_from_dict({"name": "w", "steps": [
        {"id": "a", "capability": "upper", "needs": ["ghost"]}]})
    with pytest.raises(WorkflowError, match="unknown step"):
        topo_sort(wf)


# -- execution -------------------------------------------------------------
def test_workflow_runs_and_chains_outputs(orch: Orchestrator):
    wf = workflow_from_dict({"name": "chain", "params": {"msg": "harness"}, "steps": [
        {"id": "make", "capability": "make_text", "params": {"message": "{{ params.msg }}"}},
        {"id": "shout", "capability": "upper", "needs": ["make"],
         "with": {"text": "{{ steps.make.outputs.text }}"}}]})
    summary = orch.run(wf)
    assert summary["status"] == "ok"
    loud = Path(summary["outputs"]["shout"]["loud"])
    assert loud.read_text().strip() == "HARNESS"


def test_dry_run_resolves_commands_without_executing(registry, tmp_path):
    o = Orchestrator(registry, Workspace(tmp_path / "r"), Guardrails(dry_run=True))
    summary = o.run(workflow_from_dict(
        {"name": "d", "steps": [{"id": "make", "capability": "make_text"}]}))
    assert summary["by_status"]["skipped"] == 1
    assert "echo" in summary["tasks"][0]["command"]
    assert not list((Path(summary["dir"]) / "steps").rglob("out.txt"))


def test_failure_halts_downstream_steps(orch: Orchestrator):
    summary = orch.run(workflow_from_dict({"name": "f", "steps": [
        {"id": "bad", "capability": "broken"},
        {"id": "after", "capability": "make_text", "needs": ["bad"]}]}))
    assert summary["status"] == "failed"
    assert summary["by_status"]["failed"] == 1
    assert "after" not in [t["step"] for t in summary["tasks"] if t["status"] == "ok"]


def test_missing_required_input_is_reported(orch: Orchestrator):
    summary = orch.run(workflow_from_dict(
        {"name": "m", "steps": [{"id": "u", "capability": "upper"}]}))
    assert summary["status"] == "failed"
    assert "requires input" in summary["tasks"][0]["error"]


def test_when_guard_skips_step(orch: Orchestrator):
    summary = orch.run(workflow_from_dict({"name": "g", "params": {"mode": "off"}, "steps": [
        {"id": "make", "capability": "make_text", "when": "{{ params.mode }} == on"}]}))
    assert summary["tasks"][0]["status"] == "skipped"


def test_scatter_fans_out_one_task_per_item(registry, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    files = []
    for name in ("a", "b", "c"):
        f = src / f"{name}.txt"
        f.write_text(f"{name}\n")
        files.append(str(f))
    o = Orchestrator(registry, Workspace(tmp_path / "r"), Guardrails())
    summary = o.run(workflow_from_dict({"name": "s", "steps": [
        {"id": "up", "capability": "upper", "scatter": "text",
         "with": {"text": "{{ params.files }}"}}]}), params={"files": files})
    assert summary["status"] == "ok"
    assert len([t for t in summary["tasks"] if t["step"].startswith("up.")]) == 3


def test_retries_are_attempted(registry, tmp_path):
    o = Orchestrator(registry, Workspace(tmp_path / "r"), Guardrails())
    o.run(workflow_from_dict({"name": "r", "steps": [
        {"id": "bad", "capability": "broken", "retries": 2}]}))
    retries = [e for e in o.ws.ledger() if e["event"] == "task.retry"]
    assert len(retries) == 2


# -- provenance ------------------------------------------------------------
def test_provenance_records_hashes(orch: Orchestrator):
    summary = orch.run(workflow_from_dict(
        {"name": "p", "steps": [{"id": "make", "capability": "make_text"}]}))
    arts = [e["payload"] for e in orch.ws.ledger() if e["event"] == "artifact.created"]
    assert arts and len(arts[0]["sha256"]) == 64
    assert json.loads((Path(summary["dir"]) / "summary.json").read_text())["status"] == "ok"


def test_run_listing_finds_the_run(orch: Orchestrator, tmp_path):
    orch.run(workflow_from_dict({"name": "l", "steps": [
        {"id": "make", "capability": "make_text"}]}))
    runs = Workspace.list_runs(tmp_path / "runs")
    assert len(runs) == 1 and runs[0]["workflow"] == "l"


# -- guardrails ------------------------------------------------------------
def test_budget_trips_on_step_ceiling():
    b = Budget(max_steps=2)
    b.charge(steps=2)
    with pytest.raises(GuardrailTripped, match="steps"):
        b.check()


def test_step_budget_blocks_further_execution(registry, tmp_path):
    rails = Guardrails(budget=Budget(max_steps=1))
    o = Orchestrator(registry, Workspace(tmp_path / "r"), rails)
    summary = o.run(workflow_from_dict({"name": "b", "steps": [
        {"id": "one", "capability": "make_text"},
        {"id": "two", "capability": "make_text", "needs": ["one"]}]}))
    assert summary["by_status"].get("blocked") == 1


def test_circuit_breaker_opens_then_cools_down():
    cb = CircuitBreaker(failure_threshold=2, cooldown_s=0.0)
    cb.record_failure("x")
    cb.record_failure("x")
    assert cb.state("x") == "half-open"       # cooldown already elapsed
    cb2 = CircuitBreaker(failure_threshold=1, cooldown_s=60)
    cb2.record_failure("y")
    with pytest.raises(GuardrailTripped, match="circuit breaker"):
        cb2.guard("y")


def test_intervention_ladder_escalates_and_relaxes():
    i = Intervention()
    assert i.escalate("a", "r1") == "steer"
    assert i.escalate("a", "r2") == "constrain"
    assert i.autonomy_for("a", Autonomy.FULL) is Autonomy.SUGGEST
    assert i.escalate("a", "r3") == "stop"
    assert i.stopped("a")
    i.relax("a")
    assert i.level("a") == "constrain"


# -- hive ------------------------------------------------------------------
def test_router_delivers_between_mailboxes(tmp_path):
    hive = Hive(tmp_path / "hive")
    hive.enroll("alice", "planner")
    hive.enroll("bob", "runner")
    hive.send("alice", "bob", "plan.request", {"goal": "call variants"})
    msgs = hive.mailbox("bob").read()
    assert len(msgs) == 1 and msgs[0].body["goal"] == "call variants"
    assert hive.mailbox("bob").pending() == 0     # consumed


def test_broadcast_reaches_everyone_but_sender(tmp_path):
    hive = Hive(tmp_path / "hive")
    for n in ("a", "b", "c"):
        hive.enroll(n, "role")
    hive.send("a", "*", "announce", {"x": 1})
    assert hive.mailbox("b").pending() == 1
    assert hive.mailbox("c").pending() == 1
    assert hive.mailbox("a").pending() == 0


def test_blackboard_round_trips(tmp_path):
    hive = Hive(tmp_path / "hive")
    hive.blackboard.put("qc/sample1", {"verdict": "pass"}, agent="validator")
    assert hive.blackboard.get("qc/sample1")["verdict"] == "pass"
    assert "qc/sample1" in hive.blackboard.keys()


def test_memory_palace_search_finds_note(tmp_path):
    hive = Hive(tmp_path / "hive")
    hive.enroll("analyst", "analyst")
    hive.append_memory("analyst", "Coverage on sample7 was 12x, below the 20x gate.")
    hits = hive.palace.search("coverage gate")
    assert hits and "sample7" in hits[0]["text"]


def test_cost_ledger_aggregates(tmp_path):
    hive = Hive(tmp_path / "hive")
    hive.charge("planner", tokens_in=1000, tokens_out=500, usd=0.02)
    hive.charge("planner", tokens_in=200, tokens_out=100, usd=0.01)
    ledger = hive.cost_ledger()
    assert ledger["by_agent"]["planner"]["calls"] == 2
    assert ledger["total_usd"] == pytest.approx(0.03)
    assert ledger["total_tokens"] == 1800


# -- agents ----------------------------------------------------------------
@pytest.fixture
def ctx(registry: Registry, tmp_path: Path) -> AgentContext:
    return AgentContext(hive=Hive(tmp_path / "hive"), registry=registry,
                        guardrails=Guardrails(), workdir=tmp_path / "agents")


def test_curator_blocks_missing_input(ctx: AgentContext):
    curator = Curator("curator", ctx)
    ctx.hive.enroll("pi", "pi")
    ctx.hive.send("pi", "curator", "data.validate",
                  {"inputs": {"reads": "/nowhere/sample.fastq.gz"}, "types": {"reads": "fastq"}})
    assert curator.tick() == 1
    assert ctx.hive.blackboard.get("curation/last")["ok"] is False


def test_curator_passes_valid_input(ctx: AgentContext, tmp_path: Path):
    reads = tmp_path / "s.fastq.gz"
    reads.write_text("@r\nACGT\n+\nIIII\n")
    curator = Curator("curator", ctx)
    ctx.hive.enroll("pi", "pi")
    ctx.hive.send("pi", "curator", "data.validate",
                  {"inputs": {"reads": str(reads)}, "types": {"reads": "fastq"}})
    curator.tick()
    assert ctx.hive.blackboard.get("curation/last")["ok"] is True


def test_curator_flags_wrong_extension(ctx: AgentContext, tmp_path: Path):
    bad = tmp_path / "reads.bam"
    bad.write_text("not really")
    curator = Curator("curator", ctx)
    ctx.hive.enroll("pi", "pi")
    ctx.hive.send("pi", "curator", "data.validate",
                  {"inputs": {"reads": str(bad)}, "types": {"reads": "fastq"}})
    curator.tick()
    problems = ctx.hive.blackboard.get("curation/last")["problems"]
    assert any("does not look like fastq" in p for p in problems)


def test_validator_fails_below_threshold(ctx: AgentContext):
    validator = Validator("validator", ctx)
    ctx.hive.enroll("runner", "runner")
    ctx.hive.send("runner", "validator", "qc.check",
                  {"metrics": {"percent_mapped": 61.0, "mean_quality": 34.0}})
    validator.tick()
    qc = ctx.hive.blackboard.get("qc/last")
    assert qc["verdict"] == "fail"
    assert qc["failures"][0]["metric"] == "percent_mapped"


def test_validator_passes_when_all_gates_met(ctx: AgentContext):
    validator = Validator("validator", ctx)
    ctx.hive.enroll("runner", "runner")
    ctx.hive.send("runner", "validator", "qc.check",
                  {"metrics": {"percent_mapped": 96.0, "mean_quality": 34.0}})
    validator.tick()
    assert ctx.hive.blackboard.get("qc/last")["verdict"] == "pass"


def test_agent_error_is_reported_not_raised(ctx: AgentContext):
    validator = Validator("validator", ctx)
    ctx.hive.enroll("runner", "runner")
    ctx.hive.send("runner", "validator", "qc.check", {"metrics": "not-a-dict"})
    assert validator.tick() == 0                    # handled as an error message
    assert ctx.hive.mailbox("runner").peek()         # error was mailed back


# -- python-runtime capability --------------------------------------------
def test_metrics_extract_parses_fastp_json(tmp_path: Path):
    from bioharness.tools.metrics import extract
    src = tmp_path / "fastp.json"
    src.write_text(json.dumps({"summary": {"after_filtering":
                                           {"q30_rate": 0.94, "total_reads": 1000}},
                               "duplication": {"rate": 0.11}}))
    out = extract({"source": [str(src)]}, {}, tmp_path)
    metrics = json.loads(Path(out["metrics"]).read_text())["metrics"]
    assert metrics["percent_q30"] == 94.0
    assert metrics["percent_duplicates"] == 11.0


def test_metrics_extract_parses_flagstat_text(tmp_path: Path):
    from bioharness.tools.metrics import extract
    src = tmp_path / "flagstat.txt"
    src.write_text("1000 + 0 in total (QC-passed reads + QC-failed reads)\n"
                   "950 + 0 mapped (95.00% : N/A)\n")
    out = extract({"source": str(src)}, {}, tmp_path)
    metrics = json.loads(Path(out["metrics"]).read_text())["metrics"]
    assert metrics["total_reads"] == 1000.0


# -- dry-run resolves the whole DAG ---------------------------------------
def test_dry_run_resolves_every_step_not_just_the_first(registry, tmp_path):
    o = Orchestrator(registry, Workspace(tmp_path / "r"), Guardrails(dry_run=True))
    summary = o.run(workflow_from_dict({"name": "d", "steps": [
        {"id": "make", "capability": "make_text"},
        {"id": "shout", "capability": "upper", "needs": ["make"],
         "with": {"text": "{{ steps.make.outputs.text }}"}}]}))
    assert summary["by_status"]["skipped"] == 2
    assert "tr a-z A-Z" in summary["tasks"][1]["command"]


def test_dry_run_predicts_downstream_paths(registry, tmp_path):
    o = Orchestrator(registry, Workspace(tmp_path / "r"), Guardrails(dry_run=True))
    summary = o.run(workflow_from_dict({"name": "d", "steps": [
        {"id": "make", "capability": "make_text"}]}))
    assert summary["outputs"]["make"]["text"].endswith("steps/make/out.txt")


def test_unresolvable_binding_fails_that_step_with_a_clear_message(registry, tmp_path):
    o = Orchestrator(registry, Workspace(tmp_path / "r"), Guardrails())
    summary = o.run(workflow_from_dict({"name": "x", "steps": [
        {"id": "u", "capability": "upper", "with": {"text": "{{ params.nope }}"}}]}))
    assert summary["status"] == "failed"
    assert "cannot resolve 'params.nope'" in summary["tasks"][0]["error"]


# -- auto-planned workflows are complete ----------------------------------
def test_auto_plan_binds_every_required_port(tmp_path):
    reg = Registry.load(Path(__file__).resolve().parents[1] / "capabilities")
    ctx = AgentContext(hive=Hive(tmp_path / "hive"), registry=reg,
                       guardrails=Guardrails(), workdir=tmp_path / "agents")
    from bioharness.agents.roles import Planner
    wf = Planner("planner", ctx)._linear_workflow(
        "fastq-to-vcf", plan_by_types(reg, "fastq", "vcf", "ngs"))
    # every required input of every step must be bound to something
    for step in wf["steps"]:
        for port in reg.get(step["capability"]).inputs:
            if not port.optional:
                assert port.name in step["with"], f"{step['id']}.{port.name} unbound"
    assert "reference" in wf["params"]


def test_auto_planned_workflow_dry_runs_end_to_end(tmp_path):
    reg = Registry.load(Path(__file__).resolve().parents[1] / "capabilities")
    ctx = AgentContext(hive=Hive(tmp_path / "hive"), registry=reg,
                       guardrails=Guardrails(), workdir=tmp_path / "agents")
    from bioharness.agents.roles import Planner
    raw = Planner("planner", ctx)._linear_workflow(
        "fastq-to-vcf", plan_by_types(reg, "fastq", "vcf", "ngs"))
    o = Orchestrator(reg, Workspace(tmp_path / "r"), Guardrails(dry_run=True))
    summary = o.run(workflow_from_dict(raw),
                    params={"reads": "/data/x.fastq.gz", "reference": "/refs/g.fa"})
    assert summary["status"] == "ok"
    assert all(t["command"] for t in summary["tasks"])


# -- shipped workflow files stay valid ------------------------------------
@pytest.mark.parametrize("name", ["wgs-variants", "md-stability", "dock-screen"])
def test_shipped_workflows_load_and_reference_real_capabilities(name):
    root = Path(__file__).resolve().parents[1]
    reg = Registry.load(root / "capabilities")
    from bioharness.core.loader import load_workflow
    wf = load_workflow(root / "workflows" / f"{name}.yaml")
    assert wf.steps
    for step in wf.steps:
        assert step.capability in reg, f"{name}: unknown capability {step.capability}"
    topo_sort(wf)     # must be acyclic and fully connected


def test_workflow_runs_in_a_directory_with_spaces_and_quotes(registry, tmp_path):
    workspace = Workspace(tmp_path / "Lab with spaces and 'quotes'")
    orchestrator = Orchestrator(registry, workspace, Guardrails())
    summary = orchestrator.run(workflow_from_dict({'name': 'quoted-path', 'steps': [
        {'id': 'make', 'capability': 'make_text', 'params': {'message': "hello 'lab' $HOME; literal"}},
        {'id': 'upper', 'capability': 'upper', 'needs': ['make'],
         'with': {'text': '{{ steps.make.outputs.text }}'}}]}))
    assert summary['status'] == 'ok'
    output = workspace.step_dir('upper') / 'upper.txt'
    assert output.read_text().strip() == "HELLO 'LAB' $HOME; LITERAL"


@pytest.mark.parametrize('script', [
    'printf %s {{ value }}',
    'printf %s "{{ value }}"',
    "printf %s '{{ value }}'",
])
def test_shell_substitutions_preserve_literal_special_characters(script):
    import shlex
    import subprocess
    from bioharness.core.template import render_argv
    value = 'spaces \' " $HOME `echo unwanted` ; $(echo nope) \\ end'
    argv = render_argv('sh -c ' + shlex.quote(script), {'value': value})
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert result.stdout == value


def test_direct_commands_expand_lists_without_splitting_paths():
    from bioharness.core.template import render_argv
    assert render_argv('tool {{ files }} --out "{{ outdir }}/file.txt"',
                       {'files': ['one file', 'two files'], 'outdir': 'my lab'}) == [
                           'tool', 'one file', 'two files', '--out', 'my lab/file.txt']
