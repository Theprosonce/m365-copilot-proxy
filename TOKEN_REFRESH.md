# Token Refresh Automation Options

The `substrate.office.com` API requires a user JWT that expires in ~1 hour. Admin consent is blocked, so tokens cannot be obtained programmatically via MSAL device code flow. Browser automation is required.

## Current manual flow

```powershell
uv run copilot-openai-proxy set-token
# paste full WebSocket URL from DevTools → Network → substrate WebSocket → Headers
```

---

## Option A — Playwright (recommended)

Launch a hidden browser using the existing Edge user profile (already authenticated). Navigate to M365 Copilot, intercept the WebSocket connection, extract the token, update `.env`, restart the server.

**Pros:** fully automatic, works even if Edge is not open  
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

## Option B — Edge remote debugging (CDP)

Launch Edge once with the remote debugging flag, then connect to the running browser via CDP without opening a new one.

**One-time setup:** create an Edge shortcut with extra flag:
```
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
```

Then a script connects to `http://localhost:9222` and executes JS in the Copilot tab to extract the token.

**Pros:** lightweight, no new browser, uses `websockets` (already installed)  
**Cons:** Edge must always be launched with the debug flag; less reliable if tab is closed

---

## Option C — Windows WAM / MSAL broker

`msal` with `allow_broker=True` on Windows 10/11 uses the OS-level Web Account Manager. Investigated but **not viable** — WAM token caches are per-app and the `substrate.office.com` resource requires pre-authorization (`AADSTS65002`), which blocks even cached token reuse from external client IDs.

---

## Option D — Admin consent (cleanest long-term fix)

Ask the ALTEN IT admin to either:
1. Register a new Entra app and grant delegated Graph permissions (original approach), or
2. Grant admin consent for the `Microsoft Graph Command Line Tools` app (`14d82eec-...`)

Either removes the need for token automation entirely.
