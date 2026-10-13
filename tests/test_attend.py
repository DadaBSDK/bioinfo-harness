"""The attendant loop, which routes mail and ticks the roster so messages sent
from the dashboard are acted on.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bioharness.cli import attend, roster_key
from bioharness.core.hive import Hive


def run_until(hive_root: Path, caps: list[str], done, timeout: float = 8.0) -> bool:
    """Run the attendant in a thread until `done()` or the timeout."""
    stop = threading.Event()
    thread = threading.Thread(target=attend, daemon=True, args=(str(hive_root), caps),
                              kwargs={"interval": 0.05, "stop": stop, "quiet": True})
    thread.start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if done():
                return True
            time.sleep(0.05)
        return False
    finally:
        stop.set()
        thread.join(timeout=5)


def test_a_message_queued_by_a_human_gets_answered(tmp_path: Path):
    hive_root = tmp_path / "hive"
    hive = Hive(hive_root)
    hive.enroll("hugo", "pi", provider="builtin")
    hive.enroll("ada", "planner", provider="builtin")
    hive.enroll("human", "operator")
    hive.send("human", "hugo", "goal", {"goal": "call variants"})

    fresh = Hive(hive_root)
    assert run_until(hive_root, [], lambda: fresh.mailbox("ada").pending() == 0
                     and any(e["event"] == "message.routed"
                             and e["payload"].get("to") == "ada"
                             for e in fresh.events())), \
        "the PI never delegated: nothing turned the crank"
    assert hive.mailbox("hugo").pending() == 0


def test_the_attendant_picks_up_someone_hired_while_it_runs(tmp_path: Path):
    hive_root = tmp_path / "hive"
    hive = Hive(hive_root)
    hive.enroll("hugo", "pi", provider="builtin")
    before = roster_key(hive)
    hired = []

    def hire_once_then_wait():
        """Hire on the first poll, then wait for the curator to have worked."""
        if not hired:
            hive.enroll("iris", "curator", provider="builtin")
            hive.send("human", "iris", "data.validate",
                      {"inputs": {"reads": str(tmp_path / "nope.fastq")}})
            hired.append(True)
            return False
        return hive.blackboard.get("curation/last") is not None

    assert run_until(hive_root, [], hire_once_then_wait), \
        "an agent hired after the attendant started never ran"
    assert roster_key(hive) != before
    assert hive.blackboard.get("curation/last")["ok"] is False   # the path is missing


def test_one_broken_agent_does_not_stop_the_room(tmp_path: Path, monkeypatch):
    hive_root = tmp_path / "hive"
    hive = Hive(hive_root)
    hive.enroll("vera", "validator", provider="builtin")
    hive.enroll("iris", "curator", provider="builtin")

    from bioharness.agents import roles

    def explode(self, msg):
        raise RuntimeError("the QC bench is on fire")

    monkeypatch.setattr(roles.Validator, "handle", explode)
    hive.send("human", "vera", "qc.check", {"metrics": {"mean_quality": 9.0}})
    hive.send("human", "iris", "data.validate", {"inputs": {}})

    fresh = Hive(hive_root)
    assert run_until(hive_root, [], lambda: fresh.mailbox("iris").pending() == 0), \
        "the curator never ran, so a failing validator took the room down"


def test_a_mission_runs_with_the_hired_roster(tmp_path: Path, capsys):
    from bioharness.cli import main
    hive = Hive(tmp_path / "hive")
    hive.enroll("hugo", "pi", provider="builtin")
    hive.enroll("ada", "planner", provider="builtin")
    code = main(["--hive", str(tmp_path / "hive"), "--runs", str(tmp_path / "runs"),
                 "mission", "call variants", "--from-type", "fastq",
                 "--to-type", "vcf", "-d", "ngs", "--dry-run", "--max-rounds", "4"])
    assert code == 0
    assert "staffed from the hive: ada, hugo" in capsys.readouterr().out
