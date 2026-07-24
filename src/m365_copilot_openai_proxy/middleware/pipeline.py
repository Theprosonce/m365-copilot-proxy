from __future__ import annotations

import json
import uuid
from typing import Any

from m365_copilot_openai_proxy.models import AnthropicMessagesRequest, OpenAIChatRequest, ToolCall
from .adapters import (
    anthropic_tools_to_standard,
    openai_functions_to_standard,
    openai_tools_to_standard,
    standard_tool_call_to_openai,
    standard_tools_to_anthropic,
    standard_tools_to_openai,
)
from .models import StandardFunctionCall, StandardToolCall

_TOOL_CALL_PREFIX = "EXT_TOOL: "
_TOOL_CALL_SUFFIX = " :END_EXT_TOOL"


class ToolMiddlewarePipeline:
    """Minimum protocol-neutral tool translator.

    This build does not modify prompts or execute tools internally. It only
    translates client tool definitions and converts received EXT_TOOL blocks
    into real OpenAI/Anthropic-compatible message shapes.
    """

    def __init__(self, _settings: object | None = None):
        pass


    @property
    def force_non_streaming(self) -> bool:
        return False

    def is_openai_active(self, request: OpenAIChatRequest) -> bool:
        return False

    def preflight_openai(
        self, request: OpenAIChatRequest
    ) -> tuple[OpenAIChatRequest, str | None, list[dict[str, Any]]]:
        normalized_tools = self._normalize_openai_tools(request)
        return request, None, normalized_tools

    def openai_proxy_request_from_anthropic(
        self, request: AnthropicMessagesRequest
    ) -> OpenAIChatRequest:
        standard_tools = anthropic_tools_to_standard(request.tools)
        return OpenAIChatRequest(
            model=request.model,
            messages=[],
            tools=standard_tools_to_openai(standard_tools) or None,
            tool_choice=request.tool_choice,
        )

    def anthropic_tools_from_openai(
        self, request: OpenAIChatRequest
    ) -> list[dict[str, Any]]:
        standard_tools = [
            *openai_tools_to_standard(request.tools),
            *openai_functions_to_standard(request.functions),
        ]
        return standard_tools_to_anthropic(standard_tools)

    def is_anthropic_active(self, request: AnthropicMessagesRequest) -> bool:
        return False

    def preflight_anthropic(
        self, request: AnthropicMessagesRequest
    ) -> tuple[OpenAIChatRequest, str | None, list[dict[str, Any]]]:
        proxy_request = self.openai_proxy_request_from_anthropic(request)
        normalized_tools = self._normalize_openai_tools(proxy_request)
        return proxy_request, None, normalized_tools

    def _normalize_openai_tools(self, request: OpenAIChatRequest) -> list[dict[str, Any]]:
        return standard_tools_to_openai(
            [
                *openai_tools_to_standard(request.tools),
                *openai_functions_to_standard(request.functions),
            ]
        )

    def tool_calls_from_text(self, text: str) -> tuple[list[ToolCall] | None, str]:
        """Convert received EXT_TOOL blocks into OpenAI-compatible tool calls.

        Only captures when the ENTIRE response consists of EXT_TOOL blocks:
        each line must start with `EXT_TOOL: ` and end with ` :END_EXT_TOOL`.
        Any surrounding text means this is not a tool-call response and the
        text is returned unchanged.
        """
        stripped = text.strip()
        if not stripped:
            return None, text

        lines = stripped.splitlines()
        raw_calls: list[dict[str, Any]] = []

        for line in lines:
            if not line.strip():
                return None, text
            stripped_line = line.strip()
            if not stripped_line.startswith(_TOOL_CALL_PREFIX):
                return None, text
            if not stripped_line.endswith(_TOOL_CALL_SUFFIX):
                return None, text
            inner = stripped_line[len(_TOOL_CALL_PREFIX):-len(_TOOL_CALL_SUFFIX)].strip()
            items = self._loads_tool_calls(inner)
            if items is not None:
                raw_calls.extend(items)

        if not raw_calls:
            return None, text

        calls: list[ToolCall] = []
        for item in raw_calls:
            call = self._tool_call_from_item(item)
            if call is not None:
                calls.append(call)

        return (calls or None), ""

    def anthropic_content_from_tool_calls(
        self, calls: list[ToolCall] | None, text: str
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if text and text.strip():
            content.append({"type": "text", "text": text})
        for call in calls or []:
            content.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.function.name,
                    "input": self._args_obj(call.function.arguments),
                }
            )
        return content

    def _loads_tool_calls(self, payload: str) -> list[dict[str, Any]] | None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)]

    def _tool_call_from_item(self, item: dict[str, Any]) -> ToolCall | None:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        arguments = item.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_{uuid.uuid4().hex[:24]}"
        return standard_tool_call_to_openai(
            StandardToolCall(
                id=call_id,
                function=StandardFunctionCall(name=name, arguments=arguments),
            )
        )

    def _args_obj(self, arguments: str) -> dict[str, Any]:
        try:
            obj = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return obj if isinstance(obj, dict) else {}
