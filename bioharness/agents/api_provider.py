"""Claude API provider: agents backed by the Anthropic SDK rather than a CLI.

Compared with the CLI providers in `base.py`, this reports real token usage
per call and allows per-agent model, effort and thinking settings.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import LLMReply, ProviderUnavailable

try:
    import anthropic
except ImportError:  # pragma: no cover - the provider reports itself unavailable
    anthropic = None

DEFAULT_MODEL = "claude-opus-5"

# USD per million tokens. Cache reads are ~0.1x input, cache writes ~1.25x.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5-1": (10.00, 50.00),
}

# Models offered in the dashboard's hire form.
MODELS = list(PRICING)

# Default reasoning effort per role.
ROLE_EFFORT = {
    "planner": "high", "analyst": "high", "pi": "high",
    "curator": "low", "validator": "low", "scribe": "medium",
}


def price(model: str, usage: Any) -> float:
    """Cost of one call in USD, from the response's own usage figures."""
    rate_in, rate_out = PRICING.get(model, PRICING[DEFAULT_MODEL])
    plain = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (plain * rate_in
            + cache_read * rate_in * 0.1
            + cache_write * rate_in * 1.25
            + out * rate_out) / 1_000_000


def _ant_profile_active() -> bool:
    """`ant auth login` stores a profile the SDK reads with no env var set."""
    try:
        proc = subprocess.run(["ant", "auth", "status"], capture_output=True,
                              text=True, timeout=10)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "no active" not in proc.stdout.lower()


def credentials_present() -> tuple[bool, str]:
    """An unset ANTHROPIC_API_KEY does not mean there are no credentials."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True, "ANTHROPIC_API_KEY"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True, "ANTHROPIC_AUTH_TOKEN"
    if _ant_profile_active():
        return True, "ant auth profile"
    return False, ("no credentials: set ANTHROPIC_API_KEY or run `ant auth login`")


@dataclass
class APIProvider:
    """Drop-in replacement for CLIProvider, backed by the Messages API."""

    name: str = "claude-api"
    model: str = DEFAULT_MODEL
    max_tokens: int = 16000
    effort: str = "high"          # low | medium | high | xhigh | max
    thinking: bool = True
    timeout_s: float = 600.0
    _client: Any = None

    def available(self) -> bool:
        return anthropic is not None and credentials_present()[0]

    def unavailable_reason(self) -> str:
        if anthropic is None:
            return "the `anthropic` package is not installed (pip install anthropic)"
        return credentials_present()[1]

    def client(self) -> Any:
        if self._client is None:
            if anthropic is None:
                raise ProviderUnavailable("the `anthropic` package is not installed")
            # Resolves API key, auth token, or `ant auth login` profile, in that order.
            self._client = anthropic.Anthropic(timeout=self.timeout_s)
        return self._client

    def ask(self, prompt: str, cwd: Path | None = None,
            env: dict[str, str] | None = None) -> LLMReply:
        if anthropic is None:
            raise ProviderUnavailable("the `anthropic` package is not installed")
        ok, why = credentials_present()
        if not ok:
            raise ProviderUnavailable(why)

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": self.effort},
        }
        if self.thinking:
            request["thinking"] = {"type": "adaptive"}

        started = time.time()
        try:
            response = self.client().messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise ProviderUnavailable(f"authentication failed: {exc}") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderUnavailable(f"key lacks permission: {exc}") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderUnavailable(f"unknown model {self.model!r}: {exc}") from exc
        except anthropic.RateLimitError as exc:
            retry = exc.response.headers.get("retry-after", "60")
            raise ProviderUnavailable(f"rate limited; retry after {retry}s") from exc
        except anthropic.APIStatusError as exc:
            text = str(getattr(exc, "message", "") or exc)
            # Out of credit fails here, not at auth; say so explicitly.
            if "credit balance" in text.lower() or "billing" in text.lower():
                raise ProviderUnavailable(
                    "the API key is valid but the account has no credit balance "
                    "(add credit at console.anthropic.com under Plans & Billing)"
                ) from exc
            raise ProviderUnavailable(f"API error {exc.status_code}: {text}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(f"network error reaching the API: {exc}") from exc

        # Refusals arrive as HTTP 200; check before reading content.
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            category = getattr(detail, "category", None) if detail else None
            raise ProviderUnavailable(
                f"the model declined this request"
                + (f" (category: {category})" if category else ""))

        text = "\n".join(b.text for b in response.content if b.type == "text")
        usage = response.usage
        return LLMReply(
            text=text.strip(),
            provider=f"{self.name}:{self.model}",
            seconds=time.time() - started,
            tokens_in=(getattr(usage, "input_tokens", 0) or 0)
                      + (getattr(usage, "cache_read_input_tokens", 0) or 0),
            tokens_out=getattr(usage, "output_tokens", 0) or 0,
            usd=price(self.model, usage),
        )


def make_provider(kind: str, model: str | None = None, **kw: Any) -> Any:
    """Build a provider by name: `claude-api`, `ollama`, or any CLI provider."""
    from .base import CLIProvider, PROVIDERS
    from .antigravity_provider import AntigravityProvider
    from .gemini_provider import GeminiProvider
    from .local_provider import DEFAULT_MODEL as LOCAL_MODEL, OllamaProvider
    if kind in ("claude-api", "api", "anthropic"):
        return APIProvider(model=model or DEFAULT_MODEL,
                           effort=kw.pop("effort", "high"), **kw)
    if kind in ("ollama", "local"):
        kw.pop("effort", None)
        return OllamaProvider(model=model or LOCAL_MODEL, **kw)
    if kind in ("antigravity", "agy"):
        return AntigravityProvider(model=model or "",
                                   effort=kw.pop("effort", "") or "", **kw)
    if kind in ("gemini-api", "google"):
        kw.pop("effort", None)
        return GeminiProvider(model=model or "", **kw)
    if kind in PROVIDERS:
        return CLIProvider(kind, model, **kw)
    raise ProviderUnavailable(
        f"unknown provider {kind!r}; known: claude-api, gemini-api, ollama, "
        f"{', '.join(sorted(PROVIDERS))}")
