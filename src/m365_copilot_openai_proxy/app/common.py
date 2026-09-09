from __future__ import annotations

import json
from pathlib import Path

from ..config import Settings
from ..substrate_client import MODEL_TO_TONE

def _debug_dump(label: str, content: str) -> None:
    if not Settings().debug:
        return
    try:
        with (Path.cwd() / ".sessions" / "debug.log").open("a", encoding="utf-8") as f:
            f.write(f"\n===== {label} =====\n{content}\n")
    except Exception:
        pass


_PERSIST_MODEL_SUFFIX = ":persist"
_SESSION_ID_HEADER = "x-m365-session-id"
def _session_id_config_value() -> str:
    return (Settings().session_id or "").strip()


def _effective_disable_memory(settings: Settings) -> bool:
    # Fixed session/conversation IDs require a non-temporary Copilot conversation.
    if _session_id_config_value() or settings.conversation_id.strip():
        return False
    return settings.disable_memory


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


def _is_title_request(text: str, settings: Settings) -> bool:
    """Recognize the client's synthetic title-generation turn when history replay is disabled."""
    if not settings.disable_history_replay:
        return False
    normalized = " ".join((text or "").lower().split())
    return (
        "write the title in the predominant language" in normalized
        or "generate a title for this conversation" in normalized
    )


