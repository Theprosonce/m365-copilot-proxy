"""ReAct-style tool-calling shim for the text-only Copilot backend.

M365 Copilot returns plain text and has no native function calling. To let
agentic clients (OpenCode, etc.) work, we:
  1. Describe the client's tools in the prompt and ask the model to emit tool
     calls in a strict sentinel-delimited JSON block.
  2. Parse that block out of the model's text reply and turn it back into
     OpenAI `tool_calls`.

This is best-effort: the model may ignore the format. Parsing is deliberately
tolerant.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .messages import message
from .models import FunctionCall, ToolCall

_BEGIN = "<<<TOOL_CALLS>>>"
_END = "<<<END_TOOL_CALLS>>>"

_BLOCK_RE = re.compile(
    re.escape(_BEGIN) + r"\s*(.*?)\s*" + re.escape(_END),
    re.DOTALL,
)
# Fallback: a fenced ```tool_calls / ```json block holding a JSON array.
_FENCE_RE = re.compile(
    r"```(?:tool_calls|json)?\s*(\[.*?\])\s*```",
    re.DOTALL,
)


def _function_specs(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize OpenAI ({function:{...}}) and Anthropic ({name, input_schema}) tool shapes."""
    specs = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):  # OpenAI shape
            specs.append(
                {
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "parameters": fn.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                }
            )
        elif tool.get("name"):  # Anthropic shape
            specs.append(
                {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "input_schema", {"type": "object", "properties": {}}
                    ),
                }
            )
    return [s for s in specs if s.get("name")]


def _compact_signature(spec: dict[str, Any]) -> str:
    params = spec.get("parameters") or {}
    props = params.get("properties", {}) if isinstance(params, dict) else {}
    required = set(params.get("required", []) if isinstance(params, dict) else [])
    parts = []
    for pname, pdef in props.items():
        ptype = pdef.get("type", "any") if isinstance(pdef, dict) else "any"
        mark = "" if pname in required else "?"
        parts.append(f"{pname}{mark}:{ptype}")
    sig = f"{spec['name']}({', '.join(parts)})"
    desc = (spec.get("description") or "").strip().splitlines()
    first = desc[0].strip() if desc else ""
    if len(first) > 160:
        first = first[:157] + "..."
    return f"{sig}" + (f" — {first}" if first else "")


def build_tools_system_prompt(
    tools: list[dict[str, Any]],
    tool_choice: Any | None = None,
) -> str:
    specs = _function_specs(tools)
    if not specs:
        return ""

    lines: list[str] = []
    lines.append(message("tools.intro"))
    lines.append("")
    lines.append(message("tools.verify_first"))
    lines.append("")
    lines.append(message("tools.project_reach"))
    lines.append("")
    lines.append(message("tools.read_length"))
    lines.append("")
    lines.append("# Callable functions")
    for s in specs:
        lines.append(f"- {_compact_signature(s)}")
    lines.append("")
    lines.append("# Output protocol (MANDATORY)")
    lines.append(message("tools.protocol_intro"))
    lines.append(_BEGIN)
    lines.append(
        '[{"name": "<function_name>", "arguments": {<json object matching the schema>}}]'
    )
    lines.append(_END)
    lines.append(message("tools.args_rule"))
    lines.append(message("tools.invalid_rule"))
    lines.append(message("tools.no_discuss"))
    lines.append("")
    lines.append("# Example")
    lines.append(message("tools.example_intro"))
    lines.append(_BEGIN)
    lines.append('[{"name": "glob", "arguments": {"pattern": "**/*"}}]')
    lines.append(_END)

    choice_name = _forced_tool_name(tool_choice)
    if tool_choice == "required" or (
        isinstance(tool_choice, str) and tool_choice not in ("auto", "none")
    ):
        lines.append("")
        lines.append(message("tools.required"))
    if choice_name:
        lines.append("")
        lines.append(message("tools.forced", name=choice_name))

    return "\n".join(lines)


def _forced_tool_name(tool_choice: Any | None) -> str | None:
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            return fn.get("name")
    return None


def has_tool_block(text: str) -> bool:
    """True if the reply contains a (possibly invalid) tool-call block."""
    if not text:
        return False
    if _BLOCK_RE.search(text):
        return True
    f = _FENCE_RE.search(text)
    return bool(f and '"name"' in f.group(1))


_BYPASS_PATTERNS = re.compile(
    r"teams\.microsoft\.com|sharepoint|\boffice\.com\b|loop\.|asyncgw|"
    r"\bsandbox\b|copiarlo|copialo|copia (?:il|qui)|incolla|paste it|copy (?:it|the file)|"
    r"caricar|carica il file|nella mia|in my (?:sandbox|canvas|environment)|"
    # "here's the content, paste it yourself" write-bypass
    r"copy[\- ]?paste|paste (?:it|the|this)|copia[\- ]?incolla|"
    r"(?:don'?t|do not|non) (?:have|hanno|ho) (?:the )?(?:same |stesso )?access|"
    r"the write tool|can'?t write|cannot write|non posso scrivere|salvalo (?:tu|nel)|save it (?:yourself|manually|to)|"
    # identity refusals / "do it in Claude Code yourself"
    r"microsoft (?:enterprise )?copilot|enterprise copilot|i'?m not claude code|"
    r"i (?:don'?t|do not) have access to (?:your |the )?(?:local )?(?:file ?system|filesystem|tools)|"
    r"non ho accesso (?:al|ai|diretto)|different runtime|run (?:this|it).{0,20}claude code|"
    r"i can'?t do this task|cannot do this task|those are claude code|enterprise search",
    re.IGNORECASE,
)


def looks_like_bypass(text: str) -> bool:
    """True if the reply ducked the tools (own canvas/Loop, a link, or 'copy it manually')."""
    return bool(text) and bool(_BYPASS_PATTERNS.search(text))


def build_correction_prompt(base_prompt: str, prev_text: str = "") -> str:
    """Re-issue the task asking for a valid tool-call block. Does NOT echo the prior (failed) reply."""
    return base_prompt + message("tools.correction", begin=_BEGIN, end=_END)


def _allowed_map(tools: list[dict[str, Any]] | None) -> dict[str, set[str]] | None:
    """name -> set(required params), or None if no tool list given."""
    if not tools:
        return None
    out: dict[str, set[str]] = {}
    for s in _function_specs(tools):
        params = s.get("parameters") or {}
        req = params.get("required", []) if isinstance(params, dict) else []
        out[s["name"]] = set(req if isinstance(req, list) else [])
    return out


def parse_tool_calls(
    text: str, tools: list[dict[str, Any]] | None = None
) -> list[ToolCall] | None:
    """Extract and verify tool calls from the model's text reply, or None if there are none.

    Verification (the "JSON verifier"): each call must have a name (in the allowed tool set
    when one is provided), `arguments` must be a JSON object, and required params must be present.
    Invalid calls are dropped.
    """
    if not text:
        return None
    raw = None
    m = _BLOCK_RE.search(text)
    if m:
        raw = m.group(1)
    else:
        f = _FENCE_RE.search(text)
        if f and '"name"' in f.group(1):
            raw = f.group(1)
    if raw is None:
        return None

    data = _loads_tolerant(raw)
    if data is None:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None

    allowed = _allowed_map(tools)
    calls: list[ToolCall] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name or not isinstance(name, str):
            continue
        if allowed is not None and name not in allowed:
            continue  # hallucinated tool name
        args = item.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue  # arguments claimed as string but not valid JSON
        if not isinstance(args, dict):
            continue  # arguments must be a JSON object
        if allowed is not None and not allowed[name].issubset(args.keys()):
            continue  # missing required parameter(s)
        calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:24]}",
                type="function",
                function=FunctionCall(
                    name=name, arguments=json.dumps(args, ensure_ascii=False)
                ),
            )
        )
    return calls or None


def _loads_tolerant(raw: str) -> Any | None:
    raw = raw.strip()
    # Strip an accidental ```json fence around the array.
    raw = re.sub(r"^```(?:json|tool_calls)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Extract the first balanced [...] or {...} span (tolerates trailing junk like `]]`).
    span = _first_balanced(raw)
    if span is not None:
        try:
            return json.loads(span)
        except json.JSONDecodeError:
            return None
    return None


def _first_balanced(s: str) -> str | None:
    start = -1
    opener = closer = ""
    for i, ch in enumerate(s):
        if ch in "[{":
            start = i
            opener, closer = ch, "]" if ch == "[" else "}"
            break
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None
