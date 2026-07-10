"""Perimeter tests across multiple allowed roots: project_root, WORKSPACE_ROOTS,
and FILESYSTEM_UNRESTRICTED -- see plan "Verification" item 8 and Phase 0/7.
"""

import dataclasses
from pathlib import Path

from chatrepo_mcp.config import Settings
from chatrepo_mcp.security import SecurityError, resolve_repo_path
from chatrepo_mcp.workspace import resolve_roots


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


def test_path_inside_project_root_is_allowed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "src" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('ok')\n", encoding="utf-8")

    assert resolve_repo_path("src/main.py", settings) == target.resolve()


def test_path_above_project_root_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"

    try:
        resolve_repo_path(str(outside), settings)
        assert False, "expected SecurityError"
    except SecurityError:
        assert True


def test_dot_dot_traversal_outside_root_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    try:
        resolve_repo_path("../escape.txt", settings)
        assert False, "expected SecurityError"
    except SecurityError:
        assert True


def test_workspace_roots_grants_access_to_extra_folder(tmp_path: Path) -> None:
    extra = tmp_path.parent / f"extra-{tmp_path.name}"
    extra.mkdir()
    (extra / "shared.md").write_text("shared\n", encoding="utf-8")
    settings = make_settings(tmp_path, workspace_roots=(str(extra),))

    resolved = resolve_repo_path(str(extra / "shared.md"), settings)

    assert resolved == (extra / "shared.md").resolve()
    # And a path elsewhere (not project_root, not the extra root) is still rejected.
    unrelated = tmp_path.parent / f"unrelated-{tmp_path.name}.txt"
    try:
        resolve_repo_path(str(unrelated), settings)
        assert False, "expected SecurityError"
    except SecurityError:
        assert True


def test_workspace_roots_is_reflected_in_resolve_roots(tmp_path: Path) -> None:
    extra = tmp_path.parent / f"extra2-{tmp_path.name}"
    extra.mkdir()
    settings = make_settings(tmp_path, workspace_roots=(str(extra),))

    roots = resolve_roots(settings)

    assert tmp_path.resolve() in roots
    assert extra.resolve() in roots


def test_filesystem_unrestricted_allows_arbitrary_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"unrestricted-{tmp_path.name}"
    outside.mkdir()
    (outside / "notes.txt").write_text("hi\n", encoding="utf-8")
    settings = make_settings(tmp_path, filesystem_unrestricted=True)

    resolved = resolve_repo_path(str(outside / "notes.txt"), settings)

    assert resolved == (outside / "notes.txt").resolve()


def test_filesystem_unrestricted_still_blocks_secret_globs(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"unrestricted2-{tmp_path.name}"
    outside.mkdir()
    (outside / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (outside / "id_rsa.pem").write_text("fake-key\n", encoding="utf-8")
    settings = make_settings(tmp_path, filesystem_unrestricted=True)

    for secret_path in (outside / ".env", outside / "id_rsa.pem"):
        try:
            resolve_repo_path(str(secret_path), settings)
            assert False, f"expected SecurityError for {secret_path}"
        except SecurityError:
            assert True


def test_full_secret_access_can_open_structured_secret_paths(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("SECRET=value\n", encoding="utf-8")
    settings = make_settings(
        tmp_path,
        access_mode="full",
        filesystem_unrestricted=True,
        allow_secret_access=True,
    )

    assert resolve_repo_path(str(secret), settings, allow_hidden=True) == secret.resolve()


def test_filesystem_unrestricted_makes_resolve_roots_the_filesystem_root(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, filesystem_unrestricted=True)

    roots = resolve_roots(settings)

    assert roots == [Path("/")]
