import pytest

from app.agent.tool_registry import (
    DuplicateToolNameError,
    ToolManifestError,
    discover_tools,
)


def _write_tool(tools_dir, name: str, *, trigger_cue: str | None = None) -> None:
    tool_dir = tools_dir / name
    tool_dir.mkdir(parents=True)
    trigger_cue_line = f"trigger_cue: {trigger_cue!r}\n" if trigger_cue else ""
    (tool_dir / "manifest.yaml").write_text(
        f"name: {name}\n"
        f"description: \"{name} 的描述\"\n"
        f"{trigger_cue_line}"
        "parameters_schema:\n"
        "  type: object\n"
        "  properties:\n"
        "    query:\n"
        "      type: string\n"
        "  required: [query]\n",
        encoding="utf-8",
    )
    (tool_dir / "tool.py").write_text(
        "class _FakeTool:\n"
        "    async def resolve_arguments(self, raw_arguments, *, context):\n"
        "        return raw_arguments\n"
        "\n"
        "    async def execute(self, arguments, *, context):\n"
        "        return ({'ok': True, 'name': %r}, [])\n"
        "\n"
        "TOOL = _FakeTool()\n" % name,
        encoding="utf-8",
    )


def test_discover_tools_finds_and_registers_tools_sorted_by_name(tmp_path):
    _write_tool(tmp_path, "zzz_tool")
    _write_tool(tmp_path, "aaa_tool", trigger_cue="遇到aaa场景时使用")

    registry = discover_tools(tmp_path)

    names = [manifest.name for _, manifest in registry.all()]
    assert names == ["aaa_tool", "zzz_tool"]
    assert registry.trigger_cues() == ["遇到aaa场景时使用"]


async def test_discover_tools_registered_tool_resolve_and_execute_work(tmp_path):
    _write_tool(tmp_path, "echo_tool")
    registry = discover_tools(tmp_path)

    tool, manifest = registry.get("echo_tool")
    assert manifest.name == "echo_tool"
    resolved = await tool.resolve_arguments({"query": "x"}, context=None)
    assert resolved == {"query": "x"}
    result, records = await tool.execute(resolved, context=None)
    assert result == {"ok": True, "name": "echo_tool"}
    assert records == []


def test_discover_tools_raises_on_duplicate_name(tmp_path):
    _write_tool(tmp_path, "dup_tool")
    # 两个不同目录名，manifest 里声明相同的 name。
    (tmp_path / "dup_tool_2").mkdir()
    (tmp_path / "dup_tool_2" / "manifest.yaml").write_text(
        "name: dup_tool\ndescription: \"重复\"\nparameters_schema:\n  type: object\n  properties: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "dup_tool_2" / "tool.py").write_text(
        "class _T:\n"
        "    async def resolve_arguments(self, raw_arguments, *, context):\n"
        "        return raw_arguments\n"
        "    async def execute(self, arguments, *, context):\n"
        "        return ({}, [])\n"
        "TOOL = _T()\n",
        encoding="utf-8",
    )

    with pytest.raises(DuplicateToolNameError):
        discover_tools(tmp_path)


def test_discover_tools_raises_on_manifest_missing_required_field(tmp_path):
    tool_dir = tmp_path / "broken_tool"
    tool_dir.mkdir()
    (tool_dir / "manifest.yaml").write_text("description: \"缺 name 字段\"\n", encoding="utf-8")
    (tool_dir / "tool.py").write_text("TOOL = None\n", encoding="utf-8")

    with pytest.raises(ToolManifestError):
        discover_tools(tmp_path)


def test_discover_tools_raises_on_invalid_yaml(tmp_path):
    tool_dir = tmp_path / "bad_yaml_tool"
    tool_dir.mkdir()
    (tool_dir / "manifest.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    (tool_dir / "tool.py").write_text("TOOL = None\n", encoding="utf-8")

    with pytest.raises(ToolManifestError):
        discover_tools(tmp_path)


def test_discover_tools_raises_when_tool_py_missing_TOOL_export(tmp_path):
    tool_dir = tmp_path / "no_export_tool"
    tool_dir.mkdir()
    (tool_dir / "manifest.yaml").write_text(
        "name: no_export_tool\ndescription: \"x\"\nparameters_schema:\n  type: object\n  properties: {}\n",
        encoding="utf-8",
    )
    (tool_dir / "tool.py").write_text("# 没有定义 TOOL\n", encoding="utf-8")

    with pytest.raises(ToolManifestError):
        discover_tools(tmp_path)


def test_tool_manifest_to_schema_shape(tmp_path):
    _write_tool(tmp_path, "shape_tool")
    registry = discover_tools(tmp_path)
    _, manifest = registry.get("shape_tool")

    schema = manifest.to_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "shape_tool"
    assert schema["function"]["parameters"]["required"] == ["query"]


def test_registry_schemas_only_includes_registered_tools(tmp_path):
    _write_tool(tmp_path, "only_tool")
    registry = discover_tools(tmp_path)

    schemas = registry.schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "only_tool"
