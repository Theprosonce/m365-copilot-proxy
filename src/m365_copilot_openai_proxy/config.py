from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    access_token: str = Field(default="", alias="M365_ACCESS_TOKEN")
    time_zone: str = Field(default="Asia/Tokyo", alias="M365_TIME_ZONE")
    model_alias: str = Field(default="m365-copilot", alias="M365_MODEL_ALIAS")
    # True -> agent=work (enterprise grounding); False -> agent=web (no work grounding).
    # Coding agents (OpenCode) usually want False so Copilot doesn't pull enterprise files.
    work_grounding: bool = Field(default=True, alias="M365_WORK_GROUNDING")
    # True -> reuse ONE substrate conversation per client chat (rotated when a fresh chat starts),
    # instead of creating a new conversation per request/correction. Cuts the server-side footprint.
    persist_default: bool = Field(default=True, alias="M365_PERSIST_DEFAULT")
    # True -> open every conversation as a temporary/private chat (WS `disableMemory=1`): it is
    # not saved to the user's Copilot history and produces no memories. Captured from the web
    # client's incognito toggle. Default on so the proxy doesn't pollute the user's chat history.
    disable_memory: bool = Field(default=True, alias="M365_DISABLE_MEMORY")
    # SQLite file for the session/conversation store. Empty -> default under the home dir.
    # Tests point this at a tmp file so they never touch the real store.
    session_db_path: str = Field(default="", alias="M365_SESSION_DB")
    # Seconds without any substrate frame before giving up; WS handshake open timeout.
    recv_timeout: int = Field(default=90, alias="M365_RECV_TIMEOUT")
    open_timeout: int = Field(default=30, alias="M365_OPEN_TIMEOUT")
    # True -> keep one substrate WebSocket alive per persistent session (skips the per-turn
    # handshake). Default off until validated against substrate (see Workstream A).
    ws_reuse: bool = Field(default=False, alias="M365_WS_REUSE")
    # Path to the Edge executable used for the debug token-capture window.
    edge_path: str = Field(
        default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        alias="M365_EDGE_PATH",
    )
