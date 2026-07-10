from __future__ import annotations

import json
from pathlib import Path

import pytest

from chatrepo_mcp import git_tools, github_tools
from chatrepo_mcp.command_tools import ConfirmationRequiredError
from test_command_tools import make_settings


def _settings(tmp_path: Path):
    return make_settings(tmp_path)


def test_github_guard_disabled_is_consistent() -> None:
    settings = _settings(Path("/tmp"))
    settings = settings.__class__(**{**settings.__dict__, "github_tools_enabled": False})

    disabled = github_tools._guard(settings)

    assert disabled == {"ok": False, "error_kind": "github_tools_disabled"}


def test_require_gh_ready_reports_not_authenticated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda: {"installed": True, "authenticated": False, "hint": "run `gh auth login` to authenticate", "version": "gh 2"},
    )

    status = github_tools._require_gh_ready()

    assert status["ok"] is False
    assert status["error_kind"] == "gh_not_authenticated"


def test_gh_available_with_empty_version_and_auth_failure(monkeypatch) -> None:
    monkeypatch.setattr(github_tools.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, *args, **kwargs):
        if cmd == ["gh", "--version"]:
            return _Completed(1, "", "")
        if cmd == ["gh", "auth", "status"]:
            return _Completed(1, "", "not logged in")
        raise AssertionError(cmd)

    monkeypatch.setattr(github_tools.subprocess, "run", fake_run)

    status = github_tools._gh_available()

    assert status["version"] is None
    assert status["authenticated"] is False


def test_gh_available_with_auth_failure_detected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(github_tools.shutil, "which", lambda name: "/usr/bin/gh")

    class _Proc:
        def __init__(self, code: int, out: str = "", err: str = "") -> None:
            self.returncode = code
            self.stdout = out
            self.stderr = err

    def fake_run(cmd, *args, **kwargs):
        if cmd == ["gh", "--version"]:
            return _Proc(0, "gh 2.0.0\n")
        if cmd == ["gh", "auth", "status"]:
            return _Proc(1, "", "Could not authenticate")
        raise AssertionError(cmd)

    monkeypatch.setattr(github_tools.subprocess, "run", fake_run)

    status = github_tools._gh_available()

    assert status["authenticated"] is False


def test_run_gh_not_installed_and_no_remote_from_resolve_error(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda: {"installed": False, "authenticated": False, "hint": github_tools.GH_INSTALL_HINT, "version": None},
    )

    result = github_tools._run_gh(["status"], settings)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_unavailable"


def test_run_gh_detects_not_authenticated_marker(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_gh_available", lambda: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"})
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] != "gh":
            raise AssertionError(cmd)
        return _Completed(1, "", "Not logged in with gh")

    monkeypatch.setattr(github_tools.subprocess, "run", fake_run)

    result = github_tools._run_gh(["status"], settings)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_not_authenticated"


def test_repo_owner_name_rejects_missing_or_invalid_payload(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    monkeypatch.setattr(github_tools, "_run_gh", lambda args, settings, *, repo=None: {"ok": False, "stdout": "", "stderr": "", "exit_code": 1})
    assert github_tools._repo_owner_name(settings, None) is None

    monkeypatch.setattr(github_tools, "_run_gh", lambda args, settings, *, repo=None: {"ok": True, "stdout": "{invalid", "stderr": "", "exit_code": 0})
    assert github_tools._repo_owner_name(settings, None) is None

    monkeypatch.setattr(github_tools, "_run_gh", lambda args, settings, *, repo=None: {"ok": True, "stdout": json.dumps({"owner": {}, "name": ""}), "stderr": "", "exit_code": 0})
    assert github_tools._repo_owner_name(settings, None) is None


def test_repo_owner_name_success_with_payload(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": json.dumps({"owner": {"login": "octo"}, "name": "repo"}), "stderr": "", "exit_code": 0},
    )

    owner_name = github_tools._repo_owner_name(settings, None)

    assert owner_name == ("octo", "repo")


def test_branch_push_status_no_upstream_and_head_not_set(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)

    def run_git(args, settings, cwd=None):
        if args == ["branch", "--show-current"]:
            return "feature\n"
        raise git_tools.GitToolError("no upstream")

    monkeypatch.setattr(github_tools.git_tools, "_run_git", run_git)

    result = github_tools._branch_push_status(settings, None)

    assert result["pushed"] is False
    assert result["branch"] == "feature"
    assert result["upstream"] is None
    assert "no upstream configured" in result["reason"]


def test_branch_push_status_not_ahead_and_compare_error(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)

    def run_git(args, settings, cwd=None):
        calls.append(" ".join(args))
        if args == ["branch", "--show-current"]:
            return "feature\n"
        if "rev-parse" in args[0]:
            return "origin/feature\n"
        if "rev-list" in args[0]:
            return "x y"
        raise git_tools.GitToolError(f"unexpected: {args}")

    monkeypatch.setattr(github_tools.git_tools, "_run_git", run_git)

    result = github_tools._branch_push_status(settings, None, branch="feature")

    assert calls == [
        "rev-parse --abbrev-ref --symbolic-full-name feature@{u}",
        "rev-list --left-right --count origin/feature...feature",
    ]
    assert result["pushed"] is False
    assert result["reason"] == "could not compare branch with upstream"


def test_branch_push_status_ahead_and_cleared(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)

    def run_git(args, settings, cwd=None):
        if args == ["branch", "--show-current"]:
            return "feature\n"
        if args[0] == "rev-parse":
            return "origin/feature\n"
        if args[0] == "rev-list":
            return "1\t2\n"
        raise git_tools.GitToolError(f"unexpected: {args}")

    monkeypatch.setattr(github_tools.git_tools, "_run_git", run_git)

    ahead = github_tools._branch_push_status(settings, "repo", branch="feature")
    assert ahead["ahead"] == 2
    assert ahead["pushed"] is False
    assert ahead["reason"] == "2 commit(s) not pushed to origin/feature"

    def run_git_zero(args, settings, cwd=None):
        if args == ["branch", "--show-current"]:
            return "feature\n"
        if args[0] == "rev-parse":
            return "origin/feature\n"
        return "0\t0\n"

    monkeypatch.setattr(github_tools.git_tools, "_run_git", run_git_zero)
    pushed = github_tools._branch_push_status(settings, "repo", branch="feature")
    assert pushed["pushed"] is True
    assert pushed["reason"] is None


def test_gh_status_auth_error_and_empty_rate_limit_core(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda: {"installed": True, "authenticated": False, "hint": "run `gh auth login` to authenticate", "version": "gh 2"},
    )
    result = github_tools.gh_status(settings)
    assert result["ok"] is False
    assert result["error_kind"] == "gh_not_authenticated"

    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"},
    )
    monkeypatch.setattr(github_tools, "_run_gh", lambda args, settings, *, repo=None, timeout=None: {"ok": True, "stdout": json.dumps({}), "stderr": "", "exit_code": 0})
    result = github_tools.gh_status(settings)
    assert result["ok"] is True
    assert "rate_limit" not in result


def test_gh_pr_list_returns_invalid_json_and_success(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "[1, 2", "stderr": "", "exit_code": 0},
    )
    bad = github_tools.gh_pr_list(settings)
    assert bad["ok"] is False
    assert bad["error_kind"] == "gh_bad_output"

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": json.dumps([{"number": 7}, {"number": 8}]), "stderr": "", "exit_code": 0},
    )
    result = github_tools.gh_pr_list(settings, repo=".", state="closed", limit=3)
    assert result["ok"] is True
    assert result["count"] == 2
    assert result["prs"][1]["number"] == 8


def test_gh_pr_comment_direct_and_reply_error_modes(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda: None)

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": False, "error_kind": "gh_command_failed", "stdout": "", "stderr": "boom", "exit_code": 1},
    )
    denied = github_tools.gh_pr_comment(settings, 8, "hi", confirmed=True)
    assert denied["ok"] is False
    assert denied["error_kind"] == "gh_command_failed"

    monkeypatch.setattr(github_tools, "_repo_owner_name", lambda settings, repo: ("octo", "repo"))
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "{invalid", "stderr": "", "exit_code": 0},
    )
    bad_reply = github_tools.gh_pr_comment(settings, 8, "bad", reply_to=2, confirmed=True)
    assert bad_reply["ok"] is True
    assert bad_reply["url"] is None


def test_gh_pr_comment_requires_ready_and_confirmation(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_require_gh_ready",
        lambda: {"ok": False, "error_kind": "gh_not_authenticated", "install_hint": "run `gh auth login` to authenticate"},
    )
    not_ready = github_tools.gh_pr_comment(settings, 9, "nope", confirmed=True)
    assert not_ready["error_kind"] == "gh_not_authenticated"

    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda: None)
    with pytest.raises(ConfirmationRequiredError):
        github_tools.gh_pr_comment(settings, 9, "nope", confirmed=False)


def test_gh_pr_merge_success_and_confirmation_path(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda: None)
    monkeypatch.setattr(github_tools, "_run_gh", lambda args, settings, *, repo=None: {"ok": True, "stdout": "merged", "stderr": "", "exit_code": 0})

    merged = github_tools.gh_pr_merge(settings, 1, method="merge", confirmed=True)
    assert merged["ok"] is True
    assert merged["merged"] is True

    with pytest.raises(ConfirmationRequiredError):
        github_tools.gh_pr_merge(settings, 1, method="merge", confirmed=False)


def test_gh_checks_pr_and_ref_paths(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": json.dumps([{"name": "ci", "state": "completed", "bucket": "success", "link": "https://x"}]), "stderr": "", "exit_code": 0},
    )
    checks = github_tools.gh_checks(settings, pr_number=3, repo="repo")
    assert checks["ok"] is True
    assert checks["checks"][0]["conclusion"] == "success"

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {
            "ok": True,
            "stdout": json.dumps(
                {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "failure", "html_url": "u"}]}
            ),
            "stderr": "",
            "exit_code": 0,
        },
    )
    monkeypatch.setattr(github_tools, "_repo_owner_name", lambda settings, repo: ("octo", "repo"))
    ref_checks = github_tools.gh_checks(settings, ref="abc", repo="repo")
    assert ref_checks["ok"] is True
    assert ref_checks["checks"][0]["conclusion"] == "failure"

    monkeypatch.setattr(github_tools, "_repo_owner_name", lambda settings, repo: None)
    no_remote = github_tools.gh_checks(settings, ref="abc", repo="repo")
    assert no_remote["error_kind"] == "no_github_remote"


def test_gh_run_view_with_run_id_handles_failed_logs_error_and_invalid_json(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)

    def fake_run_gh(args, settings, *, repo=None, timeout=None):
        if args[:2] == ["run", "view"] and "--log-failed" in args:
            return {"ok": False, "error_kind": "gh_command_failed", "stdout": "", "stderr": "logs missing", "exit_code": 1}
        return {"ok": True, "stdout": json.dumps({"databaseId": 7, "status": "completed"}), "stderr": "", "exit_code": 0}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run_gh)
    failed_logs = github_tools.gh_run_view(settings, run_id="55", failed_only=True, log_tail=2)
    assert failed_logs["failed_logs_error"]["error_kind"] == "gh_command_failed"

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None, timeout=None: {"ok": True, "stdout": "{not", "stderr": "", "exit_code": 0},
    )
    invalid_json = github_tools.gh_run_view(settings, run_id="77")
    assert invalid_json["ok"] is False
    assert invalid_json["error_kind"] == "gh_bad_output"


def test_gh_run_view_run_id_skips_failed_logs_when_disabled(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None, timeout=None: {"ok": True, "stdout": json.dumps({"databaseId": 12, "status": "queued"}), "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_run_view(settings, run_id="42", failed_only=False)
    assert result["ok"] is True
    assert result["run"]["databaseId"] == 12
    assert "failed_logs" not in result


def test_gh_run_rerun_success_and_confirmation_required(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "rerun started", "stderr": "", "exit_code": 0},
    )
    result = github_tools.gh_run_rerun(settings, "10", failed_only=False, confirmed=True)
    assert result["ok"] is True
    assert result["run_id"] == "10"

    with pytest.raises(ConfirmationRequiredError):
        github_tools.gh_run_rerun(settings, "10", failed_only=False, confirmed=False)


def test_gh_issue_list_and_view_success_paths(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": json.dumps([{"number": 1}, {"number": 2}]), "stderr": "", "exit_code": 0},
    )
    listed = github_tools.gh_issue_list(settings, repo="repo", limit=3)
    assert listed["count"] == 2

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": json.dumps({"number": 1, "title": "Hi"}), "stderr": "", "exit_code": 0},
    )
    viewed = github_tools.gh_issue_view(settings, 1, repo="repo")
    assert viewed["issue"]["title"] == "Hi"


class _Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
