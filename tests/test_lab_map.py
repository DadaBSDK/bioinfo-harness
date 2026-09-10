"""The lab as a shared place: one map, and agents who know where people are."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bioharness.agents import lab, persona
from bioharness.agents.base import AgentContext
from bioharness.agents.roles import ROLES, Curator
from bioharness.core.guardrails import Guardrails
from bioharness.core.hive import Hive
from bioharness.core.registry import Registry

UI = Path(__file__).resolve().parents[1] / "bioharness" / "ui" / "app.js"


@pytest.fixture
def ctx(tmp_path: Path) -> AgentContext:
    return AgentContext(hive=Hive(tmp_path / "hive"), registry=Registry(),
                        guardrails=Guardrails(), workdir=tmp_path / "agents")


def test_every_role_has_a_room():
    for role in ROLES:
        assert lab.room_for(role)["id"] != "lobby", f"{role} has nowhere to work"


def test_the_map_matches_the_floor_the_operator_sees():
    """If a room moves it moves in both, or the lab an agent talks about stops
    being the lab you can watch."""
    drawn = UI.read_text()
    for room in lab.ROOMS:
        assert f"id: '{room['id']}'" in drawn, f"{room['id']} is not on the floor"


def test_a_persona_haunts_its_own_room():
    for role in ROLES:
        p = persona.make("someone", role)
        assert p["haunt"] in lab.landmarks(role)
        assert p["room"] == lab.room_for(role)["label"]


def test_speech_reads_as_second_person():
    """These follow "You ..." in the prompt; a third-person entry produced
    "You over-explains"."""
    for line in persona.SPEECH:
        assert not line.split()[0].endswith("s") or line.startswith("swear"), line


def test_peeves_point_at_things_the_lab_has():
    everything = " ".join(i for room in lab.ROOMS for i in room["instruments"])
    for peeve in persona.PEEVES:
        for invented in ("third floor", "cold room", "fume hood", "fire drill"):
            assert invented not in peeve, f"{peeve!r} is not in this building"
    assert "-80" in everything and "printer" in everything


def test_an_agent_knows_the_building_and_who_is_in_it(ctx: AgentContext):
    ctx.hive.enroll("hugo", "pi")
    ctx.hive.enroll("rex", "runner")
    iris = Curator("iris", ctx)
    prompt = iris.system_prompt
    assert "Sequencing Bay" in prompt and "You work in the Sequencing Bay" in prompt
    assert "hugo (pi) — Planning Office" in prompt
    assert "rex (runner) — Compute Cluster" in prompt
    assert "iris (curator) —" not in prompt          # it knows where it is already
    assert "Do not invent one" in prompt


def test_the_map_forbids_inventing_rooms(ctx: AgentContext):
    assert "no third floor" in lab.map_prompt("curator")


def test_whereabouts_is_empty_for_a_lab_of_one(ctx: AgentContext):
    assert lab.whereabouts([{"name": "iris", "role": "curator"}], me="iris") == ""
