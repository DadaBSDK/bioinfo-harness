"""The lab as a place: rooms, what is in them, and who sits where.

Without a map, agents furnish the building themselves. Ours invented a -80 with
a broken alarm, a coin-eating coffee machine and a third-floor printer inside an
hour -- charming, and no two agents ever agreed on where any of it was. This
module is the single answer to "what is here and where is it", shared by the
prompt an agent sees and the floor the operator watches.

The room ids and labels match `ROOMS` in `ui/app.js`. If a room moves, it moves
in both, or the lab an agent talks about stops being the lab you can see.
"""
from __future__ import annotations

from typing import Any

ROOMS: list[dict[str, Any]] = [
    {
        "id": "planning", "label": "Planning Office", "roles": ["pi", "planner"],
        "instruments": [
            "the do-not-erase whiteboard",
            "two desks with dual monitors",
            "a wall clock that runs four minutes fast",
            "a pinned-up coverage plot nobody has taken down",
            "a filing cabinet of old grant drafts",
        ],
    },
    {
        "id": "seq", "label": "Sequencing Bay", "roles": ["curator"],
        "instruments": [
            "the benchtop sequencer",
            "the -80 freezer with the temperamental alarm",
            "a thermocycler",
            "a TapeStation",
            "a Qubit fluorometer",
            "the pipette bank and tip racks",
            "a sample fridge that is always too full",
        ],
    },
    {
        "id": "qc", "label": "QC Bench", "roles": ["validator"],
        "instruments": [
            "the microscope",
            "a plate reader",
            "the good centrifuge",
            "a gel imager",
            "a vortex mixer",
            "the screen with the QC dashboards on it",
        ],
    },
    {
        "id": "compute", "label": "Compute Cluster", "roles": ["runner", "orchestrator"],
        "instruments": [
            "four racks of compute nodes",
            "the head-node terminal",
            "a tape backup library",
            "the UPS that beeps during storms",
            "a KVM cart with a sticky spacebar",
            "the cold aisle, where it is always too loud to talk",
        ],
    },
    {
        "id": "structure", "label": "Structure Lab", "roles": ["analyst"],
        "instruments": [
            "a GPU workstation",
            "twin monitors running a molecular viewer",
            "a 3D-printed protein model used as a paperweight",
            "a whiteboard covered in binding poses",
            "the window seat, which is the best seat in the building",
        ],
    },
    {
        "id": "writeup", "label": "Write-up Desk", "roles": ["scribe"],
        "instruments": [
            "the printer, which jams on figures",
            "a shelf of reference manuals nobody opens",
            "a standing lamp",
            "a mug collection of disputed ownership",
            "the citation whiteboard",
        ],
    },
    {
        "id": "lobby", "label": "Waiting Lobby", "roles": ["operator"],
        "instruments": [
            "two sofas",
            "low tables and a tired rubber plant",
            "the coffee machine that eats coins",
            "the noticeboard with the coffee rota on it",
        ],
    },
]

BY_ROLE = {role: room for room in ROOMS for role in room["roles"]}
BY_ID = {room["id"]: room for room in ROOMS}


def room_for(role: str) -> dict[str, Any]:
    """Where someone in this role works. Unknown roles get the lobby."""
    return BY_ROLE.get(role, BY_ID["lobby"])


def landmarks(role: str = "") -> list[str]:
    """Things a person in this role could plausibly be standing next to."""
    return list(room_for(role)["instruments"]) if role else \
        [i for room in ROOMS for i in room["instruments"]]


def map_prompt(role: str = "", name: str = "") -> str:
    """The floor plan, as an agent sees it."""
    lines = [f"# {name}" if name else "# The lab",
             (f"This is the whole building, {name}. " if name else "This is the whole building. ")
             + "It has these rooms and nothing else:"]
    for room in ROOMS:
        who = ", ".join(room["roles"]) or "nobody"
        lines.append(f"- **{room['label']}** ({who}): "
                     + "; ".join(room["instruments"]) + ".")
    if role:
        mine = room_for(role)
        lines += ["", f"You work in the {mine['label']}."]
    lines += [
        "",
        "There is no other room, floor, instrument or piece of equipment. Do not "
        "invent one -- no third floor, no cold room, no machine that is not on "
        "this list. If you need something the lab does not have, say that it "
        "does not have it.",
    ]
    return "\n".join(lines)


def whereabouts(roster: list[dict[str, Any]], me: str = "") -> str:
    """Who your colleagues are and where each of them sits."""
    others = [a for a in roster if a.get("name") != me]
    if not others:
        return ""
    lines = ["# Who else is here"]
    for a in sorted(others, key=lambda x: str(x.get("name"))):
        room = room_for(str(a.get("role", "")))
        line = f"- {a['name']} ({a.get('role', '?')}) — {room['label']}"
        persona = a.get("persona") or {}
        if persona.get("temperament"):
            line += f"; {persona['temperament']}"
        lines.append(line)
    lines.append("Address a colleague by name, not by role, and say where they "
                 "are only if you actually need to.")
    return "\n".join(lines)
