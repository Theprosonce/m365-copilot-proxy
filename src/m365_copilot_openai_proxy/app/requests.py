from __future__ import annotations

import json

from fastapi import Request

from ..config import Settings
from ..models import ExtractedImage
from ..translator import extract_file_attachments, extract_images, flatten_content
from .common import _debug_dump

async def _debug_raw(raw_request: Request) -> None:
    """Log inbound headers + top-level body keys, to discover any stable per-chat id the client
    sends (which we'd otherwise drop via pydantic extra='ignore')."""
    if not Settings().debug:
        return
    try:
        headers = {
            k: v
            for k, v in raw_request.headers.items()
            if k.lower() not in ("authorization", "cookie")
        }
        body = await raw_request.json()
        # Full request straight into debug.log (headers + entire body, message/content structure
        # intact) so we can see exactly how/whether the client encodes an image.
        _debug_dump(
            "RAW REQUEST (FULL)",
            f"headers={json.dumps(headers, ensure_ascii=False)}\n"
            f"body={json.dumps(body, ensure_ascii=False)}",
        )
    except Exception as exc:
        _debug_dump("RAW REQUEST", f"(could not introspect: {exc})")


def _debug_images(messages: list | None, images: list[ExtractedImage] | None) -> None:
    """Log exactly what the current user turn carried and what we resolved, so we can see
    whether VS Code sent an inline image, a file:// attachment, or an empty <attachments> tag."""
    if not Settings().debug:
        return
    raw = ""
    for m in reversed(messages or []):
        if getattr(m, "role", None) == "user":
            raw = flatten_content(getattr(m, "content", None))
            break
    resolved = [f"{i.file_name}({len(i.data_uri)}b)" for i in (images or [])]
    _debug_dump(
        "IMAGES", f"resolved={resolved}\nlast_user_content[:2000]={raw[:2000]!r}"
    )


def _request_images(messages: list | None) -> list[ExtractedImage] | None:
    """Images from the CURRENT turn. VS Code splits one turn into several consecutive user
    messages (text / image_url / text), so we scan the trailing run of user messages, not just
    the last one — otherwise the image (in a non-last user message) is missed. History images
    (before an assistant/tool reply) are excluded, so they are not re-uploaded.

    Sources: inline `image_url`/`source` parts (VS Code, OpenAI, Anthropic) and VS Code's
    `image:file://` attachment references resolved off the local disk."""
    if not messages:
        return None
    images: list[ExtractedImage] = []
    text_parts: list[str] = []
    for m in reversed(messages):
        if getattr(m, "role", None) != "user":
            break  # reached the previous assistant/tool reply -> end of current turn
        content = getattr(m, "content", None)
        images = list(extract_images(content)) + images
        text_parts.append(flatten_content(content))
    if images:
        return images
    resolved = extract_file_attachments("\n".join(text_parts))
    return resolved or None
