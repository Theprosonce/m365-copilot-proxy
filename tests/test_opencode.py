from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
import json
import hashlib
from pathlib import Path
from urllib.request import urlopen
import pytest

from middleware.tool_emulation import (
    ToolEmulationPipeline,
)
from m365_copilot_openai_proxy.config import Settings

TARGET_MODEL = "m365-copilot-proxy/m365-gpt-think"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _read_token(repo_root: Path) -> str:
    config_file = repo_root / "config.ini"
    if config_file.exists():
        try:
            import configparser
            parser = configparser.ConfigParser()
            parser.read(config_file, encoding="utf-8")
            if parser.has_section("settings") and parser.has_option("settings", "access_token"):
                val = parser.get("settings", "access_token").strip().strip('"').strip("'")
                if val:
                    return val
        except Exception:
            pass

    env_file = repo_root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("access_token="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")

    return ""


def _wait_for_proxy(port: int, timeout_seconds: int = 45) -> bool:
    deadline = time.time() + timeout_seconds
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _check_proxy_healthy(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urlopen(url, timeout=1) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                return data.get("status") == "ok" and data.get("token", {}).get(
                    "valid", False
                )
    except Exception:
        pass
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


def _generate_unpredictable_sentinel() -> str:
    random_bytes = secrets.token_bytes(32)
    hex_hash = hashlib.sha256(random_bytes).hexdigest()[:24]
    return f"SENTINEL_{hex_hash}"


def test_opencode_path_safety_and_normalization() -> None:
    """
    Unit-level test verifying that the ToolEmulationPipeline:
    1. Normalizes relative paths to absolute paths within the workspace.
    2. Rejects and filters out any paths that try to escape/access files outside the workspace.
    """
    settings = Settings(
        access_token="fake",
        tool_emulation_enabled=True,
    )
    pipeline = ToolEmulationPipeline(settings)

    workspace_root = "/tmp/my_workspace"
    tools = [
        {
            "name": "read",
            "parameters": {
                "type": "object",
                "properties": {"filePath": {"type": "string"}},
                "required": ["filePath"],
            },
        }
    ]

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "read", "arguments": {"filePath": "subdir/sample.txt"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is not None
    assert len(calls) == 1
    args = json.loads(calls[0].function.arguments)
    assert args["filePath"] == "/tmp/my_workspace/subdir/sample.txt"

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "read", "arguments": {"filePath": "/tmp/my_workspace/test.txt"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is not None
    assert len(calls) == 1
    args = json.loads(calls[0].function.arguments)
    assert args["filePath"] == "/tmp/my_workspace/test.txt"

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "read", "arguments": {"filePath": "../outside.txt"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is None

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "read", "arguments": {"filePath": "/etc/passwd"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is None


def test_tool_emulation_preserves_unrelated_path_parameters() -> None:
    """
    Regression test: Ensure that path normalization is NOT applied to tools
    that have a 'path' parameter but are NOT file tools (e.g., webfetch with URL path).
    """
    settings = Settings(
        access_token="fake",
        tool_emulation_enabled=True,
    )
    pipeline = ToolEmulationPipeline(settings)

    workspace_root = "/tmp/my_workspace"
    tools = [
        {
            "name": "webfetch",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "format": {"type": "string"},
                },
                "required": ["url"],
            },
        }
    ]

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "webfetch", "arguments": {"url": "https://example.com/some/path"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is not None
    assert len(calls) == 1
    args = json.loads(calls[0].function.arguments)
    assert args["url"] == "https://example.com/some/path"


def test_tool_emulation_preserves_api_path_parameter() -> None:
    """
    Regression test: A non-file tool with a 'path' argument (like an API endpoint)
    must NOT have that path normalized to a filesystem path.
    """
    settings = Settings(
        access_token="fake",
        tool_emulation_enabled=True,
    )
    pipeline = ToolEmulationPipeline(settings)

    workspace_root = "/tmp/my_workspace"
    tools = [
        {
            "name": "api_call",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "API endpoint path"},
                },
                "required": ["path"],
            },
        }
    ]

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "api_call", "arguments": {"path": "/v1/users"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is not None, "API call tool should be parsed"
    assert len(calls) == 1
    args = json.loads(calls[0].function.arguments)
    assert args["path"] == "/v1/users", (
        f"API path should NOT be normalized. Got: {args['path']}"
    )


def test_tool_emulation_preserves_api_filePath_parameter() -> None:
    """
    Regression test: A non-file tool with a 'filePath' argument (like a config path)
    must NOT have that path normalized if it's not a known file tool.
    """
    settings = Settings(
        access_token="fake",
        tool_emulation_enabled=True,
    )
    pipeline = ToolEmulationPipeline(settings)

    workspace_root = "/tmp/my_workspace"
    tools = [
        {
            "name": "config_reference",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Remote config file path (not local)",
                    },
                },
                "required": ["filePath"],
            },
        }
    ]

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "config_reference", "arguments": {"filePath": "/remote/config.json"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is not None, "Config reference tool should be parsed"
    assert len(calls) == 1
    args = json.loads(calls[0].function.arguments)
    assert args["filePath"] == "/remote/config.json", (
        f"Remote config path should NOT be normalized. Got: {args['filePath']}"
    )


def test_tool_emulation_normalizes_path_parameter_for_file_tools() -> None:
    """
    Verify that 'path' parameter (not just 'filePath') is normalized for file tools.
    """
    settings = Settings(
        access_token="fake",
        tool_emulation_enabled=True,
    )
    pipeline = ToolEmulationPipeline(settings)

    workspace_root = "/tmp/my_workspace"
    tools = [
        {
            "name": "list",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }
    ]

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "list", "arguments": {"path": "subdir"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is not None
    assert len(calls) == 1
    args = json.loads(calls[0].function.arguments)
    assert args["path"] == "/tmp/my_workspace/subdir"


def test_tool_emulation_rejects_path_traversal() -> None:
    """
    Verify that path traversal attempts are rejected.
    """
    settings = Settings(
        access_token="fake",
        tool_emulation_enabled=True,
    )
    pipeline = ToolEmulationPipeline(settings)

    workspace_root = "/tmp/my_workspace"
    tools = [
        {
            "name": "read",
            "parameters": {
                "type": "object",
                "properties": {"filePath": {"type": "string"}},
                "required": ["filePath"],
            },
        }
    ]

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "read", "arguments": {"filePath": "../../etc/passwd"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is None, "Path traversal should be rejected"

    raw_text = '<<<TOOL_CALLS>>>\n[{"name": "read", "arguments": {"filePath": "/etc/shadow"}}]\n<<<END_TOOL_CALLS>>>'
    calls = pipeline.parse_response(raw_text, tools, workspace_root=workspace_root)
    assert calls is None, "Absolute path outside workspace should be rejected"


@pytest.mark.real_model
def test_opencode_real_file_read_integration(tmp_path: Path) -> None:
    """
    TRUTH-BASED OpenCode compatibility test.

    This test MUST FAIL if:
    - OpenCode does not execute a real read tool call
    - The model answers directly without tool use
    - The sentinel is missing from tool result evidence
    - CANNOT_READ_FILE appears anywhere in output
    - No tool execution evidence exists

    This test passes ONLY when:
    - A real OpenCode command is executed
    - OpenCode connects to the proxy
    - OpenCode performs an actual file read via the read tool
    - The file read targets a real file created by the test
    - The tool result contains the unique sentinel
    - No CANNOT_READ_FILE error occurs
    """
    opencode_bin = shutil.which("opencode")
    if opencode_bin is None:
        pytest.skip("OpenCode CLI not available; real compatibility not verified")

    repo_root = Path(__file__).resolve().parents[1]
    token = _read_token(repo_root)
    if not token.startswith("eyJ"):
        pytest.skip("No substrate token available for real-model integration test")

    random_id = secrets.token_hex(4)
    sentinel = _generate_unpredictable_sentinel()

    tmp_path = repo_root / "temp" / f"test_opencode_run_{random_id}"
    shutil.rmtree(tmp_path, ignore_errors=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=workspace, capture_output=True, check=False)
    (workspace / "package.json").write_text(
        '{"name": "test-workspace"}', encoding="utf-8"
    )

    target_relative_file = f"tests/sample_{random_id}.txt"
    sample_file = workspace / target_relative_file
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_text(sentinel, encoding="utf-8")
    assert sample_file.read_text(encoding="utf-8").strip() == sentinel

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_file = tmp_path / "sessions.db"
    (runtime_dir / "config.ini").write_text(
        "\n".join(
            [
                "[settings]",
                f"access_token = {token}",
                "persist_default = false",
                "disable_memory = true",
                "work_grounding = false",
                f"session_db_path = {str(db_file.as_posix())}",
                "debug = true",
                "[tool_emulation]",
                "exclude_tools = bash",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    server_proc = None
    if _check_proxy_healthy(8000):
        port = 8000
        use_existing_proxy = True
    else:
        port = _free_port()
        use_existing_proxy = False

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
        server_env = os.environ.copy()

        server_proc = subprocess.Popen(
            server_cmd,
            cwd=runtime_dir,
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    try:
        if not use_existing_proxy:
            assert _wait_for_proxy(port), "proxy did not become healthy"

        home = tmp_path / "home"
        _write_opencode_config(home, f"http://127.0.0.1:{port}/v1")

        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCODE")}
        env["HOME"] = str(home)
        env["PWD"] = str(workspace)

        prompt = (
            f"Use the read tool to read the file at {target_relative_file}. "
            f"Reply with just the content of the file, no explanation."
        )

        cmd = [
            opencode_bin,
            "run",
            prompt,
            "-m",
            TARGET_MODEL,
            "--format",
            "json",
            "--print-logs",
            "--log-level",
            "DEBUG",
            "--dangerously-skip-permissions",
        ]

        proc = subprocess.run(
            cmd,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

        print(f"\n--- STDOUT ---\n{proc.stdout}")
        print(f"\n--- STDERR ---\n{proc.stderr}")

        events = _json_events(proc.stdout)

        text_events = [
            e.get("part", {}).get("text", "")
            for e in events
            if e.get("type") == "text" and isinstance(e.get("part"), dict)
        ]
        response_text = "".join(text_events)

        assert "CANNOT_READ_FILE" not in response_text, (
            f"OpenCode reported CANNOT_READ_FILE in response!\n"
            f"This indicates the read tool failed to execute.\n"
            f"Response: {response_text}"
        )

        tool_use_events = [e for e in events if e.get("type") == "tool_use"]
        read_tool_events = [
            e for e in tool_use_events if (e.get("part") or {}).get("tool") == "read"
        ]

        assert len(read_tool_events) > 0, (
            f"CRITICAL: No read tool execution found!\n"
            f"This means OpenCode did NOT use the read tool.\n"
            f"Tool use events: {tool_use_events}\n"
            f"All events types: {[e.get('type') for e in events]}"
        )

        read_event = read_tool_events[0]
        read_state = (read_event.get("part") or {}).get("state") or {}

        assert read_state is not None, (
            f"Read tool event found but state is missing.\nEvent: {read_event}"
        )

        assert read_state.get("status") == "completed", (
            f"Read tool did not complete successfully.\n"
            f"Status: {read_state.get('status')}\n"
            f"State: {read_state}"
        )

        filePath_used = (read_state.get("input") or {}).get("filePath")
        assert filePath_used is not None, (
            f"filePath was missing in tool call arguments.\nState: {read_state}"
        )
        assert Path(filePath_used).is_absolute(), (
            f"filePath must be normalized to absolute. Got: {filePath_used}"
        )

        resolved_file = Path(filePath_used).resolve()
        expected_file = sample_file.resolve()
        assert resolved_file == expected_file, (
            f"filePath resolved incorrectly.\n"
            f"Got: {resolved_file}\n"
            f"Expected: {expected_file}"
        )

        read_result = read_state.get("result") or {}
        if isinstance(read_result, dict):
            read_result = read_result.get("content", "")
        else:
            read_result = ""

        tool_output = read_state.get("output", "")
        if isinstance(tool_output, dict):
            tool_output = tool_output.get("content", "")
        elif not isinstance(tool_output, str):
            tool_output = ""

        assert sentinel in tool_output or sentinel in read_result, (
            f"CRITICAL: Sentinel '{sentinel}' NOT found in tool output!\n"
            f"This means the file read did not return the expected content.\n"
            f"Tool output: {tool_output[:500] if tool_output else 'None'}\n"
            f"Read result: {read_result[:500] if read_result else 'None'}"
        )

        print(f"SUCCESS: Sentinel found in tool output: {sentinel}")

        assert sentinel in response_text, (
            f"CRITICAL: Sentinel '{sentinel}' NOT found in model response!\n"
            f"The model did not return the file content.\n"
            f"Response text: {response_text[:500]}"
        )

        assert proc.returncode == 0, (
            f"OpenCode exited with non-zero status.\n"
            f"returncode: {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )

    finally:
        server_stdout = "reused existing proxy"
        server_stderr = "reused existing proxy"
        if server_proc is not None:
            server_proc.terminate()
            try:
                server_stdout, server_stderr = server_proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_stdout, server_stderr = server_proc.communicate(timeout=15)


@pytest.mark.real_model
def test_opencode_rejects_path_traversal(tmp_path: Path) -> None:
    """
    Security test: Verify that OpenCode rejects path traversal attempts.
    The read tool should fail safely when given a path outside workspace.
    """
    opencode_bin = shutil.which("opencode")
    if opencode_bin is None:
        pytest.skip("OpenCode CLI not available; real compatibility not verified")

    repo_root = Path(__file__).resolve().parents[1]
    token = _read_token(repo_root)
    if not token.startswith("eyJ"):
        pytest.skip("No substrate token available for real-model integration test")

    random_id = secrets.token_hex(4)
    tmp_path = repo_root / "temp" / f"test_opencode_traversal_{random_id}"
    shutil.rmtree(tmp_path, ignore_errors=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=workspace, capture_output=True, check=False)
    (workspace / "package.json").write_text(
        '{"name": "test-workspace"}', encoding="utf-8"
    )

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_file = tmp_path / "sessions.db"
    (runtime_dir / "config.ini").write_text(
        "\n".join(
            [
                "[settings]",
                f"access_token = {token}",
                "persist_default = false",
                "disable_memory = true",
                "work_grounding = false",
                f"session_db_path = {str(db_file.as_posix())}",
                "debug = true",
                "[tool_emulation]",
                "exclude_tools = bash",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    server_proc = None
    if _check_proxy_healthy(8000):
        port = 8000
        use_existing_proxy = True
    else:
        port = _free_port()
        use_existing_proxy = False

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
        server_env = os.environ.copy()

        server_proc = subprocess.Popen(
            server_cmd,
            cwd=runtime_dir,
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    try:
        if not use_existing_proxy:
            assert _wait_for_proxy(port), "proxy did not become healthy"

        home = tmp_path / "home"
        _write_opencode_config(home, f"http://127.0.0.1:{port}/v1")

        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCODE")}
        env["HOME"] = str(home)
        env["PWD"] = str(workspace)

        prompt = (
            "Use the read tool to read the file at /etc/passwd. "
            "Reply with 'SUCCESS_READ_OUTSIDE_WORKSPACE' if you can read it. "
            "Reply with 'ACCESS_DENIED' if you cannot."
        )

        cmd = [
            opencode_bin,
            "run",
            prompt,
            "-m",
            TARGET_MODEL,
            "--format",
            "json",
            "--dangerously-skip-permissions",
        ]

        proc = subprocess.run(
            cmd,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        print(f"\n--- STDOUT ---\n{proc.stdout}")
        print(f"\n--- STDERR ---\n{proc.stderr}")

        all_output = proc.stdout + proc.stderr

        assert "SUCCESS_READ_OUTSIDE_WORKSPACE" not in all_output, (
            f"SECURITY VIOLATION: OpenCode was able to read /etc/passwd!\n"
            f"Path traversal protection failed.\n"
            f"stdout:\n{proc.stdout}"
        )

    finally:
        if server_proc is not None:
            server_proc.terminate()
            try:
                server_proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.communicate(timeout=15)


@pytest.mark.real_model
def test_opencode_tool_result_propagation(tmp_path: Path) -> None:
    """
    Regression test for tool result propagation bug.

    This test verifies that:
    1. Read tool executes successfully
    2. Read tool output contains the expected content
    3. Final model response uses the tool result
    4. Model does NOT claim "tool result unavailable"
    5. Model does NOT call glob/bash after successful read
    6. Model does NOT claim file not found after successful read
    """
    opencode_bin = shutil.which("opencode")
    if opencode_bin is None:
        pytest.skip("OpenCode CLI not available; real compatibility not verified")

    repo_root = Path(__file__).resolve().parents[1]
    token = _read_token(repo_root)
    if not token.startswith("eyJ"):
        pytest.skip("No substrate token available for real-model integration test")

    random_id = secrets.token_hex(4)
    tmp_path = repo_root / "temp" / f"test_opencode_tool_result_{random_id}"
    shutil.rmtree(tmp_path, ignore_errors=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=workspace, capture_output=True, check=False)
    (workspace / "package.json").write_text(
        '{"name": "test-workspace"}', encoding="utf-8"
    )

    test_file_content = "SENTINEL_CONTENT_ABC123"
    sample_file = workspace / "test.txt"
    sample_file.write_text(test_file_content, encoding="utf-8")
    assert sample_file.read_text(encoding="utf-8").strip() == test_file_content

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_file = tmp_path / "sessions.db"
    (runtime_dir / "config.ini").write_text(
        "\n".join(
            [
                "[settings]",
                f"access_token = {token}",
                "persist_default = false",
                "disable_memory = true",
                "work_grounding = false",
                f"session_db_path = {str(db_file.as_posix())}",
                "debug = true",
                "[tool_emulation]",
                "exclude_tools = bash",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    server_proc = None
    if _check_proxy_healthy(8000):
        port = 8000
        use_existing_proxy = True
    else:
        port = _free_port()
        use_existing_proxy = False

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
        server_env = os.environ.copy()

        server_proc = subprocess.Popen(
            server_cmd,
            cwd=runtime_dir,
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    try:
        if not use_existing_proxy:
            assert _wait_for_proxy(port), "proxy did not become healthy"

        home = tmp_path / "home"
        _write_opencode_config(home, f"http://127.0.0.1:{port}/v1")

        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCODE")}
        env["HOME"] = str(home)
        env["PWD"] = str(workspace)

        prompt = (
            "Use the read tool to read test.txt. "
            "Reply with exactly the file contents and nothing else."
        )

        cmd = [
            opencode_bin,
            "run",
            prompt,
            "-m",
            TARGET_MODEL,
            "--format",
            "json",
            "--print-logs",
            "--dangerously-skip-permissions",
        ]

        proc = subprocess.run(
            cmd,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

        print(f"\n--- STDOUT ---\n{proc.stdout}")
        print(f"\n--- STDERR ---\n{proc.stderr}")

        events = _json_events(proc.stdout)

        tool_use_events = [e for e in events if e.get("type") == "tool_use"]
        read_tool_events = [
            e for e in tool_use_events if (e.get("part") or {}).get("tool") == "read"
        ]

        assert len(read_tool_events) > 0, (
            f"CRITICAL: No read tool execution found!\n"
            f"Tool use events: {tool_use_events}"
        )

        read_event = read_tool_events[0]
        read_state = (read_event.get("part") or {}).get("state") or {}
        assert read_state.get("status") == "completed", (
            f"Read tool did not complete.\nState: {read_state}"
        )

        tool_output = read_state.get("output", "")
        assert test_file_content in tool_output, (
            f"Read tool output does not contain expected content.\n"
            f"Expected: {test_file_content}\n"
            f"Got: {tool_output[:500]}"
        )

        glob_tool_events = [
            e for e in tool_use_events if (e.get("part") or {}).get("tool") == "glob"
        ]
        bash_tool_events = [
            e for e in tool_use_events if (e.get("part") or {}).get("tool") == "bash"
        ]

        assert len(glob_tool_events) == 0, (
            "Model called glob after successful read - unnecessary tool call.\n"
            "Read was successful, model should use the result directly."
        )
        assert len(bash_tool_events) == 0, (
            "Model called bash after successful read - unnecessary tool call.\n"
            "Read was successful, model should use the result directly."
        )

        text_events = [
            e.get("part", {}).get("text", "")
            for e in events
            if e.get("type") == "text" and isinstance(e.get("part"), dict)
        ]
        response_text = "".join(text_events)

        assert test_file_content in response_text, (
            f"CRITICAL: Final response does not contain file content!\n"
            f"Expected: {test_file_content}\n"
            f"Response: {response_text[:500]}"
        )

        assert "not found" not in response_text.lower(), (
            f"Model claimed file not found despite successful read!\n"
            f"Response: {response_text}"
        )

        assert (
            "tool result" not in response_text.lower()
            or "unavailable" not in response_text.lower()
        ), (
            f"Model claimed tool result unavailable despite successful read!\n"
            f"Response: {response_text}"
        )

        assert proc.returncode == 0

    finally:
        if server_proc is not None:
            server_proc.terminate()
            try:
                server_proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.communicate(timeout=15)
