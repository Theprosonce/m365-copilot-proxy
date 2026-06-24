from __future__ import annotations

import json
from pathlib import Path

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


def test_openai_compatible_accepts_anthropic_shaped_tool_schema() -> None:
    tools = [
        {
            "name": "Read",
            "description": "Read file contents",
            "input_schema": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        }
    ]

    standard = openai_tools_to_standard(tools)

    assert len(standard) == 1
    assert standard[0].function.name == "Read"
    assert standard[0].function.parameters["properties"]["file_path"]["type"] == "string"


def test_prompt_renders_mixed_openai_and_anthropic_compatible_tools() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="inspect files")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "Glob",
                    "description": "Find files",
                    "parameters": {
                        "type": "object",
                        "properties": {"pattern": {"type": "string"}},
                        "required": ["pattern"],
                    },
                },
            },
            {
                "name": "Read",
                "description": "Read file contents",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
            },
        ],
    )

    new_request, prompt, tools = pipeline.preflight_openai(request)

    assert new_request.tools is None
    assert [tool["name"] for tool in tools] == ["Glob", "Read"]
    assert "Glob(pattern:string)" in (prompt or "")
    assert "Read(file_path:string)" in (prompt or "")


def test_prompt_lists_all_non_excluded_tools() -> None:
    pipeline = ToolMiddlewarePipeline(
        Settings(
            access_token="fake",
            tool_emulation_exclude_tools="SkipMe",
            tool_emulation_max_tools_in_prompt=1,
        )
    )
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="use tools")],
        tools=[
            {"name": "KeepOne", "description": "First tool", "input_schema": {"type": "object"}},
            {"name": "SkipMe", "description": "Excluded tool", "input_schema": {"type": "object"}},
            {"name": "KeepTwo", "description": "Second tool", "input_schema": {"type": "object"}},
        ],
    )

    _new_request, prompt, tools = pipeline.preflight_openai(request)

    assert [tool["name"] for tool in tools] == ["KeepOne", "SkipMe", "KeepTwo"]
    assert "KeepOne(" in (prompt or "")
    assert "KeepTwo(" in (prompt or "")
    assert "SkipMe(" not in (prompt or "")


def test_prompt_forced_tool_only_lists_selected_tool() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    request = OpenAIChatRequest(
        model="m365-opus",
        messages=[OpenAIMessage(role="user", content="read file")],
        tools=[
            {
                "name": "Read",
                "description": "Read file contents",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
            },
            {
                "name": "Write",
                "description": "Write file contents",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        ],
        tool_choice={"type": "function", "function": {"name": "Read"}},
    )

    _new_request, prompt, tools = pipeline.preflight_openai(request)

    assert [tool["name"] for tool in tools] == ["Read", "Write"]
    assert "Read(file_path:string)" in (prompt or "")
    assert "Write(path:string, content:string)" not in (prompt or "")


def test_middleware_openai_tool_choice_none_preserves_no_prompt_injection() -> None:
    pipeline = ToolMiddlewarePipeline(
        Settings(access_token="fake", tool_emulation_enabled=True)
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
        Settings(access_token="fake", tool_emulation_enabled=True)
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


def test_anthropic_prompt_preserves_actual_tool_metadata() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
    request = AnthropicMessagesRequest(
        model="m365-opus",
        messages=[],
        tools=[
            {
                "name": "Glob",
                "description": "Find files by glob pattern",
                "input_schema": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                    "required": ["pattern"],
                },
            },
            {
                "name": "Read",
                "description": "Read file contents",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
            },
        ],
    )

    _proxy_request, prompt, tools = pipeline.preflight_anthropic(request)

    assert [tool["name"] for tool in tools] == ["Glob", "Read"]
    assert "Glob(input_schema=" in (prompt or "")
    assert "Find files by glob pattern" in (prompt or "")
    assert "Read(input_schema=" in (prompt or "")
    assert "Read file contents" in (prompt or "")
    assert '"file_path": {"type": "string"}' in (prompt or "")



def test_middleware_defaults_keep_tools_enabled() -> None:
    pipeline = ToolMiddlewarePipeline(Settings(access_token="fake"))
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
            access_token="fake",
            M365_TOOL_MIDDLEWARE_ENABLED=False,
            tool_emulation_enabled=True,
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
        Settings(access_token="fake", M365_TOOL_MIDDLEWARE_MODE="off")
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
    settings = Settings(access_token="fake")

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
        Settings(access_token="fake", M365_TOOL_MIDDLEWARE_MODE="native"),
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
        Settings(access_token="fake", M365_TOOL_MIDDLEWARE_MODE="auto"),
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
        Settings(access_token="fake", M365_TOOL_MIDDLEWARE_MODE="auto"),
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


def test_tool_emulation_injection_success(tmp_path, monkeypatch) -> None:
    import sys
    import importlib
    
    injection_file = tmp_path / "tool_emulation_injection.md"
    injection_file.write_text("Hello World from Injection", encoding="utf-8")
    
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        f"[settings]\ntool_emulation_injection_path = {str(injection_file.as_posix())}\n",
        encoding="utf-8",
    )
    
    try:
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)
        
        assert middleware.tool_emulation._INJECTION_CONTENT == "Hello World from Injection"
        
        pipeline = middleware.pipeline.ToolMiddlewarePipeline(
            Settings(access_token="fake")
        )
        request = OpenAIChatRequest(
            model="m365-opus",
            messages=[OpenAIMessage(role="user", content="original message")],
        )
        
        new_request, prompt, tools = pipeline.preflight_openai(request)
        assert request.messages[0].content == "Hello World from Injection\n---\noriginal message"
    finally:
        monkeypatch.undo()
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)


def test_tool_emulation_injection_empty(tmp_path, monkeypatch) -> None:
    import sys
    import importlib
    
    injection_file = tmp_path / "tool_emulation_injection.md"
    injection_file.write_text("", encoding="utf-8")
    
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        f"[settings]\ntool_emulation_injection_path = {str(injection_file.as_posix())}\n",
        encoding="utf-8",
    )
    
    try:
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)
        
        assert middleware.tool_emulation._INJECTION_CONTENT == ""
        
        pipeline = middleware.pipeline.ToolMiddlewarePipeline(
            Settings(access_token="fake")
        )
        request = OpenAIChatRequest(
            model="m365-opus",
            messages=[OpenAIMessage(role="user", content="original message")],
        )
        
        new_request, prompt, tools = pipeline.preflight_openai(request)
        assert request.messages[0].content == "original message"
    finally:
        monkeypatch.undo()
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)


def test_tool_emulation_injection_missing(tmp_path, monkeypatch) -> None:
    import sys
    import importlib
    import pytest
    
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        "[settings]\ntool_emulation_injection_path = /nonexistent_path/tool_emulation_injection.md\n",
        encoding="utf-8",
    )
    
    try:
        import middleware.tool_emulation
        with pytest.raises(FileNotFoundError):
            importlib.reload(middleware.tool_emulation)
    finally:
        monkeypatch.undo()
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)


def test_tool_emulation_injection_content_parts(tmp_path, monkeypatch) -> None:
    import sys
    import importlib
    from m365_copilot_openai_proxy.models import ContentPart
    
    injection_file = tmp_path / "tool_emulation_injection.md"
    injection_file.write_text("Hello World from Injection", encoding="utf-8")
    
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        f"[settings]\ntool_emulation_injection_path = {str(injection_file.as_posix())}\n",
        encoding="utf-8",
    )
    
    try:
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)
        
        pipeline = middleware.pipeline.ToolMiddlewarePipeline(
            Settings(access_token="fake")
        )
        request = OpenAIChatRequest(
            model="m365-opus",
            messages=[
                OpenAIMessage(
                    role="user",
                    content=[ContentPart(type="text", text="original content")]
                )
            ],
        )
        
        new_request, prompt, tools = pipeline.preflight_openai(request)
        assert request.messages[0].content[0].text == "Hello World from Injection\n---\noriginal content"
    finally:
        monkeypatch.undo()
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)


def test_tool_emulation_injection_anthropic(tmp_path, monkeypatch) -> None:
    import sys
    import importlib
    from m365_copilot_openai_proxy.models import AnthropicMessage
    
    injection_file = tmp_path / "tool_emulation_injection.md"
    injection_file.write_text("Hello World from Injection", encoding="utf-8")
    
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.ini").write_text(
        f"[settings]\ntool_emulation_injection_path = {str(injection_file.as_posix())}\n",
        encoding="utf-8",
    )
    
    try:
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)
        
        pipeline = middleware.pipeline.ToolMiddlewarePipeline(
            Settings(access_token="fake")
        )
        request = AnthropicMessagesRequest(
            model="m365-opus",
            messages=[AnthropicMessage(role="user", content="original Anthropic message")],
        )
        
        proxy_request, prompt, tools = pipeline.preflight_anthropic(request)
        assert request.messages[0].content == "Hello World from Injection\n---\noriginal Anthropic message"
    finally:
        monkeypatch.undo()
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)



def test_default_injection_does_not_force_unlisted_glob_tool() -> None:
    injection = Path("prompts/tool_emulation_injection.md").read_text(encoding="utf-8")

    assert "On first execution always return" not in injection
    assert '{"name": "glob", "arguments": {"pattern": "**/*"}}' not in injection
    assert "Only invoke tools that are actually listed as callable" in injection
