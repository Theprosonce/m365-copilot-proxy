import json
import logging
import hashlib
import re
import uuid
from typing import Any, Tuple

from ..config import Settings
from ..models import OpenAIChatRequest, ToolCall, FunctionCall

logger = logging.getLogger(__name__)


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
            is_ours = request.model.startswith("m365-") or request.model.startswith("copilot")
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
            raise NotImplementedError(f"Tool emulation mode {self.settings.tool_emulation_mode!r} is not supported.")

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

    def _render_prompt(self, tools: list[dict[str, Any]], tool_choice: Any) -> str:
        if not tools and tool_choice in (None, "auto"):
            return ""

        lines = [
            "# Tool Instructions",
            "You are an AI assistant capable of using tools. When you need external information or action, you must use a tool.",
            "Always think step-by-step before using a tool.",
            "",
            "## Available Tools:",
        ]

        for t in tools:
            name = t.get("name", "unknown")
            desc = (t.get("description", "") or "").replace("\n", " ")
            if len(desc) > 200:
                desc = desc[:197] + "..."

            params = t.get("parameters", {})
            props = params.get("properties", {})
            req = params.get("required", [])

            args_str = []
            for k, v in props.items():
                m = "" if k in req else "?"
                typ = v.get("type", "any")
                args_str.append(f"{k}{m}:{typ}")

            sig = f"{name}({', '.join(args_str)})"
            if desc:
                sig += f" - {desc}"

            if len(sig) > self.settings.tool_emulation_max_single_tool_schema_chars:
                sig = (
                    sig[: self.settings.tool_emulation_max_single_tool_schema_chars - 3]
                    + "..."
                )
            lines.append(f"- {sig}")

        lines.append("")
        lines.append("## Output Protocol (MANDATORY)")
        lines.append(
            "To call a tool, you MUST output a JSON array of tool calls wrapped in strict sentinels:"
        )
        lines.append("<<<TOOL_CALLS>>>")
        lines.append('[{"name": "tool_name", "arguments": {"arg_name": "arg_value"}}]')
        lines.append("<<<END_TOOL_CALLS>>>")
        lines.append("")
        lines.append("Rules:")
        lines.append("1. Do not use Markdown for the block.")
        lines.append(
            "2. Output ONLY the tool block when using a tool. Do not write text before or after."
        )
        lines.append("3. Use exactly one tool call unless specified.")
        lines.append("4. Arguments MUST strictly match the provided schema and types.")
        lines.append(
            "5. Do not invent missing required arguments; ask the user instead."
        )
        lines.append(
            "6. If no tool is needed, answer the user normally without the block."
        )

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
            forced_name = tool_choice

        if forced_name:
            lines.append(f"\nYou MUST use the tool '{forced_name}'.")
        elif tool_choice == "required":
            lines.append("\nYou MUST use at least one tool to answer this query.")

        final_prompt = "\n".join(lines)
        if len(final_prompt) > self.settings.tool_emulation_max_tool_schema_chars:
            final_prompt = (
                final_prompt[: self.settings.tool_emulation_max_tool_schema_chars - 3]
                + "..."
            )

        return final_prompt

    def parse_response(
        self, text: str, tools: list[dict[str, Any]]
    ) -> list[ToolCall] | None:
        if not text:
            return None

        text = text[: self.settings.tool_emulation_max_parse_chars]
        calls_data = None

        # 1. Delimiter First
        if self.settings.tool_emulation_parser_mode == "delimiter_first":
            m = re.search(
                r"<<<TOOL_CALLS>>>\s*(.*?)\s*<<<END_TOOL_CALLS>>>", text, re.DOTALL
            )
            if m:
                calls_data = self._try_parse_json(m.group(1))

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

            if self.settings.tool_emulation_validate_schema:
                if name not in tool_map:
                    continue
                tdef = tool_map[name]
                params_schema = tdef.get("parameters", {})
                req = params_schema.get("required", [])
                props = params_schema.get("properties", {})
                
                args = item.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        continue
                if not isinstance(args, dict):
                    continue
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
                        elif t in ("integer", "number") and not isinstance(v, (int, float)):
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

            args_str = item.get("arguments", "{}")
            if isinstance(args_str, dict):
                args_str = json.dumps(args_str, ensure_ascii=False)

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
        return (
            base_prompt
            + "\n\nYou attempted to use a tool, but the format was invalid or incomplete. You MUST strictly use the requested JSON format enclosed in <<<TOOL_CALLS>>> and <<<END_TOOL_CALLS>>>."
        )

    def looks_like_bypass(self, text: str) -> bool:
        if not text:
            return False
        patterns = [
            r"teams\.microsoft\.com|sharepoint|\boffice\.com\b|loop\.|asyncgw|\bsandbox\b",
            r"copiarlo|copialo|copia (?:il|qui)|incolla|paste it|copy (?:it|the file)",
            r"caricar|carica il file|nella mia|in my (?:sandbox|canvas|environment)",
            r"copy[\- ]?paste|paste (?:it|the|this)|copia[\- ]?incolla",
            r"(?:don'?t|do not|non) (?:have|hanno|ho) (?:the )?(?:same |stesso )?access",
            r"the write tool|can'?t write|cannot write|non posso scrivere|salvalo (?:tu|nel)|save it (?:yourself|manually|to)",
            r"microsoft (?:enterprise )?copilot|enterprise copilot|i'?m not claude code",
            r"i (?:don'?t|do not) have access to (?:your |the )?(?:local )?(?:file ?system|filesystem|tools)",
            r"non ho accesso (?:al|ai|diretto)|different runtime|run (?:this|it).{0,20}claude code",
            r"i can'?t do this task|cannot do this task|those are claude code|enterprise search",
        ]
        return bool(re.search("|".join(patterns), text, re.IGNORECASE))

    def has_tool_block_heuristic(self, text: str) -> bool:
        return bool(re.search(r"<<<TOOL_CALLS>>>|```(?:json|tool_calls)", text))

    async def execute_upstream(
        self,
        client: Any,
        prompt: str,
        additional_context: list[str],
        session: Any,
        tone: str,
        normalized_tools: list[dict[str, Any]],
        images: list[Any] | None = None,
    ) -> Tuple[list[ToolCall] | None, str]:
        """
        Executes the LLM call with retry loop for parsing and fixing malformed tool blocks.
        """
        text = await client.chat(prompt, additional_context, session, tone, images)
        calls = self.parse_response(text, normalized_tools)

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
            calls = self.parse_response(text, normalized_tools)

        return calls, text
