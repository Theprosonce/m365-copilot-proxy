from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ToolProtocol = Literal["openai", "anthropic"]
ToolMiddlewareMode = Literal["off", "emulation", "native", "auto"]


@dataclass(frozen=True)
class StandardToolFunction:
    """Protocol-neutral function metadata for a callable tool."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StandardToolDefinition:
    """Protocol-neutral tool definition used inside the middleware layer."""

    type: Literal["function"] = "function"
    function: StandardToolFunction = field(default_factory=lambda: StandardToolFunction(name=""))


@dataclass(frozen=True)
class StandardFunctionCall:
    """Protocol-neutral function call. Arguments stay structured internally."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StandardToolCall:
    """Protocol-neutral assistant tool call."""

    id: str
    type: Literal["function"] = "function"
    function: StandardFunctionCall = field(default_factory=lambda: StandardFunctionCall(name=""))


@dataclass(frozen=True)
class StandardToolResult:
    """Protocol-neutral tool result from a client/tool runtime."""

    tool_call_id: str
    content: Any


@dataclass(frozen=True)
class ToolMiddlewareContext:
    """Normalized request-edge tool context."""

    protocol: ToolProtocol
    tools: list[StandardToolDefinition] = field(default_factory=list)
    tool_choice: Any | None = None
    tool_results: list[StandardToolResult] = field(default_factory=list)


@dataclass(frozen=True)
class ToolMiddlewareOutput:
    """Normalized model/tool-middleware output."""

    calls: list[StandardToolCall] | None = None
    text: str = ""
