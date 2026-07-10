from __future__ import annotations

import subprocess
from pathlib import Path

from chatrepo_mcp.git_tools import GitToolError
from chatrepo_mcp.git_workflow_tools import (
    _conflicted_paths,
    git_add,
    git_fetch,
    git_merge,
    git_pull,
    git_push,
    git_stash,
    git_switch_branch,
    git_worktree_remove,
)
from test_command_tools import make_settings


def test_conflicted_paths_ignores_short_lines_and_collects_refs() -> None:
    assert _conflicted_paths("x\nAA good.txt\n") == ["good.txt"]


def test_git_switch_branch_refuses_start_point_without_create(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        if args == ["branch", "--show-current"]:
            return "main\n"
        if args == ["status", "--porcelain"]:
            return ""
        raise GitToolError(f"unexpected call: {args}")

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    try:
        git_switch_branch(settings, "feature/x", start_point="HEAD")
        assert False, "expected GitToolError for start_point without create=True"
    except GitToolError as exc:
        assert "start_point is only valid together with create=true" in str(exc)


def test_git_switch_branch_restores_stash_when_switch_fails(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    calls: list[list[str]] = []

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        calls.append(list(args))
        if args == ["branch", "--show-current"]:
            return "main\n"
        if args == ["status", "--porcelain"]:
            return " M README.md\n"
        if args == ["stash", "push", "-u", "-m", "chatrepo-mcp: autostash before switching to feature/recovery"]:
            return "Saved"
        if args == ["switch", "feature/recovery"]:
            raise GitToolError("cannot switch due local issues")
        if args == ["stash", "pop"]:
            return ""
        raise GitToolError(f"unexpected git call: {args}")

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    try:
        git_switch_branch(settings, "feature/recovery", stash_first=True)
        assert False, "expected GitToolError because switch fails"
    except GitToolError:
        assert True

    assert ["stash", "push", "-u", "-m", "chatrepo-mcp: autostash before switching to feature/recovery"] in calls
    assert ["stash", "pop"] in calls


def test_git_add_skips_secret_and_binary_paths(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    calls: list[tuple[list[str], dict]] = []

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        calls.append((list(args), {"cwd": str(cwd) if cwd else None}))
        return ""

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    result = git_add(settings, [".env", ".env.secret"], dry_run=True)

    assert result["ok"] is True
    assert result["staged"] == []
    assert ".env" in result["skipped_blocked"]
    assert ".env.secret" in result["skipped_blocked"]
    assert calls == []


def test_git_add_dry_run_parses_quoted_candidates(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        if args[:2] == ["add", "--dry-run"]:
            return "add 'space name.py'\nadd 'src/main.py'\n"
        return ""

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    result = git_add(settings, ["space name.py", "src/main.py"])

    assert result["dry_run"] is True
    assert result["staged"] == ["space name.py", "src/main.py"]


def test_git_stash_action_validation_rejects_unknown_action(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    try:
        git_stash(settings, action="noop")
        assert False, "expected GitToolError for unknown action"
    except GitToolError as exc:
        assert "action must be one of" in str(exc)


def test_git_stash_list_and_show_return_expected_structure(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        if args == ["stash", "list"]:
            return "stash@{0}: WIP on main: abc\nstash@{1}: WIP on dev: def\n"
        if args == ["stash", "show", "-p", "stash@{0}"]:
            return "diff --git a.txt b.txt\n+hello\n"
        raise GitToolError(f"unexpected call: {args}")

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    list_result = git_stash(settings, action="list")
    show_result = git_stash(settings, action="show")

    assert list_result["ok"] is True
    assert list_result["action"] == "list"
    assert len(list_result["stashes"]) == 2
    assert show_result["ok"] is True
    assert show_result["action"] == "show"
    assert show_result["stash_ref"] == "stash@{0}"
    assert "WIP on main" not in show_result["diff"]


def test_git_fetch_reports_added_and_updated_refs(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    phase = {"state": "before"}

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        if args == ["for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/origin"]:
            if phase["state"] == "before":
                phase["state"] = "after"
                return "refs/remotes/origin/main abc123\nrefs/remotes/origin/dev deadbeef\n"
            return "refs/remotes/origin/main 000000\n"
        if args == ["fetch", "origin"]:
            return ""
        raise GitToolError(f"unexpected call: {args}")

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    result = git_fetch(settings, remote="origin")

    assert result["ok"] is True
    assert result["remote"] == "origin"
    refs = {item["ref"]: item["before"] for item in result["updated_refs"]}
    assert refs["refs/remotes/origin/main"] == "abc123"
    assert refs["refs/remotes/origin/dev"] == "deadbeef"


def test_git_pull_success_includes_changed_files(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    state = {"step": 0}

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        if args == ["branch", "--show-current"]:
            return "main\n"
        if args == ["rev-parse", "HEAD"]:
            if state["step"] == 0:
                state["step"] = 1
                return "111\n"
            return "222\n"
        if args == ["pull", "--ff-only", "origin", "main"]:
            return ""
        if args == ["diff", "--name-only", "111", "222"]:
            return "a.py\nsrc/b.py\n"
        raise GitToolError(f"unexpected call: {args}")

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    result = git_pull(settings, confirmed=True)

    assert result["ok"] is True
    assert result["before_sha"] == "111"
    assert result["after_sha"] == "222"
    assert result["files_changed"] == ["a.py", "src/b.py"]


def test_git_pull_non_conflict_failure_rethrows(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    calls: list[list[str]] = []

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        calls.append(list(args))
        if args == ["branch", "--show-current"]:
            return "main\n"
        if args == ["rev-parse", "HEAD"]:
            return "aaa"
        if args == ["status", "--porcelain"]:
            return ""
        if args == ["pull", "--ff-only", "origin", "main"]:
            raise GitToolError("fatal: unable to access remote")
        raise GitToolError(f"unexpected call: {args}")

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    try:
        git_pull(settings, confirmed=True)
        assert False, "expected GitToolError for non-conflict pull failure"
    except GitToolError as exc:
        assert "unable to access remote" in str(exc)

    assert ["pull", "--ff-only", "origin", "main"] in calls


def test_git_push_force_with_lease_is_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "allow_force_push": False})

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        if args == ["branch", "--show-current"]:
            return "feature/test\n"
        if args == ["rev-parse", "refs/remotes/origin/feature/test"]:
            return "originsha"
        return ""

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    try:
        git_push(settings, force_with_lease=True, confirmed=True)
        assert False, "expected GitToolError when ALLOW_FORCE_PUSH is false"
    except GitToolError as exc:
        assert "force_with_lease is disabled" in str(exc)


def test_git_push_timeout_is_reported(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "protected_branches": (), "git_network_timeout": 1})

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        if args == ["branch", "--show-current"]:
            return "release\n"
        if args == ["rev-parse", "HEAD"]:
            return "rev123"
        if args == ["rev-parse", "refs/remotes/origin/release"]:
            return "remote987"
        return ""

    def fake_subprocess_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools.subprocess.run", fake_subprocess_run)

    result = git_push(settings, dry_run=False, confirmed=True, branch="release")

    assert result["ok"] is False
    assert result["error_kind"] == "push_timeout"


def test_git_merge_abort_calls_merge_abort(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    calls: list[list[str]] = []

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_run_git(args: list[str], settings, cwd=None, max_bytes=None, network=False) -> str:
        calls.append(list(args))
        if args == ["merge", "--abort"]:
            return ""
        raise GitToolError(f"unexpected call: {args}")

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", fake_run_git)

    result = git_merge(settings, "whatever", abort=True)

    assert result["ok"] is True
    assert result["aborted"] is True
    assert ["merge", "--abort"] in calls


def test_git_worktree_remove_rejects_path_escape(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    def fake_resolve_repo_toplevel(repo: str | None, settings) -> Path:
        return tmp_path

    def fake_confirmed(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._resolve_repo_toplevel", fake_resolve_repo_toplevel)
    monkeypatch.setattr("chatrepo_mcp.git_workflow_tools._run_git", lambda *args, **kwargs: "")
    monkeypatch.setattr("chatrepo_mcp.config.Settings.confirmation_granted", fake_confirmed)

    try:
        git_worktree_remove(settings, "../outside")
        assert False, "expected GitToolError for path escape"
    except GitToolError as exc:
        assert "escapes allowed workspace roots" in str(exc)
