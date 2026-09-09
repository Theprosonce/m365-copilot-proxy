from types import SimpleNamespace

import pytest

from copilot_proxy_server import cli


def _serve_args(*, manage_chromium: bool = True):
    return SimpleNamespace(
        host="127.0.0.1",
        port=8000,
        cdp_port=9222,
        configure_clients=False,
        manage_chromium=manage_chromium,
    )


def test_serve_manages_chromium_for_server_lifetime(monkeypatch):
    events = []
    monkeypatch.setattr(cli, "_start_chromium_container", lambda: events.append("start"))
    monkeypatch.setattr(cli, "_run_server", lambda _args: events.append("serve"))
    monkeypatch.setattr(cli, "_stop_chromium_container", lambda: events.append("stop"))

    cli.serve_command(_serve_args())

    assert events == ["start", "serve", "stop"]


def test_serve_does_not_run_when_chromium_start_fails_and_no_cdp(monkeypatch):
    def fail_start():
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(cli, "_start_chromium_container", fail_start)
    monkeypatch.setattr(cli, "_wait_for_m365_page", lambda _port, _timeout: False)
    monkeypatch.setattr(cli, "_run_server", lambda _args: pytest.fail("server should not start"))
    monkeypatch.setattr(
        cli,
        "_stop_chromium_container",
        lambda: pytest.fail("chromium was not started"),
    )

    with pytest.raises(RuntimeError, match="docker unavailable"):
        cli.serve_command(_serve_args())


def test_serve_continues_with_existing_cdp_when_docker_start_fails(monkeypatch):
    events = []

    def fail_start():
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(cli, "_start_chromium_container", fail_start)
    monkeypatch.setattr(cli, "_wait_for_m365_page", lambda _port, _timeout: True)
    monkeypatch.setattr(cli, "_stop_chromium_container", lambda: events.append("stop"))
    monkeypatch.setattr(cli, "_run_server", lambda _args: events.append("serve"))

    cli.serve_command(_serve_args())

    assert events == ["serve"]


def test_serve_skips_chromium_lifecycle_when_disabled(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_start_chromium_container",
        lambda: pytest.fail("chromium start should be disabled"),
    )
    monkeypatch.setattr(
        cli,
        "_stop_chromium_container",
        lambda: pytest.fail("chromium stop should be disabled"),
    )
    served = []
    monkeypatch.setattr(cli, "_run_server", lambda _args: served.append(True))

    cli.serve_command(_serve_args(manage_chromium=False))

    assert served == [True]
