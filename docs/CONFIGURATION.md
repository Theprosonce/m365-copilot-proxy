# Configuration Guide

On first run, if `config.ini` is missing, the proxy will automatically create it in the project root directory using `config.ini.template` (or an embedded template).

## Configuration Sections

*   **`[settings]`** — Core operational parameters, session policies, paths, and integration credentials.
*   **`[serve]`** — API server parameters, address/port bindings, and remote CDP auto-refresh controls.
*   **`[capture_token]`** — Controls for the dedicated `capture-token` CLI utility.
*   **`[configure]`** — Client integration setup helpers.

---

## 1. Core Settings (`[settings]`)

The short-lived Microsoft 365 Copilot Substrate access token is not stored in `config.ini`. It is read from `M365_ACCESS_TOKEN` in `.env` or the process environment. Startup token-capture routines update `.env` automatically.


| Parameter | Default | Description |
| :--- | :--- | :--- |
| **`time_zone`** | `Asia/Tokyo` | Time zone used by the proxy when communicating with the Substrate API. |
| **`model_alias`** | `m365-copilot` | The OpenAI model alias name returned by `/v1/models` and used by inference endpoints. |
| **`work_grounding`** | `true` | `true` uses **Enterprise grounding** (grants access to corporate/work context and files); `false` uses **Web grounding**. Coding agents usually want `false` to avoid pulling irrelevant internal company documents. |
| **`persist_default`** | `true` | Retain and reuse exactly one Substrate conversation per client chat. Cuts down the footprint on the server-side. |
| **`disable_memory`** | `true` | Open conversations as a temporary/private chat (i.e. `disableMemory=1`): history and memories are not saved to Microsoft Copilot. |
| **`session_db_path`** | *empty* | Path to the SQLite database used to persist conversation session mappings. Defaults to `./.sessions/sessions.db`. |
| **`session_max`** | `1000` | Maximum number of conversations to store in the cache/DB. Excess conversations are evicted using an LRU (Least-Recently Used) policy. Use `0` for no cap. |
| **`session_ttl_seconds`** | `0` | Seconds after which unused conversations are automatically evicted from the database/cache. `0` disables time-based eviction. |
| **`recv_timeout`** | `90` | Handshake and socket frame read timeouts (in seconds) before the proxy gives up. |
| **`open_timeout`** | `30` | WebSocket handshake open timeout (in seconds). |
| **`substrate_concurrency_limit`** | `2` | Maximum FIFO-queued Substrate requests allowed in flight. Each request waits for a slot before SENT and retains it through the complete RECV; 0 or negative disables the limit. |
| **`truncation_before_sending`** | `true` | Whether to truncate the combined prompt/context before sending it to Substrate. Set to `false` to send the full text. |
| **`session_id`** | *empty* | Process-level persistent session identifier (formerly set via the `M365_SESSION` environment variable). Disables temporary/private chats when specified. |
| **`conversation_id`** | *empty* | Explicit Microsoft 365 Copilot conversation ID to use for the persistent session. Kept separate from the proxy `session_id`. |
| **`session_salt`** | *empty* | Salt used for the automatic client conversation fingerprinting. Set a custom value to ensure hashes remain stable across restarts. |
| **`debug`** | `false` | Writes detailed request and response payloads, logs, and diagnostics to `.sessions/debug.log`. |
| **`timing`** | `false` | Enables extra diagnostic latency and response timing logs. |
| **`ws_reuse`** | `false` | True keeps a single WebSocket alive per persistent session to skip handshakes. |
| **`browser_cdp_url`** | *empty* | Remote Chromium CDP base URL (for example `http://127.0.0.1:9222` or `http://chromium:9222`). When empty, the proxy uses `http://localhost:<cdp_port>`. |
| **`substrate_config_path`** | *empty* | Custom local file override for the substrate configuration JSON. |

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
| **`host`** | `0.0.0.0` | Bind the FastAPI proxy server to all network interfaces. Local clients use `127.0.0.1`; remote clients use the server hostname or IP address. |
| **`port`** | `8000` | The port the proxy server listens on. |
| **`cdp_port`** | `9222` | Fallback Chrome DevTools Protocol port when `browser_cdp_url` is not set. |
| **`auto_refresh`** | `true` | Automatically run background token refreshing routines before token expiration. |
| **`capture_on_start`** | `true` | Attempt to capture a token immediately on startup if none is present or if the current token is expired. |
| **`capture_timeout_seconds`**| `180` | Maximum seconds to wait for a successful remote Chromium CDP capture on startup. |
| **`refresh_before_seconds`** | `900` | Seconds before expiration to trigger a background token refresh (default: 15 minutes). |
| **`refresh_retry_seconds`** | `60` | Delay in seconds before retrying a failed token refresh. |
| **`configure_clients`** | `true` | Attempt to auto-configure local tools (like Claude Code and VS Code settings) on start. |
| **`manage_chromium`** | `true` | Start the bundled Docker Chromium service before serving and stop that service when `serve` exits. Startup fails if Docker Compose or the Chromium service is unavailable. |

---

## 3. Capture and Configuration Helpers (`[capture_token]`, `[configure]`)

These sections control specific command overrides:

*   **`[capture_token]`**:
    *   `cdp_port` (`9222`) — Chrome DevTools Protocol port.
    *   `timeout_seconds` (`60`) — Token capture timeout.
*   **`[configure]`**:
    *   `undo` (`false`) — Undo client integrations.
