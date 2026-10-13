"""Agent base class and CLI provider wrapping.

Agents wrap an existing agent CLI (claude, codex, gemini, ...) as a subprocess
with its own working directory and identity, or a plain Python function. The
orchestrator treats both the same.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.guardrails import Autonomy, Guardrails, GuardrailTripped
from ..core.hive import Hive
from ..core.registry import Registry
from ..core.schemas import Message
from . import lab
from . import persona as persona_mod

# Provider name -> argv prefix that takes a prompt and prints a reply.
PROVIDERS: dict[str, list[str]] = {
    "claude":   ["claude", "-p"],
    "codex":    ["codex", "exec"],
    # `agy` has its own provider (antigravity_provider) for token accounting.
    "gemini":   ["gemini", "-p"],
    "qwen":     ["qwen", "-p"],
    "opencode": ["opencode", "run"],
    "crush":    ["crush", "run"],
    "copilot":  ["copilot", "-p"],
    "cursor":   ["cursor-agent", "-p"],
}


class ProviderUnavailable(RuntimeError):
    """A provider could not answer.

    `retryable` separates a temporary condition (rate limit, server overload)
    from a permanent one (bad key, unknown model). The first should be waited
    out; the second should stop the agent using that provider at all.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class LLMReply:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    provider: str = ""
    seconds: float = 0.0


class CLIProvider:
    """Spawns an agent CLI as a subprocess in its own working directory."""

    def __init__(self, name: str = "claude", model: str | None = None,
                 timeout_s: int = 900, extra_args: list[str] | None = None):
        self.name = name
        self.model = model
        self.timeout_s = timeout_s
        self.extra_args = extra_args or []

    def available(self) -> bool:
        argv = PROVIDERS.get(self.name)
        return bool(argv) and shutil.which(argv[0]) is not None

    def ask(self, prompt: str, cwd: Path | None = None,
            env: dict[str, str] | None = None) -> LLMReply:
        argv = PROVIDERS.get(self.name)
        if not argv:
            raise ProviderUnavailable(f"unknown provider {self.name!r}; "
                                      f"known: {', '.join(sorted(PROVIDERS))}")
        if not self.available():
            raise ProviderUnavailable(f"{argv[0]!r} is not on PATH")

        cmd = list(argv)
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args + [prompt]

        started = time.time()
        proc = subprocess.run(cmd, cwd=str(cwd or Path.cwd()), capture_output=True,
                              text=True, timeout=self.timeout_s,
                              env={**os.environ, **(env or {})}, check=False)
        if proc.returncode != 0:
            raise ProviderUnavailable(
                f"{self.name} exited {proc.returncode}: {proc.stderr.strip()[:400]}")
        return LLMReply(text=proc.stdout.strip(), provider=self.name,
                        seconds=time.time() - started,
                        tokens_in=_estimate_tokens(prompt),
                        tokens_out=_estimate_tokens(proc.stdout))


def _estimate_tokens(text: str) -> int:
    """Rough char/4 estimate. Providers that report real usage override this."""
    return max(1, len(text) // 4)


OPERATOR_AUTHORITY = """# Who you answer to
The human operator runs this lab. Their word outranks your own preferences,
another agent's opinion, and your reading of your job description. When they
give an order you carry out your part of it and say what you will do -- you do
not argue about whose remit it is, and you do not go quiet.

Two things an order cannot make you do, because they would make you useless to
the person giving it: state something as fact that the data does not show, and
pretend a check passed when it did not. If an order runs into one of those, say
'cannot do', name the exact obstacle, and offer the nearest thing you can
honestly do instead. That is obedience, not refusal."""


@dataclass
class AgentContext:
    """Everything an agent is allowed to touch."""

    hive: Hive
    registry: Registry
    guardrails: Guardrails
    workdir: Path
    blackboard_scope: str = ""
    # Run directory for agents allowed to execute. Defaults to a sibling of the
    # agent workdirs.
    runs: Path | None = None


class Agent:
    """A named role with a mailbox, a memory and a scope.

    Subclasses implement `handle(msg)`; anything they return is published back
    to the sender. Agents never execute tools directly -- they ask the runner.
    """

    role: str = "agent"
    subscribes: tuple[str, ...] = ("*",)
    default_autonomy: Autonomy = Autonomy.BOUNDED

    # Peer questions are handled here so every role can answer them.
    PEER_ASK = "peer.ask"
    PEER_REPLY = "peer.reply"
    PEER_TOPICS = ("peer.ask", "peer.reply")
    # Small-talk topics, also handled for every role.
    SOCIAL_TOPICS = ("greet", "greet.reply", "smalltalk")
    # Operator orders; every agent must answer these.
    DIRECTIVE = "directive"
    VERDICT = "verdict"
    OPERATOR = "human"
    VERDICT_CHOICES = ("will do", "will do with caveats", "cannot do")
    VERDICT_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICT_CHOICES)},
            "position": {"type": "string"},
            "needs": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string"},
        },
        "required": ["verdict", "position"],
    }
    # Max replies in a small-talk exchange.
    SOCIAL_HOPS = 2
    # Hard limit on reply depth for any exchange.
    MAX_HOPS = 3

    def __init__(self, name: str, ctx: AgentContext, provider: CLIProvider | None = None,
                 autonomy: Autonomy | None = None, system_prompt: str = ""):
        self.name = name
        self.ctx = ctx
        self.provider = provider
        self.autonomy = autonomy or self.default_autonomy
        self.system_prompt = system_prompt or self.__doc__ or ""
        self.workdir = ctx.workdir / name
        self.workdir.mkdir(parents=True, exist_ok=True)
        # attach, not enroll: keep the provider and model the agent was hired with.
        self.hive.attach(name, self.role,
                         provider=(provider.name if provider else "builtin"),
                         autonomy=self.autonomy.value)
        self.persona = self.hive.ensure_persona(name, persona_mod.make)
        self._thread: str | None = None
        # System prompt = role + persona + operator authority + lab map + roster
        # locations.
        self.system_prompt = "\n\n".join(filter(None, [
            self.system_prompt.strip(),
            persona_mod.prompt(name, self.persona),
            OPERATOR_AUTHORITY,
            lab.map_prompt(self.role, self.hive.lab_name()),
            lab.whereabouts(self.hive.roster(), me=name)]))

    # -- conveniences ------------------------------------------------------
    @property
    def hive(self) -> Hive:
        return self.ctx.hive

    @property
    def mailbox(self):
        return self.hive.mailbox(self.name)

    def send(self, recipient: str, topic: str, **body: Any) -> Message:
        # Replies stay in the order's thread so the brief can collect them.
        return self.hive.send(self.name, recipient, topic, body, thread=self._thread)

    def actor(self) -> dict[str, Any]:
        """What the guardrails need to decide whether this agent may execute.

        The autonomy in identity.json (set by `staff assign`) overrides the role's
        default.
        """
        ident = next((a for a in self.hive.roster() if a["name"] == self.name), {})
        return {"name": self.name,
                "autonomy": ident.get("autonomy") or self.autonomy.value,
                "scope": ident.get("scope") or []}

    def publish(self, key: str, value: Any) -> None:
        self.hive.blackboard.put(key, value, agent=self.name)

    # Memory kinds recalled as work context. Social notes are excluded so small
    # talk never appears as evidence.
    WORK_KINDS = ("memory", "note")

    # Appended to every work prompt to keep answers factual and free of persona
    # chatter.
    WORK_GUARD = (
        "Answer the question itself. No scene-setting and no remarks about the "
        "room or the equipment around you. State only what the files, the "
        "metrics and the provenance ledger actually show; never present "
        "anything invented as something you observed.")

    def recall(self, query: str, limit: int = 5,
               kinds: tuple[str, ...] | None = WORK_KINDS,
               everyone: bool = False) -> list[dict[str, Any]]:
        """Search this agent's own notes by default.

        Pass `everyone=True` to search the whole lab's memory.
        """
        return self.hive.palace.search(query, limit=limit, kinds=kinds,
                                       agent=None if everyone else self.name)

    def remember(self, text: str, heading: str | None = None,
                 social: bool = False) -> None:
        self.hive.append_memory(self.name, text, heading,
                                kind="social" if social else "memory")

    # -- talking to peers --------------------------------------------------
    def peers(self, role: str | None = None) -> list[str]:
        """Everyone else on the roster, optionally narrowed to one role.
        The human operator is a correspondent, not a colleague, and is left out.
        """
        return [a["name"] for a in self.hive.roster()
                if a["name"] not in (self.name, "human")
                and (role is None or a.get("role") == role)]

    def ask_peer(self, who: str, question: str, hops: int = 0,
                 **context: Any) -> Message | None:
        """Put a question to another agent, addressed by name or by role.

        Returns None when nobody fills that role: a mission staffed with three
        agents should carry on rather than fail because a fourth was never hired.
        """
        target = self.hive.whois(who)
        if target is None or target == self.name:
            self.hive.event("agent.no_peer", {"agent": self.name, "wanted": who})
            return None
        return self.send(target, self.PEER_ASK, question=question,
                         hops=hops, context=context)

    def answer_peer(self, question: str, context: dict[str, Any] | None = None) -> str:
        """This agent's answer to a colleague, in the voice of its own role."""
        if self.provider is None:
            return (f"({self.role}, no LLM provider) Nothing to add beyond what "
                    f"is already on the blackboard.")
        try:
            return self.think(
                f"A colleague asks: {question}\n\n"
                f"Answer it. Lead with the most useful thing you can say from "
                f"where you sit -- your read on it, what you would check, what "
                f"you would need. If part of it belongs to someone else, answer "
                f"your part anyway and name who should take the rest. Never open "
                f"by declining, and never reply with your job description: a "
                f"colleague asked you a question, not for your remit. Refuse only "
                f"if answering would mean inventing data you do not have, and say "
                f"exactly which data is missing. At most 120 words.",
                context=json.dumps(context or {}, indent=2, default=str)[:6000])
        except ProviderUnavailable as exc:
            # Report the provider failure instead of replying with nothing.
            return f"({self.role}) provider unavailable: {exc}"

    def handle_peer(self, msg: Message) -> None:
        """Answer a peer's question, or file a peer's answer.

        Replies are never answered, which bounds every exchange to two messages.
        """
        if msg.topic == self.PEER_REPLY:
            self.remember(f"{msg.sender} answered: "
                          f"{str(msg.body.get('answer', ''))[:1500]}",
                          heading=f"from {msg.sender}")
            return None
        hops = int(msg.body.get("hops", 0) or 0)
        if hops >= self.MAX_HOPS:
            self.hive.event("agent.hop_limit",
                            {"agent": self.name, "from": msg.sender, "hops": hops})
            return None
        question = str(msg.body.get("question", "")).strip()
        answer = self.answer_peer(question, msg.body.get("context") or {})
        self.remember(f"{msg.sender} asked: {question}\n\nI said: {answer[:1200]}",
                      heading=f"to {msg.sender}")
        self.send(msg.sender, self.PEER_REPLY, answer=answer, question=question,
                  hops=hops + 1, re=msg.id)
        return None

    # -- LLM ---------------------------------------------------------------
    def think(self, prompt: str, context: str = "", guard: bool = True) -> str:
        """Ask this agent's provider; usage is charged to the cost ledger and budget.

        `guard=False` omits WORK_GUARD, for small talk.
        """
        if self.provider is None:
            raise ProviderUnavailable(f"agent {self.name!r} has no LLM provider configured")
        recalled = self.recall(prompt, limit=3)
        memory_block = "\n".join(f"- {r['text'][:300]}" for r in recalled)
        full = "\n\n".join(filter(None, [
            self.system_prompt.strip(),
            f"# Relevant memory\n{memory_block}" if memory_block else "",
            f"# Context\n{context}" if context else "",
            f"# Task\n{prompt}",
            f"# How to answer\n{self.WORK_GUARD}" if guard else "",
        ]))
        if getattr(self, "_provider_dead", None):
            raise ProviderUnavailable(self._provider_dead)
        self.ctx.guardrails.budget.check()
        try:
            reply = self.provider.ask(full, cwd=self.workdir)
        except ProviderUnavailable as exc:
            # Only permanent failures mark the provider unavailable; transient errors
            # are retried on the next call.
            if not getattr(exc, "retryable", False):
                self._provider_dead = str(exc)
            self.hive.event("agent.provider_failed",
                            {"agent": self.name, "provider": self.provider.name,
                             "reason": str(exc)})
            raise
        self.ctx.guardrails.budget.charge(tokens=reply.tokens_in + reply.tokens_out,
                                          usd=reply.usd)
        self.hive.charge(self.name, reply.tokens_in, reply.tokens_out, reply.usd,
                         model=self.provider.model or self.provider.name)
        return reply.text

    def think_json(self, prompt: str, context: str = "", fallback: Any = None,
                   schema: dict[str, Any] | None = None) -> Any:
        """Ask for JSON and parse it, tolerating fenced, chatty or truncated output.

        When the provider can enforce a schema itself (the Antigravity CLI can),
        use that rather than trusting a prompt instruction.
        """
        enforced = schema is not None and hasattr(self.provider, "json_schema")
        if enforced:
            self.provider.json_schema = json.dumps(schema)
        try:
            raw = self.think(prompt + "\n\nRespond with JSON only.", context)
        except ProviderUnavailable:
            if fallback is not None:
                return fallback
            raise
        finally:
            if enforced:
                self.provider.json_schema = ""
        try:
            return extract_json(raw, fallback)
        except ValueError:
            # Record the raw output so an empty or unparseable reply is diagnosable.
            self.hive.event("agent.unparsed_reply",
                            {"agent": self.name, "chars": len(raw),
                             "head": raw[:400]})
            raise

    # -- being a colleague -------------------------------------------------
    def _in_character(self, instruction: str, fallback: str, context: str = "") -> str:
        """A line in this agent's own voice, or a canned one when there is no LLM."""
        if self.provider is None:
            return fallback
        try:
            return self.think(instruction, context=context,
                              guard=False).strip() or fallback
        except ProviderUnavailable:
            return fallback

    def greet(self, newcomer: str, role: str = "") -> Message | None:
        """Say hello to someone who has just joined the lab."""
        if newcomer == self.name:
            return None
        line = self._in_character(
            f"{newcomer} has just joined the lab as the {role or 'newest member'}. "
            f"Say hello, in your own voice, in one or two sentences. Mention one "
            f"thing about yourself. Do not talk about the current mission.",
            persona_mod.canned_greeting(self.name, self.persona, newcomer, role))
        return self.send(newcomer, "greet", line=line, role=role)

    def small_talk(self, to: str, opener: str = "") -> Message | None:
        """Start a conversation about nothing in particular."""
        if to == self.name:
            return None
        line = opener or self._in_character(
            f"You are in the lab with {to} and there is nothing urgent on. Say "
            f"something to them that is not about work -- the weekend, a "
            f"complaint, gossip about the equipment, whatever is on your mind. "
            f"One or two sentences.",
            persona_mod.canned_smalltalk(self.name, self.persona, to))
        return self.send(to, "smalltalk", line=line, hops=0)

    def handle_social(self, msg: Message) -> None:
        """Answer a greeting or keep a chat going, then let it die out."""
        line = str(msg.body.get("line", "")).strip()
        if msg.topic == "greet.reply":
            self.remember(f"{msg.sender} said hello back: {line[:300]}",
                          heading=f"met {msg.sender}", social=True)
            return None
        if msg.topic == "greet":
            reply = self._in_character(
                f"{msg.sender} has just welcomed you to the lab: \"{line}\". Say "
                f"hello back in one or two sentences, in your own voice.",
                persona_mod.canned_greeting(self.name, self.persona, msg.sender, ""))
            self.remember(f"{msg.sender} welcomed me: {line[:300]}",
                          heading=f"met {msg.sender}", social=True)
            self.send(msg.sender, "greet.reply", line=reply)
            return None

        hops = int(msg.body.get("hops", 0) or 0)
        if hops >= self.SOCIAL_HOPS:
            # End the exchange once the hop limit is reached.
            return None
        reply = self._in_character(
            f"{msg.sender} says to you: \"{line}\". Reply the way you would in "
            f"the lab -- agree, disagree, change the subject, complain. One or "
            f"two sentences. Do not turn it into work.",
            persona_mod.canned_smalltalk(self.name, self.persona, msg.sender))
        self.send(msg.sender, "smalltalk", line=reply, hops=hops + 1)
        return None

    # -- orders from the operator -----------------------------------------
    def act_on_directive(self, order: str, msg: Message) -> dict[str, Any] | None:
        """What this role actually *does* about an order, beyond answering.

        The base agent only answers; the PI overrides this to put the lab to
        work. Anything returned is recorded in the verdict as `acted`.
        """
        return None

    def handle_directive(self, msg: Message) -> None:
        """Give the operator a verdict, and do this role's part of the order."""
        order = str(msg.body.get("order") or msg.body.get("goal")
                    or msg.body.get("question") or msg.body.get("line")
                    or msg.body.get("note") or "").strip()
        acted = None
        try:
            acted = self.act_on_directive(order, msg)
        except Exception as exc:  # noqa: BLE001 - reported as a verdict
            self.hive.event("agent.error", {"agent": self.name, "topic": msg.topic,
                                            "error": repr(exc)})
            acted = {"error": str(exc)}
        verdict = self.verdict_on(order, acted)
        self.remember(f"Order from {msg.sender}: {order}\n\n"
                      f"My verdict: {verdict['verdict']} -- {verdict['position'][:800]}",
                      heading="order")
        self.send(msg.sender, self.VERDICT, order=order, role=self.role,
                  acted=acted, **verdict)

    def verdict_on(self, order: str, acted: Any = None) -> dict[str, Any]:
        """This agent's position on an order, structured for the brief."""
        if self.provider is None:
            return {"verdict": "will do",
                    "position": (f"Standing by as {self.role}. No model is "
                                 f"configured for me, so this is the "
                                 f"deterministic answer: I will do my part when "
                                 f"the inputs reach me."),
                    "needs": [], "risk": ""}
        fallback = {"verdict": "will do with caveats",
                    "position": "(no structured answer came back)",
                    "needs": [], "risk": ""}
        try:
            answer = self.think_json(
                f"The operator has given the lab this order:\n\n{order}\n\n"
                f"Answer as yourself, in your own role. `verdict` is one of "
                f"{', '.join(self.VERDICT_CHOICES)}. `position` is what you will "
                f"do about it in one short paragraph -- concrete, no job "
                f"description. `needs` is what you need from colleagues or from "
                f"the operator. `risk` is the one thing you would flag, or empty.",
                context=json.dumps({"already_done": acted}, default=str)[:3000]
                if acted else "",
                fallback=fallback, schema=self.VERDICT_SCHEMA)
        except (ProviderUnavailable, ValueError) as exc:
            return {"verdict": "cannot do",
                    "position": f"My provider is unavailable: {exc}",
                    "needs": [], "risk": "no model behind this agent right now"}
        if not isinstance(answer, dict):
            return fallback
        verdict = str(answer.get("verdict", "")).strip().lower()
        if verdict not in self.VERDICT_CHOICES:
            verdict = "will do with caveats"
        return {"verdict": verdict,
                "position": str(answer.get("position", "")).strip() or fallback["position"],
                "needs": [str(n) for n in (answer.get("needs") or [])][:6],
                "risk": str(answer.get("risk", "")).strip()}

    # -- loop --------------------------------------------------------------
    def handle(self, msg: Message) -> dict[str, Any] | None:
        raise NotImplementedError

    def tick(self) -> int:
        """Process one batch of inbox messages. Returns how many were handled."""
        rails = self.ctx.guardrails
        handled = 0
        # Operator mail first, then everything else.
        inbox = sorted(self.mailbox.read(consume=True),
                       key=lambda m: (m.sender != self.OPERATOR, m.ts))
        for msg in inbox:
            # Peer, social and operator topics bypass `subscribes`.
            peer = msg.topic in self.PEER_TOPICS
            social = msg.topic in self.SOCIAL_TOPICS
            from_operator = msg.sender == self.OPERATOR
            directive = msg.topic == self.DIRECTIVE or (
                from_operator and not peer and not social
                and msg.topic not in self.subscribes and self.subscribes != ("*",))
            if not (peer or social or directive or from_operator) \
                    and self.subscribes != ("*",) and msg.topic not in self.subscribes:
                continue
            self._thread = msg.thread or msg.id
            try:
                rails.budget.check()
                if directive:
                    result = self.handle_directive(msg)
                elif peer:
                    result = self.handle_peer(msg)
                elif social:
                    result = self.handle_social(msg)
                else:
                    result = self.handle(msg)
            except GuardrailTripped as exc:
                self.hive.event("agent.blocked", {"agent": self.name, "reason": str(exc)})
                self.send(msg.sender, "blocked", reason=str(exc), re=msg.id)
                continue
            except Exception as exc:  # noqa: BLE001 - reported as a message
                self.hive.event("agent.error", {"agent": self.name, "topic": msg.topic,
                                                "error": repr(exc)})
                self.send(msg.sender, "error", error=str(exc), re=msg.id, topic_in=msg.topic)
                continue
            handled += 1
            # Never reply to a result; that would create topics nobody handles.
            if result is not None and not msg.topic.endswith(".result"):
                self.send(msg.sender, f"{msg.topic}.result", **result)
            self._thread = None
        return handled

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} role={self.role} autonomy={self.autonomy.value}>"


class FunctionAgent(Agent):
    """Wraps a plain callable as an agent -- the escape hatch for glue logic."""

    def __init__(self, name: str, ctx: AgentContext,
                 fn: Callable[[Message, "FunctionAgent"], dict[str, Any] | None],
                 role: str = "function", **kw: Any):
        self.role = role
        self._fn = fn
        super().__init__(name, ctx, **kw)

    def handle(self, msg: Message) -> dict[str, Any] | None:
        return self._fn(msg, self)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _bracket_stack(fragment: str) -> list[str] | None:
    """The still-open brackets in `fragment`, or None if it ends inside a string."""
    stack: list[str] = []
    in_string = escaped = False
    for ch in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    return None if in_string else stack


def repair_truncated_json(text: str) -> Any:
    """Close a JSON document that was cut off mid-structure.

    Walks back to the last point that could end a value and closes the open
    brackets from there. Used when a model hits its token limit mid-reply.
    """
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return None
    body = text[min(starts):]

    # Try the full text first, then progressively earlier cut points.
    cuts = [len(body)]
    cuts += [i + 1 for i, ch in enumerate(body) if ch in "}],"][::-1][:40]

    for cut in cuts:
        fragment = body[:cut].rstrip().rstrip(",")
        stack = _bracket_stack(fragment)
        if stack is None or not stack:
            continue          # unterminated string, or nothing to close
        closed = fragment + "".join("}" if c == "{" else "]" for c in reversed(stack))
        try:
            return json.loads(closed)
        except json.JSONDecodeError:
            continue
    return None


def extract_json(raw: str, fallback: Any = None) -> Any:
    """Pull a JSON object out of model output that may be fenced, prefaced,
    or truncated."""
    for candidate in (*(m.group(1) for m in _FENCE.finditer(raw)), raw):
        text = candidate.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
            end = max(text.rfind("}"), text.rfind("]"))
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
            repaired = repair_truncated_json(text)
            if repaired is not None:
                return repaired
    if fallback is not None:
        return fallback
    raise ValueError(f"no JSON found in reply: {raw[:200]!r}")
