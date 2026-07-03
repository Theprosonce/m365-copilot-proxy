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

_TOOL_CALL_BEGIN = "<<<TOOL_CALLS>>>"
_TOOL_CALL_END = "<<<END_TOOL_CALLS>>>"


class ToolMiddlewarePipeline:
    """Minimum protocol-neutral tool translator.

    This build does not modify prompts or execute tools internally. It only
    translates client tool definitions and converts received TOOL_CALL blocks
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
        """Convert a received TOOL_CALL block into OpenAI-compatible tool calls."""
        payload, trailing_text = self._extract_tool_call_payload(text)
        if payload is None:
            return None, text

        raw_calls = self._loads_tool_calls(payload)
        if raw_calls is None:
            return None, text

        calls: list[ToolCall] = []
        for item in raw_calls:
            call = self._tool_call_from_item(item)
            if call is not None:
                calls.append(call)
        return (calls or None), trailing_text

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

    def _extract_tool_call_payload(self, text: str) -> tuple[str | None, str]:
        if not text or not text.lstrip().startswith(_TOOL_CALL_BEGIN):
            return None, text
        start = text.find(_TOOL_CALL_BEGIN)
        end = text.find(_TOOL_CALL_END, start + len(_TOOL_CALL_BEGIN))
        if end < 0:
            return None, text
        payload = text[start + len(_TOOL_CALL_BEGIN) : end].strip()
        trailing = text[end + len(_TOOL_CALL_END) :].strip()
        return payload, trailing

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
