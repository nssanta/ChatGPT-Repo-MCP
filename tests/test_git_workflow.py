"""git_workflow_tools.py: branch/staging/push/reset structural tools.

Uses a real temporary git repository (git init + an initial commit) rather
than mocking subprocess, since these tools are thin, audited wrappers around
real `git` invocations -- see plan Phase 4 and Verification item 7.
"""

import dataclasses
import subprocess
from pathlib import Path

from chatrepo_mcp.command_tools import ConfirmationRequiredError
from chatrepo_mcp.config import Settings
from chatrepo_mcp.git_tools import GitToolError
from chatrepo_mcp.git_workflow_tools import (
    git_add,
    git_create_branch,
    git_push,
    git_reset,
    git_switch_branch,
)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        app_name="test",
        host="127.0.0.1",
        port=8000,
        transport="streamable-http",
        project_root=tmp_path,
        max_file_bytes=1000,
        max_response_chars=1000,
        max_read_files=8,
        max_search_results=50,
        max_tree_entries=100,
        max_diff_bytes=1000,
        max_log_commits=10,
        subprocess_timeout=5,
        blocked_globs=(".env", ".env.*", "*.pem", "*.key", "**/.git/**"),
        allow_hidden_default=True,
        allowed_hosts=("127.0.0.1", "localhost"),
        enable_dns_rebinding_protection=True,
        canonical_namespace="/test",
        ephemeral_handles_supported=False,
        writable_globs=("**/*",),
        max_write_file_bytes=1000,
        dangerously_allow_all_writes=True,
        require_expected_hash_for_writes=True,
        max_batch_operations=50,
        max_combined_diff_chars=300000,
        allow_move_delete_operations=True,
        max_patch_bytes=500000,
        max_command_output_chars=200000,
        command_timeout_ms=120000,
        command_audit_log_path=tmp_path / "audit.log",
        mcp_auth_mode="none",
        mcp_bearer_token=None,
        command_policy_mode="guarded",
        command_jobs_dir=tmp_path / "jobs",
        workspace_roots=(),
        filesystem_unrestricted=False,
        workspace_scan_depth=2,
        denied_words=("sudo", "su"),
        destructive_words=(
            "rm -rf",
            "rmdir",
            "git push --force",
            "git reset --hard",
            "git clean",
            "docker system prune",
            "chmod -R",
            "chown -R",
            "mkfs",
            "dd",
        ),
        command_shell_prelude="",
        git_network_timeout=10,
        protected_branches=("main", "master"),
        allow_force_push=False,
        gh_timeout=60,
        github_tools_enabled=True,
        secret_globs=(".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "**/.git/**"),
        binary_globs=(
            "**/.venv/**",
            "**/node_modules/**",
            "**/*.db",
            "**/*.sqlite",
            "**/*.sqlite3",
            "**/*.bin",
            "**/*.png",
            "**/*.jpg",
            "**/*.jpeg",
            "**/*.webp",
            "**/*.pdf",
            "**/*.zip",
            "**/*.tar",
            "**/*.gz",
        ),
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "init")


def _current_branch(tmp_path: Path) -> str:
    return _git(tmp_path, "branch", "--show-current").stdout.strip()


# --------------------------------------------------------------------------
# git_create_branch / git_switch_branch
# --------------------------------------------------------------------------


def test_git_create_branch_creates_and_checks_out(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)

    result = git_create_branch(settings, "feature/one")

    assert result["ok"] is True
    assert result["branch"] == "feature/one"
    assert result["checked_out"] is True
    assert _current_branch(tmp_path) == "feature/one"


def test_git_create_branch_without_checkout_leaves_current_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)

    result = git_create_branch(settings, "feature/no-checkout", checkout=False)

    assert result["ok"] is True
    assert result["checked_out"] is False
    assert _current_branch(tmp_path) == "main"


def test_git_switch_branch_switches_to_existing_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)
    git_create_branch(settings, "feature/two", checkout=False)

    result = git_switch_branch(settings, "feature/two")

    assert result["ok"] is True
    assert result["branch"] == "feature/two"
    assert result["previous_branch"] == "main"
    assert _current_branch(tmp_path) == "feature/two"


def test_git_switch_branch_refuses_dirty_tree_without_stash(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)
    git_create_branch(settings, "feature/three", checkout=False)
    (tmp_path / "README.md").write_text("dirty change\n", encoding="utf-8")

    result = git_switch_branch(settings, "feature/three")

    assert result["ok"] is False
    assert result["error_kind"] == "git_dirty"
    assert _current_branch(tmp_path) == "main"


# --------------------------------------------------------------------------
# git_add
# --------------------------------------------------------------------------


def test_git_add_dry_run_stages_explicit_paths_without_mutating(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)
    (tmp_path / "new.txt").write_text("content\n", encoding="utf-8")

    result = git_add(settings, ["new.txt"], dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert "new.txt" in result["staged"]
    staged_names = _git(tmp_path, "diff", "--cached", "--name-only").stdout
    assert staged_names == ""


def test_git_add_real_run_stages_explicit_paths(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)
    (tmp_path / "new.txt").write_text("content\n", encoding="utf-8")

    result = git_add(settings, ["new.txt"], dry_run=False)

    assert result["ok"] is True
    assert result["staged"] == ["new.txt"]
    staged_names = _git(tmp_path, "diff", "--cached", "--name-only").stdout.strip()
    assert staged_names == "new.txt"


def test_git_add_rejects_blanket_dot_and_dash_a(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)

    for forbidden in (["."], ["-A"], ["--all"]):
        try:
            git_add(settings, forbidden, dry_run=True)
            assert False, f"expected rejection for {forbidden}"
        except GitToolError:
            assert True


# --------------------------------------------------------------------------
# git_push
# --------------------------------------------------------------------------


def _add_bare_remote(tmp_path: Path) -> Path:
    remote_dir = tmp_path.parent / f"{tmp_path.name}-remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
    _git(tmp_path, "remote", "add", "origin", str(remote_dir))
    return remote_dir


def test_git_push_protected_branch_requires_confirmed_even_for_dry_run(tmp_path: Path) -> None:
    # PROTECTED_BRANCHES always requires confirmed=true, even for a dry-run
    # preview (see git_push docstring point 2).
    _init_repo(tmp_path)
    _add_bare_remote(tmp_path)
    settings = make_settings(tmp_path)

    try:
        git_push(settings, remote="origin", branch="main", dry_run=True, confirmed=False)
        assert False, "expected ConfirmationRequiredError for protected branch"
    except ConfirmationRequiredError:
        assert True


def test_git_push_dry_run_preview_on_unprotected_branch_does_not_require_confirmed(tmp_path: Path) -> None:
    # A dry-run preview against a non-protected branch is a read-only
    # `git push --dry-run` and does not itself need confirmation; only a
    # *real* push does (see next test).
    _init_repo(tmp_path)
    _add_bare_remote(tmp_path)
    settings = make_settings(tmp_path)
    git_create_branch(settings, "feature/preview")

    result = git_push(settings, remote="origin", branch="feature/preview", dry_run=True, confirmed=False)

    assert result["dry_run"] is True


def test_git_push_real_push_on_unprotected_branch_requires_confirmed(tmp_path: Path) -> None:
    # Any real (non-dry-run) push requires confirmed=true, protected branch
    # or not (git_push docstring point 4).
    _init_repo(tmp_path)
    _add_bare_remote(tmp_path)
    settings = make_settings(tmp_path)
    git_create_branch(settings, "feature/push-me")

    try:
        git_push(settings, remote="origin", branch="feature/push-me", dry_run=False, confirmed=False)
        assert False, "expected ConfirmationRequiredError for a real push"
    except ConfirmationRequiredError:
        assert True

    result = git_push(
        settings,
        remote="origin",
        branch="feature/push-me",
        dry_run=False,
        confirmed=True,
        set_upstream=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is False


# --------------------------------------------------------------------------
# git_reset
# --------------------------------------------------------------------------


def test_git_reset_hard_mode_is_not_implemented(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)
    (tmp_path / "README.md").write_text("second commit\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "second")

    try:
        git_reset(settings, mode="hard", target="HEAD~1", confirmed=True)
        assert False, "expected GitToolError: mode='hard' is intentionally unimplemented"
    except GitToolError:
        assert True


def test_git_reset_mixed_mode_requires_confirmed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    settings = make_settings(tmp_path)
    (tmp_path / "README.md").write_text("second commit\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "second")

    try:
        git_reset(settings, mode="mixed", target="HEAD~1", confirmed=False)
        assert False, "expected ConfirmationRequiredError"
    except ConfirmationRequiredError:
        assert True

    result = git_reset(settings, mode="mixed", target="HEAD~1", confirmed=True)
    assert result["ok"] is True
    assert result["mode"] == "mixed"
