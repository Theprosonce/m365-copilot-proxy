from __future__ import annotations

import argparse
import asyncio
import json
import logging
import msvcrt
import re
import subprocess
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import websockets

from .app import create_app


class _SuppressCtrlC(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "CTRL+C" not in record.getMessage()


logging.getLogger("uvicorn.error").addFilter(_SuppressCtrlC())

_CDP_JS = """
(() => {
    const stores = [sessionStorage, localStorage];
    for (const store of stores) {
        for (const k of Object.keys(store)) {
            if (!k.includes('accesstoken')) continue;
            try {
                const v = JSON.parse(store.getItem(k));
                if (v && v.secret && v.secret.startsWith('eyJ') &&
                    v.target && v.target.includes('substrate')) {
                    return v.secret;
                }
            } catch {}
        }
    }
    return null;
})()
"""


async def _cdp_extract_token(port: int) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            tabs = (await client.get(f"http://localhost:{port}/json")).json()
    except Exception:
        return None

    tab = next(
        (t for t in tabs if "m365.cloud.microsoft" in t.get("url", "")),
        None,
    )
    if not tab:
        return None

    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"]) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": _CDP_JS}}))
            result = json.loads(await ws.recv())
            return result.get("result", {}).get("result", {}).get("value")
    except Exception:
        return None


def _try_auto_refresh(cdp_port: int) -> bool:
    token = asyncio.run(_cdp_extract_token(cdp_port))
    if not token:
        return False
    _write_token(token)
    print("Token refreshed automatically.")
    return True


def _write_token(token: str) -> None:
    env_path = Path(".env")
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^M365_ACCESS_TOKEN=.*$", f"M365_ACCESS_TOKEN={token}", text)
        if "M365_ACCESS_TOKEN=" not in text:
            text += f"\nM365_ACCESS_TOKEN={token}\n"
    else:
        text = f"M365_ACCESS_TOKEN={token}\n"
    env_path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="copilot-openai-proxy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("set-token").set_defaults(func=set_token_command)
    subparsers.add_parser("launch-edge").set_defaults(func=launch_edge_command)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--cdp-port", type=int, default=9222)
    serve_parser.set_defaults(func=serve_command)

    args = parser.parse_args()
    args.func(args)


def launch_edge_command(_args) -> None:
    subprocess.Popen([
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "--remote-debugging-port=9222",
        "https://m365.cloud.microsoft/chat",
    ])
    print("Edge launched with remote debugging on port 9222.")


def set_token_command(_args) -> None:
    print("Paste the full WebSocket URL (or just the access_token value), then press Enter:")
    raw = input().strip()
    match = re.search(r"access_token=([^&\s]+)", raw)
    token = match.group(1) if match else raw
    if not token.startswith("eyJ"):
        print("Error: could not find a valid token. Make sure you copied the full WebSocket URL.")
        return
    _write_token(token)
    print(".env updated.")


def serve_command(args: argparse.Namespace) -> None:
    cdp_port: int = args.cdp_port
    while True:
        app = create_app()
        config = uvicorn.Config(app, host=args.host, port=args.port)
        server = uvicorn.Server(config)

        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        while not server.started and thread.is_alive():
            time.sleep(0.05)
        print("\n  [q] quit    [r] refresh token\n")

        action = None
        while thread.is_alive():
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
            time.sleep(0.05)

        thread.join()

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
