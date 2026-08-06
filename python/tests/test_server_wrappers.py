from __future__ import annotations

import asyncio

from test_command_tools import make_settings

from chatrepo_mcp import server
from chatrepo_mcp.command_tools import CommandPolicyError, ConfirmationRequiredError, GitCommitError
from chatrepo_mcp.git_tools import GitToolError
from chatrepo_mcp.resource_profile import ResourceBusyError


def test_batch_dispatch_routes_read_and_blocks_mutation_without_dry_run(monkeypatch) -> None:
    def fake_list_dir(*, settings, path=".", include_hidden=True, limit=200, **kwargs):
        return {"ok": True, "path": path, "entries": [], "limit": limit, "include_hidden": include_hidden}

    monkeypatch.setattr(server, "list_dir", fake_list_dir)

    read_result = server._batch_dispatch("list_dir", {"path": "services", "limit": 12})

    assert read_result["ok"] is True
    assert read_result["path"] == "services"
    assert read_result["limit"] == 12

    try:
        server._batch_dispatch("replace_text_in_file", {"path": "x.txt", "find": "a", "replace": "b", "dry_run": False})
        assert False, "expected ValueError from write guard"
    except ValueError as exc:
        assert "batch_call only allows write tools when dry_run=true" in str(exc)


def test_batch_dispatch_allows_mutation_for_dry_run_only(monkeypatch) -> None:
    def fake_replace(**kwargs):
        assert kwargs["dry_run"] is True
        return {"ok": True, "dry_run": True}

    monkeypatch.setattr(server, "replace_text_in_file", fake_replace)

    result = server._batch_dispatch("replace_text_in_file", {"path": "x.txt", "find": "a", "replace": "b", "dry_run": True})

    assert result == {"ok": True, "dry_run": True}


def test_batch_dispatch_rejects_unknown_tool() -> None:
    try:
        server._batch_dispatch("not_real", {})
        assert False, "expected ValueError for unknown tool"
    except ValueError as exc:
        assert "not allowed for batch_call" in str(exc)


def test_structural_result_maps_confirmation_and_git_errors(monkeypatch) -> None:
    monkeypatch.setattr(server, "git_add", lambda *args, **kwargs: (_ for _ in ()).throw(ConfirmationRequiredError("confirm")))
    confirmation = server._structural_result(server.git_add, object(), ["x"], repo=None)
    assert confirmation == {"ok": False, "error_kind": "confirmation_required", "message": "confirm"}

    monkeypatch.setattr(server, "git_add", lambda *args, **kwargs: (_ for _ in ()).throw(GitToolError("git broken")))
    git_error = server._structural_result(server.git_add, object(), ["x"], repo=None)
    assert git_error == {"ok": False, "error_kind": "git_error", "message": "git broken"}


def test_command_result_maps_command_errors_and_timeout_like_paths(monkeypatch) -> None:
    recorded = {}

    def fake_run_command(*, command, settings, timeout_ms=None, cwd=None, env=None, max_output_chars=None, tail_lines=200, confirmed=False, parse_kind="auto"):
        recorded["confirmed"] = confirmed
        recorded["command"] = command
        raise CommandPolicyError("bad command")

    monkeypatch.setattr(server, "run_command", fake_run_command)

    result = server._command_result("cat /tmp")

    assert result["ok"] is False
    assert result["error_kind"] == "command_not_allowed"
    assert recorded["command"] == "cat /tmp"
    assert recorded["confirmed"] is False

    def fake_run_command(*, command, settings, timeout_ms=None, cwd=None, env=None, max_output_chars=None, tail_lines=200, confirmed=False, parse_kind="auto"):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "run_command", fake_run_command)
    generic = server._command_result("anything")
    assert generic["ok"] is False
    assert generic["error_kind"] == "command_failed"
    assert "boom" in generic["error"]

    monkeypatch.setattr(
        server, "run_command",
        lambda **kwargs: (_ for _ in ()).throw(ResourceBusyError(3)),
    )
    busy = server._command_result("anything")
    assert busy["error_kind"] == "resource_busy"
    assert busy["capacity"] == 3
    assert "Retry" in busy["retry_hint"]


def test_run_command_tool_maps_command_policy_error(monkeypatch) -> None:
    called = {}

    def fake_run_command(*, command, settings, timeout_ms=None, cwd=None, env=None, max_output_chars=None, tail_lines=200, confirmed=False, parse_kind="auto"):
        called["confirmed"] = confirmed
        raise CommandPolicyError("policy block")

    monkeypatch.setattr(server, "run_command", fake_run_command)

    result = server.run_command_tool("git status", parse_kind="auto", confirmed=False)

    assert result["ok"] is False
    assert result["error_kind"] == "command_not_allowed"
    assert called["confirmed"] is False


def test_batch_call_tool_collects_success_and_error_items(monkeypatch) -> None:
    def fake_dispatch(tool, args):
        if tool == "list_dir":
            return {"ok": True, "tool": tool, **args}
        if tool == "replace_text_in_file":
            raise ValueError("batch_call only allows write tools when dry_run=true")
        raise ValueError("bad args")

    monkeypatch.setattr(server, "_batch_dispatch", fake_dispatch)

    result = server.batch_call_tool(
        [
            {"tool": "list_dir", "args": {"path": "docs", "limit": 3}},
            {"tool": "replace_text_in_file", "args": {"path": "x"}},
            {"tool": "list_dir", "args": "bad"},
        ]
    )

    assert result["count"] == 3
    assert result["results"][0]["ok"] is True
    assert result["results"][0]["tool"] == "list_dir"
    assert result["results"][1]["ok"] is False
    assert "error" in result["results"][1]
    assert "batch_call only allows write tools when dry_run=true" in result["results"][1]["error"]
    assert result["results"][2]["ok"] is False


def test_config_info_exposes_detected_and_applied_resource_truth(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "settings",
        server.settings.__class__(
            **{
                **server.settings.__dict__,
                "resource_profile": "auto",
                "resource_profile_applied": "medium",
                "resource_detected_memory_bytes": 8 * 1024**3,
                "resource_buffer_bytes": 32 * 1024**2,
                "max_heavy_operations": 4,
                "persist_full_output": True,
            }
        ),
    )
    info = server._write_config_info()
    assert info["resource_profile"] == "auto"
    assert info["resource_profile_applied"] == "medium"
    assert info["resource_detected_memory_bytes"] == 8 * 1024**3
    assert info["resource_buffer_bytes"] == 32 * 1024**2
    assert info["resource_buffer_enforced"] is False
    assert info["resource_buffer_semantics"] == "diagnostic_estimate_only"
    assert info["max_heavy_operations"] == 4
    assert info["persist_full_output"] is True


def test_search_surface_returns_typed_resource_busy(monkeypatch) -> None:
    monkeypatch.setattr(
        server, "search_text", lambda **kwargs: (_ for _ in ()).throw(ResourceBusyError(2)),
    )
    result = server.search_text_tool("needle")
    assert result["error_kind"] == "resource_busy"
    assert result["capacity"] == 2
    assert "Retry" in result["retry_hint"]


def test_wrapper_returns_structural_error_for_empty_git_add_paths() -> None:
    result = server.git_add_tool(paths=[], repo=None, dry_run=True)

    assert result["ok"] is False
    assert result["error_kind"] == "git_error"


def test_structural_result_respects_confirmation_flow_for_run_command_tool(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConfirmationRequiredError("run requires confirmation")),
    )

    result = server.run_command_tool("git push", parse_kind="auto", confirmed=True)

    assert result["ok"] is False
    assert result["error_kind"] == "confirmation_required"


def test_tool_wrappers_delegate_to_underlying_implementations(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(server, "settings", settings)

    observed: list[str] = []

    def spy(name: str):
        def _fake(*_args, **_kwargs):
            observed.append(name)
            return {"ok": True, "name": name}

        return _fake

    monkeypatches: list[str] = [
        "_repo_info_with_git",
        "list_dir",
        "tree",
        "read_text_file",
        "read_multiple_files",
        "file_metadata",
        "find_files",
        "search_text",
        "symbol_search",
        "recent_changes",
        "todo_scan",
        "dependency_map",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "git_branches",
        "git_blame",
        "git_grep",
        "list_repos",
        "write_text_file",
        "replace_text_in_file",
        "insert_text_in_file",
        "delete_text_in_file",
        "create_text_file",
        "move_path",
        "delete_path",
        "ensure_directory",
        "batch_edit_files",
        "apply_change_set",
        "replace_lines",
        "insert_before_line",
        "insert_after_line",
        "insert_before_heading",
        "insert_after_heading",
        "append_to_file",
        "apply_patch_diff",
        "update_current_mission",
        "_command_result",
        "run_commands",
        "run_test_preset",
        "list_test_presets",
        "run_quality_gate",
        "quality_gate_and_commit",
        "scan_new_policy_violations",
        "command_policy_check",
        "get_command_log",
        "summarize_command_log",
        "git_worktree_guard",
        "start_command_job",
        "get_command_job",
        "get_job_status",
        "cancel_command_job",
        "git_commit",
        "git_switch_branch",
        "git_create_branch",
        "git_add",
        "git_restore",
        "git_stash",
        "git_fetch",
        "git_pull",
        "git_push",
        "git_merge",
        "git_revert",
        "git_reset",
        "git_worktree_add",
        "git_worktree_list",
        "git_worktree_remove",
        "gh_status",
        "gh_pr_create",
        "gh_pr_list",
        "gh_pr_view",
        "gh_pr_comment",
        "gh_pr_merge",
        "gh_checks",
        "gh_run_view",
        "gh_run_rerun",
        "gh_issue_list",
        "gh_issue_view",
        "code_diagnostics",
        "symbol_definition",
        "document_symbols",
        "workspace_symbols",
    ]

    for target in monkeypatches:
        monkeypatch.setattr(server, target, spy(target))

    wrappers = [
        (server.repo_info_tool, {}),
        (server.list_dir_tool, {"path": "src", "include_hidden": False, "limit": 3}),
        (server.tree_tool, {"path": "src", "depth": 2, "include_hidden": True}),
        (server.read_text_file_tool, {"path": "x.txt"}),
        (server.read_multiple_files_tool, {"paths": ["x.txt"]}),
        (server.file_metadata_tool, {"path": "x.txt"}),
        (server.find_files_tool, {"pattern": "*.py", "path": ".", "include_hidden": True, "limit": 10}),
        (server.search_text_tool, {"query": "needle", "path": ".", "limit": 10}),
        (server.symbol_search_tool, {"symbol": "MySymbol", "path": ".", "limit": 10}),
        (server.recent_changes_tool, {"path": ".", "limit": 10}),
        (server.todo_scan_tool, {"path": ".", "limit": 10}),
        (server.dependency_map_tool, {"path": "."}),
        (server.git_status_tool, {"short": True}),
        (server.git_diff_tool, {"context_lines": 1}),
        (server.git_log_tool, {"limit": 3}),
        (server.git_show_tool, {"revision": "HEAD"}),
        (server.git_branches_tool, {"all_branches": True}),
        (server.git_blame_tool, {"path": "x.txt"}),
        (server.git_grep_tool, {"query": "needle"}),
        (server.list_repos_tool, {}),
        (server.write_text_file_tool, {"path": "x.txt", "content": "v"}),
        (server.replace_text_in_file_tool, {"path": "x.txt", "find": "a", "replace": "b"}),
        (server.insert_text_in_file_tool, {"path": "x.txt", "anchor": "a", "position": "before", "content": "x"}),
        (server.delete_text_in_file_tool, {"path": "x.txt", "find": "x"}),
        (server.create_text_file_tool, {"path": "x.txt", "content": "x"}),
        (server.move_path_tool, {"source_path": "x.txt", "destination_path": "y.txt"}),
        (server.delete_path_tool, {"path": "y.txt"}),
        (server.ensure_directory_tool, {"path": "tmp"}),
        (server.batch_edit_files_tool, {"operations": [{"op": "ensure_directory", "path": "tmp"}]}),
        (server.apply_change_set_tool, {"operations": [{"op": "write", "path": "x.txt", "content": "v"}]}),
        (server.replace_lines_tool, {"path": "x.txt", "start_line": 1, "end_line": 1, "replacement": "r"}),
        (server.insert_before_line_tool, {"path": "x.txt", "line": 1, "content": "r"}),
        (server.insert_after_line_tool, {"path": "x.txt", "line": 1, "content": "r"}),
        (server.insert_before_heading_tool, {"path": "x.txt", "heading": "# H", "content": "x"}),
        (server.insert_after_heading_tool, {"path": "x.txt", "heading": "# H", "content": "x"}),
        (server.append_to_file_tool, {"path": "x.txt", "content": "x"}),
        (server.apply_patch_tool, {"patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n"}),
        (server.update_current_mission_tool, {"section_title": "t", "content": "x"}),
        (server.run_command_tool, {"command": "git status --short"}),
        (server.run_commands_tool, {"commands": ["git status --short"]}),
        (server.run_test_preset_tool, {"preset": "test"}),
        (server.list_test_presets_tool, {}),
        (server.run_quality_gate_tool, {"checks": [{"preset": "test"}]}),
        (server.quality_gate_and_commit_tool, {"checks": [{"preset": "test"}], "commit": {"message": "m", "paths": ["x.txt"]}}),
        (server.scan_new_policy_violations_tool, {"base_ref": "HEAD"}),
        (server.command_policy_check_tool, {"command": "git status"}),
        (server.get_command_log_tool, {"log_id": "1"}),
        (server.summarize_command_log_tool, {"log_id": "1"}),
        (server.git_worktree_guard_tool, {}),
        (server.start_command_job_tool, {"command": "sleep 1"}),
        (server.get_command_job_tool, {"job_id": "1"}),
        (server.get_job_status_tool, {"job_id": "1"}),
        (server.cancel_command_job_tool, {"job_id": "1"}),
        (server.git_commit_tool, {"message": "m", "paths": ["x.txt"]}),
        (server.git_switch_branch_tool, {"branch": "main"}),
        (server.git_create_branch_tool, {"branch": "feature"}),
        (server.git_add_tool, {"paths": ["x.txt"]}),
        (server.git_restore_tool, {"paths": ["x.txt"]}),
        (server.git_stash_tool, {}),
        (server.git_fetch_tool, {}),
        (server.git_pull_tool, {}),
        (server.git_push_tool, {}),
        (server.git_merge_tool, {"branch": "main"}),
        (server.git_revert_tool, {"revision": "HEAD"}),
        (server.git_reset_tool, {"target": "HEAD~1"}),
        (server.git_worktree_add_tool, {"branch": "feature"}),
        (server.git_worktree_list_tool, {}),
        (server.git_worktree_remove_tool, {"worktree_path": ".chatrepo-worktrees/x"}),
        (server.gh_status_tool, {}),
        (server.gh_pr_create_tool, {"title": "t", "body": "b"}),
        (server.gh_pr_list_tool, {}),
        (server.gh_pr_view_tool, {"number": 1}),
        (server.gh_pr_comment_tool, {"number": 1, "body": "b"}),
        (server.gh_pr_merge_tool, {"number": 1}),
        (server.gh_checks_tool, {"pr_number": 1}),
        (server.gh_run_view_tool, {}),
        (server.gh_run_rerun_tool, {"run_id": "1"}),
        (server.gh_issue_list_tool, {}),
        (server.gh_issue_view_tool, {"number": 1}),
        (server.code_diagnostics_tool, {}),
        (server.symbol_definition_tool, {"symbol": "s"}),
        (server.document_symbols_tool, {"path": "x.txt"}),
        (server.workspace_symbols_tool, {"query": "q"}),
    ]

    for wrapper, kwargs in wrappers:
        result = wrapper(**kwargs)
        assert result["ok"] is True

    assert len(observed) == len(monkeypatches)


def test_wrapper_error_paths_are_structured(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(server, "settings", settings)

    monkeypatch.setattr(
        server,
        "write_text_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("generic bad write payload")),
    )
    written = server.write_text_file_tool(path="x.txt", content="x", create_if_missing=True, dry_run=False)
    assert written["ok"] is False
    assert written["error_kind"] == "validation_error"

    monkeypatch.setattr(
        server,
        "git_commit",
        lambda **_kwargs: (_ for _ in ()).throw(GitCommitError("commit blocked")),
    )
    commit = server.git_commit_tool(message="x", paths=["x.txt"], repo=None)
    assert commit["ok"] is False
    assert commit["error_kind"] == "git_commit_rejected"

    monkeypatch.setattr(server, "_capability_matrix", lambda: {"git": {"found": True, "path": "/bin/git"}})
    monkeypatch.setattr(server, "_repo_info_with_git", lambda repo=None: {"project_root": str(tmp_path), "git": {"status": "ok"}})
    monkeypatch.setattr(
        server,
        "_batch_dispatch",
        lambda tool, args=None: (_ for _ in ()).throw(RuntimeError("unexpected"))
        if tool == "read_text_file" and (args or {}).get("path") == ".env"
        else {"ok": True},
    )
    monkeypatch.setattr(server, "_mission_context_candidates", lambda: [".chatrepo/mcp.yml"])
    monkeypatch.setattr(server, "_first_existing_repo_file", lambda candidates: "README.md")
    monkeypatch.setattr(server, "_search_probe_token", lambda: "x")
    monkeypatch.setattr(server, "list_workspace_repos", lambda settings: [])
    status = server.doctor_tool()
    assert status["checks"]["repo_info"]["ok"] is True
    assert status["checks"]["blocked_policy"]["ok"] is True
    assert status["checks"]["git_status"]["ok"] is True


def test_tool_names_prefers_fastmcp_and_falls_back_on_manager_error(monkeypatch) -> None:
    default = server._tool_names()
    assert "repo_info" in default

    class FakeTool:
        def __init__(self, name: str):
            self.name = name

    class FakeNamespace:
        def __init__(self, names: list[str]):
            self._names = names

        def list_tools(self):
            return [FakeTool(name) for name in self._names]

    fake_manager = FakeNamespace(["b", "a"])
    monkeypatch.setattr(server.mcp, "_tool_manager", fake_manager)
    assert server._tool_names() == ["a", "b"]

    class BrokenManager:
        def list_tools(self):
            raise RuntimeError("disabled")

    monkeypatch.setattr(server.mcp, "_tool_manager", BrokenManager())
    fallback = server._tool_names()
    assert "repo_info" in fallback


def test_server_helpers_cover_security_and_dry_probe_paths(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(
        **{
            **settings.__dict__,
            "access_mode": "full",
            "command_policy_mode": "unrestricted",
            "filesystem_unrestricted": True,
            "blocked_globs": (".secret",),
            "mcp_auth_mode": "bearer",
            "mcp_bearer_token": "token",
        }
    )
    monkeypatch.setattr(server, "settings", settings)

    verifier = server.StaticBearerVerifier()

    async def check() -> None:
        ok = await verifier.verify_token("token")
        assert ok is not None
        assert ok.client_id == "chatgpt"

        wrong = await verifier.verify_token("bad")
        assert wrong is None

    asyncio.run(check())

    settings_none = settings.__class__(**{**settings.__dict__, "mcp_auth_mode": "none"})
    monkeypatch.setattr(server, "settings", settings_none)

    no_auth = server.StaticBearerVerifier().verify_token("abc")
    assert asyncio.run(no_auth).client_id == "no-auth"


def test_batch_call_error_paths_and_doctor_profiles(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(server, "settings", settings)

    with_statement = [
        {"tool": "list_dir", "args": {"path": ".", "limit": 1}},
        {"tool": 42, "args": {"path": "x"}},
        {"tool": "not_a_tool"},
        {"tool": "read_text_file", "args": {"path": "x"}},
    ]
    called = []
    monkeypatch.setattr(server, "_batch_dispatch", lambda tool, args=None: called.append((tool, args)) or {"ok": True, "tool": tool})

    result = server.batch_call_tool(with_statement)
    assert result["count"] == 4
    assert any(not item["ok"] for item in result["results"])

    # Explicit cap check
    try:
        server.batch_call_tool([{"tool": "list_dir", "args": {}} for _ in range(11)])
        assert False, "expected limit error"
    except ValueError as exc:
        assert "too many calls" in str(exc)


def test_doctor_tool_supports_mission_and_workspace_skips(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "_capability_matrix", lambda: {"ok": True})
    monkeypatch.setattr(server, "_repo_info_with_git", lambda repo=None: {"project_root": str(tmp_path), "git": {"status": "ok"}})
    monkeypatch.setattr(server, "_batch_dispatch", lambda tool, args=None: {"ok": True, "tool": tool, "args": args})

    # Mission file path exists
    (tmp_path / "notes.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(server, "_mission_context_candidates", lambda: ["notes.md"])
    monkeypatch.setattr(server, "_first_existing_repo_file", lambda candidates: "notes.md")

    status = server.doctor_tool()
    assert status["checks"]["mission_context"]["ok"] is True
    assert status["checks"]["search_text"]["ok"] is True
    assert status["checks"]["symbol_search"]["ok"] is True

    # Mission missing => skipped branch
    monkeypatch.setattr(server, "_first_existing_repo_file", lambda candidates: None)
    status_skipped = server.doctor_tool()
    assert status_skipped["checks"]["mission_context"]["status"] == "skipped"


def test_smoke_all_covers_skipped_blocks_and_blocked_policy(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(server, "settings", settings)
    monkeypatch.setattr(server, "_search_probe_token", lambda: "probe")
    monkeypatch.setattr(server, "_first_existing_repo_file", lambda candidates: None)
    monkeypatch.setattr(server, "_first_root_markdown_file", lambda: None)
    monkeypatch.setattr(
        server,
        "_batch_dispatch",
        lambda tool, args=None: {"ok": True, "entries": [{"name": "README.md"}], "count": 1} if tool == "list_dir" else {"ok": True},
    )
    monkeypatch.setattr(server, "list_test_presets", lambda _settings, path=None: {"ok": True, "presets": []})
    monkeypatch.setattr(server, "scan_new_policy_violations", lambda _settings, **_kwargs: {"ok": True})

    status = server.smoke_all_tool()
    assert status["ok"] is False
    keys = [item["name"] for item in status["checks"]]
    assert "mission_context" in keys
    assert "write_dry_run" in keys
    assert any(item["name"] == "blocked_policy" and item["ok"] is False for item in status["checks"])
    assert any(item.get("status") == "skipped" and item["name"] == "mission_context" for item in status["checks"])


def test_context_bootstrap_returns_missing_and_workspace_errors(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(server, "settings", settings)

    profile = type("P", (), {"mission": {"current": "notes.md", "memory": "memory.md"}})()
    monkeypatch.setattr(server, "load_repo_profile", lambda _: profile)

    def fake_read_text_file(*, path, settings):
        raise ValueError("not a file") if path == "notes.md" else {"path": path, "missing": True}

    monkeypatch.setattr(server, "read_text_file", fake_read_text_file)
    monkeypatch.setattr(server, "list_workspace_repos", lambda settings: [1, 2, 3])

    result = server.context_bootstrap_tool()
    assert result["count"] >= 5
    assert len(result["workspace"]) == 3
    assert any(entry.get("missing") is True and entry.get("path") == "notes.md" for entry in result["files"])


def test_wrapper_exception_mapping_for_structural_helpers(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(server, "settings", settings)

    monkeypatch.setattr(server, "run_test_preset", lambda *_, **__: (_ for _ in ()).throw(CommandPolicyError("blocked")))
    assert server.run_test_preset_tool("test")["error_kind"] == "command_not_allowed"

    monkeypatch.setattr(server, "run_quality_gate", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert server.run_quality_gate_tool([{"preset": "x"}])["error_kind"] == "quality_gate_failed"

    monkeypatch.setattr(server, "quality_gate_and_commit", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert server.quality_gate_and_commit_tool([{"preset": "x"}], {"message": "m", "paths": []})["error_kind"] == "quality_gate_commit_failed"

    monkeypatch.setattr(server, "scan_new_policy_violations", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert server.scan_new_policy_violations_tool()["error_kind"] == "policy_scan_failed"

    assert server.get_command_log_tool("id", stream="stderr", start_line=1, end_line=1, grep="x")["error_kind"] == "command_log_error"
    assert server.summarize_command_log_tool("id", parser="auto")["error_kind"] == "command_log_error"
