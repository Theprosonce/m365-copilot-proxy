"""Strict two-phase ``<<<TOOL_CALLS>>>`` extraction.

These tests lock in the contract described in ``_extract_delimited_block``:

* Phase 1 (gate): the response must literally BEGIN with the marker. A
  mid-sentence mention in conversational prose is NOT a tool call and must be
  treated as ordinary text (no parsing, no retry, no leak).
* Phase 2 (capture): the block is bounded by the FINAL ``<<<END_TOOL_CALLS>>>``
  delimiter, so trailing chatter or an echoed opening marker can't corrupt it.

They also guarantee that no raw wire token ever reaches the end user: a failed
parse returns ``None`` and the surfaced text is redacted.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.models import ToolCall
from middleware.tool_emulation import (
    ToolEmulationPipeline,
)

_BEGIN = "<<<TOOL_CALLS>>>"
_END = "<<<END_TOOL_CALLS>>>"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        M365_ACCESS_TOKEN="fake",
        M365_TOOL_EMULATION_ENABLED=True,
    )


@pytest.fixture
def pipeline(settings: Settings) -> ToolEmulationPipeline:
    return ToolEmulationPipeline(settings)


@pytest.fixture
def weather_tools() -> list[dict]:
    return [
        {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        }
    ]


def _wrap(payload: str) -> str:
    return f"{_BEGIN}\n{payload}\n{_END}"


# --------------------------------------------------------------------------- #
# Phase 1 + 2: valid tool-call payloads are extracted reliably.
# --------------------------------------------------------------------------- #


def test_extract_starts_with_marker(pipeline, weather_tools) -> None:
    text = _wrap('[{"name": "get_weather", "arguments": {"location": "London"}}]')
    calls = pipeline.parse_response(text, weather_tools)

    assert calls is not None
    assert len(calls) == 1
    assert calls[0].function.name == "get_weather"
    assert json.loads(calls[0].function.arguments) == {"location": "London"}


def test_extract_tolerates_leading_whitespace(pipeline, weather_tools) -> None:
    # Leading whitespace before the marker is allowed; the contract is "begins
    # with the marker" not "byte zero is the marker".
    text = "  \n" + _wrap('[{"name": "get_weather", "arguments": {"location": "London"}}]')
    calls = pipeline.parse_response(text, weather_tools)

    assert calls is not None
    assert calls[0].function.name == "get_weather"


def test_extract_multiple_calls(pipeline) -> None:
    tools = [
        {"name": "glob", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}}},
    ]
    text = _wrap(
        '[{"name": "glob", "arguments": {"pattern": "*.py"}}, '
        '{"name": "glob", "arguments": {"pattern": "*.md"}}]'
    )
    calls = pipeline.parse_response(text, tools)

    assert calls is not None
    assert len(calls) == 2
    assert calls[0].function.name == "glob"
    assert calls[1].function.name == "glob"


def test_extract_nested_braces_in_arguments(pipeline, weather_tools) -> None:
    # The closing delimiter is matched from the END, so a JSON value containing
    # braces (or text resembling the markers) between them is preserved intact.
    payload = (
        '[{"name": "get_weather", "arguments": '
        '{"location": "London", "filter": {"city": "London}"}}}]'
    )
    text = _wrap(payload)
    calls = pipeline.parse_response(text, weather_tools)

    assert calls is not None
    args = json.loads(calls[0].function.arguments)
    assert args["filter"] == {"city": "London}"}


def test_extract_ignores_trailing_prose_after_block(pipeline, weather_tools) -> None:
    # The closing delimiter bounds the payload from the end, so a well-formed
    # block followed by trailing prose parses cleanly: the prose is not part of
    # the payload and is not mis-parsed as a second call.
    text = (
        _wrap('[{"name": "get_weather", "arguments": {"location": "London"}}]')
        + "\nSome trailing chatter, no markers here.\n"
    )
    calls = pipeline.parse_response(text, weather_tools)

    assert calls is not None
    assert len(calls) == 1
    assert json.loads(calls[0].function.arguments) == {"location": "London"}


# --------------------------------------------------------------------------- #
# Phase 1 gate: false positives are NOT misinterpreted as tool calls.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        # Conversational mention of the marker mid-sentence.
        f"I will use `{_BEGIN}` to call a tool.",
        # Marker mentioned as part of an explanation that also names the closer.
        f"To call tools you wrap JSON in {_BEGIN} ... {_END}.",
        # Quote-style mention with an example payload after prose.
        f"Example: {_BEGIN} not-a-call {_END}",
        # Marker embedded in a question.
        f"What does {_BEGIN} mean in the prompt?",
        # Marker near the start but NOT at the start (prose prefix).
        f"Sure. {_BEGIN}\n[{{}}]\n{_END}",
    ],
)
def test_conversational_mention_is_not_a_tool_call(
    pipeline, weather_tools, text
) -> None:
    """The defining regression: a mere mention must never be parsed."""
    calls = pipeline.parse_response(text, weather_tools)
    assert calls is None


def test_marker_only_at_end_is_not_a_tool_call(pipeline, weather_tools) -> None:
    text = f"Here is my reasoning...\n{_BEGIN}\n[{{}}]\n{_END}"
    calls = pipeline.parse_response(text, weather_tools)
    assert calls is None


def test_unclosed_marker_is_not_a_tool_call(pipeline, weather_tools) -> None:
    # Opens with the marker but never closes -> malformed block, refused.
    text = f"{_BEGIN}\n[{json.dumps({'name': 'get_weather', 'arguments': {'location': 'X'}})}]"
    calls = pipeline.parse_response(text, weather_tools)
    assert calls is None


def test_empty_text_returns_none(pipeline, weather_tools) -> None:
    assert pipeline.parse_response("", weather_tools) is None
    assert pipeline.parse_response("   \n  ", weather_tools) is None


# --------------------------------------------------------------------------- #
# Redaction: raw wire tokens never leak to the user on a failed parse.
# --------------------------------------------------------------------------- #


def test_redact_strips_complete_block() -> None:
    pipeline = ToolEmulationPipeline(Settings(M365_ACCESS_TOKEN="fake"))
    text = f"prefix {_BEGIN}\n[{json.dumps([{'name': 'x'}])}]\n{_END} suffix"
    assert _BEGIN not in pipeline._redact_tool_sentinels(text)
    assert _END not in pipeline._redact_tool_sentinels(text)


def test_redact_strips_dangling_sentinels() -> None:
    pipeline = ToolEmulationPipeline(Settings(M365_ACCESS_TOKEN="fake"))
    # No matching END here -> the block regex leaves the open marker behind,
    # which the per-marker scrub must then remove.
    text = f"I will use {_BEGIN} for this."
    redacted = pipeline._redact_tool_sentinels(text)
    assert _BEGIN not in redacted
    assert _END not in redacted
    assert redacted.startswith("I will use")


def test_redact_preserves_clean_text() -> None:
    pipeline = ToolEmulationPipeline(Settings(M365_ACCESS_TOKEN="fake"))
    text = "Just a normal assistant reply with no markers."
    assert pipeline._redact_tool_sentinels(text) == text


def test_extract_delimited_block_helper(pipeline) -> None:
    # Phase 1 gate.
    assert pipeline._extract_delimited_block("no marker here") is None
    assert pipeline._extract_delimited_block(f"prose {_BEGIN} x {_END}") is None
    # Phase 2 capture: clean block bounded by its single closer.
    payload = pipeline._extract_delimited_block(_wrap('[{"name": "a"}]'))
    assert payload == '[{"name": "a"}]'
    # rfind bounds with the LAST closer: trailing prose (no stray closer) is
    # excluded from the payload.
    payload = pipeline._extract_delimited_block(
        _wrap('[{"name": "a"}]') + "\ntrailing prose, no markers\n"
    )
    assert payload == '[{"name": "a"}]'


# --------------------------------------------------------------------------- #
# Retry heuristic alignment: a mere mention must NOT trigger a correction retry.
# --------------------------------------------------------------------------- #


def test_has_tool_block_heuristic_ignores_mention(pipeline) -> None:
    # A conversational mention is not a genuine (malformed) attempt.
    assert pipeline.has_tool_block_heuristic(f"I use {_BEGIN}") is False
    # A block that genuinely starts with the marker IS a real attempt.
    assert pipeline.has_tool_block_heuristic(f"{_BEGIN} not json") is True
    # Fenced-JSON is still a genuine structural attempt.
    assert (
        pipeline.has_tool_block_heuristic("```json\n[{\"name\":\"x\"}]\n```")
        is True
    )


# --------------------------------------------------------------------------- #
# execute_upstream end-to-end: no leak + no false tool call.
# --------------------------------------------------------------------------- #


class _FakeChatClient:
    """Returns a canned reply, regardless of the prompt."""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def chat(self, *_args, **_kwargs) -> str:
        return self.reply


def test_execute_upstream_false_positive_yields_no_calls_and_redacted_text(
    pipeline, weather_tools
) -> None:
    reply = f"I will use `{_BEGIN}` to read a file."
    client = _FakeChatClient(reply)

    calls, text = asyncio.run(
        pipeline.execute_upstream(
            client, "prompt", [], object(), "neutral", weather_tools
        )
    )

    assert calls is None
    # No raw token leaks to the surfaced reply.
    assert _BEGIN not in text
    assert _END not in text
    assert "I will use" in text


def test_execute_upstream_valid_block_yields_calls(pipeline, weather_tools) -> None:
    reply = _wrap('[{"name": "get_weather", "arguments": {"location": "London"}}]')
    client = _FakeChatClient(reply)

    calls, _text = asyncio.run(
        pipeline.execute_upstream(
            client, "prompt", [], object(), "neutral", weather_tools
        )
    )

    assert calls is not None
    assert isinstance(calls[0], ToolCall)
    assert calls[0].function.name == "get_weather"
