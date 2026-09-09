"""Gemini provider: key discovery, model discovery, and error translation.

No network: every path is exercised against stubs or an unreachable host.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bioharness.agents.api_provider import make_provider
from bioharness.agents.base import ProviderUnavailable
from bioharness.agents.gemini_provider import (KEY_VARS, GeminiProvider, _explain,
                                               api_key, available_models)

MOD = "bioharness.agents.gemini_provider"


@pytest.fixture(autouse=True)
def no_ambient_key(monkeypatch):
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def http_error(code: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("u", code, "err", {}, BytesIO(body))


# -- credentials -----------------------------------------------------------
@pytest.mark.parametrize("var", KEY_VARS)
def test_any_of_the_documented_key_vars_is_accepted(monkeypatch, var):
    monkeypatch.setenv(var, "AIza-test")
    assert api_key() == "AIza-test"


def test_no_key_is_reported_clearly():
    p = GeminiProvider()
    assert not p.available()
    reason = p.unavailable_reason()
    assert "GEMINI_API_KEY" in reason
    # The distinction that actually trips people up.
    assert "AV_API_KEY" in reason


def test_asking_without_a_key_raises():
    with pytest.raises(ProviderUnavailable, match="no Gemini API key"):
        GeminiProvider().ask("hello")


def test_model_listing_is_empty_without_a_key():
    assert available_models() == []


# -- model discovery -------------------------------------------------------
def test_only_models_that_can_generate_are_offered(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(f"{MOD}._request", lambda *a, **k: {"models": [
        {"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embed-y", "supportedGenerationMethods": ["embedContent"]},
    ]})
    assert available_models() == ["gemini-x"]


def test_a_model_is_chosen_when_none_was_named(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(f"{MOD}.available_models",
                        lambda *a, **k: ["gemini-pro-x", "gemini-flash-x"])
    p = GeminiProvider()
    p.refresh()
    assert p.model == "gemini-flash-x", "the cheaper flash tier should win by default"


def test_a_model_the_key_cannot_use_is_reported(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(f"{MOD}.available_models", lambda *a, **k: ["gemini-x"])
    p = GeminiProvider(model="gemini-nonexistent")
    assert not p.available()
    assert "not available to this key" in p.unavailable_reason()


# -- error translation -----------------------------------------------------
@pytest.mark.parametrize("code,expect", [
    (401, "key was rejected"), (403, "key was rejected"),
    (429, "rate limit"), (404, "no such Gemini model"), (500, "error 500"),
])
def test_http_errors_become_readable(code, expect):
    assert expect in _explain(http_error(code, "boom"))


def test_a_rejected_key_surfaces_as_provider_unavailable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "bad")
    monkeypatch.setattr(f"{MOD}.available_models", lambda *a, **k: ["gemini-x"])

    def boom(*a, **k):
        raise http_error(403, "API key not valid")
    monkeypatch.setattr(f"{MOD}._request", boom)
    with pytest.raises(ProviderUnavailable, match="rejected"):
        GeminiProvider(model="gemini-x").ask("hi")


def test_unreachable_endpoint_is_reported_not_raised_raw(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(f"{MOD}.API_ROOT", "http://127.0.0.1:1/v1beta")
    monkeypatch.setattr(f"{MOD}.available_models", lambda *a, **k: ["gemini-x"])
    with pytest.raises(ProviderUnavailable, match="cannot reach"):
        GeminiProvider(model="gemini-x").ask("hi")


def test_a_blocked_prompt_is_explained(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(f"{MOD}.available_models", lambda *a, **k: ["gemini-x"])
    monkeypatch.setattr(f"{MOD}._request",
                        lambda *a, **k: {"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(ProviderUnavailable, match="SAFETY"):
        GeminiProvider(model="gemini-x").ask("hi")


# -- a successful call -----------------------------------------------------
def test_reply_carries_text_and_token_usage(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(f"{MOD}.available_models", lambda *a, **k: ["gemini-x"])
    monkeypatch.setattr(f"{MOD}._request", lambda *a, **k: {
        "candidates": [{"content": {"parts": [{"text": "BWA"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 3},
    })
    reply = GeminiProvider(model="gemini-x").ask("name an aligner")
    assert reply.text == "BWA"
    assert reply.tokens_in == 40 and reply.tokens_out == 3
    assert reply.provider == "gemini-api:gemini-x"
    # Tokens are recorded; no price table is claimed for Gemini.
    assert reply.usd == 0.0


def test_factory_builds_the_gemini_provider():
    p = make_provider("gemini-api", "gemini-x")
    assert isinstance(p, GeminiProvider) and p.model == "gemini-x"
