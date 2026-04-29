from pathlib import Path
import subprocess

from chatrepo_mcp.config import Settings
from chatrepo_mcp.workflows import (
    git_worktree_guard,
    quality_gate_and_commit,
    run_quality_gate,
    scan_new_policy_violations,
)


def make_settings(tmp_path: Path) -> Settings:
    from test_command_tools import make_settings as base

    settings = base(tmp_path)
    return settings.__class__(
        **{
            **settings.__dict__,
            "command_policy_mode": "full_repo",
            "command_audit_log_path": tmp_path.parent / f"{tmp_path.name}-audit.log",
            "command_jobs_dir": tmp_path.parent / f"{tmp_path.name}-jobs",
        }
    )


def init_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("const a = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)


def test_scan_new_policy_violations_catches_added_any(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "src" / "a.ts").write_text("const a: any = 1;\n", encoding="utf-8")

    result = scan_new_policy_violations(make_settings(tmp_path), base_ref="HEAD", paths=["src"])

    assert result["ok"] is False
    assert result["violations"][0]["rule"] == "no_new_colon_any"


def test_run_quality_gate_blocks_required_failure(tmp_path: Path) -> None:
    init_repo(tmp_path)

    result = run_quality_gate(
        make_settings(tmp_path),
        checks=[{"id": "bad", "command": "bash -lc 'exit 2'", "required": True}],
    )

    assert result["ok"] is False
    assert result["failed_check"] == "bad"


def test_run_quality_gate_allows_optional_failure(tmp_path: Path) -> None:
    init_repo(tmp_path)

    result = run_quality_gate(
        make_settings(tmp_path),
        checks=[
            {"id": "optional", "command": "bash -lc 'exit 2'", "required": False},
            {"id": "diff", "preset": "git_diff_check", "required": True},
        ],
    )

    assert result["ok"] is True
    assert len(result["checks"]) == 2


def test_quality_gate_and_commit_commits_only_after_green_checks(tmp_path: Path) -> None:
    init_repo(tmp_path)
    target = tmp_path / "src" / "a.ts"
    target.write_text("const a = 2;\n", encoding="utf-8")

    result = quality_gate_and_commit(
        make_settings(tmp_path),
        checks=[{"id": "diff", "preset": "git_diff_check", "required": True}],
        commit={"message": "fix: update a", "paths": ["src/a.ts"]},
    )

    status = subprocess.run(["git", "status", "--short"], cwd=tmp_path, text=True, capture_output=True, check=True)
    assert result["ok"] is True
    assert result["committed"] is True
    assert status.stdout == ""


def test_quality_gate_and_commit_does_not_commit_on_failed_gate(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "src" / "a.ts").write_text("const a = 2;\n", encoding="utf-8")

    result = quality_gate_and_commit(
        make_settings(tmp_path),
        checks=[{"id": "bad", "command": "bash -lc 'exit 3'", "required": True}],
        commit={"message": "fix: update a", "paths": ["src/a.ts"]},
    )

    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, text=True, capture_output=True, check=True)
    assert result["ok"] is False
    assert result["committed"] is False
    assert len(log.stdout.splitlines()) == 1


def test_git_worktree_guard_reports_unexpected_dirty_path(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "src" / "a.ts").write_text("const a = 2;\n", encoding="utf-8")

    result = git_worktree_guard(make_settings(tmp_path), allowed_dirty_paths=["docs/allowed.md"])

    assert result["ok"] is False
    assert result["dirty_unexpected"] == ["src/a.ts"]
