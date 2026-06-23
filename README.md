# Microsoft 365 Copilot OpenAI Proxy

Use Microsoft 365 Copilot through OpenAI-compatible clients, local scripts, and coding tools.

This project runs a local FastAPI proxy that talks to the same `substrate.office.com` WebSocket API used by the M365 Copilot web UI, then exposes it as OpenAI-style HTTP endpoints.

No Azure app registration. No admin consent. Sign in with your normal M365 Copilot browser session.

> Fork of [kuchris/m365-copilot-openai-proxy](https://github.com/kuchris/m365-copilot-openai-proxy), extended with a model picker, vision, protocol-neutral tool middleware, temporary chats, and a session-management API.

## Why Use This

- Use M365 Copilot from OpenAI-compatible clients
- Works with your existing signed-in Copilot web session
- Runs locally on `127.0.0.1` by default
- Auto-captures and refreshes the short-lived browser token
- **Model picker** — choose Claude Opus or GPT‑5.5 (quick / reasoning) via the model name
- **Work / Web grounding** toggle
- **Vision** — forwards images (OpenAI `image_url` base64 and VS Code attachments) to Copilot
- **Tool middleware** — a protocol-neutral tool layer with a ReAct emulation backend so agentic clients (OpenCode, VS Code, Claude Code) can drive tools, even though Copilot returns only text
- **Temporary chats** by default — proxy conversations are not saved to your Copilot history and produce no memories
- **Per-chat persistence** in SQLite, plus a CRUD API over the tracked conversations
- Supports OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages style requests
- Interactive **OpenAPI docs** at `/docs`

## Quick Start

```powershell
uv sync
uv run copilot-openai-proxy serve
```

The server starts at:

```text
http://127.0.0.1:8000
```

On first run, the proxy opens a dedicated Browser window. Sign in to M365 Copilot there once. The proxy will capture the required Substrate token and write it to `.env`.

The dedicated browser profile is stored at:

```text
%USERPROFILE%\.m365-copilot-openai-proxy\edge-profile
```

If startup says it is waiting for a token, click the Copilot message box and type one character. You do not need to send the message.

### Run from source (no .exe)

Use this on machines where the signed release binaries are blocked by Application Control / Smart App Control. Source pulled via `git` carries no Mark-of-the-Web, so the interpreter runs normally.

```powershell
# clone, then from the repo root:
powershell -ExecutionPolicy Bypass -File .\run.ps1            # tray GUI (bare invocation)
powershell -ExecutionPolicy Bypass -File .\run.ps1 serve      # headless API
```

```bash
./run.sh            # tray GUI (needs a desktop + python3-tk)
./run.sh serve      # headless API
```

The scripts use `uv` when available, otherwise fall back to a local `.venv` + `pip install -e .`. Equivalent one-liners without the scripts:

```bash
uv run copilot-openai-proxy serve
# or, without uv (after `pip install -e .`):
python -m m365_copilot_openai_proxy serve
```

### Build / toggle scripts

One unified command per platform that toggles the proxy on/off and ensures
the runtime is in place before the first start.

| Script | OS | Purpose |
|---|---|---|
| `proxy.ps1` *(recommended on Windows)* | Windows | Toggle on/off; build the standalone exe if missing, then start it headless. `.\proxy.ps1` toggles, `.\proxy.ps1 -ForceBuild` rebuilds first. Locally built → no Mark-of-the-Web → no SmartScreen prompt. |
| `proxy.sh` *(recommended on macOS / Linux)* | macOS / Linux | Toggle on/off; create the `.venv` + editable install on first start, then run headless from source in background. `./proxy.sh --reinstall` forces a fresh `pip install -e .`. |
| `proxy-toggle.bat` | Windows | Simple toggle on/off of the venv console script. No build step — assumes `.venv` is already set up. |
| `build-exe.ps1` | Windows | Explicit PyInstaller build of `dist\m365-copilot-proxy.exe`, self-signed. Called automatically by `proxy.ps1` when the exe is missing. |
| `run.ps1` / `run.sh` | All | Foreground run from source (tray GUI or `serve`). Use these for dev, not for "fire and forget". |

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\proxy.ps1              # toggle
powershell -ExecutionPolicy Bypass -File .\proxy.ps1 -ForceBuild  # rebuild + start
```

```bash
# macOS / Linux
./proxy.sh              # toggle
./proxy.sh --reinstall  # refresh editable install + start
```

## Test It

```powershell
$body = @{
  model = "m365-copilot"
  messages = @(
    @{ role = "user"; content = "Say hello in one short sentence." }
  )
} | ConvertTo-Json -Depth 10

$r = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/v1/chat/completions" `
  -ContentType "application/json" `
  -Body $body

$r.choices[0].message.content
```

## Connect A Client

Use these settings for any OpenAI-compatible client:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8000/v1` |
| API Key | `dummy` |
| Model | `m365-opus` (or any id below) |

### Models (picker)

The model name selects the underlying Copilot model (substrate `tone`). `GET /v1/models` lists them.

| Model id | Underlying model |
|---|---|
| `m365-copilot`, `m365-auto`, `m365-opus`, `m365-claude` | Claude Opus |
| `m365-gpt`, `m365-gpt-quick` | GPT‑5.5 (quick) |
| `m365-gpt-think`, `m365-gpt-reasoning` | GPT‑5.5 (reasoning) |

Append `:persist` to any id (e.g. `m365-opus:persist`) to reuse one Copilot conversation per chat.

### **OpenCode**

#### Option A: Temporary
Windows:
```powershell
$env:OPENAI_BASE_URL = "http://127.0.0.1:8000"
$env:OPENAI_API_KEY = "dummy"
opencode
```

Linux:
```bash
export OPENAI_BASE_URL="http://127.0.0.1:8000"
export OPENAI_API_KEY="dummy"
opencode
```

Select **OpenAI API** as the provider, then use:

```text
m365-copilot
```

#### Option B: Permanent


Windows Setup:

- **Add/update** the provider:
  ```cmd
  setup\opencode.bat
  ```
  or
  ```powershell
  .\setup\opencode.ps1
  ```

- **Remove** the provider:
  ```cmd
  setup\opencode.bat --remove
  ```
  or
  ```powershell
  .\setup\opencode.ps1 --remove
  ```

Config location: `%USERPROFILE%\.config\opencode\opencode.json`

Linux Setup:
```bash
chmod +x setup/opencode.sh
./setup/opencode.sh
opencode
```

__Note: Use :persist suffix to enable persistent sessions__

### Continue

Add this to `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "M365 Copilot",
      "provider": "openai",
      "model": "m365-copilot:persist",
      "apiBase": "http://127.0.0.1:8000/v1",
      "apiKey": "dummy"
    }
  ]
}
```

### Claude Code

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8000"
$env:ANTHROPIC_API_KEY = "dummy"
claude
```

Claude Code note: agentic tool use currently works through the middleware's best-effort ReAct **emulation backend** (see below). Copilot returns only text, so the proxy injects the tool schemas into the prompt and parses the model's reply back into `tool_calls`. It is functional but less reliable than a model with native function calling.

### VS Code

There are two ways to use the proxy inside VS Code.

#### 1. As a model in Copilot Chat (Bring Your Own Model)

This makes M365 Copilot appear in the VS Code Chat model picker, with tools and vision.

Edit `chatLanguageModels.json` in your VS Code user folder:

- Windows: `%APPDATA%\Code\User\chatLanguageModels.json`
- macOS: `~/Library/Application Support/Code/User/chatLanguageModels.json`
- Linux: `~/.config/Code/User/chatLanguageModels.json`

```json
{
  "name": "Custom Endpoint",
  "vendor": "customendpoint",
  "models": [
    {
      "id": "m365-opus:persist",
      "name": "M365 Opus 4.6 [200k] (proxy-default)",
      "url": "http://127.0.0.1:8000/v1/chat/completions",
      "toolCalling": true,
      "vision": true,
      "maxInputTokens": 200000,
      "maxOutputTokens": 16000
    }
  ]
}
```

Then reload VS Code and pick **M365 Opus** in the Chat model dropdown.

Notes:
- `maxInputTokens + maxOutputTokens` is the number VS Code shows as the context window (200k + 16k → "216k"). Keep `maxOutputTokens` modest and put the budget on input.
- Add more entries (e.g. `m365-gpt-think`) to switch models from the picker.
- **Images**: attach them as a **file** (drag a `.png` in, or use the attach button) — VS Code then sends the bytes as `image_url` and the proxy uploads them to Copilot. Pasting a screenshot from the clipboard is unreliable on custom endpoints (some builds drop it); a saved file always works.

#### 2. With the Claude Code extension

Point Claude Code at the proxy's Anthropic-compatible endpoint. In your workspace, create `.claude/settings.local.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
    "ANTHROPIC_API_KEY": "dummy"
  }
}
```

Claude Code then routes its requests through the proxy (using the tool middleware emulation backend). The same env vars work from a terminal (`$env:ANTHROPIC_BASE_URL = ...; claude`).

## Persistent Sessions

By default (`M365_PERSIST_DEFAULT=true`), the proxy maps **each client chat to one Copilot conversation** and keeps that mapping in SQLite. Substrate retains the thread under a reused conversation id, so after the first turn the proxy stops re-sending the prior transcript.

How the mapping key is chosen, in order of precedence:

1. `X-M365-Session-Id` header — an explicit, stable id you control (best when your client supports custom headers).
2. `M365_SESSION` environment variable — a process-level stable id for clients that cannot set custom headers; when set, temporary chat is disabled so the session can use Copilot history/memory.
3. `m365-...:persist` **plus** the OpenAI `user` field — one shared session per user.
4. Otherwise — an automatic per-chat fingerprint (project + first real user message), so distinct chats get distinct conversations.

```http
X-M365-Session-Id: my-work-session
```

```text
m365-opus:persist
```

> Clients that re-send their full history each turn (e.g. VS Code) provide no stable per-chat id, so the proxy keys on the first real user message — distinct chats stay separate, and the same chat stays together. Manage or reset mappings via the `/v1/chats` CRUD endpoints.

## Token Refresh

M365 Copilot browser tokens usually expire in about 1 hour. The proxy refreshes them from the dedicated signed-in browser window.

Auto-refresh is on by default:

```powershell
uv run copilot-openai-proxy serve
```

Useful controls:

```powershell
uv run copilot-openai-proxy serve --refresh-before-seconds 300
uv run copilot-openai-proxy serve --no-auto-refresh
uv run copilot-openai-proxy serve --no-capture-on-start
uv run copilot-openai-proxy serve --no-launch-edge
```

You can also press `r` in the server console to refresh the token manually.

### Manual Fallback

```powershell
uv run copilot-openai-proxy set-token
```

Then paste a fresh Substrate WebSocket URL:

1. Open the signed-in M365 Copilot browser window.
2. Open DevTools (`F12`) -> **Network** tab.
3. Filter by `substrate`.
4. Click the WebSocket entry.
5. Go to **Headers** -> right-click the **Request URL** -> **Copy link address**.
6. Paste it into the terminal.

The command extracts `access_token` automatically and writes it to `.env`.

## Token Health

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:8000/v1/token/status
```

Example:

```json
{
  "status": "ok",
  "token": {
    "valid": true,
    "expires_at": "2026-05-14T02:50:53+00:00",
    "seconds_remaining": 4200
  }
}
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /healthz` | Service health plus token status |
| `GET /v1/token/status` | Token validity, expiry time, and seconds remaining |
| `GET /v1/models` | OpenAI-compatible model list (the picker variants) |
| `POST /v1/chat/completions` | OpenAI Chat Completions, streaming + tools + vision |
| `POST /v1/responses` | OpenAI Responses API, streaming supported |
| `POST /v1/messages` | Anthropic Messages API style endpoint, tools + vision |
| `GET /v1/chats` | List tracked conversation mappings |
| `POST /v1/chats` | Create a conversation mapping |
| `GET /v1/chats/{key}` | Get one mapping |
| `PATCH /v1/chats/{key}` | Update label / rotate to a fresh conversation |
| `DELETE /v1/chats/{key}` | Forget a mapping |
| `GET /docs`, `GET /openapi.json` | Interactive OpenAPI docs and schema |

### Tool middleware

Microsoft 365 Copilot returns plain text and has no native function calling. To let agentic clients work, the proxy routes OpenAI and Anthropic tool definitions through a protocol-neutral middleware layer. The default `emulation` backend injects the client's tool schemas into the prompt (with a strict sentinel-delimited output contract) and parses the model's reply back into OpenAI `tool_calls` / Anthropic `tool_use`. This is best-effort: the model may ignore the format, so the proxy verifies and asks for a correction. Send your tools as usual on `/v1/chat/completions`, `/v1/responses`, or `/v1/messages`.

The middleware has an explicit native backend seam for future real tool execution. `M365_TOOL_MIDDLEWARE_MODE=native` is intentionally separate from emulation and does not grant arbitrary local execution by itself. See [docs/TOOL_MIDDLEWARE.md](docs/TOOL_MIDDLEWARE.md) for modes, internal models, security boundaries, and unsupported areas.

### Vision

Send images the normal OpenAI way — an `image_url` content part with a `data:` base64 URI (Anthropic `image`/`source` base64 also works). The proxy uploads each image to Copilot (`UploadFile`) and references it in the prompt. VS Code's file attachments are supported (it sends them as `image_url`, or as a local `file://` reference that the proxy reads off disk). Only images in the current turn are uploaded.

### Session management

Each client chat maps to one Copilot conversation. The mapping key is, in order of precedence: the `X-M365-Session-Id` header, then the `M365_SESSION` environment variable, then `:persist` + the OpenAI `user` field, then an automatic per-chat fingerprint. Mappings are persisted in SQLite so chats survive a proxy restart, and can be listed / relabelled / rotated / deleted via the `/v1/chats` endpoints above.

## More Examples

### Streaming

```powershell
$body = @{
  model = "m365-copilot"
  stream = $true
  messages = @(@{ role = "user"; content = "hi" })
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/v1/chat/completions" `
  -ContentType "application/json" `
  -Body $body
```

### Persistent Session

```powershell
$body = @{
  model = "m365-copilot"
  messages = @(
    @{ role = "user"; content = "Remember this code word: sakura. Reply only OK." }
  )
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/v1/chat/completions" `
  -Headers @{ "X-M365-Session-Id" = "test1" } `
  -ContentType "application/json" `
  -Body $body
```

### Anthropic-Style Messages

```powershell
$body = @{
  model = "m365-copilot"
  system = "Be concise."
  messages = @(@{ role = "user"; content = "hi" })
} | ConvertTo-Json -Depth 10

$r = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/v1/messages" `
  -ContentType "application/json" `
  -Body $body

$r.content[0].text
```

## Security Notes

- The proxy listens on `127.0.0.1` by default.
- The browser token is stored locally in `.env`.
- `.env`, `.venv/`, Python cache files, `*.har` captures, and `debug.log` are ignored by Git. HAR captures and debug logs can contain tokens, cookies, and tenant data — never commit them.
- The proxy does not send your token to any external service besides Microsoft 365 Copilot's own `substrate.office.com` endpoint.
- Anyone who can read your `.env` can use the token until it expires. Treat it like a secret.
- Temporary chats (`M365_DISABLE_MEMORY=true`, default) keep proxy traffic out of your Copilot history, but the requests still hit Microsoft's servers — this is normal Copilot use, not anonymisation.

## Environment Variables

Most users only need `.env` after the proxy captures a token.

| Variable | Default | Description |
|---|---|---|
| `M365_ACCESS_TOKEN` | optional at startup | Browser WebSocket token. If missing, startup capture can fill `.env`. |
| `M365_TIME_ZONE` | `Asia/Tokyo` | Time zone sent to Copilot. |
| `M365_MODEL_ALIAS` | `m365-copilot` | Model name returned as the alias by `/v1/models`. |
| `M365_WORK_GROUNDING` | `true` | `true` = Work grounding (enterprise data); `false` = Web only. Coding agents usually want `false`. |
| `M365_PERSIST_DEFAULT` | `true` | Auto-map each client chat to one Copilot conversation. |
| `M365_DISABLE_MEMORY` | `true` | Open every conversation as a temporary chat (`disableMemory=1`): no history, no memories. Ignored when `M365_SESSION` is set, because fixed sessions require memory/history. |
| `M365_SESSION` | empty | Process-level persistent session id used when `X-M365-Session-Id` is not supplied; also disables temporary chat so memory/history can attach to that session. |
| `M365_SESSION_DB` | `~/.m365-copilot-openai-proxy/sessions.db` | SQLite file for the conversation store. |
| `M365_SESSION_SALT` | random per process | Salt for the auto conversation fingerprint. Set it to keep keys stable across restarts. |
| `M365_RECV_TIMEOUT` | `90` | Seconds to wait for a substrate frame before giving up. |
| `M365_OPEN_TIMEOUT` | `30` | WebSocket handshake timeout (seconds). |
| `M365_EDGE_PATH` | Browser default path | Browser executable used for the debug token-capture window. |
| `M365_DEBUG` | unset | When set, writes request/response diagnostics to `debug.log`. |
| `M365_TOOL_MIDDLEWARE_ENABLED` | `true` | Enables the protocol-neutral tool middleware facade. Set `false` to bypass middleware without changing legacy emulation settings. |
| `M365_TOOL_MIDDLEWARE_MODE` | `emulation` | Tool middleware mode: `off`, `emulation`, `native`, or `auto`. `emulation` preserves current behavior; `native` is a separate backend seam; `auto` prefers native only when a configured backend can execute. |
| `M365_TOOL_EMULATION_ENABLED` | `true` | Enables the prompt/sentinel emulation backend used by default middleware mode. |
| `M365_TOOL_EMULATION_FORCE_NON_STREAMING` | `true` | Forces a non-streaming upstream turn while emulating tool calls, then wraps the result back into the requested streaming shape when needed. |

## Limitations

- This is an unofficial local proxy over the browser-facing M365 Copilot API, reverse-engineered from the web client. The captured protocol (in `substrate.json`) can break without notice.
- Token refresh depends on a signed-in browser profile.
- Tool calling currently defaults to best-effort prompt emulation, not native Copilot function calling — it can fail or misformat.
- Token usage numbers are placeholders.
- System prompts and prior conversation history are translated into plain text context.
- Vision depends on the client sending the image bytes; some clients only send a reference or nothing.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Token Automation Details

See [docs/TOKEN_REFRESH.md](docs/TOKEN_REFRESH.md) for the deeper browser CDP refresh notes and alternatives.

## Credits

This is a fork of [kuchris/m365-copilot-openai-proxy](https://github.com/kuchris/m365-copilot-openai-proxy), which provides the token capture, WebSocket bridge, and OpenAI/Anthropic-compatible output this build extends.
