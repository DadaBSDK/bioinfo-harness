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


def _render_shell_script(template: str, context: dict[str, Any]) -> str:
    """Escape substitutions for their position in a shell script's quotes."""
    parts: list[str] = []
    quote = ""
    escaped = False
    previous = 0
    for match in _TOKEN.finditer(template):
        literal = template[previous:match.start()]
        parts.append(literal)
        for char in literal:
            if escaped:
                escaped = False
            elif char == "\\" and quote != "'":
                escaped = True
            elif char in ("'", '"'):
                if quote == char:
                    quote = ""
                elif not quote:
                    quote = char
        value = lookup(context, match.group(1))
        if quote == "'":
            replacement = stringify(value, False).replace("'", "'\\''")
        elif quote == '"':
            replacement = re.sub(r'([\\"$`])', r'\\\1', stringify(value, False))
        else:
            replacement = stringify(value, True)
        parts.append(replacement)
        previous = match.end()
    parts.append(template[previous:])
    return "".join(parts)


def render_argv(template: str, context: dict[str, Any]) -> list[str]:
    """Parse the command structure before injecting data into its arguments.

    This preserves paths containing spaces or quotes in both direct commands
    and sh/bash -c scripts. A list used as a whole argument expands to argv.
    """
    prefix = "__BIOHARNESS_TEMPLATE_"
    while prefix in template:
        prefix += "_"
    tokens: dict[str, str] = {}

    def mask(match: re.Match[str]) -> str:
        marker = f"{prefix}{len(tokens)}__"
        tokens[marker] = match.group(0)
        return marker

    words = shlex.split(_TOKEN.sub(mask, template))
    shell_script = None
    if words and words[0].rsplit("/", 1)[-1] in {"sh", "bash", "dash", "zsh", "ksh"}:
        for index, word in enumerate(words[1:], 1):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", word):
                shell_script = index + 1
                break
    argv: list[str] = []
    for index, word in enumerate(words):
        original = word
        for marker, token in tokens.items():
            word = word.replace(marker, token)
        if index == shell_script:
            argv.append(_render_shell_script(word, context))
        elif original in tokens:
            match = _TOKEN.fullmatch(tokens[original])
            value = lookup(context, match.group(1))
            values = value if isinstance(value, (list, tuple)) else [value]
            argv.extend(stringify(v, False) for v in values)
        else:
            argv.append(render(word, context))
    return argv
