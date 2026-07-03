import json
import logging
import os
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any, Tuple

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.messages import message
from m365_copilot_openai_proxy.models import OpenAIChatRequest, ToolCall, FunctionCall
from .bypass import looks_like_bypass as _looks_like_bypass

logger = logging.getLogger(__name__)

# Tool-call sentinels (kept in code: coupled to the parser regex below).
_BEGIN = "<<<TOOL_CALLS>>>"
_END = "<<<END_TOOL_CALLS>>>"

# Load and cache tool emulation injection content at startup.
# Config is read from config.ini only; environment overrides are intentionally ignored.
_configured_injection_path = Settings().tool_emulation_injection_path
if _configured_injection_path:
    _injection_file_path = Path(_configured_injection_path)
else:
    _injection_file_path = Path("./prompts/tool_emulation_injection.md")

if not _injection_file_path.exists():
    raise FileNotFoundError(f"Injection file not found at {_injection_file_path.absolute()}")

with _injection_file_path.open("r", encoding="utf-8") as _f:
    _INJECTION_CONTENT = _f.read()

if not _INJECTION_CONTENT.strip():
    logger.warning(f"Injection file at {_injection_file_path.absolute()} exists but is empty.")


def _part_type(part: Any) -> str | None:
    if hasattr(part, "type"):
        return getattr(part, "type", None)
    if isinstance(part, dict):
        return part.get("type")
    return None


def _part_text(part: Any) -> str:
    if hasattr(part, "text"):
        return getattr(part, "text", None) or ""
    if isinstance(part, dict):
        return part.get("text") or ""
    return ""


def _has_tool_result_part(content: list[Any]) -> bool:
    return any(_part_type(part) == "tool_result" for part in content)


def _has_nonempty_text_part(content: list[Any]) -> bool:
    return any(
        _part_type(part) == "text" and bool(_part_text(part).strip())
        for part in content
    )


def _apply_message_injection(messages: list[Any], settings: Settings | None = None) -> None:
    current_settings = settings or Settings()
    if not current_settings.prompt_injection_enabled:
        return
    if not _INJECTION_CONTENT.strip():
        return

    escaped = re.escape(_INJECTION_CONTENT.strip())
    pattern = re.compile(rf"^\s*{escaped}\s*\n?---\n?\s*", re.DOTALL)

    for msg in messages:
        role = getattr(msg, "role", None)
        if isinstance(role, str) and role.strip().lower() == "user":
            if isinstance(msg.content, str):
                if pattern.match(msg.content):
                    continue
                logger.debug(
                    f"Prepend injection length: {len(_INJECTION_CONTENT)}, first 50 chars: {repr(_INJECTION_CONTENT[:50])}"
                )
                msg.content = f"{_INJECTION_CONTENT}\n---\n{msg.content}"
            elif isinstance(msg.content, list):
                # Anthropic tool results are represented as role=user messages with
                # tool_result content blocks. Injecting the tool protocol into those
                # continuation turns creates fake user text and can erase the real
                # agentic continuation prompt after app.py strips the injected prefix.
                if _has_tool_result_part(msg.content) and not _has_nonempty_text_part(msg.content):
                    logger.debug("Skip injection for Anthropic tool_result-only user message.")
                    continue

                text_part = None
                for part in msg.content:
                    if _part_type(part) == "text":
                        text_part = part
                        break

                if text_part is not None:
                    if hasattr(text_part, "text"):
                        val = text_part.text or ""
                        if pattern.match(val):
                            continue
                        logger.debug(
                            f"Prepend injection length: {len(_INJECTION_CONTENT)}, first 50 chars: {repr(_INJECTION_CONTENT[:50])}"
                        )
                        text_part.text = f"{_INJECTION_CONTENT}\n---\n{val}"
                    elif isinstance(text_part, dict):
                        val = text_part.get("text") or ""
                        if pattern.match(val):
                            continue
                        logger.debug(
                            f"Prepend injection length: {len(_INJECTION_CONTENT)}, first 50 chars: {repr(_INJECTION_CONTENT[:50])}"
                        )
                        text_part["text"] = f"{_INJECTION_CONTENT}\n---\n{val}"
                else:
                    logger.debug(
                        f"Prepend injection length: {len(_INJECTION_CONTENT)}, first 50 chars: {repr(_INJECTION_CONTENT[:50])}"
                    )
                    if msg.content and hasattr(type(msg.content[0]), "model_fields"):
                        from m365_copilot_openai_proxy.models import ContentPart
                        new_part = ContentPart(type="text", text=f"{_INJECTION_CONTENT}\n---\n")
                        msg.content.insert(0, new_part)
                    else:
                        new_part = {"type": "text", "text": f"{_INJECTION_CONTENT}\n---\n"}
                        msg.content.insert(0, new_part)
            elif msg.content is None:
                logger.debug(
                    f"Prepend injection length: {len(_INJECTION_CONTENT)}, first 50 chars: {repr(_INJECTION_CONTENT[:50])}"
                )
                msg.content = f"{_INJECTION_CONTENT}\n---\n"


from emulator.schemas import FILE_TOOLS_WITH_FILEPATH, FILE_TOOLS_WITH_PATH, _DEFAULT_TOOL_SCHEMAS


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
        if self.settings.prompt_injection_enabled:
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

        def normalize_function(fn: dict[str, Any]) -> dict[str, Any] | None:
            name = fn.get("name")
            if not name:
                return None
            normalized = dict(fn)
            if "parameters" not in normalized and isinstance(normalized.get("input_schema"), dict):
                # Anthropic-compatible clients use input_schema. The prompt renderer
                # and parser validate against OpenAI-style parameters internally, so
                # normalize at the edge while preserving the callable tool name.
                normalized["parameters"] = dict(normalized["input_schema"])
            return normalized

        if request.tools:
            for t in request.tools:
                fn = None
                if t.get("type") == "function" and isinstance(t.get("function"), dict):
                    fn = t["function"]
                elif isinstance(t.get("function"), dict):
                    fn = t["function"]
                elif "name" in t:
                    # Anthropic shape fallback or direct function dict.
                    fn = t
                if fn:
                    normalized = normalize_function(fn)
                    if normalized:
                        tools.append(normalized)
        if request.functions:
            for f in request.functions:
                normalized = normalize_function(f)
                if normalized:
                    tools.append(normalized)
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

        # Do not limit the model or use keyword-based tool reduction; return all tools as-is.
        return tools

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
        # prompt catalog tool keys); only the per-tool signatures and sentinels are built here.
        if not tools and tool_choice in (None, "auto"):
            return ""

        forced_name = self._forced_tool_name(tool_choice)
        displayed_tools = tools
        if forced_name:
            displayed_tools = [t for t in tools if t.get("name") == forced_name]

        lines = [
            _INJECTION_CONTENT,
            "",
            "# Callable functions",
        ]
        for t in displayed_tools:
            lines.append(f"- {self._tool_signature(t)}")

        if forced_name:
            lines += ["", message("tools.forced", name=forced_name)]
        elif tool_choice == "required":
            lines += ["", message("tools.required")]

        final_prompt = "\n".join(lines)
        cap_total = self.settings.tool_emulation_max_tool_schema_chars
        if len(final_prompt) > cap_total:
            final_prompt = final_prompt[: cap_total - 3] + "..."
        return final_prompt

    def _anthropic_tool_signature(self, tool: dict[str, Any]) -> str:
        name = str(tool.get("name") or "unknown")
        desc = str(tool.get("description") or "").replace("\n", " ").strip()
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            schema = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {}
        schema_json = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        sig = f"{name}(input_schema={schema_json})"
        if desc:
            sig += f" - {desc}"
        cap = self.settings.tool_emulation_max_single_tool_schema_chars
        if len(sig) > cap:
            sig = sig[: cap - 3] + "..."
        return sig

    def render_anthropic_prompt(
        self,
        original_tools: list[dict[str, Any]],
        reduced_tools: list[dict[str, Any]],
        tool_choice: Any,
    ) -> str:
        if not reduced_tools and tool_choice in (None, "auto"):
            return ""

        allowed_names = {t.get("name") for t in reduced_tools}
        forced_name = self._forced_tool_name(tool_choice)
        displayed_tools = [
            t for t in original_tools
            if t.get("name") in allowed_names and (not forced_name or t.get("name") == forced_name)
        ]

        # If an Anthropic-compatible client supplied tools only after adapter
        # normalization, still render something useful rather than dropping the
        # tool list. The normal path above preserves each original name,
        # description, and input_schema exactly as supplied.
        if not displayed_tools:
            displayed_tools = reduced_tools

        lines = [
            _INJECTION_CONTENT,
            "",
            "# Callable functions",
        ]
        for t in displayed_tools:
            lines.append(f"- {self._anthropic_tool_signature(t)}")

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
        from m365_copilot_openai_proxy.debug_logger import log_event, log_raw_event
        if not text:
            log_event("TOOL_PARSE_SKIPPED", {
                "source": "assistant_output",
                "reason": "empty response text"
            })
            return None

        log_event("TOOL_PARSE_INPUT", {
            "source": "assistant_output",
            "text": text,
            "text_len": len(text)
        })

        # Do not parse title JSON as task output
        try:
            cleaned_text = text.strip()
            if cleaned_text.startswith("```"):
                lines = cleaned_text.splitlines()
                if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```"):
                    cleaned_text = "\n".join(lines[1:-1]).strip()
            parsed_test = json.loads(cleaned_text)
            if isinstance(parsed_test, dict) and "title" in parsed_test:
                log_event("TITLE_GENERATION_DECISION", {
                    "detected": True,
                    "short_circuited": True,
                    "appended_to_task_history": False
                })
                log_raw_event("Title", {
                    "title_generation_detected": True,
                    "reason": "user requested title"
                })
                self._last_parse_result = {
                    "has_sentinels": False,
                    "raw_tool_calls": [],
                    "accepted_tool_calls": [],
                    "rejected_tool_calls": [],
                    "parse_error": None,
                }
                return None
        except Exception:
            pass

        text = text[: self.settings.tool_emulation_max_parse_chars]
        calls_data = None

        # 1. Delimiter First — STRICT: the response must begin with the marker.
        #   See _extract_delimited_block: a mid-sentence mention of the sentinel
        #   is NOT a tool call and must not be parsed as one.
        if self.settings.tool_emulation_parser_mode == "delimiter_first":
            payload = self._extract_delimited_block(text)
            if payload is not None:
                calls_data = self._try_parse_json(payload)

        # 1.5. Delimited Block Recovery — fallback for when the delimited block doesn't start at the very beginning of the text
        if calls_data is None:
            start_idx = text.find(_BEGIN)
            if start_idx >= 0:
                end_idx = text.find(_END, start_idx + len(_BEGIN))
                if end_idx >= 0:
                    fallback_payload = text[start_idx + len(_BEGIN) : end_idx].strip()
                    calls_data = self._try_parse_json(fallback_payload)

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

        has_sentinels = text.find(_BEGIN) >= 0 or bool(re.search(r"```(?:json|tool_calls)", text))
        if not calls_data:
            if has_sentinels:
                # try to extract a candidate JSON block for the raw_block logging
                raw_block_text = None
                m = re.search(r"```(?:json|tool_calls)?\s*(\[.*?\])\s*```", text, re.DOTALL)
                if m:
                    raw_block_text = m.group(1)
                else:
                    start_idx = text.find(_BEGIN)
                    if start_idx >= 0:
                        end_idx = text.find(_END, start_idx + len(_BEGIN))
                        if end_idx >= 0:
                            raw_block_text = text[start_idx + len(_BEGIN) : end_idx].strip()
                self._last_parse_result = {
                    "has_sentinels": True,
                    "raw_tool_calls": [],
                    "accepted_tool_calls": [],
                    "rejected_tool_calls": [],
                    "parse_error": "invalid JSON",
                    "raw_block": raw_block_text or text
                }
                log_event("TOOL_PARSE_RESULT", {
                    "found": True,
                    "parse_error": "invalid JSON",
                    "raw_block": raw_block_text or text
                })
                log_raw_event("Tool Parse", {
                    "found": True,
                    "reason": "invalid JSON",
                    "raw_block": raw_block_text or text
                })
            else:
                self._last_parse_result = {
                    "has_sentinels": False,
                    "raw_tool_calls": [],
                    "accepted_tool_calls": [],
                    "rejected_tool_calls": [],
                    "parse_error": None,
                }
                log_event("TOOL_PARSE_RESULT", {
                    "found": False,
                    "tool_call_count": 0
                })
                log_raw_event("Tool Parse", {
                    "found": False,
                    "reason": "no TOOL_CALLS block"
                })
            return None

        if isinstance(calls_data, dict):
            calls_data = [calls_data]

        if not isinstance(calls_data, list):
            self._last_parse_result = {
                "has_sentinels": has_sentinels,
                "raw_tool_calls": [],
                "accepted_tool_calls": [],
                "rejected_tool_calls": [],
                "parse_error": "invalid JSON",
            }
            return None

        tool_map = {t.get("name"): t for t in tools if t.get("name")}
        # Merge default schemas for standard workspace tools if they aren't already registered
        for default_name, default_schema in _DEFAULT_TOOL_SCHEMAS.items():
            if default_name not in tool_map:
                tool_map[default_name] = default_schema

        tool_map_lower = {t.get("name").lower(): t for t in tool_map.values() if t.get("name")}

        raw_tool_calls = []
        accepted_tool_calls = []
        rejected_tool_calls = []
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

            raw_tool_calls.append({
                "name": name,
                "arguments": args
            })

            if name not in tool_map:
                name_lower = name.lower()
                if name_lower in tool_map_lower:
                    normalized_name = tool_map_lower[name_lower].get("name")
                    if normalized_name != name:
                        log_event("TOOL_NAME_NORMALIZED", {
                            "from": name,
                            "to": normalized_name
                        })
                    name = normalized_name

            rejected = False
            rejection_reason = ""

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
                            rejected = True
                            rejection_reason = "path outside workspace"
                        else:
                            args["filePath"] = str(resolved_path)

                    # Normalize and validate path only for known file tools
                    if (
                        not rejected
                        and name in FILE_TOOLS_WITH_PATH
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
                            rejected = True
                            rejection_reason = "path outside workspace"
                        else:
                            args["path"] = str(resolved_path)
                except Exception as e:
                    logger.warning(f"Error normalizing/validating path: {e}")
                    rejected = True
                    rejection_reason = f"error validating path: {str(e)}"

            if not rejected and self.settings.tool_emulation_validate_schema:
                if name not in tool_map:
                    rejected = True
                    rejection_reason = "tool not registered"
                else:
                    tdef = tool_map[name]
                    params_schema = tdef.get("parameters", {})
                    req = params_schema.get("required", [])
                    props = params_schema.get("properties", {})

                    missing = [r for r in req if r not in args]
                    if missing:
                        rejected = True
                        rejection_reason = f"missing required parameters: {', '.join(missing)}"
                    else:
                        # Basic type and enum validation
                        invalid = False
                        invalid_reason = ""
                        for k, v in args.items():
                            if k in props:
                                p_schema = props[k]
                                t = p_schema.get("type")
                                if t == "string" and not isinstance(v, str):
                                    invalid = True
                                    invalid_reason = f"parameter {k} must be string"
                                elif t in ("integer", "number") and not isinstance(
                                    v, (int, float)
                                ):
                                    invalid = True
                                    invalid_reason = f"parameter {k} must be number"
                                elif t == "boolean" and not isinstance(v, bool):
                                    invalid = True
                                    invalid_reason = f"parameter {k} must be boolean"
                                elif t == "array" and not isinstance(v, list):
                                    invalid = True
                                    invalid_reason = f"parameter {k} must be array"
                                elif t == "object" and not isinstance(v, dict):
                                    invalid = True
                                    invalid_reason = f"parameter {k} must be object"

                                enum_vals = p_schema.get("enum")
                                if enum_vals and v not in enum_vals:
                                    invalid = True
                                    invalid_reason = f"parameter {k} must be one of {enum_vals}"

                        if invalid:
                            rejected = True
                            rejection_reason = invalid_reason

            if rejected:
                rejected_tool_calls.append({
                    "name": name,
                    "reason": rejection_reason
                })
                continue

            accepted_tool_calls.append({
                "name": name,
                "arguments": args
            })

            args_str = json.dumps(args, ensure_ascii=False)

            tool_calls.append(
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:24]}",
                    type="function",
                    function=FunctionCall(name=name, arguments=args_str),
                )
            )

        self._last_parse_result = {
            "has_sentinels": has_sentinels,
            "raw_tool_calls": raw_tool_calls,
            "accepted_tool_calls": accepted_tool_calls,
            "rejected_tool_calls": rejected_tool_calls,
            "parse_error": None,
        }

        # TOOL_PARSE_RESULT log
        log_event("TOOL_PARSE_RESULT", {
            "found": True,
            "parse_error": None,
            "tool_call_count": len(tool_calls),
            "raw_tool_calls": raw_tool_calls,
            "accepted_tool_calls": accepted_tool_calls,
            "rejected_tool_calls": rejected_tool_calls,
            "tool_calls": accepted_tool_calls
        })
        log_raw_event("Tool Parse", {
            "found": True,
            "raw_tool_calls": raw_tool_calls,
            "accepted_tool_calls": accepted_tool_calls,
            "rejected_tool_calls": rejected_tool_calls,
            "tool_calls": accepted_tool_calls
        })
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

    def _diagnose_tool_protocol_error(self, text: str, calls: list[ToolCall] | None) -> Tuple[bool, str, str, str, str]:
        parse_res = getattr(self, "_last_parse_result", {})
        has_error = False
        category = ""
        tool_name = "unknown"
        exact_reason = ""
        feedback_lines = []

        if parse_res.get("parse_error") == "invalid JSON":
            has_error = True
            category = "malformed_tool_calls_json"
            exact_reason = "invalid JSON"
            feedback_lines.append(
                "Tool protocol feedback: Invalid JSON structure in TOOL_CALLS block. "
                "Please re-emit ONLY a corrected TOOL_CALLS block with valid JSON."
            )
        elif parse_res.get("rejected_tool_calls"):
            has_error = True
            rejected_details = parse_res.get("rejected_tool_calls")

            # Check if any is "tool not registered"
            has_unregistered = any(rc.get("reason") == "tool not registered" for rc in rejected_details)
            if has_unregistered:
                category = "unsupported_tool_call"
                exact_reason = "unsupported tool"
            else:
                category = "schema_invalid_or_unsafe_path"
                exact_reason = "unsupported tool, schema-invalid, or unsafe path"

            for rc in rejected_details:
                name_val = rc.get("name") or "unknown"
                reason_val = rc.get("reason") or "unknown"
                tool_name = name_val
                if reason_val == "tool not registered":
                    feedback_lines.append(
                        f'Tool protocol feedback: Tool "{name_val}" is not available in the current route. '
                        f'Use one of the available tools or continue without it.'
                    )
                else:
                    feedback_lines.append(
                        f'Tool protocol feedback: Tool call "{name_val}" was rejected: {reason_val}. '
                        f'Please re-emit a corrected TOOL_CALLS block.'
                    )
        elif calls is None and self.has_tool_block_heuristic(text):
            has_error = True
            category = "malformed_tool_calls_json"
            exact_reason = "Invalid or unsupported tool call in tool block"
            feedback_lines.append(
                "Tool protocol feedback: Invalid or unsupported tool call in tool block. "
                "Please re-emit ONLY a corrected TOOL_CALLS block."
            )

        feedback_message = "\n".join(feedback_lines) if feedback_lines else ""
        return has_error, category, tool_name, exact_reason, feedback_message

    def _check_tool_protocol_error(self, text: str, calls: list[ToolCall] | None) -> Tuple[bool, str]:
        has_error, error_category, error_tool, error_reason, feedback_message = self._diagnose_tool_protocol_error(text, calls)
        if has_error:
            parse_res = getattr(self, "_last_parse_result", {})
            rejected_details = parse_res.get("rejected_tool_calls", [])
            if rejected_details:
                details_list = []
                for rc in rejected_details:
                    name_val = rc.get("name") or "unknown"
                    reason_val = rc.get("reason") or "unknown"
                    details_list.append(f"- Tool '{name_val}': {reason_val}")
                details_text = "\n".join(details_list)
            else:
                details_text = "Invalid JSON structure. Could not parse tool calls."

            error_message = (
                "Tool protocol error: TOOL_CALLS block was detected but could not be executed.\n\n"
                f"Reason:\n{error_reason}\n\n"
                f"Rejected calls:\n{details_text}\n\n"
                "Please re-emit ONLY a corrected TOOL_CALLS block, or continue without the unsupported tool.\n\n"
                "Do not include prose outside the block if retrying."
            )
            return True, error_message
        return False, ""

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
        If a tool call is parsed, executes it locally via RuntimeBridge, sends the result back,
        and returns the final text result.
        """
        from m365_copilot_openai_proxy.debug_logger import log_event, log_raw_event

        attempt = 0
        retry_limit = getattr(self.settings, "tool_emulation_protocol_error_retry_limit", 1)

        # We start with the original additional_context
        current_context = list(additional_context)

        # Initial upstream call
        text = await client.chat(prompt, current_context, session, tone, images)
        log_event("MODEL_RECV_RAW", {
            "status_code": 200,
            "raw": text,
            "chunks": [text]
        })
        log_raw_event("Receive", {
            "content": text
        })

        calls = self.parse_response(
            text, normalized_tools, workspace_root=workspace_root
        )

        has_error, error_category, error_tool, error_reason, feedback_message = self._diagnose_tool_protocol_error(text, calls)

        while has_error and attempt < retry_limit:
            # 1. Log the Tool Protocol Error (user_visible=False because we will retry internally)
            log_payload = {
                "event": "Tool Protocol Error",
                "category": error_category,
                "tool": error_tool,
                "user_visible": False,
                "will_retry_model": True
            }
            log_raw_event("Tool Protocol Error", log_payload)
            log_event("Tool Protocol Error", log_payload)

            # 2. Log the Tool Protocol Feedback Sent To Model
            feedback_payload = {
                "feedback": feedback_message,
                "attempt": attempt + 1,
                "user_visible": False
            }
            log_raw_event("Tool Protocol Feedback Sent To Model", feedback_payload)
            log_event("Tool Protocol Feedback Sent To Model", feedback_payload)

            # 3. Update current_context with the model's malformed/invalid turn and the feedback turn
            current_context = list(current_context)
            current_context.append(f"Assistant:\n{text}")
            current_context.append(feedback_message)

            attempt += 1

            # 4. Call the model again
            text = await client.chat(prompt, current_context, session, tone)

            # Log Tool Protocol Retry Receive
            retry_payload = {
                "content": text,
                "attempt": attempt,
                "user_visible": False
            }
            log_raw_event("Tool Protocol Retry Receive", retry_payload)
            log_event("Tool Protocol Retry Receive", retry_payload)

            # Log standard receive logs to keep other tracing happy
            log_event("MODEL_RECV_RAW", {
                "status_code": 200,
                "raw": text,
                "chunks": [text]
            })
            log_raw_event("Receive", {
                "content": text
            })

            # 5. Parse the new response
            calls = self.parse_response(
                text, normalized_tools, workspace_root=workspace_root
            )

            # 6. Diagnose again
            has_error, error_category, error_tool, error_reason, feedback_message = self._diagnose_tool_protocol_error(text, calls)

        if has_error:
            # We've exhausted the retry limit, and we still have an error.
            # Now we must expose this to the user as a final fallback.

            # 1. Log the final Tool Protocol Error with user_visible=True and will_retry_model=False
            log_payload = {
                "event": "Tool Protocol Error",
                "category": error_category,
                "tool": error_tool,
                "user_visible": True,
                "will_retry_model": False
            }
            log_raw_event("Tool Protocol Error", log_payload)
            log_event("Tool Protocol Error", log_payload)

            # 2. Log final Tool Return
            final_return_payload = {
                "format": "tool_protocol_error",
                "error": error_reason,
                "user_visible": True
            }
            log_raw_event("Tool Return", final_return_payload)
            log_event("Tool Return", final_return_payload)

            # 3. Determine final user-visible response text:
            if error_category == "unsupported_tool_call":
                visible_message = (
                    "I could not complete the tool step because the requested tool is unsupported by the current runtime."
                )
            else:
                visible_message = (
                    f"Tool protocol error: TOOL_CALLS block was detected but could not be executed.\n\n"
                    f"Reason:\n{error_reason}\n\n"
                    f"Please re-emit ONLY a corrected TOOL_CALLS block, or continue without the tools."
                )

            return None, visible_message

        # Determine whether to execute locally based on the config run_mode:
        # If execution_enabled is False, or run_mode is "platform", always bypass local execution.
        run_mode = (self.settings.tool_emulation_run_mode or "auto").strip().lower()

        if self.settings.tool_emulation_execution_enabled and run_mode != "platform":
            from emulator.tool_emulation import LocalToolEmulator
            emulator = LocalToolEmulator(self.settings)
            return await emulator.execute_react_loop(
                client,
                prompt,
                current_context,
                session,
                tone,
                normalized_tools,
                calls,
                text,
                parse_response_fn=self.parse_response,
                redact_tool_sentinels_fn=self._redact_tool_sentinels,
                workspace_root=workspace_root,
            )
        else:
            # Log Tool Return
            tool_return_payload = {
                "format": "tool_calls",
                "tool_calls": [c.model_dump() if hasattr(c, "model_dump") else str(c) for c in (calls or [])],
                "user_visible": True
            }
            log_raw_event("Tool Return", tool_return_payload)
            log_event("Tool Return", tool_return_payload)

            return calls, self._redact_tool_sentinels(text)
