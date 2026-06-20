# Tool Middleware

The proxy exposes OpenAI-compatible, Anthropic-compatible, and Responses-style HTTP endpoints, while Microsoft 365 Copilot itself returns plain text through the browser-facing Substrate WebSocket. Tool support therefore has two layers:

1. **Protocol-neutral middleware models** used inside the proxy.
2. **Compatibility adapters/backends** that preserve public OpenAI and Anthropic response shapes.

The current runtime default is still prompt/sentinel emulation. The new middleware layer creates a clean seam for future native tool execution without folding that work into `ToolEmulationPipeline`.

## Goals

- Preserve existing OpenAI `tool_calls` responses.
- Preserve existing Anthropic `tool_use` responses.
- Keep legacy `M365_TOOL_EMULATION_*` behavior intact.
- Avoid using emulation config names for non-emulation semantics.
- Represent tool definitions, tool calls, and tool results with protocol-neutral internal objects.
- Keep native execution behind an explicit backend boundary.

## Modes

The middleware facade is controlled by:

| Variable | Default | Meaning |
|---|---:|---|
| `M365_TOOL_MIDDLEWARE_ENABLED` | `true` | Enables the protocol-neutral middleware facade. |
| `M365_TOOL_MIDDLEWARE_MODE` | `emulation` | Selects `off`, `emulation`, `native`, or `auto`. |

### `off`

Disables middleware behavior at the facade. Requests keep their public tool fields instead of being converted into the emulation prompt path.

### `emulation`

The compatibility default. Requests are normalized and delegated to `ToolEmulationPipeline`, which renders a tool protocol into the prompt and parses sentinel-delimited tool calls from the model response.

This mode preserves the existing public contract:

- OpenAI Chat Completions returns `choices[0].message.tool_calls` with `finish_reason: "tool_calls"`.
- Anthropic Messages returns `content` blocks with `type: "tool_use"` and `stop_reason: "tool_use"`.
- Tool arguments remain JSON-encoded strings at the OpenAI boundary.

### `native`

Uses only the configured native backend seam. It intentionally does **not** delegate to `ToolEmulationPipeline`.

The default native backend is `NoopNativeToolBackend`, which advertises no executable capability. This is deliberate: native mode should not silently gain arbitrary local execution, filesystem access, shell access, or client-tool dispatch. A real backend must explicitly implement capability checks and execution.

### `auto`

Checks the native backend first. If the backend explicitly reports it can execute the normalized tool context, middleware keeps the request on the native path. If not, it falls back to the existing emulation backend.

## Internal model layer

The protocol-neutral model layer lives under `src/m365_copilot_openai_proxy/tool_middleware/`:

- `models.py` defines the internal dataclasses:
  - `StandardToolDefinition`
  - `StandardToolCall`
  - `StandardToolResult`
  - `ToolMiddlewareContext`
  - `ToolMiddlewareOutput`
- `adapters.py` converts OpenAI and Anthropic public shapes to/from those internal models.
- `pipeline.py` selects middleware mode and backend routing.
- `native_backend.py` defines the native backend protocol and safe no-op backend.
- `tool_emulation.py` remains the prompt/sentinel compatibility backend.

Tool result content is intentionally typed as `Any` internally so dict/list payloads can stay structured inside middleware. Public endpoint adapters still serialize results as required by each protocol.

## Native backend boundary

`native_backend.py` defines:

- `NativeToolExecutionRequest`
- `NativeToolExecutionResult`
- `NativeToolBackend`
- `NoopNativeToolBackend`

A native backend must explicitly answer whether it can execute a `ToolMiddlewareContext`. Execution is not automatic merely because clients send a tool schema. This keeps the security boundary clear and prevents accidental local tool execution.

## Responses API status

`/v1/responses` has compatibility support and can participate in the current middleware compatibility path, but full Responses-native tool semantics are not implemented yet. Future work should add explicit handling for:

- `function_call` output items
- `function_call_output` input items
- stable `call_id` handling
- Responses streaming events for function-call argument deltas

## Streaming status

Streaming text is supported. Streaming tool-call deltas are a separate milestone because each public protocol fragments tool calls differently:

- OpenAI Chat Completions streams `tool_calls[].function.arguments` deltas.
- Anthropic streams `tool_use` blocks and JSON input deltas.
- Responses API uses function-call event items.

The current middleware models represent completed tool calls, not incremental tool-call deltas.

## Security boundaries

- The middleware facade does not grant local execution by itself.
- `native` mode is separate from prompt emulation and uses an explicit backend contract.
- The default native backend is no-op.
- Any future executable backend should require an allowlist/registry boundary and clear sandboxing rules.
- Debug logs, HAR captures, browser tokens, and `.env` remain sensitive and should never be committed.

## Test coverage

The middleware tests cover:

- OpenAI tool shape round-tripping.
- Anthropic tool-use shape round-tripping.
- `tool_choice="none"` prompt suppression.
- Middleware disable/off behavior.
- Native mode not delegating to emulation.
- Auto mode falling back to emulation when native cannot execute.
- Auto mode preferring native when a backend advertises capability.
- Structured tool-result content preservation.

Run validation with:

```bash
uv run --extra dev python -m compileall src tests
uv run --extra dev pytest tests/test_standard_tool_middleware.py tests/test_tool_middleware.py
uv run --extra dev pytest
```
