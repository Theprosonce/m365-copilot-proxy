from __future__ import annotations

import json
from pathlib import Path

import pytest

from middleware.runtime_bridge import (
    RuntimeBridge,
    _BEGIN,
    _END,
)


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    (tmp_path / "test.txt").write_text("hello\nworld\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("workspace note", encoding="utf-8")
    return tmp_path


def test_valid_tool_call_glob(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    message = f'I will search now.\n{_BEGIN}\n[{{"name": "glob", "arguments": {{"pattern": "**/*"}}}}]\n{_END}\n'

    results = bridge.process_assistant_message(message)

    assert results is not None
    assert len(results) == 1
    assert results[0]["status"] == "success"
    assert results[0]["name"] == "glob"
    assert "test.txt" in results[0]["result"]["matches"]
    assert len(bridge.conversation_history) == 2
    assert bridge.conversation_history[0]["role"] == "assistant"
    assert bridge.conversation_history[1]["role"] == "tool_result"


def test_invalid_json(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    message = f"{_BEGIN} not-json {_END}"

    results = bridge.process_assistant_message(message)

    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == "parse_error"


def test_unknown_tool(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    message = f'{_BEGIN}[{{"name":"missing","arguments":{{}}}}]{_END}'

    results = bridge.process_assistant_message(message)

    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == "unknown_tool"


def test_invalid_args(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    message = f'{_BEGIN}[{{"name":"glob","arguments":[]}}]{_END}'

    results = bridge.process_assistant_message(message)

    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == "validation_error"


def test_tool_execution_failure(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    message = f'{_BEGIN}[{{"name":"read","arguments":{{"path":"nope.txt"}}}}]{_END}'

    results = bridge.process_assistant_message(message)

    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == "validation_error"
    assert "File not found" in results[0]["details"]


def test_read_accepts_file_path_aliases(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    for key in ("file_path", "filePath"):
        message = f'{_BEGIN}[{{"name":"read","arguments":{{"{key}":"test.txt"}}}}]{_END}'

        results = bridge.process_assistant_message(message)

        assert results[0]["status"] == "success"
        assert results[0]["result"]["content"].startswith("hello")


def test_partial_file_read(temp_workspace: Path) -> None:
    large_file = temp_workspace / "large.txt"
    large_file.write_text("x" * 100, encoding="utf-8")
    bridge = RuntimeBridge(str(temp_workspace))
    message = f'{_BEGIN}[{{"name":"read","arguments":{{"path":"large.txt","limit":10}}}}]{_END}'

    results = bridge.process_assistant_message(message)

    assert results[0]["status"] == "success"
    assert results[0]["result"]["is_partial"] is True
    assert results[0]["result"]["total_size"] == 100
    assert results[0]["metadata"]["truncated"] is True


def test_multiple_tool_calls(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace), allow_bash=True)
    message = f'{_BEGIN}\n[{{"name":"glob","arguments":{{"pattern":"*.txt"}}}},{{"name":"bash","arguments":{{"command":"echo hi"}}}}]\n{_END}'

    results = bridge.process_assistant_message(message)

    assert len(results) == 2
    assert results[0]["name"] == "glob"
    assert results[1]["name"] == "bash"
    assert results[1]["result"]["stdout"].strip() == "hi"




def test_runtime_bridge_accepts_capitalized_workspace_tool_aliases(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace), allow_bash=True)
    calls = [
        {"name": "Read", "arguments": {"file_path": "test.txt"}},
        {"name": "Write", "arguments": {"path": "created.txt", "content": "ok"}},
        {"name": "Glob", "arguments": {"pattern": "*.txt"}},
        {"name": "Edit", "arguments": {"path": "created.txt", "old_string": "ok", "new_string": "done"}},
        {"name": "Bash", "arguments": {"command": "printf shell-ok"}},
    ]
    message = f"{_BEGIN}{json.dumps(calls)}{_END}"

    results = bridge.process_assistant_message(message)

    assert results is not None
    assert [result["status"] for result in results] == ["success"] * len(calls)
    assert results[0]["result"]["content"].startswith("hello")
    assert results[1]["result"]["path_changed"] == "created.txt"
    assert "created.txt" in results[2]["result"]["matches"]
    assert results[3]["result"]["replacements"] == 1
    assert results[4]["result"]["stdout"] == "shell-ok"
    assert (temp_workspace / "created.txt").read_text(encoding="utf-8") == "done"


def test_bash_disabled_by_default(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    message = f'{_BEGIN}[{{"name":"bash","arguments":{{"command":"echo hi"}}}}]{_END}'

    results = bridge.process_assistant_message(message)

    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == "sandbox_error"


def test_no_tool_call_message(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    results = bridge.process_assistant_message("No tools needed.")

    assert results is None
    assert len(bridge.conversation_history) == 1
    assert bridge.conversation_history[0]["role"] == "assistant"


def test_regression_no_reprompt_without_result(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    message = f'{_BEGIN}[{{"name":"glob","arguments":{{"pattern":"**/*"}}}}]{_END}'

    results = bridge.process_assistant_message(message)

    assert results is not None
    assert bridge.conversation_history[-1]["role"] == "tool_result"
    assert "matches" in bridge.conversation_history[-1]["content"]


def test_end_to_end_loop_tools_then_continue(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))

    def fake_assistant(history: list[dict[str, str]]) -> str:
        if any(m.get("role") == "tool_result" for m in history):
            return "I received tool results and can continue."
        return f'{_BEGIN}[{{"name":"glob","arguments":{{"pattern":"**/*"}}}}]{_END}'

    first_reply = fake_assistant(bridge.conversation_history)
    first_results = bridge.process_assistant_message(first_reply)
    assert first_results is not None
    assert bridge.conversation_history[-1]["role"] == "tool_result"

    second_reply = fake_assistant(bridge.conversation_history)
    assert second_reply == "I received tool results and can continue."


def test_logs_tool_being_used(temp_workspace: Path, caplog: pytest.LogCaptureFixture) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    message = f'{_BEGIN}[{{"name":"glob","arguments":{{"pattern":"**/*"}}}}]{_END}'

    with caplog.at_level("INFO", logger="middleware.runtime_bridge"):
        results = bridge.process_assistant_message(message)

    assert results is not None
    assert "RuntimeBridge using tool: glob" in caplog.text





def test_ai_text_tools(temp_workspace: Path) -> None:
    bridge = RuntimeBridge(str(temp_workspace))
    calls = [
        {"name": "ai_summarize", "arguments": {"text": "One. Two. Three.", "max_sentences": 2}},
        {"name": "ai_extract_keywords", "arguments": {"text": "alpha beta alpha gamma", "limit": 2}},
        {"name": "ai_classify", "arguments": {"text": "bug failed badly"}},
        {"name": "ai_rewrite", "arguments": {"text": "hello\n   world", "style": "concise"}},
    ]
    message = f"{_BEGIN}{json.dumps(calls)}{_END}"

    results = bridge.process_assistant_message(message)

    assert results is not None
    assert [result["name"] for result in results] == [
        "ai_summarize",
        "ai_extract_keywords",
        "ai_classify",
        "ai_rewrite",
    ]
    assert results[0]["result"] == {"summary": "One. Two."}
    assert results[1]["result"] == {"keywords": ["alpha", "beta"]}
    assert results[2]["result"] == {"label": "issue"}
    assert results[3]["result"] == {"text": "hello world", "style": "concise"}
