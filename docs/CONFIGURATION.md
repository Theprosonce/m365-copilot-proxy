# Configuration Guide

Microsoft 365 Copilot OpenAI Proxy is configured entirely using a standard INI configuration file named `config.ini`. On first run, if `config.ini` is missing, the proxy will automatically create it in the project root directory using `config.ini.template` (or an embedded template).

## Configuration Sections

*   **`[settings]`** — Core operational parameters, session policies, paths, and integration credentials.
*   **`[serve]`** — API server parameters, address/port bindings, and auto-refresh browser controls.
*   **`[tool_middleware]`** — Setup for the protocol-neutral tool middleware layer.
*   **`[tool_emulation]`** — Advanced settings for the ReAct emulation backend (such as schema rules and iteration limits).
*   **`[capture_token]`** — Controls for the dedicated `capture-token` CLI utility.
*   **`[launch_edge]`** — Controls for the dedicated `launch-edge` CLI utility.
*   **`[configure]`** — Client integration setup helpers.

---

## 1. Core Settings (`[settings]`)

| Parameter | Default | Description |
| :--- | :--- | :--- |
| **`access_token`** | *empty* | The short-lived Microsoft 365 Copilot Substrate access token. If missing, startup token-capture routines will automatically acquire and populate this field. |
| **`time_zone`** | `Asia/Tokyo` | Time zone used by the proxy when communicating with the Substrate API. |
| **`model_alias`** | `m365-copilot` | The OpenAI model alias name returned by `/v1/models` and used by inference endpoints. |
| **`work_grounding`** | `true` | `true` uses **Enterprise grounding** (grants access to corporate/work context and files); `false` uses **Web grounding**. Coding agents usually want `false` to avoid pulling irrelevant internal company documents. |
| **`persist_default`** | `true` | Retain and reuse exactly one Substrate conversation per client chat. Cuts down the footprint on the server-side. |
| **`disable_memory`** | `true` | Open conversations as a temporary/private chat (i.e. `disableMemory=1`): history and memories are not saved to Microsoft Copilot. |
| **`session_db_path`** | *empty* | Path to the SQLite database used to persist conversation session mappings. Defaults to `~/.m365-copilot-openai-proxy/sessions.db`. |
| **`session_max`** | `1000` | Maximum number of conversations to store in the cache/DB. Excess conversations are evicted using an LRU (Least-Recently Used) policy. Use `0` for no cap. |
| **`session_ttl_seconds`** | `0` | Seconds after which unused conversations are automatically evicted from the database/cache. `0` disables time-based eviction. |
| **`recv_timeout`** | `90` | Handshake and socket frame read timeouts (in seconds) before the proxy gives up. |
| **`open_timeout`** | `30` | WebSocket handshake open timeout (in seconds). |
| **`session_id`** | *empty* | Process-level persistent session identifier (formerly set via the `M365_SESSION` environment variable). Disables temporary/private chats when specified. |
| **`session_salt`** | *empty* | Salt used for the automatic client conversation fingerprinting. Set a custom value to ensure hashes remain stable across restarts. |
| **`debug`** | `false` | Writes detailed request and response payloads, logs, and diagnostics to `debug.log`. |
| **`timing`** | `false` | Enables extra diagnostic latency and response timing logs. |
| **`edge_headless`** | `false` | True launches Edge in headless mode for auto-token refresh (no visible window). |
| **`edge_path`** | `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` | Absolute path to the Edge browser executable. |
| **`ws_reuse`** | `false` | True keeps a single WebSocket alive per persistent session to skip handshakes. |
| **`hide_on_token_success`**| `true` | Automatically close or hide the Edge debug window once a fresh token is acquired. |
| **`substrate_config_path`** | *empty* | Custom local file override for the substrate configuration JSON. |
| **`prompt_catalog_path`** | *empty* | Custom local file override for the prompt catalog. |
| **`tool_emulation_injection_path`**| *empty* | Custom local file override for the tool emulation markdown prompt template. |

### OAuth / Refresh State (Auto-Populated)
These variables are automatically negotiated and refreshed during browser capture:
*   **`refresh_token`** — Captured Microsoft refresh token.
*   **`tenant_id`** — Microsoft tenant ID.
*   **`client_id`** — Microsoft OAuth client ID.

### Anthropic Passthrough settings
These settings allow sending non-M365 model queries directly to Anthropic:
*   **`anthropic_passthrough`** (`false`) — Forward unrecognized models (e.g. `claude-3-opus-20240229`) to Anthropic.
*   **`anthropic_upstream`** (`https://api.anthropic.com`) — Base URL for Anthropic.
*   **`anthropic_version`** (`2023-06-01`) — Target Anthropic API version.
*   **`anthropic_creds_file`** (*empty*) — Path to a Claude Code credential source file.
*   **`anthropic_key`** (*empty*) — Override API Key for Anthropic passthrough.

---

## 2. Server Settings (`[serve]`)

| Parameter | Default | Description |
| :--- | :--- | :--- |
| **`host`** | `127.0.0.1` | The local IP address to bind the FastAPI proxy server to. |
| **`port`** | `8000` | The port the proxy server listens on. |
| **`cdp_port`** | `9222` | The port used by Chrome DevTools Protocol to attach to the Edge browser process. |
| **`auto_refresh`** | `true` | Automatically run background token refreshing routines before token expiration. |
| **`launch_edge`** | `true` | Launch Edge automatically on startup to capture/refresh tokens. |
| **`capture_on_start`** | `true` | Attempt to capture a token immediately on startup if none is present or if the current token is expired. |
| **`capture_timeout_seconds`**| `180` | Maximum seconds to wait for a successful Edge CDP capture on startup. |
| **`refresh_before_seconds`** | `900` | Seconds before expiration to trigger a background token refresh (default: 15 minutes). |
| **`refresh_retry_seconds`** | `60` | Delay in seconds before retrying a failed token refresh. |
| **`configure_clients`** | `true` | Attempt to auto-configure local tools (like Claude Code and VS Code settings) on start. |

---

## 3. Tool Middleware (`[tool_middleware]`)

| Parameter | Default | Description |
| :--- | :--- | :--- |
| **`enabled`** | `false` | Turns on the protocol-neutral tool middleware facade. |
| **`mode`** | `emulation` | Middleware policy mode: `off`, `emulation`, `native`, or `auto`. `emulation` converts tool schemas to ReAct prompt definitions; `native` targets real execution backend seams; `auto` prefers native only when executing. |
| **`plugin_paths`** | *empty* | Comma-separated paths to external tool/middleware plugin modules. |

---

## 4. Tool Emulation Backend (`[tool_emulation]`)

These control the advanced prompt-based ReAct loop that simulates tool capabilities:

| Parameter | Default | Description |
| :--- | :--- | :--- |
| **`enabled`** | `false` | Enable the prompt emulation backend for tool definitions. |
| **`exclude_tools`** | *empty* | Comma-separated list of tool/function names to exclude from emulation (e.g. `bash` to prevent agents from executing commands). |
| **`emulate_when_capability_unknown`** | `true` | Emulates tools for Anthropic-compatible clients sending unknown capabilities. |
| **`native_passthrough`** | `true` | Forwards native tool definitions directly when native mode is allowed. |
| **`mode`** | `response_only` | Mode of emulation (default is `response_only`). |
| **`prompt_template_version`**| `v1` | Emulation system prompt template format. |
| **`max_tools_in_prompt`** | `8` | Maximum number of tools to expose to the model in the system prompt turn. |
| **`max_tool_schema_chars`** | `12000` | Max character budget for combined tool schemas. |
| **`max_single_tool_schema_chars`**| `3000` | Max character budget for a single tool definition. |
| **`compact_schema`** | `true` | Minimizes tool schema formatting white space to save input tokens. |
| **`cache_rendered_tool_prompts`**| `true` | Caches tool definition prompts internally to skip compilation overhead. |
| **`force_non_streaming`** | `true` | Force a non-streaming turn from the upstream API during a tool emulation cycle, converting back to streaming if requested by the client. |
| **`override_temperature`** | `false` | Overrides client-provided temperature to ensure deterministic tool extraction. |
| **`default_temperature`** | `0.0` | Target temperature to enforce when `override_temperature=true`. |
| **`parser_mode`** | `delimiter_first`| Parsing contract mode for raw response parsing. |
| **`allow_plain_json`** | `true` | Accept plain JSON outputs as valid tool call syntax. |
| **`allow_markdown_json_recovery`**| `true` | Extract JSON schemas from markdown codeblocks on malformed turn replies. |
| **`allow_loose_json_recovery`**| `false` | Attempts fuzzy regex parsing to extract arguments from broken JSON structures. |
| **`max_parse_chars`** | `20000` | Maximum output characters to scan when looking for tool delimiters. |
| **`validate_schema`** | `true` | Validate returned arguments against the client-supplied JSON schemas. |
| **`repair_invalid_tool_call_once`**| `true` | Sends schema errors back to the model for one correction iteration. |
| **`max_agent_iterations`** | `1` | Max agent ReAct steps allowed per incoming turn. |
| **`max_total_tool_calls`** | `3` | Maximum tool calls processed per incoming turn. |
| **`prevent_repeated_tool_calls`**| `true` | Detect and prevent infinite repetition of exact same tool parameters. |
| **`execution_enabled`** | `false` | Allows the proxy to execute certain standard script blocks locally. |
| **`execution_sandbox`** | `true` | Runs execution scripts inside a local security sandbox. |

---

## 5. Capturing / Launch Helpers (`[capture_token]`, `[launch_edge]`, `[configure]`)

These sections control specific command overrides:

*   **`[capture_token]`**:
    *   `cdp_port` (`9222`) — Chrome DevTools Protocol port.
    *   `timeout_seconds` (`60`) — Token capture timeout.
*   **`[launch_edge]`**:
    *   `cdp_port` (`9222`) — Chrome DevTools Protocol port.
*   **`[configure]`**:
    *   `undo` (`false`) — Undo client integrations.
