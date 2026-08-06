"""Exact, additive structured output schemas for all public MCP tools."""

from __future__ import annotations

from functools import reduce
from operator import or_
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, RootModel, TypeAdapter, create_model


class _AdditiveObject(BaseModel):
    """Lets current and future result keys pass through unchanged."""

    model_config = ConfigDict(extra="allow")


class _TypedError(_AdditiveObject):
    ok: Literal[False]
    error_kind: str


class _LegacyError(_AdditiveObject):
    error: str | dict[str, Any] | list[Any]


# Every tuple is one real success shape: (has_ok_discriminator, required core
# fields).  Multiple tuples encode genuine runtime variants, not optional
# kitchen-sink envelopes.
ToolSuccessSpec = tuple[bool, tuple[str, ...]]
TOOL_SUCCESS_SPECS: dict[str, tuple[ToolSuccessSpec, ...]] = {
    "repo_info": ((False, ("project_root", "exists", "is_dir", "config")),),
    "list_dir": ((False, ("path", "entries", "truncated")),),
    "tree": ((False, ("path", "tree", "entries", "max_entries")), (True, ("path", "tree", "entries", "truncated"))),
    "read_text_file": (
        (False, ("path", "start_line", "end_line", "content", "line_count", "sha256")),
        (True, ("path", "start_line", "end_line", "content", "total_lines", "sha256", "truncated")),
    ),
    "read_multiple_files": ((False, ("files",)),),
    "file_metadata": ((False, ("path", "exists", "type", "name", "suffix")),),
    "find_files": ((False, ("pattern", "path", "matches", "count")), (True, ("pattern", "matches", "count", "truncated"))),
    "search_text": (
        (False, ("query", "path", "results", "count", "mode")),
        (True, ("job_id", "status", "log_id", "query", "path", "mode")),
        (True, ("query", "matches", "count", "truncated", "engine")),
    ),
    "symbol_search": ((False, ("symbol", "results", "count")), (True, ("query", "matches", "count", "truncated", "engine"))),
    "recent_changes": ((False, ("path", "paths", "files", "count")),),
    "todo_scan": ((False, ("path", "results", "count")), (True, ("query", "matches", "count", "truncated", "engine"))),
    "dependency_map": ((False, ("manifests", "count")), (True, ("path", "stack", "dependencies"))),
    "git_status": ((False, ("repo", "status", "stderr", "truncated")), (True, ("repo", "output", "truncated", "artifact", "receipt"))),
    "git_diff": ((False, ("repo", "diff", "stderr", "truncated")),),
    "git_log": ((False, ("repo", "commits", "count")),),
    "git_show": ((False, ("repo", "revision", "content", "stderr", "truncated")),),
    "git_branches": ((False, ("repo", "branches")),),
    "git_blame": ((False, ("repo", "path", "blame", "stderr", "truncated")),),
    "git_grep": (
        (False, ("repo", "query", "results", "count", "truncated")),
        (False, ("polyrepo", "repos_searched", "query", "results", "count", "truncated")),
        (True, ("query", "matches", "count")),
    ),
    "list_repos": ((False, ("repos",)),),
    "doctor": ((False, ("project_root", "tool_count", "checks")), (True, ("implementation", "tool_count", "capabilities"))),
    "smoke_all": ((True, ("project_root", "checks")), (True, ("checks", "tool_count"))),
    "context_bootstrap": ((False, ("files", "count", "workspace")), (True, ("repo", "repos"))),
    "batch_call": ((True, ("execution", "results", "count")),),
    "write_text_file": ((True, ("path", "changed", "dry_run")),),
    "replace_text_in_file": ((True, ("path", "changed", "dry_run")),),
    "insert_text_in_file": ((True, ("path", "changed", "dry_run")),),
    "delete_text_in_file": ((True, ("path", "changed", "dry_run")),),
    "create_text_file": ((True, ("path", "changed", "dry_run")),),
    "move_path": ((True, ("path", "destination_path", "changed", "dry_run")),),
    "delete_path": ((True, ("path", "changed", "dry_run")),),
    "ensure_directory": ((True, ("path", "changed", "dry_run")),),
    "batch_edit_files": ((True, ("results", "operations_total")),),
    "apply_change_set": ((True, ("results", "operations_total")),),
    "replace_lines": ((True, ("path", "changed", "dry_run")),),
    "insert_before_line": ((True, ("path", "changed", "dry_run")),),
    "insert_after_line": ((True, ("path", "changed", "dry_run")),),
    "insert_before_heading": ((True, ("path", "changed", "dry_run")),),
    "insert_after_heading": ((True, ("path", "changed", "dry_run")),),
    "append_to_file": ((True, ("path", "changed", "dry_run")),),
    "apply_patch": ((True, ("changed", "dry_run", "applied", "repo", "changed_files")),),
    "update_current_mission": ((True, ("path", "changed", "dry_run")),),
    "run_command": ((True, ("command", "exit_code", "stdout", "stderr", "timed_out", "log_id")),),
    "run_commands": ((True, ("results", "count")),),
    "run_test_preset": ((True, ("preset",)),),
    "list_test_presets": ((False, ("presets", "count")),),
    "run_quality_gate": ((True, ("checks", "count")),),
    "quality_gate_and_commit": ((True, ("committed", "gate")),),
    "scan_new_policy_violations": ((True, ("violations", "count")),),
    "command_policy_check": ((False, ("command", "allowed")), (True, ("allowed", "normalized", "policy_mode"))),
    "read_artifact": ((True, ("artifact_id", "payload", "byte_range", "eof")),),
    "get_command_log": ((True, ("log_id", "stream", "content", "line_count")), (True, ("log_id", "stream", "content", "total_lines", "truncated"))),
    "summarize_command_log": ((True, ("log_id", "summary")),),
    "git_worktree_guard": ((True, ("repo",)),),
    "start_command_job": ((True, ("job_id", "status", "log_id")),),
    "get_command_job": ((True, ("job_id", "status")),),
    "get_job_status": ((True, ("job_id", "status")),),
    "list_command_jobs": ((True, ("jobs", "count")),),
    "cancel_command_job": ((True, ("job_id", "status", "cancelled")),),
    "start_terminal_session": ((True, ("session_id", "status", "artifact")),),
    "read_terminal_session": ((True, ("session_id", "data", "next_cursor", "eof")),),
    "write_terminal_session": ((True, ("session_id", "bytes_written")),),
    "resize_terminal_session": ((True, ("session_id", "status", "cols", "rows")),),
    "close_terminal_session": ((True, ("session_id", "status", "closed")),),
    "list_terminal_sessions": ((True, ("sessions", "count")),),
    "git_commit": ((True, ("repo", "dry_run", "paths")),),
    "git_switch_branch": ((True, ("repo", "branch")),),
    "git_create_branch": ((True, ("repo", "branch")),),
    "git_add": ((True, ("repo", "staged", "skipped_blocked", "dry_run")),),
    "git_restore": ((True, ("repo", "restored", "staged")),),
    "git_stash": ((True, ("repo", "action")),),
    "git_fetch": ((True, ("repo",)), (True, ("results",))),
    "git_pull": ((True, ("repo", "remote", "branch")),),
    "git_push": ((True, ("repo", "remote", "branch")),),
    "git_merge": ((True, ("repo", "branch")),),
    "git_revert": ((True, ("repo", "commit")),),
    "git_reset": ((True, ("repo", "revision", "mode")),),
    "git_worktree_add": ((True, ("repo", "path", "branch")),),
    "prepare_task_worktree": ((True, ("repo", "path", "branch")),),
    "git_worktree_list": ((True, ("worktrees",)),),
    "git_worktree_remove": ((True, ("repo", "removed")),),
    "gh_status": ((True, ("installed", "authenticated")),),
    "gh_pr_create": ((True, ("url", "number")), (True, ("dry_run", "would_run"))),
    "gh_pr_list": ((True, ("prs", "count")),),
    "gh_pr_view": ((True, ("pr",)),),
    "gh_pr_comment": ((True, ("url",)),),
    "gh_pr_merge": ((True, ("merged",)),),
    "gh_checks": ((True, ("checks",)),),
    "gh_run_view": ((True, ("run",)),),
    "gh_run_rerun": ((True, ("rerun", "run_id")),),
    "gh_issue_list": ((True, ("issues", "count")),),
    "gh_issue_view": ((True, ("issue",)),),
    "code_diagnostics": ((True, ("language", "tool_used", "diagnostics", "missing_tools", "truncated", "output_truncated")),),
    "symbol_definition": ((True, ("symbol", "definitions", "count", "engine")),),
    "document_symbols": ((True, ("path", "symbols", "engine")),),
    "workspace_symbols": ((True, ("query", "symbols", "count", "engine")),),
}


_FIELD_TYPES: dict[str, Any] = {
    "project_root": str, "exists": bool, "is_dir": bool, "config": dict[str, Any], "path": str,
    "entries": list[Any] | int, "truncated": bool, "tree": str, "max_entries": int, "start_line": int,
    "end_line": int, "content": str, "line_count": int, "total_lines": int, "sha256": str, "files": list[Any], "type": str,
    "name": str, "suffix": str, "pattern": str, "matches": list[Any], "count": int, "query": str,
    "results": list[Any], "job_id": str, "status": str, "log_id": str, "mode": str, "symbol": str,
    "paths": list[str], "manifests": dict[str, Any], "repo": str, "polyrepo": bool,
    "repos_searched": list[str], "stderr": str, "diff": str,
    "commits": list[Any], "revision": str, "branches": list[Any], "blame": str, "repos": list[Any], "tool_count": int,
    "checks": list[Any] | dict[str, Any], "workspace": list[Any], "execution": str, "changed": bool,
    "dry_run": bool, "destination_path": str, "operations_total": int, "applied": bool,
    "changed_files": list[Any], "command": str, "exit_code": int, "stdout": str, "timed_out": bool,
    "preset": str, "result": dict[str, Any], "committed": bool, "gate": dict[str, Any],
    "violations": list[Any], "allowed": bool, "artifact_id": str, "payload": dict[str, Any],
    "byte_range": dict[str, int], "eof": bool, "stream": str, "summary": dict[str, Any] | str,
    "jobs": list[Any], "cancelled": bool, "session_id": str, "artifact": dict[str, Any], "data": str,
    "next_cursor": int, "bytes_written": int, "cols": int, "rows": int, "closed": bool,
    "sessions": list[Any], "branch": str, "staged": list[Any] | bool, "skipped_blocked": list[Any],
    "restored": list[Any] | bool, "action": str, "remote": str, "commit": str,
    "worktrees": list[Any], "removed": str, "installed": bool, "authenticated": bool, "url": str,
    "number": int, "would_run": str, "prs": list[Any], "pr": dict[str, Any],
    "merged": bool, "run": dict[str, Any], "rerun": bool, "run_id": str, "issues": list[Any],
    "issue": dict[str, Any], "diagnostics": list[Any], "engine": str, "definitions": list[Any],
    "symbols": list[Any],
    "language": str, "tool_used": list[Any], "missing_tools": list[Any], "output_truncated": bool,
    "stack": list[str] | str, "dependencies": dict[str, Any] | list[Any], "implementation": str,
    "capabilities": dict[str, Any], "normalized": str, "policy_mode": str, "output": str,
    "receipt": dict[str, Any],
}


def _success_model(tool_name: str, index: int, has_ok: bool, core: tuple[str, ...]) -> type[_AdditiveObject]:
    fields: dict[str, Any] = {field: (_FIELD_TYPES.get(field, Any) | None, ...) for field in core}
    if has_ok:
        # Status-bearing tools use false both for normal domain outcomes
        # (non-zero command, partial batch) and for early typed errors.
        fields["ok"] = (bool, ...)
    return create_model(
        f"{''.join(part.title() for part in tool_name.split('_'))}Success{index}",
        __base__=_AdditiveObject,
        __module__=__name__,
        **fields,
    )


def _summary_properties(specs: tuple[ToolSuccessSpec, ...]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "ok": {"type": "boolean"},
        "error_kind": {"type": "string"},
        "error": {},
    }
    for _, core in specs:
        for field in core:
            properties.setdefault(field, TypeAdapter(_FIELD_TYPES.get(field, Any)).json_schema())
    return properties


def _result_model(tool_name: str, specs: tuple[ToolSuccessSpec, ...]) -> type[RootModel[Any]]:
    success_models = [_success_model(tool_name, index, has_ok, core) for index, (has_ok, core) in enumerate(specs)]
    union = reduce(or_, [*success_models, _TypedError, _LegacyError])

    def schema_hook(cls: type[RootModel[Any]], core_schema: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core_schema)
        # RootModel's generated anyOf branches hold exact required-only
        # variants.  The outer object stays MCP-compatible and additive.
        schema["type"] = "object"
        schema["additionalProperties"] = True
        schema["properties"] = _summary_properties(specs)
        return schema

    root_base = RootModel[union]  # type: ignore[valid-type]
    return type(
        f"{''.join(part.title() for part in tool_name.split('_'))}Result",
        (root_base,),
        {"__module__": __name__, "__get_pydantic_json_schema__": classmethod(schema_hook)},
    )


TOOL_RESULT_MODELS: dict[str, type[RootModel[Any]]] = {
    name: _result_model(name, specs) for name, specs in TOOL_SUCCESS_SPECS.items()
}


def result_model_for_tool(name: str) -> type[RootModel[Any]]:
    try:
        return TOOL_RESULT_MODELS[name]
    except KeyError as exc:
        raise RuntimeError(f"missing structured result model for MCP tool {name!r}") from exc
