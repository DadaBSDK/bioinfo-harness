"""Antigravity CLI provider. No subprocess is really spawned."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bioharness.agents.antigravity_provider import (AntigravityProvider, available_models,
                                                    binary_present)
from bioharness.agents.api_provider import make_provider
from bioharness.agents.base import PROVIDERS, ProviderUnavailable

MOD = "bioharness.agents.antigravity_provider"
OK_JSON = {
    "conversation_id": "abc", "status": "SUCCESS", "response": "harness online\n",
    "duration_seconds": 3.0, "num_turns": 1,
    "usage": {"input_tokens": 5450, "output_tokens": 28, "thinking_tokens": 25,
              "cache_read_tokens": 8129, "total_tokens": 5478},
}


def run_returns(stdout="", stderr="", code=0):
    return lambda *a, **k: SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


@pytest.fixture(autouse=True)
def clear_cache():
    """The model listing is cached across calls; each test starts cold."""
    from bioharness.agents import antigravity_provider as ap
    ap._CACHE.update(models=None, at=0.0)
    yield
    ap._CACHE.update(models=None, at=0.0)


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(f"{MOD}.shutil.which", lambda _: "/usr/bin/agy")


def test_missing_binary_is_reported(monkeypatch):
    monkeypatch.setattr(f"{MOD}.shutil.which", lambda _: None)
    p = AntigravityProvider()
    assert not p.available()
    assert "not on PATH" in p.unavailable_reason()
    assert available_models() == []


def test_models_are_parsed_from_the_tab_separated_listing(monkeypatch, installed):
    listing = ("Fetching available models...\n"
               "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
               "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n")
    monkeypatch.setattr(f"{MOD}.subprocess.run", run_returns(stdout=listing))
    assert available_models() == ["gemini-3.8-flash-low", "claude-sonnet-4-6"]


def test_a_model_the_cli_does_not_offer_is_reported(monkeypatch, installed):
    monkeypatch.setattr(f"{MOD}.available_models", lambda *a, **k: ["gemini-3.8-flash-low"])
    p = AntigravityProvider(model="not-a-model")
    assert not p.available()
    assert "not offered" in p.unavailable_reason()


def test_no_model_named_is_allowed(monkeypatch, installed):
    """With no --model the CLI picks its own default; that is fine."""
    assert AntigravityProvider().available()


def test_usage_includes_thinking_and_cache_reads(monkeypatch, installed):
    monkeypatch.setattr(f"{MOD}.subprocess.run", run_returns(stdout=json.dumps(OK_JSON)))
    r = AntigravityProvider(model="m").ask("hi")
    assert r.text == "harness online"
    assert r.tokens_in == 5450 + 8129
    assert r.tokens_out == 28 + 25
    assert r.seconds == 3.0
    assert r.usd == 0.0


def test_model_and_effort_reach_the_command_line(monkeypatch, installed):
    seen = {}

    def capture(cmd, **k):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout=json.dumps(OK_JSON), stderr="", returncode=0)
    monkeypatch.setattr(f"{MOD}.subprocess.run", capture)
    AntigravityProvider(model="gemini-3.8-flash-low", effort="high").ask("hi")
    cmd = seen["cmd"]
    assert "--print" in cmd and "--output-format" in cmd and "json" in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-low"
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_an_invalid_effort_is_not_passed_through(monkeypatch, installed):
    seen = {}

    def capture(cmd, **k):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout=json.dumps(OK_JSON), stderr="", returncode=0)
    monkeypatch.setattr(f"{MOD}.subprocess.run", capture)
    AntigravityProvider(model="m", effort="turbo").ask("hi")
    assert "--effort" not in seen["cmd"]


def test_a_nonzero_exit_is_surfaced(monkeypatch, installed):
    monkeypatch.setattr(f"{MOD}.subprocess.run",
                        run_returns(stderr="not signed in", code=1))
    with pytest.raises(ProviderUnavailable, match="exited 1"):
        AntigravityProvider(model="m").ask("hi")


def test_non_json_output_is_surfaced(monkeypatch, installed):
    monkeypatch.setattr(f"{MOD}.subprocess.run", run_returns(stdout="Welcome!"))
    with pytest.raises(ProviderUnavailable, match="did not return JSON"):
        AntigravityProvider(model="m").ask("hi")


def test_a_failure_status_is_surfaced(monkeypatch, installed):
    monkeypatch.setattr(f"{MOD}.subprocess.run", run_returns(
        stdout=json.dumps({"status": "ERROR", "error": "quota exhausted"})))
    with pytest.raises(ProviderUnavailable, match="ERROR"):
        AntigravityProvider(model="m").ask("hi")


@pytest.mark.parametrize("status,retryable", [
    ("RATE_LIMITED", True), ("TIMEOUT", True), ("UNAVAILABLE", True), ("ERROR", False)])
def test_transient_statuses_are_marked_retryable(monkeypatch, installed, status, retryable):
    monkeypatch.setattr(f"{MOD}.subprocess.run",
                        run_returns(stdout=json.dumps({"status": status})))
    with pytest.raises(ProviderUnavailable) as exc:
        AntigravityProvider(model="m").ask("hi")
    assert exc.value.retryable is retryable


def test_a_timeout_is_retryable(monkeypatch, installed):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("agy", 1)
    monkeypatch.setattr(f"{MOD}.subprocess.run", slow)
    with pytest.raises(ProviderUnavailable) as exc:
        AntigravityProvider(model="m", timeout_s=1).ask("hi")
    assert exc.value.retryable is True


def test_factory_builds_the_dedicated_provider():
    p = make_provider("antigravity", "gemini-3.8-flash-low")
    assert isinstance(p, AntigravityProvider)
    assert p.model == "gemini-3.8-flash-low"


def test_the_generic_cli_wrapper_does_not_shadow_it():
    """make_provider must pick the dedicated provider, which reports token usage."""
    assert "antigravity" not in PROVIDERS


def test_the_model_listing_is_cached(monkeypatch, installed):
    """Model listing spawns the CLI, so results are cached."""
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return SimpleNamespace(stdout="m1\tOne\n", stderr="", returncode=0)
    monkeypatch.setattr(f"{MOD}.subprocess.run", counted)
    assert available_models() == ["m1"]
    assert available_models() == ["m1"]
    assert calls["n"] == 1, "the second call must come from cache"


def test_refresh_bypasses_the_cache(monkeypatch, installed):
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return SimpleNamespace(stdout="m1\tOne\n", stderr="", returncode=0)
    monkeypatch.setattr(f"{MOD}.subprocess.run", counted)
    available_models()
    available_models(refresh=True)
    assert calls["n"] == 2


def test_a_failed_listing_is_cached_too(monkeypatch, installed):
    """A failed listing is cached, so polls do not retry a slow failure."""
    calls = {"n": 0}

    def failing(*a, **k):
        calls["n"] += 1
        return SimpleNamespace(stdout="", stderr="not signed in", returncode=1)
    monkeypatch.setattr(f"{MOD}.subprocess.run", failing)
    assert available_models() == []
    assert available_models() == []
    assert calls["n"] == 1


def test_structured_output_wins_over_the_prose_response(monkeypatch, installed):
    """With --json-schema the CLI puts the validated object in
    structured_output and leaves response as fenced prose."""
    monkeypatch.setattr(f"{MOD}.subprocess.run", run_returns(stdout=json.dumps({
        "status": "SUCCESS",
        "response": '```json\n{"name":"x"}\n```\n{"toolAction":"Finishing task"}\n',
        "structured_output": {"name": "x", "steps": []},
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })))
    text = AntigravityProvider(model="m", json_schema="{}").ask("hi").text
    assert json.loads(text) == {"name": "x", "steps": []}


def test_the_schema_reaches_the_command_line(monkeypatch, installed):
    seen = {}

    def capture(cmd, **k):
        seen["cmd"] = cmd
        return SimpleNamespace(stdout=json.dumps(OK_JSON), stderr="", returncode=0)
    monkeypatch.setattr(f"{MOD}.subprocess.run", capture)
    AntigravityProvider(model="m", json_schema='{"type":"object"}').ask("hi")
    assert seen["cmd"][seen["cmd"].index("--json-schema") + 1] == '{"type":"object"}'


def test_an_empty_schema_reply_is_retried_without_the_schema(monkeypatch, installed):
    """An empty reply to a schema-constrained prompt is retried without the
    schema.
    """
    calls = []

    def flaky(cmd, **k):
        calls.append(cmd)
        if "--json-schema" in cmd:
            return SimpleNamespace(stdout=json.dumps(
                {"status": "SUCCESS", "response": "",
                 "usage": {"input_tokens": 10, "output_tokens": 1186}}),
                stderr="", returncode=0)
        return SimpleNamespace(stdout=json.dumps(
            {"status": "SUCCESS", "response": '{"name":"recovered"}',
             "usage": {"input_tokens": 10, "output_tokens": 20}}),
            stderr="", returncode=0)
    monkeypatch.setattr(f"{MOD}.subprocess.run", flaky)
    r = AntigravityProvider(model="m", json_schema='{"type":"object"}').ask("hi")
    assert json.loads(r.text) == {"name": "recovered"}
    assert len(calls) == 2 and "--json-schema" not in calls[1]
    assert r.tokens_out == 1186 + 20


def test_a_good_schema_reply_is_not_retried(monkeypatch, installed):
    calls = []

    def once(cmd, **k):
        calls.append(cmd)
        return SimpleNamespace(stdout=json.dumps(
            {"status": "SUCCESS", "structured_output": {"ok": True},
             "usage": {"input_tokens": 1, "output_tokens": 1}}),
            stderr="", returncode=0)
    monkeypatch.setattr(f"{MOD}.subprocess.run", once)
    AntigravityProvider(model="m", json_schema="{}").ask("hi")
    assert len(calls) == 1


def test_no_schema_means_no_retry_on_empty(monkeypatch, installed):
    monkeypatch.setattr(f"{MOD}.subprocess.run", run_returns(stdout=json.dumps(
        {"status": "SUCCESS", "response": "", "usage": {}})))
    assert AntigravityProvider(model="m").ask("hi").text == ""
