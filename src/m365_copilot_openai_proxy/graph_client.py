from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import msal

from .config import Settings
from .models import CopilotConversation


class GraphCopilotError(RuntimeError):
    pass


RESERVED_SCOPES = {"openid", "profile", "offline_access"}


def _load_token_cache(path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))
    return cache


def _save_token_cache(path: Path, cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        path.write_text(cache.serialize(), encoding="utf-8")


def filter_reserved_scopes(scopes: list[str] | tuple[str, ...]) -> list[str]:
    return [scope for scope in scopes if scope not in RESERVED_SCOPES]


class DeviceCodeTokenProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._cache = _load_token_cache(settings.token_cache_path)
        self._app = msal.PublicClientApplication(
            client_id=settings.client_id,
            authority=settings.authority,
            token_cache=self._cache,
        )

    def get_access_token(self) -> str:
        accounts = self._app.get_accounts()
        result = None
        scopes = self._effective_scopes()
        if accounts:
            result = self._app.acquire_token_silent(
                scopes=scopes,
                account=accounts[0],
            )
        if not result:
            flow = self._app.initiate_device_flow(scopes=scopes)
            if "user_code" not in flow:
                raise GraphCopilotError("Failed to start device-code flow.")
            print(flow["message"])
            result = self._app.acquire_token_by_device_flow(flow)
        _save_token_cache(self._settings.token_cache_path, self._cache)
        if not result or "access_token" not in result:
            error = result.get("error_description") if isinstance(result, dict) else None
            raise GraphCopilotError(error or "Failed to acquire a Graph access token.")
        return result["access_token"]

    def _effective_scopes(self) -> list[str]:
        return filter_reserved_scopes(self._settings.graph_scopes)


class GraphCopilotClient:
    def __init__(
        self,
        settings: Settings,
        token_provider: DeviceCodeTokenProvider | None = None,
    ):
        self._settings = settings
        self._token_provider = token_provider or DeviceCodeTokenProvider(settings)

    async def create_conversation(self) -> str:
        data = await self._request_json("POST", "/copilot/conversations", json_body={})
        conversation = CopilotConversation.model_validate(data)
        return conversation.id

    async def chat(
        self,
        prompt: str,
        additional_context: list[str],
    ) -> CopilotConversation:
        conversation_id = await self.create_conversation()
        data = await self._request_json(
            "POST",
            f"/copilot/conversations/{conversation_id}/chat",
            json_body=self._chat_body(prompt, additional_context),
        )
        return CopilotConversation.model_validate(data)

    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
    ) -> AsyncIterator[str]:
        conversation_id = await self.create_conversation()
        body = self._chat_body(prompt, additional_context)
        async for text in self._stream_chat(conversation_id, prompt, body):
            yield text

    def _chat_body(self, prompt: str, additional_context: list[str]) -> dict:
        body: dict[str, object] = {
            "message": {"text": prompt},
            "locationHint": {"timeZone": self._settings.time_zone},
        }
        if additional_context:
            body["additionalContext"] = [{"text": item} for item in additional_context]
        return body

    async def _request_json(
        self,
        method: str,
        path: str,
        json_body: dict,
    ) -> dict:
        token = self._token_provider.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(base_url=self._settings.graph_base_url, timeout=60) as client:
            response = await client.request(method, path, headers=headers, json=json_body)
        if response.status_code >= 400:
            raise GraphCopilotError(
                f"Graph request failed with {response.status_code}: {response.text}"
            )
        return response.json()

    async def _stream_chat(
        self,
        conversation_id: str,
        prompt: str,
        body: dict,
    ) -> AsyncIterator[str]:
        token = self._token_provider.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        previous_text = ""
        async with httpx.AsyncClient(base_url=self._settings.graph_base_url, timeout=None) as client:
            async with client.stream(
                "POST",
                f"/copilot/conversations/{conversation_id}/chatOverStream",
                headers=headers,
                json=body,
            ) as response:
                if response.status_code >= 400:
                    raise GraphCopilotError(
                        f"Graph stream failed with {response.status_code}: {await response.aread()}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    conversation = CopilotConversation.model_validate(data)
                    current_text = extract_assistant_text(conversation, prompt)
                    if not current_text.startswith(previous_text):
                        previous_text = ""
                    delta = current_text[len(previous_text) :]
                    previous_text = current_text
                    if delta:
                        yield delta


def extract_assistant_text(conversation: CopilotConversation, prompt: str) -> str:
    if not conversation.messages:
        return ""
    if len(conversation.messages) == 1 and conversation.messages[0].text == prompt:
        return ""
    return conversation.messages[-1].text
