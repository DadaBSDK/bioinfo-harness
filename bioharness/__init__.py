"""Bioinfo Harness -- a pluggable multi-agent harness for bioinformatics.

Any tool becomes available to the agents by dropping a YAML manifest into the
capability registry: NGS aligners, variant callers, MD engines, docking
programs, single-cell notebooks. The harness core knows about ports, types,
runtimes and provenance -- never about a specific tool.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .core.guardrails import Autonomy, Budget, CircuitBreaker, Guardrails, Intervention
from .core.hive import Hive
from .core.loader import load_workflow, workflow_from_dict
from .core.orchestrator import Mission, Orchestrator
from .core.registry import Registry
from .core.schemas import Capability, Port, Status, Step, Task, Workflow
from .core.workspace import Workspace

__all__ = [
    "Autonomy", "Budget", "Capability", "CircuitBreaker", "Guardrails", "Hive",
    "Intervention", "Mission", "Orchestrator", "Port", "Registry", "Status",
    "Step", "Task", "Workflow", "Workspace", "load_workflow", "workflow_from_dict",
    "__version__",
]
