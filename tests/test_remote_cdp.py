from copilot_proxy_server import cli


def test_remote_cdp_urls(monkeypatch):
    settings = type("Settings", (), {"browser_cdp_url": "http://chromium:9222/"})()
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    assert cli._cdp_http_url(9999, "/json") == "http://chromium:9222/json"
    assert cli._cdp_websocket_url(
        9999, "ws://127.0.0.1:9222/devtools/page/abc"
    ) == "ws://chromium:9222/devtools/page/abc"


def test_local_cdp_urls(monkeypatch):
    settings = type("Settings", (), {"browser_cdp_url": ""})()
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    assert cli._cdp_http_url(9333, "json") == "http://localhost:9333/json"
    ws_url = "ws://localhost:9333/devtools/page/abc"
    assert cli._cdp_websocket_url(9333, ws_url) == ws_url


def test_local_cdp_rewrites_unspecified_bind_host(monkeypatch):
    settings = type("Settings", (), {"browser_cdp_url": ""})()
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    ws_url = "ws://0.0.0.0:9222/devtools/page/abc"
    assert cli._cdp_websocket_url(9222, ws_url) == "ws://localhost:9222/devtools/page/abc"
