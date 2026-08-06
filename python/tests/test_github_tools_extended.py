from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from chatrepo_mcp import github_tools


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        github_tools_enabled=True,
        max_diff_bytes=10_000,
        gh_timeout=10,
        full_access=False,
        confirmation_granted=lambda confirmed: confirmed is True,
    )


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_gh_available_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr(github_tools.shutil, "which", lambda *_args, **_kwargs: None)

    result = github_tools._gh_available()

    assert result["installed"] is False
    assert result["authenticated"] is False
    assert result["version"] is None



def test_gh_available_authenticated(monkeypatch) -> None:
    def which_binary(name: str) -> str:
        if name == "gh":
            return "/usr/bin/gh"
        return "/bin/true"

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["gh", "--version"]:
            return _FakeCompleted(0, "gh version 2.0.0\n")
        if cmd == ["gh", "auth", "status"]:
            return _FakeCompleted(0)
        raise AssertionError(f"unexpected gh call: {cmd}")

    monkeypatch.setattr(github_tools.shutil, "which", which_binary)
    monkeypatch.setattr(github_tools, "run_bounded", fake_run)

    result = github_tools._gh_available()

    assert result["installed"] is True
    assert result["authenticated"] is True
    assert result["version"] == "gh version 2.0.0"



def test_require_gh_ready_blocks_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {"installed": False, "authenticated": False, "hint": github_tools.GH_INSTALL_HINT, "version": None},
    )

    status = github_tools._require_gh_ready()
    assert status["ok"] is False
    assert status["error_kind"] == "gh_unavailable"



def test_run_gh_translates_no_remote_to_structured_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"},
    )
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)

    def fake_run(cmd, *args, **kwargs):
        return _FakeCompleted(1, stdout="", stderr="could not determine repository")

    monkeypatch.setattr(github_tools, "run_bounded", fake_run)

    result = github_tools._run_gh(["pr", "view"], _settings(), repo=None)

    assert result["ok"] is False
    assert result["error_kind"] == "no_github_remote"



def test_run_gh_reports_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"},
    )
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)

    def raise_timeout(*args, **kwargs):
        raise github_tools.subprocess.TimeoutExpired(["gh", "run"], 10)

    monkeypatch.setattr(github_tools, "run_bounded", raise_timeout)

    result = github_tools._run_gh(["run", "list"], _settings(), repo=None)

    assert result["ok"] is False
    assert result["error_kind"] == "gh_timeout"



def test_json_or_error_returns_bad_output_when_invalid() -> None:
    payload = "{not valid json"
    parsed, err = github_tools._json_or_error({"stdout": payload, "ok": True}, default=[{"ok": True}])
    assert parsed == [{"ok": True}]
    assert err is not None
    assert err["error_kind"] == "gh_bad_output"



def test_github_pr_create_blocks_when_head_not_pushed(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_branch_push_status", lambda _settings, _repo, _branch=None: {"pushed": False, "branch": "feature", "ahead": 1, "upstream": None, "reason": "1 not pushed"})

    result = github_tools.gh_pr_create(settings, "title", "body", dry_run=True)

    assert result["ok"] is False
    assert result["error_kind"] == "branch_not_pushed"



def test_gh_status_includes_rate_limit_payload(monkeypatch) -> None:
    payload = {"resources": {"core": {"limit": 5000, "remaining": 4998, "reset": 1710000000}}}

    monkeypatch.setattr(github_tools, "_gh_available", lambda _settings: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"})
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None, timeout=None: {"ok": True, "stdout": json.dumps(payload), "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_status(_settings())

    assert result["ok"] is True
    assert result["rate_limit"]["limit"] == 5000
    assert result["rate_limit"]["remaining"] == 4998



def test_gh_checks_reports_missing_argument(tmp_path: Path) -> None:
    result = github_tools.gh_checks(_settings(), repo=str(tmp_path))

    assert result["ok"] is False
    assert result["error_kind"] == "missing_argument"



def test_gh_issue_list_rejects_invalid_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "{invalid", "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_issue_list(_settings(), repo=str(tmp_path))

    assert result["ok"] is False
    assert result["error_kind"] == "gh_bad_output"


def test_run_gh_marks_unauthenticated_as_structured_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"},
    )
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(
        github_tools,
        "run_bounded",
        lambda *args, **kwargs: _FakeCompleted(1, stdout="", stderr="You are not logged in"),
    )

    result = github_tools._run_gh(["api", "user"], _settings())

    assert result["ok"] is False
    assert result["error_kind"] == "gh_not_authenticated"


def test_run_gh_marks_command_failed_otherwise(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        github_tools,
        "_gh_available",
        lambda _settings: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"},
    )
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(
        github_tools,
        "run_bounded",
        lambda *args, **kwargs: _FakeCompleted(1, stdout="boom", stderr="exit code 1"),
    )

    result = github_tools._run_gh(["api", "repos"], _settings())

    assert result["ok"] is False
    assert result["error_kind"] == "gh_command_failed"


def test_gh_checks_falls_back_to_text_when_json_not_available(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    calls: list[str] = []

    def fake_run(args, settings, *, repo=None):
        calls.append(args[0])
        if "--json" in args:
            return {"ok": False, "stdout": "", "stderr": "unknown flag: --json", "exit_code": 1}
        return {"ok": True, "stdout": "CHECK\n", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run)

    result = github_tools.gh_checks(_settings(), pr_number=12)

    assert result["ok"] is True
    assert result["checks"] == []
    assert result["raw"] == "CHECK\n"


def test_gh_run_view_returns_not_found_when_no_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(
        github_tools.git_tools,
        "_run_git",
        lambda args, settings, cwd=None: "main",
    )

    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": "[]", "stderr": "", "exit_code": 0}
        if args[0:2] == ["run", "list"]
        else {"ok": True, "stdout": "", "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_run_view(_settings())

    assert result["ok"] is False
    assert result["error_kind"] == "run_not_found"


def test_gh_status_disabled_tools_returns_structured_error(monkeypatch) -> None:
    monkeypatch.setattr(
        github_tools,
        "_guard",
        lambda _settings: {"ok": False, "error_kind": "github_tools_disabled", "error": "disabled"},
    )
    result = github_tools.gh_status(_settings())

    assert result["ok"] is False
    assert result["error_kind"] == "github_tools_disabled"
