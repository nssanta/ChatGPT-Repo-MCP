import anyio

from chatrepo_mcp.server import mcp


def _tools_by_name() -> dict:
    async def collect() -> dict:
        tools = await mcp.list_tools()
        return {tool.name: tool for tool in tools}

    return anyio.run(collect)


def test_problem_tools_have_clear_argument_descriptions() -> None:
    tools = _tools_by_name()

    for tool_name, arg_names in {
        "batch_call": ("calls",),
        "batch_edit_files": ("operations", "atomic", "dry_run"),
        "apply_change_set": ("operations", "atomic", "dry_run", "name"),
        "delete_text_in_file": ("path", "find", "start_line", "end_line", "expected_sha256", "dry_run"),
        "insert_text_in_file": ("path", "anchor", "position", "content", "expected_sha256", "dry_run"),
        "run_command": ("command", "timeout_ms", "cwd", "env", "tail_lines"),
        "run_commands": ("commands", "stop_on_failure", "timeout_ms", "tail_lines"),
        "run_test_preset": ("preset", "timeout_ms", "tail_lines", "background"),
        "list_test_presets": (),
        "run_quality_gate": ("checks", "name", "stop_on_failure"),
        "quality_gate_and_commit": ("checks", "commit", "name", "require_clean_after_commit"),
        "scan_new_policy_violations": ("base_ref", "paths", "rules"),
        "command_policy_check": ("command",),
        "get_command_log": ("log_id", "stream", "start_line", "end_line", "grep"),
        "summarize_command_log": ("log_id", "parser"),
        "git_worktree_guard": ("allowed_dirty_paths", "require_branch", "require_not_rebasing"),
        "start_command_job": ("command", "timeout_ms", "cwd", "env", "tail_lines", "concurrency_key", "on_conflict"),
        "get_job_status": ("job_id",),
        "update_current_mission": ("section_title", "content", "position", "preset", "chunks", "dry_run"),
    }.items():
        properties = tools[tool_name].inputSchema["properties"]
        for arg_name in arg_names:
            assert properties[arg_name].get("description"), f"{tool_name}.{arg_name} lacks description"


def test_choice_arguments_expose_enums() -> None:
    tools = _tools_by_name()

    insert_position = tools["insert_text_in_file"].inputSchema["properties"]["position"]
    test_preset = tools["run_test_preset"].inputSchema["properties"]["preset"]

    assert insert_position["enum"] == ["before", "after"]
    assert "built-ins" in test_preset["description"]


def test_command_tools_are_not_marked_destructive() -> None:
    tools = _tools_by_name()

    for tool_name in ("run_command", "run_commands", "run_test_preset", "start_command_job", "run_quality_gate"):
        annotations = tools[tool_name].annotations
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is False
        assert annotations.openWorldHint is True


def test_quality_gate_commit_is_marked_write_action() -> None:
    annotations = _tools_by_name()["quality_gate_and_commit"].annotations

    assert annotations.readOnlyHint is False
    assert annotations.destructiveHint is True
