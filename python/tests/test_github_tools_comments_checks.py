from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from chatrepo_mcp import github_tools
from chatrepo_mcp.command_tools import ConfirmationRequiredError
from test_command_tools import make_settings


class _Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        github_tools_enabled=True,
        max_diff_bytes=10_000,
        gh_timeout=10,
        confirmation_granted=lambda confirmed: confirmed is True,
    )


def test_gh_available_handles_version_probe_exception(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["gh", "--version"]:
            raise OSError("no version")
        raise OSError("auth failed")

    monkeypatch.setattr(github_tools.subprocess, "run", fake_run)

    status = github_tools._gh_available()

    assert status["installed"] is True
    assert status["authenticated"] is False
    assert status["version"] is None


def test_run_gh_returns_unavailable_on_subprocess_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "_gh_available", lambda: {"installed": True, "authenticated": True, "hint": "", "version": "gh 2"})
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(github_tools.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))

    result = github_tools._run_gh(["pr", "list"], _settings())

    assert result["ok"] is False
    assert result["error_kind"] == "gh_unavailable"
    assert result.get("error") == "boom"


def test_gh_pr_merge_rejects_invalid_method(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)

    result = github_tools.gh_pr_merge(_settings(), 12, method="invalid", confirmed=True)

    assert result["ok"] is False
    assert result["error_kind"] == "invalid_method"


def test_gh_pr_comment_reply_to_success_paths_reply_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda: None)
    monkeypatch.setattr(
        github_tools,
        "_repo_owner_name",
        lambda settings, repo: ("octo", "repo"),
    )
    monkeypatch.setattr(
        github_tools,
        "_run_gh",
        lambda args, settings, *, repo=None: {"ok": True, "stdout": json.dumps({"html_url": "https://github.com/octo/repo/pull/12#issuecomment-7"}), "stderr": "", "exit_code": 0},
    )

    result = github_tools.gh_pr_comment(make_settings(tmp_path), 12, "LGTM", reply_to=88, repo="octo/repo", confirmed=True)

    assert result["ok"] is True
    assert result["url"] == "https://github.com/octo/repo/pull/12#issuecomment-7"


def test_gh_pr_comment_reply_to_without_owner_returns_no_remote(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda: None)
    monkeypatch.setattr(
        github_tools,
        "_repo_owner_name",
        lambda settings, repo: None,
    )

    result = github_tools.gh_pr_comment(_settings(), 7, "Needs follow-up", reply_to=12, confirmed=True)

    assert result["ok"] is False
    assert result["error_kind"] == "no_github_remote"


def test_gh_pr_comment_blocks_without_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda: None)

    try:
        github_tools.gh_pr_comment(_settings(), 12, "Blocked", confirmed=False)
        assert False, "expected ConfirmationRequiredError"
    except ConfirmationRequiredError as exc:
        assert "gh_pr_comment posts a real comment" in str(exc)


def test_gh_checks_ref_path_uses_check_runs_api(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "_guard", lambda _settings: None)
    monkeypatch.setattr(github_tools, "_repo_owner_name", lambda settings, repo: ("octo", "repo"))
    payload = {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success", "html_url": "http://example"}]}

    def fake_run_gh(args, settings, *, repo=None):
        assert args[0:2] == ["api", "repos/octo/repo/commits/abc123/check-runs"]
        return {"ok": True, "stdout": json.dumps(payload), "stderr": "", "exit_code": 0}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run_gh)

    result = github_tools.gh_checks(_settings(), ref="abc123")

    assert result["ok"] is True
    assert result["checks"][0]["status"] == "completed"
    assert result["checks"][0]["conclusion"] == "success"
