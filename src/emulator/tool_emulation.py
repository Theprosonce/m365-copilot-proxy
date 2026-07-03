import json
import logging
from typing import Any, Tuple
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.models import ToolCall
from .runtime_bridge import RuntimeBridge

logger = logging.getLogger(__name__)

class LocalToolEmulator:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def execute_react_loop(
        self,
        client: Any,
        prompt: str,
        additional_context: list[str],
        session: Any,
        tone: str,
        normalized_tools: list[dict[str, Any]],
        calls: list[ToolCall],
        text: str,
        parse_response_fn: Any,
        redact_tool_sentinels_fn: Any,
        workspace_root: str | None = None,
    ) -> Tuple[list[ToolCall] | None, str]:
        from m365_copilot_openai_proxy.debug_logger import log_event, log_raw_event

        run_mode = (self.settings.tool_emulation_run_mode or "auto").strip().lower()

        while calls:
            bridge = RuntimeBridge(
                root_dir=workspace_root or ".",
                allow_bash=not self.settings.tool_emulation_execution_sandbox
            )

            # In "auto" mode, we only run locally if ALL parsed tool calls are supported locally.
            # If run_mode is "local", we proceed with local execution regardless.
            if run_mode == "auto":
                all_supported = True
                for call in calls:
                    if call.function.name not in bridge.tools:
                        all_supported = False
                        break

                if not all_supported:
                    logger.info("Some tool calls are not supported locally in auto mode. Returning to client platform.")
                    break

            logger.info(f"ReAct Loop: Executing tool calls: {calls}")

            results = []
            for call in calls:
                call_dict = {
                    "name": call.function.name,
                    "arguments": json.loads(call.function.arguments) if isinstance(call.function.arguments, str) else call.function.arguments
                }
                log_event("TOOL_SENT", {
                    "name": call.function.name,
                    "arguments": call_dict["arguments"]
                })
                log_raw_event("Tool Send", {
                    "name": call.function.name,
                    "arguments": call_dict["arguments"]
                })
                res = bridge.execute_call(call_dict)

                success = res.get("success", False)
                output_data = res.get("output", "")
                stdout_str = str(output_data) if not isinstance(output_data, dict) else json.dumps(output_data, ensure_ascii=False)
                exit_code = res.get("metadata", {}).get("exit_code", 0) if isinstance(res.get("metadata"), dict) else 0
                stderr_str = res.get("metadata", {}).get("stderr", "") if isinstance(res.get("metadata"), dict) else ""

                log_event("TOOL_RECV", {
                    "name": call.function.name,
                    "success": success,
                    "exit_code": exit_code,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "stdout_len": len(stdout_str),
                    "stderr_len": len(stderr_str)
                })
                log_raw_event("Tool Receive", {
                    "success": success,
                    "stdout": stdout_str
                })
                results.append(res)

            tool_result_str = "\n".join(
                f"Tool result [{call.function.name}]: {json.dumps(res, ensure_ascii=False)}"
                for call, res in zip(calls, results)
            )

            log_event("TOOL_RESULT_RETURNED", {
                "format": "text",
                "payload_len": len(tool_result_str),
                "payload": {"text": tool_result_str}
            })
            log_raw_event("Tool Return", {
                "result": tool_result_str
            })

            additional_context = list(additional_context)
            assistant_transcript = f"Assistant (tool call):\n{redact_tool_sentinels_fn(text)}"
            additional_context.append(assistant_transcript)
            additional_context.append(f"Tool results:\n{tool_result_str}")

            text = await client.chat(prompt, additional_context, session, tone)
            log_event("MODEL_RECV_RAW", {
                "status_code": 200,
                "raw": text,
                "chunks": [text]
            })
            log_raw_event("Receive", {
                "content": text
            })
            calls = parse_response_fn(
                text, normalized_tools, workspace_root=workspace_root
            )

            pipeline = getattr(parse_response_fn, "__self__", None)
            if pipeline and hasattr(pipeline, "_diagnose_tool_protocol_error"):
                has_err, err_cat, err_tool, err_reason, feedback_msg = pipeline._diagnose_tool_protocol_error(text, calls)
                
                react_attempt = 0
                retry_limit = getattr(self.settings, "tool_emulation_protocol_error_retry_limit", 1)
                
                while has_err and react_attempt < retry_limit:
                    # Log internal error and feedback
                    log_payload = {
                        "event": "Tool Protocol Error",
                        "category": err_cat,
                        "tool": err_tool,
                        "user_visible": False,
                        "will_retry_model": True
                    }
                    log_raw_event("Tool Protocol Error", log_payload)
                    log_event("Tool Protocol Error", log_payload)

                    feedback_payload = {
                        "feedback": feedback_msg,
                        "attempt": react_attempt + 1,
                        "user_visible": False
                    }
                    log_raw_event("Tool Protocol Feedback Sent To Model", feedback_payload)
                    log_event("Tool Protocol Feedback Sent To Model", feedback_payload)

                    # Update context for retry
                    additional_context = list(additional_context)
                    additional_context.append(f"Assistant:\n{text}")
                    additional_context.append(feedback_msg)

                    react_attempt += 1

                    # Call model again
                    text = await client.chat(prompt, additional_context, session, tone)
                    
                    retry_payload = {
                        "content": text,
                        "attempt": react_attempt,
                        "user_visible": False
                    }
                    log_raw_event("Tool Protocol Retry Receive", retry_payload)
                    log_event("Tool Protocol Retry Receive", retry_payload)

                    log_event("MODEL_RECV_RAW", {
                        "status_code": 200,
                        "raw": text,
                        "chunks": [text]
                    })
                    log_raw_event("Receive", {
                        "content": text
                    })

                    # Parse again
                    calls = parse_response_fn(
                        text, normalized_tools, workspace_root=workspace_root
                    )
                    has_err, err_cat, err_tool, err_reason, feedback_msg = pipeline._diagnose_tool_protocol_error(text, calls)

                if has_err:
                    # Final fallback
                    log_payload = {
                        "event": "Tool Protocol Error",
                        "category": err_cat,
                        "tool": err_tool,
                        "user_visible": True,
                        "will_retry_model": False
                    }
                    log_raw_event("Tool Protocol Error", log_payload)
                    log_event("Tool Protocol Error", log_payload)

                    final_return_payload = {
                        "format": "tool_protocol_error",
                        "error": err_reason,
                        "user_visible": True
                    }
                    log_raw_event("Tool Return", final_return_payload)
                    log_event("Tool Return", final_return_payload)

                    if err_cat == "unsupported_tool_call":
                        visible_message = (
                            "I could not complete the tool step because the requested tool is unsupported by the current runtime."
                        )
                    else:
                        visible_message = (
                            f"Tool protocol error: TOOL_CALLS block was detected but could not be executed.\n\n"
                            f"Reason:\n{err_reason}\n\n"
                            f"Please re-emit ONLY a corrected TOOL_CALLS block, or continue without the tools."
                        )
                    return None, visible_message

        if calls:
            return calls, redact_tool_sentinels_fn(text)
        else:
            return None, redact_tool_sentinels_fn(text)
