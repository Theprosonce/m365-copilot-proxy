from __future__ import annotations

import json

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.models import AnthropicMessagesRequest, OpenAIChatRequest, OpenAIMessage
from m365_copilot_openai_proxy.middleware.adapters import (
    anthropic_tools_to_standard,
    openai_tools_to_standard,
    standard_tool_call_to_anthropic,
    standard_tool_call_to_openai,
)
from m365_copilot_openai_proxy.middleware.models import StandardFunctionCall, StandardToolCall
from m365_copilot_openai_proxy.middleware.pipeline import ToolMiddlewarePipeline


def test_openai_tools_round_trip_preserves_response_shape() -> None:
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}}}]
    standard = openai_tools_to_standard(tools)
    assert len(standard) == 1
    assert standard[0].function.name == "get_weather"

    call = StandardToolCall(id="call_123", function=StandardFunctionCall(name="get_weather", arguments={"location": "London"}))
    dumped = standard_tool_call_to_openai(call).model_dump()
    assert dumped["id"] == "call_123"
    assert dumped["type"] == "function"
    assert dumped["function"]["name"] == "get_weather"
    assert json.loads(dumped["function"]["arguments"]) == {"location": "London"}


def test_anthropic_tools_round_trip_preserves_tool_use_shape() -> None:
    tools = [{"name": "read_file", "description": "Read file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
    standard = anthropic_tools_to_standard(tools)
    assert len(standard) == 1
    assert standard[0].function.name == "read_file"

    block = standard_tool_call_to_anthropic(StandardToolCall(id="toolu_123", function=StandardFunctionCall(name="read_file", arguments={"path": "a.txt"})))
    assert block == {"type": "tool_use", "id": "toolu_123", "name": "read_file", "input": {"path": "a.txt"}}


def test_openai_compatible_accepts_anthropic_shaped_tool_schema() -> None:
    standard = openai_tools_to_standard([{"name": "Read", "description": "Read file contents", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}])
    assert len(standard) == 1
    assert standard[0].function.name == "Read"
    assert standard[0].function.parameters["properties"]["file_path"]["type"] == "string"


def test_minimum_openai_preflight_preserves_request_and_normalizes_tools() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    request = OpenAIChatRequest(model="m365-opus", messages=[OpenAIMessage(role="user", content="inspect files")], tools=[{"type": "function", "function": {"name": "Glob", "description": "Find files", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}}, {"name": "Read", "description": "Read file contents", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}])
    new_request, prompt, tools = pipeline.preflight_openai(request)
    assert new_request is request
    assert prompt is None
    assert [tool["function"]["name"] for tool in tools] == ["Glob", "Read"]
    assert request.tools is not None


def test_minimum_anthropic_preflight_translates_tools_to_openai_shape() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    request = AnthropicMessagesRequest(model="m365-opus", messages=[], tools=[{"name": "read_file", "description": "Read file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}])
    proxy_request, prompt, tools = pipeline.preflight_anthropic(request)
    assert prompt is None
    assert proxy_request.tools == tools
    assert proxy_request.tools[0]["type"] == "function"
    assert proxy_request.tools[0]["function"]["name"] == "read_file"


def test_pipeline_never_reports_active_internal_execution() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    openai_request = OpenAIChatRequest(model="m365-opus", messages=[OpenAIMessage(role="user", content="hi")], tools=[{"type": "function", "function": {"name": "test_tool"}}])
    anthropic_request = AnthropicMessagesRequest(model="m365-opus", messages=[], tools=[{"name": "test_tool", "input_schema": {"type": "object"}}])
    assert pipeline.is_openai_active(openai_request) is False
    assert pipeline.is_anthropic_active(anthropic_request) is False
    assert pipeline.force_non_streaming is False


def test_openai_to_anthropic_tool_translation() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    request = OpenAIChatRequest(model="m365-opus", messages=[OpenAIMessage(role="user", content="hi")], tools=[{"type": "function", "function": {"name": "search", "description": "Search things", "parameters": {"type": "object"}}}])
    assert pipeline.anthropic_tools_from_openai(request) == [{"name": "search", "description": "Search things", "input_schema": {"type": "object"}}]


def test_received_tool_call_block_becomes_openai_tool_calls() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text('EXT_TOOL: [{"id":"call_fixed","name":"Read","arguments":{"file_path":"README.md"}}] :END_EXT_TOOL')

    assert text == ""
    assert calls is not None
    assert len(calls) == 1
    dumped = calls[0].model_dump()
    assert dumped["id"] == "call_fixed"
    assert dumped["type"] == "function"
    assert dumped["function"]["name"] == "Read"
    assert json.loads(dumped["function"]["arguments"]) == {"file_path": "README.md"}


def test_surrounding_text_means_no_tool_calls() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text('EXT_TOOL: [{"id":"toolu_fixed","name":"Glob","arguments":{"pattern":"**/*"}}]\nI will inspect files.')

    assert calls is None
    assert text == 'EXT_TOOL: [{"id":"toolu_fixed","name":"Glob","arguments":{"pattern":"**/*"}}]\nI will inspect files.'


def test_surrounding_text_still_extracts_tool_calls() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text(
        'Let me check.\n'
        'EXT_TOOL: [{"id":"call_1","name":"Read","arguments":{"file_path":"a.txt"}}, {"id":"call_2","name":"Write","arguments":{"file_path":"b.txt","content":"hello"}}] :END_EXT_TOOL\n'
        'Done.'
    )

    assert text == ""
    assert calls is not None
    assert len(calls) == 2
    assert calls[0].id == "call_1"
    assert calls[0].function.name == "Read"
    assert calls[1].id == "call_2"
    assert calls[1].function.name == "Write"


def test_ext_tool_with_surrounding_text_passthrough() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text(
        'Let me check that.\n'
        'EXT_TOOL: [{"id":"call_1","name":"Glob","arguments":{"pattern":"**/*.py"}}]\n'
        'EXT_TOOL: [{"id":"call_2","name":"Read","arguments":{"file_path":"main.py"}}]\n'
        'Done.'
    )

    assert calls is None
    assert text == 'Let me check that.\nEXT_TOOL: [{"id":"call_1","name":"Glob","arguments":{"pattern":"**/*.py"}}]\nEXT_TOOL: [{"id":"call_2","name":"Read","arguments":{"file_path":"main.py"}}]\nDone.'


def test_multiple_tools_in_single_ext_tool_block() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text(
        'EXT_TOOL: [{"id":"call_1","name":"Read","arguments":{"file_path":"a.txt"}}, {"id":"call_2","name":"Write","arguments":{"file_path":"b.txt","content":"hello"}}] :END_EXT_TOOL'
    )

    assert text == ""
    assert calls is not None
    assert len(calls) == 2
    assert calls[0].id == "call_1"
    assert calls[0].function.name == "Read"
    assert calls[1].id == "call_2"
    assert calls[1].function.name == "Write"


def test_malformed_ext_tool_block_rejects_entire_response() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text(
        'EXT_TOOL: not valid json\n'
        'EXT_TOOL: [{"id":"call_1","name":"Read","arguments":{"file_path":"a.txt"}}] :END_EXT_TOOL'
    )

    assert calls is None
    assert text == 'EXT_TOOL: not valid json\nEXT_TOOL: [{"id":"call_1","name":"Read","arguments":{"file_path":"a.txt"}}] :END_EXT_TOOL'


def test_non_tool_call_text_passes_through() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text("plain answer")

    assert calls is None
    assert text == "plain answer"


def test_trailing_quote_after_suffix() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text(
        'EXT_TOOL: [{"id":"call_1","name":"Read","arguments":{"file_path":"test.md"}}] :END_EXT_TOOL"'
    )

    assert calls is not None
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].function.name == "Read"
    assert text == ""


def test_cmpr_line_after_suffix() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    calls, text = pipeline.tool_calls_from_text(
        'EXT_TOOL: [{"id":"call_1","name":"Read","arguments":{"file_path":"test.md"}}] :END_EXT_TOOL\n-cmpr'
    )

    assert calls is not None
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].function.name == "Read"
    assert text == ""
