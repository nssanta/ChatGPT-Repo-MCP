from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from test_command_tools import make_settings

from chatrepo_mcp import git_tools, github_tools
from chatrepo_mcp.output_store import artifact_reference


def _settings(tmp_path: Path):
    return make_settings(tmp_path)


def test_require_gh_ready_returns_none_when_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2", "path": "gh"},
    )

    assert github_tools._require_gh_ready(_settings(Path("/tmp"))) is None


def test_run_gh_returns_no_remote_when_resolve_toplevel_raises(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"},
    )
    def _raise(_repo, _settings):
        raise git_tools.GitToolError("path is not inside a git repository: nope")

    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", _raise)

    result = github_tools._run_gh(["status"], _settings(tmp_path))

    assert result["ok"] is False
    assert result["error_kind"] == "no_github_remote"
    assert "not inside a git repository" in result["error"]


def test_run_gh_success_returns_structured_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2", "path": "gh"},
    )
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(
        github_tools,
        "run_bounded",
        lambda *args, **kwargs: github_tools.subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout="ok\n",
            stderr="",
        ),
    )

    result = github_tools._run_gh(["pr", "status"], _settings(tmp_path))

    assert result["ok"] is True
    assert result["stdout"] == "ok\n"


def test_run_gh_shares_inline_budget_and_marks_combined_truncation(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {
            "installed": True, "authenticated": True, "hint": "", "version": "gh 2", "path": "gh",
        },
    )
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(
        github_tools,
        "run_bounded",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="A" * 40_000 + "Z" * 40_000,
            stderr="B" * 40_000 + "Y" * 40_000,
            stdout_bytes=80_000,
            stderr_bytes=80_000,
            truncated=False,
            artifact=artifact_reference("gh-inline-budget", complete=True),
        ),
    )

    result = github_tools._run_gh(["pr", "status"], _settings(tmp_path))

    returned = len(result["stdout"].encode()) + len(result["stderr"].encode())
    assert result["ok"] is True
    assert returned <= 65_536
    assert result["output_truncated"] is True
    assert result["receipt"]["status"] == "partial"
    assert result["continuation"]["tool"] == "read_artifact"


def test_json_fail_closed_preserves_artifact_evidence() -> None:
    artifact = artifact_reference("gh-json-incomplete", complete=True, reason="inline_limit")
    result = {
        "ok": True,
        "stdout": "{\"partial\":",
        "output_truncated": True,
        "artifact": artifact,
        "continuation": artifact["continuation"],
        "receipt": artifact["receipt"],
    }

    data, error = github_tools._json_or_error(result, default={})

    assert data == {}
    assert error is not None
    assert error["error_kind"] == "gh_output_truncated"
    assert error["artifact"]["artifact_id"] == "gh-json-incomplete"
    assert error["continuation"]["tool"] == "read_artifact"
    assert error["receipt"]["reason"] == "inline_limit"


def test_gh_status_uninstalled_tool_is_structured(monkeypatch) -> None:
    settings = _settings(Path("/tmp"))
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {
            "installed": False,
            "authenticated": False,
            "hint": github_tools.GH_INSTALL_HINT,
            "version": None,
        },
    )

    result = github_tools.gh_status(settings)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_unavailable"
    assert result["install_hint"] == github_tools.GH_INSTALL_HINT


def test_gh_pr_create_dry_run_false_runs_and_parses_pr_number(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    calls: list[list[str]] = []

    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_branch_push_status",
        lambda settings, repo, branch=None: {"pushed": True, "branch": "feature", "upstream": "origin/feature", "ahead": 0},
    )

    def fake_run_gh(args, settings, *, repo=None):
        calls.append(args)
        return {"ok": True, "stdout": "https://github.com/team/repo/pull/42", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run_gh)

    result = github_tools.gh_pr_create(
        settings,
        "Title",
        "Body",
        repo="team/repo",
        base="main",
        head="feature",
        draft=True,
        dry_run=False,
        confirmed=True,
    )

    assert result["ok"] is True
    assert result["number"] == 42
    assert calls[0][:3] == ["pr", "create", "--title"]
    assert "--base" in calls[0]
    assert "--head" in calls[0]
    assert "--draft" in calls[0]


def test_gh_pr_create_push_status_error_returns_no_remote(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda _settings: None)

    def raise_git_error(_settings, _repo, _branch=None):
        raise git_tools.GitToolError("path is not inside a git repository")

    monkeypatch.setattr(github_tools, "_branch_push_status", raise_git_error)

    result = github_tools.gh_pr_create(settings, "Title", "Body")

    assert result["ok"] is False
    assert result["error_kind"] == "no_github_remote"


def test_gh_pr_list_uses_error_payload_when_command_fails(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": False, "error_kind": "gh_command_failed", "stdout": "", "stderr": "failed", "exit_code": 1},
    )

    result = github_tools.gh_pr_list(settings, repo="team/repo")

    assert result["ok"] is False
    assert result["error_kind"] == "gh_command_failed"


def test_gh_pr_list_guarded_blocked_tools(monkeypatch) -> None:
    settings = _settings(Path("/tmp"))
    settings = settings.__class__(**{**settings.__dict__, "github_tools_enabled": False})

    result = github_tools.gh_pr_list(settings)

    assert result["ok"] is False
    assert result["error_kind"] == "github_tools_disabled"


def test_gh_pr_view_no_comments_and_diff_success_path(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    calls: list[list[str]] = []

    def fake_run_gh(args, settings, *, repo=None):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return {"ok": True, "stdout": json.dumps({"number": 11, "title": "Title"}), "stderr": "", "exit_code": 0}
        return {"ok": True, "stdout": "diff content", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run_gh)

    result = github_tools.gh_pr_view(settings, 11, include_diff=True, include_comments=False)

    assert result["ok"] is True
    assert result["pr"]["title"] == "Title"
    assert result["diff"] == "diff content"
    assert calls[0] == ["pr", "view", "11", "--json", "number,title,body,state,url,headRefName,baseRefName,author,mergeable,statusCheckRollup,reviews"]


def test_gh_pr_view_direct_failures_returned(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": False, "error_kind": "gh_command_failed", "stdout": "", "stderr": "boom", "exit_code": 1},
    )

    result = github_tools.gh_pr_view(settings, 12)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_command_failed"


def test_gh_pr_comment_direct_success_returns_last_line_as_url(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "line1\nhttps://github.com/team/repo/pull/11#issuecomment-1", "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_pr_comment(settings, 11, "hello", confirmed=True)

    assert result["ok"] is True
    assert result["url"] == "https://github.com/team/repo/pull/11#issuecomment-1"


def test_gh_pr_merge_command_failed(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": False, "error_kind": "gh_command_failed", "stdout": "", "stderr": "failed", "exit_code": 1},
    )

    result = github_tools.gh_pr_merge(settings, 99, confirmed=True)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_command_failed"


def test_gh_checks_pr_number_invalid_json_keeps_response_as_raw(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "{not", "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_checks(settings, pr_number=21)

    assert result["ok"] is True
    assert result["checks"] == []
    assert result["raw"] == "{not"


def test_gh_checks_ref_invalid_json_is_structural_error(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_repo_owner_name", lambda settings, repo: ("team", "repo"))
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "{invalid", "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_checks(settings, ref="abc123")

    assert result["ok"] is False
    assert result["error_kind"] == "gh_bad_output"


def test_gh_run_view_autodetect_with_invalid_list_payload(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(
        github_tools.git_tools,
        "_run_git",
        lambda args, settings, cwd=None: "",
    )

    def fake_run_gh(args, settings, *, repo=None, timeout=None):
        if args[0:2] == ["run", "list"]:
            return {"ok": True, "stdout": "not-json", "stderr": "", "exit_code": 0}
        return {"ok": True, "stdout": json.dumps({"databaseId": 7}), "stderr": "", "exit_code": 0}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run_gh)

    result = github_tools.gh_run_view(settings)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_bad_output"


def test_gh_run_view_view_fails_after_resolving_run_id(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(github_tools.git_tools, "_run_git", lambda args, settings, cwd=None: "main")

    def fake_run_gh(args, settings, *, repo=None, timeout=None):
        if args[0:2] == ["run", "list"]:
            return {"ok": True, "stdout": json.dumps([{"databaseId": 42}]), "stderr": "", "exit_code": 0}
        return {"ok": False, "error_kind": "gh_command_failed", "stdout": "", "stderr": "no run", "exit_code": 1}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run_gh)

    result = github_tools.gh_run_view(settings)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_command_failed"


def test_gh_run_rerun_handles_command_failure(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": False, "error_kind": "gh_command_failed", "stdout": "", "stderr": "boom", "exit_code": 1},
    )

    result = github_tools.gh_run_rerun(settings, "77", confirmed=True)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_command_failed"


def test_gh_issue_list_command_failure_is_returned(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": False, "error_kind": "gh_command_failed", "stdout": "", "stderr": "boom", "exit_code": 1},
    )

    result = github_tools.gh_issue_list(settings)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_command_failed"


def test_gh_issue_view_invalid_json_returns_error(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "not-json", "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_issue_view(settings, 3)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_bad_output"


def test_gh_pr_merge_not_ready_blocks_without_running_gh(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        github_tools,
        "_require_gh_ready",
        lambda _settings: {"ok": False, "error_kind": "gh_unavailable", "install_hint": github_tools.GH_INSTALL_HINT},
    )
    result = github_tools.gh_pr_merge(settings, 3, confirmed=True)
    assert result["error_kind"] == "gh_unavailable"


def test_gh_run_rerun_not_ready_blocks(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        github_tools,
        "_require_gh_ready",
        lambda _settings: {"ok": False, "error_kind": "gh_not_authenticated", "install_hint": "run `gh auth login` to authenticate"},
    )
    result = github_tools.gh_run_rerun(settings, "5", confirmed=True)
    assert result["error_kind"] == "gh_not_authenticated"
