"""Command-line entrypoint: `bioharness <command>`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .agents.base import AgentContext, CLIProvider
from .agents.roles import ROLES, plan_by_types
from .core.guardrails import Budget, Guardrails
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
                       workdir=Path(args.hive).parent / "agents")
    provider = CLIProvider(args.provider, args.model) if args.provider else None
    if provider and not provider.available():
        print(color(f"provider {args.provider!r} not on PATH; "
                    f"agents will use deterministic fallbacks", "yellow"))
        provider = None

    agents = [ROLES[name](name, ctx, provider=provider) for name in args.roles]
    mission = Mission(hive, agents, rails, max_rounds=args.max_rounds)
    mission.kickoff("pi" if "pi" in args.roles else args.roles[0], "goal",
                    goal=args.goal, start_type=args.from_type,
                    goal_type=args.to_type, domain=args.domain)
    result = mission.run()
    print(dumps(result))
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
    from .agents.base import PROVIDERS
    for name in sorted(PROVIDERS):
        p = CLIProvider(name)
        mark = color("✓", "green") if p.available() else color("✗", "dim")
        print(f"  {mark} {name}")
    print(f"\n{len(reg) - len(missing)}/{len(reg)} capabilities usable here.")
    return 0


# -- parser ----------------------------------------------------------------
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
    c.add_argument("--roles", nargs="+", default=["pi", "planner", "curator"],
                   choices=sorted(ROLES))
    c.add_argument("--from-type", dest="from_type")
    c.add_argument("--to-type", dest="to_type")
    c.add_argument("--domain", "-d")
    c.add_argument("--provider", help="agent CLI to wrap (claude, codex, gemini, ...)")
    c.add_argument("--model")
    c.add_argument("--max-usd", type=float, default=5.0)
    c.add_argument("--max-rounds", type=int, default=25)
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(func=cmd_mission)

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
