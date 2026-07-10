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
    run_test_preset,
    summarize_command_log,
    start_command_job,
)
from chatrepo_mcp.config import Settings
from chatrepo_mcp.profile import list_test_presets


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


def test_full_access_policy_allows_raw_shell_and_git_push(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(
        **{
            **settings.__dict__,
            "access_mode": "full",
            "command_policy_mode": "unrestricted",
            "filesystem_unrestricted": True,
        }
    )

    result = command_policy_check("git push origin feature", settings)

    assert result["allowed"] is True
    assert result["mode"] == "unrestricted"


def test_run_command_uses_bash_environment_and_redacts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    # _resolved_binaries now only probes binaries relevant to the detected
    # stack of `cwd` (instead of unconditionally shelling out for
    # node/npm/npx on every command), so give the cwd a node stack marker.
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    result = run_command("git status --short", settings, env={"TOKEN": "secret"}, tail_lines=10)

    assert result["cwd"] == str(tmp_path)
    assert "node" in result["resolved_binaries"]
    assert result["exit_code"] != 127
    assert (tmp_path / "audit.log").exists()


def test_run_command_confirmation_required_in_allowlist_mode(tmp_path: Path) -> None:
    # CONFIRMATION_COMMANDS is now a generic, stack-agnostic list (docker
    # compose / systemctl) instead of project-specific scripts/vitest configs.
    settings = make_settings(tmp_path)

    for command in ["docker compose up -d", "systemctl restart nginx"]:
        try:
            run_command(command, settings)
            assert False, f"expected confirmation: {command}"
        except ConfirmationRequiredError:
            assert True


def test_run_command_confirmation_required_for_destructive_in_guarded_mode(tmp_path: Path) -> None:
    # guarded is the new default policy mode: shell operators are allowed,
    # but destructive_words patterns (rm -rf, git reset --hard, docker
    # system prune, ...) require confirmed=true.
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "guarded"})

    for command in ["rm -rf ./scratch", "git reset --hard HEAD~1", "docker system prune -f"]:
        try:
            run_command(command, settings)
            assert False, f"expected confirmation: {command}"
        except ConfirmationRequiredError:
            assert True


def test_run_command_blocks_raw_git_push_in_every_mode(tmp_path: Path) -> None:
    # git_push (git_workflow_tools) is the single audited door for a real push: raw
    # `git push` through run_command is blocked outright, in every command_policy_mode,
    # even with confirmed=true and even in the otherwise-unrestricted full_repo mode.
    settings = make_settings(tmp_path)

    for mode in ("guarded", "allowlist", "full_repo"):
        mode_settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": mode})
        for command in ["git push", "git push --force origin main", "git push origin main"]:
            try:
                run_command(command, mode_settings, confirmed=True)
                assert False, f"expected raw git push to be blocked in mode={mode}: {command}"
            except CommandPolicyError as exc:
                assert "git_push" in str(exc)


def test_guarded_mode_denies_words_by_first_token_only(tmp_path: Path) -> None:
    # Regression test for the old bug where DENIED_WORDS matched any token
    # anywhere in the command (so `git log --grep curl` was falsely
    # blocked). _deny_check now only matches the first executable token of
    # each `;`/`|`/`&&`/`||` segment.
    settings = make_settings(tmp_path)
    settings = settings.__class__(
        **{**settings.__dict__, "command_policy_mode": "guarded", "denied_words": ("sudo", "su", "curl")}
    )
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    ok_result = run_command("git log --grep curl", settings)
    assert ok_result["exit_code"] == 0

    try:
        run_command("curl https://example.com", settings)
        assert False, "expected curl itself to be denied"
    except CommandPolicyError:
        assert True

    try:
        run_command("sudo rm -rf /", settings, confirmed=True)
        assert False, "expected sudo to be denied even when confirmed"
    except CommandPolicyError:
        assert True


def test_guarded_mode_is_unrestricted_when_denied_words_is_empty(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "guarded", "denied_words": ()})

    result = run_command("node --version", settings, confirmed=True)

    assert result["exit_code"] != 127


def test_full_repo_mode_does_not_require_confirmation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo", "kill_grace_ms": 25})
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
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo", "kill_grace_ms": 25})

    started = start_command_job("bash -lc 'sleep 10 & wait'", settings, timeout_ms=50)
    import time

    deadline = time.time() + 3
    status = get_job_status(started["job_id"], settings)
    while status["status"] in {"running", "terminating"} and time.time() < deadline:
        time.sleep(0.05)
        status = get_job_status(started["job_id"], settings)

    assert status["status"] == "timed_out"
    assert status["timed_out"] is True
    assert status["process_alive"] is False
    assert status["kill_status"] in {"terminated", "killed", "not_running"}


def test_run_test_preset_resolves_bare_action_at_workspace_root(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'root'\n")
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    settings = make_settings(tmp_path)

    result = run_test_preset("test", settings)

    assert result["command"] == "pytest -x -q"
    assert result["resolved_action"] == "test"
    assert result["resolved_cwd"] == ""
    assert result["ok"] is True
    assert result["parsed"]["kind"] == "pytest"


def test_run_test_preset_resolves_composite_service_action(tmp_path: Path) -> None:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "pyproject.toml").write_text("[project]\nname = 'svc'\n")
    (tmp_path / "svc" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    settings = make_settings(tmp_path)

    result = run_test_preset("svc:test", settings)

    assert result["command"] == "pytest -x -q"
    assert result["resolved_action"] == "test"
    assert result["resolved_cwd"] == "svc"
    assert result["cwd"].endswith("svc")
    assert result["ok"] is True


def test_run_test_preset_unknown_action_lists_available_actions(tmp_path: Path) -> None:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "go.mod").write_text("module example.com/svc\n\ngo 1.21\n")
    settings = make_settings(tmp_path)

    try:
        run_test_preset("svc:bogus", settings)
        raise AssertionError("expected CommandPolicyError")
    except CommandPolicyError as exc:
        message = str(exc)
        assert "svc" in message
        assert "test" in message
        assert "lint" in message


def test_run_test_preset_profile_override_wins_for_matching_cwd(tmp_path: Path) -> None:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "pyproject.toml").write_text("[project]\nname = 'svc'\n")
    chatrepo_dir = tmp_path / ".chatrepo"
    chatrepo_dir.mkdir()
    (chatrepo_dir / "mcp.yml").write_text("presets:\n  test:\n    command: echo overridden\n    cwd: svc\n")
    settings = make_settings(tmp_path)

    result = run_test_preset("svc:test", settings)

    assert result["command"] == "echo overridden"
    assert result["resolved_cwd"] == "svc"
    assert result["ok"] is True


def test_run_test_preset_named_profile_preset_still_works(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    result = run_test_preset("git_diff_check", settings)

    assert result["command"] == "git diff --check"
    assert result["resolved_action"] == "git_diff_check"
    assert result["ok"] is True


def test_list_test_presets_without_path_summarizes_workspace(tmp_path: Path) -> None:
    (tmp_path / "go-svc").mkdir()
    (tmp_path / "go-svc" / "go.mod").write_text("module example.com/go-svc\n\ngo 1.21\n")
    (tmp_path / "py-svc").mkdir()
    (tmp_path / "py-svc" / "pyproject.toml").write_text("[project]\nname = 'py-svc'\n")
    settings = make_settings(tmp_path)

    summary = list_test_presets(settings)

    repos_by_path = {repo["path"]: repo for repo in summary["repos"]}
    assert set(repos_by_path["go-svc"]["actions"]) == {"test", "lint", "build", "format"}
    assert set(repos_by_path["py-svc"]["actions"]) == {"test", "lint", "typecheck", "format"}
    assert "git_diff_check" in summary["presets"]


def test_list_test_presets_with_path_resolves_directory(tmp_path: Path) -> None:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "pyproject.toml").write_text("[project]\nname = 'svc'\n")
    settings = make_settings(tmp_path)

    resolved = list_test_presets(settings, path="svc")

    assert resolved["path"] == "svc"
    assert resolved["presets"]["test"]["command"] == "pytest -x -q"
    assert resolved["presets"]["test"]["cwd"] == "svc"
