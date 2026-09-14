# Microsoft 365 Copilot OpenAI Proxy

Use Microsoft 365 Copilot through OpenAI-compatible clients, local scripts, and coding tools.

This project runs a local FastAPI proxy that talks to the same `substrate.office.com` WebSocket API used by the M365 Copilot web UI, then exposes it as OpenAI-style HTTP endpoints.

No Azure app registration. No admin consent. Sign in with your normal M365 Copilot browser session.

Extended with a model picker, vision, protocol-neutral tool translation, temporary chats, and a session-management API.

## Why Use This

- Use M365 Copilot from OpenAI-compatible clients
- Works with your existing signed-in Copilot web session
- Runs locally on `127.0.0.1` by default
- Auto-captures and refreshes the short-lived browser token
- **Model picker** — choose Claude Opus or GPT‑5.6 (quick / reasoning) via the model name
- **Work / Web grounding** toggle
- **Vision** — forwards images (OpenAI `image_url` base64 and VS Code attachments) to Copilot
- **Tool translation** — protocol-neutral normalization between OpenAI-compatible and Anthropic-compatible tool definitions; no prompt rewriting or local tool execution
- **Temporary chats** by default — proxy conversations are not saved to your Copilot history and produce no memories
- **Per-chat persistence** in SQLite, plus a CRUD API over the tracked conversations
- Supports OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages style requests
- Interactive **OpenAPI docs** at `/docs`

## Quick Start

This project supports Linux and macOS. Install dependencies and start the proxy:

```bash
./scripts/installer.sh --install
uv run copilot-proxy-server
```

The installer verifies Docker Compose, installs `uv` when needed, installs the locked project dependencies, builds the bundled Chromium/noVNC image, and validates both the noVNC and Chrome DevTools endpoints. Use `./scripts/installer.sh --uninstall` to remove project-managed containers, images, volumes, the virtual environment, and generated local state. Docker and `uv` remain installed because other projects may use them.

The server listens on `0.0.0.0:8000`. Local clients can use `http://127.0.0.1:8000`, while remote clients use `http://<server>:8000`. By default, the bare command starts the bundled Docker Chromium service and stops it when the proxy exits.

On first run, open the Chromium noVNC page at `http://127.0.0.1:6080/vnc.html` locally or `http://<server>:6080/vnc.html` remotely, sign in to M365 Copilot, and keep the Copilot tab open. TCP ports 8000 and 6080 must be allowed by the server firewall for remote client and noVNC access. Chrome DevTools remains restricted to `127.0.0.1:9222` and is used internally by the proxy. The proxy captures the Substrate token from that CDP session and writes it to `.env` as `M365_ACCESS_TOKEN`.

To use an externally managed browser instead, disable container management:

```bash
uv run copilot-proxy-server serve --no-manage-chromium
```

### Project scripts

- `scripts/installer.sh --install`: install and validate first-run prerequisites.
- `scripts/installer.sh --uninstall`: remove project-managed runtime resources and local state.
- `scripts/run.sh`: foreground development run.
- `scripts/proxy.sh`: background toggle with optional `--reinstall`.
- `scripts/opencode.sh`: add or remove the OpenCode provider configuration.

```bash
./scripts/proxy.sh
./scripts/proxy.sh --reinstall
```

## Test It

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"m365-copilot","messages":[{"role":"user","content":"Say hello in one short sentence."}]}'
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
| `m365-gpt-5.5-quick` | GPT‑5.5 (quick) |
| `m365-gpt-5.5-think` | GPT‑5.5 (reasoning) |
| `m365-gpt-5.6-quick`, `m365-gpt` | GPT‑5.6 (quick) |
| `m365-gpt-5.6-think`, `m365-gpt-reasoning` | GPT‑5.6 (reasoning) |

Append `:persist` to any id (e.g. `m365-opus:persist`) to reuse one Copilot conversation per chat.

### **OpenCode**

#### Option A: Temporary
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


Setup:

```bash
chmod +x scripts/opencode.sh
./scripts/opencode.sh
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

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8000"
export ANTHROPIC_API_KEY="dummy"
claude
```

Claude Code note: this minimum build forwards normal Anthropic-compatible request content to the proxy and keeps tool definitions as native protocol fields where supported. It does not rewrite prompts with tool schemas or locally execute tools.

### VS Code

There are two ways to use the proxy inside VS Code.

#### 1. As a model in Copilot Chat (Bring Your Own Model)

This makes M365 Copilot appear in the VS Code Chat model picker, with tools and vision.

Edit `chatLanguageModels.json` in your VS Code user folder:

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
- Add more entries (e.g. `m365-gpt-5.6-think`) to switch models from the picker.
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

Claude Code then routes its requests through the proxy. The same env vars work from a terminal (`$env:ANTHROPIC_BASE_URL = ...; claude`).

## Persistent Sessions

By default (`persist_default = true` in `config.ini`), the proxy maps **each client chat to one Copilot conversation** and keeps that mapping in SQLite. Substrate retains the thread under a reused conversation id, so after the first turn the proxy stops re-sending the prior transcript.

How the mapping key is chosen, in order of precedence:

1. `X-M365-Session-Id` header — an explicit, stable id you control (best when your client supports custom headers).
2. `session_id` in `config.ini` — a process-level stable id for clients that cannot set custom headers; when set, temporary chat is disabled so the session can use Copilot history/memory.
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

```bash
uv run copilot-proxy-server serve
```

Useful controls:

```bash
uv run copilot-proxy-server serve --refresh-before-seconds 300
uv run copilot-proxy-server serve --no-auto-refresh
uv run copilot-proxy-server serve --no-capture-on-start
uv run copilot-proxy-server serve
```

You can also press `r` in the server console to refresh the token manually.

### Manual Fallback

```bash
uv run copilot-proxy-server set-token
```

Then paste a fresh Substrate WebSocket URL:

1. Open the signed-in M365 Copilot browser window.
2. Open DevTools (`F12`) -> **Network** tab.
3. Filter by `substrate`.
4. Click the WebSocket entry.
5. Go to **Headers** -> right-click the **Request URL** -> **Copy link address**.
6. Paste it into the terminal.

The command extracts `access_token` automatically and writes it to `.env` as `M365_ACCESS_TOKEN`.

## Token Health

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/v1/token/status
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

### Tool translation

The minimum build keeps tool handling deliberately small: OpenAI-compatible and Anthropic-compatible tool definitions are normalized at the request edge and translated between protocol shapes where needed. The proxy does not rewrite prompts with tool instructions, parse model text for synthetic tool calls, or execute tools locally.


### Vision

Send images the normal OpenAI way — an `image_url` content part with a `data:` base64 URI (Anthropic `image`/`source` base64 also works). The proxy uploads each image to Copilot (`UploadFile`) and references it in the prompt. VS Code's file attachments are supported (it sends them as `image_url`, or as a local `file://` reference that the proxy reads off disk). Only images in the current turn are uploaded.

### Session management

Each client chat maps to one Copilot conversation. The mapping key is, in order of precedence: the `X-M365-Session-Id` header, then the `session_id` config value in `config.ini`, then `:persist` + the OpenAI `user` field, then an automatic per-chat fingerprint. Mappings are persisted in SQLite so chats survive a proxy restart, and can be listed / relabelled / rotated / deleted via the `/v1/chats` endpoints above.

## More Examples

### Streaming

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"m365-copilot","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

### Persistent Session

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-M365-Session-Id: test1' \
  -d '{"model":"m365-copilot","messages":[{"role":"user","content":"Remember this code word: sakura. Reply only OK."}]}'
```

### Anthropic-Style Messages

```bash
curl -s http://127.0.0.1:8000/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"m365-copilot","system":"Be concise.","messages":[{"role":"user","content":"hi"}]}'
```

## Security Notes

- The proxy listens on `0.0.0.0` by default. Local clients use `127.0.0.1`; remote clients use the server hostname or IP address.
- The browser token is stored locally in `.env` as `M365_ACCESS_TOKEN`.
- `config.ini`, `.env`, `.venv/`, `.sessions/`, Python cache files, and `*.har` captures are ignored by Git. HAR captures and debug logs can contain tokens, cookies, and tenant data — never commit them.
- The proxy does not send your token to any external service besides Microsoft 365 Copilot's own `substrate.office.com` endpoint.
- Anyone who can read your `.env` can use the token until it expires. Treat it like a secret.
- Temporary chats (`disable_memory = true` in `config.ini`, default) keep proxy traffic out of your Copilot history, but the requests still hit Microsoft's servers — this is normal Copilot use, not anonymisation.

## Configuration

Non-secret configuration is done via `config.ini` in the current working directory or the project root. The Substrate access token is stored only in `.env` as `M365_ACCESS_TOKEN`.

For a comprehensive guide to all configuration sections, variables, and usage details, please see [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Limitations

- This is an unofficial local proxy over the browser-facing M365 Copilot API, reverse-engineered from the web client. The captured protocol (in `substrate.json`) can break without notice.
- Token refresh depends on a signed-in browser profile.
- Tool definitions are translated between supported request shapes, but this build does not synthesize tool calls from plain model text.
- Token usage numbers are placeholders.
- System prompts and prior conversation history are translated into plain text context.
- Vision depends on the client sending the image bytes; some clients only send a reference or nothing.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Token Automation Details

See [docs/TOKEN_REFRESH.md](docs/TOKEN_REFRESH.md) for the deeper browser CDP refresh notes and alternatives.

## Credits

Original fork of [kuchris/m365-copilot-openai-proxy](https://github.com/kuchris/m365-copilot-openai-proxy), which provides the token capture, WebSocket bridge, and OpenAI/Anthropic-compatible output this build extends.

Thanks to fork [MassimilianoPili/m365-copilot-proxy](https://github.com/MassimilianoPili/m365-copilot-proxy),
that provides model selection, vision, tool-calling shim, temporary chats, and a session-management API.