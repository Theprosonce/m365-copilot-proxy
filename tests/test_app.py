from __future__ import annotations

import types
import base64
import json
import time
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.cli import (
    _debug_browser_profile_dir,
    _find_m365_page,
    _is_substrate_token,
    _needs_substrate_token,
    _read_token,
    _resolve_debug_browser_path,
    _seconds_remaining,
    _write_token,
)
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.session_store import PersistentSessionStore
from m365_copilot_openai_proxy.substrate_client import (
    SubstrateCopilotClient,
    SubstrateCopilotError,
)


class FakeCopilotClient:
    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []
        self.sessions: list[object | None] = []

    async def chat(
        self,
        prompt: str,
        additional_context: list[str],
        session: object | None = None,
        tone: str | None = None,
        images: list[dict[str, str]] | None = None,
    ) -> str:
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        return "copilot reply"

    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
        session: object | None = None,
        tone: str | None = None,
        images: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        yield "hello"
        yield " world"


class FailingStreamCopilotClient(FakeCopilotClient):
    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
        session: object | None = None,
        tone: str | None = None,
        images: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        raise SubstrateCopilotError("upstream broke")
        yield ""


def build_client(fake: FakeCopilotClient) -> TestClient:
    settings = Settings(M365_ACCESS_TOKEN="fake-token", M365_PERSIST_DEFAULT=False)
    app = create_app(settings=settings, copilot_client_factory=lambda: fake)
    return TestClient(app)


def make_jwt(exp: int, aud: str = "https://substrate.office.com/sydney") -> str:
    def encode(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'aud': aud, 'exp': exp, 'oid': 'oid', 'tid': 'tid'})}.sig"


def test_models_endpoint() -> None:
    client = build_client(FakeCopilotClient())
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["id"] == "m365-copilot"


def test_app_starts_without_token_for_startup_capture() -> None:
    app = create_app(settings=Settings(M365_ACCESS_TOKEN=""))
    client = TestClient(app)

    response = client.get("/v1/token/status")

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False


def test_token_status_reports_expiry() -> None:
    settings = Settings(M365_ACCESS_TOKEN=make_jwt(int(time.time()) + 3600))
    app = create_app(
        settings=settings, copilot_client_factory=lambda: FakeCopilotClient()
    )
    client = TestClient(app)

    response = client.get("/v1/token/status")

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["expires_at"]
    assert body["seconds_remaining"] > 0


def test_healthz_includes_token_remaining_time() -> None:
    settings = Settings(M365_ACCESS_TOKEN=make_jwt(int(time.time()) + 3600))
    app = create_app(
        settings=settings, copilot_client_factory=lambda: FakeCopilotClient()
    )
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["token"]["valid"] is True
    assert body["token"]["seconds_remaining"] > 0


def test_token_status_rejects_non_substrate_token() -> None:
    settings = Settings(
        M365_ACCESS_TOKEN=make_jwt(int(time.time()) + 3600, aud="394866fc-eedb")
    )
    app = create_app(
        settings=settings, copilot_client_factory=lambda: FakeCopilotClient()
    )
    client = TestClient(app)

    response = client.get("/v1/token/status")

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["error"] == "Access token is not a substrate.office.com token."


def test_substrate_client_rejects_non_substrate_token() -> None:
    token = make_jwt(int(time.time()) + 3600, aud="394866fc-eedb")

    try:
        SubstrateCopilotClient(token)
    except SubstrateCopilotError as exc:
        assert "not a substrate.office.com token" in str(exc)
    else:
        raise AssertionError("SubstrateCopilotClient accepted a non-Substrate token")


def test_default_client_factory_reloads_token_from_env(tmp_path, monkeypatch) -> None:
    first_token = make_jwt(int(time.time()) + 3600)
    second_token = make_jwt(int(time.time()) + 7200)
    env_path = tmp_path / ".env"
    env_path.write_text(f"M365_ACCESS_TOKEN={first_token}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    seen_tokens: list[str] = []

    class RecordingCopilotClient(FakeCopilotClient):
        def __init__(self, access_token: str, *_args, **_kwargs):
            super().__init__()
            seen_tokens.append(access_token)

    monkeypatch.setattr(
        "m365_copilot_openai_proxy.app.SubstrateCopilotClient",
        RecordingCopilotClient,
    )
    settings = Settings(M365_ACCESS_TOKEN=first_token)
    app = create_app(settings=settings)
    client = TestClient(app)

    time.sleep(0.01)
    env_path.write_text(f"M365_ACCESS_TOKEN={second_token}\n", encoding="utf-8")
    response = client.post(
        "/v1/chat/completions",
        json={"model": "ignored", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    assert seen_tokens == [second_token]


def test_cli_reads_current_token_from_env(tmp_path, monkeypatch) -> None:
    token = make_jwt(int(time.time()) + 3600)
    (tmp_path / ".env").write_text(f"M365_ACCESS_TOKEN='{token}'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _read_token() == token


def test_cli_write_token_ignores_commented_token_line(tmp_path, monkeypatch) -> None:
    token = make_jwt(int(time.time()) + 3600)
    env_path = tmp_path / ".env"
    env_path.write_text("# M365_ACCESS_TOKEN=old\nOTHER=value\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    _write_token(token)

    assert _read_token() == token
    assert env_path.read_text(encoding="utf-8").count("M365_ACCESS_TOKEN=") == 2


def test_cli_seconds_remaining_uses_jwt_exp() -> None:
    token = make_jwt(int(time.time()) + 3600)

    remaining = _seconds_remaining(token)

    assert 0 < remaining <= 3600


def test_cli_accepts_only_substrate_tokens() -> None:
    assert _is_substrate_token(make_jwt(int(time.time()) + 3600))
    assert not _is_substrate_token(
        make_jwt(int(time.time()) + 3600, aud="394866fc-eedb")
    )


def test_cli_knows_when_startup_capture_is_needed() -> None:
    assert _needs_substrate_token(None)
    assert _needs_substrate_token(
        make_jwt(int(time.time()) + 3600, aud="394866fc-eedb")
    )
    assert _needs_substrate_token(make_jwt(int(time.time()) - 1))
    assert not _needs_substrate_token(make_jwt(int(time.time()) + 3600))


def test_cli_startup_refresh_can_do_full_fallback(monkeypatch) -> None:
    from m365_copilot_openai_proxy.cli import _startup_capture_loop

    seen_allow_nudge: list[bool] = []
    capture_called = False

    def fake_refresh(_port: int, *, allow_nudge: bool = True) -> bool:
        seen_allow_nudge.append(allow_nudge)
        return allow_nudge

    def fake_capture(_port: int, _timeout: int) -> bool:
        nonlocal capture_called
        capture_called = True
        return False

    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._wait_for_m365_page",
        lambda _port, _timeout: True,
    )
    monkeypatch.setattr("m365_copilot_openai_proxy.cli._try_auto_refresh", fake_refresh)
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._capture_token_to_env", fake_capture
    )
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli.time.sleep", lambda _seconds: None
    )

    _startup_capture_loop(9222, timeout_seconds=1)

    assert seen_allow_nudge[-1] is True
    assert capture_called is False


def test_cli_startup_refresh_waits_for_m365_page(monkeypatch) -> None:
    from m365_copilot_openai_proxy.cli import _startup_capture_loop

    calls: list[str] = []

    def fake_wait(_port: int, _timeout: int) -> bool:
        calls.append("wait")
        return True

    def fake_refresh(_port: int, *, allow_nudge: bool = True) -> bool:
        calls.append("refresh")
        return True

    monkeypatch.setattr("m365_copilot_openai_proxy.cli._wait_for_m365_page", fake_wait)
    monkeypatch.setattr("m365_copilot_openai_proxy.cli._try_auto_refresh", fake_refresh)

    _startup_capture_loop(9222, timeout_seconds=1)

    assert calls == ["wait", "refresh"]


def test_cli_finds_real_m365_page_not_devtools() -> None:
    tabs = [
        {
            "type": "page",
            "url": "devtools://devtools/bundled/devtools_app.html?remoteBase=https://m365.cloud.microsoft/chat",
        },
        {"type": "page", "url": "https://m365.cloud.microsoft/chat"},
    ]

    assert _find_m365_page(tabs) == tabs[1]


def test_cli_linux_browser_priority_prefers_chromium(monkeypatch) -> None:
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._read_env_value", lambda _key: None
    )
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.os.name", "posix")
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.sys.platform", "linux")

    installed = {
        "chromium": "/usr/bin/chromium",
        "chromium-browser": "/usr/bin/chromium-browser",
        "google-chrome": "/usr/bin/google-chrome",
        "microsoft-edge": "/usr/bin/microsoft-edge",
    }
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli.shutil.which", lambda name: installed.get(name)
    )

    assert _resolve_debug_browser_path() == "/usr/bin/chromium"


def test_cli_debug_browser_path_keeps_windows_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._read_env_value", lambda _key: None
    )
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.os.name", "nt")

    assert (
        _resolve_debug_browser_path()
        == r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    )


def test_cli_linux_snap_profile_dir_uses_non_hidden_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.sys.platform", "linux")
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.Path.home", lambda: tmp_path)

    profile_dir = _debug_browser_profile_dir("/snap/bin/chromium")

    assert profile_dir == tmp_path / "m365-copilot-openai-proxy-browser-profile"


def test_cli_linux_close_debug_browser_calls_cdp(monkeypatch) -> None:
    from m365_copilot_openai_proxy.cli import _close_debug_browser

    called_port = None

    async def fake_cdp_close(port: int) -> None:
        nonlocal called_port
        called_port = port

    monkeypatch.setattr("m365_copilot_openai_proxy.cli.sys.platform", "linux")
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._cdp_close_browser", fake_cdp_close
    )

    _close_debug_browser(1234)
    assert called_port == 1234


def test_cli_close_debug_browser_honors_setting(monkeypatch) -> None:
    from m365_copilot_openai_proxy.cli import _close_debug_browser

    called = False

    async def fake_cdp_close(port: int) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._cdp_close_browser", fake_cdp_close
    )
    monkeypatch.setenv("M365_HIDE_ON_TOKEN_SUCCESS", "false")

    _close_debug_browser(1234)
    assert not called


def test_openai_chat_completion_translates_history() -> None:
    from pathlib import Path
    expected_injection = Path("prompts/tool_emulation_injection.md").read_text("utf-8")

    fake = FakeCopilotClient()
    client = build_client(fake)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "copilot reply"
    assert fake.calls == [
        (
            f"{expected_injection}\n---\nSecond question",
            [
                "System instructions:\nBe concise.",
                f"Prior conversation transcript:\nUser: {expected_injection}\n---\nFirst question\nAssistant: First answer",
            ],
        )
    ]
    assert fake.sessions == [None]


def test_openai_persistent_session_header_reuses_session() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    body = {
        "model": "m365-copilot",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    first = client.post(
        "/v1/chat/completions", headers={"X-M365-Session-Id": "work"}, json=body
    )
    second = client.post(
        "/v1/chat/completions", headers={"X-M365-Session-Id": "work"}, json=body
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not None


def test_m365_session_env_disables_temporary_memory(monkeypatch) -> None:
    from m365_copilot_openai_proxy.app import _effective_disable_memory

    settings = types.SimpleNamespace(disable_memory=True)

    monkeypatch.delenv("M365_SESSION", raising=False)
    assert _effective_disable_memory(settings) is True

    monkeypatch.setenv("M365_SESSION", "work")
    assert _effective_disable_memory(settings) is False

    monkeypatch.delenv("M365_SESSION", raising=False)
    assert _effective_disable_memory(settings) is True

    settings.disable_memory = False
    assert _effective_disable_memory(settings) is False


def test_m365_session_env_prints_on_startup(monkeypatch, capsys) -> None:
    fake = FakeCopilotClient()
    monkeypatch.setenv("M365_SESSION", "work")

    with build_client(fake):
        pass

    assert "X-M365-Session-Id: Session attached: work" in capsys.readouterr().out


def test_openai_persistent_session_env_reuses_session(monkeypatch) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    body = {
        "model": "m365-copilot",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    monkeypatch.setenv("M365_SESSION", "work")
    first = client.post("/v1/chat/completions", json=body)
    second = client.post("/v1/chat/completions", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not None
    assert "X-M365-Session-Id: Session attached: work" not in fake.calls[0][1]


def test_openai_persistent_session_header_overrides_env(monkeypatch) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    body = {
        "model": "m365-copilot",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    monkeypatch.setenv("M365_SESSION", "env-work")
    first = client.post(
        "/v1/chat/completions", headers={"X-M365-Session-Id": "header-work"}, json=body
    )
    second = client.post(
        "/v1/chat/completions", headers={"X-M365-Session-Id": "header-work"}, json=body
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not None
    assert "X-M365-Session-Id: Session attached: env-work" not in fake.calls[0][1]


def test_openai_persistent_model_suffix_uses_user_as_session_key() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)

    for user in ("alice", "alice", "bob"):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "m365-copilot:persist",
                "user": user,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 200

    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not fake.sessions[2]


def test_persistent_session_turn_flags_are_reserved_in_order() -> None:
    session = PersistentSessionStore().get("work")

    first_turn = session.reserve_turn()
    second_turn = session.reserve_turn()

    assert first_turn.conversation_id == second_turn.conversation_id
    assert first_turn.client_session_id == second_turn.client_session_id
    assert first_turn.is_start_of_session is True
    assert second_turn.is_start_of_session is False


def test_session_store_evicts_least_recently_used_over_cap() -> None:
    store = PersistentSessionStore(max_sessions=2)
    store.get("a")
    time.sleep(0.01)
    store.get("b")
    time.sleep(0.01)
    store.get("c")  # over cap -> "a" (LRU) evicted

    keys = {k for k, _ in store.items()}
    assert keys == {"b", "c"}


def test_session_store_evicts_sessions_past_ttl() -> None:
    store = PersistentSessionStore(ttl_seconds=100)
    stale = store.get("old")
    stale.last_used = time.time() - 200  # force past the TTL
    store.get("new")  # any insert triggers the prune

    assert {k for k, _ in store.items()} == {"new"}


def test_session_store_does_not_evict_a_leased_session() -> None:
    import asyncio

    async def scenario() -> None:
        store = PersistentSessionStore(max_sessions=1)
        leased = store.get("a")
        await leased.lock.acquire()  # a concurrent request is mid-turn on "a"
        try:
            store.get("b")  # over cap, but "a" is leased -> must survive
            assert "a" in {k for k, _ in store.items()}
        finally:
            leased.lock.release()
        store.get("c")  # "a" now free -> evictable as LRU
        assert "a" not in {k for k, _ in store.items()}

    asyncio.run(scenario())


def test_persistent_session_rotates_on_truncated_history_edit() -> None:

    from m365_copilot_openai_proxy.app import _persistent_session

    store = PersistentSessionStore()
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            session_store=store, settings=types.SimpleNamespace(persist_default=True)
        )
    )
    req = types.SimpleNamespace(headers={})

    def msg(role: str, text: str):
        return types.SimpleNamespace(role=role, content=text)

    base = [msg("user", "hello there")]
    s = _persistent_session(app, req, "m365-copilot", None, base)
    assert s is not None
    s.reserve_turn()
    s.reserve_turn()  # we've served 2 turns on this chat
    conv = s.conversation_id

    # Faithful continuation: history carries both assistant turns -> keep the conversation.
    cont = [
        msg("user", "hello there"),
        msg("assistant", "a1"),
        msg("user", "u2"),
        msg("assistant", "a2"),
        msg("user", "u3"),
    ]
    s2 = _persistent_session(app, req, "m365-copilot", None, cont)
    assert s2.conversation_id == conv

    # Truncated/regenerated: only 1 assistant turn left -> rotate to a fresh conversation.
    trunc = [msg("user", "hello there"), msg("assistant", "a1"), msg("user", "u2b")]
    s3 = _persistent_session(app, req, "m365-copilot", None, trunc)
    assert s3.conversation_id != conv
    assert s3.turn_count == 0


def test_is_proxy_model_classifies_ours_vs_passthrough() -> None:
    from m365_copilot_openai_proxy.app import _is_proxy_model

    s = Settings(M365_MODEL_ALIAS="m365-copilot")
    assert _is_proxy_model(s, "m365-opus")
    assert _is_proxy_model(s, "m365-gpt:persist")
    assert _is_proxy_model(s, "m365-copilot")
    assert _is_proxy_model(s, "")  # no model -> keep substrate default
    assert not _is_proxy_model(s, "claude-sonnet-4-6")
    assert not _is_proxy_model(s, "gpt-4o")


def test_anthropic_messages_passthrough_only_for_non_m365_models(monkeypatch) -> None:
    from fastapi.responses import JSONResponse

    from m365_copilot_openai_proxy import anthropic_passthrough as ap
    from m365_copilot_openai_proxy.app import create_app

    monkeypatch.setattr(ap, "credential_available", lambda settings: True)

    async def fake_forward(settings, body, headers):
        return JSONResponse({"forwarded": True})

    monkeypatch.setattr(ap, "forward_messages", fake_forward)

    settings = Settings(
        M365_ACCESS_TOKEN="fake-token",
        M365_PERSIST_DEFAULT=False,
        M365_ANTHROPIC_PASSTHROUGH=True,
    )
    fake = FakeCopilotClient()
    client = TestClient(
        create_app(settings=settings, copilot_client_factory=lambda: fake)
    )

    body = {"max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}

    # Non-m365 model -> forwarded to Anthropic, substrate untouched.
    r = client.post("/v1/messages", json={**body, "model": "claude-sonnet-4-6"})
    assert r.json() == {"forwarded": True}
    assert fake.calls == []

    # Our model -> substrate, NOT forwarded.
    r2 = client.post("/v1/messages", json={**body, "model": "m365-opus"})
    assert r2.status_code == 200
    assert fake.calls


def test_passthrough_headers_oauth_and_apikey(tmp_path) -> None:
    from m365_copilot_openai_proxy import anthropic_passthrough as ap

    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "tok-abc",
                    "refreshToken": "r",
                    "expiresAt": 9_999_999_999_000,
                }
            }
        ),
        encoding="utf-8",
    )

    class S:
        anthropic_version = "2023-06-01"
        anthropic_creds_file = str(creds)
        anthropic_key = ""

    class H:
        def __init__(self, d):
            self._d = d

        def get(self, k):
            return self._d.get(k)

    # OAuth mode: Bearer + the required oauth-2025-04-20 beta flag, no x-api-key.
    h = ap._upstream_headers(S(), H({}))
    assert h["Authorization"] == "Bearer tok-abc"
    assert "oauth-2025-04-20" in h["anthropic-beta"]
    assert "x-api-key" not in h
    assert ap.credential_available(S())

    # A client-supplied beta is preserved and merged with the oauth flag.
    h2 = ap._upstream_headers(S(), H({"anthropic-beta": "foo"}))
    assert h2["anthropic-beta"] == "foo,oauth-2025-04-20"

    # API-key override -> x-api-key, no Authorization/oauth beta.
    class SK(S):
        anthropic_key = "sk-test-1234"

    h3 = ap._upstream_headers(SK(), H({}))
    assert h3["x-api-key"] == "sk-test-1234"
    assert "Authorization" not in h3
    assert "anthropic-beta" not in h3


def test_launch_debug_edge_reloads_existing_tab(monkeypatch) -> None:
    from m365_copilot_openai_proxy import cli

    calls = {"reload": 0, "popen": 0}
    monkeypatch.setattr(
        cli,
        "_edge_debug_tabs",
        lambda port: [
            {
                "type": "page",
                "url": "https://m365.cloud.microsoft/chat",
                "webSocketDebuggerUrl": "ws://debug",
            }
        ],
    )

    async def fake_reload(ws_url):
        calls["reload"] += 1

    monkeypatch.setattr(cli, "_cdp_reload_m365", fake_reload)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda *a, **k: calls.__setitem__("popen", calls["popen"] + 1),
    )

    cli._launch_debug_edge(9222)

    assert calls == {
        "reload": 1,
        "popen": 0,
    }  # reused + reloaded, no duplicate window spawned


def test_launch_debug_edge_spawns_visible_when_no_tab(monkeypatch, tmp_path) -> None:
    from m365_copilot_openai_proxy import cli

    popen_calls: list[tuple] = []
    monkeypatch.setattr(cli, "_edge_debug_tabs", lambda port: None)
    monkeypatch.setattr(
        cli, "_resolve_debug_browser_path", lambda: str(tmp_path / "edge.exe")
    )
    monkeypatch.setattr(
        cli, "_debug_browser_profile_dir", lambda p: tmp_path / "profile"
    )
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k))
    )
    monkeypatch.delenv("M365_EDGE_HEADLESS", raising=False)

    cli._launch_debug_edge(9222)

    assert len(popen_calls) == 1
    (argv,), kwargs = popen_calls[0]
    assert argv[-1] == "https://m365.cloud.microsoft/chat"
    assert kwargs.get("startupinfo") is None  # launched VISIBLE, not minimized


def test_openai_streaming_returns_sse() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "stream": True,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    ) as response:
        payload = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in response.iter_text()
        )
    assert response.status_code == 200
    assert '"role": "assistant"' in payload
    assert '"content": "hello"' in payload
    assert '"content": " world"' in payload
    assert "data: [DONE]" in payload


def test_openai_streaming_returns_error_event_on_upstream_failure() -> None:
    client = build_client(FailingStreamCopilotClient())
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "stream": True,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    ) as response:
        payload = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in response.iter_text()
        )

    assert response.status_code == 200
    assert '"type": "upstream_error"' in payload
    assert '"message": "upstream broke"' in payload
    assert "data: [DONE]" in payload


def test_responses_streaming_returns_error_event_on_upstream_failure() -> None:
    client = build_client(FailingStreamCopilotClient())
    with client.stream(
        "POST",
        "/v1/responses",
        json={"model": "ignored", "stream": True, "input": "Hello"},
    ) as response:
        payload = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in response.iter_text()
        )

    assert response.status_code == 200
    assert '"type": "error"' in payload
    assert '"message": "upstream broke"' in payload


def test_anthropic_messages_endpoint() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    response = client.post(
        "/v1/messages",
        json={
            "model": "ignored",
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "copilot reply"


def test_anthropic_messages_does_not_log_all_offered_tools(capsys) -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    response = client.post(
        "/v1/messages",
        json={
            "model": "ignored",
            "messages": [{"role": "user", "content": "Hello"}],
            "tools": [{"name": "bash", "description": "Run shell commands"}],
        },
    )

    assert response.status_code == 200
    out = capsys.readouterr().out
    assert "-> /v1/messages model=" in out
    assert "-> tool=bash" not in out


def test_anthropic_streaming_returns_error_event_on_upstream_failure() -> None:
    client = build_client(FailingStreamCopilotClient())
    with client.stream(
        "POST",
        "/v1/messages",
        json={
            "model": "ignored",
            "stream": True,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    ) as response:
        payload = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for chunk in response.iter_text()
        )

    assert response.status_code == 200
    assert "event: error" in payload
    assert '"message": "upstream broke"' in payload


def test_responses_requires_final_user_message() -> None:
    client = build_client(FakeCopilotClient())
    response = client.post(
        "/v1/responses",
        json={
            "model": "ignored",
            "input": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ],
        },
    )

    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "The final Responses input message must be a user message."
    )


def test_endpoints_with_tool_emulation_injection(tmp_path, monkeypatch) -> None:
    import sys
    import importlib
    
    injection_file = tmp_path / "tool_emulation_injection.md"
    injection_file.write_text("CUSTOM_INJECTION_HEADER", encoding="utf-8")
    
    monkeypatch.setenv("TOOL_EMULATION_INJECTION_PATH", str(injection_file))
    
    try:
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)
        
        fake = FakeCopilotClient()
        client = build_client(fake)
        
        # Test /v1/chat/completions
        resp1 = client.post(
            "/v1/chat/completions",
            json={
                "model": "ignored",
                "messages": [{"role": "user", "content": "Hello OpenAI"}],
            }
        )
        assert resp1.status_code == 200
        assert len(fake.calls) == 1
        assert fake.calls[0][0].startswith("CUSTOM_INJECTION_HEADER\n---\nHello OpenAI")
        
        # Test /v1/messages
        resp2 = client.post(
            "/v1/messages",
            json={
                "model": "ignored",
                "messages": [{"role": "user", "content": "Hello Anthropic"}],
            }
        )
        assert resp2.status_code == 200
        assert len(fake.calls) == 2
        assert fake.calls[1][0].startswith("CUSTOM_INJECTION_HEADER\n---\nHello Anthropic")
        
        # Test /v1/responses
        resp3 = client.post(
            "/v1/responses",
            json={
                "model": "ignored",
                "input": "Hello Responses",
            }
        )
        assert resp3.status_code == 200
        assert len(fake.calls) == 3
        assert fake.calls[2][0].startswith("CUSTOM_INJECTION_HEADER\n---\nHello Responses")
        
    finally:
        monkeypatch.delenv("TOOL_EMULATION_INJECTION_PATH", raising=False)
        import middleware.tool_emulation
        import middleware.pipeline
        importlib.reload(middleware.tool_emulation)
        importlib.reload(middleware.pipeline)

