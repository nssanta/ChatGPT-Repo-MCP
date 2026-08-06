from __future__ import annotations

import os
import subprocess
import sys
import time
import tracemalloc
from dataclasses import replace

import pytest
from test_command_tools import make_settings

from chatrepo_mcp import bounded_subprocess, git_tools, github_tools, index_tools, lsp_tools
from chatrepo_mcp.bounded_subprocess import BoundedProcessResult, run_bounded
from chatrepo_mcp.output_store import (
    ArtifactPersistenceError,
    ArtifactQuotaError,
    OutputArtifact,
    artifact_reference,
    read_artifact,
    store_for,
)


def _git(cwd, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_generic_capture_drains_large_output_with_bounded_python_memory(tmp_path) -> None:
    payload_bytes = 2 * 1024 * 1024
    tracemalloc.start()
    result = run_bounded(
        [sys.executable, "-c", f"import sys;sys.stdout.write('x'*{payload_bytes})"],
        cwd=tmp_path, timeout=10, max_stdout_bytes=1024, max_stderr_bytes=1024,
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result.returncode == 0
    assert result.stdout_bytes == payload_bytes
    assert len(result.stdout.encode()) <= 1024
    assert result.stdout_truncated is True
    assert peak < 2 * 1024 * 1024


def test_generic_capture_redacts_split_secrets_before_retention(tmp_path) -> None:
    settings = make_settings(tmp_path)
    script = """
import os
parts = [
    b"token=split-secret-value ",
    b"-----BEGIN PRIVATE KEY-----\\n",
    b"private-material\\n",
    b"-----END PRIVATE KEY-----\\n",
    "ééé".encode(),
]
for part in parts:
    for byte in part:
        os.write(1, bytes([byte]))
"""
    result = run_bounded(
        [sys.executable, "-c", script], cwd=tmp_path, timeout=10,
        max_stdout_bytes=65, max_stderr_bytes=65,
        artifact_settings=settings,
    )

    assert "split-secret-value" not in result.stdout
    assert "private-material" not in result.stdout
    assert "token=<redacted>" in result.stdout
    assert "[REDACTED PRIVATE KEY]" in result.stdout
    assert "�" not in result.stdout
    assert result.artifact is not None
    page = read_artifact(result.artifact["artifact_id"], settings, max_bytes=4_096)
    artifact_text = "".join(
        record["data"] for record in page["payload"]["records"] if record["stream"] == "stdout"
    )
    assert "split-secret-value" not in artifact_text
    assert "private-material" not in artifact_text


def test_large_stdin_that_child_never_reads_obeys_timeout(tmp_path) -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path, timeout=0.2, max_stdout_bytes=64,
            input_text="x" * (32 * 1024 * 1024),
        )
    assert time.monotonic() - started < 3


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group regression")
def test_timeout_kills_grandchild_that_inherits_output_pipes(tmp_path) -> None:
    script = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(
            [sys.executable, "-c", script], cwd=tmp_path, timeout=0.2,
            max_stdout_bytes=64,
        )
    assert time.monotonic() - started < 3


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX inherited-pipe regression")
def test_exited_parent_cannot_leave_inherited_pipe_hanging(tmp_path) -> None:
    script = (
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
    )
    started = time.monotonic()
    result = run_bounded(
        [sys.executable, "-c", script], cwd=tmp_path, timeout=5,
        max_stdout_bytes=64,
    )
    assert result.returncode == 0
    assert time.monotonic() - started < 3


def test_windows_tree_kill_uses_bounded_taskkill_seam(monkeypatch) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bounded_subprocess.subprocess, "run", fake_run)
    bounded_subprocess._terminate_windows_tree(4242)
    command = captured["command"]
    assert command[0] == str(bounded_subprocess._windows_taskkill_path())
    assert command[1:] == ["/PID", "4242", "/T", "/F"]
    assert bounded_subprocess._windows_taskkill_path().is_absolute()
    assert captured["timeout"] == 5
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL


def test_windows_job_setup_failure_kills_suspended_process(monkeypatch) -> None:
    events = []

    class FakeProcess:
        def kill(self) -> None:
            events.append("kill")

        def wait(self) -> int:
            events.append("wait")
            return 1

    def fail_job_setup(_process) -> None:
        raise OSError("injected Job Object setup failure")

    monkeypatch.setattr(bounded_subprocess, "_create_windows_job", fail_job_setup)
    with pytest.raises(OSError, match="injected Job Object setup failure"):
        bounded_subprocess._setup_windows_process(FakeProcess())
    assert events == ["kill", "wait"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree CI proof")
def test_windows_timeout_kills_descendant_tree(tmp_path) -> None:
    script = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        "time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(
            [sys.executable, "-c", script], cwd=tmp_path, timeout=0.2,
            max_stdout_bytes=64,
        )
    assert time.monotonic() - started < 7


@pytest.mark.skipif(sys.platform != "win32", reason="Windows inherited-pipe CI proof")
def test_windows_exited_parent_cannot_leave_inherited_pipe_or_descendant(tmp_path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    grandchild = (
        "import os,pathlib,sys,time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    parent = (
        "import subprocess,sys;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r},sys.argv[1]])"
    )
    started = time.monotonic()
    result = run_bounded(
        [sys.executable, "-c", parent, str(pid_file)], cwd=tmp_path, timeout=5,
        max_stdout_bytes=64,
    )
    assert result.returncode == 0
    assert time.monotonic() - started < 7
    descendant_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(descendant_pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"Windows descendant {descendant_pid} survived owned job termination")


@pytest.mark.parametrize("limit", range(1, 13))
def test_utf8_preview_is_valid_at_every_truncation_boundary(tmp_path, limit: int) -> None:
    source = "😀яéz"
    result = run_bounded(
        [sys.executable, "-c", f"print({source!r}, end='')"],
        cwd=tmp_path, timeout=5, max_stdout_bytes=limit,
    )
    assert "�" not in result.stdout
    assert len(result.stdout.encode()) <= limit
    assert source.startswith(result.stdout)
    assert result.stdout_truncated is (len(source.encode()) > len(result.stdout.encode()))


def test_partial_artifact_reference_separates_inline_and_source_completion() -> None:
    reference = artifact_reference("a", complete=True, reason="inline_limit")
    assert reference["has_more"] is True
    assert reference["eof"] is False
    assert reference["receipt"]["completeness"] == "partial"
    assert reference["receipt"]["applied"]["source_complete"] is True


def test_artifact_quota_failure_cleans_both_streams_and_active_state(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), artifact_total_bytes=1, artifact_max_bytes=1,
        artifact_disk_reserve_bytes=0,
    )
    with pytest.raises(ArtifactQuotaError):
        run_bounded(
            [sys.executable, "-c", "print('x'*9000)"], cwd=tmp_path, timeout=5,
            max_stdout_bytes=64, artifact_settings=settings,
        )
    store = store_for(settings)
    assert store._active == {}
    assert not list(store.root.glob("subprocess-*"))


@pytest.mark.parametrize("message", ["injected ENOSPC", "short artifact write: 1/2"])
def test_artifact_persistence_failure_never_writes_completed_manifest(
    tmp_path, monkeypatch, message: str,
) -> None:
    settings = replace(make_settings(tmp_path), artifact_disk_reserve_bytes=0)
    original = OutputArtifact._persist

    def fail(self, data: bytes) -> None:
        if data:
            raise ArtifactPersistenceError(message)
        original(self, data)

    monkeypatch.setattr(OutputArtifact, "_persist", fail)
    with pytest.raises(ArtifactPersistenceError, match=message.split(":", 1)[0]):
        run_bounded(
            [sys.executable, "-c", "print('x'*9000)"], cwd=tmp_path, timeout=5,
            max_stdout_bytes=64, artifact_settings=settings,
        )
    store = store_for(settings)
    assert store._active == {}
    assert not list(store.root.glob("subprocess-*"))


def test_large_git_diff_and_status_expose_truncation_truth(tmp_path) -> None:
    _git(tmp_path, "init", "-q")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-qm", "init")
    tracked.write_text("base\n" + "large-diff-line\n" * 10_000, encoding="utf-8")
    for index in range(100):
        (tmp_path / f"untracked-{index:03d}-with-a-long-name.txt").write_text("x", encoding="utf-8")
    settings = replace(make_settings(tmp_path), max_diff_bytes=256, max_response_chars=256)

    diff = git_tools.git_diff(settings)
    status = git_tools.git_status(settings)

    assert diff["truncated"] is True
    assert status["truncated"] is True
    assert len(diff["diff"].encode()) < 300
    assert len(status["status"].encode()) < 300
    assert diff["continuation"]["tool"] == "read_artifact"

    cursor = None
    stdout_parts: list[str] = []
    while True:
        page = read_artifact(diff["artifact"]["artifact_id"], settings, cursor=cursor, max_bytes=257)
        payload = page["payload"]
        assert payload["type"] == "records"
        stdout_parts.extend(
            record["data"] for record in payload["records"] if record["stream"] == "stdout"
        )
        if page["eof"]:
            break
        cursor = page["next_cursor"]
        assert cursor
    assert "large-diff-line" in "".join(stdout_parts)


def test_diagnostics_exposes_checker_output_truncation(tmp_path, monkeypatch) -> None:
    settings = replace(make_settings(tmp_path), max_response_chars=64)
    monkeypatch.setattr(lsp_tools, "_find_tsc", lambda _cwd: "/fake/tsc")
    monkeypatch.setattr(
        lsp_tools,
        "run_bounded",
        lambda *args, **kwargs: BoundedProcessResult(
            args=["tsc"], returncode=2,
            stdout="bad.ts(1,1): error TS1000: broken\n", stderr="",
            stdout_bytes=10_000, stderr_bytes=0,
            stdout_truncated=True, stderr_truncated=False,
        ),
    )

    result = lsp_tools.code_diagnostics(settings, language="ts", limit=10)

    assert result["output_truncated"] is True
    assert result["diagnostics"][0]["code"] == "TS1000"


def test_github_refuses_to_parse_truncated_json() -> None:
    data, error = github_tools._json_or_error(
        {"ok": True, "stdout": '[{"id": 1}', "output_truncated": True}, default=[],
    )

    assert data == []
    assert error is not None
    assert error["error_kind"] == "gh_output_truncated"


def test_ctags_refuses_to_parse_truncated_ndjson(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(index_tools, "_ctags_binary", lambda: "/fake/ctags")
    monkeypatch.setattr(
        index_tools,
        "run_bounded",
        lambda *args, **kwargs: BoundedProcessResult(
            args=["ctags"], returncode=0,
            stdout='{"_type":"tag","name":"partial"}\n', stderr="",
            stdout_bytes=10_000, stderr_bytes=0,
            stdout_truncated=True, stderr_truncated=False,
        ),
    )

    with pytest.raises(index_tools.CtagsIncompleteError):
        index_tools._run_ctags_recursive(tmp_path, max_bytes=64)
