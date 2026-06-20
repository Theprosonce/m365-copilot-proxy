from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings
from .session_store import PersistentSession, PersistentSessionStore
from .substrate_client import (
    MODEL_TO_TONE,
    SubstrateCopilotClient,
    SubstrateCopilotError,
    resolve_tone,
)
from .token_store import AccessTokenStore
from .models import (
    AnthropicMessagesRequest,
    ChatCreateRequest,
    ChatInfo,
    ChatListResponse,
    ChatUpdateRequest,
    DeleteResponse,
    ExtractedImage,
    OpenAIChatRequest,
    OpenAIResponsesRequest,
)
from .translator import (
    extract_file_attachments,
    extract_images,
    flatten_content,
    translate_anthropic_request,
    translate_openai_request,
    translate_responses_request,
)
from .tool_middleware.tool_emulation import ToolEmulationPipeline


def _debug_dump(label: str, content: str) -> None:
    if not os.environ.get("M365_DEBUG"):
        return
    try:
        with Path("debug.log").open("a", encoding="utf-8") as f:
            f.write(f"\n===== {label} =====\n{content}\n")
    except Exception:
        pass


_PERSIST_MODEL_SUFFIX = ":persist"
_SESSION_ID_HEADER = "x-m365-session-id"


def _is_proxy_model(settings: Settings, model: str | None) -> bool:
    """True if the model is one of ours (route to substrate); False -> passthrough candidate."""
    base = (model or "").split(":", 1)[0].strip().lower()
    if not base:
        return True  # no model -> keep current substrate default
    return (
        base == settings.model_alias.lower()
        or base in MODEL_TO_TONE
        or base.startswith("m365")
    )


def create_app(
    settings: Settings | None = None,
    copilot_client_factory: Callable[[], SubstrateCopilotClient] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Microsoft 365 Copilot OpenAI Proxy",
        description=(
            "OpenAI/Anthropic-compatible proxy over Microsoft 365 Copilot (substrate). "
            "Exposes the model picker (`tone`), Work/Web grounding, a ReAct tool-calling shim, "
            "vision (image upload), and CRUD over the persisted conversation mappings. "
            "Interactive docs at /docs, schema at /openapi.json."
        ),
        version="0.2.0",
        openapi_tags=[
            {
                "name": "inference",
                "description": "OpenAI/Anthropic-compatible chat endpoints.",
            },
            {
                "name": "chats",
                "description": "CRUD over persisted conversation mappings (key -> substrate conversation).",
            },
            {"name": "ops", "description": "Health and token status."},
        ],
    )
    resolved_settings = settings or Settings()
    app.state.settings = resolved_settings
    app.state.token_store = AccessTokenStore(resolved_settings.access_token)
    app.state.session_store = PersistentSessionStore(
        db_path=resolved_settings.session_db_path
        or str(Path.home() / ".m365-copilot-openai-proxy" / "sessions.db"),
        max_sessions=resolved_settings.session_max,
        ttl_seconds=resolved_settings.session_ttl_seconds,
    )
    app.state.copilot_client_factory = copilot_client_factory or (
        lambda: SubstrateCopilotClient(
            app.state.token_store.get(),
            resolved_settings.time_zone,
            resolved_settings.work_grounding,
            resolved_settings.recv_timeout,
            resolved_settings.open_timeout,
            resolved_settings.disable_memory,
        )
    )

    def get_settings() -> Settings:
        return app.state.settings

    def get_copilot_client() -> SubstrateCopilotClient:
        try:
            return app.state.copilot_client_factory()
        except SubstrateCopilotError as exc:
            # e.g. token missing/expired: surface a clean 502, not an unhandled 500.
            try:
                status = app.state.token_store.status()
            except Exception:
                status = "?"
            print(f"[502] substrate client unavailable: {exc} | token={status}")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        return {"status": "ok", "token": app.state.token_store.status()}

    @app.get("/v1/token/status", tags=["ops"])
    async def token_status() -> dict:
        return app.state.token_store.status()

    @app.get("/v1/models", tags=["inference"])
    async def list_models(settings: Settings = Depends(get_settings)) -> dict:
        ids = [settings.model_alias, *MODEL_TO_TONE.keys()]
        seen: list[str] = []
        for mid in ids:
            if mid not in seen:
                seen.append(mid)
        return {
            "object": "list",
            "data": [
                {"id": mid, "object": "model", "owned_by": "microsoft-365-copilot"}
                for mid in seen
            ],
        }

    @app.get("/v1/chats", response_model=ChatListResponse, tags=["chats"])
    async def list_chats() -> ChatListResponse:
        chats = [_chat_info(key, s) for key, s in app.state.session_store.items()]
        chats.sort(key=lambda c: c.idle_seconds)
        return ChatListResponse(count=len(chats), chats=chats)

    @app.post("/v1/chats", response_model=ChatInfo, status_code=201, tags=["chats"])
    async def create_chat(body: ChatCreateRequest) -> ChatInfo:
        key = body.key or f"manual:{uuid.uuid4().hex}"
        session = app.state.session_store.create(key, label=body.label)
        return _chat_info(key, session)

    @app.get("/v1/chats/{key:path}", response_model=ChatInfo, tags=["chats"])
    async def get_chat(key: str) -> ChatInfo:
        session = app.state.session_store.find(key)
        if session is None:
            raise HTTPException(status_code=404, detail=f"No chat with key {key!r}")
        return _chat_info(key, session)

    @app.patch("/v1/chats/{key:path}", response_model=ChatInfo, tags=["chats"])
    async def update_chat(key: str, body: ChatUpdateRequest) -> ChatInfo:
        session = app.state.session_store.update(
            key, label=body.label, rotate=body.rotate
        )
        if session is None:
            raise HTTPException(status_code=404, detail=f"No chat with key {key!r}")
        return _chat_info(key, session)

    @app.delete("/v1/chats/{key:path}", response_model=DeleteResponse, tags=["chats"])
    async def delete_chat(key: str) -> DeleteResponse:
        deleted = app.state.session_store.delete(key)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"No chat with key {key!r}")
        return DeleteResponse(key=key, deleted=True)

    @app.post("/v1/chat/completions", tags=["inference"])
    async def chat_completions(
        raw_request: Request,
        request: OpenAIChatRequest,
        settings: Settings = Depends(get_settings),
        client: SubstrateCopilotClient = Depends(get_copilot_client),
    ):
        print(
            f"-> /v1/chat/completions model={request.model!r} stream={request.stream} tools={bool(request.tools)}"
        )
        try:
            await _debug_raw(raw_request)
            pipeline = ToolEmulationPipeline(settings)

            original_stream = request.stream
            is_emulating = pipeline.is_emulation_active(request)
            request, tools_prompt, normalized_tools = pipeline.preflight(request)

            translated = translate_openai_request(request)
            session = _persistent_session(
                app, raw_request, request.model, request.user, request.messages
            )
            tone = resolve_tone(request.model)
            images = _request_images(request.messages)
            _debug_images(request.messages, images)
            _debug_dump(
                "SESSION",
                f"model={request.model} persist={session is not None} conv={getattr(session, 'conversation_id', None)} turn={getattr(session, 'turn_count', None)} hint={_project_hint(request.messages)!r}",
            )
            ctx = _trim_history(list(translated.additional_context), session)
            prompt = translated.prompt

            if tools_prompt:
                # The client's own system prompt frames the model as a different agent -> drop it.
                ctx = [c for c in ctx if not c.startswith("System instructions:")]
                tool_names = [t.get("name") for t in normalized_tools]
                _debug_dump(
                    "REQUEST",
                    f"tools={tool_names}\ntool_choice={request.tool_choice}\nctx={json.dumps(ctx, ensure_ascii=False)[:4000]}",
                )
                # Put the protocol IN the user turn
                prompt = f"{tools_prompt}\n\n# Conversation / current request\n{translated.prompt}"
                _debug_dump("FINAL PROMPT", prompt[:6000])

            if request.stream:
                return StreamingResponse(
                    _openai_stream(
                        settings.model_alias, client, prompt, ctx, session, tone, images
                    ),
                    media_type="text/event-stream",
                )

            if is_emulating:
                calls, text = await pipeline.execute_upstream(
                    client,
                    prompt,
                    ctx,
                    session,
                    tone,
                    normalized_tools,
                    images=images,
                    # No reliable project root -> None so paths are NOT rewritten to the server's
                    # CWD; the client executes the calls against its own workspace.
                    workspace_root=_project_hint(request.messages) or None,
                )
            else:
                calls, text = (
                    None,
                    await client.chat(prompt, ctx, session, tone, images),
                )
        except ValueError as exc:
            print(f"[400] bad request: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SubstrateCopilotError as exc:
            print(f"[502] substrate error: {exc}")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if original_stream:
            return StreamingResponse(
                _openai_emulated_stream(settings.model_alias, calls, text),
                media_type="text/event-stream",
            )

        if calls:
            return JSONResponse(
                {
                    "id": f"chatcmpl_{uuid.uuid4().hex}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": settings.model_alias,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [c.model_dump() for c in calls],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                }
            )

        return JSONResponse(
            {
                "id": f"chatcmpl_{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": settings.model_alias,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    @app.post("/v1/responses", tags=["inference"])
    async def openai_responses(
        raw: Request,
        settings: Settings = Depends(get_settings),
        client: SubstrateCopilotClient = Depends(get_copilot_client),
    ):
        body = await raw.json()
        try:
            request = OpenAIResponsesRequest.model_validate(body)
            translated = translate_responses_request(request)
            session = _persistent_session(app, raw, request.model)
            tone = resolve_tone(request.model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if request.stream:
            return StreamingResponse(
                _responses_stream(
                    settings.model_alias,
                    client,
                    translated.prompt,
                    translated.additional_context,
                    session,
                    tone,
                ),
                media_type="text/event-stream",
            )

        try:
            text = await client.chat(
                translated.prompt, translated.additional_context, session, tone
            )
        except SubstrateCopilotError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return JSONResponse(
            {
                "id": f"resp_{uuid.uuid4().hex}",
                "object": "response",
                "created_at": int(time.time()),
                "model": settings.model_alias,
                "output": [
                    {
                        "type": "message",
                        "id": f"msg_{uuid.uuid4().hex}",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            }
        )

    @app.post("/v1/messages", tags=["inference"])
    async def anthropic_messages(
        raw_request: Request,
        request: AnthropicMessagesRequest,
        settings: Settings = Depends(get_settings),
    ):
        # Passthrough: a model that isn't ours -> forward verbatim to the real Anthropic API.
        # Checked BEFORE resolving the substrate client, so an expired substrate token (they last
        # ~1h) never blocks passthrough.
        ours = _is_proxy_model(settings, request.model)
        print(
            f"-> /v1/messages model={request.model!r} stream={getattr(request, 'stream', False)} "
            f"route={'substrate' if ours else 'passthrough'} passthrough_enabled={settings.anthropic_passthrough}"
        )
        if settings.anthropic_passthrough and not ours:
            from .anthropic_passthrough import credential_available, forward_messages

            if credential_available(settings):
                return await forward_messages(
                    settings, await raw_request.body(), raw_request.headers
                )
            print(
                "  ! passthrough requested but no Anthropic credential available -> using substrate"
            )
        client = get_copilot_client()
        try:
            pipeline = ToolEmulationPipeline(settings)

            is_emulating = pipeline.is_emulation_active(
                OpenAIChatRequest(
                    model=request.model,
                    messages=[],
                    tools=request.tools,
                    tool_choice=request.tool_choice,
                )
            )
            dummy_req = OpenAIChatRequest(
                model=request.model,
                messages=[],
                tools=request.tools,
                tool_choice=request.tool_choice,
            )
            dummy_req, tools_prompt, normalized_tools = pipeline.preflight(dummy_req)
            original_stream = getattr(request, "stream", False)
            if is_emulating and pipeline.settings.tool_emulation_force_non_streaming:
                request.stream = False

            translated = translate_anthropic_request(request)
            session = _persistent_session(
                app, raw_request, request.model, None, request.messages
            )
            tone = resolve_tone(request.model)
            images = _request_images(request.messages)
            _debug_images(request.messages, images)
            ctx = _trim_history(list(translated.additional_context), session)
            prompt = translated.prompt
            _debug_dump(
                "ANTHROPIC REQUEST",
                f"model={request.model} n_tools={len(request.tools) if request.tools else 0} tool_choice={request.tool_choice}\nuser_prompt_tail={translated.prompt[-500:]!r}",
            )
            if tools_prompt:
                ctx = [c for c in ctx if not c.startswith("System instructions:")]
                prompt = f"{tools_prompt}\n\n# Conversation / current request\n{translated.prompt}"

            if request.stream:
                return StreamingResponse(
                    _anthropic_stream(
                        settings.model_alias, client, prompt, ctx, session, tone, images
                    ),
                    media_type="text/event-stream",
                )

            if is_emulating:
                calls, text = await pipeline.execute_upstream(
                    client,
                    prompt,
                    ctx,
                    session,
                    tone,
                    normalized_tools,
                    images=images,
                    # No reliable project root -> None so paths are NOT rewritten to the server's
                    # CWD; the client executes the calls against its own workspace.
                    workspace_root=_project_hint(request.messages) or None,
                )
            else:
                calls, text = (
                    None,
                    await client.chat(prompt, ctx, session, tone, images),
                )
        except ValueError as exc:
            print(f"[400] bad request: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SubstrateCopilotError as exc:
            print(f"[502] substrate error: {exc}")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if original_stream:
            return StreamingResponse(
                _anthropic_emulated_stream(settings.model_alias, calls, text),
                media_type="text/event-stream",
            )

        if calls:
            content = [
                {
                    "type": "tool_use",
                    "id": c.id,
                    "name": c.function.name,
                    "input": _args_obj(c.function.arguments),
                }
                for c in calls
            ]
            return JSONResponse(
                {
                    "id": f"msg_{uuid.uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "model": settings.model_alias,
                    "content": content,
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            )

        return JSONResponse(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": settings.model_alias,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        )

    return app


def _args_obj(arguments: str) -> dict:
    try:
        obj = json.loads(arguments or "{}")
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


# Per-process salt so a conversation key is not a bare content hash (privacy / no cross-store
# collisions). Override with M365_SESSION_SALT to make keys stable across restarts.
_SESSION_SALT = os.environ.get("M365_SESSION_SALT") or uuid.uuid4().hex

# Project/working-directory hint, so the SAME opening (e.g. /init) in DIFFERENT projects does
# not collide into one substrate conversation. Labelled form first, then any absolute path.
_CWD_LABEL_RE = re.compile(
    r"(?:working directory|current working directory|cwd|project root|workspace(?: ?root| ?folder)?)"
    r"\s*[:=]?\s*[\"'`<]?\s*([A-Za-z]:[\\/][^\s\"'`<>\n]+|/[^\s\"'`<>\n]+)",
    re.IGNORECASE,
)
_ANY_PATH_RE = re.compile(r"([A-Za-z]:[\\/][^\s\"'`<>\n]{2,}|/[A-Za-z0-9._\-/]{3,})")


def _msg_text(m) -> str:
    content = getattr(m, "content", "")
    if isinstance(content, list):
        return " ".join(getattr(p, "text", "") or "" for p in content)
    return str(content or "")


def _project_hint(messages: list) -> str:
    """Best-effort stable project path from the conversation (cwd in the client's system block)."""
    blob = " ".join(
        _msg_text(m) for m in messages if getattr(m, "role", None) != "assistant"
    )
    mm = _CWD_LABEL_RE.search(blob)
    if mm:
        return mm.group(1).rstrip("\\/")
    mm = _ANY_PATH_RE.search(blob)
    return mm.group(1).rstrip("\\/") if mm else ""


# VS Code injects these identical wrapper turns at the head of EVERY chat in a workspace; keying
# on them would collapse all chats in one project into a single substrate conversation.
_VSCODE_WRAPPERS = ("<environment_info", "<workspace_info", "<attachments")


def _first_real_user_text(messages: list) -> str:
    """First user turn that carries real content (skips VS Code's env/workspace wrapper turns),
    so distinct chats in the same workspace get distinct keys."""
    fallback = ""
    for m in messages:
        if getattr(m, "role", None) != "user":
            continue
        t = _msg_text(m).strip()
        if not t:
            continue
        if not fallback:
            fallback = t
        if not t.startswith(_VSCODE_WRAPPERS):
            return t
    return fallback


def _conversation_key(messages: list) -> str:
    """Salted fingerprint of (project, first real user turn) — stable per chat, unique per project+chat."""
    hint = _project_hint(messages)
    first_user = _first_real_user_text(messages)
    if not first_user and not hint:
        return "default"
    digest = hashlib.sha1(
        f"{_SESSION_SALT}:{hint.lower()}:{first_user}".encode("utf-8", "ignore")
    ).hexdigest()
    return digest[:16]


def _persistent_session(
    app: FastAPI,
    raw_request: Request,
    model: str,
    fallback_key: str | None = None,
    messages: list | None = None,
) -> PersistentSession | None:
    header_key = (raw_request.headers.get(_SESSION_ID_HEADER) or "").strip()
    if header_key:
        # Explicit client-supplied session id wins.
        key = f"header:{header_key}"
    elif model.endswith(_PERSIST_MODEL_SUFFIX) and fallback_key:
        # `:persist` WITH an explicit user id -> one stable shared session for that user.
        key = f"model:{fallback_key}"
    elif messages is not None and (
        app.state.settings.persist_default or model.endswith(_PERSIST_MODEL_SUFFIX)
    ):
        # One substrate conversation per client chat, keyed by its first real user turn.
        # (VS Code sends no per-chat id and no `user`, so this content fingerprint is what
        # groups a chat's turns; `:persist` without a user lands here too.)
        key = f"auto:{_conversation_key(messages)}"
    else:
        return None
    session = app.state.session_store.get(key)
    # Edit/regeneration detection (auto-keyed chats only): a faithful continuation resends every
    # assistant turn we produced, so the history should carry >= turn_count assistant turns. If it
    # carries fewer, the client truncated history (edited/regenerated an earlier turn) -> branch
    # onto a FRESH substrate conversation instead of continuing — and polluting — the old one.
    if key.startswith("auto:") and session.turn_count > 0 and messages is not None:
        assistant_turns = sum(
            1 for m in messages if getattr(m, "role", None) == "assistant"
        )
        if assistant_turns < session.turn_count:
            session = app.state.session_store.update(key, rotate=True) or session
    if not session.label and messages:
        session.label = _session_label(messages)
    app.state.session_store.persist(key, session)
    return session


def _session_label(messages: list) -> str:
    hint = _project_hint(messages)
    base = hint.rstrip("/\\").replace("\\", "/").split("/")[-1] if hint else ""
    first = ""
    for m in messages:
        if getattr(m, "role", None) == "user":
            first = _msg_text(m).strip().replace("\n", " ")[:60]
            break
    return (f"{base}: " if base else "") + first


def _chat_info(key: str, s: PersistentSession) -> ChatInfo:
    now = time.time()
    return ChatInfo(
        key=key,
        conversation_id=s.conversation_id,
        client_session_id=s.client_session_id,
        turns=s.turn_count,
        label=s.label,
        created_at=int(s.created_at),
        age_seconds=int(now - s.created_at),
        idle_seconds=int(now - s.last_used),
    )


def _trim_history(ctx: list[str], session: PersistentSession | None) -> list[str]:
    """On continued turns of a persistent session, drop the re-sent prior transcript: substrate
    keeps the thread under the same conversation_id, so resending it just bloats the prompt and
    buries the current message. The first turn (turn_count == 0) still seeds full context.
    System-instruction blocks are kept (they carry the client's standing directives).
    Tool results are ALWAYS kept - the model needs them to answer the user's request."""
    if session is None or session.turn_count == 0:
        return ctx
    return [
        c
        for c in ctx
        if not c.startswith("Prior conversation transcript:")
        or c.startswith("Tool results:")
    ]


async def _debug_raw(raw_request: Request) -> None:
    """Log inbound headers + top-level body keys, to discover any stable per-chat id the client
    sends (which we'd otherwise drop via pydantic extra='ignore')."""
    if not os.environ.get("M365_DEBUG"):
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
    if not os.environ.get("M365_DEBUG"):
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


async def _openai_stream(
    model_alias: str,
    client: SubstrateCopilotClient,
    prompt: str,
    additional_context: list[str],
    session: PersistentSession | None = None,
    tone: str = "Magic",
    images: list[ExtractedImage] | None = None,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())
    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [
            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
        ],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n"
    try:
        async for delta in client.chat_stream(
            prompt, additional_context, session, tone, images
        ):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_alias,
                "choices": [
                    {"index": 0, "delta": {"content": delta}, "finish_reason": None}
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
    except SubstrateCopilotError as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return
    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


async def _responses_stream(
    model_alias: str,
    client: SubstrateCopilotClient,
    prompt: str,
    additional_context: list[str],
    session: PersistentSession | None = None,
    tone: str = "Magic",
) -> AsyncIterator[str]:
    resp_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'in_progress', 'output': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'content': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"

    full_text = ""
    try:
        async for delta in client.chat_stream(
            prompt, additional_context, session, tone
        ):
            full_text += delta
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'delta': delta})}\n\n"
    except SubstrateCopilotError as exc:
        yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'response.output_text.done', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'text': full_text})}\n\n"
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'completed', 'output': [{'id': item_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text}]}], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"


async def _anthropic_stream(
    model_alias: str,
    client: SubstrateCopilotClient,
    prompt: str,
    additional_context: list[str],
    session: PersistentSession | None = None,
    tone: str = "Magic",
    images: list[ExtractedImage] | None = None,
) -> AsyncIterator[str]:
    msg_id = f"msg_{uuid.uuid4().hex}"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model_alias,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    yield sse("ping", {"type": "ping"})

    try:
        async for delta in client.chat_stream(
            prompt, additional_context, session, tone, images
        ):
            yield sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": delta},
                },
            )
    except SubstrateCopilotError as exc:
        yield sse(
            "error",
            {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}},
        )
        return

    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        },
    )
    yield sse("message_stop", {"type": "message_stop"})


async def _openai_emulated_stream(
    model_alias: str,
    calls: list | None,
    text: str,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())

    if calls:
        tool_calls_payload = [
            {
                "index": i,
                "id": c.get("id")
                if isinstance(c, dict)
                else getattr(c, "id", f"call_{i}"),
                "type": "function",
                "function": {
                    "name": c.get("function", {}).get("name")
                    if isinstance(c, dict)
                    else c.function.name,
                    "arguments": c.get("function", {}).get("arguments")
                    if isinstance(c, dict)
                    else c.function.arguments,
                },
            }
            for i, c in enumerate(calls)
        ]
        first = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_alias,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": tool_calls_payload,
                    },
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(first)}\n\n"
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_alias,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }
        yield f"data: {json.dumps(final)}\n\n"
    else:
        first = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_alias,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(first)}\n\n"
        final = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_alias,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final)}\n\n"

    yield "data: [DONE]\n\n"


async def _anthropic_emulated_stream(
    model_alias: str,
    calls: list | None,
    text: str,
) -> AsyncIterator[str]:
    msg_id = f"msg_{uuid.uuid4().hex}"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model_alias,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    if calls:
        for i, c in enumerate(calls):
            name = (
                c.get("function", {}).get("name")
                if isinstance(c, dict)
                else c.function.name
            )
            args = (
                c.get("function", {}).get("arguments")
                if isinstance(c, dict)
                else c.function.arguments
            )
            cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", f"call_{i}")
            try:
                args_obj = json.loads(args or "{}") if isinstance(args, str) else args
            except Exception:
                args_obj = {}

            yield sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {
                        "type": "tool_use",
                        "id": cid,
                        "name": name,
                        "input": {},
                    },
                },
            )
            yield sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(args_obj, ensure_ascii=False)
                        if isinstance(args_obj, dict)
                        else str(args_obj),
                    },
                },
            )
            yield sse("content_block_stop", {"type": "content_block_stop", "index": i})
        yield sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        )
    else:
        yield sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        yield sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )
        yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        )

    yield sse("message_stop", {"type": "message_stop"})
