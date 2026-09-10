from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import uvicorn
import websockets

from .app import create_app
from .config import Settings, read_config_value, write_config_value
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


def _cdp_base_url(port: int) -> str:
    configured = Settings().browser_cdp_url.strip()
    return configured.rstrip("/") if configured else f"http://localhost:{port}"


def _cdp_http_url(port: int, path: str) -> str:
    return f"{_cdp_base_url(port)}/{path.lstrip('/')}"


def _cdp_websocket_url(port: int, ws_url: str) -> str:
    configured = Settings().browser_cdp_url.strip()
    ws = urlsplit(ws_url)
    if configured:
        cdp = urlsplit(configured)
        scheme = "wss" if cdp.scheme == "https" else "ws"
        return urlunsplit((scheme, cdp.netloc, ws.path, ws.query, ws.fragment))

    # Docker Chromium commonly reports 0.0.0.0 in webSocketDebuggerUrl.
    # Normalize to localhost so host-side clients can connect like direct browser mode.
    if ws.hostname in {"0.0.0.0", "::"}:
        netloc = f"localhost:{port}"
        return urlunsplit((ws.scheme or "ws", netloc, ws.path, ws.query, ws.fragment))
    return ws_url


def _cdp_debug_tabs(port: int) -> list[dict] | None:
    try:
        with httpx.Client(timeout=1) as client:
            return client.get(_cdp_http_url(port, "/json")).json()
    except Exception:
        return None


def _edge_debug_tabs(cdp_port: int) -> list[dict] | None:
    """Compatibility alias for older call sites."""
    return _cdp_debug_tabs(cdp_port)


async def _cdp_extract_token(port: int, *, allow_nudge: bool = True) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=1) as client:
            tabs = (await client.get(_cdp_http_url(port, "/json"))).json()
    except Exception:
        return None

    tab = _find_m365_page(tabs)
    if not tab:
        return None

    try:
        async with websockets.connect(_cdp_websocket_url(port, tab["webSocketDebuggerUrl"])) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {"expression": _CDP_JS},
                    }
                )
            )
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
                tabs = (await client.get(_cdp_http_url(port, "/json"))).json()
        except Exception:
            await asyncio.sleep(1)
            continue

        tab = _find_m365_page(tabs)
        if not tab:
            await asyncio.sleep(1)
            continue

        try:
            async with websockets.connect(_cdp_websocket_url(port, tab["webSocketDebuggerUrl"])) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
                # Reload the page so the app deterministically opens a fresh authenticated
                # websocket (an idle tab won't create one on its own).
                await ws.send(json.dumps({"id": 2, "method": "Page.enable"}))
                await ws.send(
                    json.dumps(
                        {
                            "id": 3,
                            "method": "Page.reload",
                            "params": {"ignoreCache": False},
                        }
                    )
                )
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
            tab
            for tab in tabs
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
                tabs = client.get(_cdp_http_url(cdp_port, "/json")).json()
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
    print("Waiting for the remote Chromium M365 tab...")
    _wait_for_m365_page(cdp_port, min(timeout_seconds, 30))
    print("Trying to refresh Substrate token from the remote Chromium tab...")
    if _try_auto_refresh(cdp_port):
        return
    print("Waiting for a Substrate token from the remote Chromium M365 Copilot tab...")
    print(
        "If needed: press F5 in Copilot, click the message box, and type one character."
    )
    if _capture_token_to_env(cdp_port, timeout_seconds):
        print(".env updated with Substrate token.")
    else:
        print("Startup token capture timed out. Manual set-token is still available.")


async def _cdp_nudge_and_wait_for_token(ws) -> str | None:
    await ws.send(json.dumps({"id": 2, "method": "Network.enable"}))
    await ws.send(
        json.dumps(
            {
                "id": 3,
                "method": "Runtime.evaluate",
                "params": {"expression": _CDP_NUDGE_JS},
            }
        )
    )
    await ws.send(
        json.dumps({"id": 4, "method": "Input.insertText", "params": {"text": " "}})
    )
    await ws.send(
        json.dumps(
            {
                "id": 5,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyDown",
                    "windowsVirtualKeyCode": 8,
                    "nativeVirtualKeyCode": 8,
                    "key": "Backspace",
                    "code": "Backspace",
                },
            }
        )
    )
    await ws.send(
        json.dumps(
            {
                "id": 6,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyUp",
                    "windowsVirtualKeyCode": 8,
                    "nativeVirtualKeyCode": 8,
                    "key": "Backspace",
                    "code": "Backspace",
                },
            }
        )
    )
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
            tabs = (await client.get(_cdp_http_url(port, "/json"))).json()
    except Exception:
        return None
    tab = _find_m365_page(tabs)
    if not tab:
        return None
    try:
        async with websockets.connect(_cdp_websocket_url(port, tab["webSocketDebuggerUrl"]), max_size=None) as ws:
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {"expression": _MSAL_READ_JS, "returnByValue": True},
                    }
                )
            )
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == 1:
                    return r.get("result", {}).get("result", {}).get("value")
    except Exception:
        return None


def _mint_substrate(
    refresh_token: str, tenant: str, client_id: str
) -> tuple[str, str] | None:
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
        print(
            f"Mint rejected: {j.get('error')} - {str(j.get('error_description', ''))[:160]}"
        )
        return None
    return access, j.get("refresh_token") or refresh_token


def _foci_refresh_from_env() -> bool:
    rt = read_config_value("refresh_token")
    tenant = read_config_value("tenant_id")
    client_id = read_config_value("client_id")
    if not (rt and tenant and client_id):
        return False
    minted = _mint_substrate(rt, tenant, client_id)
    if not minted:
        return False
    access, new_rt = minted
    _write_token(access)
    if new_rt and new_rt != rt:
        write_config_value("refresh_token", new_rt)  # rotate
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
    write_config_value("refresh_token", rt)
    write_config_value("tenant_id", tenant)
    write_config_value("client_id", client_id)
    print(
        "Captured MSAL refresh token; minting substrate token via HTTP (no browser needed from now on)."
    )
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
    return Settings().access_token or None


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

        print(f"Token expires in {max(remaining, 0)} seconds; refreshing via remote CDP...")
        if not _try_auto_refresh(cdp_port):
            print("Auto-refresh failed; will retry later.")
        stop_event.wait(retry_seconds)


def _write_token(token: str) -> None:
    write_config_value("access_token", token)


def _write_env_value(key: str, value: str) -> None:
    write_config_value(key, value)


def _read_env_value(key: str) -> str | None:
    return read_config_value(key)


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="copilot-proxy-server",
        description="M365 Copilot <-> OpenAI/Anthropic proxy. Bare invocation defaults to 'serve'.",
    )
    # Not required: a bare invocation (e.g. double-clicking the .exe) defaults to `serve`.
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser(
        "set-token", help="paste a substrate access token or WebSocket URL into .env"
    ).set_defaults(func=set_token_command)
    capture_parser = subparsers.add_parser(
        "capture-token", help="listen for a substrate token via remote CDP"
    )
    capture_parser.add_argument(
        "--cdp-port",
        type=int,
        default=settings.capture_token_cdp_port,
        help=f"remote CDP port (default: {settings.capture_token_cdp_port})",
    )
    capture_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=settings.capture_token_timeout_seconds,
        help=f"give up after this many seconds (default: {settings.capture_token_timeout_seconds})",
    )
    capture_parser.set_defaults(func=capture_token_command)

    serve_parser = subparsers.add_parser(
        "serve", help="start the proxy server (default when no command is given)"
    )
    serve_parser.add_argument(
        "--host",
        default=settings.serve_host,
        help=f"listen address (default: {settings.serve_host})",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=settings.serve_port,
        help=f"listen port (default: {settings.serve_port})",
    )
    serve_parser.add_argument(
        "--cdp-port",
        type=int,
        default=settings.serve_cdp_port,
        help=f"remote CDP port (default: {settings.serve_cdp_port})",
    )
    serve_parser.add_argument(
        "--auto-refresh",
        dest="auto_refresh",
        action=argparse.BooleanOptionalAction,
        default=settings.serve_auto_refresh,
        help=f"enable automatic token refresh (default: {settings.serve_auto_refresh})",
    )
    serve_parser.add_argument(
        "--capture-on-start",
        dest="capture_on_start",
        action=argparse.BooleanOptionalAction,
        default=settings.serve_capture_on_start,
        help=f"capture a token at startup (default: {settings.serve_capture_on_start})",
    )
    serve_parser.add_argument(
        "--capture-timeout-seconds",
        type=int,
        default=settings.serve_capture_timeout_seconds,
        help=f"startup token-capture timeout (default: {settings.serve_capture_timeout_seconds})",
    )
    serve_parser.add_argument(
        "--refresh-before-seconds",
        type=int,
        default=settings.serve_refresh_before_seconds,
        help=f"refresh the token this many seconds before expiry (default: {settings.serve_refresh_before_seconds})",
    )
    serve_parser.add_argument(
        "--refresh-retry-seconds",
        type=int,
        default=settings.serve_refresh_retry_seconds,
        help=f"wait between refresh retries (default: {settings.serve_refresh_retry_seconds})",
    )
    serve_parser.add_argument(
        "--configure-clients",
        dest="configure_clients",
        action=argparse.BooleanOptionalAction,
        default=settings.serve_configure_clients,
        help=f"wire Claude Code/VS Code to the proxy on start (default: {settings.serve_configure_clients})",
    )
    serve_parser.add_argument(
        "--manage-chromium",
        dest="manage_chromium",
        action=argparse.BooleanOptionalAction,
        default=settings.serve_manage_chromium,
        help=f"manage docker chromium lifecycle with serve (default: {settings.serve_manage_chromium})",
    )
    serve_parser.set_defaults(func=serve_command)

    configure_parser = subparsers.add_parser(
        "configure",
        help="wire Claude Code (global env) + VS Code (custom endpoint) to the proxy",
    )
    configure_parser.add_argument(
        "--undo",
        action="store_true",
        default=settings.configure_undo,
        help=f"remove the proxy wiring instead of adding it (default: {settings.configure_undo})",
    )
    configure_parser.set_defaults(func=configure_command)

    args = parser.parse_args()
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve"])
    try:
        args.func(args)
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C (cleanup already ran in serve_command's finally) — no traceback.
        pass
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from None


def set_token_command(_args) -> None:
    print(
        "Paste the full WebSocket URL (or just the access_token value), then press Enter:"
    )
    raw = input().strip()
    match = re.search(r"access_token=([^&\s]+)", raw)
    token = match.group(1) if match else raw
    if not token.startswith("eyJ"):
        print(
            "Error: could not find a valid token. Make sure you copied the full WebSocket URL."
        )
        return
    if not _is_substrate_token(token):
        print("Error: token is not a substrate.office.com WebSocket token.")
        print(
            "Copy the full wss://substrate.office.com/... URL from the Network WebSocket request."
        )
        return
    _write_token(token)
    print(".env updated.")


def capture_token_command(args: argparse.Namespace) -> None:
    print("Listening for a Substrate WebSocket token...")
    print(
        "In the remote Chromium M365 Copilot tab, click the message box and type one character. No need to send."
    )
    token = asyncio.run(
        _cdp_capture_websocket_token(args.cdp_port, args.timeout_seconds)
    )
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
        _CLAUDE_SETTINGS.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if undo:
            print(f"  - Claude Code: removed proxy env from {_CLAUDE_SETTINGS}")
        else:
            print(
                f"  + Claude Code: ANTHROPIC_BASE_URL={base_url} -> {_CLAUDE_SETTINGS}"
            )
    except Exception as exc:  # never let config wiring break serve
        print(f"  ! Claude settings not updated: {exc}")

    # VS Code custom-endpoint model (%APPDATA%/Code/User/chatLanguageModels.json).
    vs = _vscode_models_path()
    if vs is not None:
        try:
            arr = _load_json(vs, [])
            if not isinstance(arr, list):
                arr = []
            ep = next(
                (
                    e
                    for e in arr
                    if isinstance(e, dict) and e.get("vendor") == "customendpoint"
                ),
                None,
            )
            if undo:
                if ep is not None:
                    ep["models"] = [
                        m
                        for m in ep.get("models", [])
                        if m.get("id") != _VSCODE_MODEL_ID
                    ]
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
                    arr.append(
                        {
                            "name": "Custom Endpoint",
                            "vendor": "customendpoint",
                            "models": [model],
                        }
                    )
                else:
                    models = [
                        m
                        for m in ep.get("models", [])
                        if m.get("id") != _VSCODE_MODEL_ID
                    ]
                    models.append(model)
                    ep["models"] = models
            vs.write_text(
                json.dumps(arr, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            if undo:
                print(f"  - VS Code: removed model {_VSCODE_MODEL_ID} from {vs}")
            else:
                print(f"  + VS Code: added model {_VSCODE_MODEL_ID} -> {vs}")
        except Exception as exc:
            print(f"  ! VS Code models not updated: {exc}")

    print(f"  client config {'removed' if undo else 'applied'} (Claude Code + VS Code)")


def configure_command(args: argparse.Namespace) -> None:
    _configure_clients(undo=args.undo)


def _resolve_compose_file() -> Path:
    candidates = [Path.cwd() / "compose.server.yml", Path(__file__).parents[2] / "compose.server.yml"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "Chromium lifecycle requires compose.server.yml, but no compose file was found in the current directory or project root."
    )


def _run_compose(*args: str) -> None:
    compose_file = _resolve_compose_file()
    cmd = ["docker", "compose", "-f", str(compose_file), *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Chromium lifecycle requires Docker Compose (`docker compose`), but `docker` is not available in PATH."
        ) from exc
    if result.returncode == 0:
        return

    detail = (result.stderr or result.stdout or "").strip()
    lowered = detail.lower()
    if "permission denied" in lowered and "docker api" in lowered:
        sg_cmd = ["sg", "docker", "-c", shlex.join(cmd)]
        try:
            sg_result = subprocess.run(sg_cmd, capture_output=True, text=True)
        except FileNotFoundError:
            sg_result = None
        if sg_result is not None:
            if sg_result.returncode == 0:
                return
            sg_detail = (sg_result.stderr or sg_result.stdout or "").strip()
            if sg_detail:
                detail = f"{detail}\nFallback via `sg docker` also failed:\n{sg_detail}"

    if detail:
        raise RuntimeError(
            f"Docker Compose command failed: {' '.join(cmd)}\n{detail}"
        )
    raise RuntimeError(f"Docker Compose command failed: {' '.join(cmd)}")


def _start_chromium_container() -> None:
    _run_compose("up", "-d", "chromium")


def _stop_chromium_container() -> None:
    _run_compose("stop", "chromium")


def serve_command(args: argparse.Namespace) -> None:
    base_url = f"http://{args.host}:{args.port}"
    wire = bool(getattr(args, "configure_clients", True))
    manage_chromium = bool(getattr(args, "manage_chromium", True))
    chromium_started = False
    previous_sigterm_handler = None

    def handle_sigterm(_signum, _frame) -> None:
        raise KeyboardInterrupt

    if wire:
        _configure_clients(undo=False, base_url=base_url)
    try:
        if manage_chromium:
            print("Starting docker chromium service...")
            try:
                _start_chromium_container()
                chromium_started = True
                print("Docker chromium service is running.")
            except RuntimeError as exc:
                if _wait_for_m365_page(args.cdp_port, 2):
                    print(
                        "Warning: Docker Chromium could not be started, but a reachable CDP browser was found; continuing with existing browser."
                    )
                    print(f"Docker startup error: {exc}")
                else:
                    raise
            if hasattr(signal, "SIGTERM"):
                previous_sigterm_handler = signal.signal(signal.SIGTERM, handle_sigterm)
        _run_server(args)
    finally:
        if previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
        if manage_chromium and chromium_started:
            try:
                print("Stopping docker chromium service...")
                _stop_chromium_container()
            except Exception as exc:
                print(f"Warning: failed to stop docker chromium service: {exc}")
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

        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        if args.capture_on_start and _needs_substrate_token(_read_token()):
            capture_thread = threading.Thread(
                target=_startup_capture_loop,
                args=(cdp_port, args.capture_timeout_seconds),
                daemon=True,
            )
            capture_thread.start()
        if args.auto_refresh:
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
        auto_refresh_label = "on" if args.auto_refresh else "off"
        capture_label = "on" if args.capture_on_start else "off"
        print(
            f"\n  [q] quit    [r] refresh token"
            f"    auto-refresh: {auto_refresh_label}"
            f"    startup-capture: {capture_label}\n"
        )

        action = None
        # Without an interactive terminal, run until stopped by a signal.
        kb_ok = bool(getattr(sys.stdin, "isatty", lambda: False)())
        try:
            while thread.is_alive():
                if kb_ok:
                    try:
                        import select

                        if select.select([sys.stdin], [], [], 0)[0]:
                            key = sys.stdin.read(1).lower()
                            if key == "q":
                                action = "quit"
                                server.should_exit = True
                                break
                            elif key == "r":
                                action = "refresh"
                                server.should_exit = True
                                break
                    except (OSError, ValueError):
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
                print(
                    "Auto-refresh failed (remote Chromium CDP is not reachable)."
                )
                print("Falling back to manual mode.")
                set_token_command(None)
            print("Restarting server...")
        else:
            break


if __name__ == "__main__":
    main()
