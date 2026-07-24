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
                "truncation_before_sending = false",
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
    assert settings.truncation_before_sending is False
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
    assert "truncation_before_sending = true" in content


def test_firefox_browser_support():
    from m365_copilot_openai_proxy.cli import (
        _debug_browser_profile_dir,
        _LINUX_BROWSER_PRIORITY,
    )
    # Check priority
    assert "firefox" in _LINUX_BROWSER_PRIORITY

    # Check profile directory for firefox vs other browsers
    firefox_profile = _debug_browser_profile_dir("firefox")
    assert "firefox-profile" in str(firefox_profile)

    chrome_profile = _debug_browser_profile_dir("google-chrome")
    assert "edge-profile" in str(chrome_profile)


def test_prefer_active_browser_config():
    settings = Settings()
    assert settings.prefer_active_browser is True


def test_linux_browser_priority_and_analysis(monkeypatch):
    import sys
    from m365_copilot_openai_proxy.cli import (
        _analyse_installed_browsers,
        _resolve_debug_browser_path,
    )

    # Mock sys.platform to be linux
    monkeypatch.setattr(sys, "platform", "linux")

    # Mock shutil.which to simulate what's installed
    installed = {
        "chromium": "/usr/bin/chromium",
        "firefox": "/usr/bin/firefox",
    }
    monkeypatch.setattr("shutil.which", lambda cmd: installed.get(cmd))

    # Mock Settings to return defaults
    from m365_copilot_openai_proxy.config import Settings
    mock_settings = Settings()
    # Force prefer_active_browser to be False for this test to verify raw priority
    monkeypatch.setattr(mock_settings, "prefer_active_browser", False)
    # Ensure edge_path is the default so it is ignored on Linux
    monkeypatch.setattr(mock_settings, "edge_path", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.Settings", lambda: mock_settings)

    # Call analysis
    analysis = _analyse_installed_browsers()
    assert analysis["chromium"] == "/usr/bin/chromium"
    assert analysis["chrome"] is None
    assert analysis["edge"] is None
    assert analysis["firefox"] == "/usr/bin/firefox"

    # Resolve should pick chromium since it's highest priority and prefer_active is False
    resolved = _resolve_debug_browser_path()
    assert resolved == "/usr/bin/chromium"


def test_linux_browser_strict_priority_over_active(monkeypatch):
    import sys
    from m365_copilot_openai_proxy.cli import (
        _analyse_installed_browsers,
        _resolve_debug_browser_path,
    )

    monkeypatch.setattr(sys, "platform", "linux")

    installed = {
        "chromium": "/usr/bin/chromium",
        "firefox": "/usr/bin/firefox",
    }
    monkeypatch.setattr("shutil.which", lambda cmd: installed.get(cmd))

    # Mock active browser process check: only firefox is running
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._is_browser_process_running",
        lambda path: "firefox" in path
    )

    # Force prefer_active_browser to be True, but resolve must STILL pick chromium
    from m365_copilot_openai_proxy.config import Settings
    mock_settings = Settings()
    monkeypatch.setattr(mock_settings, "prefer_active_browser", True)
    monkeypatch.setattr(mock_settings, "edge_path", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.Settings", lambda: mock_settings)

    # Resolve should pick chromium since it has strictly higher priority, ignoring active status
    resolved = _resolve_debug_browser_path()
    assert resolved == "/usr/bin/chromium"


def test_linux_browser_not_installed(monkeypatch):
    import sys
    from m365_copilot_openai_proxy.cli import (
        _resolve_debug_browser_path,
    )

    monkeypatch.setattr(sys, "platform", "linux")

    # No browsers installed
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    # Mock _attempt_install_chromium to do nothing (so it doesn't try to run apt/snap/etc.)
    called_install = False
    def mock_install():
        nonlocal called_install
        called_install = True
    monkeypatch.setattr("m365_copilot_openai_proxy.cli._attempt_install_chromium", mock_install)

    from m365_copilot_openai_proxy.config import Settings
    mock_settings = Settings()
    monkeypatch.setattr(mock_settings, "edge_path", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.Settings", lambda: mock_settings)

    # Since no browser is found, it will try to install and then fail with RuntimeError
    import pytest
    with pytest.raises(RuntimeError):
        _resolve_debug_browser_path()

    assert called_install is True

