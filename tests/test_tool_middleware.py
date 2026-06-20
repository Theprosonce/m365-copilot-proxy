from __future__ import annotations

import pytest
import json

from middleware.tool_emulation import (
    ToolEmulationPipeline,
)
from m365_copilot_openai_proxy.models import OpenAIChatRequest, OpenAIMessage
from m365_copilot_openai_proxy.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        M365_ACCESS_TOKEN="fake",
        M365_TOOL_EMULATION_ENABLED=True,
        M365_TOOL_EMULATION_NATIVE_PASSTHROUGH=True,
    )


def test_fast_passthrough_no_tools(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    req = OpenAIChatRequest(
        model="m365-opus", messages=[OpenAIMessage(role="user", content="hi")]
    )
    assert pipeline.is_emulation_active(req) is False


def test_emulation_active_with_tools(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    req = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test", "description": "d"}}],
    )
    assert pipeline.is_emulation_active(req) is True


def test_emulation_preflight_strips_native_fields(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    req = OpenAIChatRequest(
        model="m365-opus",
        stream=True,
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
    )
    new_req, prompt, tools = pipeline.preflight(req)
    assert new_req.tools is None
    assert new_req.tool_choice is None
    assert len(tools) == 1
    assert tools[0]["name"] == "test_tool"
    assert "test_tool" in prompt


def test_tool_choice_none_skips_emulation(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    req = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "test_tool"}}],
        tool_choice="none",
    )
    new_req, prompt, tools = pipeline.preflight(req)
    # With tool_choice="none", it shouldn't inject tools into the prompt
    assert prompt in ("", None)


def test_parse_valid_tool_call(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    tools = [
        {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        }
    ]

    text = '<<<TOOL_CALLS>>>\n[{"name": "get_weather", "arguments": {"location": "London"}}]\n<<<END_TOOL_CALLS>>>\n'
    calls = pipeline.parse_response(text, tools)

    assert calls is not None
    assert len(calls) == 1
    assert calls[0].function.name == "get_weather"
    args = json.loads(calls[0].function.arguments)
    assert args["location"] == "London"


def test_parse_ignores_invalid_json(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    tools = [{"name": "get_weather"}]

    text = "<<<TOOL_CALLS>>>\nnot json\n<<<END_TOOL_CALLS>>>\n"
    calls = pipeline.parse_response(text, tools)
    assert calls is None


def test_tool_reducer_ranks_by_relevance(settings: Settings) -> None:
    settings.tool_emulation_max_tools_in_prompt = 2
    pipeline = ToolEmulationPipeline(settings)
    tools = [
        {
            "type": "function",
            "function": {"name": "irrelevant_1", "description": "foo"},
        },
        {
            "type": "function",
            "function": {
                "name": "relevant_tool",
                "description": "This gets the current weather",
            },
        },
        {
            "type": "function",
            "function": {"name": "irrelevant_2", "description": "bar"},
        },
        {
            "type": "function",
            "function": {
                "name": "another_relevant",
                "description": "Checks weather forecast",
            },
        },
    ]
    req = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="What is the weather forecast?")],
        tools=tools,
    )
    new_req, prompt, norm_tools = pipeline.preflight(req)

    # Check that 'relevant_tool' and 'another_relevant' are the only ones kept in the prompt
    assert "relevant_tool" in prompt
    assert "another_relevant" in prompt
    assert "irrelevant_1" not in prompt
    assert "irrelevant_2" not in prompt


def test_tool_rejection_missing_args(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    tools = [
        {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        }
    ]

    text = '<<<TOOL_CALLS>>>\n[{"name": "get_weather", "arguments": {}}]\n<<<END_TOOL_CALLS>>>\n'
    calls = pipeline.parse_response(text, tools)
    assert calls is None  # should be rejected because 'location' is missing


def test_tool_rejection_invalid_type(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    tools = [
        {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        }
    ]

    text = '<<<TOOL_CALLS>>>\n[{"name": "get_weather", "arguments": {"location": 123}}]\n<<<END_TOOL_CALLS>>>\n'
    calls = pipeline.parse_response(text, tools)
    assert calls is None  # should be rejected because 'location' is an int, not string


def test_tool_rejection_unknown_tool(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    tools = [{"name": "get_weather"}]

    text = '<<<TOOL_CALLS>>>\n[{"name": "unknown_tool", "arguments": {}}]\n<<<END_TOOL_CALLS>>>\n'
    calls = pipeline.parse_response(text, tools)
    assert calls is None  # should be rejected because 'unknown_tool' doesn't exist


def test_plain_json_parsing(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    tools = [{"name": "get_weather"}]

    text = '   [{"name": "get_weather", "arguments": {}}]   '
    calls = pipeline.parse_response(text, tools)
    assert calls is not None
    assert calls[0].function.name == "get_weather"


def test_tool_rejection_invalid_enum(settings: Settings) -> None:
    pipeline = ToolEmulationPipeline(settings)
    tools = [
        {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"unit": {"type": "string", "enum": ["C", "F"]}},
            },
        }
    ]

    text = '<<<TOOL_CALLS>>>\n[{"name": "get_weather", "arguments": {"unit": "K"}}]\n<<<END_TOOL_CALLS>>>\n'
    calls = pipeline.parse_response(text, tools)
    assert calls is None  # should be rejected because 'K' is not in ['C', 'F']


def test_translator_separates_tool_results_from_transcript() -> None:
    """Tool results must be in a separate context block, not buried in transcript."""
    from m365_copilot_openai_proxy.translator import translate_openai_request
    from m365_copilot_openai_proxy.models import OpenAIChatRequest, OpenAIMessage

    request = OpenAIChatRequest(
        model="test",
        messages=[
            OpenAIMessage(role="user", content="Read file.txt"),
            OpenAIMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"filePath": "file.txt"}',
                        },
                    }
                ],
            ),
            OpenAIMessage(
                role="tool",
                tool_call_id="call_123",
                content="<content>Hello, world!</content>",
            ),
        ],
    )

    translated = translate_openai_request(request)

    # Tool results must be in a separate block
    tool_results_block = None
    for ctx in translated.additional_context:
        if ctx.startswith("Tool results:"):
            tool_results_block = ctx
            break

    assert tool_results_block is not None, "Tool results block must exist"
    assert "Hello, world!" in tool_results_block, (
        "Tool result content must be preserved"
    )
    assert "Tool result [call_123]" in tool_results_block, (
        "Tool call ID must be referenced"
    )


def test_translator_anthropic_separates_tool_results() -> None:
    """Anthropic tool_result blocks must be in a separate context block."""
    from m365_copilot_openai_proxy.translator import translate_anthropic_request
    from m365_copilot_openai_proxy.models import (
        AnthropicMessagesRequest,
        AnthropicMessage,
        ContentPart,
    )

    request = AnthropicMessagesRequest(
        model="test",
        messages=[
            AnthropicMessage(
                role="user",
                content=[ContentPart(type="text", text="Read file.txt")],
            ),
            AnthropicMessage(
                role="assistant",
                content=[
                    ContentPart(
                        type="tool_use",
                        id="toolu_123",
                        name="read",
                        input={"filePath": "file.txt"},
                    )
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    ContentPart(
                        type="tool_result",
                        tool_use_id="toolu_123",
                        content="<content>Hello, world!</content>",
                    )
                ],
            ),
        ],
    )

    translated = translate_anthropic_request(request)

    # Tool results must be in a separate block
    tool_results_block = None
    for ctx in translated.additional_context:
        if ctx.startswith("Tool results:"):
            tool_results_block = ctx
            break

    assert tool_results_block is not None, "Tool results block must exist"
    assert "Hello, world!" in tool_results_block, (
        "Tool result content must be preserved"
    )
    assert "Tool result [toolu_123]" in tool_results_block, (
        "Tool use ID must be referenced"
    )
