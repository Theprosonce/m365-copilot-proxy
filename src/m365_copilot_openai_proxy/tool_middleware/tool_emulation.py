import json
import logging
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any, Tuple

from ..config import Settings
from ..messages import message
from ..models import OpenAIChatRequest, ToolCall, FunctionCall
from .bypass import looks_like_bypass as _looks_like_bypass

logger = logging.getLogger(__name__)

# Tool-call sentinels (kept in code: coupled to the parser regex below).
_BEGIN = "<<<TOOL_CALLS>>>"
_END = "<<<END_TOOL_CALLS>>>"

FILE_TOOLS_WITH_FILEPATH = frozenset({"read"})

FILE_TOOLS_WITH_PATH = frozenset({"write", "edit", "glob", "list", "search", "bash"})


class ToolEmulationPipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._prompt_cache = {}

    def is_emulation_active(self, request: OpenAIChatRequest) -> bool:
        if not self.settings.tool_emulation_enabled:
            return False

        has_tools = bool(request.tools or request.functions)
        if (
            not has_tools
            and request.tool_choice is None
            and request.function_call is None
        ):
            return False

        if self.settings.tool_emulation_native_passthrough:
            # If native passthrough is enabled, check if we know it's native.
            # If it's a known non-m365/copilot model, it may be native.
            is_ours = request.model.startswith("m365-") or request.model.startswith(
                "copilot"
            )
            if not is_ours:
                if not self.settings.tool_emulation_emulate_when_capability_unknown:
                    return False

        return True

    def preflight(
        self, request: OpenAIChatRequest
    ) -> tuple[OpenAIChatRequest, str | None, list[dict[str, Any]]]:
        """
        Runs preflight, normalizer, reducer, prompt rendering, and request mutation.
        Returns (mutated_request, tools_prompt, normalized_tools).
        """
        if self.settings.tool_emulation_mode not in ("response_only",):
            raise NotImplementedError(
                f"Tool emulation mode {self.settings.tool_emulation_mode!r} is not supported."
            )

        normalized_tools = self._normalize_tools(request)

        if (
            not normalized_tools
            and request.tool_choice in (None, "none", "auto")
            and request.function_call in (None, "none")
        ):
            return request, None, []

        reduced_tools = self._reduce_tools(
            normalized_tools, request.tool_choice or request.function_call, request
        )

        tools_prompt = None
        tool_choice = request.tool_choice or request.function_call
        if reduced_tools or tool_choice not in (None, "none", "auto"):
            cache_key = self._get_prompt_cache_key(reduced_tools, tool_choice)
            if (
                self.settings.tool_emulation_cache_rendered_tool_prompts
                and cache_key in self._prompt_cache
            ):
                tools_prompt = self._prompt_cache[cache_key]
            else:
                tools_prompt = self._render_prompt(reduced_tools, tool_choice)
                if self.settings.tool_emulation_cache_rendered_tool_prompts:
                    self._prompt_cache[cache_key] = tools_prompt

        new_req = request.model_copy(deep=True)
        new_req.tools = None
        new_req.tool_choice = None
        new_req.functions = None
        new_req.function_call = None

        if self.settings.tool_emulation_force_non_streaming:
            new_req.stream = False

        if self.settings.tool_emulation_override_temperature:
            new_req.temperature = self.settings.tool_emulation_default_temperature

        return new_req, tools_prompt, normalized_tools

    def _normalize_tools(self, request: OpenAIChatRequest) -> list[dict[str, Any]]:
        tools = []
        if request.tools:
            for t in request.tools:
                if t.get("type") == "function" and "function" in t:
                    tools.append(t["function"])
                elif "name" in t:
                    # Anthropic shape fallback or direct function dict
                    tools.append(t)
        if request.functions:
            for f in request.functions:
                tools.append(f)
        return tools

    def _reduce_tools(
        self, tools: list[dict[str, Any]], tool_choice: Any, request: OpenAIChatRequest
    ) -> list[dict[str, Any]]:
        logger.info(
            f"EXCLUDE TOOLS SETTING: {self.settings.tool_emulation_exclude_tools!r}"
        )
        exclude_list = [
            t.strip()
            for t in self.settings.tool_emulation_exclude_tools.split(",")
            if t.strip()
        ]
        if exclude_list:
            tools = [t for t in tools if t.get("name") not in exclude_list]

        if tool_choice == "none":
            return []

        forced_name = None
        if isinstance(tool_choice, dict):
            forced_name = tool_choice.get("function", {}).get(
                "name"
            ) or tool_choice.get("name")
        elif isinstance(tool_choice, str) and tool_choice not in (
            "auto",
            "none",
            "required",
        ):
            # Assume it's a legacy force by string if not auto/none
            forced_name = tool_choice

        if forced_name:
            filtered = [t for t in tools if t.get("name") == forced_name]
            if not filtered:
                raise ValueError(f"Forced tool '{forced_name}' not found in tools.")
            return filtered

        if len(tools) <= self.settings.tool_emulation_max_tools_in_prompt:
            return tools

        # Deterministic Ranking
        # Extract terms from last user message
        query_terms = set()
        if request.messages:
            last_msg = request.messages[-1]
            if getattr(last_msg, "role", "") in ("user", "system", "developer"):
                content = getattr(last_msg, "content", "")
                if isinstance(content, str):
                    query_terms = set(re.findall(r"\w+", content.lower()))

        def score_tool(t: dict[str, Any]) -> int:
            text = f"{t.get('name', '')} {t.get('description', '')}".lower()
            params = t.get("parameters", {}).get("properties", {})
            for p in params:
                text += f" {p}"
            terms = set(re.findall(r"\w+", text))
            return len(query_terms & terms)

        # Sort by score descending, then by name alphabetically for determinism
        ranked = sorted(tools, key=lambda t: (-score_tool(t), t.get("name", "")))
        return ranked[: self.settings.tool_emulation_max_tools_in_prompt]

    def _get_prompt_cache_key(
        self, tools: list[dict[str, Any]], tool_choice: Any
    ) -> str:
        data = {
            "tools": tools,
            "tool_choice": tool_choice,
            "v": self.settings.tool_emulation_prompt_template_version,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _forced_tool_name(tool_choice: Any) -> str | None:
        if isinstance(tool_choice, dict):
            return tool_choice.get("function", {}).get("name") or tool_choice.get("name")
        if isinstance(tool_choice, str) and tool_choice not in ("auto", "none", "required"):
            return tool_choice
        return None

    def _tool_signature(self, t: dict[str, Any]) -> str:
        name = t.get("name", "unknown")
        desc = (t.get("description", "") or "").replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:197] + "..."
        params = t.get("parameters", {}) or {}
        props = params.get("properties", {}) if isinstance(params, dict) else {}
        req = params.get("required", []) if isinstance(params, dict) else []
        args_str = []
        for k, v in props.items():
            mark = "" if k in req else "?"
            typ = v.get("type", "any") if isinstance(v, dict) else "any"
            args_str.append(f"{k}{mark}:{typ}")
        sig = f"{name}({', '.join(args_str)})"
        if desc:
            sig += f" - {desc}"
        cap = self.settings.tool_emulation_max_single_tool_schema_chars
        if len(sig) > cap:
            sig = sig[: cap - 3] + "..."
        return sig

    def _render_prompt(self, tools: list[dict[str, Any]], tool_choice: Any) -> str:
        # The fixed prose comes from the shared message bundle (messages.properties /
        # M365_PROMPT_TOOLS_*); only the per-tool signatures and sentinels are built here.
        if not tools and tool_choice in (None, "auto"):
            return ""

        lines = [
            message("tools.intro"),
            "",
            message("tools.verify_first"),
            "",
            message("tools.project_reach"),
            "",
            message("tools.read_length"),
            "",
            "# Callable functions",
        ]
        for t in tools:
            lines.append(f"- {self._tool_signature(t)}")
        lines += [
            "",
            "# Output protocol (MANDATORY)",
            message("tools.protocol_intro"),
            _BEGIN,
            '[{"name": "<function_name>", "arguments": {<json object matching the schema>}}]',
            _END,
            message("tools.args_rule"),
            message("tools.invalid_rule"),
            message("tools.no_discuss"),
            "",
            "# Example",
            message("tools.example_intro"),
            _BEGIN,
            '[{"name": "glob", "arguments": {"pattern": "**/*"}}]',
            _END,
        ]

        forced_name = self._forced_tool_name(tool_choice)
        if forced_name:
            lines += ["", message("tools.forced", name=forced_name)]
        elif tool_choice == "required":
            lines += ["", message("tools.required")]

        final_prompt = "\n".join(lines)
        cap_total = self.settings.tool_emulation_max_tool_schema_chars
        if len(final_prompt) > cap_total:
            final_prompt = final_prompt[: cap_total - 3] + "..."
        return final_prompt

    def _extract_delimited_block(self, text: str) -> str | None:
        """Strict two-phase extraction of the tool-call payload.

        Phase 1 (gate): the response must literally BEGIN with the
        ``<<<TOOL_CALLS>>>`` marker (leading whitespace tolerated). The rendered
        protocol contract is that a tool-calling turn is nothing but the
        delimited block, so a well-formed reply starts with the marker. If the
        marker only appears mid-sentence (e.g. "I will use <<<TOOL_CALLS>>>"),
        there is no tool call here: bail out and let the caller treat the text as
        ordinary prose instead of mis-parsing the mention.

        Phase 2 (capture): scan from the END of the response for the matching
        ``<<<END_TOOL_CALLS>>>`` delimiter and return the payload between them.
        Bounding with the *last* closer is robust against a model that echoes the
        opening marker inside its JSON or tacks on trailing chatter.

        Returns the stripped payload string, or ``None`` when no strict block is
        present (the caller then redacts any stray sentinel before replying).
        """
        if not text:
            return None
        # Phase 1: the block must sit at the very start of the response.
        if not text.lstrip().startswith(_BEGIN):
            return None
        # Phase 2: bound the payload with the final closing delimiter.
        end = text.rfind(_END)
        if end < 0:
            # Opening marker present but the block never closed -> malformed.
            # Refuse to parse; the redaction layer scrubs the raw marker.
            return None
        start = text.find(_BEGIN)
        return text[start + len(_BEGIN) : end].strip()

    def _redact_tool_sentinels(self, text: str) -> str:
        """Strip every trace of the internal tool-call sentinels from ``text``.

        Guarantees the raw ``<<<TOOL_CALLS>>>`` / ``<<<END_TOOL_CALLS>>>`` wire
        tokens never reach the end user. A complete delimited block is removed as
        a unit; any sentinel left dangling (a malformed block, or a stray mention
        in conversational prose) is then scrubbed individually, and the orphaned
        whitespace is tidied up so the message has no blank gaps.
        """
        if not text:
            return text
        # 1. Drop complete blocks (DOTALL; non-greedy so each END closes its own
        #    block, sweeping up trailing whitespace too).
        scrubbed = re.sub(
            re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\s*",
            "",
            text,
            flags=re.DOTALL,
        )
        # 2. Remove any sentinel left without a matching partner.
        scrubbed = scrubbed.replace(_BEGIN, "").replace(_END, "")
        # 3. Collapse whitespace orphaned by the removals.
        scrubbed = re.sub(r"[ \t]{2,}", " ", scrubbed)
        return scrubbed.strip()

    def parse_response(
        self, text: str, tools: list[dict[str, Any]], workspace_root: str | None = None
    ) -> list[ToolCall] | None:
        if not text:
            return None

        text = text[: self.settings.tool_emulation_max_parse_chars]
        calls_data = None

        # 1. Delimiter First — STRICT: the response must begin with the marker.
        #   See _extract_delimited_block: a mid-sentence mention of the sentinel
        #   is NOT a tool call and must not be parsed as one.
        if self.settings.tool_emulation_parser_mode == "delimiter_first":
            payload = self._extract_delimited_block(text)
            if payload is not None:
                calls_data = self._try_parse_json(payload)

        # 2. Fenced JSON (Markdown recovery)
        if (
            calls_data is None
            and self.settings.tool_emulation_allow_markdown_json_recovery
        ):
            m = re.search(r"```(?:json|tool_calls)?\s*(\[.*?\])\s*```", text, re.DOTALL)
            if m:
                calls_data = self._try_parse_json(m.group(1))

        # 3. Plain JSON
        if calls_data is None and self.settings.tool_emulation_allow_plain_json:
            m = re.search(r"^\s*(\[.*?\])\s*$", text, re.DOTALL)
            if m:
                calls_data = self._try_parse_json(m.group(1))

        # 4. Loose recovery
        if (
            calls_data is None
            and self.settings.tool_emulation_allow_loose_json_recovery
        ):
            calls_data = self._first_balanced_array(text)

        if not calls_data:
            return None

        if isinstance(calls_data, dict):
            calls_data = [calls_data]

        if not isinstance(calls_data, list):
            return None

        tool_map = {t.get("name"): t for t in tools}

        tool_calls = []
        for item in calls_data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue

            args = item.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if not isinstance(args, dict):
                args = {}

            if workspace_root:
                try:
                    # Normalize and validate filePath only for known file tools
                    if (
                        name in FILE_TOOLS_WITH_FILEPATH
                        and "filePath" in args
                        and isinstance(args["filePath"], str)
                    ):
                        fp = args["filePath"]
                        workspace_path = Path(workspace_root).resolve()
                        target_path = Path(fp)
                        if not target_path.is_absolute():
                            target_path = workspace_path / target_path
                        resolved_path = target_path.resolve()

                        if not (
                            workspace_path in resolved_path.parents
                            or resolved_path == workspace_path
                        ):
                            logger.warning(f"Rejected path outside workspace: {fp}")
                            continue

                        args["filePath"] = str(resolved_path)

                    # Normalize and validate path only for known file tools
                    if (
                        name in FILE_TOOLS_WITH_PATH
                        and "path" in args
                        and isinstance(args["path"], str)
                    ):
                        p = args["path"]
                        workspace_path = Path(workspace_root).resolve()
                        target_path = Path(p)
                        if not target_path.is_absolute():
                            target_path = workspace_path / target_path
                        resolved_path = target_path.resolve()

                        if not (
                            workspace_path in resolved_path.parents
                            or resolved_path == workspace_path
                        ):
                            logger.warning(f"Rejected path outside workspace: {p}")
                            continue

                        args["path"] = str(resolved_path)
                except Exception as e:
                    logger.warning(f"Error normalizing/validating path: {e}")
                    continue

            if self.settings.tool_emulation_validate_schema:
                if name not in tool_map:
                    continue
                tdef = tool_map[name]
                params_schema = tdef.get("parameters", {})
                req = params_schema.get("required", [])
                props = params_schema.get("properties", {})

                missing = [r for r in req if r not in args]
                if missing:
                    continue

                # Basic type and enum validation
                invalid = False
                for k, v in args.items():
                    if k in props:
                        p_schema = props[k]
                        t = p_schema.get("type")
                        if t == "string" and not isinstance(v, str):
                            invalid = True
                        elif t in ("integer", "number") and not isinstance(
                            v, (int, float)
                        ):
                            invalid = True
                        elif t == "boolean" and not isinstance(v, bool):
                            invalid = True
                        elif t == "array" and not isinstance(v, list):
                            invalid = True
                        elif t == "object" and not isinstance(v, dict):
                            invalid = True

                        enum_vals = p_schema.get("enum")
                        if enum_vals and v not in enum_vals:
                            invalid = True

                if invalid:
                    continue

            args_str = json.dumps(args, ensure_ascii=False)

            tool_calls.append(
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:24]}",
                    type="function",
                    function=FunctionCall(name=name, arguments=args_str),
                )
            )

        return tool_calls if tool_calls else None

    def _try_parse_json(self, s: str) -> Any:
        s = s.strip()
        try:
            return json.loads(s)
        except Exception:
            return None

    def _first_balanced_array(self, s: str) -> Any:
        start = s.find("[")
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
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return self._try_parse_json(s[start : i + 1])
        return None

    def build_correction_prompt(self, base_prompt: str) -> str:
        return base_prompt + message("tools.correction", begin=_BEGIN, end=_END)

    def looks_like_bypass(self, text: str) -> bool:
        return _looks_like_bypass(text)

    def has_tool_block_heuristic(self, text: str) -> bool:
        """Heuristic: did the model make a GENUINE attempt at the tool protocol?

        Used to decide whether a single correction retry is worthwhile. This must
        agree with ``_extract_delimited_block``'s strict contract: a real (if
        malformed) tool block *starts* with the sentinel, whereas a mid-sentence
        mention of ``<<<TOOL_CALLS>>>`` in conversational prose does NOT count and
        must not trigger a retry. The fenced-JSON shape is still treated as a
        genuine attempt because the model followed the structure, just in markdown.
        """
        if text and text.lstrip().startswith(_BEGIN):
            return True
        return bool(re.search(r"```(?:json|tool_calls)", text))

    async def execute_upstream(
        self,
        client: Any,
        prompt: str,
        additional_context: list[str],
        session: Any,
        tone: str,
        normalized_tools: list[dict[str, Any]],
        images: list[Any] | None = None,
        workspace_root: str | None = None,
    ) -> Tuple[list[ToolCall] | None, str]:
        """
        Executes the LLM call with retry loop for parsing and fixing malformed tool blocks.
        """
        text = await client.chat(prompt, additional_context, session, tone, images)
        calls = self.parse_response(
            text, normalized_tools, workspace_root=workspace_root
        )

        attempt = 0
        max_retries = (
            1 if self.settings.tool_emulation_repair_invalid_tool_call_once else 0
        )

        while calls is None and attempt < max_retries:
            if not text.strip():
                retry_prompt = prompt
            elif self.has_tool_block_heuristic(text) or self.looks_like_bypass(text):
                retry_prompt = self.build_correction_prompt(prompt)
            else:
                break

            attempt += 1
            text = await client.chat(retry_prompt, additional_context, session, tone)
            calls = self.parse_response(
                text, normalized_tools, workspace_root=workspace_root
            )

        # Guarantee no internal wire token ever reaches the user. When calls were
        # extracted the caller ignores `text`; when they weren't, `text` is the
        # surfaced reply and may still carry the sentinel (a malformed block, a
        # dangling open marker, or a stray mention). Scrubbing in every case is a
        # belt-and-braces redaction: callers never see the raw sentinel.
        return calls, self._redact_tool_sentinels(text)
