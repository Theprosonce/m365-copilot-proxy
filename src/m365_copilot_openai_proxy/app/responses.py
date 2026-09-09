from __future__ import annotations

import json
import uuid

def _responses_output_from_tool_calls(calls: list | None) -> list[dict]:
    output: list[dict] = []
    for call in calls or []:
        output.append(
            {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": getattr(call, "id", ""),
                "name": call.function.name,
                "arguments": call.function.arguments,
            }
        )
    return output

def _args_obj(arguments: str) -> dict:
    try:
        obj = json.loads(arguments or "{}")
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}
