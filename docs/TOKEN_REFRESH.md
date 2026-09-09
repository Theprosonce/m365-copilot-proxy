# Token Refresh Automation Options

The `substrate.office.com` API requires a user JWT that expires in ~1 hour. Admin consent is blocked, so tokens cannot be obtained programmatically via MSAL device code flow. Browser automation is required.

## Current manual flow

```powershell
uv run copilot-proxy-server set-token
# paste full WebSocket URL from DevTools → Network → substrate WebSocket → Headers
```

---

## Option A — Playwright (recommended)

Launch a hidden browser using the existing browser user profile (already authenticated). Navigate to M365 Copilot, intercept the WebSocket connection, extract the token, update `.env`, restart the server.

**Pros:** fully automatic, works even if the browser is not open  
**Cons:** requires `playwright` + `playwright install msedge`, takes ~5s per refresh

Implementation sketch:
```python
from playwright.async_api import async_playwright

async def get_fresh_token() -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="C:/Users/<user>/AppData/Local/Microsoft/Edge/User Data",
            channel="msedge",
            headless=True,
        )
        token = None
        page = await browser.new_page()
        async def on_websocket(ws):
            nonlocal token
            m = re.search(r"access_token=([^&]+)", ws.url)
            if m:
                token = m.group(1)
        page.on("websocket", on_websocket)
        await page.goto("https://m365.cloud.microsoft/chat")
        await page.wait_for_timeout(5000)
        await browser.close()
        return token
```

Schedule with `schedule` or `apscheduler` every 50 minutes.

---

## Option B — Browser remote debugging (CDP)

Launch a dedicated browser profile with the remote debugging flag, then connect to it via CDP.

**Start the server:**
```powershell
uv run copilot-proxy-server serve
```

By default, `serve` starts the bundled Docker Chromium service and stops it when the server exits. Docker Compose must be available or startup fails. Point the proxy at the published CDP endpoint:

```ini
# config.ini
[settings]
browser_cdp_url = http://127.0.0.1:9222
```

Then open [http://127.0.0.1:6080/vnc.html](http://127.0.0.1:6080/vnc.html), sign in to M365 Copilot once, and keep that tab/profile for refresh capture. To use an external browser instead, start with `--no-manage-chromium`.
The server reads tabs from `browser_cdp_url` (or falls back to `http://localhost:9222`) and extracts tokens from the Copilot tab.

`uv run copilot-proxy-server serve` starts an auto-refresh loop by default. It refreshes when the
current JWT has less than 5 minutes left.
If the current token is missing, expired, or not a Substrate token, `serve` first tries the same `r`-style
refresh from the current remote Chromium tab. If no Substrate token is available yet, it starts a one-shot
startup capture listener. Generate a new WebSocket by pressing `F5` in the remote Chromium Copilot tab, clicking
the message box, and typing one character. The message does not need to be sent.

Useful serve flags:
```powershell
uv run copilot-proxy-server serve --refresh-before-seconds 300
uv run copilot-proxy-server serve
uv run copilot-proxy-server serve --no-capture-on-start
uv run copilot-proxy-server serve --no-auto-refresh
```

**Pros:** lightweight, uses `websockets` (already installed), works even if the normal browser is already open  
**Cons:** requires a separate browser profile; less reliable if the Copilot tab is closed

---

## Option C — Windows WAM / MSAL broker

`msal` with `allow_broker=True` on Windows 10/11 uses the OS-level Web Account Manager. Investigated but **not viable** — WAM token caches are per-app and the `substrate.office.com` resource requires pre-authorization (`AADSTS65002`), which blocks even cached token reuse from external client IDs.

---

## Option D — Admin consent (cleanest long-term fix)

Ask the IT admin to either:
1. Register a new Entra app and grant delegated Graph permissions (original approach), or
2. Grant admin consent for the `Microsoft Graph Command Line Tools` app (`14d82eec-...`)

Either removes the need for token automation entirely.
