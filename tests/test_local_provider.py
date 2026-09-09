"""Local (Ollama) provider, truncated-JSON repair, and plan validation.

Nothing here needs a running Ollama server: the provider is exercised against
its own failure paths, and the parsing/validation logic is pure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bioharness.agents.base import (AgentContext, extract_json, repair_truncated_json,
                                    ProviderUnavailable)
from bioharness.agents.local_provider import OllamaProvider, _host, installed_models
from bioharness.agents.api_provider import make_provider
from bioharness.agents.roles import Planner
from bioharness.core.guardrails import Guardrails
from bioharness.core.hive import Hive
from bioharness.core.registry import Registry

ROOT = Path(__file__).resolve().parents[1]


# -- provider --------------------------------------------------------------
def test_host_normalisation():
    assert _host("127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert _host("http://box:11434/") == "http://box:11434"


def test_unreachable_server_is_reported_not_raised():
    p = OllamaProvider(host="http://127.0.0.1:1")     # nothing listens here
    assert not p.available()
    assert "no Ollama server" in p.unavailable_reason()


def test_asking_an_unreachable_server_raises_provider_unavailable():
    p = OllamaProvider(host="http://127.0.0.1:1")
    with pytest.raises(ProviderUnavailable):
        p.ask("hello")


def test_missing_model_names_what_is_installed(monkeypatch):
    monkeypatch.setattr("bioharness.agents.local_provider.installed_models",
                        lambda *a, **k: ["qwen2.5:3b"])
    p = OllamaProvider(model="not-pulled:70b")
    assert not p.available()
    reason = p.unavailable_reason()
    assert "not pulled" in reason and "qwen2.5:3b" in reason


def test_local_inference_is_free(monkeypatch):
    """Cost is measured in tokens, never dollars, for a model on your own box."""
    monkeypatch.setattr("bioharness.agents.local_provider.installed_models",
                        lambda *a, **k: ["m"])
    monkeypatch.setattr("bioharness.agents.local_provider._post",
                        lambda *a, **k: {"message": {"content": "hi"},
                                         "prompt_eval_count": 12, "eval_count": 3})
    reply = OllamaProvider(model="m").ask("x")
    assert reply.usd == 0.0
    assert reply.tokens_in == 12 and reply.tokens_out == 3


def test_factory_builds_the_local_provider():
    p = make_provider("ollama", "qwen2.5:3b")
    assert isinstance(p, OllamaProvider) and p.model == "qwen2.5:3b"


def test_installed_models_is_empty_when_nothing_answers():
    assert installed_models("http://127.0.0.1:1", timeout=1.0) == []


# -- truncated JSON --------------------------------------------------------
def test_repairs_a_reply_cut_one_brace_short():
    truncated = ('{"name": "W", "steps": [{"id": "s1", "capability": "fastp", '
                 '"needs": []}, {"id": "s2", "capability": "bwa_mem", "needs": ["s1"]}]')
    got = extract_json(truncated)
    assert got["name"] == "W"
    assert [s["capability"] for s in got["steps"]] == ["fastp", "bwa_mem"]


def test_repairs_a_reply_cut_mid_string():
    got = extract_json('{"name": "W", "steps": [{"id": "s1", "capability": "fast')
    assert got["name"] == "W" and got["steps"] == [{"id": "s1"}]


def test_repair_keeps_complete_json_untouched():
    doc = {"a": 1, "b": [1, 2, {"c": 3}]}
    assert extract_json(json.dumps(doc)) == doc


def test_fenced_and_chatty_output_still_parses():
    assert extract_json('sure!\n```json\n{"a": 1}\n```\nhope that helps') == {"a": 1}


def test_unrepairable_text_still_raises():
    with pytest.raises(ValueError, match="no JSON"):
        extract_json("I would rather not.")


def test_repair_returns_none_when_there_is_no_json():
    assert repair_truncated_json("nothing here") is None


# -- plan validation -------------------------------------------------------
@pytest.fixture
def planner(tmp_path: Path) -> Planner:
    ctx = AgentContext(hive=Hive(tmp_path / "hive"),
                       registry=Registry.load(ROOT / "capabilities"),
                       guardrails=Guardrails(), workdir=tmp_path / "agents")
    return Planner("planner", ctx)


def test_invented_capabilities_are_dropped(planner):
    wf = planner._validate_workflow({"name": "w", "steps": [
        {"id": "a", "capability": "fastp"},
        {"id": "b", "capability": "quantum_aligner_9000"}]})
    assert [s["capability"] for s in wf["steps"]] == ["fastp"]


def test_dependencies_on_non_steps_are_dropped(planner):
    """Small models put port names and templates in `needs`."""
    wf = planner._validate_workflow({"name": "w", "steps": [
        {"id": "s1", "capability": "fastp", "needs": ["reference"]},
        {"id": "s2", "capability": "bwa_mem",
         "needs": ["{{ steps.1.outputs.bam }}", "s1"]}]})
    assert wf["steps"][0]["needs"] == []
    assert wf["steps"][1]["needs"] == ["s1"]


def test_forward_references_are_dropped(planner):
    """A step may only depend on one already accepted above it."""
    wf = planner._validate_workflow({"name": "w", "steps": [
        {"id": "s1", "capability": "fastp", "needs": ["s2"]},
        {"id": "s2", "capability": "fastqc"}]})
    assert wf["steps"][0]["needs"] == []


def test_a_validated_plan_is_always_a_runnable_dag(planner):
    from bioharness.core.loader import workflow_from_dict
    from bioharness.core.orchestrator import topo_sort
    wf = planner._validate_workflow({"name": "w", "steps": [
        {"id": "s1", "capability": "fastp", "needs": ["ghost"]},
        {"id": "s2", "capability": "fastqc", "needs": ["s1"]},
        {"id": "s3", "capability": "bwa_mem", "needs": ["s2", "nonsense"]}]})
    assert [s.id for s in topo_sort(workflow_from_dict(wf))] == ["s1", "s2", "s3"]


def test_a_plan_with_no_real_capability_is_rejected(planner):
    assert planner._validate_workflow(
        {"name": "w", "steps": [{"id": "a", "capability": "nope"}]}) is None


def test_junk_shapes_are_rejected(planner):
    assert planner._validate_workflow(None) is None
    assert planner._validate_workflow({"name": "w"}) is None
    assert planner._validate_workflow({"name": "w", "steps": "not a list"}) is None


def test_duplicate_step_ids_are_made_unique(planner):
    wf = planner._validate_workflow({"name": "w", "steps": [
        {"id": "s", "capability": "fastp"}, {"id": "s", "capability": "fastqc"}]})
    assert len({s["id"] for s in wf["steps"]}) == 2
