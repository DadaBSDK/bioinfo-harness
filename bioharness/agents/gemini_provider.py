"""Gemini provider: agents backed by Google's Gemini API.

Distinct from the Antigravity CLI, which authenticates with its own
`AV_API_KEY` from the Antigravity dashboard rather than a Gemini API key. This
provider takes an AI Studio key (`GEMINI_API_KEY` / `GOOGLE_API_KEY`) and talks
to the Generative Language API directly.

Model ids are discovered from the API rather than hardcoded -- Google's line-up
moves, and a stale constant is worse than an extra round trip on startup.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import LLMReply, ProviderUnavailable

API_ROOT = os.environ.get("GEMINI_API_ROOT",
                          "https://generativelanguage.googleapis.com/v1beta")
DEFAULT_MODEL = os.environ.get("BIOHARNESS_GEMINI_MODEL", "")
KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")


def api_key() -> str:
    for var in KEY_VARS:
        value = os.environ.get(var)
        if value:
            return value.strip()
    return ""


def _request(url: str, payload: dict[str, Any] | None, timeout: float,
             key: str = "") -> Any:
    """The key travels in a header, never the query string: URLs end up in
    proxy logs, access logs and shell history."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-goog-api-key"] = key
    req = urllib.request.Request(
        url, data=data, method="POST" if payload is not None else "GET",
        headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# Overload and rate limiting pass; a bad key or a missing model does not.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _explain(exc: urllib.error.HTTPError) -> str:
    """Turn Google's error envelope into something a person can act on."""
    try:
        body = json.loads(exc.read())
        message = body.get("error", {}).get("message", "")
    except (json.JSONDecodeError, OSError, ValueError):
        message = ""
    if exc.code in (401, 403):
        return f"the Gemini API key was rejected ({exc.code}): {message or 'check the key'}"
    if exc.code == 429:
        return f"Gemini rate limit or quota exhausted: {message}"
    if exc.code == 404:
        return f"no such Gemini model: {message}"
    return f"Gemini API error {exc.code}: {message or exc.reason}"


_CACHE: dict[str, Any] = {"models": None, "at": 0.0, "key": ""}
CACHE_TTL = 300.0


def available_models(key: str = "", timeout: float = 10.0,
                     refresh: bool = False) -> list[str]:
    """Models this key may actually call generateContent on. Cached briefly:
    the dashboard asks on every poll and this is a network round trip."""
    key = key or api_key()
    if not key:
        return []
    if (not refresh and _CACHE["models"] is not None and _CACHE["key"] == key
            and time.time() - _CACHE["at"] < CACHE_TTL):
        return _CACHE["models"]
    try:
        data = _request(f"{API_ROOT}/models?pageSize=200", None, timeout, key)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        _CACHE.update(models=[], at=time.time(), key=key)
        return []
    out = []
    for m in data.get("models", []):
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        out.append(str(m.get("name", "")).removeprefix("models/"))
    models = sorted(n for n in out if n)
    _CACHE.update(models=models, at=time.time(), key=key)
    return models


@dataclass
class GeminiProvider:
    """Same interface as the Claude, Ollama and CLI providers."""

    name: str = "gemini-api"
    model: str = DEFAULT_MODEL
    max_tokens: int = 2048
    temperature: float = 0.2
    timeout_s: float = 300.0
    retries: int = 3
    _models: list[str] = field(default_factory=list)

    def refresh(self) -> list[str]:
        self._models = available_models()
        # With no model named, take the first the key can actually use.
        if not self.model and self._models:
            preferred = ([m for m in self._models if m == "gemini-flash-latest"]
                         or [m for m in self._models if "flash" in m and "lite" not in m]
                         or self._models)
            self.model = preferred[0]
        return self._models

    def available(self) -> bool:
        if not api_key():
            return False
        models = self._models or self.refresh()
        return bool(models) and (not self.model or self.model in models)

    def unavailable_reason(self) -> str:
        if not api_key():
            return (f"no Gemini API key: set one of {', '.join(KEY_VARS)} "
                    f"(an AI Studio key, not an Antigravity AV_API_KEY)")
        models = self._models or self.refresh()
        if not models:
            return "the key was rejected, or the Gemini API is unreachable"
        return f"model {self.model!r} is not available to this key"

    def _call(self, url: str, payload: dict[str, Any], key: str) -> Any:
        try:
            return _request(url, payload, self.timeout_s, key)
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(_explain(exc),
                                      retryable=exc.code in RETRYABLE_STATUS) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderUnavailable(f"cannot reach the Gemini API: {exc}",
                                      retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(f"Gemini sent malformed JSON: {exc}") from exc

    def ask(self, prompt: str, cwd: Path | None = None,
            env: dict[str, str] | None = None) -> LLMReply:
        key = api_key()
        if not key:
            raise ProviderUnavailable(self.unavailable_reason())
        if not self.model:
            self.refresh()
        if not self.model:
            raise ProviderUnavailable(self.unavailable_reason())

        url = f"{API_ROOT}/models/{urllib.parse.quote(self.model)}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": self.max_tokens,
                                 "temperature": self.temperature},
        }

        started = time.time()
        data = None
        for attempt in range(self.retries + 1):
            try:
                data = self._call(url, payload, key)
                break
            except ProviderUnavailable as exc:
                # Ride out a busy service rather than failing the mission for it.
                if not exc.retryable or attempt == self.retries:
                    raise
                time.sleep(min(8.0, 1.5 * (2 ** attempt)) + random.uniform(0, 0.4))
        assert data is not None

        candidates = data.get("candidates") or []
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise ProviderUnavailable(
                f"Gemini returned no candidates"
                + (f" (blocked: {blocked})" if blocked else ""))

        first = candidates[0]
        text = "".join(p.get("text", "")
                       for p in (first.get("content") or {}).get("parts", []))
        if not text.strip() and first.get("finishReason") not in (None, "STOP"):
            raise ProviderUnavailable(
                f"Gemini stopped without output (finishReason: {first['finishReason']})")

        usage = data.get("usageMetadata") or {}
        # Gemini bills thinking separately from the visible answer, and it
        # dominates: a two-token reply can carry ~190 thinking tokens. Counting
        # only candidatesTokenCount under-reports usage by an order of magnitude.
        generated = (int(usage.get("candidatesTokenCount") or 0)
                     + int(usage.get("thoughtsTokenCount") or 0))
        return LLMReply(
            text=text.strip(),
            provider=f"{self.name}:{self.model}",
            seconds=time.time() - started,
            tokens_in=int(usage.get("promptTokenCount") or 0),
            tokens_out=generated,
            # Tokens are recorded; dollars are not, because this harness does
            # not carry a verified Gemini price table. The ledger will show
            # usage without inventing a cost.
            usd=0.0,
        )


def peek_models() -> list[str] | None:
    """Whatever is cached, without spawning anything. None means not yet known."""
    return _CACHE["models"]


def warm_models() -> None:
    """Refresh the cache in the background.

    The dashboard polls every few seconds and listing models is slow (a CLI
    spawn or a network round trip). Blocking the request on it makes the whole
    panel feel broken, so the endpoint reads the cache and this fills it.
    """
    if _CACHE.get("warming"):
        return
    _CACHE["warming"] = True

    def run() -> None:
        try:
            available_models(refresh=True)
        except Exception:  # noqa: BLE001 - a warm-up must never take the server down
            pass
        finally:
            _CACHE["warming"] = False

    threading.Thread(target=run, daemon=True).start()
