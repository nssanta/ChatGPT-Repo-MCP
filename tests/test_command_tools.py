from pathlib import Path

from chatrepo_mcp.command_tools import CommandPolicyError, run_command
from chatrepo_mcp.config import Settings


def make_settings(tmp_path: Path) -> Settings:
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
        blocked_globs=(".env", ".env.*", "*.pem", "*.key", "**/.git/**"),
        allow_hidden_default=True,
        allowed_hosts=("127.0.0.1", "localhost"),
        enable_dns_rebinding_protection=True,
        canonical_namespace="/Eva_Ai",
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
    )


def test_run_command_allows_git_diff_check(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    result = run_command("git diff --check", settings)

    assert result["exit_code"] == 0
    assert result["timed_out"] is False


def test_run_command_rejects_shell_and_denied_commands(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    for command in ["git status --short | cat", "cat .env", "rm -rf /", "curl https://example.com"]:
        try:
            run_command(command, settings)
            assert False, f"expected rejection: {command}"
        except CommandPolicyError:
            assert True
