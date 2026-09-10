"""Command-line entrypoint: `bioharness <command>`."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .agents.api_provider import (APIProvider, MODELS, credentials_present,
                                  make_provider)
from .agents.base import AgentContext, CLIProvider
from .agents.roles import ROLES, plan_by_types
from .core.guardrails import Budget, Guardrails, GuardrailTripped
from .core.hive import Hive
from .core.loader import find_workflow, load_workflow
from .core.orchestrator import Mission, Orchestrator
from .core.registry import Registry
from .core.schemas import dumps
from .core.workspace import Workspace

DEFAULT_CAPS = os.environ.get("BIOHARNESS_CAPABILITIES", "capabilities")
DEFAULT_RUNS = os.environ.get("BIOHARNESS_RUNS", "runs")
DEFAULT_HIVE = os.environ.get("BIOHARNESS_HIVE", ".bioharness/hive")

C = {"dim": "\033[2m", "bold": "\033[1m", "green": "\033[32m", "red": "\033[31m",
     "yellow": "\033[33m", "cyan": "\033[36m", "off": "\033[0m"}


def color(text: str, name: str) -> str:
    return text if not sys.stdout.isatty() else f"{C[name]}{text}{C['off']}"


def load_registry(paths: list[str]) -> Registry:
    reg = Registry()
    for p in paths:
        if Path(p).exists():
            reg.load_path(Path(p))
    return reg


def parse_kv(pairs: list[str]) -> dict[str, Any]:
    """`--set key=value`, with JSON values parsed when they look like JSON."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        try:
            out[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            out[key.strip()] = value
    return out


# -- commands --------------------------------------------------------------
def cmd_caps(args: argparse.Namespace) -> int:
    reg = load_registry(args.capabilities)
    caps = reg.search(args.query) if args.query else reg.all()
    if args.domain:
        caps = [c for c in caps if c.domain == args.domain]
    if args.json:
        print(dumps([{"id": c.id, "domain": c.domain, "version": c.version,
                      "summary": c.summary, "runtime": c.runtime.value,
                      "inputs": [f"{p.name}:{p.type}" for p in c.inputs],
                      "outputs": [f"{p.name}:{p.type}" for p in c.outputs],
                      "available": reg.probe(c.id)[0]} for c in caps]))
        return 0
    if not caps:
        print("No capabilities found. Check --capabilities path.")
        return 1
    domain = None
    for cap in caps:
        if cap.domain != domain:
            domain = cap.domain
            print(f"\n{color(domain.upper(), 'bold')}")
        ok, reason = reg.probe(cap.id)
        mark = color("●", "green") if ok else color("○", "dim")
        ports = (f"{', '.join(p.type for p in cap.inputs) or '-'} → "
                 f"{', '.join(p.type for p in cap.outputs) or '-'}")
        print(f"  {mark} {cap.id:<22} {color(ports, 'cyan'):<40} {cap.summary[:60]}")
        if args.verbose and not ok:
            print(f"      {color(reason, 'dim')}")
    print(f"\n{len(caps)} capabilities, "
          f"{sum(1 for c in caps if reg.probe(c.id)[0])} available here.")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    reg = load_registry(args.capabilities)
    chain = plan_by_types(reg, args.from_type, args.to_type, args.domain)
    if not chain:
        print(color(f"No route from {args.from_type} to {args.to_type}.", "red"))
        near = reg.producing(args.to_type)
        if near:
            print("Capabilities that produce "
                  f"{args.to_type}: {', '.join(c.id for c in near)}")
        return 1
    print(color(f"{args.from_type} → {args.to_type}", "bold"))
    for i, cap_id in enumerate(chain, 1):
        cap = reg.get(cap_id)
        ok = reg.probe(cap_id)[0]
        mark = color("●", "green") if ok else color("○", "yellow")
        print(f"  {i}. {mark} {cap.id} — {cap.summary}")
    if args.emit:
        # Reuse the planner's wiring so `plan --emit` and the planner agent
        # cannot drift apart.
        from .agents.base import AgentContext
        from .agents.roles import Planner
        from .core.guardrails import Guardrails
        from .core.hive import Hive
        ctx = AgentContext(hive=Hive(args.hive), registry=reg,
                           guardrails=Guardrails(), workdir=Path(args.hive).parent / "agents")
        planner = Planner("planner", ctx)
        wf = planner._linear_workflow(f"{args.from_type}-to-{args.to_type}", chain)
        Path(args.emit).write_text(dumps(wf))
        print(f"\nWrote workflow to {args.emit}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    reg = load_registry(args.capabilities)
    wf = load_workflow(find_workflow(args.workflow, [Path(p) for p in args.workflows]))
    rails = Guardrails(
        budget=Budget(max_steps=args.max_steps, max_usd=args.max_usd,
                      max_wallclock_s=args.max_seconds),
        dry_run=args.dry_run)
    ws = Workspace(args.runs)
    hive = Hive(args.hive) if args.hive else None
    orch = Orchestrator(reg, ws, rails, hive)

    print(color(f"▶ {wf.name}", "bold"), color(f"({len(wf.steps)} steps)", "dim"))
    if args.dry_run:
        print(color("  dry-run: commands are resolved but not executed", "yellow"))

    summary = orch.run(wf, parse_kv(args.set), stop_on_failure=not args.keep_going)

    print()
    for t in summary["tasks"]:
        tint = {"ok": "green", "failed": "red", "blocked": "yellow",
                "skipped": "dim"}.get(t["status"], "dim")
        label = color(f"{t['status'].upper():<8}", tint)
        print(f"  {label}{t['step']:<24} {color(t['capability'], 'cyan')} {t['seconds']}s")
        if t["error"]:
            print(f"           {color(t['error'][:200], 'red')}")
        if args.dry_run and t["command"]:
            print(f"           {color('$ ' + t['command'][:300], 'dim')}")

    status_color = "green" if summary["status"] == "ok" else "red"
    print(f"\n{color(summary['status'].upper(), status_color)} "
          f"run {summary['run_id']} in {summary['seconds']}s → {summary['dir']}")
    if args.json:
        print(dumps(summary))
    return 0 if summary["status"] == "ok" else 1


def cmd_mission(args: argparse.Namespace) -> int:
    reg = load_registry(args.capabilities)
    hive = Hive(args.hive)
    rails = Guardrails(budget=Budget(max_usd=args.max_usd), dry_run=args.dry_run)
    ctx = AgentContext(hive=hive, registry=reg, guardrails=rails,
                       workdir=Path(args.hive).parent / "agents",
                       runs=Path(args.runs))
    provider = make_provider(args.provider, args.model) if args.provider else None
    if provider and not provider.available():
        why = (provider.unavailable_reason() if hasattr(provider, "unavailable_reason")
               else "not on PATH")
        print(color(f"provider {args.provider!r} unavailable ({why}); "
                    f"agents will use deterministic fallbacks", "yellow"))
        provider = None

    # Without --roles, staff the mission from whoever is already hired, using
    # each agent's own provider rather than one provider for everybody.
    if args.roles:
        agents = [ROLES[name](name, ctx, provider=provider) for name in args.roles]
        names = list(args.roles)
    else:
        roster = [a for a in hive.roster() if a.get("role") in ROLES]
        if not roster:
            print(color("nobody hired. Use --roles, or: "
                        "bioharness staff hire ada --role planner", "red"))
            return 1
        agents, names = [], []
        for ident in roster:
            own = provider or provider_for(ident)
            agents.append(ROLES[ident["role"]](ident["name"], ctx, provider=own))
            names.append(ident["name"])
        print(color(f"staffed from the hive: {', '.join(names)}", "dim"))

    # Send the kickoff to someone who actually subscribes to it. A PI is the
    # natural recipient; a planner can start a mission on its own. Silently
    # mailing "goal" to an agent that does not listen for it looks exactly like
    # a mission that ran and found nothing.
    body = dict(goal=args.goal, start_type=args.from_type,
                goal_type=args.to_type, domain=args.domain)
    pi = next((a for a in agents if a.role == "pi"), None)
    if pi is not None:
        first, topic = pi.name, "goal"
    else:
        planner = next((a for a in agents if a.role == "planner"), None)
        if planner is None:
            print(color("no agent can receive a mission: hire a pi (or a planner).\n"
                        "  bioharness staff hire chief --role pi", "red"))
            return 1
        first, topic = planner.name, "plan.request"
        print(color(f"no pi on the roster; starting at {first}", "yellow"))

    mission = Mission(hive, agents, rails, max_rounds=args.max_rounds)
    mission.kickoff(first, topic, **body)
    result = mission.run()
    print(dumps(result))
    return 0


def roster_agents(hive: Hive, ctx: AgentContext) -> list[Any]:
    """Instantiate whoever is hired, each with the provider it was hired with."""
    return [ROLES[a["role"]](a["name"], ctx, provider=provider_for(a))
            for a in hive.roster() if a.get("role") in ROLES]


def roster_key(hive: Hive) -> str:
    """Changes when someone is hired, fired or pointed at another backend."""
    return ",".join(sorted(
        f"{a['name']}:{a.get('role', '')}:{a.get('provider', '')}:{a.get('model', '')}"
        for a in hive.roster()))


def welcome(hive: Hive, agents: list[Any], newcomer: str, quiet: bool = False) -> None:
    """Everyone downs tools and says hello to the new arrival."""
    role = next((a.get("role", "") for a in hive.roster()
                 if a["name"] == newcomer), "")
    greeters = [a for a in agents if a.name != newcomer]
    for agent in greeters:
        try:
            agent.greet(newcomer, role)
        except Exception as exc:  # noqa: BLE001 - a bad hello is not a crash
            hive.event("attend.error", {"agent": agent.name, "error": repr(exc)})
    hive.event("social.welcome", {"newcomer": newcomer, "role": role,
                                  "by": [a.name for a in greeters]})
    if not quiet and greeters:
        print(color(f"  {newcomer} joined — {len(greeters)} agent(s) went over to say hi",
                    "green"))


def gossip(hive: Hive, agents: list[Any], rng: random.Random,
           quiet: bool = False) -> None:
    """One agent says something to another that has nothing to do with the work.

    Weighted by persona: the chatty ones start most of it, which is also true of
    every lab I have worked in.
    """
    if len(agents) < 2:
        return
    weights = [max(0.05, float(a.persona.get("chattiness", 0.4))) for a in agents]
    speaker = rng.choices(agents, weights=weights, k=1)[0]
    listener = rng.choice([a for a in agents if a.name != speaker.name])
    try:
        speaker.small_talk(listener.name)
    except Exception as exc:  # noqa: BLE001
        hive.event("attend.error", {"agent": speaker.name, "error": repr(exc)})
        return
    hive.event("social.gossip", {"from": speaker.name, "to": listener.name})
    if not quiet:
        print(color(f"  {speaker.name} wandered over to {listener.name}", "dim"))


def attend(hive_root: str, capabilities: list[str], max_usd: float = 5.0,
           interval: float = 2.0, stop: threading.Event | None = None,
           quiet: bool = False, social_every: float = 180.0,
           seed: int | None = None, runs: str | None = None,
           dry_run: bool = False) -> None:
    """Keep the hired roster running: route mail, tick everyone, repeat.

    Posting a message from the dashboard only drops it into a mailbox --
    something has to turn the crank, or that message sits in an inbox forever
    and every agent shows `awaiting`. This is that crank. It builds its own Hive
    because the memory palace holds a SQLite connection that belongs to the
    thread that opened it.
    """
    hive = Hive(hive_root)
    ctx = AgentContext(hive=hive, registry=load_registry(capabilities),
                       guardrails=Guardrails(budget=Budget(max_usd=max_usd),
                                             dry_run=dry_run),
                       workdir=Path(hive_root).parent / "agents",
                       runs=Path(runs) if runs else None)
    agents: list[Any] = []
    key = None
    rng = random.Random(seed)
    # Nobody greets the people who were already in the room when the lights came
    # on -- only someone who arrives while the lab is running.
    known = {a["name"] for a in hive.roster()}
    last_social = time.time()
    while not (stop and stop.is_set()):
        current = roster_key(hive)
        if current != key:
            agents = roster_agents(hive, ctx)
            key = current
            if not quiet:
                print(color(f"  attending {len(agents)} agent(s): "
                            f"{', '.join(a.name for a in agents) or 'nobody'}", "dim"))
        for newcomer in sorted({a.name for a in agents} - known):
            known.add(newcomer)
            welcome(hive, agents, newcomer, quiet=quiet)
        hive.route()
        handled = 0
        for agent in agents:
            try:
                handled += agent.tick()
            except GuardrailTripped as exc:
                # A tripped budget stops the loop, not the process: the operator
                # should be told once rather than watch it spin.
                hive.event("attend.halted", {"reason": str(exc)})
                print(color(f"  budget guardrail tripped: {exc}", "yellow"))
                return
            except Exception as exc:  # noqa: BLE001 - one bad agent must not stop the room
                hive.event("attend.error", {"agent": agent.name, "error": repr(exc)})
                print(color(f"  {agent.name}: {exc}", "red"))
        hive.route()
        if handled and not quiet:
            print(color(f"  {time.strftime('%H:%M:%S')}  handled {handled} message(s)",
                        "dim"))
        # Only when the room is otherwise quiet: gossip never delays the work,
        # and a busy lab has no time for it anyway.
        if social_every and not handled and time.time() - last_social >= social_every:
            gossip(hive, agents, rng, quiet=quiet)
            last_social = time.time()
        time.sleep(interval)


def cmd_attend(args: argparse.Namespace) -> int:
    hive = Hive(args.hive)
    if not hive.roster():
        print(color("nobody hired. Try: bioharness staff hire ada --role planner", "red"))
        return 1
    print(color("Bioinfo Harness attendant", "bold"))
    print(f"  hive {hive.root}")
    print("  routing mail and ticking the roster; ctrl-c to stop")
    try:
        attend(args.hive, args.capabilities, max_usd=args.max_usd,
               interval=args.interval, runs=args.runs, dry_run=args.dry_run,
               social_every=0 if args.no_social else args.social_every)
    except KeyboardInterrupt:
        print("\n  stopped")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .server import Dashboard, serve
    dash = Dashboard(runs=Path(args.runs), hive_root=Path(args.hive),
                     capabilities=[Path(p) for p in args.capabilities])
    print(color("Bioinfo Harness dashboard", "bold"))
    print(f"  runs {dash.runs}")
    print(f"  hive {dash.hive_root}")
    if args.attend:
        # The server itself stays read-only: this is a separate loop that
        # happens to share the process, exactly as `bioharness attend` would in
        # another terminal.
        threading.Thread(target=attend, daemon=True,
                         args=(args.hive, args.capabilities),
                         kwargs={"max_usd": args.max_usd, "quiet": True,
                                 "runs": args.runs, "dry_run": args.dry_run,
                                 "social_every": (0 if args.no_social
                                                  else args.social_every)}).start()
        print(color("  attending: messages sent from the dashboard will be "
                    "picked up and answered", "green"))
    else:
        print(color("  read-only: add --attend (or run `bioharness attend`) for "
                    "agents to act on what you send them", "dim"))
    serve(dash, host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def provider_for(identity: dict[str, Any]) -> Any:
    """Build the provider an enrolled agent was hired with."""
    kind = identity.get("provider", "builtin")
    if kind in ("builtin", "", None):
        return None
    try:
        p = make_provider(kind, identity.get("model") or None)
    except Exception as exc:  # noqa: BLE001 - a bad record must not kill the run
        print(color(f"  {identity['name']}: {exc}", "yellow"))
        return None
    if not p.available():
        why = p.unavailable_reason() if hasattr(p, "unavailable_reason") else "unavailable"
        print(color(f"  {identity['name']}: {kind} unavailable ({why}); "
                    f"falling back to deterministic", "yellow"))
        return None
    return p


def cmd_staff(args: argparse.Namespace) -> int:
    from .server import Dashboard
    dash = Dashboard(runs=Path(args.runs), hive_root=Path(args.hive),
                     capabilities=[Path(p) for p in args.capabilities])

    if args.staff_cmd == "list":
        roster = dash.hive().roster()
        if not roster:
            print("nobody hired yet. Try: bioharness staff hire ada --role planner")
            return 0
        from .agents.persona import brief
        print(color(f"{'NAME':<14}{'ROLE':<12}{'PROVIDER':<14}"
                    f"{'MODEL':<26}AUTONOMY", "bold"))
        for a in roster:
            print(f"{a['name']:<14}{a.get('role',''):<12}{a.get('provider',''):<14}"
                  f"{(a.get('model') or '-') + '  ':<26}{a.get('autonomy','-')}")
            who = brief(a.get("persona") or {})
            if who:
                print(color(f"{'':<14}{who}", "dim"))
            if a.get("autonomy") == "bounded" and a.get("role") in ROLES:
                scope = a.get("scope") or []
                print(color(f"{'':<14}may run: "
                            + (", ".join(scope) if scope else "nothing (no scope declared)"),
                            "dim"))
        return 0

    if args.staff_cmd == "assign":
        roster = dash.hive().roster()
        names = args.names or [a["name"] for a in roster
                               if a.get("role") != "operator"]
        result = dash.assign(names, args.provider, args.model or "",
                             args.autonomy or "", scope=args.scope)
        if not result.get("ok"):
            print(color(f"error: {result['error']}", "red"))
            return 1
        target = args.provider + (f"/{args.model}" if args.model else "")
        print(color(f"pointed {len(result['changed'])} agent(s) at {target}: "
                    f"{', '.join(result['changed'])}", "green"))
        if result.get("missing"):
            print(color(f"not on the roster: {', '.join(result['missing'])}", "yellow"))
        return 0

    if args.staff_cmd == "hire":
        result = dash.hire(args.name, args.role, args.provider, args.model or "",
                           args.autonomy, scope=args.scope)
        if not result.get("ok"):
            print(color(f"error: {result['error']}", "red"))
            return 1
        print(color(f"hired {args.name} as {args.role} "
                    f"({args.provider}{'/' + args.model if args.model else ''})", "green"))
        from .agents.persona import brief
        who = brief((result.get("agent") or {}).get("persona") or {})
        if who:
            print(color(f"  {who}", "dim"))
        return 0

    result = dash.fire(args.name)
    if not result.get("ok"):
        print(color(f"error: {result['error']}", "red"))
        return 1
    print(f"dismissed {args.name}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    hive = Hive(args.hive)
    print(color("HIVE", "bold"))
    print(dumps(hive.status()))
    runs = Workspace.list_runs(args.runs)
    print(f"\n{color('RUNS', 'bold')} ({len(runs)})")
    for r in runs[:args.limit]:
        print(f"  {r.get('run_id', '?'):<20} {r.get('workflow', '?'):<24} "
              f"{r.get('created_iso', '')}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    hive = Hive(args.hive)
    hits = hive.palace.search(args.query, limit=args.limit)
    if not hits:
        print("nothing remembered about that.")
        return 1
    for h in hits:
        print(f"{color(h['agent'], 'cyan')} [{h['kind']}] {h['text'][:400]}\n")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    reg = load_registry(args.capabilities)
    missing = []
    print(color("CAPABILITY AVAILABILITY", "bold"))
    for cap in reg.all():
        ok, reason = reg.probe(cap.id)
        mark = color("✓", "green") if ok else color("✗", "red")
        print(f"  {mark} {cap.id:<22} {color(reason, 'dim')}")
        if not ok:
            missing.append(cap.id)
    print(f"\n{color('AGENT PROVIDERS', 'bold')}")
    api = APIProvider()
    mark = color("✓", "green") if api.available() else color("✗", "red")
    detail = (credentials_present()[1] if api.available() else api.unavailable_reason())
    print(f"  {mark} claude-api          {color(detail, 'dim')}")
    print(f"    {color('models: ' + ', '.join(MODELS), 'dim')}")
    from .agents.gemini_provider import GeminiProvider, available_models
    gem = available_models()
    mark = color("✓", "green") if gem else color("✗", "red")
    detail = (f"{len(gem)} model(s), e.g. {', '.join(gem[:3])}" if gem
              else GeminiProvider().unavailable_reason())
    print(f"  {mark} gemini-api          {color(detail, 'dim')}")
    from .agents.antigravity_provider import (AntigravityProvider,
                                              available_models as agy_models)
    agy = agy_models()
    mark = color("✓", "green") if agy else color("✗", "red")
    detail = (f"{len(agy)} model(s), e.g. {', '.join(agy[:3])}" if agy
              else AntigravityProvider().unavailable_reason())
    print(f"  {mark} antigravity         {color(detail, 'dim')}")
    from .agents.local_provider import OllamaProvider, installed_models
    local = installed_models()
    mark = color("✓", "green") if local else color("✗", "red")
    detail = (f"{len(local)} model(s): {', '.join(local)}" if local
              else OllamaProvider().unavailable_reason())
    print(f"  {mark} ollama              {color(detail, 'dim')}")
    from .agents.base import PROVIDERS
    for name in sorted(PROVIDERS):
        p = CLIProvider(name)
        mark = color("✓", "green") if p.available() else color("✗", "dim")
        reason = "on PATH" if p.available() else "not on PATH"
        print(f"  {mark} {name:<19} {color(reason, 'dim')}")
    print(f"\n{len(reg) - len(missing)}/{len(reg)} capabilities usable here.")
    return 0


# -- parser ----------------------------------------------------------------
def add_social_args(c: argparse.ArgumentParser) -> None:
    """Off-topic talk costs tokens like anything else, so it is on a timer the
    operator can slow down or switch off."""
    c.add_argument("--social-every", type=float, default=180.0, metavar="SECONDS",
                   help="how often the room may start a conversation about "
                        "nothing, when it is otherwise idle (default 180)")
    c.add_argument("--no-social", action="store_true",
                   help="work only: no gossip, greetings still happen")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bioharness",
        description="A pluggable multi-agent harness for bioinformatics.")
    p.add_argument("--version", action="version", version=f"bioharness {__version__}")
    p.add_argument("--capabilities", "-c", action="append", default=None,
                   help=f"capability manifest dir or file (default: {DEFAULT_CAPS})")
    p.add_argument("--hive", default=DEFAULT_HIVE, help="hive root directory")
    p.add_argument("--runs", default=DEFAULT_RUNS, help="run workspace root")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("caps", help="list or search registered capabilities")
    c.add_argument("query", nargs="?", default="")
    c.add_argument("--domain", "-d")
    c.add_argument("--json", action="store_true")
    c.add_argument("--verbose", "-v", action="store_true")
    c.set_defaults(func=cmd_caps)

    c = sub.add_parser("plan", help="find a capability route between two data types")
    c.add_argument("from_type", metavar="FROM")
    c.add_argument("to_type", metavar="TO")
    c.add_argument("--domain", "-d")
    c.add_argument("--emit", help="write the resulting workflow to this JSON file")
    c.set_defaults(func=cmd_plan)

    c = sub.add_parser("run", help="execute a workflow")
    c.add_argument("workflow")
    c.add_argument("--set", "-s", action="append", default=[], metavar="KEY=VALUE")
    c.add_argument("--workflows", action="append", default=["workflows"])
    c.add_argument("--dry-run", action="store_true", help="resolve commands, execute nothing")
    c.add_argument("--keep-going", action="store_true", help="do not halt on first failure")
    c.add_argument("--max-steps", type=int, default=200)
    c.add_argument("--max-usd", type=float, default=25.0)
    c.add_argument("--max-seconds", type=float, default=24 * 3600)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_run)

    c = sub.add_parser("mission", help="run the agent roster against a goal")
    c.add_argument("goal")
    c.add_argument("--roles", nargs="+", default=None, choices=sorted(ROLES),
                   help="override the hive roster for this mission")
    c.add_argument("--from-type", dest="from_type")
    c.add_argument("--to-type", dest="to_type")
    c.add_argument("--domain", "-d")
    c.add_argument("--provider",
                   help="force one provider for every agent: claude-api, or a CLI "
                        "(claude, codex, gemini, ...)")
    c.add_argument("--model")
    c.add_argument("--max-usd", type=float, default=5.0)
    c.add_argument("--max-rounds", type=int, default=25)
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(func=cmd_mission)

    c = sub.add_parser("attend", help="keep the hired roster running: route mail "
                                     "and tick every agent until stopped")
    c.add_argument("--interval", type=float, default=2.0,
                   help="seconds between rounds (default 2)")
    c.add_argument("--max-usd", type=float, default=5.0)
    c.add_argument("--dry-run", action="store_true",
                   help="agents may plan and dispatch runs, but every step is "
                        "resolved and skipped rather than executed")
    add_social_args(c)
    c.set_defaults(func=cmd_attend)

    c = sub.add_parser("ui", help="serve the local dashboard")
    c.add_argument("--attend", action="store_true",
                   help="also run the roster, so messages sent from the browser "
                        "are actually acted on")
    c.add_argument("--max-usd", type=float, default=5.0,
                   help="budget ceiling for --attend")
    c.add_argument("--dry-run", action="store_true",
                   help="with --attend: resolve every step and execute nothing")
    add_social_args(c)
    c.add_argument("--port", type=int, default=8765)
    c.add_argument("--host", default="127.0.0.1",
                   help="loopback by default; the dashboard is read-only either way")
    c.add_argument("--no-open", action="store_true", help="do not open a browser")
    c.set_defaults(func=cmd_ui)

    c = sub.add_parser("staff", help="hire, list or dismiss agents")
    ssub = c.add_subparsers(dest="staff_cmd", required=True)
    sl = ssub.add_parser("list", help="show the roster")
    sl.set_defaults(func=cmd_staff)
    sh = ssub.add_parser("hire", help="add an agent to the hive")
    sh.add_argument("name")
    sh.add_argument("--role", required=True, choices=sorted(ROLES))
    sh.add_argument("--provider", default="builtin",
                    help="builtin | claude-api | gemini-api | antigravity | "
                         "ollama | a CLI name")
    sh.add_argument("--model", default="", help=f"for claude-api: {', '.join(MODELS)}")
    sh.add_argument("--autonomy", default="bounded",
                    choices=["read_only", "suggest", "bounded", "full"])
    sh.add_argument("--scope", nargs="*", default=None, metavar="CAP",
                    help="what a bounded agent may execute: capability ids, or "
                         "domain:<name>. Ignored for full and read_only.")
    sh.set_defaults(func=cmd_staff)
    sa = ssub.add_parser("assign",
                         help="point existing agents at a different provider, "
                              "keeping their memory")
    sa.add_argument("names", nargs="*", help="agent names; omit for everyone")
    sa.add_argument("--provider", required=True)
    sa.add_argument("--model", default="")
    sa.add_argument("--autonomy", default="",
                    choices=["", "read_only", "suggest", "bounded", "full"])
    sa.add_argument("--scope", nargs="*", default=None, metavar="CAP",
                    help="replace what a bounded agent may execute")
    sa.set_defaults(func=cmd_staff)

    sf = ssub.add_parser("fire", help="dismiss an agent (its memory is archived)")
    sf.add_argument("name")
    sf.set_defaults(func=cmd_staff)
    c.set_defaults(func=cmd_staff)

    c = sub.add_parser("status", help="show hive and run status")
    c.add_argument("--limit", type=int, default=10)
    c.set_defaults(func=cmd_status)

    c = sub.add_parser("recall", help="search the memory palace")
    c.add_argument("query")
    c.add_argument("--limit", type=int, default=5)
    c.set_defaults(func=cmd_recall)

    c = sub.add_parser("doctor", help="check what is actually installed here")
    c.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.capabilities is None:
        args.capabilities = [DEFAULT_CAPS]
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI reports errors, never tracebacks
        print(color(f"error: {exc}", "red"), file=sys.stderr)
        if os.environ.get("BIOHARNESS_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
