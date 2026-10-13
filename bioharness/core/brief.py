"""The verdict brief: what the lab said about an order, as a PDF.

Every order the operator gives ends up as one document -- the order itself, then
each agent's verdict in its own words, then what was actually run and what it
cost. It exists because a conversation scrolling past in a browser is not a
record you can take to anyone, and because "what did the lab think of this?"
should have exactly one answer per order.

The whole thing is reconstructed from the hive on disk: no state is kept for it,
so a brief can be rendered again months later from a run directory alone.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..tools.pdf import Pdf
from .hive import Hive

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
VERDICT_ORDER = {"cannot do": 0, "will do with caveats": 1, "will do": 2}


def line_of(m: dict[str, Any]) -> str:
    """One line of plain English for a message.

    Falls back to compact JSON rather than to nothing: an unrecognised topic
    should still show what was actually said.
    """
    topic = m.get("topic", "")
    body = m.get("body", {}) or {}
    # An answer carries the question it answers, so `answer` is checked first --
    # otherwise every reply reads as an echo of what was asked. `instruction` is
    # what a dispatched agent was told to do; without it a tasking message
    # renders as the whole JSON body of its work item.
    for key in ("position", "line", "answer", "question", "instruction", "goal",
                "order", "summary", "reason", "error"):
        if body.get(key):
            return str(body[key]).strip()
    if topic.endswith(".result") or topic in ("data.validate", "qc.check"):
        bits = []
        if "verdict" in body:
            bits.append(str(body["verdict"]))
        if "ok" in body:
            bits.append("ok" if body["ok"] else "not ok")
        for key in ("problems", "failures", "briefed", "candidates"):
            if body.get(key):
                bits.append(f"{len(body[key])} {key}")
        if body.get("workflow"):
            wf = body["workflow"]
            steps = wf.get("steps", []) if isinstance(wf, dict) else []
            bits.append(" -> ".join(s.get("capability", "?") for s in steps) or "workflow")
        if bits:
            return ", ".join(bits)
    text = json.dumps(body, default=str)
    return text if len(text) <= 220 else text[:217] + "..."


def orders(hive: Hive, limit: int = 25) -> list[dict[str, Any]]:
    """Every order the operator has given, newest last."""
    out = []
    for m in hive.transcript(limit=1000):
        if m.get("sender") != "human" or not m.get("thread"):
            continue
        if m["thread"] != m.get("id"):
            continue                      # a follow-up inside someone's thread
        out.append({"thread": m["thread"], "ts": float(m.get("ts", 0.0)),
                    "to": m.get("recipient", ""), "topic": m.get("topic", ""),
                    "order": line_of(m)})
    return out[-limit:]


def gather(hive: Hive, thread: str) -> dict[str, Any]:
    """Everything that happened because of one order."""
    msgs = [m for m in hive.transcript(limit=4000) if m.get("thread") == thread]
    msgs.sort(key=lambda m: float(m.get("ts", 0.0)))
    if not msgs:
        return {}
    first = msgs[0]
    roles = {a["name"]: a for a in hive.roster()}

    verdicts: dict[str, dict[str, Any]] = {}
    for m in msgs:
        if m.get("topic") != "verdict":
            continue
        body = m.get("body", {}) or {}
        verdicts[m["sender"]] = {
            "agent": m["sender"],
            "role": body.get("role") or roles.get(m["sender"], {}).get("role", ""),
            "verdict": body.get("verdict", ""),
            "position": body.get("position", ""),
            "needs": body.get("needs") or [],
            "risk": body.get("risk", ""),
            "acted": body.get("acted"),
            "ts": float(m.get("ts", 0.0)),
        }

    # Anyone who spoke in the thread but never returned a verdict still belongs
    # in the brief -- silence from an agent is a finding, not an omission.
    said: dict[str, str] = {}
    for m in msgs:
        if m["sender"] in ("human",) or m.get("topic") == "verdict":
            continue
        said.setdefault(m["sender"], line_of(m))

    started, ended = float(first.get("ts", 0.0)), float(msgs[-1].get("ts", 0.0))
    runs = [e["payload"] for e in hive.events("workflow.finished")
            if started <= e["ts"] <= ended + 5]

    # Artifacts are recorded in each run's own ledger rather than in the hive,
    # so they are read back from the run directory. A brief that says "0
    # artifacts" while three files sit on disk is worse than no brief.
    artifacts: list[dict[str, Any]] = []
    for run in runs:
        ledger = Path(str(run.get("dir", ""))) / "ledger.jsonl"
        if not ledger.is_file():
            continue
        for line in ledger.read_text(errors="replace").splitlines():
            if '"artifact.created"' not in line:
                continue
            try:
                artifacts.append(json.loads(line)["payload"])
            except (json.JSONDecodeError, KeyError):
                continue
    charges = [e["payload"] for e in hive.events("cost.charged")
               if started <= e["ts"] <= ended + 5]
    spend: dict[str, dict[str, float]] = {}
    for c in charges:
        acc = spend.setdefault(c.get("agent", "?"),
                               {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0})
        acc["calls"] += 1
        acc["tokens_in"] += c.get("tokens_in", 0)
        acc["tokens_out"] += c.get("tokens_out", 0)
        acc["usd"] += c.get("usd", 0.0)

    ran_ok = [r for r in runs if r.get("status") == "ok"]
    # What the operator has to supply, gathered from wherever it was said: the
    # PI's held plan, the runner's refusal, and every verdict's `needs`.
    asked: list[str] = []
    # The precise ones first -- a `needs` message names parameters the operator
    # can literally type back, while a verdict describes them in a sentence.
    for topics in (("needs",), ("verdict",), None):
        for m in msgs:
            topic = m.get("topic", "")
            if topics is None:
                if topic in ("needs", "verdict") or not topic.endswith(".result"):
                    continue
            elif topic not in topics:
                continue
            for need in (m.get("body", {}) or {}).get("needs") or []:
                if str(need) not in asked:
                    asked.append(str(need))
    if ran_ok:
        outcome = (f"{len(ran_ok)} run(s) finished and "
                   f"{len(artifacts)} file(s) were produced. "
                   f"The order was carried out.")
    elif runs:
        outcome = (f"{len(runs)} run(s) started and none finished clean. "
                   f"Nothing usable was produced.")
    elif asked:
        outcome = ("Nothing was executed. The lab is waiting on the operator "
                   "for the inputs listed below.")
    else:
        outcome = "Nothing was executed under this order."

    return {
        "thread": thread,
        "order": line_of(first),
        "outcome": outcome,
        "produced": artifacts,
        "asked_of_operator": asked,
        "issued_to": first.get("recipient", ""),
        "issued_at": started,
        "settled_at": ended,
        "messages": len(msgs),
        "roster": [{"name": a["name"], "role": a.get("role", ""),
                    "provider": a.get("provider", ""), "model": a.get("model", ""),
                    "autonomy": a.get("autonomy", "")}
                   for a in hive.roster() if a.get("role") != "operator"],
        "verdicts": sorted(verdicts.values(),
                           key=lambda v: (VERDICT_ORDER.get(v["verdict"], 9), v["agent"])),
        "said_without_verdict": said,
        "runs": runs,
        "spend": spend,
    }


def stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "—"


def render(brief: dict[str, Any], out_dir: str | Path) -> Path:
    """Write the brief as a PDF and return where it landed."""
    doc = Pdf(title=f"Verdict brief — {brief.get('order', '')[:60]}",
              subject="bioharness")
    doc.heading("Verdict brief")
    doc.para("Every agent's position on one order from the operator, with what "
             "was run because of it.", gray=0.4)
    doc.rule()

    doc.section("Outcome", rule=False)
    doc.para(brief.get("outcome", ""), gray=0.0)
    asked = brief.get("asked_of_operator") or []
    if asked and not (brief.get("runs") or []):
        doc.section("What the lab needs from you", rule=False)
        for need in asked[:12]:
            doc.bullet(need)
        doc.para("Until these arrive, the plan is held and nothing downstream "
                 "of them can run.", gray=0.35)
    produced = brief.get("produced") or []
    if produced:
        doc.section("What was produced", rule=False)
        for art in produced[:20]:
            doc.mono(str(art.get("path", art)))
    doc.rule()

    doc.section("The order", rule=False)
    doc.quote(brief.get("order", "(no text)"))
    doc.kv("issued to", brief.get("issued_to", "—"))
    doc.kv("issued", stamp(brief.get("issued_at", 0)))
    doc.kv("settled", stamp(brief.get("settled_at", 0)))
    doc.kv("messages", str(brief.get("messages", 0)))
    doc.kv("thread", brief.get("thread", "—"))
    doc.space(6)

    verdicts = brief.get("verdicts", [])
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    doc.section("Verdicts")
    if counts:
        doc.para(", ".join(f"{n} {k}" for k, n in counts.items()))
    if not verdicts:
        doc.para("No agent returned a verdict on this order.")
    for v in verdicts:
        doc.section(f"{v['agent']} ({v['role']}) — {v['verdict'] or 'no verdict'}",
                    rule=False)
        doc.para(v.get("position") or "(said nothing)")
        for need in v.get("needs") or []:
            doc.bullet(f"needs: {need}")
        if v.get("risk"):
            doc.bullet(f"risk: {v['risk']}")
        acted = v.get("acted")
        if isinstance(acted, dict) and acted.get("dispatched"):
            doc.bullet("dispatched: " + ", ".join(
                f"{d.get('to')} ({d.get('topic')})" for d in acted["dispatched"]))
        elif isinstance(acted, dict) and acted.get("needs"):
            doc.bullet("held, waiting on: " + ", ".join(str(n) for n in acted["needs"]))
        elif isinstance(acted, dict) and acted.get("error"):
            doc.bullet(f"failed while acting: {acted['error']}")

    quiet = {k: t for k, t in (brief.get("said_without_verdict") or {}).items()
             if k not in {v["agent"] for v in verdicts}}
    if quiet:
        doc.section("Spoke, but returned no verdict")
        for who, what in quiet.items():
            doc.section(who, rule=False)
            doc.para(what[:900])

    doc.section("What ran")
    runs = brief.get("runs") or []
    if not runs:
        doc.para("No pipeline was executed. A run starts only when a plan has "
                 "every input bound, the curator has cleared them, and the "
                 "runner is allowed to execute -- the section above says which "
                 "of those was missing.")
    for r in runs:
        doc.section(f"{r.get('workflow', '?')} — {r.get('status', '?')}", rule=False)
        doc.kv("run", str(r.get("run_id", "—")))
        doc.kv("steps", ", ".join(f"{t.get('step')}:{t.get('status')}"
                                  for t in r.get("tasks", [])) or "—")
        doc.kv("seconds", str(r.get("seconds", "—")))
        if r.get("dir"):
            doc.mono(str(r["dir"]))

    spend = brief.get("spend") or {}
    if spend:
        doc.section("What it cost")
        total = sum(v["usd"] for v in spend.values())
        tokens = sum(v["tokens_in"] + v["tokens_out"] for v in spend.values())
        doc.para(f"{tokens:,} tokens across {sum(int(v['calls']) for v in spend.values())} "
                 f"model calls, ${total:.4f} priced.")
        for agent, v in sorted(spend.items()):
            doc.kv(agent, f"{int(v['calls'])} calls, "
                          f"{int(v['tokens_in']):,} in / {int(v['tokens_out']):,} out")

    doc.space(10)
    doc.rule()
    doc.para(f"Generated by bioharness from the hive at "
             f"{stamp(time.time())}. Every line above is reconstructed from "
             f"messages on disk.", gray=0.45)

    name = f"brief-{brief.get('thread', 'unknown')}.pdf"
    return doc.save(Path(out_dir) / name)
