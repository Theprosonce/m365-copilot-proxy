from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows-only module.
    msvcrt = None

import httpx
import uvicorn
import websockets

from .app import create_app
from .token_store import decode_jwt_payload, is_substrate_token_claims


class _SuppressCtrlC(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "CTRL+C" not in record.getMessage()


logging.getLogger("uvicorn.error").addFilter(_SuppressCtrlC())

_CDP_JS = """
(() => {
    const candidates = [];
    for (const store of [sessionStorage, localStorage]) {
        for (const key of ['LokiAuthToken', ...Object.keys(store).filter(k => k.startsWith('LokiAuthToken'))]) {
            const token = store.getItem(key);
            if (token && token.startsWith('eyJ')) candidates.push(token);
        }
    }
    for (const entry of performance.getEntriesByType('resource')) {
        if (!entry.name.includes('substrate.office.com') ||
            !entry.name.includes('access_token=')) continue;
        const match = entry.name.match(/[?&]access_token=([^&]+)/);
        if (match) candidates.push(decodeURIComponent(match[1]));
    }
    const stores = [sessionStorage, localStorage];
    for (const store of stores) {
        for (const k of Object.keys(store)) {
            if (!k.includes('accesstoken')) continue;
            try {
                const v = JSON.parse(store.getItem(k));
                if (v && v.secret && v.secret.startsWith('eyJ') &&
                    ((v.target && v.target.includes('substrate')) || k.includes('substrate'))) {
                    candidates.push(v.secret);
                }
            } catch {}
        }
    }
    return candidates;
})()
"""

_CDP_NUDGE_JS = """
(() => {
    const input = document.querySelector('[aria-label="Message Copilot"], textarea, [contenteditable="true"], [role="textbox"]');
    if (!input) return false;
    input.focus();
    return true;
})()
"""


async def _cdp_extract_token(port: int, *, allow_nudge: bool = True) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=1) as client:
            tabs = (await client.get(f"http://localhost:{port}/json")).json()
    except Exception:
        return None

    tab = _find_m365_page(tabs)
    if not tab:
        return None

    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"]) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": _CDP_JS}}))
            result = json.loads(await ws.recv())
            candidates = result.get("result", {}).get("result", {}).get("value") or []
            for token in candidates:
                if _is_substrate_token(token):
                    return token
            if not allow_nudge:
                return None
            return await _cdp_nudge_and_wait_for_token(ws)
    except Exception:
        return None


async def _cdp_capture_websocket_token(port: int, timeout_seconds: int) -> str | None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                tabs = (await client.get(f"http://localhost:{port}/json")).json()
        except Exception:
            await asyncio.sleep(1)
            continue

        tab = _find_m365_page(tabs)
        if not tab:
            await asyncio.sleep(1)
            continue

        try:
            async with websockets.connect(tab["webSocketDebuggerUrl"]) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
                # Reload the page so the app deterministically opens a fresh authenticated
                # websocket (an idle tab won't create one on its own).
                await ws.send(json.dumps({"id": 2, "method": "Page.enable"}))
                await ws.send(json.dumps({"id": 3, "method": "Page.reload", "params": {"ignoreCache": False}}))
                token = await _wait_for_substrate_websocket_token(ws, deadline)
                if token:
                    return token
        except Exception:
            await asyncio.sleep(1)
            continue
    return None


async def _wait_for_substrate_websocket_token(ws, deadline: float) -> str | None:
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        if msg.get("method") != "Network.webSocketCreated":
            continue
        url = msg.get("params", {}).get("url", "")
        if "substrate.office.com" not in url:
            continue
        match = re.search(r"[?&]access_token=([^&]+)", url)
        if not match:
            continue
        token = match.group(1)
        if _is_substrate_token(token):
            return token
    return None


def _find_m365_page(tabs: list[dict]) -> dict | None:
    return next(
        (
            tab for tab in tabs
            if tab.get("type") == "page"
            and tab.get("url", "").startswith("https://m365.cloud.microsoft/")
        ),
        None,
    )


def _wait_for_m365_page(cdp_port: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=1) as client:
                tabs = client.get(f"http://localhost:{cdp_port}/json").json()
        except Exception:
            time.sleep(0.5)
            continue
        if _find_m365_page(tabs):
            return True
        time.sleep(0.5)
    return False


def _capture_token_to_env(cdp_port: int, timeout_seconds: int) -> bool:
    token = asyncio.run(_cdp_capture_websocket_token(cdp_port, timeout_seconds))
    if not token:
        return False
    _write_token(token)
    return True


def _needs_substrate_token(token: str | None) -> bool:
    if not token or not _is_substrate_token(token):
        return True
    try:
        return _seconds_remaining(token) <= 0
    except Exception:
        return True


def _startup_capture_loop(cdp_port: int, timeout_seconds: int) -> None:
    print("Waiting for the debug Edge M365 tab...")
    _wait_for_m365_page(cdp_port, min(timeout_seconds, 30))
    print("Trying to refresh Substrate token from the debug Edge tab...")
    if _try_auto_refresh(cdp_port):
        return
    print("Waiting for a Substrate token from the debug Edge M365 Copilot tab...")
    print("If needed: press F5 in Copilot, click the message box, and type one character.")
    if _capture_token_to_env(cdp_port, timeout_seconds):
        print(".env updated with Substrate token.")
    else:
        print("Startup token capture timed out. Manual set-token is still available.")

async def _cdp_nudge_and_wait_for_token(ws) -> str | None:
    await ws.send(json.dumps({"id": 2, "method": "Network.enable"}))
    await ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {"expression": _CDP_NUDGE_JS}}))
    await ws.send(json.dumps({"id": 4, "method": "Input.insertText", "params": {"text": " "}}))
    await ws.send(json.dumps({
        "id": 5,
        "method": "Input.dispatchKeyEvent",
        "params": {
            "type": "keyDown",
            "windowsVirtualKeyCode": 8,
            "nativeVirtualKeyCode": 8,
            "key": "Backspace",
            "code": "Backspace",
        },
    }))
    await ws.send(json.dumps({
        "id": 6,
        "method": "Input.dispatchKeyEvent",
        "params": {
            "type": "keyUp",
            "windowsVirtualKeyCode": 8,
            "nativeVirtualKeyCode": 8,
            "key": "Backspace",
            "code": "Backspace",
        },
    }))
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1)
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        if msg.get("method") != "Network.webSocketCreated":
            continue
        url = msg.get("params", {}).get("url", "")
        if "substrate.office.com" not in url:
            continue
        match = re.search(r"[?&]access_token=([^&]+)", url)
        if not match:
            continue
        token = match.group(1)
        if _is_substrate_token(token):
            return token
    return None


def _is_substrate_token(token: str) -> bool:
    try:
        claims = decode_jwt_payload(token)
    except Exception:
        return False
    return is_substrate_token_claims(claims)


_SUBSTRATE_SCOPE = "https://substrate.office.com/sydney/.default"

_MSAL_READ_JS = r"""
(() => {
  let rt = null, sub = null;
  for (const k of Object.keys(localStorage)) {
    const v = localStorage.getItem(k) || "";
    if (k.includes("refreshtoken")) { try { const o = JSON.parse(v); if (o && o.secret) rt = o; } catch (e) {} }
    if (k.includes("accesstoken") && k.includes("substrate.office.com/sydney")) { try { sub = JSON.parse(v); } catch (e) {} }
  }
  return { rt, sub };
})()
"""


async def _read_msal_via_cdp(port: int) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            tabs = (await client.get(f"http://localhost:{port}/json")).json()
    except Exception:
        return None
    tab = _find_m365_page(tabs)
    if not tab:
        return None
    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": _MSAL_READ_JS, "returnByValue": True}}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == 1:
                    return r.get("result", {}).get("result", {}).get("value")
    except Exception:
        return None


def _mint_substrate(refresh_token: str, tenant: str, client_id: str) -> tuple[str, str] | None:
    """Exchange a (FOCI) refresh token for a fresh substrate/sydney access token. Returns (access, new_refresh)."""
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": _SUBSTRATE_SCOPE,
    }
    try:
        resp = httpx.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data=body,
            headers={"Origin": "https://m365.cloud.microsoft"},
            timeout=20,
        )
        j = resp.json()
    except Exception as exc:
        print(f"Mint failed: {exc}")
        return None
    access = j.get("access_token")
    if not access:
        print(f"Mint rejected: {j.get('error')} - {str(j.get('error_description', ''))[:160]}")
        return None
    return access, j.get("refresh_token") or refresh_token


def _foci_refresh_from_env() -> bool:
    rt = _read_env_value("M365_REFRESH_TOKEN")
    tenant = _read_env_value("M365_TENANT_ID")
    client_id = _read_env_value("M365_CLIENT_ID")
    if not (rt and tenant and client_id):
        return False
    minted = _mint_substrate(rt, tenant, client_id)
    if not minted:
        return False
    access, new_rt = minted
    _write_token(access)
    if new_rt and new_rt != rt:
        _write_env_value("M365_REFRESH_TOKEN", new_rt)  # rotate
    return True


def _capture_refresh_token_via_cdp(cdp_port: int) -> bool:
    data = asyncio.run(_read_msal_via_cdp(cdp_port))
    if not data or not data.get("rt"):
        return False
    rt = data["rt"].get("secret")
    sub = data.get("sub") or {}
    client_id = sub.get("clientId") or data["rt"].get("clientId")
    tenant = sub.get("realm") or data["rt"].get("realm")
    if not (rt and client_id and tenant):
        return False
    _write_env_value("M365_REFRESH_TOKEN", rt)
    _write_env_value("M365_TENANT_ID", tenant)
    _write_env_value("M365_CLIENT_ID", client_id)
    print("Captured MSAL refresh token; minting substrate token via HTTP (no browser needed from now on).")
    return _foci_refresh_from_env()


def _try_auto_refresh(cdp_port: int, *, allow_nudge: bool = True) -> bool:
    # 1. Pure-HTTP mint from a stored refresh token (no browser required).
    if _foci_refresh_from_env():
        print("Token refreshed via refresh-token (HTTP).")
        return True
    # 2. Read the refresh token from the signed-in Edge once, then mint.
    if _capture_refresh_token_via_cdp(cdp_port):
        print("Token refreshed via refresh-token (HTTP).")
        return True
    # 3. Legacy browser capture (localStorage access token / websocket).
    token = asyncio.run(_cdp_extract_token(cdp_port, allow_nudge=allow_nudge))
    if not token:
        token = asyncio.run(_cdp_capture_websocket_token(cdp_port, 25))
    if not token:
        return False
    _write_token(token)
    print("Token refreshed automatically.")
    return True


def _read_token() -> str | None:
    env_path = Path(".env")
    if not env_path.exists():
        return None
    text = env_path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^M365_ACCESS_TOKEN=(.*)$", text)
    return match.group(1).strip().strip("\"'") if match else None


def _seconds_remaining(token: str) -> int:
    claims = decode_jwt_payload(token)
    return int(claims["exp"]) - int(time.time())


def _auto_refresh_loop(
    cdp_port: int,
    refresh_before_seconds: int,
    retry_seconds: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        token = _read_token()
        if not token:
            stop_event.wait(retry_seconds)
            continue

        try:
            remaining = _seconds_remaining(token)
        except Exception as exc:
            print(f"Auto-refresh skipped: cannot decode current token: {exc}")
            stop_event.wait(retry_seconds)
            continue

        if remaining > refresh_before_seconds:
            wait_seconds = min(remaining - refresh_before_seconds, 300)
            stop_event.wait(wait_seconds)
            continue

        print(f"Token expires in {max(remaining, 0)} seconds; refreshing from Edge...")
        if not _try_auto_refresh(cdp_port):
            print("Auto-refresh failed; will retry later.")
        stop_event.wait(retry_seconds)


def _write_token(token: str) -> None:
    _write_env_value("M365_ACCESS_TOKEN", token)


def _write_env_value(key: str, value: str) -> None:
    env_path = Path(".env")
    pattern = rf"(?m)^{re.escape(key)}=.*$"
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        if re.search(pattern, text):
            text = re.sub(pattern, f"{key}={value}", text)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + f"{key}={value}\n"
    else:
        text = f"{key}={value}\n"
    env_path.write_text(text, encoding="utf-8")


def _read_env_value(key: str) -> str | None:
    env_path = Path(".env")
    if not env_path.exists():
        return None
    match = re.search(rf"(?m)^{re.escape(key)}=(.*)$", env_path.read_text(encoding="utf-8"))
    return match.group(1).strip().strip("\"'") if match else None


def main() -> None:
    _attach_parent_console()
    parser = argparse.ArgumentParser(
        prog="copilot-openai-proxy",
        description="M365 Copilot <-> OpenAI/Anthropic proxy. Bare invocation defaults to 'serve'.",
    )
    # Not required: a bare invocation (e.g. double-clicking the .exe) defaults to `serve`.
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser(
        "set-token", help="paste a substrate access token or WebSocket URL into .env"
    ).set_defaults(func=set_token_command)
    capture_parser = subparsers.add_parser("capture-token", help="listen for a substrate token via Edge CDP")
    capture_parser.add_argument("--cdp-port", type=int, default=9222, help="Edge remote-debugging port (default: 9222)")
    capture_parser.add_argument("--timeout-seconds", type=int, default=60, help="give up after this many seconds (default: 60)")
    capture_parser.set_defaults(func=capture_token_command)

    launch_parser = subparsers.add_parser("launch-edge", help="open the dedicated debug Edge window for M365 Copilot")
    launch_parser.add_argument("--cdp-port", type=int, default=9222, help="Edge remote-debugging port (default: 9222)")
    launch_parser.set_defaults(func=launch_edge_command)

    serve_parser = subparsers.add_parser("serve", help="start the proxy server (default when no command is given)")
    serve_parser.add_argument("--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8000, help="listen port (default: 8000)")
    serve_parser.add_argument("--cdp-port", type=int, default=9222, help="Edge remote-debugging port (default: 9222)")
    serve_parser.add_argument("--no-auto-refresh", action="store_true", help="disable automatic token refresh")
    serve_parser.add_argument("--no-launch-edge", action="store_true", help="don't open a debug Edge window on start")
    serve_parser.add_argument("--no-capture-on-start", action="store_true", help="don't capture a token at startup")
    serve_parser.add_argument("--capture-timeout-seconds", type=int, default=180, help="startup token-capture timeout (default: 180)")
    serve_parser.add_argument(
        "--refresh-before-seconds", type=int,
        default=int(os.environ.get("M365_REFRESH_BEFORE", "900")),
        help="refresh the token this many seconds before expiry (default: 900 / env M365_REFRESH_BEFORE)",
    )
    serve_parser.add_argument("--refresh-retry-seconds", type=int, default=60, help="wait between refresh retries (default: 60)")
    serve_parser.add_argument(
        "--no-configure-clients", action="store_true",
        help="don't wire Claude Code/VS Code to the proxy on start (and don't undo on stop)",
    )
    serve_parser.set_defaults(func=serve_command)

    configure_parser = subparsers.add_parser(
        "configure", help="wire Claude Code (global env) + VS Code (custom endpoint) to the proxy"
    )
    configure_parser.add_argument("--undo", action="store_true", help="remove the proxy wiring instead of adding it")
    configure_parser.set_defaults(func=configure_command)

    subparsers.add_parser("tray", help="open the tray app (default when double-clicked)").set_defaults(func=tray_command)

    args = parser.parse_args()
    if not getattr(args, "command", None):
        # Bare run (double-click) -> tray app; fall back to console serve if the GUI deps are missing.
        try:
            from .tray_app import run_tray

            run_tray()
            return
        except Exception as exc:
            print(f"(tray app unavailable: {exc}; falling back to console serve)")
            args = parser.parse_args(["serve"])
    try:
        args.func(args)
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C (cleanup already ran in serve_command's finally) — no traceback.
        pass


def launch_edge_command(args: argparse.Namespace) -> None:
    _launch_debug_edge(args.cdp_port)


_DEFAULT_EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
_LINUX_BROWSER_PRIORITY = ("chromium", "chromium-browser", "google-chrome", "microsoft-edge")


def _resolve_debug_browser_path() -> str:
    configured = _read_env_value("M365_EDGE_PATH")
    if configured:
        return configured
    if os.name == "nt":
        return _DEFAULT_EDGE_PATH
    if sys.platform.startswith("linux"):
        for candidate in _LINUX_BROWSER_PRIORITY:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
    return _DEFAULT_EDGE_PATH


def _debug_browser_profile_dir(browser_path: str) -> Path:
    if sys.platform.startswith("linux") and browser_path.startswith("/snap/bin/"):
        return Path.home() / "m365-copilot-openai-proxy-browser-profile"
    return Path.home() / ".m365-copilot-openai-proxy" / "edge-profile"


def _launch_debug_edge(cdp_port: int) -> None:
    edge_path = _resolve_debug_browser_path()
    profile_dir = _debug_browser_profile_dir(edge_path)
    profile_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        edge_path,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
    ]
    if (os.environ.get("M365_EDGE_HEADLESS") or "").strip().lower() in ("1", "true", "yes", "on"):
        # Invisible refresh — works only if the profile is already signed in and the tenant
        # does not require interactive WAM re-auth. First sign-in must be done non-headless.
        argv += ["--headless=new", "--disable-gpu"]
    argv.append("https://m365.cloud.microsoft/chat")
    # Detach from this process's job object so Edge survives when the launcher (uv run) exits
    # or when `serve` is restarted — otherwise the job teardown kills the browser.
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x01000000 | 0x00000200  # DETACHED_PROCESS | BREAKAWAY_FROM_JOB | NEW_PROCESS_GROUP
    subprocess.Popen(argv, creationflags=flags, close_fds=True)
    print(f"Edge launched with remote debugging on port {cdp_port}.")
    print(f"Dedicated Edge profile: {profile_dir}")
    print("Sign in to M365 Copilot in that window once, then retry refresh.")


def set_token_command(_args) -> None:
    print("Paste the full WebSocket URL (or just the access_token value), then press Enter:")
    raw = input().strip()
    match = re.search(r"access_token=([^&\s]+)", raw)
    token = match.group(1) if match else raw
    if not token.startswith("eyJ"):
        print("Error: could not find a valid token. Make sure you copied the full WebSocket URL.")
        return
    if not _is_substrate_token(token):
        print("Error: token is not a substrate.office.com WebSocket token.")
        print("Copy the full wss://substrate.office.com/... URL from the Network WebSocket request.")
        return
    _write_token(token)
    print(".env updated.")


def capture_token_command(args: argparse.Namespace) -> None:
    print("Listening for a Substrate WebSocket token...")
    print("In the debug Edge M365 Copilot tab, click the message box and type one character. Do not need to send.")
    token = asyncio.run(_cdp_capture_websocket_token(args.cdp_port, args.timeout_seconds))
    if not token:
        print("Error: no Substrate WebSocket token captured before timeout.")
        return
    _write_token(token)
    print(".env updated with Substrate token.")


_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
_VSCODE_MODEL_ID = "m365-opus:persist"


def _vscode_models_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    user_dir = Path(appdata) / "Code" / "User"
    return user_dir / "chatLanguageModels.json" if user_dir.is_dir() else None


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _configure_clients(undo: bool, base_url: str = "http://127.0.0.1:8000") -> None:
    """Wire Claude Code (global) and VS Code to the proxy, or remove that wiring (undo).
    Only the keys/entries this manages are touched; the rest of each file is preserved."""
    # Claude Code global env (~/.claude/settings.json) -> routes Claude Code through the proxy.
    try:
        data = _load_json(_CLAUDE_SETTINGS, {})
        env_val = data.get("env")
        env = env_val if isinstance(env_val, dict) else {}
        if undo:
            if env.get("ANTHROPIC_BASE_URL") == base_url:
                env.pop("ANTHROPIC_BASE_URL", None)
                env.pop("ANTHROPIC_API_KEY", None)
        else:
            env["ANTHROPIC_BASE_URL"] = base_url
            env["ANTHROPIC_API_KEY"] = "dummy"
        if env:
            data["env"] = env
        else:
            data.pop("env", None)
        _CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        _CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        if undo:
            print(f"  - Claude Code: removed proxy env from {_CLAUDE_SETTINGS}")
        else:
            print(f"  + Claude Code: ANTHROPIC_BASE_URL={base_url} -> {_CLAUDE_SETTINGS}")
    except Exception as exc:  # never let config wiring break serve
        print(f"  ! Claude settings not updated: {exc}")

    # VS Code custom-endpoint model (%APPDATA%/Code/User/chatLanguageModels.json).
    vs = _vscode_models_path()
    if vs is not None:
        try:
            arr = _load_json(vs, [])
            if not isinstance(arr, list):
                arr = []
            ep = next((e for e in arr if isinstance(e, dict) and e.get("vendor") == "customendpoint"), None)
            if undo:
                if ep is not None:
                    ep["models"] = [m for m in ep.get("models", []) if m.get("id") != _VSCODE_MODEL_ID]
                    if not ep["models"]:
                        arr = [e for e in arr if e is not ep]
            else:
                model = {
                    "id": _VSCODE_MODEL_ID,
                    "name": "M365 Opus 4.6 [200k] (proxy-default)",
                    "url": f"{base_url}/v1/chat/completions",
                    "toolCalling": True,
                    "vision": True,
                    "maxInputTokens": 200000,
                    "maxOutputTokens": 16000,
                }
                if ep is None:
                    arr.append({"name": "Custom Endpoint", "vendor": "customendpoint", "models": [model]})
                else:
                    models = [m for m in ep.get("models", []) if m.get("id") != _VSCODE_MODEL_ID]
                    models.append(model)
                    ep["models"] = models
            vs.write_text(json.dumps(arr, indent=2, ensure_ascii=False), encoding="utf-8")
            if undo:
                print(f"  - VS Code: removed model {_VSCODE_MODEL_ID} from {vs}")
            else:
                print(f"  + VS Code: added model {_VSCODE_MODEL_ID} -> {vs}")
        except Exception as exc:
            print(f"  ! VS Code models not updated: {exc}")

    print(f"  client config {'removed' if undo else 'applied'} (Claude Code + VS Code)")


def configure_command(args: argparse.Namespace) -> None:
    _configure_clients(undo=args.undo)


def tray_command(_args: argparse.Namespace) -> None:
    from .tray_app import run_tray

    run_tray()


def _attach_parent_console() -> None:
    """The windowed build has no console. If launched from a terminal, attach to the parent
    console so CLI subcommands (serve/configure/--help/set-token) show output and read input.
    A double-click has no parent console, so this no-ops and the app stays a pure GUI."""
    if os.name != "nt":
        return
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            try:
                sys.stdin = open("CONIN$", "r", encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass


def serve_command(args: argparse.Namespace) -> None:
    base_url = f"http://{args.host}:{args.port}"
    wire = not getattr(args, "no_configure_clients", False)
    if wire:
        _configure_clients(undo=False, base_url=base_url)
    try:
        _run_server(args)
    finally:
        # Clean exits (q / Ctrl+C / window close) revert the wiring; a hard kill leaves it,
        # and the next `serve` re-applies it. So clients point at the proxy only while it runs.
        if wire:
            _configure_clients(undo=True, base_url=base_url)


def _run_server(args: argparse.Namespace) -> None:
    cdp_port: int = args.cdp_port
    while True:
        app = create_app()
        config = uvicorn.Config(app, host=args.host, port=args.port)
        server = uvicorn.Server(config)
        stop_auto_refresh = threading.Event()
        auto_refresh_thread = None
        capture_thread = None

        if not args.no_launch_edge:
            _launch_debug_edge(cdp_port)

        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        if not args.no_capture_on_start and _needs_substrate_token(_read_token()):
            capture_thread = threading.Thread(
                target=_startup_capture_loop,
                args=(cdp_port, args.capture_timeout_seconds),
                daemon=True,
            )
            capture_thread.start()
        if not args.no_auto_refresh:
            auto_refresh_thread = threading.Thread(
                target=_auto_refresh_loop,
                args=(
                    cdp_port,
                    args.refresh_before_seconds,
                    args.refresh_retry_seconds,
                    stop_auto_refresh,
                ),
                daemon=True,
            )
            auto_refresh_thread.start()

        while not server.started and thread.is_alive():
            time.sleep(0.05)
        auto_refresh_label = "off" if args.no_auto_refresh else "on"
        capture_label = "off" if args.no_capture_on_start else "on"
        print(
            f"\n  [q] quit    [r] refresh token"
            f"    auto-refresh: {auto_refresh_label}"
            f"    startup-capture: {capture_label}\n"
        )

        action = None
        # The [q]/[r] keyboard loop needs an interactive console. When launched without one
        # (redirected stdin, background, .bat with start /min), just run the server; the
        # auto-refresh thread keeps the token fresh regardless.
        kb_ok = bool(getattr(sys.stdin, "isatty", lambda: False)())
        try:
            while thread.is_alive():
                if kb_ok and msvcrt is not None:
                    try:
                        if msvcrt.kbhit():
                            key = msvcrt.getwch().lower()
                            if key == "q":
                                action = "quit"
                                server.should_exit = True
                                break
                            elif key == "r":
                                action = "refresh"
                                server.should_exit = True
                                break
                    except OSError:
                        kb_ok = False
                time.sleep(0.05)
        except KeyboardInterrupt:
            # Ctrl+C: ask uvicorn to stop, then fall through to the clean shutdown path so
            # serve_command's `finally` can undo the client wiring.
            action = "quit"
            server.should_exit = True

        stop_auto_refresh.set()
        thread.join(timeout=5)
        if auto_refresh_thread:
            auto_refresh_thread.join(timeout=1)
        if capture_thread:
            capture_thread.join(timeout=1)

        if action == "refresh":
            print("Refreshing token...")
            if not _try_auto_refresh(cdp_port):
                print("Auto-refresh failed (Edge not running with --remote-debugging-port).")
                print("Falling back to manual mode.")
                set_token_command(None)
            print("Restarting server...")
        else:
            break


if __name__ == "__main__":
    main()
