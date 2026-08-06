from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

from test_command_tools import make_settings

from chatrepo_mcp import git_tools
from chatrepo_mcp.git_tools import GitToolError
from chatrepo_mcp.output_store import read_artifact


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def test_resolve_repo_toplevel_rejects_path_without_git_marker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "plain").mkdir()

    try:
        git_tools._resolve_repo_toplevel("plain", settings)
        assert False, "expected GitToolError"
    except GitToolError:
        assert True


def test_repo_git_info_detects_polyrepo_when_root_has_no_git(tmp_path: Path) -> None:
    (tmp_path / "svc-a" / ".git").mkdir(parents=True)
    (tmp_path / "svc-b" / ".git").mkdir(parents=True)

    result = git_tools.repo_git_info(make_settings(tmp_path))

    assert result["polyrepo"] is True
    paths = {entry["path"] for entry in result["repos"]}
    assert paths == {"svc-a", "svc-b"}


def test_repo_git_info_with_explicit_repo_returns_metadata(tmp_path: Path) -> None:
    _git("init", "-q", cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", ".", cwd=tmp_path)
    _git("-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "init", cwd=tmp_path)

    result = git_tools.repo_git_info(make_settings(tmp_path), repo=".")

    assert result["repo"] == ""
    assert result["top_level"] == str(tmp_path)
    assert result["remotes"] == []
    assert result["git_dir"].endswith(".git")


def test_list_repos_marks_non_git_when_workspace_has_no_nested_repo(tmp_path: Path) -> None:
    (tmp_path / "plain-dir").mkdir()
    settings = make_settings(tmp_path)

    report = git_tools.list_repos(settings)
    paths = {entry["path"] for entry in report["repos"]}

    assert "plain-dir" in paths
    assert all((entry["is_git"] is False) for entry in report["repos"])


def test_run_git_grep_returncode_one_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    _git("init", "-q", cwd=tmp_path)

    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = ""

    FakeResult.stdout_truncated = False
    monkeypatch.setattr(git_tools, "run_bounded", lambda *args, **kwargs: FakeResult())

    output, truncated, artifact = git_tools._run_git_grep(["grep", "x"], settings, tmp_path)
    assert output == ""
    assert truncated is False
    assert artifact is None


def test_run_git_grep_propagates_fatal_error(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    _git("init", "-q", cwd=tmp_path)

    class FakeResult:
        returncode = 2
        stdout = ""
        stderr = "fatal: boom"

    FakeResult.stdout_truncated = False
    monkeypatch.setattr(git_tools, "run_bounded", lambda *args, **kwargs: FakeResult())

    try:
        git_tools._run_git_grep(["grep", "x"], settings, tmp_path)
        assert False, "expected GitToolError"
    except GitToolError as exc:
        assert "fatal: boom" in str(exc)


def test_failed_git_capture_keeps_redacted_artifact_receipt(tmp_path: Path) -> None:
    _git("init", "-q", cwd=tmp_path)
    settings = make_settings(tmp_path)

    try:
        git_tools._run_git_capture(["show", "does-not-exist"], settings, cwd=tmp_path, persist_artifact=True)
        assert False, "expected GitToolError"
    except GitToolError as exc:
        assert exc.result is not None
        assert exc.result["error_kind"] == "git_error"
        assert exc.result["artifact"]["artifact_id"]
        assert exc.result["receipt"]["applied"]["source_complete"] is True


def test_failed_git_two_stream_preview_and_full_artifact(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "git"
    binary.write_text(
        """#!/bin/sh
printf 'OUT_HEAD:'
i=0; while [ "$i" -lt 600 ]; do printf 'o'; i=$((i+1)); done
printf ':OUT_TAIL'
printf 'ERR_HEAD:' >&2
i=0; while [ "$i" -lt 600 ]; do printf 'e' >&2; i=$((i+1)); done
printf ':ERR_TAIL' >&2
exit 7
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    settings = replace(
        make_settings(tmp_path), default_inline_output_bytes=256,
        artifact_disk_reserve_bytes=0,
    )

    try:
        git_tools._run_git_capture(["status"], settings, cwd=tmp_path, persist_artifact=True)
        assert False, "expected GitToolError"
    except GitToolError as exc:
        assert exc.result is not None
        result = exc.result
        assert result["truncated"] is True
        assert len(result["stdout"].encode()) + len(result["stderr"].encode()) <= 256
        for marker in ("OUT_HEAD:", ":OUT_TAIL"):
            assert marker in result["stdout"]
        for marker in ("ERR_HEAD:", ":ERR_TAIL"):
            assert marker in result["stderr"]
        assert result["continuation"]["tool"] == "read_artifact"
        page = read_artifact(result["artifact"]["artifact_id"], settings, max_bytes=4096)
        full = "".join(record["data"] for record in page["payload"]["records"])
        for marker in ("OUT_HEAD:", ":OUT_TAIL", "ERR_HEAD:", ":ERR_TAIL"):
            assert marker in full


def test_git_grep_polyrepo_aggregates_results_from_multiple_repos_and_limits(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    _git("init", "-q", cwd=repo_a)
    (repo_a / "x.txt").write_text("need\n", encoding="utf-8")
    _git("add", "x.txt", cwd=repo_a)
    _git("-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "init", cwd=repo_a)

    _git("init", "-q", cwd=repo_b)
    (repo_b / "y.txt").write_text("need\n", encoding="utf-8")
    _git("add", "y.txt", cwd=repo_b)
    _git("-c", "user.email=b@example.com", "-c", "user.name=B", "commit", "-m", "init", cwd=repo_b)

    base = make_settings(tmp_path)
    settings = base.__class__(**{**base.__dict__, "max_search_results": 1})

    result = git_tools.git_grep(settings, "need", repo=None)

    assert result["polyrepo"] is True
    assert result["count"] == 1
    assert result["results"]
    assert result["results"][0]["repo"] in {"a", "b"}
