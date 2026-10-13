"""Dashboard server tests.

The dashboard must be strictly read-only and must never crash the run it is
watching, so these cover the failure paths as much as the happy ones.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bioharness.core.guardrails import Guardrails
from bioharness.core.hive import Hive
from bioharness.core.loader import workflow_from_dict
from bioharness.core.orchestrator import Orchestrator
from bioharness.core.registry import Registry, capability_from_dict
from bioharness.core.workspace import Workspace
from bioharness.server import Dashboard, serve

ECHO = {
    "id": "make_text", "domain": "test", "summary": "write a file",
    "runtime": {"kind": "binary", "binary": "sh"},
    "command": "sh -c 'echo hi > {{ outdir }}/out.txt'",
    "inputs": [], "outputs": [{"name": "text", "type": "text", "glob": "out.txt"}],
}
UPPER = {
    "id": "upper", "domain": "test", "summary": "uppercase",
    "runtime": {"kind": "binary", "binary": "sh"},
    "command": "sh -c 'tr a-z A-Z < {{ inputs.text }} > {{ outdir }}/upper.txt'",
    "inputs": [{"name": "text", "type": "text"}],
    "outputs": [{"name": "loud", "type": "shout", "glob": "upper.txt"}],
}

WORKFLOW = {"name": "demo", "steps": [
    {"id": "make", "capability": "make_text"},
    {"id": "shout", "capability": "upper", "needs": ["make"],
     "with": {"text": "{{ steps.make.outputs.text }}"}}]}


@pytest.fixture
def live(tmp_path: Path):
    """A dashboard over one completed run and a populated hive."""
    reg = Registry()
    for raw in (ECHO, UPPER):
        reg.register(capability_from_dict(raw))

    runs, hive_root = tmp_path / "runs", tmp_path / "hive"
    hive = Hive(hive_root)
    hive.enroll("planner", "planner")
    hive.enroll("pi", "pi")
    hive.send("pi", "planner", "plan.request", {"goal": "call variants"})
    hive.blackboard.put("qc/last", {"verdict": "pass"}, agent="validator")
    hive.append_memory("planner", "Routed fastq to vcf via bwa_mem.")
    hive.charge("planner", tokens_in=800, tokens_out=200, usd=0.015)

    orch = Orchestrator(reg, Workspace(runs), Guardrails(), hive)
    summary = orch.run(workflow_from_dict(WORKFLOW))

    caps = tmp_path / "caps"
    caps.mkdir()
    (caps / "test.yaml").write_text(json.dumps({"capabilities": [ECHO, UPPER]}))

    dash = Dashboard(runs, hive_root, [caps])
    server = serve(dash, port=0, block=False)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield dash, base, summary
    server.shutdown()
    server.server_close()


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return json.loads(r.read())


# -- reading state ---------------------------------------------------------
def test_overview_counts_runs_and_capabilities(live):
    _, base, _ = live
    o = get(base, "/api/overview")
    assert o["runs"]["total"] == 1 and o["runs"]["ok"] == 1
    assert o["capabilities"]["total"] == 2
    assert o["agents"] == 2


def test_run_list_reports_status(live):
    _, base, summary = live
    runs = get(base, "/api/runs")
    assert len(runs) == 1
    assert runs[0]["run_id"] == summary["run_id"]
    assert runs[0]["status"] == "ok"
    assert runs[0]["steps"] == 2          # graph recorded in run.json


def test_run_detail_exposes_graph_tasks_and_hashes(live):
    _, base, summary = live
    d = get(base, f"/api/runs/{summary['run_id']}")
    assert [s["id"] for s in d["graph"]] == ["make", "shout"]
    assert d["graph"][1]["needs"] == ["make"]
    assert {t["step"] for t in d["tasks"]} == {"make", "shout"}
    assert len(d["artifacts"][0]["sha256"]) == 64
    assert d["ledger"]


def test_run_log_returns_step_output(live):
    _, base, summary = live
    log = get(base, f"/api/runs/{summary['run_id']}/log?step=make&stream=stdout")
    assert log["exists"] is True


def test_hive_state_includes_mail_blackboard_and_cost(live):
    _, base, _ = live
    h = get(base, "/api/hive")
    planner = next(a for a in h["agents"] if a["name"] == "planner")
    assert planner["inbox"][0]["topic"] == "plan.request"
    assert planner["inbox_pending"] == 1       # peeked, never consumed
    assert any(e["key"] == "qc/last" for e in h["blackboard"])
    assert h["cost"]["total_usd"] == pytest.approx(0.015)


def test_dashboard_does_not_consume_mail(live):
    dash, base, _ = live
    get(base, "/api/hive")
    get(base, "/api/hive")
    assert dash.hive().mailbox("planner").pending() == 1


def test_caps_reports_availability(live):
    _, base, _ = live
    caps = get(base, "/api/caps")
    assert {c["id"] for c in caps} == {"make_text", "upper"}
    assert all(c["available"] for c in caps)


def test_recall_searches_the_palace(live):
    _, base, _ = live
    hits = get(base, "/api/recall?q=fastq")
    assert hits and "bwa_mem" in hits[0]["text"]


def test_index_html_is_served(live):
    _, base, _ = live
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        body = r.read().decode()
    assert "Bioinfo" in body and r.headers["Content-Type"].startswith("text/html")


# -- failure paths ---------------------------------------------------------
def test_unknown_run_returns_404(live):
    _, base, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/runs/run-does-not-exist")
    assert exc.value.code == 404


def test_unknown_endpoint_returns_404(live):
    _, base, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/nope")
    assert exc.value.code == 404


def test_path_traversal_is_refused(live):
    _, base, _ = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/../../../etc/passwd", timeout=5)
    assert exc.value.code == 404


def test_bad_run_identifier_is_rejected_before_touching_disk(live):
    dash, _, _ = live
    assert dash.run_detail("../../etc") is None
    assert dash.run_log("../../etc", "passwd")["error"] == "bad identifier"


def test_missing_paths_do_not_crash(tmp_path: Path):
    dash = Dashboard(tmp_path / "no-runs", tmp_path / "no-hive", [tmp_path / "no-caps"])
    assert dash.run_list() == []
    assert dash.overview()["runs"]["total"] == 0
    assert dash.hive_state()["agents"] == []


# -- floor state -----------------------------------------------------------
def test_floor_state_seats_and_statuses_agents(live):
    _, base, _ = live
    f = get(base, "/api/floor")
    by = {a["name"]: a for a in f["agents"]}
    assert set(by) >= {"pi", "planner"}
    # planner is holding the message pi sent it
    assert by["planner"]["status"] == "awaiting"
    assert by["planner"]["inbox_pending"] == 1
    assert all("role" in a and "status" in a for a in f["agents"])


def test_floor_state_reports_traffic_and_activity(live):
    _, base, _ = live
    f = get(base, "/api/floor")
    assert any(t["from"] == "pi" and t["to"] == "planner" for t in f["traffic"])
    assert f["activity"] and f["current_run"]["status"] == "ok"


def test_idle_agent_when_nothing_pending(live):
    dash, base, _ = live
    dash.hive().mailbox("planner").read(consume=True)     # drain the mail
    f = get(base, "/api/floor")
    planner = next(a for a in f["agents"] if a["name"] == "planner")
    assert planner["status"] in ("idle", "working")       # never "awaiting" now
    assert planner["inbox_pending"] == 0


# -- the one write path ----------------------------------------------------
def post(base: str, path: str, payload: dict):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_message_reaches_the_agent_mailbox(live):
    dash, base, _ = live
    status, body = post(base, "/api/message",
                        {"to": "pi", "topic": "goal", "body": {"goal": "call variants"}})
    assert status == 200 and body["ok"] is True
    msgs = dash.hive().mailbox("pi").peek()
    assert any(m.topic == "goal" and m.body.get("goal") == "call variants" for m in msgs)


def test_message_to_unknown_agent_is_refused(live):
    _, base, _ = live
    status, body = post(base, "/api/message", {"to": "nobody", "topic": "goal", "body": {}})
    assert status == 400 and body["ok"] is False
    assert "no agent named" in body["error"]


def test_message_with_disallowed_topic_is_refused(live):
    dash, base, _ = live
    status, body = post(base, "/api/message",
                        {"to": "pi", "topic": "rm -rf", "body": {}})
    assert status == 400 and "not allowed" in body["error"]
    assert dash.hive().mailbox("pi").pending() == 0


def test_malformed_json_is_refused(live):
    _, base, _ = live
    req = urllib.request.Request(base + "/api/message", data=b"{not json",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400


def test_lab_is_called_voxel_lab_until_named(live):
    _, base, _ = live
    assert get(base, "/api/floor")["lab_name"] == "Voxel Lab"


def test_renaming_the_lab_persists_in_the_hive(live):
    dash, base, _ = live
    status, body = post(base, "/api/lab", {"name": "  Kim   Lab "})
    assert status == 200 and body == {"ok": True, "name": "Kim Lab"}
    assert get(base, "/api/floor")["lab_name"] == "Kim Lab"
    assert Hive(dash.hive_root).lab_name() == "Kim Lab"


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
def test_unusable_lab_names_are_refused(live, name):
    dash, base, _ = live
    status, body = post(base, "/api/lab", {"name": name})
    assert status == 400 and body["ok"] is False
    assert dash.hive().lab_name() == "Voxel Lab"


def test_lab_name_must_be_a_string(live):
    _, base, _ = live
    status, _ = post(base, "/api/lab", {"name": 42})
    assert status == 400


def test_post_to_other_endpoints_is_refused(live):
    _, base, _ = live
    status, _ = post(base, "/api/runs", {})
    assert status == 404


def test_ui_assets_are_served(live):
    _, base, _ = live
    for asset, kind in (("/app.css", "text/css"), ("/app.js", "javascript")):
        with urllib.request.urlopen(base + asset, timeout=5) as r:
            assert r.status == 200
            assert kind in r.headers["Content-Type"]


# -- staffing --------------------------------------------------------------
def test_staff_options_lists_roles_providers_and_models(live):
    _, base, _ = live
    opt = get(base, "/api/staff/options")
    assert {"pi", "planner", "curator", "validator", "analyst", "scribe"} <= set(opt["roles"])
    ids = {p["id"] for p in opt["providers"]}
    assert {"builtin", "claude-api", "claude"} <= ids
    api = next(p for p in opt["providers"] if p["id"] == "claude-api")
    assert "claude-opus-5" in api["models"]
    assert isinstance(api["available"], bool) and api["reason"]


def test_hire_adds_an_agent_to_the_roster(live):
    dash, base, _ = live
    status, body = post(base, "/api/staff/hire",
                        {"name": "ada", "role": "analyst", "provider": "builtin"})
    assert status == 200 and body["ok"] is True
    assert any(a["name"] == "ada" for a in dash.hive().roster())


def test_hired_agent_keeps_its_provider_and_model(live):
    dash, base, _ = live
    post(base, "/api/staff/hire", {"name": "nyx", "role": "planner",
                                   "provider": "claude-api", "model": "claude-sonnet-5"})
    nyx = next(a for a in dash.hive().roster() if a["name"] == "nyx")
    assert nyx["provider"] == "claude-api" and nyx["model"] == "claude-sonnet-5"


def test_model_is_not_stored_for_non_api_providers(live):
    dash, base, _ = live
    post(base, "/api/staff/hire", {"name": "rex", "role": "scribe",
                                   "provider": "builtin", "model": "claude-opus-5"})
    rex = next(a for a in dash.hive().roster() if a["name"] == "rex")
    assert rex["model"] == "", "a builtin agent must not claim to run a model"


@pytest.mark.parametrize("payload,expect", [
    ({"name": "Bad Name", "role": "planner"}, "lowercase"),
    ({"name": "9lives", "role": "planner"}, "lowercase"),
    ({"name": "ok", "role": "wizard"}, "unknown role"),
    ({"name": "ok", "role": "planner", "provider": "gpt9"}, "unknown provider"),
    ({"name": "ok", "role": "planner", "autonomy": "godmode"}, "unknown autonomy"),
])
def test_hire_validates_its_input(live, payload, expect):
    _, base, _ = live
    status, body = post(base, "/api/staff/hire", payload)
    assert status == 400 and expect in body["error"]


def test_hire_refuses_a_duplicate_name(live):
    _, base, _ = live
    post(base, "/api/staff/hire", {"name": "dup", "role": "planner"})
    status, body = post(base, "/api/staff/hire", {"name": "dup", "role": "scribe"})
    assert status == 400 and "already on the roster" in body["error"]


def test_fire_archives_rather_than_deletes(live):
    dash, base, _ = live
    post(base, "/api/staff/hire", {"name": "temp", "role": "scribe"})
    status, body = post(base, "/api/staff/fire", {"name": "temp"})
    assert status == 200 and body["ok"]
    assert not any(a["name"] == "temp" for a in dash.hive().roster())
    archived = list((dash.hive_root / "former").glob("temp-*"))
    assert archived, "a dismissed agent's memory must be kept"
    assert (archived[0] / "identity.json").is_file()


def test_fire_unknown_agent_is_refused(live):
    _, base, _ = live
    status, body = post(base, "/api/staff/fire", {"name": "ghost"})
    assert status == 400 and "no agent named" in body["error"]


def test_attach_does_not_overwrite_how_an_agent_was_hired(live):
    """A mission joining an agent must not downgrade its stored provider."""
    dash, base, _ = live
    post(base, "/api/staff/hire", {"name": "kit", "role": "analyst",
                                   "provider": "claude-api", "model": "claude-opus-5"})
    hive = dash.hive()
    hive.attach("kit", "analyst", provider="builtin", autonomy="bounded")
    kit = next(a for a in hive.roster() if a["name"] == "kit")
    assert kit["provider"] == "claude-api" and kit["model"] == "claude-opus-5"


def test_attach_creates_the_agent_when_it_is_new(live):
    dash, _, _ = live
    hive = dash.hive()
    hive.attach("fresh", "scribe", provider="builtin")
    assert any(a["name"] == "fresh" for a in hive.roster())


# -- the conversation view -------------------------------------------------
def test_conversation_reads_back_what_was_said(live):
    _, base, _ = live
    convo = get(base, "/api/conversation")
    assert convo, "the fixture sent one message; it should be in the transcript"
    first = convo[0]
    assert (first["sender"], first["recipient"]) == ("pi", "planner")
    assert first["sender_role"] == "pi"
    # A plan request says what it is asking for, not `{"goal": "..."}`.
    assert first["say"] == "call variants"


def test_conversation_deduplicates_a_delivered_message(live):
    dash, base, _ = live
    dash.hive().send("planner", "pi", "peer.reply", {"answer": "fastq → vcf via bwa_mem"})
    convo = get(base, "/api/conversation")
    ids = [c["id"] for c in convo]
    assert len(ids) == len(set(ids))
    assert convo[-1]["say"] == "fastq → vcf via bwa_mem"


def test_floor_carries_the_last_utterances_for_the_bubbles(live):
    dash, base, _ = live
    dash.hive().send("pi", "planner", "peer.ask", {"question": "how long will this take?"})
    chatter = get(base, "/api/floor")["chatter"]
    assert chatter[-1]["say"] == "how long will this take?"
    # The bubble needs a line, not a whole message body.
    assert "body" not in chatter[-1]


def test_a_human_may_ask_an_agent_a_question(live):
    dash, base, _ = live
    status, r = post(base, "/api/message",
                     {"to": "planner", "topic": "peer.ask",
                      "body": {"question": "why bwa and not minimap2?"}})
    assert (status, r["ok"]) == (200, True)
    assert [m.topic for m in dash.hive().mailbox("planner").peek()] \
        == ["plan.request", "peer.ask"]


def test_a_tasking_message_says_what_the_agent_was_told(live):
    dash, base, _ = live
    dash.hive().send("pi", "planner", "report",
                     {"tasks": [{"capability": "bwa_mem"}],
                      "instruction": "Draft Methods for the planned pipeline."})
    assert get(base, "/api/conversation")[-1]["say"] == \
        "Draft Methods for the planned pipeline."


def test_static_rejects_sibling_with_matching_directory_prefix(live, tmp_path, monkeypatch):
    """A sibling named ui-private must not pass the UI directory boundary."""
    import bioharness.server as server_module

    _, base, _ = live
    ui = tmp_path / 'ui'
    ui.mkdir()
    sibling = tmp_path / 'ui-private'
    sibling.mkdir()
    (sibling / 'secret.txt').write_text('private')
    monkeypatch.setattr(server_module, 'UI_DIR', ui)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + '/../ui-private/secret.txt')
    assert exc.value.code == 404


def test_malformed_post_payloads_return_json_errors(live):
    _, base, _ = live
    for route, payload in [('/api/message', []), ('/api/message', {'to': 'pi', 'body': 'bad'}),
                           ('/api/staff/hire', {'name': 123}),
                           ('/api/staff/assign', {'names': 'pi'}),
                           ('/api/staff/assign', {'scope': [None]}),
                           ('/api/staff/fire', None)]:
        req = urllib.request.Request(base + route, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 400
        assert json.loads(exc.value.read())['ok'] is False
    assert get(base, '/api/floor')['agents']  # the server remains responsive


def test_negative_and_oversized_content_length_are_rejected(live):
    import http.client
    from urllib.parse import urlparse
    _, base, _ = live
    for length, status in [(-1, 400), (1024 * 1024 + 1, 413)]:
        connection = http.client.HTTPConnection(urlparse(base).netloc, timeout=3)
        connection.request('POST', '/api/message', body=b'', headers={'Content-Length': str(length)})
        response = connection.getresponse()
        assert response.status == status
        assert json.loads(response.read())['ok'] is False
        connection.close()


def test_post_backend_exception_is_returned_as_json(live, monkeypatch):
    dash, base, _ = live
    def fail(*args, **kwargs):
        raise OSError('temporary storage failure')
    monkeypatch.setattr(dash, 'fire', fail)
    request = urllib.request.Request(base + '/api/staff/fire', data=b'{"name":"pi"}',
                                     headers={'Content-Type': 'application/json'})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 500
    assert 'temporary storage failure' in json.loads(exc.value.read())['error']
