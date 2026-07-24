from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


INI_CONFIG_PATHS = (
    Path('config.ini'),
)
ENV_PATH = Path('.env')
ACCESS_TOKEN_ENV_KEY = 'M365_ACCESS_TOKEN'


def _ensure_config_ini_exists() -> None:
    target_path = INI_CONFIG_PATHS[0]
    if target_path.exists():
        return

    # Try to find the installed package template first, then the canonical dev template.
    template_path = Path(__file__).parent / 'config.ini.template'
    if not template_path.exists():
        template_path = Path(__file__).parents[2] / 'config' / 'config.ini.template'

    if template_path.exists():
        try:
            target_path.write_text(template_path.read_text(encoding='utf-8'), encoding='utf-8')
            return
        except Exception:
            pass

    # Fallback to embedded template content if template file cannot be read
    fallback_content = """[settings]
# Time zone used by the proxy (default: Asia/Tokyo)
time_zone = Asia/Tokyo

# OpenAI model alias to map to Copilot (default: m365-copilot)
model_alias = m365-copilot

# True -> enterprise grounding; False -> web grounding
work_grounding = true

# Keep one substrate conversation per client chat
persist_default = true

# Use temporary/private chats (no memory/history saved to Copilot)
disable_memory = true

# SQLite database path for session/conversation store (empty -> default home dir)
session_db_path =

# Cap on stored conversations (0 -> no cap)
session_max = 1000

# Evict conversations unused for this many seconds (0 -> no TTL)
session_ttl_seconds = 0

# Handshake and socket timeouts (in seconds)
recv_timeout = 90
open_timeout = 30

# Concurrency limit for substrate requests (0 or negative -> no limit)
substrate_concurrency_limit = 2

# Truncate prompt/context before sending to substrate
truncation_before_sending = true

# Debug and timing options
debug = false
timing = false

# Path overrides
substrate_config_path =
# Browser settings
edge_headless = false
edge_path = C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe
prefer_active_browser = true

# OAuth / Auth state (automatically populated/refreshed)
refresh_token =
tenant_id =
client_id =
session_id =
session_salt =

# Keep WebSocket alive per persistent session
ws_reuse = false

# Automatically close capture window on success
hide_on_token_success = true

# Anthropic Passthrough settings
anthropic_passthrough = false
anthropic_upstream = https://api.anthropic.com
anthropic_version = 2023-06-01
anthropic_creds_file =
anthropic_key =

[serve]
host = 127.0.0.1
port = 8000
cdp_port = 9222
auto_refresh = true
launch_edge = true
capture_on_start = true
capture_timeout_seconds = 180
refresh_before_seconds = 900
refresh_retry_seconds = 60
configure_clients = true

[capture_token]
cdp_port = 9222
timeout_seconds = 60

[launch_edge]
cdp_port = 9222

[configure]
undo = false


[debug]
tooling_json_log_enabled = true
tooling_json_log_file = logs/debug_tooling.jsonl
tooling_json_log_max_chars = 50000
tooling_raw_log_enabled = true
tooling_raw_log_dir = logs
tooling_raw_log_name_pattern = activity-{timestamp}.log
tooling_raw_log_max_chars = 100000
"""
    try:
        target_path.write_text(fallback_content, encoding='utf-8')
    except Exception:
        pass


def _coerce_ini_value(value: str) -> str | bool | int | float:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    lowered = text.lower()
    if lowered in {'true', 'yes', 'on', '1'}:
        return True
    if lowered in {'false', 'no', 'off', '0'}:
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _load_ini_settings() -> dict[str, Any]:
    _ensure_config_ini_exists()
    parser = configparser.ConfigParser()
    read_paths = parser.read([str(path) for path in INI_CONFIG_PATHS], encoding='utf-8')
    if not read_paths:
        return {}

    values: dict[str, Any] = {}
    for section in parser.sections():
        section_key = section.strip().replace('-', '_').lower()
        for key, value in parser.items(section):
            normalized_key = key.replace('-', '_')
            setting_key = (
                f'{section_key}_{normalized_key}'
                if section_key and section_key != 'settings'
                else normalized_key
            )
            if setting_key == 'access_token':
                continue
            values[setting_key] = _coerce_ini_value(value)
    return values


def ini_settings_source() -> dict[str, Any]:
    import sys
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        config_path = os.path.abspath("config.ini")
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if os.path.dirname(config_path) == project_root:
            return {}
    return _load_ini_settings()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[Any, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            ini_settings_source,
        )

    access_token: str = Field(default="", alias=ACCESS_TOKEN_ENV_KEY)
    time_zone: str = Field(default="Asia/Tokyo")
    model_alias: str = Field(default="m365-copilot")
    # True -> agent=work (enterprise grounding); False -> agent=web (no work grounding).
    # Coding agents (OpenCode) usually want False so Copilot doesn't pull enterprise files.
    work_grounding: bool = Field(default=True)
    # True -> reuse ONE substrate conversation per client chat (rotated when a fresh chat starts),
    # instead of creating a new conversation per request/correction. Cuts the server-side footprint.
    persist_default: bool = Field(default=True)
    # True -> open every conversation as a temporary/private chat (WS `disableMemory=1`): it is
    # not saved to the user's Copilot history and produces no memories. Captured from the web
    # client's incognito toggle. Default on so the proxy doesn't pollute the user's chat history.
    disable_memory: bool = Field(default=True)
    # SQLite file for the session/conversation store. Empty -> default under the home dir.
    # Tests point this at a tmp file so they never touch the real store.
    session_db_path: str = Field(default="")
    # Cap on stored conversations: when exceeded, the least-recently-used ones are evicted
    # (cache + DB) so the store can't grow without bound. 0 -> no cap.
    session_max: int = Field(default=1000)
    # Evict conversations unused for this many seconds. 0 -> no TTL (cap-only eviction).
    session_ttl_seconds: int = Field(default=0)
    # Seconds without any substrate frame before giving up; WS handshake open timeout.
    recv_timeout: int = Field(default=90)
    open_timeout: int = Field(default=30)
    # Concurrency limit for substrate requests (0 or negative -> no limit)
    substrate_concurrency_limit: int = Field(default=2)
    # True -> cap the combined prompt/context before sending to substrate.
    # False -> send the full combined text without pre-send truncation.
    truncation_before_sending: bool = Field(default=True)
    session_id: str = Field(default="")
    session_salt: str = Field(default="")
    debug: bool = Field(default=False)
    timing: bool = Field(default=False)
    debug_tooling_json_log_enabled: bool = Field(
        default=True, alias="M365_DEBUG_TOOLING_JSON_LOG_ENABLED"
    )
    debug_tooling_json_log_file: str = Field(
        default="logs/debug_tooling.jsonl", alias="M365_DEBUG_TOOLING_JSON_LOG_FILE"
    )
    debug_tooling_json_log_max_chars: int = Field(
        default=50000, alias="M365_DEBUG_TOOLING_JSON_LOG_MAX_CHARS"
    )
    debug_tooling_raw_log_enabled: bool = Field(
        default=True, alias="M365_DEBUG_TOOLING_RAW_LOG_ENABLED"
    )
    debug_tooling_raw_log_dir: str = Field(
        default="logs", alias="M365_DEBUG_TOOLING_RAW_LOG_DIR"
    )
    debug_tooling_raw_log_name_pattern: str = Field(
        default="activity-{timestamp}.log", alias="M365_DEBUG_TOOLING_RAW_LOG_NAME_PATTERN"
    )
    debug_tooling_raw_log_max_chars: int = Field(
        default=100000, alias="M365_DEBUG_TOOLING_RAW_LOG_MAX_CHARS"
    )
    substrate_config_path: str = Field(default="")
    edge_headless: bool = Field(default=False)
    refresh_token: str = Field(default="")
    tenant_id: str = Field(default="")
    client_id: str = Field(default="")
    # True -> keep one substrate WebSocket alive per persistent session (skips the per-turn
    # handshake). Default off until validated against substrate (see Workstream A).
    ws_reuse: bool = Field(default=False)
    # True -> automatically close/hide the debug browser window when a token is successfully acquired.
    hide_on_token_success: bool = Field(
        default=True
    )
    # Path to the Edge executable used for the debug token-capture window.
    edge_path: str = Field(
        default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    prefer_active_browser: bool = Field(
        default=True,
    )
    # Passthrough: models NOT recognized as ours (m365-*) are forwarded to the real Anthropic API
    # on /v1/messages, instead of being routed to substrate. Off by default.
    anthropic_passthrough: bool = Field(
        default=False, alias="M365_ANTHROPIC_PASSTHROUGH"
    )
    anthropic_upstream: str = Field(
        default="https://api.anthropic.com"
    )
    anthropic_version: str = Field(default="2023-06-01")
    # OAuth credential source (default): the Claude Code login file. Free (uses the subscription).
    anthropic_creds_file: str = Field(default="")
    # API-key override: if set, passthrough uses x-api-key with this key (consumes API credits)
    # instead of the OAuth subscription token.
    anthropic_key: str = Field(default="")

    # Serve command defaults. CLI flags still override these values.
    serve_host: str = Field(default="127.0.0.1")
    serve_port: int = Field(default=8000)
    serve_cdp_port: int = Field(default=9222)
    serve_auto_refresh: bool = Field(default=True)
    serve_launch_edge: bool = Field(default=True)
    serve_capture_on_start: bool = Field(default=True)
    serve_capture_timeout_seconds: int = Field(
        default=180
    )
    serve_refresh_before_seconds: int = Field(default=900)
    serve_refresh_retry_seconds: int = Field(
        default=60
    )
    serve_configure_clients: bool = Field(default=True)

    # Capture / browser command defaults.
    capture_token_cdp_port: int = Field(default=9222)
    capture_token_timeout_seconds: int = Field(
        default=60
    )
    launch_edge_cdp_port: int = Field(default=9222)
    configure_undo: bool = Field(default=False)



def _field_name_for_config_key(key: str) -> str:
    normalized = key.strip()
    upper = normalized.upper()
    for name, field in Settings.model_fields.items():
        if upper == str(field.alias or '').upper():
            return name
    return normalized.lower().removeprefix('m365_')


def _read_env_file_value(key: str) -> str | None:
    try:
        for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            name, raw_value = stripped.split('=', 1)
            if name.strip() == key:
                return _coerce_env_value(raw_value)
    except FileNotFoundError:
        return None
    return None


def _coerce_env_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _write_env_file_value(key: str, value: str) -> None:
    lines = []
    replaced = False
    try:
        lines = ENV_PATH.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        pass
    for idx, line in enumerate(lines):
        if line.strip().startswith('#') or '=' not in line:
            continue
        name, _raw_value = line.split('=', 1)
        if name.strip() == key:
            lines[idx] = f'{key}={value}'
            replaced = True
            break
    if not replaced:
        lines.append(f'{key}={value}')
    ENV_PATH.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def read_config_value(key: str) -> str | None:
    field_name = _field_name_for_config_key(key)
    if field_name == 'access_token':
        return os.environ.get(ACCESS_TOKEN_ENV_KEY) or _read_env_file_value(ACCESS_TOKEN_ENV_KEY)
    values = _load_ini_settings()
    value = values.get(field_name)
    return None if value is None else str(value)


def write_config_value(key: str, value: str, section: str = 'settings') -> None:
    field_name = _field_name_for_config_key(key)
    if field_name == 'access_token':
        _write_env_file_value(ACCESS_TOKEN_ENV_KEY, value)
        return
    _ensure_config_ini_exists()
    path = INI_CONFIG_PATHS[0]
    parser = configparser.ConfigParser()
    parser.read(path, encoding='utf-8')
    if not parser.has_section(section):
        parser.add_section(section)
    parser.set(section, field_name, value)
    with path.open('w', encoding='utf-8') as f:
        parser.write(f)
