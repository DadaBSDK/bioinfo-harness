"""Personalities, greetings and talk that is not about the work.

The point of these is that a lab is people: a new hire gets welcomed, a quiet
room produces gossip, and none of it may run away with the budget.
"""
from __future__ import annotations

import json
import random
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bioharness.agents import persona
from bioharness.agents.base import AgentContext
from bioharness.agents.roles import Analyst, Curator, Scribe
from bioharness.cli import attend, gossip, roster_agents, welcome
from bioharness.core.guardrails import Guardrails
from bioharness.core.hive import Hive
from bioharness.core.registry import Registry
from test_peer_talk import StubProvider


@pytest.fixture
def ctx(tmp_path: Path) -> AgentContext:
    return AgentContext(hive=Hive(tmp_path / "hive"), registry=Registry(),
                        guardrails=Guardrails(), workdir=tmp_path / "agents")


# -- who they are ----------------------------------------------------------
def test_a_persona_is_stable_for_a_name():
    """`ada` must be the same person tomorrow, and on the cluster node."""
    assert persona.make("ada") == persona.make("ada")
    assert persona.make("ada") != persona.make("iris")


def test_personas_spread_across_the_tables():
    """Taking single bytes off one digest put four of six on the same hobby."""
    from collections import Counter
    names = [f"agent{i}" for i in range(400)]
    for field in ("temperament", "interest", "peeve"):
        counts = Counter(persona.make(n)[field] for n in names)
        assert len(counts) >= 10
        assert max(counts.values()) < 3 * min(counts.values())


def test_a_persona_is_written_down_not_recomputed(ctx: AgentContext):
    """Otherwise an agent's personality drifts when the source tables change."""
    ctx.hive.enroll("iris", "curator")
    first = ctx.hive.ensure_persona("iris", persona.make)
    identity = json.loads((ctx.hive.mailbox("iris").dir / "identity.json").read_text())
    assert identity["persona"] == first
    assert ctx.hive.ensure_persona("iris", lambda *a: {"temperament": "different"}) == first


def test_the_persona_reaches_the_prompt(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    iris = Curator("iris", ctx, provider=StubProvider())
    assert "Your name is iris" in iris.system_prompt
    assert iris.persona["interest"] in iris.system_prompt
    # The job description survives alongside the personality.
    assert "data steward" in iris.system_prompt.lower()


# -- saying hello ----------------------------------------------------------
def test_everyone_greets_a_new_arrival(ctx: AgentContext):
    for name, role in (("iris", "curator"), ("quill", "scribe")):
        ctx.hive.enroll(name, role)
    agents = roster_agents(ctx.hive, ctx)
    ctx.hive.enroll("newbie", "analyst")
    agents = roster_agents(ctx.hive, ctx)
    welcome(ctx.hive, agents, "newbie", quiet=True)
    ctx.hive.route()
    mail = ctx.hive.mailbox("newbie").peek()
    assert sorted(m.sender for m in mail) == ["iris", "quill"]
    assert {m.topic for m in mail} == {"greet"}
    assert all(m.body["line"] for m in mail)


def test_a_greeting_is_answered_once(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    ctx.hive.enroll("newbie", "analyst")
    newbie = Analyst("newbie", ctx, provider=StubProvider("Hi! Long story."))
    ctx.hive.send("iris", "newbie", "greet", {"line": "welcome aboard", "role": "analyst"})
    assert newbie.tick() == 1
    ctx.hive.route()
    back = ctx.hive.mailbox("iris").peek()
    assert [m.topic for m in back] == ["greet.reply"]
    # And the reply to a reply is silence, not a third hello.
    iris = Curator("iris", ctx, provider=StubProvider())
    iris.tick()
    ctx.hive.route()
    assert ctx.hive.mailbox("newbie").peek() == []


def test_nobody_greets_themselves(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    iris = Curator("iris", ctx)
    assert iris.greet("iris") is None


# -- talk about nothing ----------------------------------------------------
def test_small_talk_peters_out(ctx: AgentContext):
    """Two colleagues who cannot stop talking are a budget problem."""
    ctx.hive.enroll("iris", "curator")
    ctx.hive.enroll("quill", "scribe")
    iris = Curator("iris", ctx, provider=StubProvider("kettle's broken again"))
    quill = Scribe("quill", ctx, provider=StubProvider("it always is"))
    iris.small_talk("quill")
    exchanges = 0
    for _ in range(10):
        ctx.hive.route()
        handled = quill.tick() + iris.tick()
        if not handled:
            break
        exchanges += handled
    assert 0 < exchanges <= Curator.SOCIAL_HOPS + 1, "the chat never died out"


def test_an_agent_with_no_llm_still_has_something_to_say(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    ctx.hive.enroll("quill", "scribe")
    iris = Curator("iris", ctx)              # builtin: no provider at all
    msg = iris.small_talk("quill")
    assert msg is not None and len(msg.body["line"]) > 10
    assert "quill" in msg.body["line"] or iris.persona["haunt"] in msg.body["line"] \
        or iris.persona["peeve"] in msg.body["line"] \
        or iris.persona["interest"] in msg.body["line"]


def test_gossip_prefers_the_chatty_ones(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    ctx.hive.enroll("quill", "scribe")
    agents = roster_agents(ctx.hive, ctx)
    agents[0].persona["chattiness"] = 0.99
    agents[1].persona["chattiness"] = 0.01
    rng = random.Random(7)
    starters = []
    for _ in range(30):
        gossip(ctx.hive, agents, rng, quiet=True)
        starters.append(ctx.hive.events("social.gossip")[-1]["payload"]["from"])
    assert starters.count(agents[0].name) > starters.count(agents[1].name)


def test_social_talk_can_be_switched_off(tmp_path: Path):
    hive_root = tmp_path / "hive"
    hive = Hive(hive_root)
    hive.enroll("iris", "curator", provider="builtin")
    hive.enroll("quill", "scribe", provider="builtin")
    stop = threading.Event()
    thread = threading.Thread(target=attend, daemon=True, args=(str(hive_root), []),
                              kwargs={"interval": 0.05, "stop": stop, "quiet": True,
                                      "social_every": 0})
    thread.start()
    time.sleep(1.0)
    stop.set()
    thread.join(timeout=5)
    assert not hive.events("social.gossip")


def test_a_newcomer_is_welcomed_by_the_running_lab(tmp_path: Path):
    hive_root = tmp_path / "hive"
    hive = Hive(hive_root)
    hive.enroll("iris", "curator", provider="builtin")
    hive.enroll("quill", "scribe", provider="builtin")
    stop = threading.Event()
    thread = threading.Thread(target=attend, daemon=True, args=(str(hive_root), []),
                              kwargs={"interval": 0.05, "stop": stop, "quiet": True,
                                      "social_every": 0})
    thread.start()
    try:
        time.sleep(0.3)
        hive.enroll("newbie", "analyst", provider="builtin")
        deadline = time.time() + 8
        while time.time() < deadline and not hive.events("social.welcome"):
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=5)
    welcomes = hive.events("social.welcome")
    assert welcomes, "nobody went over to say hello"
    assert welcomes[0]["payload"]["newcomer"] == "newbie"
    assert sorted(welcomes[0]["payload"]["by"]) == ["iris", "quill"]
    # And the newcomer answered them.
    assert any(e["payload"].get("from") == "newbie"
               for e in hive.events("message.routed"))


# -- the invented lab stays invented ---------------------------------------
def test_gossip_never_comes_back_as_evidence(ctx: AgentContext):
    """The failure this guards against: an agent explaining a QC failure with a
    freezer incident that happened only in small talk."""
    ctx.hive.enroll("iris", "curator")
    ctx.hive.enroll("nova", "analyst")
    nova = Analyst("nova", ctx, provider=StubProvider())
    nova.remember("iris says someone left the -80 freezer door cracked again",
                  heading="gossip", social=True)
    nova.remember("Run r-12 duplication rate was 31 percent", heading="run r-12")

    work = nova.recall("freezer duplication")
    assert any("duplication" in r["text"] for r in work)
    assert not any("-80" in r["text"] for r in work), \
        "small talk surfaced as context for the work"
    # It is still the agent's memory -- just not evidence.
    social = nova.recall("freezer", kinds=("social",))
    assert any("-80" in r["text"] for r in social)


def test_social_memory_is_kept_in_its_own_file(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    iris = Curator("iris", ctx)
    iris.remember("the kettle is broken again", heading="kitchen", social=True)
    iris.remember("blocked run: reads.fastq was empty", heading="curation")
    box = ctx.hive.mailbox("iris").dir
    assert "kettle" in (box / "social.md").read_text()
    assert "kettle" not in (box / "memory.md").read_text()
    assert "reads.fastq" in (box / "memory.md").read_text()


def test_a_greeting_is_social_not_work(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    ctx.hive.enroll("newbie", "analyst")
    newbie = Analyst("newbie", ctx, provider=StubProvider("hi, I'm by the window"))
    ctx.hive.send("iris", "newbie", "greet", {"line": "welcome!", "role": "analyst"})
    newbie.tick()
    assert not newbie.recall("welcome"), "a hello turned into work context"
    assert newbie.recall("welcome", kinds=("social",))


def test_the_persona_says_the_lab_is_not_real(ctx: AgentContext):
    ctx.hive.enroll("iris", "curator")
    iris = Curator("iris", ctx, provider=StubProvider())
    assert "no hands" in iris.system_prompt
    assert "provenance ledger" in iris.system_prompt


def test_a_work_prompt_carries_the_guard_but_small_talk_does_not(ctx: AgentContext):
    ctx.hive.enroll("nova", "analyst")
    provider = StubProvider("fine")
    nova = Analyst("nova", ctx, provider=provider)
    nova.answer_peer("What do 31% duplicates mean?")
    assert "No scene-setting" in provider.prompts[-1]
    nova.small_talk("iris")
    assert "No scene-setting" not in provider.prompts[-1], \
        "small talk is supposed to be about the room"


def test_an_agent_is_primed_with_its_own_memory_only(ctx: AgentContext):
    """Agents were answering in each other's voices about work they never did."""
    ctx.hive.enroll("nova", "analyst")
    ctx.hive.enroll("quill", "scribe")
    ctx.hive.append_memory("quill", "I drafted Methods for run r-9 using fastp.")
    nova = Analyst("nova", ctx, provider=StubProvider())
    nova.remember("Run r-9 duplication rate was 31 percent")
    assert not any(r["agent"] == "quill" for r in nova.recall("run r-9"))
    assert any(r["agent"] == "quill" for r in nova.recall("run r-9", everyone=True))
