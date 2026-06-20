from __future__ import annotations

from typing import Any

from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.models import AnthropicMessagesRequest, OpenAIChatRequest
from .adapters import (
    anthropic_tools_to_standard,
    openai_functions_to_standard,
    openai_tools_to_standard,
    standard_tools_to_openai,
)
from .models import ToolMiddlewareContext
from .native_backend import NativeToolBackend, NoopNativeToolBackend
from .tool_emulation import ToolEmulationPipeline

_SUPPORTED_MODES = {"off", "emulation", "auto", "native"}


class ToolMiddlewarePipeline:
    """Protocol-neutral tool middleware facade.

    The facade preserves existing compatibility by keeping `emulation` as the
    default mode. `native` is now a separate backend seam: it never delegates to
    the prompt/sentinel emulation path. `auto` prefers native only when a backend
    explicitly advertises capability, then falls back to today's emulation path.
    """

    def __init__(
        self,
        settings: Settings,
        native_backend: NativeToolBackend | None = None,
        emulation_backend: ToolEmulationPipeline | None = None,
    ):
        self.settings = settings
        self.native_backend = native_backend or NoopNativeToolBackend()
        self.emulation = emulation_backend or ToolEmulationPipeline(settings)

    @property
    def force_non_streaming(self) -> bool:
        return self.emulation.settings.tool_emulation_force_non_streaming

    @property
    def mode(self) -> str:
        mode = (self.settings.tool_middleware_mode or "emulation").strip().lower()
        if mode not in _SUPPORTED_MODES:
            raise ValueError(
                f"Tool middleware mode {self.settings.tool_middleware_mode!r} is not supported. "
                f"Supported modes: {', '.join(sorted(_SUPPORTED_MODES))}."
            )
        return mode

    def _middleware_enabled(self) -> bool:
        return bool(self.settings.tool_middleware_enabled) and self.mode != "off"

    def _openai_context(self, request: OpenAIChatRequest) -> ToolMiddlewareContext:
        return ToolMiddlewareContext(
            protocol="openai",
            tools=[
                *openai_tools_to_standard(request.tools),
                *openai_functions_to_standard(request.functions),
            ],
            tool_choice=request.tool_choice or request.function_call,
        )

    def _anthropic_context(
        self, request: AnthropicMessagesRequest
    ) -> ToolMiddlewareContext:
        return ToolMiddlewareContext(
            protocol="anthropic",
            tools=anthropic_tools_to_standard(request.tools),
            tool_choice=request.tool_choice,
        )

    def _native_can_execute(self, context: ToolMiddlewareContext) -> bool:
        return bool(context.tools) and self.native_backend.can_execute(context)

    def is_openai_active(self, request: OpenAIChatRequest) -> bool:
        if not self._middleware_enabled():
            return False
        context = self._openai_context(request)
        if self.mode == "native":
            return self._native_can_execute(context)
        if self.mode == "auto" and self._native_can_execute(context):
            return True
        return self.emulation.is_emulation_active(request)

    def preflight_openai(
        self, request: OpenAIChatRequest
    ) -> tuple[OpenAIChatRequest, str | None, list[dict[str, Any]]]:
        normalized_tools = self.emulation._normalize_tools(request)
        if not self._middleware_enabled():
            return request, None, normalized_tools

        context = self._openai_context(request)
        if self.mode == "native":
            return request, None, normalized_tools
        if self.mode == "auto" and self._native_can_execute(context):
            return request, None, normalized_tools
        return self.emulation.preflight(request)

    def openai_proxy_request_from_anthropic(
        self, request: AnthropicMessagesRequest
    ) -> OpenAIChatRequest:
        standard_tools = anthropic_tools_to_standard(request.tools)
        return OpenAIChatRequest(
            model=request.model,
            messages=[],
            tools=standard_tools_to_openai(standard_tools),
            tool_choice=request.tool_choice,
        )

    def is_anthropic_active(self, request: AnthropicMessagesRequest) -> bool:
        if not self._middleware_enabled():
            return False
        context = self._anthropic_context(request)
        if self.mode == "native":
            return self._native_can_execute(context)
        if self.mode == "auto" and self._native_can_execute(context):
            return True
        return self.is_openai_active(self.openai_proxy_request_from_anthropic(request))

    def preflight_anthropic(
        self, request: AnthropicMessagesRequest
    ) -> tuple[OpenAIChatRequest, str | None, list[dict[str, Any]]]:
        proxy_request = self.openai_proxy_request_from_anthropic(request)
        if not self._middleware_enabled():
            return proxy_request, None, self.emulation._normalize_tools(proxy_request)

        context = self._anthropic_context(request)
        if self.mode == "native":
            return proxy_request, None, self.emulation._normalize_tools(proxy_request)
        if self.mode == "auto" and self._native_can_execute(context):
            return proxy_request, None, self.emulation._normalize_tools(proxy_request)
        return self.preflight_openai(proxy_request)

    async def execute_upstream(self, *args: Any, **kwargs: Any):
        if self.mode == "native":
            raise NotImplementedError(
                "Native tool middleware execution is not implemented for upstream model calls yet."
            )
        return await self.emulation.execute_upstream(*args, **kwargs)
