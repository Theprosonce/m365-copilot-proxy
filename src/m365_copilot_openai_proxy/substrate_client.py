from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets

from .models import ExtractedImage, MessageAnnotation, UploadFileResponse
from .session_store import PersistentSession
from .config import Settings
from .substrate_config import load_substrate_config
from .token_store import decode_jwt_payload, is_substrate_token_claims

SIGNALR_SEP = "\x1e"

# Capture-derived protocol payload (see substrate.json / substrate_config_path). The frame
# *skeleton* stays in code below; only these data values come from config.
_CFG = load_substrate_config()
_WS_BASE = _CFG["ws_base"]
_UPLOAD_URL = _CFG["upload_url"]
_ORIGIN = _CFG["origin"]
# Default to Opus: "Magic" (the included-tier Auto tone) hangs under Premium/officeweb.
DEFAULT_TONE: str = _CFG["default_tone"]
# Model picker -> substrate `tone` value (captured from the real web client).
MODEL_TO_TONE: dict[str, str] = _CFG["model_to_tone"]
_VARIANTS = ",".join(_CFG["variants"])
_OPTIONS_SETS = _CFG["options_sets"]
# Sent as repeated multipart fields on UploadFile (captured from the web client).
_UPLOAD_OPTIONS_SETS = _CFG["upload_options_sets"]
_UPLOAD_VARIANTS = _CFG["upload_variants"]
# Extra optionsSets the WS frame carries when the turn includes an uploaded image.
_IMAGE_FRAME_OPTIONS_SETS = _CFG["image_frame_options_sets"]
_ALLOWED_MESSAGE_TYPES = _CFG["allowed_message_types"]
_FRAME = _CFG["frame"]

# Default seconds without any frame from substrate before we give up (instead of hanging).
# Overridable per-client via Settings/config.ini (recv_timeout / open_timeout).
_RECV_TIMEOUT = 90
_OPEN_TIMEOUT = 30


def resolve_tone(model: str | None) -> str:
    if not model:
        return DEFAULT_TONE
    base = model.split(":", 1)[0].strip().lower()
    return MODEL_TO_TONE.get(base, DEFAULT_TONE)


def _emit_timing(**fields: object) -> None:
    """One parseable `TIMING ...` line per turn to debug.log when timing=true in config.ini."""
    if not Settings().timing:
        return
    try:
        line = "TIMING " + " ".join(f"{k}={v}" for k, v in fields.items())
        with Path("debug.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _ms(start: float, end: float) -> int:
    return round((end - start) * 1000)


class SubstrateCopilotError(RuntimeError):
    pass


class SubstrateCopilotClient:
    def __init__(
        self,
        access_token: str,
        time_zone: str = "Asia/Tokyo",
        work_grounding: bool = True,
        recv_timeout: int = _RECV_TIMEOUT,
        open_timeout: int = _OPEN_TIMEOUT,
        disable_memory: bool = True,
    ):
        if not access_token:
            raise SubstrateCopilotError(
                "access_token is missing in config.ini. Start the debug Edge window and let startup token capture complete, "
                "or run `uv run copilot-openai-proxy set-token`."
            )
        self._token = access_token
        self._time_zone = time_zone
        self._work_grounding = work_grounding
        self._recv_timeout = recv_timeout
        self._open_timeout = open_timeout
        self._disable_memory = disable_memory
        try:
            claims = decode_jwt_payload(access_token)
        except Exception as exc:
            raise SubstrateCopilotError(f"Cannot decode access token: {exc}") from exc
        if not is_substrate_token_claims(claims):
            raise SubstrateCopilotError(
                "Access token is not a substrate.office.com token."
            )
        if time.time() > claims.get("exp", 0):
            raise SubstrateCopilotError(
                "Access token expired. To refresh: open M365 Copilot in your browser, "
                "DevTools → Network → filter 'substrate' → click the WebSocket → Headers → "
                "copy the access_token= query param → update access_token in config.ini"
            )
        self._oid: str = claims["oid"]
        self._tid: str = claims["tid"]

    def _ws_url(self, conv_id: str, session_id: str, req_id: str) -> str:
        token = quote(self._token, safe="")
        session_key = uuid.uuid4().hex
        return (
            f"{_WS_BASE}/{self._oid}@{self._tid}"
            f"?chatsessionid={session_key}"
            f"&XRoutingParameterSessionKey={session_key}"
            f"&clientrequestid={session_key}"
            f"&X-SessionId={session_id}"
            f"&ConversationId={conv_id}"
            f"&access_token={token}"
            f"&variants={_VARIANTS}"
            f"&source={_FRAME['source']}&product={_FRAME['product']}&agentHost={_FRAME['agent_host']}"
            f"&licenseType={_FRAME['license_type']}&isEdu={_FRAME['is_edu']}"
            f"&agent={'work' if self._work_grounding else 'web'}&scenario={_FRAME['scenario']}"
            # Temporary/private chat: not saved to history, no memories (captured: incognito toggle).
            + ("&disableMemory=1" if self._disable_memory else "")
        )

    def _chat_invoke(
        self,
        text: str,
        conv_id: str,
        session_id: str,
        req_id: str,
        is_start_of_session: bool,
        tone: str = DEFAULT_TONE,
        annotations: list[MessageAnnotation] | None = None,
    ) -> str:
        ci = _FRAME["client_info"]
        message = {
            "author": "user",
            "inputMethod": "Keyboard",
            "text": text,
            "entityAnnotationTypes": _FRAME["entity_annotation_types"],
            "requestId": req_id,
            "locationInfo": {
                "timeZoneOffset": _FRAME["location_time_zone_offset"],
                "timeZone": self._time_zone,
            },
            "locale": _FRAME["locale"],
            "messageType": "Chat",
            "experienceType": "Default",
            "adaptiveCards": [],
            "clientPreferences": {},
        }
        options_sets = _OPTIONS_SETS
        if annotations:
            message["messageAnnotations"] = annotations
            options_sets = _OPTIONS_SETS + [
                o for o in _IMAGE_FRAME_OPTIONS_SETS if o not in _OPTIONS_SETS
            ]
        payload = {
            "arguments": [
                {
                    "source": _FRAME["source"],
                    "clientCorrelationId": req_id,
                    "sessionId": session_id,
                    "optionsSets": options_sets,
                    "streamingMode": _FRAME["streaming_mode"],
                    "spokenTextMode": _FRAME["spoken_text_mode"],
                    "options": {},
                    "extraExtensionParameters": {},
                    "allowedMessageTypes": _ALLOWED_MESSAGE_TYPES,
                    "sliceIds": [],
                    "threadLevelGptId": {},
                    "traceId": req_id,
                    "isStartOfSession": is_start_of_session,
                    "clientInfo": {
                        "clientPlatform": ci["clientPlatform"],
                        "clientAppName": ci["clientAppName"],
                        "clientEntrypoint": ci["clientEntrypoint"],
                        "clientSessionId": session_id,
                        "clientAppType": ci["clientAppType"],
                        "deviceOS": ci["deviceOS"],
                        "deviceType": ci["deviceType"],
                    },
                    "message": message,
                    "plugins": _FRAME["plugins"],
                    "isSbsSupported": True,
                    "tone": tone,
                    "renderReferencesBehindEOS": True,
                }
            ],
            "invocationId": "0",
            "target": "chat",
            "type": 4,
        }
        return json.dumps(payload, ensure_ascii=False) + SIGNALR_SEP

    async def chat_stream(
        self,
        prompt: str,
        additional_context: list[str],
        session: PersistentSession | None = None,
        tone: str = DEFAULT_TONE,
        images: list[ExtractedImage] | None = None,
    ) -> AsyncIterator[str]:
        text = _combine_text(prompt, additional_context)
        if session is None:
            async for chunk in self._chat_stream_for_turn(
                text=text,
                conv_id=str(uuid.uuid4()),
                session_id=str(uuid.uuid4()),
                is_start_of_session=True,
                tone=tone,
                images=images,
            ):
                yield chunk
            return

        async with session.lock:
            turn = session.reserve_turn()
            async for chunk in self._chat_stream_for_turn(
                text=text,
                conv_id=turn.conversation_id,
                session_id=turn.client_session_id,
                is_start_of_session=turn.is_start_of_session,
                tone=tone,
                images=images,
            ):
                yield chunk

    async def _upload_image(
        self, conv_id: str, image: ExtractedImage
    ) -> MessageAnnotation | None:
        """Upload one image to substrate, returning its `messageAnnotation` (carrying the
        docId), or None if the upload failed. The image must be uploaded under the same
        conversationId the prompt frame will use, before that frame is sent."""
        form = [
            ("scenario", (None, "UploadImage")),
            ("conversationId", (None, conv_id)),
            ("FileBase64", (None, image.data_uri)),
        ] + [("optionsSets", (None, o)) for o in _UPLOAD_OPTIONS_SETS]
        headers = {
            "Authorization": f"Bearer {self._token}",
            "x-anchormailbox": f"Oid:{self._oid}@{self._tid}",
            "x-scenario": _FRAME["scenario"],
            "x-variants": _UPLOAD_VARIANTS,
            "Origin": _ORIGIN,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(_UPLOAD_URL, files=form, headers=headers)
        resp.raise_for_status()
        body: UploadFileResponse = resp.json()
        doc_id = body.get("docId")
        if not doc_id:
            return None
        return {
            "id": doc_id,
            "messageAnnotationMetadata": {
                "@type": "File",
                "annotationType": "File",
                "fileType": image.file_type,
                "fileName": image.file_name,
            },
            "messageAnnotationType": "ImageFile",
        }

    async def _chat_stream_for_turn(
        self,
        text: str,
        conv_id: str,
        session_id: str,
        is_start_of_session: bool,
        tone: str = DEFAULT_TONE,
        images: list[ExtractedImage] | None = None,
    ) -> AsyncIterator[str]:
        req_id = str(uuid.uuid4())
        annotations: list[MessageAnnotation] = []
        for image in images or []:
            ann = await self._upload_image(conv_id, image)
            if ann:
                annotations.append(ann)
        url = self._ws_url(conv_id, session_id, req_id)
        t0 = time.perf_counter()
        try:
            async with (
                websockets.connect(
                    url,
                    additional_headers={
                        "Origin": _ORIGIN,
                    },
                    open_timeout=self._open_timeout,  # substrate handshake can exceed the 10s default under load
                    max_size=None,  # substrate replies (file contents, etc.) can be large
                ) as ws
            ):
                t_conn = time.perf_counter()
                await ws.send(
                    json.dumps({"protocol": "json", "version": 1}) + SIGNALR_SEP
                )
                await asyncio.wait_for(ws.recv(), timeout=self._recv_timeout)
                t_nego = time.perf_counter()
                await ws.send(
                    self._chat_invoke(
                        text,
                        conv_id,
                        session_id,
                        req_id,
                        is_start_of_session,
                        tone,
                        annotations=annotations or None,
                    )
                )
                t_sent = time.perf_counter()
                fallback_text = ""
                yielded_any = False
                first_delta: float | None = None
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=self._recv_timeout
                        )
                    except asyncio.TimeoutError as exc:
                        raise SubstrateCopilotError(
                            f"No response from substrate within {self._recv_timeout}s "
                            f"(tone={tone!r} may be invalid for this license/scenario)."
                        ) from exc
                    for part in raw.split(SIGNALR_SEP):
                        part = part.strip()
                        if not part:
                            continue
                        try:
                            msg = json.loads(part)
                        except json.JSONDecodeError:
                            continue
                        t = msg.get("type")
                        if t == 6:
                            continue
                        if t == 1 and msg.get("target") == "update":
                            args = (msg.get("arguments") or [{}])[0]
                            delta = args.get("writeAtCursor")
                            if delta:
                                if first_delta is None:
                                    first_delta = time.perf_counter()
                                if not yielded_any and fallback_text:
                                    yield fallback_text
                                yielded_any = True
                                yield delta
                            msgs = args.get("messages")
                            if msgs:
                                entries = msgs if isinstance(msgs, list) else [msgs]
                                for entry in reversed(entries):
                                    if entry.get("author") != "user":
                                        fallback_text = entry.get("text", "")
                                        break
                        if t == 2:
                            item_msgs = (msg.get("item") or {}).get("messages") or []
                            for entry in reversed(item_msgs):
                                if entry.get("author") != "user":
                                    fallback_text = entry.get("text", "")
                                    break
                        if t == 3:
                            if not yielded_any and fallback_text:
                                if first_delta is None:
                                    first_delta = time.perf_counter()
                                yield fallback_text
                            _emit_timing(
                                connect_ms=_ms(t0, t_conn),
                                negotiate_ms=_ms(t_conn, t_nego),
                                ttft_ms=_ms(t_sent, first_delta) if first_delta else -1,
                                total_ms=_ms(t_sent, time.perf_counter()),
                                reused=0,
                            )
                            return
        except SubstrateCopilotError:
            raise
        except Exception as exc:
            raise SubstrateCopilotError(str(exc)) from exc

    async def chat(
        self,
        prompt: str,
        additional_context: list[str],
        session: PersistentSession | None = None,
        tone: str = DEFAULT_TONE,
        images: list[ExtractedImage] | None = None,
    ) -> str:
        chunks: list[str] = []
        async for chunk in self.chat_stream(
            prompt, additional_context, session, tone, images
        ):
            chunks.append(chunk)
        return "".join(chunks)


def _combine_text(prompt: str, context: list[str]) -> str:
    # Lead with the actual request; trailing context is reference only. (Putting a large
    # transcript/workspace dump first buries the real message and the model loses focus.)
    if not context:
        return prompt
    return (
        prompt
        + "\n\n---\n# Reference context (prior conversation / workspace, for grounding only)\n\n"
        + "\n\n".join(context)
    )
