import dataclasses
from pathlib import Path

from chatrepo_mcp.config import Settings
from chatrepo_mcp.edit_tools import (
    PatchApplyError,
    StaleWriteError,
    WritePolicyError,
    _allows_everything,
    _is_writable_relative,
    apply_change_set,
    append_to_file,
    apply_patch_diff,
    batch_edit_files,
    create_text_file,
    delete_text_in_file,
    delete_path,
    ensure_directory,
    insert_after_heading,
    insert_after_line,
    insert_before_heading,
    insert_before_line,
    insert_text_in_file,
    move_path,
    replace_text_in_file,
    replace_lines,
    sha256_text,
    structured_error,
    update_current_mission,
    write_text_file,
)
from chatrepo_mcp.security import SecurityError


def make_settings(tmp_path: Path, *, writable_globs: tuple[str, ...] | None = None, dangerous: bool = False) -> Settings:
    return Settings(
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
        blocked_globs=(
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            "**/.git/**",
            "**/.venv/**",
            "**/node_modules/**",
            "**/*.png",
            "**/*.db",
        ),
        allow_hidden_default=True,
        allowed_hosts=("127.0.0.1", "localhost"),
        enable_dns_rebinding_protection=True,
        canonical_namespace="/Eva_Ai",
        ephemeral_handles_supported=False,
        writable_globs=writable_globs
        or (
            ".claude/**",
            "missions/**",
            "docs/**",
            "reports/**",
        ),
        max_write_file_bytes=1000,
        dangerously_allow_all_writes=dangerous,
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
        command_policy_mode="allowlist",
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


def write_allowed_file(tmp_path: Path, rel: str, text: str) -> tuple[Path, str]:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, sha256_text(text)


def test_replace_success_and_not_found(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, old_hash = write_allowed_file(tmp_path, "missions/CURRENT.md", "hello world\n")

    result = replace_text_in_file(
        "missions/CURRENT.md",
        "world",
        "team",
        settings,
        expected_sha256=old_hash,
        dry_run=False,
    )

    assert result["changed"] is True
    assert "team" in path.read_text(encoding="utf-8")
    try:
        replace_text_in_file("missions/CURRENT.md", "missing", "x", settings, expected_sha256=result["new_sha256"])
        assert False, "expected not found"
    except ValueError:
        assert True


def test_insert_before_and_after(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, old_hash = write_allowed_file(tmp_path, "missions/BACKLOG.md", "A\nB\n")

    result = insert_text_in_file("missions/BACKLOG.md", "B", "before", "before-", settings, expected_sha256=old_hash, dry_run=False)
    result = insert_text_in_file(
        "missions/BACKLOG.md",
        "before-B",
        "after",
        "-after",
        settings,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )

    assert path.read_text(encoding="utf-8") == "A\nbefore-B-after\n"


def test_delete_by_text_and_line_range(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, old_hash = write_allowed_file(tmp_path, "missions/CURRENT_REPORT.md", "one\ntwo\nthree\n")

    result = delete_text_in_file("missions/CURRENT_REPORT.md", settings, find="two\n", expected_sha256=old_hash, dry_run=False)
    result = delete_text_in_file(
        "missions/CURRENT_REPORT.md",
        settings,
        start_line=2,
        end_line=2,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )

    assert path.read_text(encoding="utf-8") == "one\n"


def test_write_create_if_missing_and_dry_run_no_mutation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "missions" / "packets" / "new.md"

    preview = write_text_file("missions/packets/new.md", "draft\n", settings, create_if_missing=True, dry_run=True)
    assert preview["changed"] is True
    assert not target.exists()

    result = write_text_file("missions/packets/new.md", "draft\n", settings, create_if_missing=True, dry_run=False)
    assert target.read_text(encoding="utf-8") == "draft\n"
    assert result["new_sha256"] == sha256_text("draft\n")


def test_stale_hash_and_missing_hash_reject(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_allowed_file(tmp_path, "missions/USER_BUGS.md", "bug\n")

    try:
        replace_text_in_file("missions/USER_BUGS.md", "bug", "fix", settings, expected_sha256="bad")
        assert False, "expected stale hash"
    except StaleWriteError:
        assert True

    try:
        replace_text_in_file("missions/USER_BUGS.md", "bug", "fix", settings)
        assert False, "expected missing hash"
    except StaleWriteError:
        assert True


def test_blocked_writable_and_traversal_reject(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('x')\n", encoding="utf-8")

    try:
        write_text_file(".env", "x", settings, expected_sha256=sha256_text("SECRET=value\n"))
        assert False, "expected blocked path"
    except SecurityError:
        # .env matches secret_globs, which is a hard perimeter block (always on,
        # even under filesystem_unrestricted) -- distinct from the softer
        # writable_globs policy, hence SecurityError rather than WritePolicyError.
        assert True

    try:
        write_text_file("src/main.py", "x", settings, expected_sha256=sha256_text("print('x')\n"))
        assert False, "expected writable policy reject"
    except WritePolicyError:
        assert True

    try:
        write_text_file("../outside.md", "x", settings)
        assert False, "expected traversal reject"
    except SecurityError:
        assert True


def test_star_requires_dangerous_flag(tmp_path: Path) -> None:
    strict = make_settings(tmp_path, writable_globs=("*",), dangerous=False)
    dangerous = make_settings(tmp_path, writable_globs=("*",), dangerous=True)
    path, old_hash = write_allowed_file(tmp_path, "src/main.py", "old\n")

    try:
        write_text_file("src/main.py", "new\n", strict, expected_sha256=old_hash)
        assert False, "expected dangerous flag reject"
    except WritePolicyError:
        assert True

    result = write_text_file("src/main.py", "new\n", dangerous, expected_sha256=old_hash, dry_run=False)
    assert result["changed"] is True
    assert path.read_text(encoding="utf-8") == "new\n"


def test_allows_everything_recognizes_all_catchall_spellings() -> None:
    assert _allows_everything(("*",)) is True
    assert _allows_everything(("**",)) is True
    assert _allows_everything(("**/*",)) is True
    assert _allows_everything((".claude/**", "docs/**")) is False
    assert _allows_everything(()) is False


def test_interlock_regression_default_writable_globs_blocks_without_dangerous_flag(tmp_path: Path) -> None:
    # Regression test for the interlock bug: the *default* WRITABLE_GLOBS is
    # ("**/*",), not the literal "*". The old buggy check only special-cased
    # the literal "*" string, so with the real default value the interlock
    # never engaged and dangerously_allow_all_writes was silently ignored
    # (writes to anywhere succeeded even with the flag left at its default
    # False). This must now be blocked.
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=False)
    write_allowed_file(tmp_path, "src/main.py", "old\n")

    assert _is_writable_relative("src/main.py", settings) is False
    try:
        write_text_file("src/main.py", "new\n", settings, expected_sha256=sha256_text("old\n"))
        assert False, "expected the write-everywhere interlock to reject this write"
    except WritePolicyError:
        assert True


def test_interlock_allows_everything_once_dangerous_flag_is_set(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    path, old_hash = write_allowed_file(tmp_path, "src/main.py", "old\n")

    assert _is_writable_relative("src/main.py", settings) is True
    result = write_text_file("src/main.py", "new\n", settings, expected_sha256=old_hash, dry_run=False)

    assert result["changed"] is True
    assert path.read_text(encoding="utf-8") == "new\n"


def test_interlock_secret_globs_always_blocked_even_with_dangerous_flag(tmp_path: Path) -> None:
    # Secrets are never writable, even in "god mode" (dangerously_allow_all_writes
    # + a catch-all writable_globs) -- defense in depth on top of the SecurityError
    # already raised earlier in resolve_write_path/_resolve_write_target.
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")

    assert _is_writable_relative(".env", settings) is False
    try:
        write_text_file(".env", "SECRET=nope\n", settings, expected_sha256=sha256_text("SECRET=value\n"))
        assert False, "expected .env to be blocked"
    except SecurityError:
        assert True


def test_create_move_delete_and_ensure_directory(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)

    dir_result = ensure_directory("src/features", settings, dry_run=False)
    create_result = create_text_file("src/features/a.ts", "export const a = 1\n", settings, dry_run=False)
    move_result = move_path(
        "src/features/a.ts",
        "src/features/b.ts",
        settings,
        expected_sha256=create_result["new_sha256"],
        dry_run=False,
    )
    delete_result = delete_path(
        "src/features/b.ts",
        settings,
        expected_sha256=move_result["new_sha256"],
        dry_run=False,
    )

    assert dir_result["changed"] is True
    assert create_result["changed"] is True
    assert move_result["destination_path"] == "src/features/b.ts"
    assert delete_result["changed"] is True
    assert not (tmp_path / "src" / "features" / "b.ts").exists()


def test_binary_blocked_even_with_full_repo_write(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    (tmp_path / "image.png").write_bytes(b"\x89PNG\n")

    try:
        write_text_file("image.png", "nope", settings, expected_sha256=sha256_text(""))
        assert False, "expected blocked binary path"
    except WritePolicyError:
        assert True


def test_batch_atomic_rollback_on_failure(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    first, first_hash = write_allowed_file(tmp_path, "src/first.py", "one\n")
    second, second_hash = write_allowed_file(tmp_path, "src/second.py", "two\n")

    result = batch_edit_files(
        [
            {
                "op": "replace",
                "path": "src/first.py",
                "find": "one",
                "replace": "ONE",
                "expected_sha256": first_hash,
            },
            {
                "op": "replace",
                "path": "src/second.py",
                "find": "missing",
                "replace": "TWO",
                "expected_sha256": second_hash,
            },
        ],
        settings,
        atomic=True,
        dry_run=False,
    )

    assert result["failed_operation_index"] == 1
    assert result["rollback_performed"] is True
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"


def test_batch_non_atomic_partial_apply_and_combined_diff(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    first, first_hash = write_allowed_file(tmp_path, "src/first.py", "one\n")
    second, second_hash = write_allowed_file(tmp_path, "src/second.py", "two\n")

    result = batch_edit_files(
        [
            {
                "op": "replace",
                "path": "src/first.py",
                "find": "one",
                "replace": "ONE",
                "expected_sha256": first_hash,
            },
            {
                "op": "replace",
                "path": "src/second.py",
                "find": "missing",
                "replace": "TWO",
                "expected_sha256": second_hash,
            },
        ],
        settings,
        atomic=False,
        dry_run=False,
    )

    assert result["failed_operation_index"] == 1
    assert result["rollback_performed"] is False
    assert "src/first.py" in result["combined_diff"]
    assert first.read_text(encoding="utf-8") == "ONE\n"
    assert second.read_text(encoding="utf-8") == "two\n"


def test_batch_create_move_edit_dry_run_no_mutation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    existing, existing_hash = write_allowed_file(tmp_path, "docs/existing.md", "old\n")

    result = batch_edit_files(
        [
            {"op": "ensure_directory", "path": "docs/new"},
            {"op": "create_file", "path": "docs/new/a.md", "content": "hello\n"},
            {"op": "write", "path": "docs/new/b.md", "content": "world\n", "create_if_missing": True},
            {
                "op": "move",
                "source_path": "docs/existing.md",
                "destination_path": "docs/existing-renamed.md",
                "expected_sha256": existing_hash,
            },
        ],
        settings,
        atomic=True,
        dry_run=True,
    )

    assert result["failed_operation_index"] is None
    assert "docs/new/a.md" in result["combined_diff"]
    assert not (tmp_path / "docs" / "new").exists()
    assert existing.exists()


def test_line_and_heading_tools(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, old_hash = write_allowed_file(tmp_path, "missions/CURRENT.md", "# Mission\n\n## Goal\nold\n")

    result = insert_before_heading(
        "missions/CURRENT.md",
        "## Goal",
        "## P0\nnotes\n",
        settings,
        expected_sha256=old_hash,
        dry_run=False,
    )
    result = replace_lines(
        "missions/CURRENT.md",
        5,
        5,
        "new",
        settings,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )
    result = insert_after_line(
        "missions/CURRENT.md",
        5,
        "after",
        settings,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )
    result = insert_before_line(
        "missions/CURRENT.md",
        1,
        "top",
        settings,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )
    result = insert_after_heading(
        "missions/CURRENT.md",
        "## P0",
        "after-heading",
        settings,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )
    result = append_to_file(
        "missions/CURRENT.md",
        "tail",
        settings,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )

    text = path.read_text(encoding="utf-8")
    assert text.startswith("top\n# Mission")
    assert "## P0\nafter-heading\nnotes\n" in text
    assert "new\nafter\n" in text
    assert text.endswith("tail\n")


def test_heading_not_found_returns_known_error_kind(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    _, old_hash = write_allowed_file(tmp_path, "missions/CURRENT.md", "# Mission\n")

    try:
        insert_before_heading("missions/CURRENT.md", "## Goal", "x", settings, expected_sha256=old_hash)
        assert False, "expected heading miss"
    except ValueError as exc:
        assert "heading not found" in str(exc)


def test_update_current_mission_before_goal(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, _ = write_allowed_file(tmp_path, "missions/CURRENT.md", "# Mission\n\n## Goal\nShip\n")

    result = update_current_mission("P0 Addendum", "Do this.", settings, dry_run=False)

    assert result["changed"] is True
    assert "## P0 Addendum\n\nDo this.\n\n## Goal" in path.read_text(encoding="utf-8")


def test_update_current_mission_preset_and_chunks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, _ = write_allowed_file(tmp_path, "missions/CURRENT.md", "# Mission\n\n## Goal\nShip\n")

    result = update_current_mission(None, None, settings, preset="mandatory_system_tool_log", dry_run=False)
    result = update_current_mission(
        "Chunked",
        None,
        settings,
        chunks=["one", "two"],
        dry_run=False,
    )

    text = path.read_text(encoding="utf-8")
    assert "mandatory separate system tool log" in text
    assert "## Chunked\n\none\ntwo\n\n" in text
    assert result["changed"] is True


def test_apply_patch_dry_run_apply_and_rejects_blocked_path(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    path, _ = write_allowed_file(tmp_path, "src/main.py", "old\n")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")

    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    patch = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new
"""

    preview = apply_patch_diff(patch, settings, dry_run=True)
    assert preview["changed_files"] == ["src/main.py"]
    assert path.read_text(encoding="utf-8") == "old\n"

    applied = apply_patch_diff(patch, settings, dry_run=False)
    assert applied["applied"] is True
    assert path.read_text(encoding="utf-8") == "new\n"

    blocked_patch = """diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -1 +1 @@
-SECRET=value
+SECRET=nope
"""
    try:
        apply_patch_diff(blocked_patch, settings, dry_run=True)
        assert False, "expected blocked path"
    except SecurityError:
        # .env matches secret_globs, a hard perimeter block raised as SecurityError.
        assert True


def test_batch_supports_type_alias_for_operations(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    path, old_hash = write_allowed_file(tmp_path, "src/first.py", "one\n")

    result = batch_edit_files(
        [
            {
                "type": "replace_lines",
                "path": "src/first.py",
                "start_line": 1,
                "end_line": 1,
                "replacement": "ONE",
                "expected_sha256": old_hash,
            },
        ],
        settings,
        atomic=True,
        dry_run=False,
    )

    assert result["failed_operation_index"] is None
    assert path.read_text(encoding="utf-8") == "ONE\n"


def test_apply_change_set_multi_file_dry_run_and_stale_error(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    first, first_hash = write_allowed_file(tmp_path, "src/first.py", "one\n")
    second, second_hash = write_allowed_file(tmp_path, "src/second.py", "two\n")

    preview = apply_change_set(
        [
            {"op": "replace", "path": "src/first.py", "find": "one", "replace": "ONE", "expected_sha256": first_hash},
            {"op": "replace", "path": "src/second.py", "find": "two", "replace": "TWO", "expected_sha256": second_hash},
        ],
        settings,
        name="two-file-preview",
        dry_run=True,
    )
    stale = apply_change_set(
        [{"op": "replace", "path": "src/first.py", "find": "one", "replace": "ONE", "expected_sha256": "bad"}],
        settings,
        dry_run=False,
    )

    assert preview["ok"] is True
    assert preview["name"] == "two-file-preview"
    assert preview["changed_files"] == ["src/first.py", "src/second.py"]
    assert "src/first.py" in preview["combined_diff"]
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"
    assert stale["ok"] is False
    assert stale["results"][0]["error_kind"] == "stale_expected_hash"


def test_apply_change_set_invalid_format_returns_example(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)

    result = apply_change_set([], settings)
    unsupported = apply_change_set([{"op": "unknown"}], settings)

    assert result["ok"] is False
    assert result["error_kind"] == "invalid_change_set_format"
    assert "operations" in result["valid_example"]
    assert unsupported["results"][0]["error_kind"] == "invalid_change_set_format"
    assert "valid_example" in unsupported


def test_invalid_patch_format_returns_valid_example(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    try:
        apply_patch_diff("not a patch", settings, dry_run=True)
        assert False, "expected invalid patch"
    except PatchApplyError as exc:
        error = structured_error(exc)

    assert error["error_kind"] == "invalid_patch_format"
    assert "diff --git" in error["valid_example"]


def test_external_workspace_edit_targets_the_external_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (project / "same.txt").write_text("project\n", encoding="utf-8")
    external_file = external / "same.txt"
    external_file.write_text("external\n", encoding="utf-8")
    settings = dataclasses.replace(
        make_settings(project, writable_globs=("**/*",), dangerous=True),
        workspace_roots=(str(external),),
        require_expected_hash_for_writes=False,
    )

    result = replace_text_in_file(
        str(external_file),
        "external",
        "changed",
        settings,
        dry_run=False,
    )

    assert result["path"] == str(external_file)
    assert external_file.read_text(encoding="utf-8") == "changed\n"
    assert (project / "same.txt").read_text(encoding="utf-8") == "project\n"


def test_atomic_batch_rolls_back_external_workspace_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    external_file = external / "same.txt"
    external_file.write_text("before\n", encoding="utf-8")
    settings = dataclasses.replace(
        make_settings(project, writable_globs=("**/*",), dangerous=True),
        workspace_roots=(str(external),),
        require_expected_hash_for_writes=False,
    )

    result = batch_edit_files(
        [
            {"op": "replace", "path": str(external_file), "find": "before", "replace": "after"},
            {"op": "replace", "path": str(external_file), "find": "missing", "replace": "x"},
        ],
        settings,
        atomic=True,
        dry_run=False,
    )

    assert result["ok"] is False
    assert result["rollback_performed"] is True
    assert external_file.read_text(encoding="utf-8") == "before\n"
