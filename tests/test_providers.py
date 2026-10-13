"""Claude API provider: pricing, credential detection, and error surfacing."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bioharness.agents.api_provider import (DEFAULT_MODEL, MODELS, PRICING, APIProvider,
                                            credentials_present, make_provider, price)
from bioharness.agents.base import CLIProvider, ProviderUnavailable


def usage(**kw):
    base = {"input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return SimpleNamespace(**{**base, **kw})


def test_price_uses_the_published_rates():
    assert price("claude-opus-5", usage(input_tokens=1_000_000)) == pytest.approx(5.0)
    assert price("claude-opus-5", usage(output_tokens=1_000_000)) == pytest.approx(25.0)


def test_cache_reads_are_a_tenth_of_input():
    plain = price("claude-opus-5", usage(input_tokens=100_000))
    cached = price("claude-opus-5", usage(cache_read_input_tokens=100_000))
    assert cached == pytest.approx(plain * 0.1)


def test_cache_writes_cost_a_premium():
    plain = price("claude-opus-5", usage(input_tokens=100_000))
    written = price("claude-opus-5", usage(cache_creation_input_tokens=100_000))
    assert written == pytest.approx(plain * 1.25)


def test_unknown_model_falls_back_to_the_default_rate():
    assert price("some-unreleased-model", usage(input_tokens=1_000_000)) == \
        pytest.approx(PRICING[DEFAULT_MODEL][0])


def test_every_offered_model_has_a_price():
    assert MODELS, "the hire form would have no models to offer"
    for model in MODELS:
        assert model in PRICING, f"{model} is offered but has no price"


def test_default_model_is_opus_5():
    assert DEFAULT_MODEL == "claude-opus-5"
    assert APIProvider().model == "claude-opus-5"


def test_api_key_is_detected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    ok, why = credentials_present()
    assert ok and why == "ANTHROPIC_API_KEY"


def test_auth_token_is_detected(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    ok, why = credentials_present()
    assert ok and why == "ANTHROPIC_AUTH_TOKEN"


def test_missing_credentials_explain_both_options(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("bioharness.agents.api_provider._ant_profile_active", lambda: False)
    ok, why = credentials_present()
    assert not ok
    assert "ANTHROPIC_API_KEY" in why and "ant auth login" in why


def test_provider_reports_itself_unusable_without_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("bioharness.agents.api_provider._ant_profile_active", lambda: False)
    p = APIProvider()
    assert not p.available()
    with pytest.raises(ProviderUnavailable, match="credentials"):
        p.ask("hello")


def test_make_provider_builds_the_api_provider():
    p = make_provider("claude-api", "claude-sonnet-5")
    assert isinstance(p, APIProvider) and p.model == "claude-sonnet-5"


def test_make_provider_builds_a_cli_provider():
    assert isinstance(make_provider("claude"), CLIProvider)


def test_make_provider_rejects_the_unknown():
    with pytest.raises(ProviderUnavailable, match="unknown provider"):
        make_provider("gpt-9")


def test_api_provider_matches_the_cli_provider_interface():
    """The Agent calls providers polymorphically; the shapes must agree."""
    api, cli = APIProvider(), CLIProvider("claude")
    for attr in ("name", "model", "available", "ask"):
        assert hasattr(api, attr) and hasattr(cli, attr), attr
