"""Agent base class and CLI provider wrapping.

Like Munder Difflin, the harness does not reimplement agents -- it wraps the
CLI you already pay for (claude, codex, gemini, ...) as a subprocess with its
own working directory and identity. An agent may equally be backed by a plain
Python function; the orchestrator cannot tell the difference and should not.
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

# Provider CLIs, mapped to the argv shape that takes a prompt and prints a reply.
PROVIDERS: dict[str, list[str]] = {
    "claude":   ["claude", "-p"],
    "codex":    ["codex", "exec"],
    "gemini":   ["gemini", "-p"],
    "qwen":     ["qwen", "-p"],
    "opencode": ["opencode", "run"],
    "crush":    ["crush", "run"],
    "copilot":  ["copilot", "-p"],
    "cursor":   ["cursor-agent", "-p"],
}


class ProviderUnavailable(RuntimeError):
    pass


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


@dataclass
class AgentContext:
    """Everything an agent is allowed to touch."""

    hive: Hive
    registry: Registry
    guardrails: Guardrails
    workdir: Path
    blackboard_scope: str = ""


class Agent:
    """A named role with a mailbox, a memory and a scope.

    Subclasses implement `handle(msg)`; anything they return is published back
    to the sender. Agents never execute tools directly -- they ask the runner.
    """

    role: str = "agent"
    subscribes: tuple[str, ...] = ("*",)
    default_autonomy: Autonomy = Autonomy.BOUNDED

    def __init__(self, name: str, ctx: AgentContext, provider: CLIProvider | None = None,
                 autonomy: Autonomy | None = None, system_prompt: str = ""):
        self.name = name
        self.ctx = ctx
        self.provider = provider
        self.autonomy = autonomy or self.default_autonomy
        self.system_prompt = system_prompt or self.__doc__ or ""
        self.workdir = ctx.workdir / name
        self.workdir.mkdir(parents=True, exist_ok=True)
        # attach, not enroll: joining a mission must never overwrite the
        # provider and model this agent was hired with.
        self.hive.attach(name, self.role,
                         provider=(provider.name if provider else "builtin"),
                         autonomy=self.autonomy.value)

    # -- conveniences ------------------------------------------------------
    @property
    def hive(self) -> Hive:
        return self.ctx.hive

    @property
    def mailbox(self):
        return self.hive.mailbox(self.name)

    def send(self, recipient: str, topic: str, **body: Any) -> Message:
        return self.hive.send(self.name, recipient, topic, body)

    def publish(self, key: str, value: Any) -> None:
        self.hive.blackboard.put(key, value, agent=self.name)

    def recall(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return self.hive.palace.search(query, limit=limit)

    def remember(self, text: str, heading: str | None = None) -> None:
        self.hive.append_memory(self.name, text, heading)

    # -- LLM ---------------------------------------------------------------
    def think(self, prompt: str, context: str = "") -> str:
        """Ask this agent's provider. Charged to the cost ledger and the budget."""
        if self.provider is None:
            raise ProviderUnavailable(f"agent {self.name!r} has no LLM provider configured")
        recalled = self.recall(prompt, limit=3)
        memory_block = "\n".join(f"- {r['text'][:300]}" for r in recalled)
        full = "\n\n".join(filter(None, [
            self.system_prompt.strip(),
            f"# Relevant memory\n{memory_block}" if memory_block else "",
            f"# Context\n{context}" if context else "",
            f"# Task\n{prompt}",
        ]))
        self.ctx.guardrails.budget.check()
        reply = self.provider.ask(full, cwd=self.workdir)
        self.ctx.guardrails.budget.charge(tokens=reply.tokens_in + reply.tokens_out,
                                          usd=reply.usd)
        self.hive.charge(self.name, reply.tokens_in, reply.tokens_out, reply.usd,
                         model=self.provider.model or self.provider.name)
        return reply.text

    def think_json(self, prompt: str, context: str = "",
                   fallback: Any = None) -> Any:
        """Ask for JSON and parse it, tolerating fenced or chatty output."""
        try:
            raw = self.think(prompt + "\n\nRespond with JSON only.", context)
        except ProviderUnavailable:
            if fallback is not None:
                return fallback
            raise
        return extract_json(raw, fallback)

    # -- loop --------------------------------------------------------------
    def handle(self, msg: Message) -> dict[str, Any] | None:
        raise NotImplementedError

    def tick(self) -> int:
        """Process one batch of inbox messages. Returns how many were handled."""
        rails = self.ctx.guardrails
        handled = 0
        for msg in self.mailbox.read(consume=True):
            if self.subscribes != ("*",) and msg.topic not in self.subscribes:
                continue
            try:
                rails.budget.check()
                result = self.handle(msg)
            except GuardrailTripped as exc:
                self.hive.event("agent.blocked", {"agent": self.name, "reason": str(exc)})
                self.send(msg.sender, "blocked", reason=str(exc), re=msg.id)
                continue
            except Exception as exc:  # noqa: BLE001 - an agent error is a message, not a crash
                self.hive.event("agent.error", {"agent": self.name, "topic": msg.topic,
                                                "error": repr(exc)})
                self.send(msg.sender, "error", error=str(exc), re=msg.id, topic_in=msg.topic)
                continue
            handled += 1
            if result is not None:
                self.send(msg.sender, f"{msg.topic}.result", **result)
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


def extract_json(raw: str, fallback: Any = None) -> Any:
    """Pull a JSON object out of model output that may be fenced or prefaced."""
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
                    continue
    if fallback is not None:
        return fallback
    raise ValueError(f"no JSON found in reply: {raw[:200]!r}")
