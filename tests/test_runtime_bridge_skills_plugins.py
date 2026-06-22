from __future__ import annotations

from pathlib import Path

from middleware.runtime_bridge import RuntimeBridge, _BEGIN, _END


def test_list_and_load_skills(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "graphify"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Graphify\n\nTurn input into a knowledge graph.", encoding="utf-8"
    )

    bridge = RuntimeBridge(str(tmp_path))

    listed = bridge.process_assistant_message(
        f'{_BEGIN}[{{"name":"list_skills","arguments":{{}}}}]{_END}'
    )
    assert listed is not None
    assert listed[0]["status"] == "success"
    assert listed[0]["result"]["count"] == 1
    assert listed[0]["result"]["skills"][0]["name"] == "graphify"

    loaded = bridge.process_assistant_message(
        f'{_BEGIN}[{{"name":"skill","arguments":{{"name":"graphify"}}}}]{_END}'
    )
    assert loaded is not None
    assert loaded[0]["status"] == "success"
    assert loaded[0]["result"]["name"] == "graphify"
    assert "knowledge graph" in loaded[0]["result"]["content"]


def test_plugin_registers_tool(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin_echo.py"
    plugin.write_text(
        "def echo(root, args):\n"
        "    return ({'echo': args.get('text', '')}, {'plugin': 'echo'})\n\n"
        "def register(registry):\n"
        "    registry.register('echo_plugin', echo)\n",
        encoding="utf-8",
    )

    bridge = RuntimeBridge(str(tmp_path), plugin_paths=["plugin_echo.py"])
    results = bridge.process_assistant_message(
        f'{_BEGIN}[{{"name":"echo_plugin","arguments":{{"text":"hello"}}}}]{_END}'
    )

    assert results is not None
    assert results[0]["status"] == "success"
    assert results[0]["result"] == {"echo": "hello"}
    assert results[0]["metadata"] == {"plugin": "echo"}


def test_plugin_cannot_override_builtin(tmp_path: Path) -> None:
    plugin = tmp_path / "bad_plugin.py"
    plugin.write_text(
        "def register(registry):\n"
        "    registry.register('read', lambda root, args: ({}, {}))\n",
        encoding="utf-8",
    )

    try:
        RuntimeBridge(str(tmp_path), plugin_paths=["bad_plugin.py"])
    except Exception as exc:
        assert "conflicts with a built-in tool" in str(exc)
    else:
        raise AssertionError("Expected plugin override to fail")


def test_extra_tools_register_without_file_plugin(tmp_path: Path) -> None:
    def double(root: Path, args: dict):
        value = args.get("value", 0)
        return {"value": value * 2}, {"source": "extra_tools"}

    bridge = RuntimeBridge(str(tmp_path), extra_tools={"double": double})
    results = bridge.process_assistant_message(
        f'{_BEGIN}[{{"name":"double","arguments":{{"value":21}}}}]{_END}'
    )

    assert results is not None
    assert results[0]["status"] == "success"
    assert results[0]["result"] == {"value": 42}
