from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import anyio
import pytest
from pydantic import ValidationError

from chatrepo_mcp import server
from chatrepo_mcp.command_tools import _tail
from chatrepo_mcp.result_models import _FIELD_TYPES, TOOL_RESULT_MODELS, TOOL_SUCCESS_SPECS
from chatrepo_mcp.server import mcp


def _tools_by_name() -> dict[str, Any]:
    async def collect() -> dict[str, Any]:
        return {tool.name: tool for tool in await mcp.list_tools()}

    return anyio.run(collect)


def _example_value(annotation: Any) -> Any:
    rendered = str(annotation)
    if "list" in rendered:
        return []
    if "dict" in rendered:
        return {}
    if "bool" in rendered:
        return False
    if "int" in rendered:
        return 0
    return "value"


def test_every_full_tool_has_exact_additive_union_schema() -> None:
    tools = _tools_by_name()

    assert len(TOOL_RESULT_MODELS) == 96
    assert set(tools) <= set(TOOL_RESULT_MODELS)
    for name, tool in tools.items():
        schema = tool.outputSchema
        assert schema is not None, name
        assert schema["type"] == "object", name
        assert schema["additionalProperties"] is True, name
        assert {"ok", "error", "error_kind"} <= set(schema["properties"]), name
        assert schema["anyOf"], name
        assert tuple(TOOL_SUCCESS_SPECS[name]), name


def test_every_exact_model_accepts_real_shape_variants_and_rejects_empty_object() -> None:
    for name, model in TOOL_RESULT_MODELS.items():
        for has_ok, core in TOOL_SUCCESS_SPECS[name]:
            payload = {field: _example_value(_FIELD_TYPES.get(field, Any)) for field in core}
            if has_ok:
                payload["ok"] = True
            assert model.model_validate(payload).model_dump() == payload
            if has_ok:
                payload["ok"] = False
                assert model.model_validate(payload).model_dump() == payload
        assert model.model_validate({"ok": False, "error_kind": "expected"}).model_dump()["error_kind"] == "expected"
        assert model.model_validate({"error": "legacy"}).model_dump()["error"] == "legacy"
        with pytest.raises(ValidationError):
            model.model_validate({})


def test_fastmcp_keeps_legacy_text_and_structured_content_without_null_injection() -> None:
    async def call() -> tuple[dict[str, Any], dict[str, Any]]:
        tool = mcp._tool_manager.get_tool("repo_info")
        result = await tool.run({}, convert_result=True)
        return json.loads(result[0][0].text), result[1]

    text, structured = anyio.run(call)
    assert text["project_root"] == structured["project_root"]
    assert "error_kind" not in structured
    assert "artifact" not in structured
    assert all(value is not None for value in (structured["project_root"], structured["config"]))


def test_status_bearing_command_and_batch_failures_keep_their_real_core_shape(monkeypatch) -> None:
    monkeypatch.setattr(server, "settings", replace(server.settings, command_policy_mode="full_repo"))
    async def calls() -> tuple[dict[str, Any], dict[str, Any]]:
        command = mcp._tool_manager.get_tool("run_command")
        batch = mcp._tool_manager.get_tool("batch_call")
        command_result = await command.run(
            {"command": 'sh -c "exit 3"', "parse_kind": "none"}, convert_result=True,
        )
        batch_result = await batch.run(
            {"calls": [{"tool": "not_a_tool", "args": {}}]}, convert_result=True,
        )
        return command_result[1], batch_result[1]

    command, batch = anyio.run(calls)
    assert command["ok"] is False
    assert command["exit_code"] == 3
    assert {"command", "stdout", "stderr", "timed_out", "log_id"} <= set(command)
    assert batch["ok"] is False
    assert batch["count"] == 1
    assert batch["results"][0]["ok"] is False


def test_real_tree_git_grep_and_diagnostics_variants_validate_through_fastmcp() -> None:
    async def calls() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        tree = await mcp._tool_manager.get_tool("tree").run({"path": ".", "depth": 0}, convert_result=True)
        grep = await mcp._tool_manager.get_tool("git_grep").run({"query": "chatrepo_mcp"}, convert_result=True)
        diagnostics = await mcp._tool_manager.get_tool("code_diagnostics").run(
            {"language": "python", "paths": ["python/src/chatrepo_mcp/result_models.py"]}, convert_result=True,
        )
        return tree[1], grep[1], diagnostics[1]

    tree, grep, diagnostics = anyio.run(calls)
    assert isinstance(tree["entries"], int)
    assert {"query", "results", "count", "truncated"} <= set(grep)
    if grep.get("polyrepo") is True:
        assert {"polyrepo", "repos_searched"} <= set(grep)
        assert isinstance(grep["repos_searched"], list)
        assert all(isinstance(repo, str) for repo in grep["repos_searched"])
    else:
        assert "repo" in grep
    assert {"language", "tool_used", "diagnostics", "missing_tools", "truncated", "output_truncated"} <= set(diagnostics)
    assert isinstance(diagnostics["tool_used"], list)


def test_tail_lines_preserves_real_trailing_lines_without_phantom_empty_line() -> None:
    assert _tail("one\ntwo\nthree\n", 2) == "two\nthree"
    assert _tail("one\ntwo\nthree", 2) == "two\nthree"
    assert _tail("one\ntwo\n", 0) == ""
    assert _tail("one\ntwo\n", None) == ""
