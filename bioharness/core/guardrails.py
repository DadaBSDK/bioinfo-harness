"""Budgets, circuit breakers and autonomy checks.

Every execution path in the harness goes through a Guardrails check first.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class GuardrailTripped(RuntimeError):
    """Raised when a budget is exhausted or a breaker is open."""


@dataclass
class Budget:
    """Hard ceilings for one run. None means unlimited."""

    max_steps: int | None = 200
    max_wallclock_s: float | None = 24 * 3600
    max_tokens: int | None = 2_000_000
    max_usd: float | None = 25.0
    max_cpu_hours: float | None = None
    max_gpu_hours: float | None = None

    steps: int = 0
    tokens: int = 0
    usd: float = 0.0
    cpu_hours: float = 0.0
    gpu_hours: float = 0.0
    started: float = field(default_factory=time.time)

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started

    def remaining(self) -> dict[str, Any]:
        def left(limit, used):
            return None if limit is None else round(limit - used, 4)
        return {
            "steps": left(self.max_steps, self.steps),
            "wallclock_s": left(self.max_wallclock_s, self.elapsed_s),
            "tokens": left(self.max_tokens, self.tokens),
            "usd": left(self.max_usd, self.usd),
            "cpu_hours": left(self.max_cpu_hours, self.cpu_hours),
            "gpu_hours": left(self.max_gpu_hours, self.gpu_hours),
        }

    def check(self) -> None:
        for label, limit, used in (
            ("steps", self.max_steps, self.steps),
            ("wallclock_s", self.max_wallclock_s, self.elapsed_s),
            ("tokens", self.max_tokens, self.tokens),
            ("usd", self.max_usd, self.usd),
            ("cpu_hours", self.max_cpu_hours, self.cpu_hours),
            ("gpu_hours", self.max_gpu_hours, self.gpu_hours),
        ):
            if limit is not None and used >= limit:
                raise GuardrailTripped(f"budget exhausted: {label} {used:.4g} >= {limit:.4g}")

    def charge(self, *, steps: int = 0, tokens: int = 0, usd: float = 0.0,
               cpu_hours: float = 0.0, gpu_hours: float = 0.0) -> None:
        self.steps += steps
        self.tokens += tokens
        self.usd += usd
        self.cpu_hours += cpu_hours
        self.gpu_hours += gpu_hours


@dataclass
class CircuitBreaker:
    """Stops a failing capability from being retried into the ground."""

    failure_threshold: int = 3
    cooldown_s: float = 300.0
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def state(self, key: str) -> str:
        opened = self._opened_at.get(key)
        if opened is None:
            return "closed"
        return "half-open" if time.time() - opened >= self.cooldown_s else "open"

    def guard(self, key: str) -> None:
        if self.state(key) == "open":
            wait = self.cooldown_s - (time.time() - self._opened_at[key])
            raise GuardrailTripped(
                f"circuit breaker open for {key!r} after "
                f"{self._failures.get(key, 0)} failures; retry in {wait:.0f}s")

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._opened_at.pop(key, None)

    def record_failure(self, key: str) -> None:
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] >= self.failure_threshold:
            self._opened_at[key] = time.time()


@dataclass
class Guardrails:
    """One object the orchestrator and executor both consult."""

    budget: Budget = field(default_factory=Budget)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    intervention: "Intervention" = field(default_factory=lambda: Intervention())
    dry_run: bool = False
    allow_network: bool = True
    confirm_destructive: bool = True

    def before_task(self, capability: str) -> None:
        self.budget.check()
        self.breaker.guard(capability)

    def after_task(self, capability: str, ok: bool, *, seconds: float = 0.0,
                   cpu: int = 1, gpu: int = 0, tokens: int = 0, usd: float = 0.0) -> None:
        hours = seconds / 3600.0
        self.budget.charge(steps=1, tokens=tokens, usd=usd,
                           cpu_hours=cpu * hours, gpu_hours=gpu * hours)
        (self.breaker.record_success if ok else self.breaker.record_failure)(capability)

    def permit(self, actor: dict[str, Any] | None, capability: str,
               domain: str = "") -> tuple[bool, str]:
        """May `actor` execute `capability` under its autonomy level?

        Returns (allowed, reason) rather than raising, so a refusal is recorded like
        any other step outcome.
        """
        if actor is None:
            return True, ""
        name = str(actor.get("name", "?"))
        declared = Autonomy(actor.get("autonomy") or Autonomy.BOUNDED)
        effective = self.intervention.autonomy_for(name, declared)
        if effective is not declared:
            note = f" (dropped from {declared.value} by intervention)"
        else:
            note = ""

        if effective is Autonomy.READ_ONLY:
            return False, f"{name} is read_only: may plan and inspect, never execute{note}"
        if effective is Autonomy.SUGGEST:
            return False, (f"{name} is suggest: {capability} needs a human to "
                           f"approve it{note}")
        if effective is Autonomy.FULL:
            return True, ""

        scope = [str(x) for x in (actor.get("scope") or [])]
        if not scope:
            return False, (f"{name} is bounded with no declared scope, so it may "
                           f"run nothing. Hire or assign it with --scope, or "
                           f"raise it to full.")
        if capability in scope or (domain and f"domain:{domain}" in scope):
            return True, ""
        return False, (f"{capability} is outside {name}'s scope "
                       f"({', '.join(scope)}){note}")

    def status(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "used": {"steps": self.budget.steps, "tokens": self.budget.tokens,
                     "usd": round(self.budget.usd, 4),
                     "elapsed_s": round(self.budget.elapsed_s, 1)},
            "remaining": self.budget.remaining(),
            "open_breakers": [k for k in self.breaker._opened_at
                              if self.breaker.state(k) == "open"],
        }


class Autonomy(str, Enum):
    """How much rope an agent gets before a human is asked."""

    READ_ONLY = "read_only"
    SUGGEST = "suggest"
    BOUNDED = "bounded"
    FULL = "full"


@dataclass
class Intervention:
    """The steer -> constrain -> stop ladder.

    A misbehaving agent is not killed on first offence. It is steered (given a
    corrective note), then constrained (autonomy dropped, scope narrowed), and
    only stopped if it keeps going. Each rung is recorded so the escalation is
    auditable after the fact.
    """

    STEER, CONSTRAIN, STOP = "steer", "constrain", "stop"
    _LADDER = (STEER, CONSTRAIN, STOP)

    rung: dict[str, int] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def level(self, agent: str) -> str:
        idx = self.rung.get(agent, -1)
        return "none" if idx < 0 else self._LADDER[min(idx, len(self._LADDER) - 1)]

    def escalate(self, agent: str, reason: str) -> str:
        idx = min(self.rung.get(agent, -1) + 1, len(self._LADDER) - 1)
        self.rung[agent] = idx
        action = self._LADDER[idx]
        self.history.append({"ts": time.time(), "agent": agent,
                             "action": action, "reason": reason})
        return action

    def relax(self, agent: str) -> None:
        """Good behaviour walks the ladder back down."""
        if agent in self.rung:
            self.rung[agent] -= 1
            if self.rung[agent] < 0:
                del self.rung[agent]

    def stopped(self, agent: str) -> bool:
        return self.level(agent) == self.STOP

    def autonomy_for(self, agent: str, declared: Autonomy) -> Autonomy:
        """Constrain drops a BOUNDED/FULL agent to SUGGEST; stop makes it read-only."""
        level = self.level(agent)
        if level == self.STOP:
            return Autonomy.READ_ONLY
        if level == self.CONSTRAIN and declared in (Autonomy.BOUNDED, Autonomy.FULL):
            return Autonomy.SUGGEST
        return declared
