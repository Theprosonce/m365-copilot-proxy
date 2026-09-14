from __future__ import annotations

import json
import uuid
from typing import Any

from copilot_proxy_server.models import AnthropicMessagesRequest, OpenAIChatRequest, ToolCall
from .adapters import (
    anthropic_tools_to_standard,
    openai_functions_to_standard,
    openai_tools_to_standard,
    standard_tool_call_to_openai,
    standard_tools_to_anthropic,
    standard_tools_to_openai,
)
from .models import StandardFunctionCall, StandardToolCall

_TOOL_CALL_PREFIX = "EXT_TOOL:"
_TOOL_CALL_SUFFIX = ":END_EXT_TOOL"


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

        Finds the first `EXT_TOOL:` marker and the last `:END_EXT_TOOL`
        marker, then parses the JSON between them.
        """
        start = text.find(_TOOL_CALL_PREFIX)
        if start == -1:
            return None, text

        end = text.rfind(_TOOL_CALL_SUFFIX)
        if end == -1:
            return None, text

        inner = text[start + len(_TOOL_CALL_PREFIX):end].strip()
        try:
            items = self._loads_tool_calls(inner)
            calls = [self._tool_call_from_item(item) for item in items]
        except ValueError as exc:
            return None, f"EXT_TOOL_FAILURE: {exc}. Fix the payload and resend the complete EXT_TOOL block."

        return calls, ""

    async def tool_calls_with_failure_retry(
        self,
        text: str,
        client: Any,
        session: Any = None,
        tone: str = "Magic",
        images: Any = None,
    ) -> tuple[list[ToolCall] | None, str]:
        calls, text = self.tool_calls_from_text(text)
        if text.startswith("EXT_TOOL_FAILURE:"):
            text = await client.chat(text, [], session, tone, images)
            calls, text = self.tool_calls_from_text(text)
        return calls, text

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

    def _loads_tool_calls(self, payload: str) -> list[dict[str, Any]]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            start = max(0, exc.pos - 3)
            end = min(len(payload), exc.pos + 4)
            excerpt = payload[start:end].replace("\n", "\\n")
            if start > 0:
                excerpt = f"...{excerpt}"
            if end < len(payload):
                excerpt = f"{excerpt}..."
            raise ValueError(
                f"JSON parse failed at character {exc.pos} near {excerpt!r}: {exc.msg}"
            ) from exc
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("tool payload must be a JSON array or object")
        if not data:
            raise ValueError("tool payload contains no calls")
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"tool call at index {index} must be a JSON object")
        return data

    def _tool_call_from_item(self, item: dict[str, Any]) -> ToolCall:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool call name must be a non-empty string")
        arguments = item.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                start = max(0, exc.pos - 3)
                end = min(len(arguments), exc.pos + 4)
                excerpt = arguments[start:end].replace("\n", "\\n")
                if start > 0:
                    excerpt = f"...{excerpt}"
                if end < len(arguments):
                    excerpt = f"{excerpt}..."
                raise ValueError(
                    f"arguments for tool {name!r} failed JSON parsing at character {exc.pos} near {excerpt!r}: {exc.msg}"
                ) from exc
        if not isinstance(arguments, dict):
            raise ValueError(f"arguments for tool {name!r} must be a JSON object")
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
