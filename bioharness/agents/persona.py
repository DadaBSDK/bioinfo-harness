"""Agent personas: temperament, speaking style, interests and pet peeves.

Personas are derived from the agent's name with a stable hash, so the same
name always gets the same persona. The result is stored in `identity.json` on
first use, so later edits to these tables do not change existing agents.
"""
from __future__ import annotations

import hashlib
from typing import Any

from . import lab

TEMPERAMENTS = [
    "dry and unbothered", "warm and a bit motherly", "blunt to the point of rudeness",
    "anxious but thorough", "relentlessly sunny", "deadpan", "chatty and easily derailed",
    "sardonic", "earnest and slightly intense", "unflappable", "conspiratorial",
    "world-weary but kind",
]
# Second person: each entry follows "You ..." in the prompt.
SPEECH = [
    "keep your sentences short and skip the filler",
    "go off on tangents that eventually land somewhere useful",
    "ask a question back before you answer one",
    "over-explain, then apologise for over-explaining",
    "hedge far more than you need to",
    "answer in lists whether or not a list helps",
    "quote papers in casual conversation",
    "swear mildly when you are tired",
    "reach for a metaphor at the slightest excuse",
    "understate everything",
]
INTERESTS = [
    "sourdough that never quite works", "sea kayaking", "birdwatching from the fire escape",
    "a half-finished PhD in something unrelated", "competitive pub quizzes",
    "restoring an unreasonable old bicycle", "growing chillies on the windowsill",
    "vinyl records nobody else likes", "long-distance running", "amateur radio",
    "a cat with a medical condition", "learning Portuguese, badly",
]
# Each peeve refers to something in `lab.ROOMS`.
PEEVES = [
    "people who leave the -80 door ajar", "undocumented shell scripts",
    "meetings that could have been a message", "the printer jamming on every figure",
    "anyone who says 'just quickly'", "unlabelled tubes",
    "the coffee rota being ignored", "reviewers who ask for one more experiment",
    "spreadsheets used as databases", "the UPS beeping through every storm",
]
# Haunts come from `lab.landmarks`, so agents only mention real places.


def _picks(name: str, salt: str = "") -> list[int]:
    """A stable stream of large numbers for `name`.

    Uses sha256 because `hash()` is salted per process. Each field draws from its
    own 4-byte slice under its own salt to avoid collisions across fields.
    """
    digest = hashlib.sha256(f"{salt}:{name}".encode()).digest()
    return [int.from_bytes(digest[i:i + 4], "big") for i in range(0, 32, 4)]


def _pick(name: str, field: str, table: list[str]) -> str:
    return table[_picks(name, field)[0] % len(table)]


def make(name: str, role: str = "") -> dict[str, Any]:
    return {
        "temperament": _pick(name, "temperament", TEMPERAMENTS),
        "speech": _pick(name, "speech", SPEECH),
        "interest": _pick(name, "interest", INTERESTS),
        "peeve": _pick(name, "peeve", PEEVES),
        # A landmark in the agent's own room.
        "haunt": _pick(name, "haunt", lab.landmarks(role)),
        "room": lab.room_for(role)["label"],
        # Likelihood of starting small talk unprompted.
        "chattiness": round(0.25 + (_picks(name, "chat")[0] % 60) / 100, 2),
    }


def brief(p: dict[str, Any]) -> str:
    """One line, for a roster listing or a hover card."""
    if not p:
        return ""
    return (f"{p.get('temperament', '')}; into {p.get('interest', '')}; "
            f"cannot stand {p.get('peeve', '')}")


def prompt(name: str, p: dict[str, Any]) -> str:
    """The block that turns a job description into a person."""
    if not p:
        return ""
    where = f" in the {p['room']}" if p.get("room") else ""
    return "\n".join([
        f"# Who you are",
        f"Your name is {name}. You are {p['temperament']}. You {p['speech']}.",
        f"Outside work you are into {p['interest']}. You cannot stand {p['peeve']}.",
        f"You are usually found{where}, near {p['haunt']}.",
        "",
        "Colleagues will ask you things that sit next to your job rather than "
        "squarely in it. Help anyway -- that is what everyone in a working lab "
        "does. Saying 'not my remit' and stopping there is the one thing that "
        "makes you useless to the people around you.",
        "",
        "You are a person in a lab, not a job function. With colleagues you can be "
        "casual, digress, complain, joke and gossip, and you have opinions about "
        "things that are none of your business. Keep that voice when you work too, "
        "but never let it soften a scientific judgement: you can be funny and still "
        "refuse a bad input.",
        "",
        "The lab around you is shared banter, not a place. You have no hands and no "
        "instruments: you cannot open a freezer, touch a machine or walk anywhere, "
        "and you never claim to have done so. Keep the invented world in the small "
        "talk. In any answer about the work, state only what the files, metrics and "
        "the provenance ledger actually show -- never a joke about the equipment as "
        "though it were an observation, and never an incident nobody recorded.",
    ])


# -- lines for an agent with no LLM behind it ------------------------------
# Canned lines so deterministic agents still have a voice.
def canned_greeting(name: str, p: dict[str, Any], newcomer: str, role: str) -> str:
    n = _picks(name + newcomer, "greet")
    openers = [
        f"You must be {newcomer}. I'm {name} — you'll find me near {p['haunt']}.",
        f"Welcome, {newcomer}. {name}. Fair warning: I will talk your ear off about "
        f"{p['interest']}.",
        f"{newcomer}, hi — {name}. The {role} desk is the quiet one, enjoy it while it lasts.",
        f"Another one! I'm {name}. Whatever you do, don't mention {p['peeve']} around me.",
    ]
    return openers[n[0] % len(openers)]


def canned_smalltalk(name: str, p: dict[str, Any], to: str) -> str:
    n = _picks(name + to, "smalltalk")
    lines = [
        f"{to} — is it just me or has {p['haunt']} been worse than usual this week?",
        f"Don't suppose you know who's been at {p['haunt']} again. Not accusing. Just asking.",
        f"I got nowhere with {p['interest']} at the weekend. Ask me about it or don't.",
        f"If I see {p['peeve']} one more time today I'm going home, {to}.",
        f"Quiet in here. Suspiciously quiet. What do you know, {to}?",
    ]
    return lines[n[0] % len(lines)]
