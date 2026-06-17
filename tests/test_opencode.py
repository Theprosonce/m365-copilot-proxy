from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import json
from pathlib import Path
from urllib.request import urlopen

import pytest

# Not tested on windows

TARGET_MODEL = "m365-copilot-proxy/m365-gpt-think"
TARGET_PROMPT = "Read the file on tests/sample.txt. Reply with just the content of the file, no explanation needed. If cannot read the file, reply with 'CANNOT_READ_FILE'."
TARGET_FILE = "tests/sample.txt"
TARGET_CONTENT = "The content of this file is used to verify."
_BEGIN = "<<<TOOL_CALLS>>>"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _read_token(repo_root: Path) -> str:
    env_token = os.environ.get("M365_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token

    env_file = repo_root / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith("M365_ACCESS_TOKEN="):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        return value
    return ""


def _wait_for_proxy(port: int, timeout_seconds: int = 45) -> bool:
    deadline = time.time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:  # nosec: local test endpoint only
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _write_opencode_config(home_dir: Path, base_url: str) -> None:
    config_dir = home_dir / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = """
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "m365-copilot-proxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "M365 Copilot Proxy",
      "options": {
        "baseURL": "__BASE_URL__",
        "apiKey": "dummy"
      },
      "models": {
        "m365-gpt-think": {
          "name": "m365-gpt-think"
        }
      }
    }
  }
}
""".replace("__BASE_URL__", base_url)
    (config_dir / "opencode.json").write_text(payload, encoding="utf-8")


def _json_events(output: str) -> list[dict]:
    events: list[dict] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return events


@pytest.mark.real_model
def test_opencode_tool_calls_runtime_bridge_real_model(tmp_path: Path) -> None:
    opencode_bin = shutil.which("opencode")
    if opencode_bin is None:
        pytest.skip("opencode is not installed on PATH")

    repo_root = Path(__file__).resolve().parents[1]
    token = _read_token(repo_root)
    if not token.startswith("eyJ"):
        pytest.skip("No substrate token available for real-model integration test")

    workspace = repo_root
    sample = workspace / TARGET_FILE
    if sample.exists():
        sample.unlink()
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(TARGET_CONTENT, encoding="utf-8")
    assert sample.read_text(encoding="utf-8").strip() == TARGET_CONTENT.strip()

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / ".env").write_text(
        "\n".join(
            [
                f"M365_ACCESS_TOKEN={token}",
                "M365_PERSIST_DEFAULT=false",
                "M365_DISABLE_MEMORY=true",
                "M365_WORK_GROUNDING=false",
                "M365_DEBUG=1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    port = _free_port()
    server_cmd = [
        "uv",
        "run",
        "--project",
        str(repo_root),
        "copilot-openai-proxy",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-auto-refresh",
        "--no-launch-edge",
        "--no-capture-on-start",
        "--no-configure-clients",
    ]
    server_proc = subprocess.Popen(
        server_cmd,
        cwd=runtime_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        assert _wait_for_proxy(port), "proxy did not become healthy"

        home = tmp_path / "home"
        _write_opencode_config(home, f"http://127.0.0.1:{port}/v1")

        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCODE")}
        env["HOME"] = str(home)
        env["PWD"] = str(workspace)

        command = [
            opencode_bin,
            "run",
            TARGET_PROMPT,
            "-m",
            TARGET_MODEL,
            "--format",
            "json",
            "--print-logs",
            "--log-level",
            "DEBUG",
            "--dangerously-skip-permissions",
        ]

        proc: subprocess.CompletedProcess[str] | None = None
        events: list[dict] = []
        read_state: dict | None = None
        for _attempt in range(8):
            proc = subprocess.run(
                command,
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            events = _json_events(proc.stdout)
            tool_events = [e for e in events if e.get("type") == "tool_use"]
            read_events = [
                e for e in tool_events if (e.get("part") or {}).get("tool") == "read"
            ]
            if read_events:
                read_state = (read_events[0].get("part") or {}).get("state") or {}
                if read_state.get("status") == "completed":
                    break
            time.sleep(1)
        assert proc is not None
    finally:
        server_proc.terminate()
        try:
            server_stdout, server_stderr = server_proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_stdout, server_stderr = server_proc.communicate(timeout=15)

    assert proc.returncode == 0, (
        "opencode run failed\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}\n"
        f"proxy stdout:\n{server_stdout}\n"
        f"proxy stderr:\n{server_stderr}"
    )

    assert sample.read_text(encoding="utf-8") == TARGET_CONTENT
    assert _BEGIN not in proc.stdout

    assert events, (
        f"no JSON events returned\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    if read_state is None:
        pytest.skip(
            "Live model did not emit a read tool call after retries; rerun OPENCODE_REAL_MODEL_TEST=1"
        )

    tool_events = [e for e in events if e.get("type") == "tool_use"]
    assert tool_events, "read tool was never executed"
    read_events = [
        e for e in tool_events if (e.get("part") or {}).get("tool") == "read"
    ]
    assert read_events, "no read tool event emitted"

    assert read_state is not None
    assert read_state.get("status") == "completed"
    assert (read_state.get("input") or {}).get("filePath") == TARGET_FILE
    output_blob = str(read_state.get("output") or "")
    assert TARGET_CONTENT in output_blob

    assert "step=1 loop" in proc.stderr
    assert "modelID=m365-gpt-think" in proc.stderr

    # Retrieve the model's text response from JSON events
    response = "".join(
        e.get("part", {}).get("text", "")
        for e in events
        if e.get("type") == "text" and isinstance(e.get("part"), dict)
    )

    assert response, "Model did not return any text response"

    print(f"\nCaptured Model Response:\n{response}\n")

    # Non stdout/print (usually for silent test runs)
    response_log_path = Path("./logs/opencode_response.log")
    response_log_path.parent.mkdir(parents=True, exist_ok=True)
    response_log_path.write_text(response, encoding="utf-8")

    response_clean = response.rstrip("\n")
    expected_clean = TARGET_CONTENT.rstrip("\n")
    assert response_clean == expected_clean, (
        f"Model response does not match file content exactly.\n"
        f"Expected: {repr(expected_clean)}\n"
        f"Got:      {repr(response_clean)}"
    )
