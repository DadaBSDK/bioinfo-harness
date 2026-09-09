"""Parse tool-specific metric files into one flat table for QC gating.

Each tool reports quality differently -- fastp writes JSON, samtools writes
prose, GROMACS writes a log. The validator only wants `{metric: number}`, so
normalising happens here rather than in six different places.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Patterns applied to plain-text metric files, newest tool first.
TEXT_PATTERNS: dict[str, re.Pattern[str]] = {
    "percent_mapped": re.compile(r"([\d.]+)%\s*:?\s*(?:N/A\s*)?\)?\s*mapped", re.I),
    "total_reads": re.compile(r"^(\d+)\s*\+\s*\d+\s+in total", re.I | re.M),
    "percent_duplicates": re.compile(r"([\d.]+)%.*duplicat", re.I),
    "coverage_mean": re.compile(r"mean\s+coverage[:\s]+([\d.]+)", re.I),
}


def _from_fastp(data: dict[str, Any]) -> dict[str, float]:
    after = data.get("summary", {}).get("after_filtering", {})
    out: dict[str, float] = {}
    if "q30_rate" in after:
        out["percent_q30"] = round(after["q30_rate"] * 100, 3)
    for src, dest in (("mean_length", "mean_read_length"),
                      ("total_reads", "total_reads"),
                      ("gc_content", "gc_content")):
        if src in after:
            out[dest] = after[src]
    if "duplication" in data:
        out["percent_duplicates"] = round(data["duplication"].get("rate", 0) * 100, 3)
    return out


def _flatten(data: Any, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            out.update(_flatten(v, f"{prefix}{k}." if not prefix else f"{prefix}{k}."))
    elif isinstance(data, (int, float)) and not isinstance(data, bool):
        out[prefix.rstrip(".")] = float(data)
    return out


def extract(inputs: dict[str, Any], params: dict[str, Any],
            workdir: Path) -> dict[str, str]:
    sources = inputs.get("source", [])
    if not isinstance(sources, list):
        sources = [sources]

    metrics: dict[str, float] = {}
    provenance: dict[str, str] = {}

    for src in sources:
        path = Path(str(src))
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        found: dict[str, float] = {}

        if path.suffix == ".json":
            try:
                data = json.loads(text)
                found = _from_fastp(data) if "summary" in data else _flatten(data)
            except json.JSONDecodeError:
                pass
        if not found:
            for name, pattern in TEXT_PATTERNS.items():
                m = pattern.search(text)
                if m:
                    found[name] = float(m.group(1))

        for k, v in found.items():
            metrics[k] = v
            provenance[k] = str(path)

    out = workdir / "metrics.json"
    out.write_text(json.dumps({"metrics": metrics, "sources": provenance},
                              indent=2, sort_keys=True))
    return {"metrics": str(out)}
