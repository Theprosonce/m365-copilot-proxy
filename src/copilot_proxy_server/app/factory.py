from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import time
import uuid
from collections.abc import AsyncIterator, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..anthropic_passthrough import credential_available, forward_messages
from ..config import Settings
from ..middleware.pipeline import ToolMiddlewarePipeline
from ..models import (
    AnthropicMessagesRequest, ChatCreateRequest, ChatInfo, ChatListResponse,
    ChatUpdateRequest, DeleteResponse, OpenAIChatRequest, OpenAIMessage,
    OpenAIResponsesRequest,
)
from ..session_store import PersistentSessionStore
from ..substrate_client import MODEL_TO_TONE, SubstrateCopilotClient, SubstrateCopilotError, resolve_tone
from ..token_store import AccessTokenStore
from ..translator import flatten_content, translate_anthropic_request, translate_openai_request, translate_responses_request
from .common import _PERSIST_MODEL_SUFFIX, _debug_dump, _effective_disable_memory, _is_proxy_model, _is_title_request
from .requests import _debug_images, _debug_raw, _request_images
from .responses import _responses_output_from_tool_calls
from .sessions import (
    _chat_info, _conversation_key, _detect_workspace_mode, _persistent_session,
    _project_hint, _session_label, _trim_history,
)
from .streams import (
    _anthropic_static_stream, _anthropic_stream, _openai_static_stream,
    _openai_stream, _responses_static_stream, _responses_stream,
)

def create_app(
    settings: Settings | None = None,
    copilot_client_factory: Callable[[], SubstrateCopilotClient] | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if session := (resolved_settings.session_id or "").strip():
            print(f"X-M365-Session-Id: Session attached: {session}", flush=True)
        yield

    app = FastAPI(
        title="Microsoft 365 Copilot OpenAI Proxy",
        description=(
            "OpenAI/Anthropic-compatible proxy over Microsoft 365 Copilot (substrate). "
            "Exposes the model picker (`tone`), Work/Web grounding, native request tool fields, "
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
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.token_store = AccessTokenStore(resolved_settings.access_token)
    app.state.session_store = PersistentSessionStore(
        db_path=resolved_settings.session_db_path
        or str(Path.cwd() / ".sessions" / "sessions.db"),
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
            _effective_disable_memory(resolved_settings),
            resolved_settings.substrate_concurrency_limit,
            resolved_settings.truncation_before_sending,
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
            pipeline = ToolMiddlewarePipeline(settings)

            request, _tools_prompt, _normalized_tools = pipeline.preflight_openai(request)

            translated = translate_openai_request(request, settings)
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

            if _is_title_request(prompt, settings):
                if request.stream:
                    return StreamingResponse(
                        _openai_static_stream(settings.model_alias, "No Title"),
                        media_type="text/event-stream",
                    )
                return JSONResponse(
                    {
                        "id": f"chatcmpl_{uuid.uuid4().hex}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": settings.model_alias,
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": "No Title"},
                            "finish_reason": "stop",
                        }],
                    }
                )

            if request.stream:
                return StreamingResponse(
                    _openai_stream(
                        settings.model_alias, client, prompt, ctx, session, tone, images
                    ),
                    media_type="text/event-stream",
                )

            text = await client.chat(prompt, ctx, session, tone, images)
            calls, text = await pipeline.tool_calls_with_failure_retry(
                text, client, session, tone, images
            )
        except ValueError as exc:
            print(f"[400] bad request: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SubstrateCopilotError as exc:
            print(f"[502] substrate error: {exc}")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

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
            pipeline = ToolMiddlewarePipeline(settings)
            translated = translate_responses_request(request, settings)
            proxy_request = OpenAIChatRequest(
                model=request.model,
                messages=[OpenAIMessage(role="user", content=translated.prompt)],
                stream=request.stream,
                temperature=request.temperature,
                user=request.user,
                tools=request.tools,
                tool_choice=request.tool_choice,
                functions=request.functions,
                function_call=request.function_call,
            )
            proxy_request, _tools_prompt, _normalized_tools = pipeline.preflight_openai(
                proxy_request
            )
            if proxy_request.messages and isinstance(proxy_request.messages[0].content, str):
                translated.prompt = proxy_request.messages[0].content
            request.stream = proxy_request.stream
            session = _persistent_session(app, raw, request.model, request.user)
            tone = resolve_tone(request.model)
            ctx = list(translated.additional_context)
            prompt = translated.prompt
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if _is_title_request(prompt, settings):
            if request.stream:
                return StreamingResponse(
                    _responses_static_stream(settings.model_alias, "No Title"),
                    media_type="text/event-stream",
                )
            return JSONResponse(
                {
                    "id": f"resp_{uuid.uuid4().hex}",
                    "object": "response",
                    "created_at": int(time.time()),
                    "model": settings.model_alias,
                    "output": [{
                        "id": f"msg_{uuid.uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "No Title"}],
                    }],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                }
            )

        if request.stream:
            return StreamingResponse(
                _responses_stream(
                    settings.model_alias,
                    client,
                    prompt,
                    ctx,
                    session,
                    tone,
                ),
                media_type="text/event-stream",
            )

        try:
            text = await client.chat(prompt, ctx, session, tone)
            calls, text = await pipeline.tool_calls_with_failure_retry(
                text, client, session, tone
            )
        except SubstrateCopilotError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        output = (
            _responses_output_from_tool_calls(calls)
            if calls
            else [
                {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex}",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        )
        return JSONResponse(
            {
                "id": f"resp_{uuid.uuid4().hex}",
                "object": "response",
                "created_at": int(time.time()),
                "model": settings.model_alias,
                "output": output,
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

        # ----------------- DIAGNOSTIC LOGGING -----------------
        from ..debug_logger import request_context, log_event, log_raw_event
        import uuid
        import json
        req_id = raw_request.headers.get("x-request-id") or uuid.uuid4().hex
        ctx_data = {
            "request_id": req_id,
            "session_id": None,
            "route": "substrate" if ours else "passthrough",
            "api": "/v1/messages",
            "model": request.model,
            "stream": getattr(request, "stream", False)
        }
        request_context.set(ctx_data)

        # 1. Incoming proxy request
        try:
            req_dict = request.model_dump() if hasattr(request, "model_dump") else request.dict()
        except Exception:
            req_dict = str(request)

        log_event("RAW_REQUEST_RECEIVED", {
            "path": "/v1/messages",
            "method": "POST",
            "model": request.model,
            "stream": getattr(request, "stream", False),
            "messages_or_input": req_dict
        })

        # 2. Message before transformation
        messages_before = []
        try:
            messages_before = request.model_dump()["messages"] if hasattr(request, "model_dump") else request.dict()["messages"]
        except Exception:
            pass

        text_before = ""
        if request.messages:
            for msg in reversed(request.messages):
                if getattr(msg, "role", None) == "user":
                    if isinstance(msg.content, str):
                        text_before = msg.content
                    elif isinstance(msg.content, list):
                        text_before = " ".join(getattr(p, "text", "") or "" for p in msg.content if getattr(p, "type", None) == "text")
                    break

        log_event("MODEL_SENT_BEFORE_MODIFICATION", {
            "text": text_before,
            "messages": messages_before
        })
        log_raw_event("Sent Before Modification", {
            "messages": messages_before
        })
        # ------------------------------------------------------

        # Log the tool selected by the model later, not every tool merely offered by the client.
        if settings.anthropic_passthrough and not ours:
            from ..anthropic_passthrough import credential_available, forward_messages

            if credential_available(settings):
                return await forward_messages(
                    settings, await raw_request.body(), raw_request.headers
                )
            print(
                "  ! passthrough requested but no Anthropic credential available -> using substrate"
            )
        client = get_copilot_client()
        try:
            pipeline = ToolMiddlewarePipeline(settings)

            _dummy_req, _tools_prompt, _normalized_tools = pipeline.preflight_anthropic(request)

            translated = translate_anthropic_request(request, settings)
            session = _persistent_session(
                app, raw_request, request.model, None, request.messages
            )

            # Update session id in request context
            if session:
                ctx_data["session_id"] = session.conversation_id
                request_context.set(ctx_data)

            # 14. Workspace and Title decisions
            workspace_detected, workspace_cleaned = _detect_workspace_mode(request.messages) if request.messages else (False, "")
            if workspace_detected:
                log_event("WORKSPACE_MODE_DECISION", {
                    "detected": True,
                    "source": "latest_user_message",
                    "cleaned_text": workspace_cleaned
                })
                log_raw_event("Workspace", {
                    "detected": True,
                    "source": "latest_user_message",
                    "cleaned_text": workspace_cleaned
                })

            title_gen = False
            if request.messages:
                last_msg_text = ""
                for msg in reversed(request.messages):
                    if getattr(msg, "role", None) == "user":
                        if isinstance(msg.content, str):
                            last_msg_text = msg.content
                        elif isinstance(msg.content, list):
                            last_msg_text = " ".join(getattr(p, "text", "") or "" for p in msg.content if getattr(p, "type", None) == "text")
                        break
                if "title" in last_msg_text.lower():
                    title_gen = True

            log_event("MESSAGE_TRANSFORM_DECISION", {
                "workspace_mode": workspace_detected,
                "title_generation_detected": title_gen,
                "changed": False,
                "reason": "minimum_native_translation_only"
            })
            log_raw_event("Modification", {
                "changed": False,
                "reason": "minimum_native_translation_only"
            })

            tone = resolve_tone(request.model)
            images = _request_images(request.messages)
            _debug_images(request.messages, images)
            ctx = _trim_history(list(translated.additional_context), session)
            prompt = translated.prompt
            if _is_title_request(prompt, settings):
                if request.stream:
                    return StreamingResponse(
                        _anthropic_static_stream(settings.model_alias, "No Title"),
                        media_type="text/event-stream",
                    )
                return JSONResponse(
                    {
                        "id": f"msg_{uuid.uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "model": settings.model_alias,
                        "content": [{"type": "text", "text": "No Title"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    }
                )
            if prompt.strip().lower() in {"hi", "test"}:
                if request.stream:
                    return StreamingResponse(
                        _anthropic_static_stream(settings.model_alias, "worked"),
                        media_type="text/event-stream",
                    )
                return JSONResponse(
                    {
                        "id": f"msg_{uuid.uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "model": settings.model_alias,
                        "content": [{"type": "text", "text": "worked"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    }
                )
            _debug_dump(
                "ANTHROPIC REQUEST",
                f"model={request.model} n_tools={len(request.tools) if request.tools else 0} tool_choice={request.tool_choice}\nuser_prompt_tail={translated.prompt[-500:]!r}",
            )
            log_event("MESSAGE_NOT_MODIFIED", {
                "reason": "minimum_native_translation_only"
            })

            log_raw_event("Sent After Modification", {
                "text": prompt,
                "additional_context": ctx
            })

            from ..substrate_client import _combine_text
            log_raw_event("Sent", {
                "model": settings.model_alias,
                "messages": [{"role": "user", "content": _combine_text(prompt, ctx)}]
            })

            # 4. Final upstream request
            try:
                raw_payload_data = request.model_dump() if hasattr(request, "model_dump") else request.dict()
            except Exception:
                raw_payload_data = {}

            log_event("MODEL_SENT_FINAL", {
                "api": "/v1/messages",
                "model": settings.model_alias,
                "stream": request.stream,
                "text": prompt,
                "raw_payload": raw_payload_data
            })

            if request.stream:
                return StreamingResponse(
                    _anthropic_stream(
                        settings.model_alias, client, prompt, ctx, session, tone, images
                    ),
                    media_type="text/event-stream",
                )

            text = await client.chat(prompt, ctx, session, tone, images)
            calls, text = await pipeline.tool_calls_with_failure_retry(
                text, client, session, tone, images
            )
            log_event("MODEL_RECV_RAW", {
                "status_code": 200,
                "raw": text,
                "chunks": [text]
            })

            # 6. Extracted assistant text
            content_block_count = 1 if (text and text.strip()) else 0
            tool_use_block_count = len(calls) if calls else 0
            log_event("MODEL_RECV_EXTRACTED", {
                "text": text or "",
                "text_len": len(text) if text else 0,
                "content_block_count": content_block_count,
                "tool_use_block_count": tool_use_block_count,
                "reasoning_block_count": 0
            })
            log_raw_event("Receive", {
                "content": text or ""
            })
            log_raw_event("Extracted Text", {
                "text": text or ""
            })

            # 7. Empty response decision
            if not text or not text.strip():
                log_event("EMPTY_RESPONSE_DECISION", {
                    "recovery_applied": False,
                    "continuation_added": False,
                    "reason": "minimum native translation only; pass through empty response"
                })
                log_raw_event("Empty Response", {
                    "empty": True,
                    "reason": "minimum native translation only; pass through empty response"
                })

            print(f"<- RECV: {text}", flush=True)
        except ValueError as exc:
            print(f"[400] bad request: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SubstrateCopilotError as exc:
            print(f"[502] substrate error: {exc}")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if calls:
            content = pipeline.anthropic_content_from_tool_calls(calls, text)
            resp_payload = {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": settings.model_alias,
                "content": content,
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
            log_event("TOOL_RESULT_RETURNED", {
                "format": "anthropic",
                "payload_len": len(json.dumps(resp_payload)),
                "payload": resp_payload
            })
            return JSONResponse(resp_payload)

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
