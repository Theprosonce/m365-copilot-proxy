# Configuration Guide

On first run, if `config.ini` is missing, the proxy will automatically create it in the project root directory using `config.ini.template` (or an embedded template).

## Configuration Sections

*   **`[settings]`** — Core operational parameters, session policies, paths, and integration credentials.
*   **`[serve]`** — API server parameters, address/port bindings, and auto-refresh browser controls.
*   **`[capture_token]`** — Controls for the dedicated `capture-token` CLI utility.
*   **`[launch_edge]`** — Controls for the dedicated `launch-edge` CLI utility.
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

## 3. Capturing / Launch Helpers (`[capture_token]`, `[launch_edge]`, `[configure]`)

These sections control specific command overrides:

*   **`[capture_token]`**:
    *   `cdp_port` (`9222`) — Chrome DevTools Protocol port.
    *   `timeout_seconds` (`60`) — Token capture timeout.
*   **`[launch_edge]`**:
    *   `cdp_port` (`9222`) — Chrome DevTools Protocol port.
*   **`[configure]`**:
    *   `undo` (`false`) — Undo client integrations.
