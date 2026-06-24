from pathlib import Path

from m365_copilot_openai_proxy.config import Settings


def test_settings_load_defaults_from_config_ini(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config.ini").write_text(
        "\n".join(
            [
                "[settings]",
                "work_grounding = false",
                "recv_timeout = 12",
                "[serve]",
                "host = 0.0.0.0",
                "port = 8181",
                "cdp_port = 9333",
                "auto_refresh = false",
                "launch_edge = false",
                "capture_on_start = false",
                "capture_timeout_seconds = 13",
                "refresh_before_seconds = 34",
                "refresh_retry_seconds = 56",
                "configure_clients = false",
                "[capture_token]",
                "cdp_port = 9444",
                "timeout_seconds = 77",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.work_grounding is False
    assert settings.recv_timeout == 12
    assert settings.serve_host == "0.0.0.0"
    assert settings.serve_port == 8181
    assert settings.serve_cdp_port == 9333
    assert settings.serve_auto_refresh is False
    assert settings.serve_launch_edge is False
    assert settings.serve_capture_on_start is False
    assert settings.serve_capture_timeout_seconds == 13
    assert settings.serve_refresh_before_seconds == 34
    assert settings.serve_refresh_retry_seconds == 56
    assert settings.serve_configure_clients is False
    assert settings.capture_token_cdp_port == 9444
    assert settings.capture_token_timeout_seconds == 77


def test_env_ignored_config_ini(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("config.ini").write_text(
        "[serve]\nport = 8181\nauto_refresh = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("M365_SERVE_PORT", "9191")
    monkeypatch.setenv("M365_AUTO_REFRESH", "true")

    settings = Settings()

    assert settings.serve_port == 8181
    assert settings.serve_auto_refresh is False


def test_config_ini_created_from_template_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.ini"
    assert not config_path.exists()

    settings = Settings()

    assert config_path.exists()
    content = config_path.read_text(encoding="utf-8")
    assert "[settings]" in content
    assert "work_grounding = true" in content

