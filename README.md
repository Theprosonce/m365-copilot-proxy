# Microsoft 365 Copilot OpenAI Proxy

A local proxy server that exposes your company's Microsoft 365 Copilot as an OpenAI-compatible API. No Azure app registration or admin consent required.

## How it works

The proxy connects to `substrate.office.com` — the same WebSocket API the M365 Copilot web UI uses — and wraps it in an OpenAI-compatible HTTP server. Authentication uses a short-lived token extracted from your browser session.

## Endpoints

- `GET /healthz`
- `GET /v1/models`
- `POST /v1/chat/completions` — OpenAI Chat Completions (streaming supported)
- `POST /v1/responses` — OpenAI Responses API (streaming supported)
- `POST /v1/messages` — Anthropic Messages API (non-streaming)

## Constraints

- Token expires in ~1 hour and must be refreshed manually.
- Each request starts a new Copilot conversation (no persistent sessions).
- System prompts and conversation history are folded into the message as plain text.
- Tool calls and token usage are not supported.
- **Claude Code:** Agentic features (file reading, bash, code editing) require tool use, which this proxy does not support. Use the proxy for general Q&A only; keep Claude Code on the real Anthropic API for coding tasks.

---

## Setup

### 1. Install

```powershell
uv sync
```

### 2. Get your token

1. Open [https://m365.cloud.microsoft/chat](https://m365.cloud.microsoft/chat) in Edge or Chrome and sign in.
2. Open DevTools (`F12`) → **Network** tab.
3. Type anything in Copilot and send it.
4. Filter by `substrate` — click the WebSocket entry (`e85750e2-...`).
5. Go to **Headers** → right-click the **Request URL** → **Copy link address**.

### 3. Save the token

```powershell
uv run copilot-openai-proxy set-token
```

Paste the copied WebSocket URL when prompted. The token is extracted automatically and written to `.env`.

### 4. Start the server

```powershell
uv run copilot-openai-proxy serve
```

Server runs at `http://127.0.0.1:8000` by default.

```powershell
uv run copilot-openai-proxy serve --host 127.0.0.1 --port 8000
```

---

## Token refresh

Tokens expire in ~1 hour. When the server returns a `502` with an expiry message, repeat steps 2–3 above:

```powershell
uv run copilot-openai-proxy set-token
```

Then restart the server.

---

## Using with AI coding tools

### OpenCode

```powershell
$env:OPENAI_BASE_URL = "http://127.0.0.1:8000"
$env:OPENAI_API_KEY = "dummy"
opencode
```

Select **OpenAI API** as the provider. Model: `m365-copilot`.

### Continue (VS Code extension)

Add to `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "M365 Copilot",
      "provider": "openai",
      "model": "m365-copilot",
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

### Any OpenAI-compatible client

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8000/v1` |
| API Key | `dummy` |
| Model | `m365-copilot` |

---

## Manual API examples

### Chat Completions

```powershell
$body = @{
  model = "m365-copilot"
  messages = @(
    @{ role = "system"; content = "Be concise." },
    @{ role = "user"; content = "hi" }
  )
} | ConvertTo-Json -Depth 10

$r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/chat/completions" -ContentType "application/json" -Body $body
$r.choices[0].message.content
```

### Streaming

```powershell
$body = @{
  model = "m365-copilot"
  stream = $true
  messages = @(@{ role = "user"; content = "hi" })
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/chat/completions" -ContentType "application/json" -Body $body
```

### Anthropic-style

```powershell
$body = @{
  model = "m365-copilot"
  system = "Be concise."
  messages = @(@{ role = "user"; content = "hi" })
} | ConvertTo-Json -Depth 10

$r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/messages" -ContentType "application/json" -Body $body
$r.content[0].text
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `M365_ACCESS_TOKEN` | required | Bearer token from browser WebSocket URL |
| `M365_TIME_ZONE` | `Asia/Tokyo` | Time zone sent with each request |
| `M365_MODEL_ALIAS` | `m365-copilot` | Model name returned by `/v1/models` |

---

## Token automation (future)

See [TOKEN_REFRESH.md](TOKEN_REFRESH.md) for options to automate token refresh (Playwright, Edge CDP).
