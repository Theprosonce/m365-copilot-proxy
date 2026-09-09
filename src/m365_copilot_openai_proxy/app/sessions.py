from __future__ import annotations

import hashlib
import re
import time
import uuid

from ..config import Settings
from ..models import ChatInfo, OpenAIMessage
from ..session_store import PersistentSession, PersistentSessionStore
from .common import _PERSIST_MODEL_SUFFIX, _SESSION_ID_HEADER, _effective_disable_memory, _session_id_config_value

# Per-process salt so conversation keys are not bare content hashes.
_SESSION_SALT = Settings().session_salt or uuid.uuid4().hex

_CWD_LABEL_RE = re.compile(
    r"(?:working directory|current working directory|cwd|project root|workspace(?: ?root| ?folder)?)"
    r"\s*[:=]?\s*[\"'`<]?\s*([A-Za-z]:[\\/][^\s\"'`<>\n]+|/[^\s\"'`<>\n]+)",
    re.IGNORECASE,
)
_ANY_PATH_RE = re.compile(r"([A-Za-z]:[\\/][^\s\"'`<>\n]{2,}|/[A-Za-z0-9._\-/]{3,})")


def _identity_text(text: str) -> str:
    return text

def _msg_text(m) -> str:
    content = getattr(m, "content", "")
    if isinstance(content, list):
        text = " ".join(getattr(p, "text", "") or "" for p in content)
    else:
        text = str(content or "")

    return _identity_text(text)


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


def _detect_workspace_mode(messages: list) -> tuple[bool, str]:
    if not messages:
        return False, ""

    latest_user_msg = None
    for m in reversed(messages):
        if getattr(m, "role", None) == "user":
            latest_user_msg = m
            break

    if not latest_user_msg:
        return False, ""

    text = _msg_text(latest_user_msg).strip()

    if text == "workspace=on":
        return True, "workspace=on"

    match = re.match(r"^\s*<session>\s*workspace=on\s*</session>\s*$", text, re.IGNORECASE)
    if match:
        return True, "workspace=on"

    return False, ""


# VS Code adds these identical wrapper turns at the head of EVERY chat in a workspace; keying
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
    env_key = _session_id_config_value()
    if header_key:
        # Explicit client-supplied session id wins.
        key = f"header:{header_key}"
    elif env_key:
        # Process-level session id for clients that cannot set custom headers.
        key = f"env:{env_key}"
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
    configured_conversation_id = (app.state.settings.conversation_id or "").strip()
    if configured_conversation_id:
        session.conversation_id = configured_conversation_id
        session.process_initialized = True
    rotated = False
    reason = None
    if not session.process_initialized and session.turn_count > 0:
        # The proxy was restarted, or the connection was newly initialized in this process.
        # Rotate to a fresh substrate conversation and reset turn_count to 0 so we clean-start
        # a fresh substrate thread with the full, un-trimmed context/history seeded.
        session.conversation_id = str(uuid.uuid4())
        session.client_session_id = str(uuid.uuid4())
        session.turn_count = 0
        session.process_initialized = True
        app.state.session_store.persist(key, session)
        rotated = True
        reason = "process_restart"

    # Edit/regeneration detection (auto-keyed chats only): a faithful continuation resends every
    # assistant turn we produced, so the history should carry >= turn_count assistant turns. If it
    # carries fewer, the client truncated history (edited/regenerated an earlier turn) -> branch
    # onto a FRESH substrate conversation instead of continuing — and polluting — the old one.
    if not configured_conversation_id and key.startswith("auto:") and session.turn_count > 0 and messages is not None:
        assistant_turns = sum(
            1 for m in messages if getattr(m, "role", None) == "assistant"
        )
        if assistant_turns < session.turn_count:
            old_id = session.conversation_id
            session = app.state.session_store.update(key, rotate=True) or session
            if session.conversation_id != old_id:
                rotated = True
                reason = "history_truncated_or_regenerated"
    if not session.label and messages:
        session.label = _session_label(messages)
    app.state.session_store.persist(key, session)

    from ..debug_logger import log_event, log_raw_event
    log_event("SESSION_DECISION", {
        "session_id": session.conversation_id if session else None,
        "rotated": rotated,
        "reason": reason or ("persistent_session_active" if session else "session_disabled")
    })
    log_raw_event("Session", {
        "conversation_id": session.conversation_id if session else None
    })
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
    """Send bootstrap instructions once, then only unseen tool results.

    A persistent substrate conversation retains its first-turn EXT_TOOL contract and callable
    tool definitions. Continued turns therefore receive only the current tool-result block; a
    normal user turn receives no appended context. Non-persistent requests keep the bootstrap
    because each request starts a new substrate conversation.
    """
    if session is None or session.turn_count == 0:
        return ctx
    return [item for item in ctx if item.startswith("Tool results:\n")]
