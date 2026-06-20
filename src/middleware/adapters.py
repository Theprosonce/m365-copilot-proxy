from __future__ import annotations

import json
from typing import Any

from m365_copilot_openai_proxy.models import FunctionCall, ToolCall
from .models import (
    StandardFunctionCall,
    StandardToolCall,
    StandardToolDefinition,
    StandardToolFunction,
    StandardToolResult,
)


def _function_from_any(tool: dict[str, Any]) -> dict[str, Any] | None:
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        return tool["function"]
    if "name" in tool:
        return tool
    return None


def openai_tools_to_standard(tools: list[dict[str, Any]] | None) -> list[StandardToolDefinition]:
    """Normalize OpenAI chat/completions tool definitions."""

    normalized: list[StandardToolDefinition] = []
    for tool in tools or []:
        fn = _function_from_any(tool)
        if not fn:
            continue
        normalized.append(
            StandardToolDefinition(
                function=StandardToolFunction(
                    name=str(fn.get("name", "")),
                    description=str(fn.get("description") or ""),
                    parameters=dict(fn.get("parameters") or {}),
                )
            )
        )
    return normalized


def openai_functions_to_standard(functions: list[dict[str, Any]] | None) -> list[StandardToolDefinition]:
    """Normalize legacy OpenAI `functions` definitions."""

    return openai_tools_to_standard(functions)


def anthropic_tools_to_standard(tools: list[dict[str, Any]] | None) -> list[StandardToolDefinition]:
    """Normalize Anthropic message tool definitions."""

    normalized: list[StandardToolDefinition] = []
    for tool in tools or []:
        name = str(tool.get("name", ""))
        if not name:
            continue
        parameters = tool.get("input_schema") or tool.get("parameters") or {}
        normalized.append(
            StandardToolDefinition(
                function=StandardToolFunction(
                    name=name,
                    description=str(tool.get("description") or ""),
                    parameters=dict(parameters),
                )
            )
        )
    return normalized


def standard_tools_to_openai(tools: list[StandardToolDefinition]) -> list[dict[str, Any]]:
    """Convert internal tools back to OpenAI-compatible function tools."""

    return [
        {
            "type": "function",
            "function": {
                "name": tool.function.name,
                "description": tool.function.description,
                "parameters": tool.function.parameters,
            },
        }
        for tool in tools
    ]


def standard_tools_to_anthropic(tools: list[StandardToolDefinition]) -> list[dict[str, Any]]:
    """Convert internal tools back to Anthropic-compatible tools."""

    return [
        {
            "name": tool.function.name,
            "description": tool.function.description,
            "input_schema": tool.function.parameters,
        }
        for tool in tools
    ]


def openai_tool_call_to_standard(call: ToolCall | dict[str, Any]) -> StandardToolCall:
    """Normalize an OpenAI tool call."""

    if hasattr(call, "model_dump"):
        call_dict = call.model_dump()
    else:
        call_dict = dict(call)
    function = call_dict.get("function") or {}
    raw_args = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return StandardToolCall(
        id=str(call_dict.get("id", "")),
        function=StandardFunctionCall(
            name=str(function.get("name", "")),
            arguments=arguments,
        ),
    )


def standard_tool_call_to_openai(call: StandardToolCall) -> ToolCall:
    """Convert an internal tool call to the existing OpenAI response model."""

    return ToolCall(
        id=call.id,
        type="function",
        function=FunctionCall(
            name=call.function.name,
            arguments=json.dumps(call.function.arguments, ensure_ascii=False),
        ),
    )


def standard_tool_call_to_anthropic(call: StandardToolCall) -> dict[str, Any]:
    """Convert an internal tool call to an Anthropic tool_use content block."""

    return {
        "type": "tool_use",
        "id": call.id,
        "name": call.function.name,
        "input": call.function.arguments,
    }


def openai_tool_result_to_standard(tool_call_id: str, content: Any) -> StandardToolResult:
    return StandardToolResult(tool_call_id=tool_call_id, content=content)


def anthropic_tool_result_to_standard(tool_use_id: str, content: Any) -> StandardToolResult:
    return StandardToolResult(tool_call_id=tool_use_id, content=content)
