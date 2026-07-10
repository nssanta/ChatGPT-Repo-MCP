from __future__ import annotations

from pathlib import Path

import time

from chatrepo_mcp import command_tools
from chatrepo_mcp.command_tools import (
    CommandPolicyError,
    ConfirmationRequiredError,
    GitCommitError,
    _bash_command,
    _check_command_policy,
    _effective_tokens,
    _first_exec_token,
    _profile_command_overrides,
    _resolved_binaries,
    _segment_tokens,
    _split_command,
    _resolve_cwd,
    _active_lock_job,
    _command_env,
    _watch_job_timeout,
    cancel_command_job,
    command_policy_check,
    get_command_job,
    get_command_log,
    run_command,
    run_commands,
    run_test_preset,
    start_command_job,
    summarize_command_log,
    git_commit,
)
from chatrepo_mcp.security import SecurityError

from test_command_tools import make_settings


def test_command_token_helpers_cover_parsing_variants() -> None:
    settings = make_settings(Path("/tmp"))

    assert _segment_tokens("A=1 B=2") == ["A=1", "B=2"]
    assert _split_command("A=1", settings) == ["A=1"]
    assert _first_exec_token("A=1") is None
    assert _effective_tokens("VAR=a env -i rm -rf /") == ["rm", "-rf", "/"]
    assert _effective_tokens("VAR=a env -i B=2 rm -rf /") == ["B=2", "rm", "-rf", "/"]


def test_split_command_rejects_invalid_syntax() -> None:
    settings = make_settings(Path("/tmp"))

    try:
        _split_command("echo 'unterminated", settings)
        raise AssertionError("expected invalid command syntax error")
    except CommandPolicyError as exc:
        assert "invalid command syntax" in str(exc)

    try:
        _split_command("", settings)
        raise AssertionError("expected empty command error")
    except CommandPolicyError as exc:
        assert "command must not be empty" in str(exc)


def test_profile_command_overrides_parse_and_parse_failure(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    mcp = tmp_path / ".chatrepo"
    mcp.mkdir()
    (mcp / "mcp.yml").write_text(
        "allowed_commands:\n"
        "  - git status --short\n"
        "  - git diff --name-only\n"
        "confirmation_commands:\n"
        "  - docker compose up\n",
        encoding="utf-8",
    )

    allowed, confirmation = _profile_command_overrides(settings)
    assert {rule.command for rule in allowed} == {"git status --short", "git diff --name-only"}
    assert {rule.allow_suffix for rule in allowed} == {False}
    assert confirmation == ("docker compose up",)

    (mcp / "mcp.yml").write_text("bad", encoding="utf-8")
    assert _profile_command_overrides(settings) == ((), ())


def test_command_check_policy_unknown_mode_and_policy_exempt_destructive(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    unknown = settings.__class__(**{**settings.__dict__, "command_policy_mode": "mystery"})

    try:
        _check_command_policy("git status --short", unknown)
        raise AssertionError("expected unknown mode error")
    except CommandPolicyError as exc:
        assert "unknown command_policy_mode" in str(exc)

    destructive_policy = settings.__class__(
        **{**settings.__dict__, "command_policy_mode": "allowlist", "destructive_words": ("rm -rf",)}
    )
    try:
        _check_command_policy("rm -rf ./tmp", destructive_policy, policy_exempt=True)
        raise AssertionError("expected confirmation for policy exempt destructive command")
    except ConfirmationRequiredError as exc:
        assert "destructive" in str(exc)


def test_resolve_cwd_and_command_env_and_bash_command_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    try:
        _resolve_cwd("../outside", settings)
        raise AssertionError("expected cwd escape error")
    except SecurityError:
        pass

    try:
        _resolve_cwd("does-not-exist", settings)
        raise AssertionError("expected cwd not dir error")
    except CommandPolicyError as exc:
        assert "cwd is not a directory" in str(exc)

    prelude = settings.__class__(**{**settings.__dict__, "command_shell_prelude": "cd /tmp"})
    assert _bash_command("echo ok", settings) == "echo ok"
    assert _bash_command("echo ok", prelude) == "cd /tmp\necho ok"

    try:
        _command_env({"bad-key!": "x"})
        raise AssertionError("expected invalid env key error")
    except CommandPolicyError as exc:
        assert "invalid env key" in str(exc)


def test_run_commands_stop_on_failure_and_policy_check_path_split(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    result = run_commands(["rm -rf /", "echo will-not-run"], settings, stop_on_failure=True)
    assert result["count"] == 1
    assert result["ok"] is False
    assert result["results"][0]["error_kind"] == "command_not_allowed"

    full_repo = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})
    semicolon = command_policy_check("git status --short; git diff --name-only", full_repo)
    assert semicolon["allowed"] is True
    assert semicolon["safe_split"] == ["git status --short", "git diff --name-only"]


def test_command_log_missing_file_errors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})
    run_command("printf hi", settings)

    try:
        get_command_log("missing-log-id", settings)
        raise AssertionError("expected missing command log")
    except FileNotFoundError as exc:
        assert "log not found" in str(exc)

    try:
        summarize_command_log("missing-log-id", settings)
        raise AssertionError("expected missing command summary")
    except FileNotFoundError as exc:
        assert "log not found" in str(exc)


def test_start_and_watch_command_job_conflict_and_lock_cleanup(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    try:
        start_command_job("echo ok", settings, on_conflict="bad")
        raise AssertionError("expected invalid on_conflict error")
    except CommandPolicyError as exc:
        assert "on_conflict must be" in str(exc)

    first = start_command_job("sleep 30", settings, concurrency_key="block", on_conflict="fail")
    wait_result = start_command_job("sleep 30", settings, concurrency_key="block", on_conflict="wait", timeout_ms=1)

    assert wait_result["ok"] is False
    assert wait_result["error_kind"] == "job_lock_conflict"
    assert wait_result["lock_status"] == "busy"
    assert wait_result["job_id"] == first["job_id"]

    result = cancel_command_job(first["job_id"], settings)
    assert result["ok"] is True
    assert result["status"] in {"cancelled", "completed"}
    time.sleep(0.05)
    assert _active_lock_job(settings, "block") is None


def test_watch_job_timeout_and_active_lock_cleanup(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(
        **{**settings.__dict__, "command_jobs_dir": tmp_path / "jobs", "command_policy_mode": "full_repo"}
    )

    monkeypatch.setattr(command_tools, "_read_job_meta", lambda *_a, **_k: (_ for _ in ()).throw(OSError("broken")))
    _watch_job_timeout("job", settings, 10)

    meta = {"pid": 999999, "job_id": "job-1", "status": "running", "started_at": 1, "timeout_ms": 10}
    meta_path, _, _ = command_tools._job_paths(settings, "job-1")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "jobs" / "locks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jobs" / "locks" / "shared.json").write_text(
        '{"job_id":"job-1","concurrency_key":"shared"}', encoding="utf-8"
    )
    monkeypatch.setattr(command_tools, "_read_job_meta", lambda *_a, **_k: meta)
    monkeypatch.setattr(command_tools, "_is_pid_running", lambda _pid: False)
    monkeypatch.setattr(command_tools, "_write_job_meta", lambda *a, **k: None)
    assert _active_lock_job(settings, "shared") is None


def test_get_command_job_timed_out_path_and_git_commit_edge_cases(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "command_policy_mode": "full_repo"})

    # Simulate running job past timeout.
    job_id = "deadbeef"
    started_at = 1.0
    meta_path, out_path, err_path = command_tools._job_paths(settings, job_id)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        '{"job_id":"deadbeef","command":"token=secret", "pid":12345, "started_at":'
        + str(started_at)
        + ',"timeout_ms":10,"status":"running","concurrency_key":null}',
        encoding="utf-8",
    )
    out_path.write_text("token=secret", encoding="utf-8")
    err_path.write_text("token=secret", encoding="utf-8")

    monkeypatch.setattr(command_tools.time, "time", lambda: started_at + 20.0)
    monkeypatch.setattr(command_tools, "_is_pid_running", lambda _pid: True)
    monkeypatch.setattr(command_tools, "_terminate_process_group", lambda _pid: "terminated")

    result = get_command_job(job_id, settings)
    assert result["timed_out"] is True
    assert result["status"] == "timed_out"
    assert result["kill_status"] == "terminated"
    assert "<redacted>" in out_path.read_text(encoding="utf-8")
    assert "<redacted>" in err_path.read_text(encoding="utf-8")

    subprocess = command_tools.subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "missions" / "CURRENT.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "missions" / "CURRENT.md").write_text("one\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt", "b.txt"], cwd=tmp_path, check=True, capture_output=True)

    try:
        git_commit("", ["missions/CURRENT.md"], settings)
        raise AssertionError("expected empty commit message")
    except GitCommitError as exc:
        assert "commit message" in str(exc)

    try:
        git_commit("msg", [], settings)
        raise AssertionError("expected empty path error")
    except GitCommitError as exc:
        assert "paths must not be empty" in str(exc)

    try:
        git_commit("msg", ["../outside.txt"], settings)
        raise AssertionError("expected path traversal error")
    except SecurityError:
        pass

    run_test_preset("git_diff_check", settings)

    try:
        git_commit("msg", ["a.txt"], settings)
        raise AssertionError("expected no staged diff conflict")
    except GitCommitError as exc:
        assert "unrelated staged changes exist" in str(exc)


def test_resolved_binaries_uses_cache(monkeypatch, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    calls: list[list[str]] = []
    command_tools._BINARY_RESOLUTION_CACHE.clear()

    class FakeResult:
        returncode = 0
        stdout = "/usr/bin/python3\n"

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return FakeResult()

    monkeypatch.setattr(command_tools, "detect_stack", lambda _cwd: ("python", "node"))
    monkeypatch.setattr(command_tools.subprocess, "run", fake_run)
    first = _resolved_binaries(tmp_path, {}, settings)
    second = _resolved_binaries(tmp_path, {}, settings)

    assert first["python3"] == "/usr/bin/python3"
    assert first == second
    assert any("command -v python3" in " ".join(item) for item in calls)
    assert len(calls) == 5
