from types import SimpleNamespace

import pytest

from copilot_proxy_server import cli


def test_run_compose_uses_sg_fallback_on_permission_denied(monkeypatch, tmp_path):
    compose_file = tmp_path / "compose.server.yml"
    compose_file.write_text("services:\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_resolve_compose_file", lambda: compose_file)

    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        if cmd[:2] == ["docker", "compose"]:
            return SimpleNamespace(
                returncode=1,
                stderr="permission denied while trying to connect to the docker API at unix:///var/run/docker.sock",
                stdout="",
            )
        if cmd[:2] == ["sg", "docker"]:
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli._run_compose("up", "-d", "chromium")

    assert calls[0][:2] == ["docker", "compose"]
    assert calls[1][:2] == ["sg", "docker"]


def test_run_compose_raises_when_fallback_fails(monkeypatch, tmp_path):
    compose_file = tmp_path / "compose.server.yml"
    compose_file.write_text("services:\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_resolve_compose_file", lambda: compose_file)

    def fake_run(cmd, capture_output, text):
        if cmd[:2] == ["docker", "compose"]:
            return SimpleNamespace(
                returncode=1,
                stderr="permission denied while trying to connect to the docker API at unix:///var/run/docker.sock",
                stdout="",
            )
        if cmd[:2] == ["sg", "docker"]:
            return SimpleNamespace(returncode=1, stderr="sg fallback failed", stdout="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Fallback via `sg docker` also failed"):
        cli._run_compose("up", "-d", "chromium")
