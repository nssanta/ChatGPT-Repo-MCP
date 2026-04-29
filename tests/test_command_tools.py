from pathlib import Path

from chatrepo_mcp.command_tools import (
    CommandPolicyError,
    ConfirmationRequiredError,
    command_policy_check,
    cancel_command_job,
    get_command_log,
    get_command_job,
    get_job_status,
    git_commit,
    run_command,
    run_commands,
    summarize_command_log,
    start_command_job,
)
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
        command_audit_log_path=tmp_path / "audit.log",
        mcp_auth_mode="none",
        mcp_bearer_token=None,
        command_policy_mode="allowlist",
        command_jobs_dir=tmp_path / "jobs",
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


def test_run_command_uses_bash_environment_and_redacts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    result = run_command("git status --short", settings, env={"TOKEN": "secret"}, tail_lines=10)

    assert result["cwd"] == str(tmp_path)
    assert "node" in result["resolved_binaries"]
    assert result["exit_code"] != 127
    assert (tmp_path / "audit.log").exists()


def test_run_command_confirmation_required(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    for command, env in [
        ("bash scripts/start_local.sh", None),
        ("npx vitest run --config vitest.e2e.live.config.ts -t Tool", None),
        ("npx vitest run packages/agent/src/example.test.ts", {"EVA_LIVE_TESTS": "1"}),
    ]:
        try:
            run_command(command, settings, env=env)
            assert False, "expected confirmation"
        except ConfirmationRequiredError:
            assert True


def test_full_repo_mode_does_not_require_confirmation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "start_local.sh").write_text("#!/usr/bin/env bash\necho started\n", encoding="utf-8")

    result = run_command("bash scripts/start_local.sh", settings)

    assert result["ok"] is True
    assert result["stdout_tail"] == "started"


def test_run_commands_collects_exit_codes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = run_commands(["git status --short", "git diff --check"], settings)

    assert result["ok"] is True
    assert [item["exit_code"] for item in result["results"]] == [0, 0]


def test_run_command_returns_summary_parsed_and_log_id(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    result = run_command("printf 'Tests  1 passed (1)\\n'", settings, parse_kind="vitest")
    log = get_command_log(result["log_id"], settings, start_line=1, end_line=1)
    summary = summarize_command_log(result["log_id"], settings, parser="vitest")

    assert result["ok"] is True
    assert result["parsed"]["kind"] == "vitest"
    assert result["summary"]
    assert log["content"] == "1: Tests  1 passed (1)"
    assert summary["parsed"]["tests"]["passed"] == 1


def test_get_command_log_supports_grep(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    result = run_command("printf 'alpha\\nbeta\\n'", settings)
    log = get_command_log(result["log_id"], settings, grep="beta")

    assert "beta" in log["content"]
    assert "alpha" not in log["content"]


def test_run_command_redacts_remote_urls_in_output_and_logs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    result = run_command("printf 'git@github.com:nssanta/private.git\\n'", settings)
    log = get_command_log(result["log_id"], settings)

    assert "git@github.com" not in result["stdout"]
    assert "git@github.com" not in log["content"]
    assert "<redacted>" in result["stdout"]


def test_command_policy_check_explains_shell_split(tmp_path: Path) -> None:
    result = command_policy_check("git status --short && git diff --check", make_settings(tmp_path))
    full_repo = make_settings(tmp_path)
    full_repo = full_repo.__class__(**{**full_repo.__dict__, "command_policy_mode": "full_repo"})
    allowed = command_policy_check("git status --short && git diff --check", full_repo)

    assert result["allowed"] is False
    assert result["safe_split"] == ["git status --short", "git diff --check"]
    assert allowed["allowed"] is True
    assert allowed["safe_split"] == ["git status --short", "git diff --check"]


def test_git_commit_dry_run_does_not_stage(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    target = tmp_path / "missions" / "CURRENT.md"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    target.write_text("new\n", encoding="utf-8")

    result = git_commit("docs: update current", ["missions/CURRENT.md"], settings, dry_run=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result["ok"] is True
    assert "new" in result["staged_diff"]
    assert staged.stdout == ""


def test_full_repo_mode_allows_shell_operators_inside_repo(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    result = run_command("node --version && npm --version", settings)

    assert result["ok"] is True
    assert result["exit_code"] == 0


def test_command_timeout_is_structured(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo", "command_timeout_ms": 50})

    result = run_command("sleep 1", settings)

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert result["error_kind"] == "command_timeout"


def test_background_command_job_can_be_polled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    started = start_command_job("printf done", settings)
    result = get_command_job(started["job_id"], settings)

    assert started["ok"] is True
    assert result["job_id"] == started["job_id"]


def test_background_job_lock_fail_attach_and_cancel(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    first = start_command_job("sleep 30", settings, concurrency_key="telegram-live-e2e", on_conflict="fail")
    conflict = start_command_job("sleep 30", settings, concurrency_key="telegram-live-e2e", on_conflict="fail")
    attached = start_command_job("sleep 30", settings, concurrency_key="telegram-live-e2e", on_conflict="attach")
    cancelled = cancel_command_job(first["job_id"], settings)
    second = start_command_job("printf done", settings, concurrency_key="telegram-live-e2e", on_conflict="fail")
    second_status = get_job_status(second["job_id"], settings)

    assert first["lock_status"] == "acquired"
    assert conflict["error_kind"] == "job_lock_conflict"
    assert conflict["job_id"] == first["job_id"]
    assert attached["attached_to_job_id"] == first["job_id"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["process_alive"] is False
    assert second["ok"] is True
    assert second_status["status"] in {"completed", "running"}


def test_get_job_status_and_timeout_kill_process_group(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    started = start_command_job("bash -lc 'sleep 10 & wait'", settings, timeout_ms=50)
    import time

    time.sleep(0.15)
    status = get_job_status(started["job_id"], settings)

    assert status["status"] == "timed_out"
    assert status["timed_out"] is True
    assert status["process_alive"] is False
    assert status["kill_status"] in {"terminated", "killed", "not_running"}
