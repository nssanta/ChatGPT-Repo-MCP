from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from chatrepo_mcp import github_tools


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        github_tools_enabled=True,
        max_diff_bytes=10_000,
        gh_timeout=30,
        full_access=False,
        confirmation_granted=lambda confirmed: confirmed is True,
    )


def test_gh_pr_view_uses_supported_json_fields(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, settings, *, repo=None, timeout=None):
        calls.append(args)
        return {"ok": True, "stdout": json.dumps({"number": 7, "reviews": []})}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run)

    result = github_tools.gh_pr_view(_settings(), 7)

    assert result["ok"] is True
    fields = calls[0][calls[0].index("--json") + 1]
    assert "reviews" in fields
    assert "reviewThreads" not in fields


def test_gh_run_view_resolves_latest_run_non_interactively(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(github_tools.git_tools, "_resolve_repo_toplevel", lambda repo, settings: tmp_path)
    monkeypatch.setattr(
        github_tools.git_tools,
        "_run_git",
        lambda args, settings, cwd=None: "feature\n",
    )

    def fake_run(args, settings, *, repo=None, timeout=None):
        calls.append(args)
        if args[:2] == ["run", "list"]:
            return {"ok": True, "stdout": json.dumps([{"databaseId": 123}])}
        if "--log-failed" in args:
            return {"ok": True, "stdout": "failed line\n"}
        return {"ok": True, "stdout": json.dumps({"databaseId": 123, "status": "completed"})}

    monkeypatch.setattr(github_tools, "_run_gh", fake_run)

    result = github_tools.gh_run_view(_settings())

    assert result["run"]["databaseId"] == 123
    assert calls[0] == ["run", "list", "--limit", "1", "--json", "databaseId", "--branch", "feature"]
    assert calls[1][:3] == ["run", "view", "123"]
    assert calls[2] == ["run", "view", "123", "--log-failed"]


def test_gh_pr_create_checks_the_requested_head(monkeypatch) -> None:
    observed: list[str | None] = []
    monkeypatch.setattr(github_tools, "_require_gh_ready", lambda _settings: None)

    def fake_status(settings, repo, branch=None):
        observed.append(branch)
        return {"pushed": True, "branch": branch, "upstream": f"origin/{branch}", "ahead": 0}

    monkeypatch.setattr(github_tools, "_branch_push_status", fake_status)

    result = github_tools.gh_pr_create(
        _settings(),
        "Title",
        "Body",
        head="feature/selected",
        dry_run=True,
    )

    assert result["ok"] is True
    assert observed == ["feature/selected"]
