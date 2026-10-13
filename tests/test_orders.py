"""Orders from the operator, and the brief every order produces.

The operator is the one voice in this lab that cannot be ignored: an order is
read first, is never dropped for being off a role's topic list, and every agent
owes it a verdict. What they said is then one PDF per order.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bioharness.agents.base import AgentContext, OPERATOR_AUTHORITY
from bioharness.agents.roles import PI, Curator, Scribe
from bioharness.core import brief as brief_mod
from bioharness.core.guardrails import Guardrails
from bioharness.core.hive import Hive
from bioharness.core.registry import Registry
from bioharness.tools.pdf import Pdf, latin
from test_peer_talk import JSONProvider, StubProvider


@pytest.fixture
def ctx(tmp_path: Path) -> AgentContext:
    return AgentContext(hive=Hive(tmp_path / "hive"), registry=Registry(),
                        guardrails=Guardrails(), workdir=tmp_path / "agents",
                        runs=tmp_path / "runs")


def order(ctx: AgentContext, to: str, text: str):
    return ctx.hive.send("human", to, "directive", {"order": text})


def test_an_order_reaches_a_role_that_subscribes_to_nothing_like_it(ctx: AgentContext):
    """A scribe subscribes to `report`. An order is still an order."""
    ctx.hive.enroll("quill", "scribe")
    quill = Scribe("quill", ctx, provider=JSONProvider(
        {"verdict": "will do", "position": "I will draft Methods from the ledger.",
         "needs": ["the run id"], "risk": ""}))
    order(ctx, "quill", "Write up what the lab did today")
    assert quill.tick() == 1
    ctx.hive.route()
    back = ctx.hive.mailbox("human").peek()
    assert [m.topic for m in back] == ["verdict"]
    assert back[0].body["verdict"] == "will do"
    assert back[0].body["needs"] == ["the run id"]


def test_a_human_message_on_an_unknown_topic_is_still_answered(ctx: AgentContext):
    """Operator mail on any topic is answered."""
    ctx.hive.enroll("iris", "curator")
    iris = Curator("iris", ctx, provider=JSONProvider(
        {"verdict": "cannot do", "position": "No inputs are declared yet."}))
    ctx.hive.send("human", "iris", "tidy.the.bench", {"note": "sort the samples"})
    assert iris.tick() == 1
    ctx.hive.route()
    assert [m.topic for m in ctx.hive.mailbox("human").peek()] == ["verdict"]


def test_the_operator_is_read_before_the_gossip(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    provider = JSONProvider({"verdict": "will do", "position": "On it."})
    iris = Curator("iris", ctx, provider=provider)
    ctx.hive.send("quill", "iris", "smalltalk", {"line": "kettle's broken"})
    order(ctx, "iris", "Check the reads for run 12")
    iris.tick()
    assert "Check the reads" in provider.prompts[0]


def test_an_order_cannot_make_an_agent_invent_data(ctx: AgentContext):
    assert "state something as fact that the data does not show" in OPERATOR_AUTHORITY
    assert "cannot do" in OPERATOR_AUTHORITY


def test_the_authority_block_is_in_every_agents_prompt(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    assert "The human operator runs this lab" in Curator("iris", ctx).system_prompt


def test_a_pi_acts_on_an_order_as_well_as_answering_it(ctx: AgentContext):
    ctx.hive.enroll("ada", "planner")
    hugo = PI("hugo", ctx, provider=JSONProvider(
        {"verdict": "will do", "position": "Routed to the planner."}))
    order(ctx, "hugo", "Call variants in the new sample")
    hugo.tick()
    ctx.hive.route()
    assert [m.topic for m in ctx.hive.mailbox("ada").peek()] == ["plan.request"]
    verdicts = [m for m in ctx.hive.mailbox("human").peek() if m.topic == "verdict"]
    assert verdicts and verdicts[0].body["acted"]["dispatched"]


def test_everything_said_in_answer_stays_in_the_orders_thread(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    iris = Curator("iris", ctx, provider=JSONProvider(
        {"verdict": "will do", "position": "Checking now."}))
    given = order(ctx, "iris", "Check the reference genome")
    iris.tick()
    ctx.hive.route()
    threads = {m["thread"] for m in ctx.hive.transcript()}
    assert threads == {given.id}, "the answer left the order's thread"


def test_a_message_that_answers_nothing_starts_its_own_thread(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    msg = ctx.hive.send("iris", "human", "peer.reply", {"answer": "hello"})
    assert msg.thread == msg.id


def _brief_for(ctx: AgentContext, text: str = "Assess the new run") -> dict:
    ctx.hive.enroll("iris", "curator")
    ctx.hive.enroll("vera", "validator")
    iris = Curator("iris", ctx, provider=JSONProvider(
        {"verdict": "will do", "position": "Inputs check out.", "needs": ["paths"]}))
    vera = Curator("vera", ctx, provider=JSONProvider(
        {"verdict": "cannot do", "position": "No metrics exist yet.",
         "risk": "gating on nothing"}))
    given = order(ctx, "*", text)
    ctx.hive.route()
    iris.tick(); vera.tick()
    ctx.hive.route()
    return brief_mod.gather(ctx.hive, given.thread)


def test_a_brief_collects_every_agents_verdict(ctx: AgentContext):
    brief = _brief_for(ctx)
    assert brief["order"] == "Assess the new run"
    assert [v["agent"] for v in brief["verdicts"]] == ["vera", "iris"], \
        "refusals should be read first"
    assert brief["verdicts"][0]["risk"] == "gating on nothing"


def test_a_brief_renders_a_pdf_that_opens(ctx: AgentContext, tmp_path: Path):
    brief = _brief_for(ctx)
    pdf = brief_mod.render(brief, tmp_path / "briefs")
    raw = pdf.read_bytes()
    assert raw.startswith(b"%PDF-1.4") and raw.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in raw and b"xref" in raw
    assert pdf.name == f"brief-{brief['thread']}.pdf"


def test_an_order_with_no_answers_still_briefs(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    given = order(ctx, "iris", "Do nothing at all")
    brief = brief_mod.gather(ctx.hive, given.thread)
    assert brief["verdicts"] == []
    assert brief["order"] == "Do nothing at all"


def test_orders_lists_what_the_operator_asked_for(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    order(ctx, "iris", "first")
    order(ctx, "iris", "second")
    assert [o["order"] for o in brief_mod.orders(ctx.hive)] == ["first", "second"]


def test_typographic_punctuation_survives_as_something_readable():
    assert latin("mean — quality ‘24’ … 30 %") == "mean - quality '24' ... 30 %"


def test_a_long_document_paginates():
    doc = Pdf(title="long")
    for i in range(200):
        doc.para(f"line {i} " * 20)
    raw = doc._render()
    assert raw.count(b"/Type /Page ") > 3
    assert b"/Count " in raw


def test_a_full_operator_mailbox_does_not_look_like_a_busy_lab(ctx: AgentContext):
    """Pending mail in the operator's own inbox does not block a brief."""
    from bioharness.cli import settled_orders
    ctx.hive.enroll("iris", "curator")
    given = order(ctx, "iris", "check the reads")
    ctx.hive.route()
    Curator("iris", ctx, provider=JSONProvider(
        {"verdict": "will do", "position": "ok"})).tick()
    ctx.hive.route()
    assert ctx.hive.mailbox("human").pending() > 0
    assert settled_orders(ctx.hive, set(), settle_after=0) == [given.thread]
    assert settled_orders(ctx.hive, {given.thread}, settle_after=0) == []


def test_a_brief_leads_with_what_actually_happened(ctx: AgentContext):
    """The brief states first whether anything actually ran."""
    ctx.hive.enroll("hugo", "pi")
    given = order(ctx, "hugo", "fetch the banana data and run it")
    ctx.hive.send("hugo", "human", "verdict",
                  {"verdict": "will do with caveats", "position": "Scoping it.",
                   "needs": ["an ENA run accession", "a reference genome URL"],
                   "role": "pi"}, thread=given.thread)
    brief = brief_mod.gather(ctx.hive, given.thread)
    assert brief["outcome"].startswith("Nothing was executed")
    assert "waiting on the operator" in brief["outcome"]
    assert brief["asked_of_operator"] == ["an ENA run accession",
                                          "a reference genome URL"]


def test_a_brief_says_when_the_order_was_carried_out(ctx: AgentContext):
    ctx.hive.enroll("rex", "runner")
    given = order(ctx, "rex", "run the qc pipeline")
    ctx.hive.event("workflow.finished", {"run_id": "run-1", "workflow": "qc",
                                         "status": "ok", "tasks": []})
    brief = brief_mod.gather(ctx.hive, given.thread)
    assert "1 run(s) finished" in brief["outcome"]
    assert "carried out" in brief["outcome"]


def test_a_brief_counts_the_files_a_run_actually_wrote(ctx: AgentContext, tmp_path: Path):
    """Artifact counts are read from the run's own ledger."""
    ctx.hive.enroll("rex", "runner")
    given = order(ctx, "rex", "fetch the reads")
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "ledger.jsonl").write_text(
        '{"event": "artifact.created", "payload": {"path": "/runs/run-1/a.fastq"}}\n'
        '{"event": "task.ok", "payload": {}}\n')
    ctx.hive.event("workflow.finished", {"run_id": "run-1", "workflow": "fetch",
                                         "status": "ok", "dir": str(run_dir),
                                         "tasks": []})
    brief = brief_mod.gather(ctx.hive, given.thread)
    assert brief["produced"][0]["path"].endswith("a.fastq")
    assert "The order was carried out" in brief["outcome"]
