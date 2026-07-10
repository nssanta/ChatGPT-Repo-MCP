import dataclasses
from pathlib import Path

from chatrepo_mcp.config import Settings
from chatrepo_mcp.workspace import (
    detect_stack,
    find_git_toplevel,
    list_workspace_repos,
    makefile_targets,
    resolve_presets_for,
    resolve_roots,
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
        git_network_timeout=60,
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


# --------------------------------------------------------------------------
# find_git_toplevel
# --------------------------------------------------------------------------


def test_find_git_toplevel_climbs_to_nearest_nested_repo(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    roots = resolve_roots(settings)

    repo_a = tmp_path / "repo-a"
    (repo_a / ".git").mkdir(parents=True)
    (repo_a / "src").mkdir()
    (repo_a / "src" / "main.go").write_text("package main\n", encoding="utf-8")

    repo_b = tmp_path / "repo-b"
    (repo_b / ".git").mkdir(parents=True)
    (repo_b / "pkg").mkdir()

    assert find_git_toplevel(repo_a / "src" / "main.go", roots) == repo_a.resolve()
    assert find_git_toplevel(repo_b / "pkg", roots) == repo_b.resolve()


def test_find_git_toplevel_returns_none_for_parent_workspace_without_git(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    roots = resolve_roots(settings)

    (tmp_path / "repo-a" / ".git").mkdir(parents=True)
    (tmp_path / "plain-folder").mkdir()

    # The workspace root itself has no .git and there is no repo above it
    # within the allowed roots, so this must be None (not a git repo).
    assert find_git_toplevel(tmp_path, roots) is None
    assert find_git_toplevel(tmp_path / "plain-folder", roots) is None


def test_find_git_toplevel_recognizes_worktree_file_marker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    roots = resolve_roots(settings)

    worktree = tmp_path / "repo-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/repo-worktree\n", encoding="utf-8")

    assert find_git_toplevel(worktree, roots) == worktree.resolve()


# --------------------------------------------------------------------------
# detect_stack
# --------------------------------------------------------------------------


def test_detect_stack_go_module(tmp_path: Path) -> None:
    directory = tmp_path / "go-svc"
    directory.mkdir()
    (directory / "go.mod").write_text("module example.com/go-svc\n\ngo 1.21\n", encoding="utf-8")

    assert detect_stack(directory) == ["go"]


def test_detect_stack_python_pyproject(tmp_path: Path) -> None:
    directory = tmp_path / "py-svc"
    directory.mkdir()
    (directory / "pyproject.toml").write_text("[project]\nname = 'py-svc'\n", encoding="utf-8")

    assert detect_stack(directory) == ["python"]


def test_detect_stack_node_with_typescript(tmp_path: Path) -> None:
    directory = tmp_path / "node-svc"
    directory.mkdir()
    (directory / "package.json").write_text("{}", encoding="utf-8")
    (directory / "tsconfig.json").write_text("{}", encoding="utf-8")

    assert detect_stack(directory) == ["node", "ts"]


def test_detect_stack_node_without_typescript(tmp_path: Path) -> None:
    directory = tmp_path / "node-svc-plain"
    directory.mkdir()
    (directory / "package.json").write_text("{}", encoding="utf-8")

    assert detect_stack(directory) == ["node"]


def test_detect_stack_rust_cargo(tmp_path: Path) -> None:
    directory = tmp_path / "rust-svc"
    directory.mkdir()
    (directory / "Cargo.toml").write_text("[package]\nname = 'rust-svc'\n", encoding="utf-8")

    assert detect_stack(directory) == ["rust"]


def test_detect_stack_make(tmp_path: Path) -> None:
    directory = tmp_path / "make-svc"
    directory.mkdir()
    (directory / "Makefile").write_text("test:\n\techo hi\n", encoding="utf-8")

    assert detect_stack(directory) == ["make"]


def test_detect_stack_combines_multiple_markers(tmp_path: Path) -> None:
    directory = tmp_path / "mixed-svc"
    directory.mkdir()
    (directory / "go.mod").write_text("module example.com/mixed\n\ngo 1.21\n", encoding="utf-8")
    (directory / "Makefile").write_text("test:\n\tgo test ./...\n", encoding="utf-8")

    stacks = detect_stack(directory)
    assert "go" in stacks
    assert "make" in stacks


# --------------------------------------------------------------------------
# makefile_targets
# --------------------------------------------------------------------------


def test_makefile_targets_parses_real_targets_only(tmp_path: Path) -> None:
    directory = tmp_path / "svc"
    directory.mkdir()
    (directory / "Makefile").write_text(
        "# a comment, not a target\n"
        ".PHONY: test lint\n"
        "test:\n"
        "\tpytest -x -q\n"
        "lint:\n"
        "\truff check .\n"
        "build: test lint\n"
        "\techo building\n",
        encoding="utf-8",
    )

    assert makefile_targets(directory) == ["test", "lint", "build"]


def test_makefile_targets_returns_empty_list_without_makefile(tmp_path: Path) -> None:
    directory = tmp_path / "no-makefile"
    directory.mkdir()

    assert makefile_targets(directory) == []


# --------------------------------------------------------------------------
# resolve_presets_for
# --------------------------------------------------------------------------


def test_resolve_presets_for_makefile_target_wins_over_stack_default(tmp_path: Path) -> None:
    directory = tmp_path / "svc"
    directory.mkdir()
    (directory / "pyproject.toml").write_text("[project]\nname = 'svc'\n", encoding="utf-8")
    (directory / "Makefile").write_text("test:\n\tmake-specific-test-runner\n", encoding="utf-8")
    settings = make_settings(tmp_path)

    presets = resolve_presets_for("svc", settings)

    assert presets["test"] == {"command": "make test", "cwd": "svc", "parser": "auto"}
    # lint has no Makefile target, so it falls back to the python stack default.
    assert presets["lint"]["command"] == "ruff check ."


def test_resolve_presets_for_falls_back_to_stack_without_makefile(tmp_path: Path) -> None:
    directory = tmp_path / "svc"
    directory.mkdir()
    (directory / "go.mod").write_text("module example.com/svc\n\ngo 1.21\n", encoding="utf-8")
    settings = make_settings(tmp_path)

    presets = resolve_presets_for("svc", settings)

    assert presets["test"]["command"] == "go test ./..."
    assert presets["build"]["command"] == "go build ./..."
    assert presets["test"]["cwd"] == "svc"


def test_resolve_presets_for_missing_directory_returns_empty(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert resolve_presets_for("does-not-exist", settings) == {}


def test_resolve_presets_for_rejects_directory_outside_allowed_roots(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "pyproject.toml").write_text("[project]\nname = 'outside'\n", encoding="utf-8")
    settings = make_settings(project)

    assert resolve_presets_for(str(outside), settings) == {}


# --------------------------------------------------------------------------
# list_workspace_repos
# --------------------------------------------------------------------------


def test_list_workspace_repos_polyrepo(tmp_path: Path) -> None:
    go_repo = tmp_path / "go-svc"
    go_repo.mkdir()
    (go_repo / ".git").mkdir()
    (go_repo / "go.mod").write_text("module example.com/go-svc\n\ngo 1.21\n", encoding="utf-8")

    py_repo = tmp_path / "py-svc"
    py_repo.mkdir()
    (py_repo / ".git").mkdir()
    (py_repo / "pyproject.toml").write_text("[project]\nname = 'py-svc'\n", encoding="utf-8")

    (tmp_path / "just-a-folder").mkdir()

    settings = make_settings(tmp_path)
    repos = list_workspace_repos(settings)
    by_path = {repo["path"]: repo for repo in repos}

    assert by_path["go-svc"]["is_git"] is True
    assert "go" in by_path["go-svc"]["stack"]
    assert by_path["py-svc"]["is_git"] is True
    assert "python" in by_path["py-svc"]["stack"]
    # Only directories that are themselves git repos are reported once at
    # least one nested repo is found (the plain sibling folder is not a
    # separate top-level entry in a discovered polyrepo).
    assert "just-a-folder" not in by_path


def test_list_workspace_repos_root_itself_is_git(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'root'\n", encoding="utf-8")
    settings = make_settings(tmp_path)

    repos = list_workspace_repos(settings)

    assert len(repos) == 1
    assert repos[0]["path"] == ""
    assert repos[0]["is_git"] is True
    assert "python" in repos[0]["stack"]
