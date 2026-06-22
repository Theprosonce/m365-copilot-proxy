from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .models import (
    AnthropicMessagesRequest,
    ContentPart,
    ExtractedImage,
    OpenAIChatRequest,
    OpenAIResponsesRequest,
    TranslatedRequest,
)
from middleware.bypass import looks_like_bypass


def flatten_content(content: str | list[ContentPart] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(part.text or "" for part in content if part.type == "text")


def _mime_to_ext(mime: str) -> str:
    ext = (mime or "").lower().split("/")[-1].split(";")[0].strip()
    return {"jpeg": "jpg"}.get(ext, ext) or "png"


def extract_images(content: str | list[ContentPart] | None) -> list[ExtractedImage]:
    """Pull image parts from one message's content.

    Supports OpenAI `image_url` (data: URI) and Anthropic `image`/`source` (base64).
    Remote http(s) image URLs are skipped — substrate's UploadFile wants the bytes inline.
    """
    if not isinstance(content, list):
        return []
    images: list[ExtractedImage] = []
    for part in content:
        data_uri = ""
        mime = ""
        if part.type == "image_url" and isinstance(part.image_url, dict):
            url = part.image_url.get("url", "")
            if isinstance(url, str) and url.startswith("data:"):
                data_uri = url
                mime = url[5:].split(";", 1)[0]
        elif part.type == "image" and isinstance(part.source, dict):
            src = part.source
            if src.get("type") == "base64" and src.get("data"):
                mime = src.get("media_type") or "image/png"
                data_uri = f"data:{mime};base64,{src['data']}"
        if data_uri:
            ext = _mime_to_ext(mime)
            images.append(
                ExtractedImage(
                    data_uri=data_uri, file_type=ext, file_name=f"image.{ext}"
                )
            )
    return images


# VS Code custom endpoints (vendor "customendpoint") do NOT inline image bytes: they embed a
# file:// reference in the prompt text, e.g.
#   <attachment name="image" id="image:file:///c%3A/Users/me/Pictures/shot.png">
# The proxy runs on the same machine, so we resolve those references off disk.
_ATTACHMENT_RE = re.compile(r'id="image:(file://[^"]+)"')
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def extract_file_attachments(text: str) -> list[ExtractedImage]:
    """Resolve VS Code `image:file://...` attachment references by reading the local file and
    inlining it as a data: URI. Only image extensions, existing files, under the size cap."""
    if not text or "image:file://" not in text:
        return []
    import os

    out: list[ExtractedImage] = []
    for m in _ATTACHMENT_RE.finditer(text):
        parsed_path = unquote(urlparse(m.group(1)).path)
        path = Path(parsed_path if os.name != "nt" else parsed_path.lstrip("/"))
        ext = path.suffix.lower().lstrip(".")
        if ext not in _IMAGE_EXTS or not path.is_file():
            continue
        try:
            if path.stat().st_size > _MAX_IMAGE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"
        b64 = base64.b64encode(data).decode("ascii")
        out.append(
            ExtractedImage(
                data_uri=f"data:{mime};base64,{b64}",
                file_type=_mime_to_ext(mime),
                file_name=path.name,
            )
        )
    return out


def _join_lines(lines: Iterable[str]) -> str:
    return "\n".join(line for line in lines if line).strip()


def _summarize_tool_calls(tool_calls: Any) -> str:
    parts: list[str] = []
    for tc in tool_calls or []:
        fn = getattr(tc, "function", None)
        if fn is not None:
            parts.append(f"{fn.name}({fn.arguments})")
        elif isinstance(tc, dict):
            f = tc.get("function", {})
            parts.append(f"{f.get('name')}({f.get('arguments', '{}')})")
    return "; ".join(parts)


def translate_openai_request(request: OpenAIChatRequest) -> TranslatedRequest:
    system_lines: list[str] = []
    transcript_lines: list[str] = []
    tool_result_lines: list[str] = []
    last_user_text: str | None = None

    for message in request.messages:
        text = flatten_content(message.content).strip()
        if message.role in {"system", "developer"}:
            if text:
                system_lines.append(text)
        elif message.role == "assistant":
            if message.tool_calls:
                transcript_lines.append(
                    f"Assistant (tool call): {_summarize_tool_calls(message.tool_calls)}"
                )
            if text and not looks_like_bypass(
                text
            ):  # drop prior refusals so they don't reinforce
                transcript_lines.append(f"Assistant: {text}")
        elif message.role == "tool":
            ref = message.name or message.tool_call_id or "tool"
            tool_result_lines.append(f"Tool result [{ref}]: {text}")
        else:  # user
            if text:
                transcript_lines.append(f"User: {text}")
                last_user_text = text

    last = request.messages[-1] if request.messages else None
    if last is not None and last.role == "user" and last_user_text:
        prompt = last_user_text
        for i in range(len(transcript_lines) - 1, -1, -1):
            if transcript_lines[i] == f"User: {last_user_text}":
                del transcript_lines[i]
                break
    else:
        # Agentic continuation: the last turn is a tool result or assistant action.
        prompt = (
            "Continue the task using the conversation and tool results above. "
            "Either call the next tool(s) using the block format, or give your final answer."
        )

    if not prompt:
        raise ValueError("A usable prompt is required.")

    additional_context: list[str] = []
    system_text = _join_lines(system_lines)
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join_lines(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")
    tool_results_text = _join_lines(tool_result_lines)
    if tool_results_text:
        additional_context.append(f"Tool results:\n{tool_results_text}")
    return TranslatedRequest(prompt=prompt, additional_context=additional_context)


def translate_responses_request(request: "OpenAIResponsesRequest") -> TranslatedRequest:
    instructions = request.instructions or ""
    if isinstance(request.input, str):
        return TranslatedRequest(
            prompt=request.input,
            additional_context=[f"System instructions:\n{instructions}"]
            if instructions
            else [],
        )
    # input is a list of message dicts
    system_lines: list[str] = []
    if instructions:
        system_lines.append(instructions)
    transcript_lines: list[str] = []
    prompt = ""
    items = request.input
    for index, item in enumerate(items):
        role = item.get("role", "") if isinstance(item, dict) else ""
        content = item.get("content", "") if isinstance(item, dict) else str(item)
        if isinstance(content, list):
            content = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            )
        text = content.strip()
        if not text:
            continue
        is_last = index == len(items) - 1
        if role in {"system", "developer"}:
            system_lines.append(text)
            continue
        if is_last:
            if role != "user":
                raise ValueError(
                    "The final Responses input message must be a user message."
                )
            prompt = text
            continue
        transcript_lines.append(f"{role.capitalize()}: {text}")
    if not prompt:
        raise ValueError("No user message found in input.")
    additional_context: list[str] = []
    system_text = _join_lines(system_lines)
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join_lines(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")
    return TranslatedRequest(prompt=prompt, additional_context=additional_context)


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(
                    b.get("text", "")
                    if b.get("type") == "text"
                    else json.dumps(b, ensure_ascii=False)
                )
            else:
                parts.append(str(b))
        return "\n".join(p for p in parts if p)
    return str(content)


def translate_anthropic_request(
    request: AnthropicMessagesRequest,
) -> TranslatedRequest:
    system_lines: list[str] = []
    base_system = flatten_content(request.system).strip()
    if base_system:
        system_lines.append(base_system)
    transcript_lines: list[str] = []
    tool_result_lines: list[str] = []
    last_user_text: str | None = None

    for message in request.messages:
        role = message.role
        content = message.content
        if role in {"system", "developer"}:
            sys_text = flatten_content(content).strip()
            if sys_text:
                system_lines.append(sys_text)
            continue
        if isinstance(content, str):
            text = content.strip()
            if text:
                transcript_lines.append(f"{role.capitalize()}: {text}")
                if role == "user":
                    last_user_text = text
            continue
        user_text_parts: list[str] = []
        for part in content:
            if part.type == "text" and part.text:
                t = part.text.strip()
                if role == "assistant" and looks_like_bypass(t):
                    continue  # drop prior refusals so they don't reinforce
                transcript_lines.append(f"{role.capitalize()}: {t}")
                if role == "user":
                    user_text_parts.append(t)
            elif part.type == "tool_use":
                args = json.dumps(part.input or {}, ensure_ascii=False)
                transcript_lines.append(f"Assistant (tool call): {part.name}({args})")
            elif part.type == "tool_result":
                ref = part.tool_use_id or "tool"
                tool_result_lines.append(
                    f"Tool result [{ref}]: {_tool_result_text(part.content)}"
                )
        if role == "user" and user_text_parts:
            last_user_text = "\n".join(user_text_parts)

    last = request.messages[-1] if request.messages else None
    last_user_text_current_turn = (
        flatten_content(last.content).strip()
        if last is not None and last.role == "user"
        else ""
    )
    if last is not None and last.role == "user" and last_user_text_current_turn:
        prompt = last_user_text_current_turn
        for i in range(len(transcript_lines) - 1, -1, -1):
            if transcript_lines[i] == f"User: {last_user_text_current_turn}":
                del transcript_lines[i]
                break
    else:
        # Anthropic tool_result blocks are user-role messages without text. Treat
        # those as agentic continuations, not as a repeat of an earlier user prompt.
        prompt = (
            "Continue the task using the conversation and tool results above. "
            "Either call the next tool(s) using the block format, or give your final answer."
        )

    additional_context: list[str] = []
    system_text = _join_lines(system_lines)
    if system_text:
        additional_context.append(f"System instructions:\n{system_text}")
    transcript_text = _join_lines(transcript_lines)
    if transcript_text:
        additional_context.append(f"Prior conversation transcript:\n{transcript_text}")
    tool_results_text = _join_lines(tool_result_lines)
    if tool_results_text:
        additional_context.append(f"Tool results:\n{tool_results_text}")
    return TranslatedRequest(prompt=prompt, additional_context=additional_context)
