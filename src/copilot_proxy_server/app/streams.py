from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

from ..config import Settings
from ..models import ExtractedImage
from ..session_store import PersistentSession
from ..substrate_client import SubstrateCopilotClient, SubstrateCopilotError
from ..middleware.pipeline import ToolMiddlewarePipeline
from .common import _debug_dump
from .responses import _responses_output_from_tool_calls

async def _openai_static_stream(model_alias: str, text: str) -> AsyncIterator[str]:
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())
    for delta, finish_reason in (
        ({"role": "assistant", "content": text}, None),
        ({}, "stop"),
    ):
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_alias,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        yield f"data: {json.dumps(payload)}\n\n"
    yield "data: [DONE]\n\n"


async def _responses_static_stream(model_alias: str, text: str) -> AsyncIterator[str]:
    resp_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    output = [{
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }]
    yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'delta': text})}\n\n"
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'model': model_alias, 'status': 'completed', 'output': output, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"

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
    full_text = ""
    try:
        async for delta in client.chat_stream(
            prompt, additional_context, session, tone, images
        ):
            full_text += delta
    except SubstrateCopilotError as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    pipeline = ToolMiddlewarePipeline()
    calls, text = await pipeline.tool_calls_with_failure_retry(
        full_text, client, session, tone, images
    )
    if calls:
        for tool_index, call in enumerate(calls):
            call_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_alias,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": tool_index,
                                    "id": call.id,
                                    "type": "function",
                                    "function": {
                                        "name": call.function.name,
                                        "arguments": call.function.arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(call_chunk)}\n\n"
        final_reason = "tool_calls"
    else:
        if text:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_alias,
                "choices": [
                    {"index": 0, "delta": {"content": text}, "finish_reason": None}
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
        final_reason = "stop"

    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [{"index": 0, "delta": {}, "finish_reason": final_reason}],
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

    full_text = ""
    try:
        async for delta in client.chat_stream(
            prompt, additional_context, session, tone
        ):
            full_text += delta
    except SubstrateCopilotError as exc:
        yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        return

    pipeline = ToolMiddlewarePipeline()
    calls, text = await pipeline.tool_calls_with_failure_retry(
        full_text, client, session, tone
    )
    if calls:
        output = _responses_output_from_tool_calls(calls)
        for index, item in enumerate(output):
            yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': index, 'item': item})}\n\n"
        yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'completed', 'output': output, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'content': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"
    if text:
        yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'delta': text})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_text.done', 'item_id': item_id, 'output_index': 0, 'content_index': 0, 'text': text})}\n\n"
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'completed', 'output': [{'id': item_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}], 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})}\n\n"


async def _anthropic_static_stream(model_alias: str, text: str) -> AsyncIterator[str]:
    msg_id = f"msg_{uuid.uuid4().hex}"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("message_start", {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": model_alias, "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}})
    yield sse("message_stop", {"type": "message_stop"})


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

    full_text = ""
    chunk_index = 0
    from ..debug_logger import log_event, log_raw_event
    try:
        async for delta in client.chat_stream(
            prompt, additional_context, session, tone, images
        ):
            full_text += delta
            log_event("MODEL_RECV_STREAM_CHUNK", {
                "chunk_index": chunk_index,
                "raw_chunk": delta,
                "parsed_chunk": {"type": "text", "text": delta}
            })
            log_raw_event("Receive Chunk", {
                "chunk": delta
            })
            chunk_index += 1
        print(f"<- RECV: {full_text}", flush=True)

        # 5. Raw response
        log_event("MODEL_RECV_RAW", {
            "status_code": 200,
            "raw": full_text,
            "chunks": [full_text]
        })
        log_raw_event("Receive", {
            "content": full_text
        })

        pipeline = ToolMiddlewarePipeline()
        calls, text = await pipeline.tool_calls_with_failure_retry(
            full_text, client, session, tone, images
        )

        # 6. Extracted assistant text
        log_event("MODEL_RECV_EXTRACTED", {
            "text": text,
            "text_len": len(text),
            "content_block_count": 1 if text.strip() else 0,
            "tool_use_block_count": len(calls) if calls else 0,
            "reasoning_block_count": 0
        })
        log_raw_event("Extracted Text", {
            "text": text
        })

        # 7. Empty response decision
        if not text or not text.strip():
            log_event("EMPTY_RESPONSE_DECISION", {
                                "recovery_applied": False,
                "continuation_added": False,
                "reason": "stream returned empty content"
            })
            log_raw_event("Empty Response", {
                "empty": True,
                "reason": "stream returned empty content"
            })

        if calls:
            if text.strip():
                yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
                yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                start_index = 1
            else:
                start_index = 0
            for offset, call in enumerate(calls):
                index = start_index + offset
                yield sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": call.id, "name": call.function.name, "input": {}}})
                yield sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": call.function.arguments}})
                yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
            yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 0}})
            yield sse("message_stop", {"type": "message_stop"})
            return

        if text:
            yield sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            )

    except SubstrateCopilotError as exc:
        log_event("MODEL_RECV_RAW", {
            "status_code": 502,
            "raw": str(exc),
            "chunks": []
        })
        log_raw_event("Error", {
            "error": str(exc)
        })
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
