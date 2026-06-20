from __future__ import annotations

import json

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.models import AnthropicMessagesRequest, OpenAIChatRequest, OpenAIMessage
from middleware.adapters import (
    anthropic_tools_to_standard,
    openai_tools_to_standard,
    standard_tool_call_to_anthropic,
    standard_tool_call_to_openai,
)
from middleware.models import StandardFunctionCall, StandardToolCall
from middleware.pipeline import ToolMiddlewarePipeline


def test_openai_tools_round_trip_preserves_response_shape() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            },
        }
    ]
    standard = openai_tools_to_standard(tools)
    assert len(standard) == 1
    assert standard[0].function.name == "get_weather"

    call = StandardToolCall(
        id="call_123",
        function=StandardFunctionCall(
            name="get_weather",
            arguments={"location": "London"},
        ),
    )
    openai_call = standard_tool_call_to_openai(call)
    dumped = openai_call.model_dump()
    assert dumped["id"] == "call_123"
    assert dumped["type"] == "function"
    assert dumped["function"]["name"] == "get_weather"
    assert json.loads(dumped["function"]["arguments"]) == {"location": "London"}


def test_anthropic_tools_round_trip_preserves_tool_use_shape() -> None:
    tools = [
        {
            "name": "read_file",
            "description": "Read file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    standard = anthropic_tools_to_standard(tools)
    assert len(standard) == 1
    assert standard[0].function.name == "read_file"

    block = standard_tool_call_to_anthropic(
        StandardToolCall(
            id="toolu_123",
            function=StandardFunctionCall(name="read_file", arguments={"path": "a.txt"}),
        )
    )
    assert block == {
        "type": "tool_use",
        "id": "toolu_123",
        "name": "read_file",
        "input": {"path": "a.txt"},
    }


def test_middleware_openai_tool_choice_none_preserves_no_prompt_injection() -> None:
    pipeline = ToolMiddlewarePipeline(
        Settings(M365_ACCESS_TOKEN="fake", M365_TOOL_EMULATION_ENABLED=True)
    )
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
        tool_choice="none",
    )

    new_request, prompt, tools = pipeline.preflight_openai(request)

    assert prompt in ("", None)
    assert new_request.tools is None
    assert new_request.tool_choice is None
    assert tools[0]["name"] == "test_tool"


def test_middleware_anthropic_uses_protocol_neutral_adapter() -> None:
    pipeline = ToolMiddlewarePipeline(
        Settings(M365_ACCESS_TOKEN="fake", M365_TOOL_EMULATION_ENABLED=True)
    )
    request = AnthropicMessagesRequest(
        model="m365-opus",
        messages=[],
        tools=[
            {
                "name": "read_file",
                "description": "Read file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
    )

    proxy_request, prompt, tools = pipeline.preflight_anthropic(request)

    assert proxy_request.tools is None
    assert len(tools) == 1
    assert tools[0]["name"] == "read_file"
    assert "read_file" in (prompt or "")



def test_middleware_defaults_keep_tools_enabled() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(M365_ACCESS_TOKEN="fake"))
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
    )

    assert pipeline.is_openai_active(request) is True
    new_request, prompt, tools = pipeline.preflight_openai(request)
    assert new_request.tools is None
    assert tools[0]["name"] == "test_tool"
    assert "test_tool" in (prompt or "")


def test_middleware_can_be_disabled_without_disabling_legacy_config() -> None:
    pipeline = ToolMiddlewarePipeline(
        Settings(
            M365_ACCESS_TOKEN="fake",
            M365_TOOL_MIDDLEWARE_ENABLED=False,
            M365_TOOL_EMULATION_ENABLED=True,
        )
    )
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
    )

    new_request, prompt, tools = pipeline.preflight_openai(request)

    assert pipeline.is_openai_active(request) is False
    assert new_request.tools == request.tools
    assert prompt is None
    assert tools[0]["name"] == "test_tool"


def test_middleware_mode_off_disables_facade() -> None:
    pipeline = ToolMiddlewarePipeline(
        Settings(M365_ACCESS_TOKEN="fake", M365_TOOL_MIDDLEWARE_MODE="off")
    )
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
    )

    assert pipeline.is_openai_active(request) is False
    new_request, prompt, _tools = pipeline.preflight_openai(request)
    assert new_request.tools == request.tools
    assert prompt is None


class _ExplodingEmulation:
    settings = Settings(M365_ACCESS_TOKEN="fake")

    def is_emulation_active(self, request):  # pragma: no cover - must not be called
        raise AssertionError("native mode must not call emulation active check")

    def preflight(self, request):  # pragma: no cover - must not be called
        raise AssertionError("native mode must not call emulation preflight")

    def _normalize_tools(self, request):
        tools = []
        for tool in request.tools or []:
            tools.append(tool.get("function", tool))
        for fn in request.functions or []:
            tools.append(fn)
        return tools


class _NoopBackend:
    def __init__(self):
        self.contexts = []

    def can_execute(self, context):
        self.contexts.append(context)
        return False


class _CapableBackend:
    def __init__(self):
        self.contexts = []

    def can_execute(self, context):
        self.contexts.append(context)
        return True


async def _unused_execute(*_args, **_kwargs):  # pragma: no cover - test fixture only
    raise AssertionError("not used")


def test_native_mode_does_not_delegate_to_emulation() -> None:
    backend = _NoopBackend()
    pipeline = ToolMiddlewarePipeline(
        Settings(M365_ACCESS_TOKEN="fake", M365_TOOL_MIDDLEWARE_MODE="native"),
        native_backend=backend,
        emulation_backend=_ExplodingEmulation(),
    )
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
    )

    assert pipeline.is_openai_active(request) is False
    new_request, prompt, tools = pipeline.preflight_openai(request)

    assert new_request is request
    assert prompt is None
    assert tools[0]["name"] == "test_tool"
    assert backend.contexts[0].protocol == "openai"


def test_auto_mode_falls_back_to_emulation_when_native_unavailable() -> None:
    backend = _NoopBackend()
    pipeline = ToolMiddlewarePipeline(
        Settings(M365_ACCESS_TOKEN="fake", M365_TOOL_MIDDLEWARE_MODE="auto"),
        native_backend=backend,
    )
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
    )

    assert pipeline.is_openai_active(request) is True
    new_request, prompt, tools = pipeline.preflight_openai(request)

    assert new_request.tools is None
    assert tools[0]["name"] == "test_tool"
    assert "test_tool" in (prompt or "")
    assert backend.contexts[0].protocol == "openai"


def test_auto_mode_prefers_native_when_backend_can_execute() -> None:
    backend = _CapableBackend()
    pipeline = ToolMiddlewarePipeline(
        Settings(M365_ACCESS_TOKEN="fake", M365_TOOL_MIDDLEWARE_MODE="auto"),
        native_backend=backend,
        emulation_backend=_ExplodingEmulation(),
    )
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
    )

    assert pipeline.is_openai_active(request) is True
    new_request, prompt, tools = pipeline.preflight_openai(request)

    assert new_request is request
    assert prompt is None
    assert tools[0]["name"] == "test_tool"
    assert backend.contexts[0].tools[0].function.name == "test_tool"


def test_standard_tool_results_preserve_structured_content() -> None:
    from middleware.adapters import (
        anthropic_tool_result_to_standard,
        openai_tool_result_to_standard,
    )

    payload = {"items": [{"name": "alpha"}], "ok": True}
    openai_result = openai_tool_result_to_standard("call_123", payload)
    anthropic_result = anthropic_tool_result_to_standard("toolu_123", [payload])

    assert openai_result.tool_call_id == "call_123"
    assert openai_result.content == payload
    assert anthropic_result.tool_call_id == "toolu_123"
    assert anthropic_result.content == [payload]
