from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from chatrepo_mcp import server
from chatrepo_mcp.command_tools import cancel_command_job, list_command_jobs, start_command_job
from chatrepo_mcp.git_workflow_tools import prepare_task_worktree
from chatrepo_mcp.lsp_tools import code_diagnostics
from chatrepo_mcp.resource_profile import ResourceBusyError
from chatrepo_mcp.runtime_env import effective_path, tool_status
from chatrepo_mcp.terminal_tools import (
    close_terminal_session,
    read_terminal_session,
    resize_terminal_session,
    start_terminal_session,
    write_terminal_session,
)


def settings_for(tmp_path: Path, **changes):
    return replace(
        server.settings,
        project_root=tmp_path,
        workspace_roots=(),
        command_jobs_dir=tmp_path / "jobs",
        command_audit_log_path=tmp_path / "audit.log",
        command_policy_mode="full_repo",
        access_mode="full",
        enable_pty=True,
        kill_grace_ms=25,
        **changes,
    )


def test_effective_path_prefers_explicit_and_reports_tool(tmp_path: Path, monkeypatch) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    binary = extra / "demo-tool"
    binary.write_text("#!/bin/sh\necho demo-1\n", encoding="utf-8")
    binary.chmod(0o755)
    settings = settings_for(tmp_path, mcp_extra_path=(str(extra),))
    monkeypatch.setenv("PATH", "/usr/bin")

    paths, sources, warnings = effective_path(settings)
    status = tool_status("demo-tool", settings)

    assert paths[0] == str(extra)
    assert sources[str(extra)] == "explicit_extra"
    assert warnings == []
    assert status["available"] is True
    assert status["source"] == "explicit_extra"
    assert status["version"] == "demo-1"


def test_code_diagnostics_runs_checker_from_mcp_extra_path(tmp_path: Path, monkeypatch) -> None:
    checker_dir = tmp_path / "checkers"
    checker_dir.mkdir()
    checker = checker_dir / "pyright"
    checker.write_text(
        "#!/bin/sh\nprintf '%s' '{\"generalDiagnostics\":[]}'\n",
        encoding="utf-8",
    )
    checker.chmod(0o755)
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    settings = settings_for(tmp_path, mcp_extra_path=(str(checker_dir),))
    monkeypatch.setenv("PATH", "/usr/bin")

    result = code_diagnostics(settings, language="python")

    assert result["tool_used"] == ["pyright --outputjson"]
    assert result["missing_tools"] == []


def test_job_cancel_kills_process_group_and_lists_job(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    child_pid = tmp_path / "child.pid"
    started = start_command_job(
        f"sleep 30 & child=$!; echo $child > {child_pid}; wait",
        settings,
        timeout_ms=10_000,
        concurrency_key="group-test",
    )
    deadline = time.time() + 2
    while not child_pid.exists() and time.time() < deadline:
        time.sleep(0.01)
    pid = int(child_pid.read_text().strip())

    cancelled = cancel_command_job(started["job_id"], settings)
    listed = list_command_jobs(settings, status=["cancelled"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["process_group_cleaned"] is True
    assert any(item["job_id"] == started["job_id"] for item in listed["jobs"])
    with __import__("pytest").raises(ProcessLookupError):
        os.kill(pid, 0)


def test_terminal_cursor_write_resize_and_exit(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    started = start_terminal_session(settings, command="read value; echo got:$value", idle_timeout_ms=5000)
    artifact = started["artifact"]
    assert artifact["has_more"] is True
    assert artifact["eof"] is False
    assert artifact["continuation"] == {
        "tool": "read_artifact", "arguments": {"artifact_id": started["log_id"]},
    }
    assert artifact["receipt"]["status"] == "partial"
    assert artifact["receipt"]["completeness"] == "partial"
    assert artifact["receipt"]["reason"] == "source_active"
    assert artifact["receipt"]["applied"]["source_complete"] is False
    write_terminal_session(started["session_id"], data="hello\n")
    resize = resize_terminal_session(started["session_id"], cols=100, rows=30)
    cursor = 0
    data = ""
    deadline = time.time() + 2
    while "got:hello" not in data and time.time() < deadline:
        chunk = read_terminal_session(started["session_id"], settings, cursor=cursor, wait_ms=250)
        data += chunk["data"]
        cursor = chunk["next_cursor"]
    second = read_terminal_session(started["session_id"], settings, cursor=cursor, wait_ms=100)
    closed = close_terminal_session(started["session_id"], settings)

    assert resize["cols"] == 100 and resize["rows"] == 30
    assert "got:hello" in data
    assert second["data"] == ""
    assert closed["status"] in {"exited", "closed"}


def test_prepare_task_worktree_uses_exact_committed_base(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    settings = settings_for(tmp_path)

    result = prepare_task_worktree(
        settings, branch="agent/test", task_name="test-task", base="main",
        dry_run=False, confirmed=True,
    )

    assert result["base_sha"] == base_sha
    assert result["parent_dirty"] is True
    assert (Path(result["worktree_path"]) / "tracked.txt").read_text() == "base\n"


def test_batch_call_parallel_is_default_and_preserves_order(monkeypatch) -> None:
    def slow(tool, args):
        time.sleep(0.12)
        return {"tool": tool, "value": args["value"]}

    monkeypatch.setattr(server, "_batch_dispatch", slow)
    started = time.monotonic()
    result = server.batch_call_tool(
        calls=[{"tool": "repo_info", "args": {"value": index}} for index in range(4)]
    )

    assert time.monotonic() - started < 0.35
    assert result["execution"] == "parallel"
    assert [item["result"]["value"] for item in result["results"]] == [0, 1, 2, 3]


def test_batch_call_keeps_light_parallelism_separate_from_heavy_capacity(monkeypatch) -> None:
    monkeypatch.setattr(server, "settings", replace(server.settings, max_heavy_operations=2))
    active = 0
    peak = 0
    lock = threading.Lock()

    def measured(tool, args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"tool": tool, "args": args}

    monkeypatch.setattr(server, "_batch_dispatch", measured)
    result = server.batch_call_tool(
        calls=[{"tool": "repo_info", "args": {}} for _ in range(6)],
        max_concurrency=6,
    )
    assert result["max_concurrency"] == 6
    assert result["requested_max_concurrency"] == 6
    assert result["applied_max_concurrency"] == 6
    assert result["heavy_capacity"] == 2
    assert peak == 6


def test_batch_call_keeps_mixed_results_order_and_nests_heavy_saturation(monkeypatch) -> None:
    def dispatch(tool, args):
        if tool == "heavy":
            raise ResourceBusyError(2)
        return {"value": args["value"]}

    monkeypatch.setattr(server, "_batch_dispatch", dispatch)
    result = server.batch_call_tool(
        calls=[
            {"tool": "light", "args": {"value": 1}},
            {"tool": "heavy", "args": {}},
            {"tool": "light", "args": {"value": 3}},
        ],
        max_concurrency=3,
    )

    assert result["ok"] is False
    assert [item["index"] for item in result["results"]] == [0, 1, 2]
    assert result["results"][0]["result"] == {"value": 1}
    busy = result["results"][1]
    assert busy["ok"] is False
    assert busy["result"]["error_kind"] == "resource_busy"
    assert busy["result"]["capacity"] == 2
    assert result["results"][2]["result"] == {"value": 3}


def test_background_heavy_starts_fail_closed_at_profile_limit(tmp_path) -> None:
    settings = replace(
        settings_for(tmp_path), command_policy_mode="full_repo", max_heavy_operations=2,
        artifact_disk_reserve_bytes=0,
    )
    first = start_command_job("sleep 5", settings)
    second = start_command_job("sleep 5", settings)
    try:
        with pytest.raises(RuntimeError, match="heavy operation limit reached: 2"):
            start_command_job("sleep 5", settings)
    finally:
        cancel_command_job(first["job_id"], settings)
        cancel_command_job(second["job_id"], settings)
