"""Message-bundle lookup for prompt fragments (Java messages.properties style).

Resolution order for a key:
  1. env / .env override   M365_PROMPT_<KEY>   (dots -> underscores, uppercased)
  2. catalog file          messages.properties (or M365_PROMPT_CATALOG=<path>)

Values support {name}/{begin}/{end} placeholders (Python str.format) and \\n \\t \\\\ \\uXXXX escapes.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path

_ENV_PREFIX = "M365_PROMPT_"
_UNICODE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})")


def _unescape(value: str) -> str:
    value = _UNICODE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "=": "=", ":": ":"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_properties(text: str) -> dict[str, str]:
    """Minimal but faithful .properties parse: `#`/`!` comments, `=`/`:` separators,
    trailing-backslash line continuation, and standard escapes."""
    props: dict[str, str] = {}
    raw_lines = text.splitlines()
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        i += 1
        if not line.lstrip() or line.lstrip()[0] in "#!":
            continue
        while line.endswith("\\") and not line.endswith("\\\\") and i < len(raw_lines):
            line = line[:-1] + raw_lines[i].lstrip()
            i += 1
        m = re.search(r"(?<!\\)[=:]", line)
        if not m:
            continue
        key = line[: m.start()].strip()
        val = line[m.end():].lstrip(" \t")
        if key:
            props[_unescape(key)] = _unescape(val)
    return props


@lru_cache(maxsize=1)
def _catalog() -> dict[str, str]:
    override = os.environ.get("M365_PROMPT_CATALOG")
    if override:
        text = Path(override).read_text(encoding="utf-8")
    else:
        text = resources.files(__package__).joinpath("messages.properties").read_text(encoding="utf-8")
    return _parse_properties(text)


def _env_key(key: str) -> str:
    return _ENV_PREFIX + key.replace(".", "_").upper()


def message(key: str, /, **params: str) -> str:
    """Resolve a prompt fragment by key (env override wins), interpolating any {placeholders}."""
    value = os.environ.get(_env_key(key))
    if value is None:
        value = _catalog().get(key, "")
    return value.format(**params) if params else value
