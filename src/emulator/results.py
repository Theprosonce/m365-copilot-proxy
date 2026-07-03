from typing import Any

def error_label(error_type: str) -> str:
    labels = {
        "parse_error": "Parse error",
        "unknown_tool": "Unknown tool error",
        "validation_error": "Validation error",
        "tool_error": "Tool error",
        "sandbox_error": "Sandbox error",
        "execution_error": "Execution error",
    }
    return labels.get(error_type, "Tool error")


def format_error_result(
    error_type: str,
    details: str,
    *,
    name: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "success": False,
        "error": error_label(error_type),
        "error_type": error_type,
        "details": details,
    }
    if name is not None:
        payload["name"] = name
    if arguments is not None:
        payload["arguments"] = arguments
    return payload
