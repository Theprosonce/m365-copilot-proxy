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

from m365_copilot_openai_proxy.app import (
    _conversation_key,
    _first_real_user_text,
    _trim_history,
    create_app,
)
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.models import ContentPart, ExtractedImage, OpenAIMessage
from m365_copilot_openai_proxy.session_store import (
    PersistentSession,
    PersistentSessionStore,
)
from m365_copilot_openai_proxy.substrate_client import (
    SubstrateCopilotClient,
    _combine_text,
    resolve_tone,
)
from m365_copilot_openai_proxy.translator import (
    extract_file_attachments,
    extract_images,
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

    async def chat(
        self, prompt, additional_context, session=None, tone=None, images=None
    ) -> str:
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        self.images.append(images)
        return "reply"

    async def chat_stream(
        self, prompt, additional_context, session=None, tone=None, images=None
    ) -> AsyncIterator[str]:
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        self.images.append(images)
        yield "x"


def _client(
    fake: FakeCopilotClient, tmp_db: Path, persist_default: bool = True
) -> TestClient:
    settings = Settings(
        access_token="fake",
        persist_default=persist_default,
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
            role="user", content="<environment_info>OS Windows</environment_info>"
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


# --- history trimming on continued turns ---


def test_trim_history_drops_transcript_after_first_turn() -> None:
    ctx = [
        "System instructions:\nbe nice",
        "Prior conversation transcript:\nUser: a\nAssistant: b",
    ]
    fresh = PersistentSession()  # turn_count 0
    assert _trim_history(list(ctx), fresh) == ctx  # first turn keeps everything
    continued = PersistentSession()
    continued.turn_count = 3
    trimmed = _trim_history(list(ctx), continued)
    assert trimmed == ["System instructions:\nbe nice"]  # transcript dropped


def test_trim_history_preserves_tool_results() -> None:
    """Tool results must be preserved even when transcript is trimmed."""
    ctx = [
        "System instructions:\nbe nice",
        "Prior conversation transcript:\nUser: read file\nAssistant (tool call): read({})",
        "Tool results:\nTool result [call_123]: <content>Hello, world!</content>",
    ]
    fresh = PersistentSession()  # turn_count 0
    assert _trim_history(list(ctx), fresh) == ctx  # first turn keeps everything
    continued = PersistentSession()
    continued.turn_count = 3
    trimmed = _trim_history(list(ctx), continued)
    # System instructions and Tool results kept, transcript dropped
    assert "System instructions:\nbe nice" in trimmed
    assert any("Tool result [call_123]" in t for t in trimmed), (
        "Tool results must be preserved"
    )
    assert not any("Prior conversation transcript:" in t for t in trimmed)


def test_combine_text_leads_with_prompt() -> None:
    out = _combine_text("THE REAL MESSAGE", ["big reference context"])
    assert out.startswith("THE REAL MESSAGE")
    assert "big reference context" in out


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
    assert resolve_tone("m365-gpt") == "Gpt_5_5_Chat"
    assert resolve_tone("m365-gpt-think") == "Gpt_5_5_Reasoning"
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
