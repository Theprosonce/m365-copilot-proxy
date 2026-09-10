from __future__ import annotations

import base64
import json
import struct
import time
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from copilot_proxy_server.app import (
    _conversation_key,
    _first_real_user_text,
    _trim_history,
    create_app,
)
from copilot_proxy_server.config import Settings
from copilot_proxy_server.models import ContentPart, ExtractedImage, OpenAIMessage
from copilot_proxy_server.session_store import (
    PersistentSession,
    PersistentSessionStore,
)
from copilot_proxy_server.substrate_client import (
    MAX_SUBSTRATE_SEND_CHARS,
    SubstrateCopilotClient,
    _combine_text,
    _truncate_substrate_text,
    resolve_tone,
)
from copilot_proxy_server.translator import (
    ext_tool_context,
    extract_file_attachments,
    extract_images,
    translate_anthropic_request,
    translate_openai_request,
    translate_responses_request,
)
from copilot_proxy_server.models import (
    AnthropicMessagesRequest,
    OpenAIChatRequest,
    OpenAIResponsesRequest,
)


def _make_jwt(exp: int, aud: str = "https://substrate.office.com/sydney") -> str:
    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc({'aud': aud, 'exp': exp, 'oid': 'oid', 'tid': 'tid'})}.sig"


def _png_bytes(
    w: int = 8, h: int = 8, rgb: tuple[int, int, int] = (220, 20, 20)
) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return (
            struct.pack(">I", len(d))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


class FakeCopilotClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.sessions: list[object | None] = []
        self.images: list[object] = []
        self.reply = "reply"
        self.stream_chunks = ["x"]

    async def chat(
        self, prompt, additional_context, session=None, tone=None, images=None
    ) -> str:
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        self.images.append(images)
        return self.reply

    async def chat_stream(
        self, prompt, additional_context, session=None, tone=None, images=None
    ) -> AsyncIterator[str]:
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        self.images.append(images)
        for chunk in self.stream_chunks:
            yield chunk


def _client(
    fake: FakeCopilotClient,
    tmp_db: Path,
    persist_default: bool = True,
    conversation_id: str = "",
) -> TestClient:
    settings = Settings(
        access_token="fake",
        persist_default=persist_default,
        conversation_id=conversation_id,
        session_db_path=str(tmp_db),
    )
    return TestClient(
        create_app(settings=settings, copilot_client_factory=lambda: fake)
    )


# --- vision: inline image extraction ---


def test_extract_images_openai_and_anthropic() -> None:
    oa = [
        ContentPart(type="text", text="hi"),
        ContentPart(type="image_url", image_url={"url": "data:image/png;base64,AAAA"}),
    ]
    an = [
        ContentPart(
            type="image",
            source={"type": "base64", "media_type": "image/jpeg", "data": "BBBB"},
        )
    ]
    a = extract_images(oa)
    b = extract_images(an)
    assert (
        len(a) == 1
        and a[0].file_type == "png"
        and a[0].data_uri.startswith("data:image/png")
    )
    assert len(b) == 1 and b[0].file_type == "jpg"  # jpeg normalized to jpg


def test_extract_images_from_anthropic_tool_result() -> None:
    parts = [
        ContentPart(
            type="tool_result",
            tool_use_id="read-image",
            content=[
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": "AAAA",
                    },
                }
            ],
        )
    ]
    images = extract_images(parts)
    assert len(images) == 1 and images[0].file_type == "jpg"


def test_extract_images_skips_remote_and_text() -> None:
    assert extract_images("plain string") == []
    assert (
        extract_images(
            [ContentPart(type="image_url", image_url={"url": "https://x/y.png"})]
        )
        == []
    )


# --- vision: VS Code file:// attachment resolution off disk ---


def test_extract_file_attachments_resolves_local_png(tmp_path) -> None:
    p = tmp_path / "shot.png"
    p.write_bytes(_png_bytes())
    uri = "file:///" + quote(str(p).replace("\\", "/"))
    text = f'<attachments>\n<attachment name="image" id="image:{uri}">\n</attachment>\n</attachments>\ndescribe'
    imgs = extract_file_attachments(text)
    assert len(imgs) == 1
    assert imgs[0].file_name == "shot.png"
    assert imgs[0].data_uri.startswith("data:image/png;base64,")


def test_extract_file_attachments_ignores_missing_and_nonimage(tmp_path) -> None:
    missing = "file:///" + quote(str(tmp_path / "nope.png").replace("\\", "/"))
    txt = tmp_path / "a.txt"
    txt.write_text("hi")
    nonimg = "file:///" + quote(str(txt).replace("\\", "/"))
    assert extract_file_attachments(f'id="image:{missing}"') == []
    assert extract_file_attachments(f'id="image:{nonimg}"') == []
    assert extract_file_attachments("no attachments here") == []


# --- session keying: distinct chats -> distinct conversations ---


def test_first_real_user_text_skips_vscode_wrappers() -> None:
    msgs = [
        OpenAIMessage(
            role="user", content="<environment_info>OS Linux</environment_info>"
        ),
        OpenAIMessage(role="user", content="<workspace_info>tree</workspace_info>"),
        OpenAIMessage(role="user", content="the real question"),
    ]
    assert _first_real_user_text(msgs) == "the real question"


def test_conversation_key_distinct_per_first_message() -> None:
    wrap = OpenAIMessage(role="user", content="<environment_info>OS</environment_info>")
    a = [wrap, OpenAIMessage(role="user", content="chat A")]
    b = [wrap, OpenAIMessage(role="user", content="chat B")]
    assert _conversation_key(a) != _conversation_key(b)
    assert _conversation_key(a) == _conversation_key(list(a))  # stable


def test_persist_without_user_is_one_conversation_per_chat(tmp_path) -> None:
    """`:persist` without a `user` must key per-chat (the VS Code case), not collapse to one."""
    fake = FakeCopilotClient()
    client = _client(fake, tmp_path / "s.db")

    def send(first: str):
        return client.post(
            "/v1/chat/completions",
            json={
                "model": "m365-opus:persist",
                "messages": [
                    {
                        "role": "user",
                        "content": "<environment_info>OS</environment_info>",
                    },
                    {"role": "user", "content": first},
                ],
            },
        )

    send("chat A opening")
    send("chat A opening")  # same chat
    send("chat B opening")  # different chat
    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not fake.sessions[2]


def test_configured_conversation_id_is_used_without_rotation(tmp_path) -> None:
    fake = FakeCopilotClient()
    conversation_id = "9648c51e-fe11-4554-9d0e-dfcf2f094271"
    client = _client(fake, tmp_path / "s.db", conversation_id=conversation_id)
    messages = [{"role": "user", "content": "same chat"}]

    client.post("/v1/chat/completions", json={"model": "m365-opus", "messages": messages})
    client.post("/v1/chat/completions", json={"model": "m365-opus", "messages": messages})

    assert [session.conversation_id for session in fake.sessions] == [
        conversation_id,
        conversation_id,
    ]


# --- clean outbound messages ---


def test_translators_send_only_current_message_and_tool_results() -> None:
    openai = translate_openai_request(
        OpenAIChatRequest(
            model="m365-opus",
            messages=[
                OpenAIMessage(role="system", content="hidden system prompt"),
                OpenAIMessage(role="user", content="old question"),
                OpenAIMessage(role="assistant", content="old answer"),
                OpenAIMessage(role="user", content="current question"),
            ],
        )
    )
    responses = translate_responses_request(
        OpenAIResponsesRequest(
            model="m365-opus",
            instructions="hidden system prompt",
            input=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current question"},
            ],
        )
    )
    anthropic = translate_anthropic_request(
        AnthropicMessagesRequest(
            model="m365-opus",
            max_tokens=256,
            system="hidden system prompt",
            messages=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current question"},
            ],
        )
    )

    for translated in (openai, responses, anthropic):
        assert translated.prompt == "current question"
        assert translated.additional_context == [
            f"System instructions:\n{ext_tool_context([])}"
        ]


def test_ext_tool_context_includes_client_tools() -> None:
    translated = translate_openai_request(
        OpenAIChatRequest(
            model="m365-opus",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "parameters": {
                            "type": "object",
                            "properties": {"file_path": {"type": "string"}},
                        },
                    },
                }
            ],
            messages=[OpenAIMessage(role="user", content="hello")],
        )
    )
    (ctx,) = translated.additional_context
    assert ctx.startswith("System instructions:\n")
    assert "Callable functions:\n- Read: " in ctx
    assert "reassess the original user request" in ctx
    assert "immediately emit the next EXT_TOOL block" in ctx
    assert "Do not require the user to say continue" in ctx


def test_title_request_returns_no_title_when_context_disabled(tmp_path) -> None:
    title_prompt = (
        "Write the title in the predominant language of the session — "
        "a stray word or code token in another language doesn't change it."
    )

    fake = FakeCopilotClient()
    client = _client(fake, tmp_path / "titles.db")

    openai = client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-opus",
            "messages": [{"role": "user", "content": title_prompt}],
        },
    )
    assert openai.status_code == 200
    assert openai.json()["choices"][0]["message"]["content"] == "No Title"

    responses = client.post(
        "/v1/responses",
        json={"model": "m365-opus", "input": title_prompt},
    )
    assert responses.status_code == 200
    assert responses.json()["output"][0]["content"][0]["text"] == "No Title"

    anthropic = client.post(
        "/v1/messages",
        json={
            "model": "m365-opus",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": title_prompt}],
        },
    )
    assert anthropic.status_code == 200
    assert anthropic.json()["content"][0]["text"] == "No Title"

    assert fake.calls == []


def test_title_request_reaches_model_when_context_enabled(tmp_path) -> None:
    fake = FakeCopilotClient()
    settings = Settings(
        access_token="fake",
        persist_default=False,
        disable_history_replay=False,
        session_db_path=str(tmp_path / "titles-enabled.db"),
    )
    client = TestClient(
        create_app(settings=settings, copilot_client_factory=lambda: fake)
    )
    prompt = "Generate a title for this conversation"
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-opus",
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    assert response.status_code == 200
    assert fake.calls[-1][0] == prompt


def test_translators_include_context_when_enabled() -> None:
    settings = Settings(disable_history_replay=False)
    openai = translate_openai_request(
        OpenAIChatRequest(
            model="m365-opus",
            messages=[
                OpenAIMessage(role="system", content="system prompt"),
                OpenAIMessage(role="user", content="old question"),
                OpenAIMessage(role="assistant", content="old answer"),
                OpenAIMessage(role="user", content="current question"),
            ],
        ),
        settings,
    )
    responses = translate_responses_request(
        OpenAIResponsesRequest(
            model="m365-opus",
            instructions="system prompt",
            input=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current question"},
            ],
        ),
        settings,
    )
    anthropic = translate_anthropic_request(
        AnthropicMessagesRequest(
            model="m365-opus",
            max_tokens=256,
            system="system prompt",
            messages=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current question"},
            ],
        ),
        settings,
    )

    for translated in (openai, responses, anthropic):
        assert translated.prompt == "current question"
        assert translated.additional_context == [
            "System instructions:\nsystem prompt",
            "Prior conversation transcript:\nUser: old question\nAssistant: old answer",
        ]


# --- history trimming on continued turns ---


def test_trim_history_sends_bootstrap_only_once() -> None:
    ctx = [
        "System instructions:\nEXT_TOOL contract and callable tools",
        "Prior conversation transcript:\nUser: a\nAssistant: b",
    ]
    fresh = PersistentSession()
    assert _trim_history(list(ctx), fresh) == ctx

    continued = PersistentSession()
    continued.turn_count = 3
    assert _trim_history(list(ctx), continued) == []


def test_trim_history_keeps_only_current_tool_results_after_bootstrap() -> None:
    ctx = [
        "System instructions:\nEXT_TOOL contract and callable tools",
        "Prior conversation transcript:\nUser: read file\nAssistant (tool call): read({})",
        "Tool results:\nEXT_TOOL_OUTPUT: [call_123] <content>Hello, world!</content>",
    ]
    continued = PersistentSession()
    continued.turn_count = 3
    assert _trim_history(list(ctx), continued) == [
        "Tool results:\nEXT_TOOL_OUTPUT: [call_123] <content>Hello, world!</content>"
    ]


def test_trim_history_keeps_bootstrap_for_nonpersistent_request() -> None:
    ctx = ["System instructions:\nEXT_TOOL contract"]
    assert _trim_history(list(ctx), None) == ctx


def test_persistent_tool_loop_never_replays_bootstrap_or_prior_results() -> None:
    bootstrap = "System instructions:\nEXT_TOOL contract and callable tools"
    session = PersistentSession()
    assert _trim_history([bootstrap], session) == [bootstrap]

    session.turn_count = 1
    first_result = "Tool results:\nEXT_TOOL_OUTPUT: [call_1] first"
    assert _trim_history([bootstrap, first_result], session) == [first_result]

    session.turn_count = 2
    second_result = "Tool results:\nEXT_TOOL_OUTPUT: [call_2] second"
    assert _trim_history([bootstrap, second_result], session) == [second_result]
    assert "call_1" not in "\n".join(_trim_history([bootstrap, second_result], session))

    session.turn_count = 3
    assert _trim_history([bootstrap], session) == []


def test_disable_history_replay_forwards_only_trailing_openai_tool_results() -> None:
    translated = translate_openai_request(
        OpenAIChatRequest(
            model="m365-opus",
            messages=[
                OpenAIMessage(role="user", content="original request"),
                OpenAIMessage(role="tool", content="old result", tool_call_id="old"),
                OpenAIMessage(role="assistant", content="working"),
                OpenAIMessage(role="tool", content="fresh one", tool_call_id="fresh-1"),
                OpenAIMessage(role="tool", content="fresh two", tool_call_id="fresh-2"),
            ],
        )
    )
    assert translated.prompt == (
        "EXT_TOOL_OUTPUT: [fresh-1] fresh one\n"
        "EXT_TOOL_OUTPUT: [fresh-2] fresh two"
    )
    assert not any(
        item.startswith("Tool results:\n") for item in translated.additional_context
    )
    joined_tool_results = "\n".join(
        item
        for item in translated.additional_context
        if item.startswith("Tool results:\n")
    )
    assert "old result" not in joined_tool_results
    assert "original request" not in joined_tool_results


def test_combine_text_labels_user_message_after_system_prompt() -> None:
    out = _combine_text(
        "THE REAL MESSAGE",
        ["System instructions:\nSYSTEM PROMPT"],
    )
    assert out == (
        "System instructions:\nSYSTEM PROMPT\n\n---\n\n"
        "User Message:\nTHE REAL MESSAGE"
    )


def test_combine_text_keeps_reference_context_after_user_message() -> None:
    out = _combine_text("THE REAL MESSAGE", ["big reference context"])
    assert out.startswith("User Message:\nTHE REAL MESSAGE")
    assert "big reference context" in out


def test_combine_text_sends_tool_continuation_directly() -> None:
    out = _combine_text(
        "",
        [
            "Tool results:\n"
            "EXT_TOOL_OUTPUT: [call_1] first result\n"
            "EXT_TOOL_OUTPUT: [call_2] second result"
        ],
    )
    assert out == (
        "EXT_TOOL_OUTPUT: [call_1] first result\n"
        "EXT_TOOL_OUTPUT: [call_2] second result"
    )
    assert "Reference context" not in out


def test_truncate_substrate_text_leaves_short_and_exact_limit_unchanged() -> None:
    short = "x" * 10
    exact = "x" * MAX_SUBSTRATE_SEND_CHARS

    assert _truncate_substrate_text(short) == short
    assert _truncate_substrate_text(exact) == exact


def test_truncate_substrate_text_caps_over_limit() -> None:
    over = "x" * (MAX_SUBSTRATE_SEND_CHARS + 1)

    truncated = _truncate_substrate_text(over)

    assert len(truncated) == MAX_SUBSTRATE_SEND_CHARS
    assert truncated == over[:MAX_SUBSTRATE_SEND_CHARS]


def test_truncate_substrate_text_can_be_disabled() -> None:
    over = "x" * (MAX_SUBSTRATE_SEND_CHARS + 1)

    assert _truncate_substrate_text(over, enabled=False) == over


# --- session store CRUD + SQLite persistence ---


def test_session_store_crud_and_persistence(tmp_path) -> None:
    db = tmp_path / "sessions.db"
    store = PersistentSessionStore(db_path=str(db))
    s = store.create("manual:x", label="demo")
    cid = s.conversation_id
    assert store.find("manual:x") is not None
    up = store.update("manual:x", label="renamed", rotate=True)
    assert up is not None and up.label == "renamed"
    assert up.conversation_id != cid and up.turn_count == 0
    # survives a fresh store instance (restart)
    store2 = PersistentSessionStore(db_path=str(db))
    assert store2.find("manual:x").label == "renamed"
    assert store.delete("manual:x") is True
    assert PersistentSessionStore(db_path=str(db)).find("manual:x") is None


def test_chats_crud_endpoints(tmp_path) -> None:
    client = _client(FakeCopilotClient(), tmp_path / "s.db")
    created = client.post("/v1/chats", json={"label": "smoke"})
    assert created.status_code == 201
    key = created.json()["key"]
    assert client.get(f"/v1/chats/{key}").json()["label"] == "smoke"
    patched = client.patch(
        f"/v1/chats/{key}", json={"label": "renamed", "rotate": True}
    )
    assert patched.json()["label"] == "renamed"
    assert client.get("/v1/chats").json()["count"] == 1
    assert client.delete(f"/v1/chats/{key}").json()["deleted"] is True
    assert client.get(f"/v1/chats/{key}").status_code == 404


# --- disableMemory (temporary chat) on the WS url ---


def test_ws_url_disable_memory_default_on() -> None:
    c = SubstrateCopilotClient(_make_jwt(int(time.time()) + 3600))
    assert "disableMemory=1" in c._ws_url("conv", "sess", "req")


def test_ws_url_disable_memory_can_be_off() -> None:
    c = SubstrateCopilotClient(_make_jwt(int(time.time()) + 3600), disable_memory=False)
    assert "disableMemory" not in c._ws_url("conv", "sess", "req")


# --- tone / model picker ---


def test_resolve_tone_mapping() -> None:
    assert resolve_tone("m365-gpt") == "Gpt_5_6_Chat"
    assert resolve_tone("m365-gpt-5.5-quick") == "Gpt_5_5_Chat"
    assert resolve_tone("m365-gpt-5.5-think") == "Gpt_5_5_Reasoning"
    assert resolve_tone("m365-gpt-5.6-quick") == "Gpt_5_6_Chat"
    assert resolve_tone("m365-gpt-5.6-think:persist") == "Gpt_5_6_Reasoning"
    assert resolve_tone("m365-opus:persist") == "Claude_Opus"  # suffix stripped
    assert resolve_tone("unknown-model") == "Claude_Opus"  # fallback


# --- OpenAPI surface ---


def test_openapi_exposes_chats_and_tags(tmp_path) -> None:
    client = _client(FakeCopilotClient(), tmp_path / "s.db")
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Microsoft 365 Copilot OpenAI Proxy"
    assert "/v1/chats" in schema["paths"]
    assert "/v1/chats/{key}" in schema["paths"]
    assert {t["name"] for t in schema["tags"]} >= {"inference", "chats", "ops"}


# --- vision wired end-to-end through the endpoint (image forwarded to client) ---


def test_chat_forwards_resolved_file_attachment_image(tmp_path) -> None:
    p = tmp_path / "pic.png"
    p.write_bytes(_png_bytes())
    uri = "file:///" + quote(str(p).replace("\\", "/"))
    fake = FakeCopilotClient()
    client = _client(fake, tmp_path / "s.db")
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-opus",
            "messages": [
                {
                    "role": "user",
                    "content": f'<attachment name="image" id="image:{uri}">\nwhat is this',
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert fake.images[-1] is not None
    assert isinstance(fake.images[-1][0], ExtractedImage)
    assert fake.images[-1][0].file_name == "pic.png"


# --- streamed TOOL_CALL blocks become native client tool events ---


def _tool_call_block() -> str:
    return 'EXT_TOOL: [{"id":"call_fixed","name":"Read","arguments":{"file_path":"README.md"}}] :END_EXT_TOOL'


def _sse_data(resp) -> list[dict]:
    events: list[dict] = []
    for line in resp.text.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line.removeprefix("data: ")
        if raw == "[DONE]":
            continue
        events.append(json.loads(raw))
    return events


def test_chat_stream_tool_call_block_becomes_openai_tool_call(tmp_path) -> None:
    fake = FakeCopilotClient()
    fake.stream_chunks = [_tool_call_block()]
    client = _client(fake, tmp_path / "s.db")
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-opus",
            "stream": True,
            "messages": [{"role": "user", "content": "read README"}],
            "tools": [{"type": "function", "function": {"name": "Read"}}],
        },
    )

    assert resp.status_code == 200
    events = _sse_data(resp)
    tool_delta = next(e for e in events if e["choices"][0]["delta"].get("tool_calls"))
    call = tool_delta["choices"][0]["delta"]["tool_calls"][0]
    assert call["id"] == "call_fixed"
    assert call["type"] == "function"
    assert call["function"]["name"] == "Read"
    assert json.loads(call["function"]["arguments"]) == {"file_path": "README.md"}
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_responses_stream_tool_call_block_becomes_function_call_item(tmp_path) -> None:
    fake = FakeCopilotClient()
    fake.stream_chunks = [_tool_call_block()]
    client = _client(fake, tmp_path / "s.db")
    resp = client.post(
        "/v1/responses",
        json={
            "model": "m365-opus",
            "stream": True,
            "input": "read README",
            "tools": [{"type": "function", "function": {"name": "Read"}}],
        },
    )

    assert resp.status_code == 200
    events = _sse_data(resp)
    item = next(e["item"] for e in events if e["type"] == "response.output_item.added")
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_fixed"
    assert item["name"] == "Read"
    assert json.loads(item["arguments"]) == {"file_path": "README.md"}
    completed = next(e for e in events if e["type"] == "response.completed")
    assert completed["response"]["output"] == [item]


def test_anthropic_stream_tool_call_block_becomes_tool_use(tmp_path) -> None:
    fake = FakeCopilotClient()
    fake.stream_chunks = [_tool_call_block()]
    client = _client(fake, tmp_path / "s.db")
    resp = client.post(
        "/v1/messages",
        json={
            "model": "m365-opus",
            "stream": True,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "read README"}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        },
    )

    assert resp.status_code == 200
    events = _sse_data(resp)
    start = next(
        e for e in events
        if e["type"] == "content_block_start"
        and e["content_block"]["type"] == "tool_use"
    )
    assert start["content_block"]["id"] == "call_fixed"
    assert start["content_block"]["name"] == "Read"
    delta = next(
        e for e in events
        if e["type"] == "content_block_delta"
        and e["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(delta["delta"]["partial_json"]) == {"file_path": "README.md"}
    stop = next(e for e in events if e["type"] == "message_delta")
    assert stop["delta"]["stop_reason"] == "tool_use"


def test_anthropic_stream_normal_text_emits_text_delta(tmp_path) -> None:
    fake = FakeCopilotClient()
    fake.stream_chunks = ["normal ", "message"]
    client = _client(fake, tmp_path / "s.db")
    resp = client.post(
        "/v1/messages",
        json={
            "model": "m365-opus",
            "stream": True,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "say hi"}],
        },
    )

    assert resp.status_code == 200
    events = _sse_data(resp)
    delta = next(
        e for e in events
        if e["type"] == "content_block_delta"
        and e["delta"]["type"] == "text_delta"
    )
    assert delta["delta"]["text"] == "normal message"
    stop = next(e for e in events if e["type"] == "message_delta")
    assert stop["delta"]["stop_reason"] == "end_turn"


def _data_uri_png() -> str:
    return "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()


def test_image_url_in_split_user_turn_is_found(tmp_path) -> None:
    """VS Code splits a turn into text / image_url / text user messages; the image is not in the
    LAST user message, so we must scan the whole trailing user run."""
    fake = FakeCopilotClient()
    client = _client(fake, tmp_path / "s.db")
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-opus",
            "messages": [
                {"role": "user", "content": "<attachments>"},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _data_uri_png()}}
                    ],
                },
                {"role": "user", "content": "</attachments>\nwhat do you see?"},
            ],
        },
    )
    assert resp.status_code == 200
    imgs = fake.images[-1]
    assert imgs is not None
    assert isinstance(imgs[0], ExtractedImage) and imgs[0].file_type == "png"


def test_history_images_before_assistant_are_excluded(tmp_path) -> None:
    """An image from a PRIOR turn (before an assistant reply) must not be re-uploaded on a later
    text-only turn — substrate already has it in the reused conversation."""
    fake = FakeCopilotClient()
    client = _client(fake, tmp_path / "s.db")
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-opus",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _data_uri_png()}}
                    ],
                },
                {"role": "assistant", "content": "saw it"},
                {"role": "user", "content": "text-only follow up"},
            ],
        },
    )
    assert resp.status_code == 200
    assert fake.images[-1] is None


def test_concurrency_semaphore() -> None:
    import asyncio
    from copilot_proxy_server.substrate_client import get_concurrency_semaphore

    async def run_test():
        entered: list[int] = []
        release = asyncio.Event()

        async def worker(index: int) -> None:
            async with get_concurrency_semaphore(2):
                entered.append(index)
                if index < 2:
                    await release.wait()

        tasks = [asyncio.create_task(worker(index)) for index in range(4)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert entered == [0, 1]

        release.set()
        await asyncio.gather(*tasks)
        assert entered == [0, 1, 2, 3]

        async with get_concurrency_semaphore(3):
            async with get_concurrency_semaphore(3):
                async with get_concurrency_semaphore(3):
                    pass

    asyncio.run(run_test())


def test_shared_httpx_client() -> None:
    import asyncio
    import httpx
    from copilot_proxy_server.substrate_client import get_shared_httpx_client

    async def run_test():
        client1 = get_shared_httpx_client()
        assert isinstance(client1, httpx.AsyncClient)

        client2 = get_shared_httpx_client()
        assert client1 is client2

    asyncio.run(run_test())
