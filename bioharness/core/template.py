"""Tiny, dependency-free `{{ dotted.path }}` renderer.

Deliberately not Jinja: capability manifests are configuration, not programs.
Anything that needs real logic belongs in a PYTHON-runtime capability.
"""
from __future__ import annotations

import re
import shlex
from typing import Any

_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][\w.\-]*)\s*\}\}")


class TemplateError(KeyError):
    pass


def lookup(context: dict[str, Any], dotted: str) -> Any:
    cur: Any = context
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, (list, tuple)) and part.isdigit():
            cur = cur[int(part)]
        else:
            raise TemplateError(dotted)
    return cur


def stringify(value: Any, quote: bool) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(stringify(v, quote) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return shlex.quote(text) if quote and text else text


def render(template: str, context: dict[str, Any], quote: bool = False) -> str:
    """Render a template string. `quote=True` shell-escapes substituted values."""

    def sub(match: re.Match[str]) -> str:
        return stringify(lookup(context, match.group(1)), quote)

    return _TOKEN.sub(sub, template)


def render_value(value: Any, context: dict[str, Any]) -> Any:
    """Render nested config. A string that is exactly one token keeps its native
    type (so `{{ params.threads }}` stays an int, not "8")."""
    if isinstance(value, str):
        whole = _TOKEN.fullmatch(value.strip())
        if whole:
            return lookup(context, whole.group(1))
        return render(value, context)
    if isinstance(value, dict):
        return {k: render_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, context) for v in value]
    return value


def missing_tokens(template: str, context: dict[str, Any]) -> list[str]:
    out = []
    for match in _TOKEN.finditer(template):
        try:
            lookup(context, match.group(1))
        except TemplateError:
            out.append(match.group(1))
    return out
