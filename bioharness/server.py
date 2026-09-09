"""Local read-only dashboard server.

Binds to loopback by default and never mutates anything: every endpoint reads
the same files the CLI writes. That means the UI can be pointed at a run
directory on a cluster (via `ssh -L`) with no risk of it disturbing the run.

Stdlib only -- a bioinformatics box should not need a JS toolchain to see
whether its pipeline is alive.
"""
from __future__ import annotations

import json
import mimetypes
import re
import socket
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .core.hive import Hive
from .core.registry import Registry
from .core.schemas import to_jsonable
from .core.workspace import Workspace

UI_DIR = Path(__file__).parent / "ui"
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class Dashboard:
    """Reads harness state off disk. One instance is shared by all requests."""

    def __init__(self, runs: Path, hive_root: Path, capabilities: list[Path]):
        self.runs = Path(runs).expanduser().resolve()
        self.hive_root = Path(hive_root).expanduser().resolve()
        self.capability_paths = [Path(p).expanduser() for p in capabilities]

    # -- registry ----------------------------------------------------------
    def registry(self) -> Registry:
        reg = Registry()
        for p in self.capability_paths:
            if p.exists():
                reg.load_path(p)
        return reg

    def caps(self) -> list[dict[str, Any]]:
        reg = self.registry()
        out = []
        for cap in reg.all():
            ok, reason = reg.probe(cap.id)
            out.append({
                "id": cap.id, "domain": cap.domain, "version": cap.version,
                "summary": cap.summary, "runtime": cap.runtime.value,
                "available": ok, "reason": reason, "tags": cap.tags,
                "citation": cap.citation,
                "inputs": [{"name": p.name, "type": p.type, "optional": p.optional,
                            "multiple": p.multiple} for p in cap.inputs],
                "outputs": [{"name": p.name, "type": p.type, "optional": p.optional,
                             "multiple": p.multiple} for p in cap.outputs],
                "resources": to_jsonable(cap.resources),
            })
        return out

    # -- hive --------------------------------------------------------------
    def hive(self) -> Hive:
        return Hive(self.hive_root)

    def hive_state(self) -> dict[str, Any]:
        hive = self.hive()
        roster = hive.roster()
        for agent in roster:
            box = hive.mailbox(agent["name"])
            agent["inbox"] = [_msg(m) for m in box.peek()]
            memory = box.dir / "memory.md"
            agent["memory"] = (memory.read_text(errors="replace")[-4000:]
                               if memory.exists() else "")
        return {
            "root": str(self.hive_root),
            "exists": self.hive_root.exists(),
            "agents": roster,
            "blackboard": [{"key": k, **(hive.blackboard.entry(k) or {})}
                           for k in hive.blackboard.keys()],
            "events": hive.events(limit=250)[::-1],
            "cost": hive.cost_ledger(),
            "palace": hive.palace.stats(),
        }

    # Status is derived, never stored: an agent is "working" while it holds
    # mail or has acted recently, "blocked" if its last event said so.
    WORKING_WINDOW_S = 25.0

    def floor_state(self) -> dict[str, Any]:
        import time as _time
        hive = self.hive()
        events = hive.events(limit=400)
        now = _time.time()

        last_event: dict[str, dict[str, Any]] = {}
        for e in events:
            who = (e.get("payload") or {}).get("agent")
            if who:
                last_event[who] = e

        agents = []
        for ident in hive.roster():
            name = ident["name"]
            ev = last_event.get(name)
            pending = ident.get("inbox_pending", 0)
            recent = bool(ev and now - ev["ts"] < self.WORKING_WINDOW_S)
            if ev and ev["event"] in ("agent.blocked", "agent.error"):
                status = "blocked"
            elif pending:
                status = "awaiting"
            elif recent:
                status = "working"
            else:
                status = "idle"
            agents.append({
                **ident, "status": status,
                "last_event": ev["event"] if ev else None,
                "last_seen": ev["ts"] if ev else ident.get("enrolled"),
                "note": (ev.get("payload", {}) or {}).get("reason", "") if ev else "",
            })

        traffic = [{"from": e["payload"].get("from"), "to": e["payload"].get("to"),
                    "topic": e["payload"].get("topic"), "ts": e["ts"]}
                   for e in events if e["event"] == "message.routed"][-25:]

        runs = self.run_list()
        current = runs[0] if runs else None
        return {
            "agents": agents,
            "traffic": traffic,
            "activity": events[-80:][::-1],
            "current_run": current,
            "cost": hive.cost_ledger(),
            "blackboard": [{"key": k, **(hive.blackboard.entry(k) or {})}
                           for k in hive.blackboard.keys()],
        }

    ROLES_AVAILABLE = ("pi", "planner", "curator", "validator", "analyst", "scribe")

    def staff_options(self) -> dict[str, Any]:
        """What the hire form may offer, and whether each backend works here."""
        from .agents.api_provider import MODELS, APIProvider
        from .agents.base import PROVIDERS, CLIProvider
        from .agents.gemini_provider import GeminiProvider, available_models
        from .agents.local_provider import OllamaProvider, installed_models
        api = APIProvider()
        local_models = installed_models()
        ollama = OllamaProvider()
        gem = GeminiProvider()
        gem_models = available_models()
        providers = [{"id": "builtin", "label": "builtin (deterministic, free)",
                      "available": True, "reason": "no LLM calls", "models": []},
                     {"id": "claude-api", "label": "Claude API",
                      "available": api.available(),
                      "reason": "ready" if api.available() else api.unavailable_reason(),
                      "models": MODELS},
                     {"id": "gemini-api", "label": "Gemini API",
                      "available": bool(gem_models),
                      "reason": (f"{len(gem_models)} model(s)" if gem_models
                                 else gem.unavailable_reason()),
                      "models": gem_models},
                     {"id": "ollama", "label": "Ollama (local, free)",
                      "available": bool(local_models),
                      "reason": (f"{len(local_models)} model(s) local"
                                 if local_models else ollama.unavailable_reason()),
                      "models": local_models}]
        for name in sorted(PROVIDERS):
            cli = CLIProvider(name)
            providers.append({"id": name, "label": f"{name} CLI",
                              "available": cli.available(),
                              "reason": "on PATH" if cli.available() else "not on PATH",
                              "models": []})
        return {"roles": list(self.ROLES_AVAILABLE), "providers": providers,
                "autonomy": ["read_only", "suggest", "bounded", "full"]}

    NAME_OK = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")

    def hire(self, name: str, role: str, provider: str = "builtin",
             model: str = "", autonomy: str = "bounded") -> dict[str, Any]:
        from .agents.api_provider import MODELS
        from .agents.base import PROVIDERS
        from .agents.gemini_provider import available_models
        from .agents.local_provider import installed_models
        name = (name or "").strip().lower()
        if not self.NAME_OK.match(name):
            return {"ok": False, "error": "name must be lowercase letters, digits, "
                                          "- or _, starting with a letter"}
        if role not in self.ROLES_AVAILABLE:
            return {"ok": False, "error": f"unknown role {role!r}; "
                                          f"pick one of {list(self.ROLES_AVAILABLE)}"}
        if provider not in ("builtin", "claude-api", "gemini-api", "ollama", *PROVIDERS):
            return {"ok": False, "error": f"unknown provider {provider!r}"}
        if provider == "claude-api" and model and model not in MODELS:
            return {"ok": False, "error": f"unknown model {model!r}"}
        if provider == "gemini-api":
            usable = available_models()
            if model and usable and model not in usable:
                return {"ok": False,
                        "error": f"model {model!r} is not available to this key"}
        if provider == "ollama":
            local = installed_models()
            if model and model not in local:
                return {"ok": False,
                        "error": f"model {model!r} is not pulled; here: {local}"}
            model = model or (local[0] if local else "")
        if autonomy not in ("read_only", "suggest", "bounded", "full"):
            return {"ok": False, "error": f"unknown autonomy {autonomy!r}"}

        hive = self.hive()
        if any(a["name"] == name for a in hive.roster()):
            return {"ok": False, "error": f"{name!r} is already on the roster"}

        # A model only means something for the API provider; storing one for a
        # builtin or CLI agent just makes the roster lie about what it runs.
        model = model if provider in ("claude-api", "gemini-api", "ollama") else ""
        identity = hive.enroll(name, role, provider=provider,
                               model=model, autonomy=autonomy)
        hive.event("agent.hired", {"name": name, "role": role,
                                   "provider": provider, "model": model})
        return {"ok": True, "agent": identity}

    def fire(self, name: str) -> dict[str, Any]:
        if not self.NAME_OK.match(name or ""):
            return {"ok": False, "error": "bad agent name"}
        if self.hive().dismiss(name):
            return {"ok": True, "dismissed": name}
        return {"ok": False, "error": f"no agent named {name!r}"}

    # The one write path in the whole server: a human dropping a message into
    # an agent's mailbox. Recipient must already be enrolled, topic must be
    # one the roster actually handles.
    ALLOWED_TOPICS = ("goal", "mission", "plan.request", "data.validate",
                      "qc.check", "analyze", "report")

    def send_message(self, to: str, topic: str, body: dict[str, Any]) -> dict[str, Any]:
        hive = self.hive()
        names = {a["name"] for a in hive.roster()}
        if to not in names:
            return {"ok": False, "error": f"no agent named {to!r}; enrolled: {sorted(names)}"}
        if topic not in self.ALLOWED_TOPICS:
            return {"ok": False,
                    "error": f"topic {topic!r} not allowed; use one of {list(self.ALLOWED_TOPICS)}"}
        if "human" not in names:
            hive.enroll("human", "operator")
        msg = hive.send("human", to, topic, body)
        hive.event("message.queued_by_human", {"to": to, "topic": topic, "id": msg.id})
        return {"ok": True, "id": msg.id, "to": to, "topic": topic}

    def recall(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.hive().palace.search(query, limit=limit) if query else []

    # -- runs --------------------------------------------------------------
    def run_list(self) -> list[dict[str, Any]]:
        out = []
        for manifest in Workspace.list_runs(self.runs):
            run_id = manifest.get("run_id")
            summary = self._json(self.runs / str(run_id) / "summary.json") or {}
            out.append({
                "run_id": run_id,
                "workflow": manifest.get("workflow", "?"),
                "created": manifest.get("created"),
                "created_iso": manifest.get("created_iso", ""),
                "dry_run": manifest.get("dry_run", False),
                "status": summary.get("status", "running"),
                "seconds": summary.get("seconds"),
                "steps": len(manifest.get("graph", []) or []),
                "by_status": summary.get("by_status", {}),
            })
        return out

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        if not RUN_ID.match(run_id):
            return None
        d = self.runs / run_id
        manifest = self._json(d / "run.json")
        if manifest is None:
            return None
        summary = self._json(d / "summary.json") or {}
        ledger = []
        ledger_file = d / "ledger.jsonl"
        if ledger_file.exists():
            for line in ledger_file.read_text(errors="replace").splitlines():
                if line.strip():
                    try:
                        ledger.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        # Task state comes from summary.json once finished, and from the live
        # ledger while the run is still going.
        tasks = {t["step"]: t for t in summary.get("tasks", [])}
        for entry in ledger:
            if not entry["event"].startswith("task."):
                continue
            payload = entry.get("payload", {})
            step = payload.get("step")
            if step and step not in tasks:
                tasks[step] = {"step": step, "capability": payload.get("capability", "?"),
                               "status": entry["event"].split(".", 1)[1],
                               "seconds": payload.get("duration_s", 0),
                               "error": payload.get("error", ""),
                               "command": " ".join(payload.get("argv", []) or []),
                               "params": {}}

        artifacts = [e["payload"] for e in ledger if e["event"] == "artifact.created"]
        return {
            "manifest": manifest,
            "summary": summary,
            "graph": manifest.get("graph", []),
            "tasks": list(tasks.values()),
            "artifacts": artifacts,
            "ledger": ledger[-400:],
            "dir": str(d),
        }

    def run_log(self, run_id: str, step: str, stream: str = "stderr",
                tail: int = 400) -> dict[str, Any]:
        if not RUN_ID.match(run_id) or not RUN_ID.match(step):
            return {"error": "bad identifier"}
        name = "stdout.log" if stream == "stdout" else "stderr.log"
        path = self.runs / run_id / "steps" / step / name
        if not path.is_file():
            return {"path": str(path), "exists": False, "text": ""}
        lines = path.read_text(errors="replace").splitlines()
        return {"path": str(path), "exists": True, "lines": len(lines),
                "text": "\n".join(lines[-tail:])}

    def overview(self) -> dict[str, Any]:
        runs = self.run_list()
        reg = self.registry()
        hive = self.hive()
        return {
            "runs": {"total": len(runs),
                     "ok": sum(1 for r in runs if r["status"] == "ok"),
                     "failed": sum(1 for r in runs if r["status"] == "failed"),
                     "running": sum(1 for r in runs if r["status"] == "running")},
            "capabilities": {"total": len(reg),
                             "available": len(reg.available()),
                             "domains": reg.domains()},
            "agents": len(hive.roster()),
            "cost": hive.cost_ledger(),
            "paths": {"runs": str(self.runs), "hive": str(self.hive_root)},
        }

    @staticmethod
    def _json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None


def _msg(m: Any) -> dict[str, Any]:
    return {"id": m.id, "sender": m.sender, "recipient": m.recipient,
            "topic": m.topic, "body": m.body, "ts": m.ts}


class Handler(BaseHTTPRequestHandler):
    server_version = "bioharness"

    def __init__(self, *args: Any, dashboard: Dashboard, **kw: Any):
        self.dash = dashboard
        super().__init__(*args, **kw)

    # Quiet by default; the CLI prints one line about where to point a browser.
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if route.startswith("/api/"):
                self._api(route, query)
            else:
                self._static(route)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - a dashboard must not take the run down
            self._send_json({"error": str(exc)}, status=500)

    def _api(self, route: str, query: dict[str, list[str]]) -> None:
        d = self.dash
        if route == "/api/overview":
            return self._send_json(d.overview())
        if route == "/api/runs":
            return self._send_json(d.run_list())
        if route == "/api/staff/options":
            return self._send_json(d.staff_options())
        if route == "/api/floor":
            return self._send_json(d.floor_state())
        if route == "/api/hive":
            return self._send_json(d.hive_state())
        if route == "/api/caps":
            return self._send_json(d.caps())
        if route == "/api/recall":
            return self._send_json(d.recall(query.get("q", [""])[0],
                                            int(query.get("limit", ["20"])[0])))
        if route.startswith("/api/runs/"):
            rest = route[len("/api/runs/"):]
            run_id, _, sub = rest.partition("/")
            if sub == "log":
                return self._send_json(d.run_log(run_id, query.get("step", [""])[0],
                                                 query.get("stream", ["stderr"])[0]))
            detail = d.run_detail(run_id)
            if detail is None:
                return self._send_json({"error": f"no such run: {run_id}"}, status=404)
            return self._send_json(detail)
        self._send_json({"error": f"no such endpoint: {route}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        route = parsed.path
        if route not in ("/api/message", "/api/staff/hire", "/api/staff/fire"):
            return self._send_json({"error": "no such endpoint"}, status=404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json({"ok": False, "error": f"bad JSON: {exc}"}, status=400)

        if route == "/api/message":
            result = self.dash.send_message(
                to=str(payload.get("to", "")), topic=str(payload.get("topic", "goal")),
                body=payload.get("body") or {})
        elif route == "/api/staff/hire":
            result = self.dash.hire(
                name=str(payload.get("name", "")), role=str(payload.get("role", "")),
                provider=str(payload.get("provider", "builtin")),
                model=str(payload.get("model", "")),
                autonomy=str(payload.get("autonomy", "bounded")))
        else:
            result = self.dash.fire(str(payload.get("name", "")))
        return self._send_json(result, status=200 if result.get("ok") else 400)

    def _static(self, route: str) -> None:
        rel = "index.html" if route in ("/", "") else route.lstrip("/")
        target = (UI_DIR / rel).resolve()
        if not str(target).startswith(str(UI_DIR.resolve())) or not target.is_file():
            self.send_error(404, "not found")
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(to_jsonable(payload), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def find_port(host: str, preferred: int, tries: int = 20) -> int:
    for port in range(preferred, preferred + tries):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in {preferred}..{preferred + tries}")


def serve(dashboard: Dashboard, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = False, block: bool = True) -> ThreadingHTTPServer:
    port = find_port(host, port)
    handler = partial(Handler, dashboard=dashboard)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if not block:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd
    print(f"  {url}")
    print("  read-only; ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()
    return httpd
