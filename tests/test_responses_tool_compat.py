from __future__ import annotations

from m365_copilot_openai_proxy.models import OpenAIResponsesRequest


def test_responses_request_accepts_standard_tool_fields() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "m365-opus",
            "input": "read file",
            "stream": True,
            "temperature": 0,
            "user": "client-session",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        }
    )

    assert req.tools and req.tools[0]["function"]["name"] == "read"
    assert req.tool_choice == "auto"
    assert req.stream is True
    assert req.temperature == 0
    assert req.user == "client-session"


def test_responses_request_accepts_legacy_function_fields() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "m365-opus",
            "input": "read file",
            "functions": [{"name": "read", "parameters": {"type": "object"}}],
            "function_call": "auto",
        }
    )

    assert req.functions and req.functions[0]["name"] == "read"
    assert req.function_call == "auto"
