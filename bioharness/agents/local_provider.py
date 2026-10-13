"""Local model provider, via an Ollama server on this machine.

No per-call cost, and sequence data and sample metadata never leave the host.
Uses the standard library only.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import LLMReply, ProviderUnavailable

DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("BIOHARNESS_LOCAL_MODEL", "qwen2.5:3b")


def _host(raw: str) -> str:
    """OLLAMA_HOST is often set bare, as `127.0.0.1:11434`."""
    raw = raw.strip().rstrip("/")
    return raw if raw.startswith(("http://", "https://")) else f"http://{raw}"


def _get(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, payload: dict[str, Any], timeout: float) -> Any:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def installed_models(host: str = DEFAULT_HOST, timeout: float = 5.0) -> list[str]:
    try:
        tags = _get(f"{_host(host)}/api/tags", timeout)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return []
    return sorted(m.get("name", "") for m in tags.get("models", []) if m.get("name"))


@dataclass
class OllamaProvider:
    """Same interface as CLIProvider and APIProvider, backed by a local server."""

    name: str = "ollama"
    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    max_tokens: int = 2048
    temperature: float = 0.2
    timeout_s: float = 600.0
    _models: list[str] = field(default_factory=list)

    def refresh(self) -> list[str]:
        self._models = installed_models(self.host)
        return self._models

    def available(self) -> bool:
        models = self._models or self.refresh()
        return bool(models) and self.model in models

    def unavailable_reason(self) -> str:
        models = self._models or self.refresh()
        if not models:
            return (f"no Ollama server answering at {_host(self.host)} "
                    f"(start one with `ollama serve`)")
        return (f"model {self.model!r} is not pulled; "
                f"available here: {', '.join(models)}")

    def ask(self, prompt: str, cwd: Path | None = None,
            env: dict[str, str] | None = None) -> LLMReply:
        if not self.available():
            raise ProviderUnavailable(self.unavailable_reason())

        started = time.time()
        try:
            data = _post(f"{_host(self.host)}/api/chat", {
                "model": self.model,
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"num_predict": self.max_tokens,
                            "temperature": self.temperature},
            }, self.timeout_s)
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(
                f"ollama returned {exc.code}: {exc.read()[:200].decode(errors='replace')}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderUnavailable(f"cannot reach ollama: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(f"ollama sent malformed JSON: {exc}") from exc

        text = (data.get("message") or {}).get("content", "")
        return LLMReply(
            text=text.strip(),
            provider=f"{self.name}:{self.model}",
            seconds=time.time() - started,
            tokens_in=int(data.get("prompt_eval_count") or 0),
            tokens_out=int(data.get("eval_count") or 0),
            usd=0.0,
        )
