from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

_BEGIN = "<<<TOOL_CALLS>>>"
_END = "<<<END_TOOL_CALLS>>>"

logger = logging.getLogger(__name__)


class ToolError(Exception):
    pass


class SandboxError(ToolError):
    pass


def resolve_and_sandbox_path(root: Path, path: str) -> Path:
    target = (
        (root / path).resolve()
        if not Path(path).is_absolute()
        else Path(path).resolve()
    )
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SandboxError(
            f"Access denied: path {path} is outside project root"
        ) from exc
    return target


def _safe_rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def tool_glob(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("Missing 'pattern' argument")
    raw_matches = sorted(root.glob(pattern))
    matches = [_safe_rel(root, p) for p in raw_matches if p.exists()]
    return ({"matches": matches, "count": len(matches)}, {"pattern": pattern})


def tool_list(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = args.get("path", ".")
    if not isinstance(path, str):
        raise ToolError("'path' must be a string")
    target = resolve_and_sandbox_path(root, path)
    if not target.exists():
        raise ToolError(f"Path not found: {path}")
    if not target.is_dir():
        raise ToolError(f"Path is not a directory: {path}")
    entries = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        entries.append(
            {
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if entry.exists() else 0,
            }
        )
    return (
        {"entries": entries, "count": len(entries)},
        {"path": _safe_rel(root, target)},
    )


def tool_read(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = args.get("path") or args.get("file_path") or args.get("filePath")
    if not isinstance(path, str) or not path:
        raise ToolError("Missing 'path' argument")
    target = resolve_and_sandbox_path(root, path)
    if not target.exists() or not target.is_file():
        raise ToolError(f"File not found: {path}")
    offset = args.get("offset", 0)
    limit = args.get("limit", 4000)
    if not isinstance(offset, int) or offset < 0:
        raise ToolError("'offset' must be a non-negative integer")
    if not isinstance(limit, int) or limit <= 0:
        raise ToolError("'limit' must be a positive integer")

    content = target.read_text(encoding="utf-8", errors="replace")
    total_size = len(content)
    sliced = content[offset : offset + limit]
    is_partial = (offset + limit) < total_size
    return (
        {
            "content": sliced,
            "total_size": total_size,
            "is_partial": is_partial,
        },
        {
            "path": _safe_rel(root, target),
            "offset": offset,
            "limit": limit,
            "returned_size": len(sliced),
            "truncated": is_partial,
        },
    )


def tool_search(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    query = args.get("query")
    if not isinstance(query, str) or not query:
        raise ToolError("Missing 'query' argument")
    path = args.get("path", ".")
    include = args.get("include", "**/*")
    if not isinstance(path, str):
        raise ToolError("'path' must be a string")
    if not isinstance(include, str) or not include:
        raise ToolError("'include' must be a non-empty string")

    target = resolve_and_sandbox_path(root, path)
    if not target.exists():
        raise ToolError(f"Path not found: {path}")
    if not target.is_dir():
        raise ToolError(f"Path is not a directory: {path}")

    matches: list[dict[str, Any]] = []
    for candidate in sorted(target.glob(include)):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hit_lines = [i + 1 for i, line in enumerate(text.splitlines()) if query in line]
        if hit_lines:
            matches.append({"path": _safe_rel(root, candidate), "lines": hit_lines})

    return (
        {"matches": matches, "count": len(matches)},
        {"path": _safe_rel(root, target), "query": query},
    )


def tool_write(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = args.get("path")
    content = args.get("content")
    if not isinstance(path, str) or not path:
        raise ToolError("Missing 'path' argument")
    if not isinstance(content, str):
        raise ToolError("Missing 'content' argument")

    target = resolve_and_sandbox_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return (
        {"success": True, "path_changed": _safe_rel(root, target)},
        {"affected_paths": [_safe_rel(root, target)], "bytes_written": len(content)},
    )


def tool_edit(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = args.get("path")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = args.get("replace_all", False)
    if (
        not isinstance(path, str)
        or not isinstance(old_string, str)
        or not isinstance(new_string, str)
    ):
        raise ToolError("Missing 'path', 'old_string', or 'new_string' argument")
    if not isinstance(replace_all, bool):
        raise ToolError("'replace_all' must be a boolean")

    target = resolve_and_sandbox_path(root, path)
    if not target.exists() or not target.is_file():
        raise ToolError(f"File not found: {path}")
    content = target.read_text(encoding="utf-8", errors="replace")
    if old_string not in content:
        raise ToolError("'old_string' not found in file")
    count = content.count(old_string) if replace_all else 1
    updated = content.replace(old_string, new_string, count)
    target.write_text(updated, encoding="utf-8")

    return (
        {
            "success": True,
            "path_changed": _safe_rel(root, target),
            "replacements": count,
        },
        {"affected_paths": [_safe_rel(root, target)], "replacements": count},
    )


def tool_bash(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    command = args.get("command")
    timeout = args.get("timeout", 10)
    workdir = args.get("workdir", ".")
    if not isinstance(command, str) or not command:
        raise ToolError("Missing 'command' argument")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ToolError("'timeout' must be a positive number")
    if not isinstance(workdir, str):
        raise ToolError("'workdir' must be a string")

    target_workdir = resolve_and_sandbox_path(root, workdir)
    if not target_workdir.exists() or not target_workdir.is_dir():
        raise ToolError(f"Working directory not found: {workdir}")

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(target_workdir),
            capture_output=True,
            text=True,
            timeout=float(timeout),
        )
        timed_out = False
        stdout = result.stdout
        stderr = result.stderr
        exit_code = result.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + "\nCommand timed out"
        exit_code = 124

    return (
        {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "working_directory": str(target_workdir),
            "timeout_applied": timeout,
            "timed_out": timed_out,
        },
        {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "working_directory": _safe_rel(root, target_workdir),
        },
    )


def _text_arg(args: dict[str, Any], *names: str) -> str:
    for name in names:
        value = args.get(name)
        if isinstance(value, str) and value:
            return value
    raise ToolError(f"Missing text argument: one of {', '.join(names)}")


def tool_ai_summarize(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    text = _text_arg(args, "text", "content", "input")
    max_sentences = args.get("max_sentences", 3)
    if not isinstance(max_sentences, int) or max_sentences <= 0:
        raise ToolError("'max_sentences' must be a positive integer")
    sentences = [part.strip() for part in text.replace("\n", " ").split(".") if part.strip()]
    summary = ". ".join(sentences[:max_sentences])
    if summary:
        summary += "."
    return {"summary": summary}, {"tool_type": "ai_text", "sentences": min(len(sentences), max_sentences)}


def tool_ai_extract_keywords(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    text = _text_arg(args, "text", "content", "input")
    limit = args.get("limit", 10)
    if not isinstance(limit, int) or limit <= 0:
        raise ToolError("'limit' must be a positive integer")
    stop = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "you", "your", "have", "has"}
    counts: dict[str, int] = {}
    for raw in text.lower().split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if len(word) < 3 or word in stop:
            continue
        counts[word] = counts.get(word, 0) + 1
    keywords = [word for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]
    return {"keywords": keywords}, {"tool_type": "ai_text", "limit": limit}


def tool_ai_classify(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    text = _text_arg(args, "text", "content", "input").lower()
    labels = args.get("labels")
    if labels is not None and (not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)):
        raise ToolError("'labels' must be a list of strings")
    if labels:
        scored = [(label, text.count(label.lower())) for label in labels]
        label = max(scored, key=lambda item: item[1])[0]
    elif any(word in text for word in ("error", "failed", "bug", "broken")):
        label = "issue"
    elif any(word in text for word in ("todo", "task", "action", "next")):
        label = "task"
    else:
        label = "general"
    return {"label": label}, {"tool_type": "ai_text"}


def tool_ai_rewrite(
    root: Path, args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    text = _text_arg(args, "text", "content", "input")
    style = args.get("style", "concise")
    if not isinstance(style, str):
        raise ToolError("'style' must be a string")
    rewritten = " ".join(text.split())
    if style == "bullet":
        rewritten = "\n".join(f"- {line.strip()}" for line in rewritten.split(". ") if line.strip())
    return {"text": rewritten, "style": style}, {"tool_type": "ai_text"}


class RuntimePluginRegistry:
    """Registry for runtime bridge tools supplied by plugins and skills.

    Plugins are explicit Python modules loaded from configured file paths. A plugin
    module may expose either ``register(registry)`` or ``TOOLS`` as a mapping of
    tool names to handlers. Handlers use the same contract as built-in tools:
    ``handler(root: Path, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]``.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    @property
    def tools(self) -> dict[str, Any]:
        return dict(self._tools)

    def register(self, name: str, handler: Any) -> None:
        if not isinstance(name, str) or not name:
            raise ToolError("Plugin tool name must be a non-empty string")
        if name in _RESERVED_TOOL_NAMES:
            raise ToolError(f"Plugin tool '{name}' conflicts with a built-in tool")
        if not callable(handler):
            raise ToolError(f"Plugin tool '{name}' must be callable")
        self._tools[name] = handler


def _load_plugin_module(path: Path) -> Any:
    import importlib.util

    module_name = f"middleware_runtime_plugin_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ToolError(f"Unable to load plugin module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin_tools(root: Path, plugin_paths: list[str] | None) -> dict[str, Any]:
    """Load plugin tools from Python files under the sandbox root."""

    registry = RuntimePluginRegistry()
    for raw_path in plugin_paths or []:
        plugin_path = resolve_and_sandbox_path(root, raw_path)
        if not plugin_path.exists() or not plugin_path.is_file():
            raise ToolError(f"Plugin not found: {raw_path}")
        if plugin_path.suffix != ".py":
            raise ToolError(f"Plugin must be a Python file: {raw_path}")

        module = _load_plugin_module(plugin_path)
        if hasattr(module, "register"):
            module.register(registry)
        elif hasattr(module, "TOOLS"):
            tools = getattr(module, "TOOLS")
            if not isinstance(tools, dict):
                raise ToolError(f"Plugin TOOLS must be a dict: {raw_path}")
            for name, handler in tools.items():
                registry.register(name, handler)
        else:
            raise ToolError(
                f"Plugin {raw_path} must expose register(registry) or TOOLS"
            )
    return registry.tools


def _find_skill_files(root: Path, skills_dir: str) -> list[Path]:
    base = resolve_and_sandbox_path(root, skills_dir)
    if not base.exists():
        return []
    if not base.is_dir():
        raise ToolError(f"Skills path is not a directory: {skills_dir}")
    files: list[Path] = []
    for candidate in sorted(base.rglob("*")):
        if candidate.is_file() and candidate.name.lower() in {"skill.md", "skill.txt"}:
            files.append(candidate)
    return files


def _skill_name(root: Path, path: Path) -> str:
    parent = path.parent.name.strip()
    if parent:
        return parent
    return path.stem


def _read_skill_doc(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def tool_list_skills(root: Path, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    skills_dir = args.get("skills_dir", ".claude/skills")
    if not isinstance(skills_dir, str):
        raise ToolError("'skills_dir' must be a string")

    skills = []
    for path in _find_skill_files(root, skills_dir):
        content = _read_skill_doc(path)
        first_heading = ""
        for line in content.splitlines():
            if line.startswith("#"):
                first_heading = line.lstrip("#").strip()
                break
        skills.append(
            {
                "name": _skill_name(root, path),
                "path": _safe_rel(root, path),
                "title": first_heading,
            }
        )
    return {"skills": skills, "count": len(skills)}, {"skills_dir": skills_dir}


def tool_skill(root: Path, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    name = args.get("name") or args.get("skill")
    skills_dir = args.get("skills_dir", ".claude/skills")
    if not isinstance(name, str) or not name:
        raise ToolError("Missing 'name' argument")
    if not isinstance(skills_dir, str):
        raise ToolError("'skills_dir' must be a string")

    requested = name.strip().lower()
    for path in _find_skill_files(root, skills_dir):
        skill_name = _skill_name(root, path)
        if skill_name.lower() == requested:
            content = _read_skill_doc(path)
            return (
                {
                    "name": skill_name,
                    "path": _safe_rel(root, path),
                    "content": content,
                },
                {"skills_dir": skills_dir, "path": _safe_rel(root, path)},
            )
    raise ToolError(f"Skill not found: {name}")


TOOLS = {
    "glob": tool_glob,
    "Glob": tool_glob,
    "list": tool_list,
    "List": tool_list,
    "read": tool_read,
    "Read": tool_read,
    "search": tool_search,
    "Search": tool_search,
    "write": tool_write,
    "Write": tool_write,
    "edit": tool_edit,
    "Edit": tool_edit,
    "bash": tool_bash,
    "Bash": tool_bash,
    "run": tool_bash,
    "Run": tool_bash,
    "ai_summarize": tool_ai_summarize,
    "ai_extract_keywords": tool_ai_extract_keywords,
    "ai_classify": tool_ai_classify,
    "ai_rewrite": tool_ai_rewrite,
    "skill": tool_skill,
    "list_skills": tool_list_skills,
}

_RESERVED_TOOL_NAMES = frozenset(TOOLS)


class RuntimeBridge:
    # `bash`/`run` execute arbitrary host commands via `subprocess.run(shell=True)`. The path
    # sandbox only constrains the *cwd*, NOT the command — so this is full RCE on the host. It is
    # gated behind an explicit opt-in (default off). When wiring execution, pass
    # `allow_bash = settings.tool_emulation_execution_enabled and not settings.tool_emulation_execution_sandbox`.
    _SHELL_TOOLS = frozenset({"bash", "run"})

    def __init__(
        self,
        root_dir: str,
        allow_bash: bool = False,
        plugin_paths: list[str] | None = None,
        extra_tools: dict[str, Any] | None = None,
    ):
        self.root = Path(root_dir).resolve()
        self.allow_bash = allow_bash
        self.conversation_history: list[dict[str, Any]] = []
        self.plugin_tools = load_plugin_tools(self.root, plugin_paths)
        if extra_tools:
            registry = RuntimePluginRegistry()
            for tool_name, handler in extra_tools.items():
                registry.register(tool_name, handler)
            self.plugin_tools.update(registry.tools)
        self.tools = {**TOOLS, **self.plugin_tools}

    def process_assistant_message(self, message: str) -> list[dict[str, Any]] | None:
        self.conversation_history.append({"role": "assistant", "content": message})
        calls = self._extract_tool_calls(message)
        if calls is None:
            return None
        if isinstance(calls, dict):
            results = [self._error("tool_error", "Parsed content is not a JSON array")]
            self._append_tool_result(results)
            return results

        results: list[dict[str, Any]] = []
        for call in calls:
            results.append(self.execute_call(call))
        self._append_tool_result(results)
        return results

    def _extract_tool_calls(self, message: str) -> Any | None:
        if not isinstance(message, str):
            return None
        begin = message.find(_BEGIN)
        if begin < 0:
            return None
        end = message.find(_END, begin + len(_BEGIN))
        if end < 0:
            return None
        payload = message[begin + len(_BEGIN) : end].strip()
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            return [self._error("parse_error", f"Invalid JSON: {exc}")]

    def execute_call(self, call: Any) -> dict[str, Any]:
        if isinstance(call, dict) and "error" in call and call.get("status") == "error":
            return call
        if not isinstance(call, dict):
            return self._error(
                "validation_error", "Each tool call entry must be an object"
            )

        name = call.get("name")
        if not isinstance(name, str) or not name:
            return self._error("validation_error", "Missing tool name")
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            return self._error(
                "validation_error", "Arguments must be an object", name=name
            )
        if name in self._SHELL_TOOLS and not self.allow_bash:
            return self._error(
                "sandbox_error",
                "Shell execution is disabled. It runs arbitrary host commands "
                "(shell=True) and the path sandbox does not constrain the command; "
                "enable explicitly via allow_bash.",
                name=name,
                arguments=arguments,
            )
        tool = self.tools.get(name)
        if tool is None:
            return self._error(
                "unknown_tool",
                f"Tool '{name}' is not supported",
                name=name,
                arguments=arguments,
            )

        try:
            logger.info("RuntimeBridge using tool: %s", name)
            output, meta = tool(self.root, arguments)
            return {
                "name": name,
                "arguments": arguments,
                "status": "success",
                "success": True,
                "result": output,
                "output": output,
                "metadata": meta,
            }
        except SandboxError as exc:
            return self._error(
                "sandbox_error", str(exc), name=name, arguments=arguments
            )
        except ToolError as exc:
            return self._error(
                "validation_error", str(exc), name=name, arguments=arguments
            )
        except Exception as exc:
            return self._error(
                "execution_error", str(exc), name=name, arguments=arguments
            )

    def format_tool_result_message(
        self, results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "role": "tool_result",
            "name": "runtime_bridge",
            "content": json.dumps(
                {"type": "tool_result", "results": results}, ensure_ascii=False
            ),
        }

    def inject_tool_result(
        self, conversation: list[dict[str, Any]], assistant_message: str
    ) -> bool:
        results = self.process_assistant_message(assistant_message)
        if results is None:
            return False
        conversation.append(self.format_tool_result_message(results))
        return True

    def _append_tool_result(self, results: list[dict[str, Any]]) -> None:
        self.conversation_history.append(
            {
                "role": "tool_result",
                "content": json.dumps(results, indent=2, ensure_ascii=False),
            }
        )

    @staticmethod
    def _error(
        error_type: str,
        details: str,
        *,
        name: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "error",
            "success": False,
            "error": _error_label(error_type),
            "error_type": error_type,
            "details": details,
        }
        if name is not None:
            payload["name"] = name
        if arguments is not None:
            payload["arguments"] = arguments
        return payload


def _error_label(error_type: str) -> str:
    labels = {
        "parse_error": "Parse error",
        "unknown_tool": "Unknown tool error",
        "validation_error": "Validation error",
        "tool_error": "Tool error",
        "sandbox_error": "Sandbox error",
        "execution_error": "Execution error",
    }
    return labels.get(error_type, "Tool error")
