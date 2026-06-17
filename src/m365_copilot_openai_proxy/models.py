from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class ExtractedImage:
    """An inline image pulled from a request, ready to upload to substrate."""

    data_uri: str  # data:image/<type>;base64,<...>
    file_type: str  # e.g. "png", "jpg"
    file_name: str  # e.g. "image.png"


# A substrate `messageAnnotations[]` entry referencing an uploaded image by its docId.
# Kept as a TypedDict so it serializes directly into the WS frame JSON.
_AnnotationMetadata = TypedDict(
    "_AnnotationMetadata",
    {"@type": str, "annotationType": str, "fileType": str, "fileName": str},
)


class MessageAnnotation(TypedDict):
    id: str  # the docId returned by UploadFile
    messageAnnotationMetadata: _AnnotationMetadata
    messageAnnotationType: str  # "ImageFile"


class UploadFileResponse(TypedDict, total=False):
    """Substrate /m365Copilot/UploadFile JSON response (captured). `docId` is the only
    field we consume — it becomes the `messageAnnotations[].id` referencing the image."""

    docId: str
    fileName: str
    fileType: str
    fileSize: int
    conversationId: str
    result: dict[str, Any]


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = None
    # Anthropic tool_use (assistant) blocks
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    # Anthropic tool_result (user) blocks
    tool_use_id: str | None = None
    content: Any | None = None
    # OpenAI vision: {"url": "data:image/png;base64,..."}
    image_url: dict[str, Any] | None = None
    # Anthropic vision: {"type": "base64", "media_type": "image/png", "data": "..."}
    source: dict[str, Any] | None = None


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: str = "{}"  # OpenAI sends arguments as a JSON-encoded string.


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class OpenAIMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class OpenAIChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[OpenAIMessage]
    stream: bool = False
    temperature: float | None = None
    user: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    functions: list[dict[str, Any]] | None = None
    function_call: Any | None = None


class AnthropicMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str  # usually user/assistant, but clients also send system/tool
    content: str | list[ContentPart]


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[AnthropicMessage]
    system: str | list[ContentPart] | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None


class CopilotMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    text: str = ""
    attributions: list[dict[str, Any]] = Field(default_factory=list)


class CopilotConversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    messages: list[CopilotMessage] = Field(default_factory=list)


class OpenAIResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    input: str | list[Any]
    instructions: str | None = None
    stream: bool = False


class TranslatedRequest(BaseModel):
    prompt: str
    additional_context: list[str] = Field(default_factory=list)


# --- Session-store CRUD API ---


class ChatInfo(BaseModel):
    """A tracked conversation mapping (proxy key -> substrate conversation)."""

    key: str
    conversation_id: str
    client_session_id: str
    turns: int
    label: str
    created_at: int
    age_seconds: int
    idle_seconds: int


class ChatListResponse(BaseModel):
    count: int
    chats: list[ChatInfo]


class ChatCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Optional explicit key; if omitted, the proxy generates a `manual:<uuid>` key.
    key: str | None = None
    label: str = ""


class ChatUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str | None = None
    # True -> start a fresh substrate conversation under the same key (new ids, turn reset).
    rotate: bool = False


class DeleteResponse(BaseModel):
    key: str
    deleted: bool
