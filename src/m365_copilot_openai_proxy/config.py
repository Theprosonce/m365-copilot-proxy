from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    access_token: str = Field(default="", alias="M365_ACCESS_TOKEN")
    time_zone: str = Field(default="Asia/Tokyo", alias="M365_TIME_ZONE")
    model_alias: str = Field(default="m365-copilot", alias="M365_MODEL_ALIAS")
    # True -> agent=work (enterprise grounding); False -> agent=web (no work grounding).
    # Coding agents (OpenCode) usually want False so Copilot doesn't pull enterprise files.
    work_grounding: bool = Field(default=True, alias="M365_WORK_GROUNDING")
    # True -> reuse ONE substrate conversation per client chat (rotated when a fresh chat starts),
    # instead of creating a new conversation per request/correction. Cuts the server-side footprint.
    persist_default: bool = Field(default=True, alias="M365_PERSIST_DEFAULT")
    # True -> open every conversation as a temporary/private chat (WS `disableMemory=1`): it is
    # not saved to the user's Copilot history and produces no memories. Captured from the web
    # client's incognito toggle. Default on so the proxy doesn't pollute the user's chat history.
    disable_memory: bool = Field(default=True, alias="M365_DISABLE_MEMORY")
    # SQLite file for the session/conversation store. Empty -> default under the home dir.
    # Tests point this at a tmp file so they never touch the real store.
    session_db_path: str = Field(default="", alias="M365_SESSION_DB")
    # Cap on stored conversations: when exceeded, the least-recently-used ones are evicted
    # (cache + DB) so the store can't grow without bound. 0 -> no cap.
    session_max: int = Field(default=1000, alias="M365_SESSION_MAX")
    # Evict conversations unused for this many seconds. 0 -> no TTL (cap-only eviction).
    session_ttl_seconds: int = Field(default=0, alias="M365_SESSION_TTL")
    # Seconds without any substrate frame before giving up; WS handshake open timeout.
    recv_timeout: int = Field(default=90, alias="M365_RECV_TIMEOUT")
    open_timeout: int = Field(default=30, alias="M365_OPEN_TIMEOUT")
    # True -> keep one substrate WebSocket alive per persistent session (skips the per-turn
    # handshake). Default off until validated against substrate (see Workstream A).
    ws_reuse: bool = Field(default=False, alias="M365_WS_REUSE")
    # True -> automatically close/hide the debug browser window when a token is successfully acquired.
    hide_on_token_success: bool = Field(
        default=True, alias="M365_HIDE_ON_TOKEN_SUCCESS"
    )
    # Path to the Edge executable used for the debug token-capture window.
    edge_path: str = Field(
        default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        alias="M365_EDGE_PATH",
    )
    # Passthrough: models NOT recognized as ours (m365-*) are forwarded to the real Anthropic API
    # on /v1/messages, instead of being routed to substrate. Off by default.
    anthropic_passthrough: bool = Field(
        default=False, alias="M365_ANTHROPIC_PASSTHROUGH"
    )
    anthropic_upstream: str = Field(
        default="https://api.anthropic.com", alias="M365_ANTHROPIC_UPSTREAM"
    )
    anthropic_version: str = Field(default="2023-06-01", alias="M365_ANTHROPIC_VERSION")
    # OAuth credential source (default): the Claude Code login file. Free (uses the subscription).
    anthropic_creds_file: str = Field(default="", alias="M365_ANTHROPIC_CREDS")
    # API-key override: if set, passthrough uses x-api-key with this key (consumes API credits)
    # instead of the OAuth subscription token.
    anthropic_key: str = Field(default="", alias="M365_ANTHROPIC_KEY")

    # Tool Middleware Policy
    # This is the protocol-neutral facade namespace for real/native tool-model support.
    # The current default keeps behavior delegated to the existing emulation backend.
    tool_middleware_enabled: bool = Field(
        default=True, alias="M365_TOOL_MIDDLEWARE_ENABLED"
    )
    tool_middleware_mode: str = Field(
        default="emulation", alias="M365_TOOL_MIDDLEWARE_MODE"
    )

    # Tool Emulation Policy
    tool_emulation_enabled: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_ENABLED"
    )
    tool_emulation_exclude_tools: str = Field(
        default="", alias="M365_TOOL_EMULATION_EXCLUDE_TOOLS"
    )
    # True lets Anthropic-compatible clients that send Claude model names
    # still use the proxy's prompt-based tool emulation when native
    # capability is unknown.
    tool_emulation_emulate_when_capability_unknown: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_UNKNOWN"
    )
    tool_emulation_native_passthrough: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_NATIVE_PASSTHROUGH"
    )
    tool_emulation_mode: str = Field(
        default="response_only", alias="M365_TOOL_EMULATION_MODE"
    )
    tool_emulation_prompt_template_version: str = Field(
        default="v1", alias="M365_TOOL_EMULATION_PROMPT_VERSION"
    )
    tool_emulation_max_tools_in_prompt: int = Field(
        default=8, alias="M365_TOOL_EMULATION_MAX_TOOLS"
    )
    tool_emulation_max_tool_schema_chars: int = Field(
        default=12000, alias="M365_TOOL_EMULATION_MAX_SCHEMA"
    )
    tool_emulation_max_single_tool_schema_chars: int = Field(
        default=3000, alias="M365_TOOL_EMULATION_MAX_SINGLE_SCHEMA"
    )
    tool_emulation_compact_schema: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_COMPACT_SCHEMA"
    )
    tool_emulation_cache_rendered_tool_prompts: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_CACHE_PROMPTS"
    )
    tool_emulation_force_non_streaming: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_FORCE_NON_STREAMING"
    )
    tool_emulation_override_temperature: bool = Field(
        default=False, alias="M365_TOOL_EMULATION_OVERRIDE_TEMP"
    )
    tool_emulation_default_temperature: float = Field(
        default=0.0, alias="M365_TOOL_EMULATION_DEFAULT_TEMP"
    )
    tool_emulation_parser_mode: str = Field(
        default="delimiter_first", alias="M365_TOOL_EMULATION_PARSER_MODE"
    )
    tool_emulation_allow_plain_json: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_ALLOW_PLAIN_JSON"
    )
    tool_emulation_allow_markdown_json_recovery: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_ALLOW_MARKDOWN_RECOVERY"
    )
    tool_emulation_allow_loose_json_recovery: bool = Field(
        default=False, alias="M365_TOOL_EMULATION_ALLOW_LOOSE_RECOVERY"
    )
    tool_emulation_max_parse_chars: int = Field(
        default=20000, alias="M365_TOOL_EMULATION_MAX_PARSE_CHARS"
    )
    tool_emulation_validate_schema: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_VALIDATE_SCHEMA"
    )
    tool_emulation_repair_invalid_tool_call_once: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_REPAIR_ONCE"
    )
    tool_emulation_max_agent_iterations: int = Field(
        default=1, alias="M365_TOOL_EMULATION_MAX_ITERATIONS"
    )
    tool_emulation_max_total_tool_calls: int = Field(
        default=3, alias="M365_TOOL_EMULATION_MAX_TOTAL_CALLS"
    )
    tool_emulation_prevent_repeated_tool_calls: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_PREVENT_REPEAT"
    )
    tool_emulation_execution_enabled: bool = Field(
        default=False, alias="M365_TOOL_EMULATION_EXECUTION_ENABLED"
    )
    tool_emulation_execution_sandbox: bool = Field(
        default=True, alias="M365_TOOL_EMULATION_EXECUTION_SANDBOX"
    )
