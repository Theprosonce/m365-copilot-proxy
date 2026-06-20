from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import StandardToolCall, ToolMiddlewareContext


@dataclass(frozen=True)
class NativeToolExecutionRequest:
    """A single protocol-neutral native tool execution request.

    This is intentionally only a backend seam. It does not grant arbitrary local
    execution, perform client tool dispatch, or change public OpenAI/Anthropic
    response shapes by itself.
    """

    context: ToolMiddlewareContext
    tool_call: StandardToolCall
    session_id: str | None = None


@dataclass(frozen=True)
class NativeToolExecutionResult:
    """Structured native tool execution result returned by a backend."""

    tool_call_id: str
    name: str
    content: Any
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class NativeToolBackend(Protocol):
    """Minimal native backend contract for future real tool execution."""

    def can_execute(self, context: ToolMiddlewareContext) -> bool:
        """Return True only when this backend can handle the provided tool context."""

    async def execute(
        self, request: NativeToolExecutionRequest
    ) -> NativeToolExecutionResult:
        """Execute a single native tool call and return structured content."""


class NoopNativeToolBackend:
    """Safe default native backend: advertises no executable capability."""

    def can_execute(self, context: ToolMiddlewareContext) -> bool:
        return False

    async def execute(
        self, request: NativeToolExecutionRequest
    ) -> NativeToolExecutionResult:
        raise NotImplementedError("No native tool backend is configured.")
