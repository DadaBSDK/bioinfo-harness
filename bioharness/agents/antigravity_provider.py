"""Antigravity CLI provider.

A dedicated provider rather than the generic CLI wrapper because
`--output-format json` reports real token usage (including thinking and cache
reads), and one credential covers several model families.

The CLI handles its own authentication (OAuth, or AV_API_KEY in CI).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import LLMReply, ProviderUnavailable

BINARY = "agy"
EFFORTS = ("low", "medium", "high")

LIST_TIMEOUT = 60.0


def binary_present() -> bool:
    return shutil.which(BINARY) is not None


_CACHE: dict[str, Any] = {"models": None, "at": 0.0}
CACHE_TTL = 300.0


def available_models(timeout: float = LIST_TIMEOUT, refresh: bool = False) -> list[str]:
    """Model ids from `agy models`, which prints `id<TAB>Display Name`."""
    if not refresh and _CACHE["models"] is not None \
            and time.time() - _CACHE["at"] < CACHE_TTL:
        return _CACHE["models"]
    if not binary_present():
        _CACHE.update(models=[], at=time.time())
        return []
    try:
        proc = subprocess.run([BINARY, "models"], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        _CACHE.update(models=[], at=time.time())
        return []
    if proc.returncode != 0:
        _CACHE.update(models=[], at=time.time())
        return []
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("fetching"):
            continue
        out.append(line.split("\t")[0].split()[0])
    models = [m for m in out if m]
    _CACHE.update(models=models, at=time.time())
    return models


@dataclass
class AntigravityProvider:
    """Same interface as the Claude, Gemini, Ollama and generic CLI providers."""

    name: str = "antigravity"
    model: str = ""
    effort: str = ""
    json_schema: str = ""
    timeout_s: float = 600.0
    _models: list[str] = field(default_factory=list)

    def refresh(self) -> list[str]:
        self._models = available_models()
        return self._models

    def available(self) -> bool:
        if not binary_present():
            return False
        if not self.model:
            return True
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
        reply = self._ask_once(prompt, cwd, env, schema=self.json_schema)
        if reply.text or not self.json_schema:
            return reply
        retry = self._ask_once(prompt, cwd, env, schema="")
        retry.tokens_in += reply.tokens_in
        retry.tokens_out += reply.tokens_out
        retry.seconds += reply.seconds
        return retry

    def _ask_once(self, prompt: str, cwd: Path | None, env: dict[str, str] | None,
                  schema: str) -> LLMReply:
        if not binary_present():
            raise ProviderUnavailable(self.unavailable_reason())

        cmd = [BINARY, "--print", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort in EFFORTS:
            cmd += ["--effort", self.effort]
        if schema:
            cmd += ["--json-schema", schema]

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
        generated = (int(usage.get("output_tokens") or 0)
                     + int(usage.get("thinking_tokens") or 0))
        structured = data.get("structured_output")
        text = (json.dumps(structured) if structured is not None
                else str(data.get("response", "")))

        return LLMReply(
            text=text.strip(),
            provider=f"{self.name}:{self.model or 'default'}",
            seconds=float(data.get("duration_seconds") or (time.time() - started)),
            tokens_in=(int(usage.get("input_tokens") or 0)
                       + int(usage.get("cache_read_tokens") or 0)),
            tokens_out=generated,
            usd=0.0,
        )


def peek_models() -> list[str] | None:
    """Whatever is cached, without spawning anything. None means not yet known."""
    return _CACHE["models"]


def warm_models() -> None:
    """Refresh the model cache in the background so dashboard requests never wait
    on a CLI spawn.
    """
    if _CACHE.get("warming"):
        return
    _CACHE["warming"] = True

    def run() -> None:
        try:
            available_models(refresh=True)
        except Exception:
            pass
        finally:
            _CACHE["warming"] = False

    threading.Thread(target=run, daemon=True).start()
