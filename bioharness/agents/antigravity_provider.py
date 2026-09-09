"""Antigravity CLI provider.

`agy` is Google's successor to the Gemini CLI. It is worth a dedicated provider
rather than the generic CLI wrapper for two reasons: `--output-format json`
returns real token usage (including thinking and cache reads) instead of a
character-count estimate, and it fronts several model families at once --
Gemini, Claude and GPT-OSS -- so one credential covers a lot of ground.

It authenticates itself (OAuth, or AV_API_KEY in CI); the harness never handles
that credential.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import LLMReply, ProviderUnavailable

BINARY = "agy"
EFFORTS = ("low", "medium", "high")

# The CLI is slow to start even when the model answers quickly, so failures
# that look like hangs are usually startup plus auth, not inference.
LIST_TIMEOUT = 60.0


def binary_present() -> bool:
    return shutil.which(BINARY) is not None


def available_models(timeout: float = LIST_TIMEOUT) -> list[str]:
    """Model ids from `agy models`, which prints `id<TAB>Display Name`."""
    if not binary_present():
        return []
    try:
        proc = subprocess.run([BINARY, "models"], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("fetching"):
            continue
        out.append(line.split("\t")[0].split()[0])
    return [m for m in out if m]


@dataclass
class AntigravityProvider:
    """Same interface as the Claude, Gemini, Ollama and generic CLI providers."""

    name: str = "antigravity"
    model: str = ""
    effort: str = ""
    timeout_s: float = 600.0
    _models: list[str] = field(default_factory=list)

    def refresh(self) -> list[str]:
        self._models = available_models()
        return self._models

    def available(self) -> bool:
        if not binary_present():
            return False
        if not self.model:
            return True          # let the CLI pick its own default
        return self.model in (self._models or self.refresh())

    def unavailable_reason(self) -> str:
        if not binary_present():
            return f"{BINARY!r} is not on PATH (install the Antigravity CLI)"
        models = self._models or self.refresh()
        if not models:
            return (f"{BINARY} is installed but listed no models "
                    f"(is it signed in? try `{BINARY} models`)")
        return f"model {self.model!r} is not offered; available: {', '.join(models[:6])}…"

    def ask(self, prompt: str, cwd: Path | None = None,
            env: dict[str, str] | None = None) -> LLMReply:
        if not binary_present():
            raise ProviderUnavailable(self.unavailable_reason())

        cmd = [BINARY, "--print", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort in EFFORTS:
            cmd += ["--effort", self.effort]

        started = time.time()
        try:
            proc = subprocess.run(cmd, cwd=str(cwd or Path.cwd()), capture_output=True,
                                  text=True, timeout=self.timeout_s, check=False,
                                  env=env)
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnavailable(
                f"{BINARY} did not answer within {self.timeout_s:.0f}s", retryable=True
            ) from exc
        except OSError as exc:
            raise ProviderUnavailable(f"could not run {BINARY}: {exc}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ProviderUnavailable(f"{BINARY} exited {proc.returncode}: {detail}")

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(
                f"{BINARY} did not return JSON: {proc.stdout.strip()[:200]}") from exc

        status = str(data.get("status", "")).upper()
        if status and status != "SUCCESS":
            raise ProviderUnavailable(
                f"{BINARY} reported {status}: {str(data.get('error') or '')[:200]}",
                retryable=status in ("TIMEOUT", "RATE_LIMITED", "UNAVAILABLE"))

        usage = data.get("usage") or {}
        # Thinking is generated output and is billed as such; counting only
        # output_tokens would under-report every reasoning model here.
        generated = (int(usage.get("output_tokens") or 0)
                     + int(usage.get("thinking_tokens") or 0))
        return LLMReply(
            text=str(data.get("response", "")).strip(),
            provider=f"{self.name}:{self.model or 'default'}",
            seconds=float(data.get("duration_seconds") or (time.time() - started)),
            tokens_in=(int(usage.get("input_tokens") or 0)
                       + int(usage.get("cache_read_tokens") or 0)),
            tokens_out=generated,
            usd=0.0,          # billed through the Antigravity plan, not per call
        )
