import json
import datetime
import contextvars
from typing import Any
from pathlib import Path

# ContextVar to capture context about the current API request
request_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "request_context", default={}
)

_RAW_LOG_PATH: Path | None = None
_RAW_LOG_PATH_CONFIG: tuple[str, str] | None = None


def _timestamp_for_filename() -> str:
    return datetime.datetime.now().astimezone().strftime("%y%m%d%H%M%S")


def _timestamped_log_path(log_dir: str, name_pattern: str) -> Path:
    timestamp = _timestamp_for_filename()
    filename = name_pattern.format(timestamp=timestamp)
    return Path(log_dir) / filename


def _get_raw_log_path(log_dir: str, name_pattern: str) -> Path:
    global _RAW_LOG_PATH, _RAW_LOG_PATH_CONFIG

    config_key = (log_dir, name_pattern)
    if _RAW_LOG_PATH is None or _RAW_LOG_PATH_CONFIG != config_key:
        _RAW_LOG_PATH = _timestamped_log_path(log_dir, name_pattern)
        _RAW_LOG_PATH_CONFIG = config_key
    return _RAW_LOG_PATH


def log_event(event_name: str, payload: Any) -> None:
    """Logs a diagnostic event as a JSON line to the configured log file."""
    try:
        from m365_copilot_openai_proxy.config import Settings
        settings = Settings()
        enabled = getattr(settings, "debug_tooling_json_log_enabled", True)
        log_file_name = getattr(settings, "debug_tooling_json_log_file", "logs/debug_tooling.jsonl")
        max_chars = getattr(settings, "debug_tooling_json_log_max_chars", 50000)
    except Exception:
        enabled = True
        log_file_name = "logs/debug_tooling.jsonl"
        max_chars = 50000

    if not enabled:
        return

    ctx = request_context.get()

    redacted_payload = redact_sensitive(payload)
    truncated_payload = truncate_strings(redacted_payload, max_chars)

    log_entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": event_name,
        "request_id": ctx.get("request_id"),
        "session_id": ctx.get("session_id"),
        "route": ctx.get("route", "substrate"),
        "api": ctx.get("api", "/v1/messages"),
        "model": ctx.get("model", "m365-gpt-think"),
        "stream": ctx.get("stream", True),
        "payload": truncated_payload
    }

    # Filter out None values from top-level keys to match user spec
    log_entry = {k: v for k, v in log_entry.items() if v is not None}

    try:
        log_path = Path(log_file_name)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[debug_logger] Error writing to JSONL log: {e}")

def redact_sensitive(data: Any) -> Any:
    """Recursively redacts values for keys containing sensitive terms."""
    if isinstance(data, dict):
        redacted = {}
        for k, v in data.items():
            k_lower = k.lower()
            if any(term in k_lower for term in [
                "authorization", "cookie", "token", "secret", "password",
                "api_key", "apikey", "access_token", "refresh_token"
            ]):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = redact_sensitive(v)
        return redacted
    elif isinstance(data, list):
        return [redact_sensitive(item) for item in data]
    return data

def truncate_strings(data: Any, max_chars: int) -> Any:
    """Recursively truncates string values that exceed max_chars."""
    if isinstance(data, str):
        if len(data) > max_chars:
            return {
                "truncated": True,
                "max_chars": max_chars,
                "length": len(data),
                "data": data[:max_chars] + "... [TRUNCATED]"
            }
        return data
    elif isinstance(data, dict):
        return {k: truncate_strings(v, max_chars) for k, v in data.items()}
    elif isinstance(data, list):
        return [truncate_strings(item, max_chars) for item in data]
    return data

def log_raw_event(status: str, payload: Any) -> None:
    """Logs a raw diagnostic event in format: [time] [Status] : raw json"""
    try:
        from m365_copilot_openai_proxy.config import Settings
        settings = Settings()
        enabled = getattr(settings, "debug_tooling_raw_log_enabled", True)
        log_dir = getattr(settings, "debug_tooling_raw_log_dir", "logs")
        name_pattern = getattr(
            settings,
            "debug_tooling_raw_log_name_pattern",
            "activity-{timestamp}.log",
        )
        max_chars = getattr(settings, "debug_tooling_raw_log_max_chars", 100000)
    except Exception:
        enabled = True
        log_dir = "logs"
        name_pattern = "activity-{timestamp}.log"
        max_chars = 100000

    if not enabled:
        return

    # Redact recursively
    redacted_payload = redact_sensitive(payload)
    # Truncate strings recursively
    truncated_payload = truncate_strings(redacted_payload, max_chars)

    # Time format: [time] e.g. [2026-06-27T15:29:00+07:00]
    time_str = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    
    # Format line: [time] [Status] : json
    log_line = f"[{time_str}] [{status}] : {json.dumps(truncated_payload, ensure_ascii=False)}"

    try:
        log_path = _get_raw_log_path(log_dir, name_pattern)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception as e:
        print(f"[debug_logger] Error writing to raw log: {e}")
