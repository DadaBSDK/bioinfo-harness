"""Agents talking to each other: addressing, answering, and stopping.

No provider is ever spawned -- a stub provider stands in for the LLM, so these
tests prove the conversation mechanics rather than a model's manners.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bioharness.agents.base import AgentContext, LLMReply
from bioharness.agents.roles import PI, Analyst, Curator, Scribe, Validator
from bioharness.core.guardrails import Guardrails
from bioharness.core.hive import Hive
from bioharness.core.registry import Registry


class StubProvider:
    """Answers instantly, records what it was asked."""

    name = "stub"
    model = "stub-1"

    def __init__(self, text: str = "my two cents"):
        self.text = text
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def ask(self, prompt: str, cwd: Path | None = None, env=None) -> LLMReply:
        self.prompts.append(prompt)
        return LLMReply(text=self.text, tokens_in=3, tokens_out=4, provider=self.name)


@pytest.fixture
def ctx(tmp_path: Path) -> AgentContext:
    return AgentContext(hive=Hive(tmp_path / "hive"), registry=Registry(),
                        guardrails=Guardrails(), workdir=tmp_path / "agents")


def test_whois_resolves_a_role_to_a_hired_name(ctx: AgentContext):
    ctx.hive.enroll("ada", "planner")
    assert ctx.hive.whois("planner") == "ada"
    assert ctx.hive.whois("ada") == "ada"
    assert ctx.hive.whois("nobody") is None


def test_pi_delegates_to_the_planner_by_role_not_by_name(ctx: AgentContext):
    """Mailing the literal string "planner" would reach nobody on a roster of
    real names."""
    ctx.hive.enroll("ada", "planner")
    hugo = PI("hugo", ctx)
    hugo.handle(_msg(ctx, "human", "hugo", "goal", goal="call variants"))
    ctx.hive.route()
    delivered = ctx.hive.mailbox("ada").peek()
    assert [m.topic for m in delivered] == ["plan.request"]
    assert not [e for e in ctx.hive.events("message.undeliverable")]


def test_pi_briefs_every_other_agent(ctx: AgentContext):
    for name, role in (("ada", "planner"), ("iris", "curator"), ("quill", "scribe")):
        ctx.hive.enroll(name, role)
    hugo = PI("hugo", ctx)
    result = hugo.handle(_msg(ctx, "human", "hugo", "goal", goal="assemble a genome"))
    ctx.hive.route()
    assert sorted(d["to"] for d in result["dispatched"]) == ["ada", "iris", "quill"]
    # The planner gets real work; the others are asked, having nothing to work on.
    assert [m.topic for m in ctx.hive.mailbox("ada").peek()] == ["plan.request"]
    assert [m.topic for m in ctx.hive.mailbox("iris").peek()] == ["peer.ask"]


def test_a_peer_question_is_answered_whatever_the_role_subscribes_to(ctx: AgentContext):
    """A scribe subscribes to `report` only, but anyone may be asked a question."""
    ctx.hive.enroll("nova", "analyst")
    provider = StubProvider("Methods will need the fastp version.")
    quill = Scribe("quill", ctx, provider=provider)
    ctx.hive.send("nova", "quill", "peer.ask",
                  {"question": "What do you need from me?", "hops": 0})
    assert quill.tick() == 1
    ctx.hive.route()
    replies = ctx.hive.mailbox("nova").peek()
    assert [m.topic for m in replies] == ["peer.reply"]
    assert replies[0].body["answer"] == "Methods will need the fastp version."
    assert "What do you need from me?" in provider.prompts[0]


def test_an_answer_is_never_answered_back(ctx: AgentContext):
    """The rule that keeps two agents from talking until the budget is gone."""
    ctx.hive.enroll("vera", "validator")
    nova = Analyst("nova", ctx, provider=StubProvider())
    ctx.hive.send("vera", "nova", "peer.reply", {"answer": "duplicates, most likely"})
    assert nova.tick() == 1
    ctx.hive.route()
    assert ctx.hive.mailbox("vera").peek() == []
    assert "duplicates, most likely" in (
        ctx.hive.mailbox("nova").dir / "memory.md").read_text()


def test_hop_limit_stops_a_forwarded_chain(ctx: AgentContext):
    ctx.hive.enroll("vera", "validator")
    nova = Analyst("nova", ctx, provider=StubProvider())
    ctx.hive.send("vera", "nova", "peer.ask",
                  {"question": "still going?", "hops": Analyst.MAX_HOPS})
    nova.tick()
    ctx.hive.route()
    assert ctx.hive.mailbox("vera").peek() == []
    assert ctx.hive.events("agent.hop_limit")


def test_a_failed_gate_becomes_a_question_for_the_analyst(ctx: AgentContext):
    ctx.hive.enroll("nova", "analyst")
    vera = Validator("vera", ctx)
    verdict = vera.handle(_msg(ctx, "human", "vera", "qc.check",
                               metrics={"mean_quality": 12.0}))
    ctx.hive.route()
    assert verdict["verdict"] == "fail"
    asked = ctx.hive.mailbox("nova").peek()
    assert [m.topic for m in asked] == ["peer.ask"]
    assert asked[0].body["context"]["failures"][0]["metric"] == "mean_quality"


def test_a_passing_gate_asks_nobody_anything(ctx: AgentContext):
    ctx.hive.enroll("nova", "analyst")
    vera = Validator("vera", ctx)
    vera.handle(_msg(ctx, "human", "vera", "qc.check", metrics={"mean_quality": 36.0}))
    ctx.hive.route()
    assert ctx.hive.mailbox("nova").peek() == []


def test_asking_a_role_nobody_fills_is_recorded_not_raised(ctx: AgentContext):
    iris = Curator("iris", ctx)
    assert iris.ask_peer("analyst", "anyone home?") is None
    assert ctx.hive.events("agent.no_peer")


def test_an_agent_without_a_provider_still_answers(ctx: AgentContext):
    ctx.hive.enroll("ada", "planner")
    iris = Curator("iris", ctx)          # builtin: no LLM at all
    ctx.hive.send("ada", "iris", "peer.ask", {"question": "thoughts?"})
    iris.tick()
    ctx.hive.route()
    answer = ctx.hive.mailbox("ada").peek()[0].body["answer"]
    assert "no LLM provider" in answer


def test_transcript_deduplicates_and_orders(ctx: AgentContext):
    ctx.hive.enroll("ada", "planner")
    ctx.hive.enroll("iris", "curator")
    first = ctx.hive.send("ada", "iris", "peer.ask", {"question": "one"})
    second = ctx.hive.send("iris", "ada", "peer.reply", {"answer": "two"})
    lines = ctx.hive.transcript()
    assert [m["id"] for m in lines] == [first.id, second.id]
    assert len({m["id"] for m in lines}) == len(lines)


def _msg(ctx: AgentContext, sender: str, to: str, topic: str, **body):
    """A message object shaped like one the router would have delivered."""
    return ctx.hive.mailbox(sender).send(to, topic, body)


# -- the PI splitting work up ----------------------------------------------
class JSONProvider(StubProvider):
    """A provider that answers with whatever JSON it was constructed with."""

    def __init__(self, payload: dict):
        import json as _json
        super().__init__(_json.dumps(payload))
        self.json_schema = ""


def _staff(hive, **roles):
    for name, role in roles.items():
        hive.enroll(name, role)


def test_pi_without_a_provider_still_splits_the_work(ctx: AgentContext):
    _staff(ctx.hive, ada="planner", iris="curator", vera="validator")
    hugo = PI("hugo", ctx)
    result = hugo.handle(_msg(ctx, "human", "hugo", "goal", goal="assemble a genome"))
    ctx.hive.route()
    assert result["ok"]
    # The planner can always be given real work; the others have no inputs or
    # metrics yet, so they are asked rather than handed an empty job.
    assert [m.topic for m in ctx.hive.mailbox("ada").peek()] == ["plan.request"]
    assert [m.topic for m in ctx.hive.mailbox("iris").peek()] == ["peer.ask"]
    assert [m.topic for m in ctx.hive.mailbox("vera").peek()] == ["peer.ask"]


def test_pi_gives_real_work_when_the_inputs_exist(ctx: AgentContext):
    _staff(ctx.hive, iris="curator", vera="validator")
    hugo = PI("hugo", ctx)
    hugo.handle(_msg(ctx, "human", "hugo", "goal", goal="check this run",
                     inputs={"reads": "/data/s1.fastq"}, metrics={"mean_quality": 31.0}))
    ctx.hive.route()
    assert [m.topic for m in ctx.hive.mailbox("iris").peek()] == ["data.validate"]
    assert [m.topic for m in ctx.hive.mailbox("vera").peek()] == ["qc.check"]


def test_pi_follows_its_own_orders(ctx: AgentContext):
    _staff(ctx.hive, ada="planner", nova="analyst", quill="scribe")
    hugo = PI("hugo", ctx, provider=JSONProvider({
        "reasoning": "Route it first, interpretation can wait.",
        "orders": [{"role": "planner", "instruction": "Route fastq to vcf."},
                   {"role": "analyst", "instruction": "Stand by for the metrics."}]}))
    result = hugo.handle(_msg(ctx, "human", "hugo", "goal", goal="call variants"))
    ctx.hive.route()
    assert [d["role"] for d in result["dispatched"]] == ["planner", "analyst"]
    assert result["reasoning"].startswith("Route it first")
    assert ctx.hive.mailbox("ada").peek()[0].body["instruction"] == "Route fastq to vcf."
    # The scribe was not named in the orders, so it was not tasked.
    assert ctx.hive.mailbox("quill").peek() == []


def test_pi_ignores_an_order_for_a_role_nobody_fills(ctx: AgentContext):
    _staff(ctx.hive, ada="planner")
    hugo = PI("hugo", ctx, provider=JSONProvider({
        "orders": [{"role": "planner", "instruction": "Plan it."},
                   {"role": "wizard", "instruction": "Do magic."}]}))
    result = hugo.handle(_msg(ctx, "human", "hugo", "goal", goal="call variants"))
    assert [d["role"] for d in result["dispatched"]] == ["planner"]
    assert ctx.hive.events("agent.no_peer")


def test_pi_falls_back_when_the_model_answers_with_nonsense(ctx: AgentContext):
    _staff(ctx.hive, ada="planner", iris="curator")
    hugo = PI("hugo", ctx, provider=StubProvider("I would rather not."))
    result = hugo.handle(_msg(ctx, "human", "hugo", "goal", goal="call variants"))
    ctx.hive.route()
    assert [d["role"] for d in result["dispatched"]] == ["planner", "curator"]


def test_a_finished_plan_reaches_the_scribe(ctx: AgentContext):
    _staff(ctx.hive, quill="scribe")
    hugo = PI("hugo", ctx)
    steps = [{"id": "bwa_mem", "capability": "bwa_mem"},
             {"id": "bcftools_call", "capability": "bcftools_call"}]
    result = hugo.handle(_msg(ctx, "ada", "hugo", "plan.request.result",
                              ok=True, workflow={"name": "fastq-to-vcf", "steps": steps}))
    ctx.hive.route()
    assert result["steps"] == ["bwa_mem", "bcftools_call"]
    mail = ctx.hive.mailbox("quill").peek()
    assert [m.topic for m in mail] == ["report"]
    assert len(mail[0].body["tasks"]) == 2


def test_a_failed_plan_becomes_a_question_not_a_silence(ctx: AgentContext):
    _staff(ctx.hive, iris="curator", nova="analyst")
    hugo = PI("hugo", ctx)
    result = hugo.handle(_msg(ctx, "ada", "hugo", "plan.request.result",
                              ok=False, reason="no route found", candidates=["fastqc"]))
    ctx.hive.route()
    assert result["ok"] is False
    assert [m.topic for m in ctx.hive.mailbox("iris").peek()] == ["peer.ask"]
    assert [m.topic for m in ctx.hive.mailbox("nova").peek()] == ["peer.ask"]


def test_a_goal_sent_straight_to_the_planner_is_planned(ctx: AgentContext):
    """What a human does from the dashboard when they message the planner."""
    from bioharness.agents.roles import Planner
    from bioharness.core.registry import Registry as _Registry
    ada = Planner("ada", ctx)
    result = ada.handle(_msg(ctx, "human", "ada", "goal", goal="say hello to the lab"))
    assert result["ok"] is False          # no capabilities registered in this ctx
    assert "goal" in result and result["goal"] == "say hello to the lab"
    assert isinstance(ctx.registry, _Registry)


def test_a_result_is_not_answered_with_another_result(ctx: AgentContext):
    """`plan.request.result.result` is traffic nobody subscribes to."""
    _staff(ctx.hive, quill="scribe", ada="planner")
    hugo = PI("hugo", ctx)
    ctx.hive.send("ada", "hugo", "plan.request.result",
                  {"ok": True, "workflow": {"name": "wf", "steps": [
                      {"id": "fastqc", "capability": "fastqc"}]}})
    assert hugo.tick() == 1
    ctx.hive.route()
    assert [m.topic for m in ctx.hive.mailbox("ada").peek()] == []
    assert [m.topic for m in ctx.hive.mailbox("quill").peek()] == ["report"]


def test_a_peer_question_is_not_an_invitation_to_decline(ctx: AgentContext):
    """A prompt inviting the agent to say a question "falls outside your remit"
    gets exactly that answer from every agent, so the prompt must not say it."""
    ctx.hive.enroll("nova", "analyst")
    provider = StubProvider("PCR duplicates, most likely.")
    quill = Scribe("quill", ctx, provider=provider)
    quill.answer_peer("What explains 31% duplicates?")
    asked = provider.prompts[-1]
    assert "falls outside your remit" not in asked
    assert "Never open by declining" in asked
    assert "name who should take the rest" in asked
    # Refusing is still allowed for the one reason that matters.
    assert "inventing data you do not have" in asked


def test_the_persona_tells_them_to_help_with_adjacent_questions(ctx: AgentContext):
    ctx.hive.enroll("quill", "scribe")
    quill = Scribe("quill", ctx, provider=StubProvider())
    assert "Help anyway" in quill.system_prompt
