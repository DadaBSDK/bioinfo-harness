"""The autonomy ladder, and agents that actually execute.

Before this, `autonomy` was a stored label: nothing consulted it, so `read_only`
restrained nothing and `full` granted nothing, and no agent could execute at all.
These tests are what make the four levels mean something.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bioharness.agents.base import AgentContext
from bioharness.agents.roles import PI, Runner
from bioharness.core.guardrails import Autonomy, Budget, Guardrails
from bioharness.core.hive import Hive
from bioharness.core.loader import workflow_from_dict
from bioharness.core.orchestrator import Orchestrator
from bioharness.core.registry import Registry, capability_from_dict
from bioharness.core.workspace import Workspace

TOUCH = {
    "id": "make_text", "domain": "test", "summary": "write a file",
    "runtime": {"kind": "binary", "binary": "sh"},
    "command": "sh -c 'echo hi > {{ outdir }}/out.txt'",
    "inputs": [], "outputs": [{"name": "text", "type": "text", "glob": "out.txt"}],
}
WORKFLOW = {"name": "demo", "steps": [{"id": "make", "capability": "make_text"}]}


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    reg.register(capability_from_dict(TOUCH))
    return reg


@pytest.fixture
def ctx(tmp_path: Path, registry: Registry) -> AgentContext:
    return AgentContext(hive=Hive(tmp_path / "hive"), registry=registry,
                        guardrails=Guardrails(budget=Budget(max_usd=1)),
                        workdir=tmp_path / "agents", runs=tmp_path / "runs")


def _run_as(ctx: AgentContext, actor: dict | None):
    orch = Orchestrator(ctx.registry, Workspace(ctx.runs), ctx.guardrails, ctx.hive)
    return orch.run(workflow_from_dict(WORKFLOW), actor=actor)


# -- the ladder, enforced --------------------------------------------------
def test_a_human_at_the_cli_is_not_gated(ctx: AgentContext):
    assert _run_as(ctx, None)["status"] == "ok"


def test_read_only_may_not_execute(ctx: AgentContext):
    summary = _run_as(ctx, {"name": "ada", "autonomy": "read_only"})
    assert summary["status"] == "failed"
    assert "read_only" in summary["tasks"][0]["error"]
    assert summary["tasks"][0]["status"] == "blocked"


def test_suggest_asks_for_a_human(ctx: AgentContext):
    summary = _run_as(ctx, {"name": "ada", "autonomy": "suggest"})
    assert "needs a human to approve" in summary["tasks"][0]["error"]


def test_bounded_runs_only_inside_its_scope(ctx: AgentContext):
    blocked = _run_as(ctx, {"name": "ada", "autonomy": "bounded",
                            "scope": ["fastqc"]})
    assert "outside ada's scope" in blocked["tasks"][0]["error"]
    ok = _run_as(ctx, {"name": "ada", "autonomy": "bounded",
                       "scope": ["make_text"]})
    assert ok["status"] == "ok"


def test_bounded_by_domain(ctx: AgentContext):
    assert _run_as(ctx, {"name": "ada", "autonomy": "bounded",
                         "scope": ["domain:test"]})["status"] == "ok"


def test_bounded_with_no_scope_runs_nothing_and_says_why(ctx: AgentContext):
    summary = _run_as(ctx, {"name": "ada", "autonomy": "bounded"})
    assert "no declared scope" in summary["tasks"][0]["error"]


def test_full_runs_anything_registered(ctx: AgentContext):
    assert _run_as(ctx, {"name": "ada", "autonomy": "full"})["status"] == "ok"


def test_the_intervention_ladder_can_take_authority_back(ctx: AgentContext):
    """A misbehaving agent is constrained even though it was hired at full."""
    ctx.guardrails.intervention.escalate("ada", "kept retrying a dead tool")
    ctx.guardrails.intervention.escalate("ada", "did it again")
    summary = _run_as(ctx, {"name": "ada", "autonomy": "full"})
    assert "dropped from full by intervention" in summary["tasks"][0]["error"]


def test_a_blocked_step_is_recorded_in_the_run(ctx: AgentContext):
    _run_as(ctx, {"name": "ada", "autonomy": "read_only"})
    events = [e["event"] for e in ctx.hive.events()]
    assert "step.blocked" in events


# -- the runner ------------------------------------------------------------
def test_a_runner_at_full_executes_the_workflow(ctx: AgentContext):
    ctx.hive.enroll("rex", "runner", autonomy="full")
    rex = Runner("rex", ctx)
    result = rex.handle(ctx.hive.mailbox("hugo").send(
        "rex", "run.request", {"workflow": WORKFLOW}))
    assert result["ok"] and result["status"] == "ok"
    assert (Path(result["dir"]) / "summary.json").is_file()


def test_a_runner_refuses_a_workflow_with_unbound_parameters(ctx: AgentContext):
    ctx.hive.enroll("rex", "runner", autonomy="full")
    rex = Runner("rex", ctx)
    holed = {**WORKFLOW, "params": {"reads": None}}
    result = rex.handle(ctx.hive.mailbox("hugo").send(
        "rex", "run.request", {"workflow": holed}))
    assert result["ok"] is False and result["needs"] == ["reads"]


def test_the_identity_autonomy_beats_the_role_default(ctx: AgentContext):
    """`staff assign --autonomy full` is the operator's decision."""
    ctx.hive.enroll("rex", "runner", autonomy="full")
    rex = Runner("rex", ctx)                      # role default is BOUNDED
    assert rex.actor()["autonomy"] == "full"


def test_nothing_runs_on_the_planners_word_alone(ctx: AgentContext):
    """The curator sees the inputs before the runner is given anything."""
    ctx.hive.enroll("iris", "curator")
    ctx.hive.enroll("rex", "runner")
    hugo = PI("hugo", ctx)
    hugo.handle(ctx.hive.mailbox("ada").send("hugo", "plan.request.result", {
        "ok": True, "workflow": {"name": "demo", "steps": WORKFLOW["steps"]}}))
    ctx.hive.route()
    assert [m.topic for m in ctx.hive.mailbox("iris").peek()] == ["data.validate"]
    assert ctx.hive.mailbox("rex").peek() == [], "the runner was handed an unchecked plan"


def test_a_cleared_plan_reaches_the_runner(ctx: AgentContext):
    ctx.hive.enroll("rex", "runner")
    hugo = PI("hugo", ctx)
    ctx.hive.blackboard.put("mission/workflow", {"name": "demo", "steps": WORKFLOW["steps"]})
    result = hugo.handle(ctx.hive.mailbox("iris").send(
        "hugo", "data.validate.result", {"ok": True, "problems": [], "checked": 2}))
    ctx.hive.route()
    assert result["ok"]
    assert [m.topic for m in ctx.hive.mailbox("rex").peek()] == ["run.request"]


def test_a_curator_block_stops_the_run(ctx: AgentContext):
    ctx.hive.enroll("rex", "runner")
    hugo = PI("hugo", ctx)
    ctx.hive.blackboard.put("mission/workflow", {"name": "demo", "steps": WORKFLOW["steps"]})
    result = hugo.handle(ctx.hive.mailbox("iris").send(
        "hugo", "data.validate.result",
        {"ok": False, "problems": ["reads: missing path /data/x.fastq"]}))
    ctx.hive.route()
    assert result["ok"] is False
    assert ctx.hive.mailbox("rex").peek() == []
    assert ctx.hive.blackboard.get("mission/held")["problems"]


def test_a_runner_catches_a_parameter_nobody_declared(ctx: AgentContext):
    """`{{ params.reads }}` with no `reads` anywhere fails at the first step with
    a template error, which reads like a tool problem rather than a missing input."""
    ctx.hive.enroll("rex", "runner", autonomy="full")
    rex = Runner("rex", ctx)
    plan = {"name": "qc", "steps": [
        {"id": "qc_raw", "capability": "make_text",
         "with": {"reads": "{{ params.reads }}"}}]}
    result = rex.handle(ctx.hive.mailbox("hugo").send(
        "rex", "run.request", {"workflow": plan}))
    assert result["ok"] is False
    assert result["needs"] == ["reads"]


def test_a_runner_accepts_a_parameter_supplied_with_the_request(ctx: AgentContext):
    ctx.hive.enroll("rex", "runner", autonomy="full")
    rex = Runner("rex", ctx)
    plan = {"name": "qc", "steps": [
        {"id": "make", "capability": "make_text",
         "with": {"note": "{{ params.reads }}"}}]}
    result = rex.handle(ctx.hive.mailbox("hugo").send(
        "rex", "run.request", {"workflow": plan, "params": {"reads": "/tmp/x.fastq"}}))
    assert result["ok"] is True
