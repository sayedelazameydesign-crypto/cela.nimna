import json
from pathlib import Path

import pytest

from nimna.providers.base import normalize_json_schema, to_gemini_schema
from nimna.tools import ToolContext, ToolError, default_registry
from nimna.tools.sandbox import run_python_code


@pytest.fixture
def ctx(settings, workspace, skills):
    from nimna.memory import MemoryStore

    return ToolContext(settings=settings, workspace=workspace, session_id="t", memory=MemoryStore(":memory:"), skills=skills)


def run(registry, name, args, ctx):
    out, ok, _ = registry.execute(name, args, ctx)
    return json.loads(out), ok


def test_path_jail(ctx):
    with pytest.raises(ToolError):
        ctx.resolve_path("../secret.txt")
    with pytest.raises(ToolError):
        ctx.resolve_path("/etc/passwd")
    assert ctx.resolve_path("sub/../notes.txt").name == "notes.txt"


def test_validation_errors_are_returned_to_model(ctx):
    registry = default_registry()
    data, ok = run(registry, "read_csv", {"path": "sales.csv", "max_rows": "many"}, ctx)
    assert not ok and "max_rows" in data["error"]
    data, ok = run(registry, "nope", {}, ctx)
    assert not ok and "unknown tool" in data["error"]


def test_csv_tools(ctx):
    registry = default_registry()
    data, ok = run(registry, "read_csv", {"path": "sales.csv", "max_rows": 3}, ctx)
    assert ok and data["rows"] > 50 and len(data["sample"]) == 3
    types = {c["name"]: c["type"] for c in data["columns"]}
    assert types["revenue"] == "float" and types["date"] == "date" and types["region"] == "text"
    missing = {c["name"]: c["missing"] for c in data["columns"]}
    assert missing["sales_rep"] > 0

    data, ok = run(registry, "calculate_statistics",
                   {"path": "sales.csv", "columns": ["revenue"], "group_by": "region", "aggregate": "sum"}, ctx)
    assert ok and data["statistics"]["revenue"]["count"] == data["rows"]
    table = data["group_by"]["table"]
    assert table and table[0]["sum_revenue"] >= table[-1]["sum_revenue"]

    data, ok = run(registry, "calculate_statistics", {"path": "sales.csv", "columns": ["region"]}, ctx)
    assert not ok and "not numeric" in data["error"]


def test_file_tools_and_dynamic_risk(ctx):
    registry = default_registry()
    data, ok = run(registry, "list_files", {"pattern": "*.txt"}, ctx)
    assert ok and data["entries"][0]["path"] == "notes.txt"
    data, ok = run(registry, "read_file", {"path": "notes.txt"}, ctx)
    assert ok and data["content"] == "hello\nworld\n"

    tool = registry.get("write_file")
    new = tool.validate({"path": "out/new.md", "content": "x"})
    assert tool.effective_risk(new, ctx) == "safe"
    existing = tool.validate({"path": "notes.txt", "content": "x", "overwrite": True})
    assert tool.effective_risk(existing, ctx) == "confirm"
    assert registry.get("delete_file").risk == "confirm"

    data, ok = run(registry, "write_file", {"path": "notes.txt", "content": "x"}, ctx)
    assert not ok and "already exists" in data["error"]


def test_report_tool(ctx, workspace):
    registry = default_registry()
    data, ok = run(registry, "write_report", {"title": "Sales Summary", "content_markdown": "- a\n- b"}, ctx)
    assert ok and data["report"] == "reports/sales-summary.md"
    text = (workspace / "reports" / "sales-summary.md").read_text(encoding="utf-8")
    assert text.startswith("# Sales Summary")


def test_memory_tools(ctx):
    registry = default_registry()
    data, ok = run(registry, "memory_save", {"content": "user prefers short tables", "kind": "preference"}, ctx)
    assert ok
    data, ok = run(registry, "memory_search", {"query": "short tables"}, ctx)
    assert ok and data["results"][0]["kind"] == "preference"


def test_skill_tools(ctx):
    registry = default_registry()
    loaded = []
    ctx.on_skill_loaded = loaded.append
    data, ok = run(registry, "load_skill", {"name": "csv_analysis"}, ctx)
    assert ok and "read_csv" in data["allowed_tools"] and loaded == ["csv_analysis"]
    data, ok = run(registry, "read_skill_reference", {"skill": "csv_analysis", "path": "references/statistics_guide.md"}, ctx)
    assert ok and "median" in data["content"]
    data, ok = run(registry, "load_skill", {"name": "missing"}, ctx)
    assert not ok


def test_sandbox_subprocess(settings, workspace):
    result = run_python_code("import os; print(sorted(os.listdir('.')))", settings, workspace)
    assert result.ok and "sales.csv" in result.stdout
    result = run_python_code("import time; time.sleep(3)", settings, workspace, timeout=1)
    assert result.timed_out
    result = run_python_code("raise SystemExit(3)", settings, workspace)
    assert result.exit_code == 3


def _all_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _all_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _all_keys(item)


def test_schemas_are_provider_compatible():
    registry = default_registry()
    for tool in registry.all():
        spec = tool.spec()
        assert spec.parameters["type"] == "object"
        keys = set(_all_keys(spec.parameters))
        assert not keys & {"$defs", "$ref", "title", "default"}, tool.name
        gemini_keys = set(_all_keys(to_gemini_schema(spec.parameters)))
        assert not gemini_keys & {"anyOf", "oneOf", "title", "default", "additionalProperties"}, tool.name


def test_gemini_schema_nullable_conversion():
    schema = normalize_json_schema({
        "type": "object",
        "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "X"}},
    })
    assert to_gemini_schema(schema)["properties"]["x"] == {"type": "string", "nullable": True}
