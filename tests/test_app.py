from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.graph_client import filter_reserved_scopes


class FakeCopilotClient:
    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    async def chat(self, prompt: str, additional_context: list[str]) -> str:
        self.calls.append((prompt, additional_context))
        return "copilot reply"

    async def chat_stream(self, prompt: str, additional_context: list[str]) -> AsyncIterator[str]:
        self.calls.append((prompt, additional_context))
        yield "hello"
        yield " world"


def build_client(fake: FakeCopilotClient) -> TestClient:
    settings = Settings(M365_ACCESS_TOKEN="fake-token")
    app = create_app(settings=settings, copilot_client_factory=lambda: fake)
    return TestClient(app)


def test_models_endpoint() -> None:
    client = build_client(FakeCopilotClient())
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["id"] == "m365-copilot"


def test_openai_chat_completion_translates_history() -> None:
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
            "Second question",
            [
                "System instructions:\nBe concise.",
                "Prior conversation transcript:\nUser: First question\nAssistant: First answer",
            ],
        )
    ]


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


def test_reserved_scopes_are_filtered_from_msal_requests() -> None:
    assert filter_reserved_scopes(
        [
            "Mail.Read",
            "offline_access",
            "openid",
            "Chat.Read",
        ]
    ) == ["Mail.Read", "Chat.Read"]
