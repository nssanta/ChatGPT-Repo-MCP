from __future__ import annotations

import contextvars
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from chatrepo_mcp.config import Settings
from chatrepo_mcp.fs_tools import recent_changes, tree
from chatrepo_mcp.operations import CURRENT, IDENTITY, OperationCancelled, Registry, registry_for, track
from chatrepo_mcp.resource_profile import acquire_heavy_operation, list_heavy_operations


def settings_for(root: Path) -> Settings:
    return replace(Settings.from_env(), project_root=root, command_audit_log_path=root / "audit.log",
                   command_jobs_dir=root / "jobs", maintenance_enabled=False)


def test_cancel_remains_active_until_worker_stops_and_is_idempotent(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    registry = registry_for(settings)
    op = registry.start("find_files", {"path": "."})
    called = threading.Event()
    op.add_cancel("worker", called.set)
    assert registry.cancel(op.id)["status"] == "cancelling"
    assert called.wait(2)
    assert registry.cancel(op.id)["cancel_requested"]
    assert registry.list()["count"] == 1
    with pytest.raises(OperationCancelled):
        op.checkpoint()
    op.finish("cancelled")
    assert registry.list()["count"] == 0
    assert registry.get(op.id)["operation"]["status"] == "cancelled"
    assert registry.cancel(op.id)["status"] == "cancelled"
    events = [json.loads(line) for line in settings.command_audit_log_path.read_text().splitlines()]
    assert [event["event"] for event in events] == ["operation_started", "operation_cancel_requested", "operation_finished"]


def test_sessions_children_and_heavy_slots_are_independent(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    registry = registry_for(settings)
    token = IDENTITY.set({"session_id": "session-a", "client": {"name": "test", "version": "1"}})
    try:
        with track(settings, "batch_call", {}) as parent:
            with track(settings, "recent_changes", {"path": "."}) as child:
                assert child.parent is parent
                assert list_heavy_operations(settings)["used"] == 0
                assert registry.list("session")["count"] == 2
                other = IDENTITY.set({"session_id": "session-b"})
                try:
                    assert registry.list("session")["count"] == 0
                    assert registry.list()["count"] == 2
                finally:
                    IDENTITY.reset(other)
                with pytest.raises(OperationCancelled):
                    registry.cancel(parent.id)
                    child.checkpoint()
    except OperationCancelled:
        pass
    finally:
        IDENTITY.reset(token)
    assert registry.list()["count"] == 0


def test_noninterruptible_mutation_and_bounded_history(tmp_path: Path) -> None:
    registry = Registry(settings_for(tmp_path))
    active = registry.start("write_text_file", {"command": "secret", "env": {"TOKEN": "secret"}})
    assert registry.cancel(active.id)["error_kind"] == "operation_not_cancellable"
    assert active.data["target"] == {"project_root": str(tmp_path)}
    for _ in range(1002):
        op = registry.start("file_metadata", {})
        op.finish("completed")
    history = registry.list(include_finished=True, limit=1000)
    assert history["total"] == 1001  # 1000 completed plus the non-evictable active write.
    assert registry.get(active.id)["ok"]
    active.finish("completed")
    assert Registry(registry.settings).get(active.id)["error_kind"] == "operation_not_found"


def test_walk_cancellation_preserves_results_without_cancel(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    for index in range(15):
        (source / f"file-{index}.txt").write_text("hello")
    baseline = recent_changes(settings, path="source", limit=5)
    with track(settings, "recent_changes", {"path": "."}) as op:
        result = recent_changes(settings, path="source", limit=5)
        assert result == baseline
        assert op.data["progress"]["files"] >= 15
        op.registry.cancel(op.id)
        with pytest.raises(OperationCancelled):
            recent_changes(settings, path="source")
    with track(settings, "tree", {"path": "."}) as op:
        op.registry.cancel(op.id)
        with pytest.raises(OperationCancelled):
            tree(".", settings)


def test_heavy_lease_links_existing_operation(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with track(settings, "run_command", {}) as op:
        lease = acquire_heavy_operation(settings, tool="run_command")
        assert list_heavy_operations(settings)["operations"][0]["tracking_operation_id"] == op.id
        lease.release()
        assert not list_heavy_operations(settings)["used"]


def test_context_propagation_does_not_share_mutable_context(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with track(settings, "batch_call", {}) as parent:
        context = contextvars.copy_context()
        seen = []
        thread = threading.Thread(target=lambda: context.run(lambda: seen.append(CURRENT.get())))
        thread.start()
        thread.join()
        assert seen == [parent]


def test_expired_history_and_shutdown(tmp_path: Path) -> None:
    registry = Registry(settings_for(tmp_path))
    finished = registry.start("tree", {})
    finished.finish("completed")
    finished.finished = time.monotonic() - 86401
    active = registry.start("recent_changes", {})
    assert registry.get(finished.id)["error_kind"] == "operation_not_found"
    registry.shutdown()
    assert registry.get(active.id)["operation"]["status"] == "cancelling"
    active.finish("cancelled")


def test_registry_errors_and_late_callback(tmp_path: Path) -> None:
    registry = Registry(settings_for(tmp_path))
    assert registry.list("invalid")["error_kind"] == "invalid_scope"
    assert registry.list("session")["error_kind"] == "session_unavailable"
    assert registry.get("missing")["error_kind"] == "operation_not_found"
    assert registry.cancel("missing")["error_kind"] == "operation_not_found"
    op = registry.start("tree", {})
    registry.cancel(op.id)
    called = threading.Event()
    op.add_cancel("late", called.set)
    assert called.is_set()
    op.remove_cancel("late")
    op.finish("cancelled")
    op.finish("failed")
    assert registry.get(op.id)["operation"]["status"] == "cancelled"


def test_fastmcp_control_stays_responsive_and_thread_cancel_is_joined(tmp_path: Path, monkeypatch) -> None:
    import anyio
    from chatrepo_mcp import server
    from chatrepo_mcp.operations import checkpoint

    settings = settings_for(tmp_path)
    monkeypatch.setattr(server, "settings", settings)
    started = threading.Event()
    exited = threading.Event()

    def slow_walk(**kwargs):
        started.set()
        try:
            while True:
                checkpoint(phase="walking", files=1)
                time.sleep(0.01)
        finally:
            exited.set()

    monkeypatch.setattr(server, "recent_changes", slow_walk)

    async def exercise() -> None:
        cancelled = anyio.Event()

        async def invoke() -> None:
            tool = server.mcp._tool_manager.get_tool("recent_changes")
            result = await tool.run({}, convert_result=True)
            assert result[1]["error_kind"] == "operation_cancelled"
            cancelled.set()

        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            while not started.is_set():
                await anyio.sleep(0.01)
            tool = server.mcp._tool_manager.get_tool("list_operations")
            result = await tool.run({}, convert_result=True)
            op = result[1]["operations"][0]
            cancel = server.mcp._tool_manager.get_tool("cancel_operation")
            await cancel.run({"operation_id": op["operation_id"]}, convert_result=True)
            with anyio.fail_after(3):
                await cancelled.wait()
            assert exited.is_set()
            assert registry_for(settings).get(op["operation_id"])["operation"]["status"] == "cancelled"

    anyio.run(exercise)


def test_fastmcp_mcp_cancellation_waits_for_worker_and_exception_is_finished(tmp_path: Path, monkeypatch) -> None:
    import anyio
    from chatrepo_mcp import server
    from chatrepo_mcp.operations import checkpoint

    settings = settings_for(tmp_path)
    monkeypatch.setattr(server, "settings", settings)
    started = threading.Event()
    exited = threading.Event()

    def slow_walk(**kwargs):
        started.set()
        try:
            while True:
                checkpoint()
                time.sleep(0.01)
        finally:
            exited.set()

    monkeypatch.setattr(server, "recent_changes", slow_walk)

    async def exercise() -> None:
        async with anyio.create_task_group() as group:
            async def invoke() -> None:
                await server.mcp._tool_manager.get_tool("recent_changes").run({})
            group.start_soon(invoke)
            while not started.is_set():
                await anyio.sleep(0.01)
            group.cancel_scope.cancel()
        assert exited.is_set()
        assert registry_for(settings).list()["count"] == 0

    anyio.run(exercise)

    def fail(**kwargs):
        raise ValueError("expected fixture failure")
    monkeypatch.setattr(server, "recent_changes", fail)
    async def invoke_failed() -> None:
        with pytest.raises(Exception, match="expected fixture failure"):
            await server.mcp._tool_manager.get_tool("recent_changes").run({})
    anyio.run(invoke_failed)
    history = registry_for(settings).list(include_finished=True)
    assert history["operations"][0]["status"] == "failed"


def test_terminal_reader_start_failure_releases_process_and_operation(tmp_path: Path, monkeypatch) -> None:
    import os
    from chatrepo_mcp import terminal_tools

    if os.name != "posix":
        pytest.skip("POSIX PTY lifecycle")
    settings = replace(settings_for(tmp_path), access_mode="full", command_policy_mode="unrestricted")

    def fail_reader(*args):
        raise OSError("fixture capture could not be opened")
    monkeypatch.setattr(terminal_tools, "_reader", fail_reader)
    result = terminal_tools.start_terminal_session(settings, command="sleep 30")
    session = terminal_tools.SESSIONS[result["session_id"]]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and registry_for(settings).list()["count"]:
        time.sleep(0.01)
    assert session.process.poll() is not None
    assert session.status == "failed"
    assert list_heavy_operations(settings)["used"] == 0
    assert registry_for(settings).list()["count"] == 0
    assert registry_for(settings).list(include_finished=True)["operations"][0]["status"] == "failed"


def test_timeout_does_not_publish_terminal_metadata_before_kill_outcome(tmp_path: Path, monkeypatch) -> None:
    import os
    from chatrepo_mcp import command_tools

    if os.name != "posix":
        pytest.skip("POSIX process groups")
    settings = replace(settings_for(tmp_path), access_mode="full", command_policy_mode="unrestricted", kill_grace_ms=25)
    sent, release, premature = threading.Event(), threading.Event(), threading.Event()
    terminate = command_tools._terminate_process_group
    write_meta = command_tools._write_job_meta

    def slow_termination(pid, *, grace_seconds=1):
        outcome = terminate(pid, grace_seconds=grace_seconds)
        sent.set()
        release.wait(3)
        return outcome

    def observe_metadata(settings, job_id, meta):
        if meta.get("status") == "timed_out" and not meta.get("kill_status"):
            premature.set()
        write_meta(settings, job_id, meta)

    monkeypatch.setattr(command_tools, "_terminate_process_group", slow_termination)
    monkeypatch.setattr(command_tools, "_write_job_meta", observe_metadata)
    started = command_tools.start_command_job("sleep 30", settings, timeout_ms=50)
    try:
        assert sent.wait(3)
        assert not premature.wait(0.2)
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and registry_for(settings).list()["count"]:
        time.sleep(0.01)
    status = command_tools.get_job_status(started["job_id"], settings)
    assert status["status"] == "timed_out"
    assert status["kill_status"] in {"terminated", "killed", "not_running"}
    assert registry_for(settings).list()["count"] == 0


def test_legacy_heavy_cancel_keeps_specialized_job_semantics(tmp_path: Path) -> None:
    from chatrepo_mcp.resource_profile import cancel_heavy_operation

    settings = settings_for(tmp_path)
    lease = acquire_heavy_operation(settings, tool="start_command_job", cancel_tool="cancel_command_job", cancel_id="job-fixture")
    called = threading.Event()
    lease.set_cancel(called.set)
    assert not list_heavy_operations(settings)["operations"][0]["cancellable"]
    assert cancel_heavy_operation(settings, lease.operation_id)["error_kind"] == "specialized_cancel_required"
    assert not called.is_set()
    assert registry_for(settings).cancel(lease.tracked.id)["ok"]
    assert called.wait(2)
    lease.release()


def test_command_audit_correlates_native_log_with_operation_session(tmp_path: Path) -> None:
    from chatrepo_mcp.command_tools import _audit

    settings = settings_for(tmp_path)
    token = IDENTITY.set({"session_id": "public-session"})
    try:
        with track(settings, "run_command", {}) as op:
            _audit(settings, {"event": "command_started", "request_id": "native-log-id"})
        events = [json.loads(line) for line in settings.command_audit_log_path.read_text().splitlines()]
        command = next(event for event in events if event["event"] == "command_started")
        assert command["request_id"] == "native-log-id"
        assert command["operation_id"] == op.id
        assert command["session_id"] == "public-session"
    finally:
        IDENTITY.reset(token)
