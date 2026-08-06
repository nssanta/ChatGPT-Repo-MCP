from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, cast

from .output_store import (
    ArtifactPersistenceError,
    ArtifactQuotaError,
    OutputArtifact,
    StreamingRedactor,
    artifact_reference,
    inline_head_tail,
    store_for,
)


@dataclass(frozen=True)
class BoundedProcessResult:
    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    artifact: dict[str, Any] | None = None

    @property
    def truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated


class _WindowsJob:
    """Own a Windows process tree even after its original parent exits."""

    def __init__(self, kernel32: Any, handle: Any) -> None:
        self._kernel32 = kernel32
        self._handle = handle
        self._closed = False

    def terminate(self) -> None:
        if not self._closed:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._kernel32.CloseHandle(self._handle)


def run_bounded(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    max_stdout_bytes: int,
    max_stderr_bytes: int | None = None,
    max_combined_bytes: int | None = None,
    input_text: str | None = None,
    artifact_settings: Any | None = None,
) -> BoundedProcessResult:
    """Drain both pipes with bounded previews and optional durable full capture."""
    stderr_limit = max_stdout_bytes if max_stderr_bytes is None else max_stderr_bytes
    artifact_id: str | None = None
    artifacts: dict[str, OutputArtifact] = {}
    popen_options: dict[str, Any] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif os.name == "nt":  # pragma: no cover - exercised by Windows CI
        popen_options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )
    process = subprocess.Popen(
        list(args), cwd=str(cwd) if cwd is not None else None, env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **popen_options,
    )
    windows_job = _setup_windows_process(process) if os.name == "nt" else None
    store = store_for(artifact_settings) if artifact_settings is not None else None

    def terminate_tree() -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif windows_job is not None:  # pragma: no cover - exercised by Windows CI
                windows_job.terminate()
            elif os.name == "nt":  # pragma: no cover - exercised by Windows CI
                _terminate_windows_tree(process.pid)
        except ProcessLookupError:
            return

    def abort_capture() -> None:
        for output_artifact in artifacts.values():
            output_artifact.abort()
        if store is not None and artifact_id is not None:
            store.abort_artifact(artifact_id)

    if artifact_settings is not None:
        artifact_id = f"subprocess-{uuid.uuid4().hex}"
        assert store is not None
        try:
            artifacts["stdout"] = store.open(
                artifact_id, "stdout", store.root / f"{artifact_id}.out", max_stdout_bytes,
            )
            artifacts["stderr"] = store.open(
                artifact_id, "stderr", store.root / f"{artifact_id}.err", stderr_limit,
            )
        except (OSError, ValueError):
            terminate_tree()
            process.wait()
            abort_capture()
            if windows_job is not None:
                windows_job.close()
            raise
    captures: dict[str, tuple[bytearray, int, int]] = {}
    drain_errors: list[OSError | ValueError] = []

    def drain(name: str, pipe, limit: int) -> None:
        kept = bytearray()
        raw_total = 0
        redacted_total = 0
        redactor = None if name in artifacts else StreamingRedactor()

        def retain(data: bytes) -> None:
            nonlocal redacted_total
            redacted_total += len(data)
            remaining = max(limit - len(kept), 0)
            if remaining:
                kept.extend(data[:remaining])

        try:
            while chunk := pipe.read(65_536):
                raw_total += len(chunk)
                if name in artifacts:
                    artifacts[name].write(chunk)
                else:
                    assert redactor is not None
                    retain(redactor.feed(chunk))
            if redactor is not None:
                retain(redactor.feed(b"", final=True))
        except (OSError, ValueError) as exc:
            drain_errors.append(exc)
            terminate_tree()
        captures[name] = (kept, raw_total, redacted_total)

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout, max(max_stdout_bytes, 0)), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr, max(stderr_limit, 0)), daemon=True),
    ]
    for thread in threads:
        thread.start()
    stdin_errors: list[OSError] = []
    stdin_fd = os.dup(process.stdin.fileno()) if process.stdin is not None else None
    if process.stdin is not None:
        process.stdin.close()

    def write_stdin() -> None:
        assert stdin_fd is not None
        view = memoryview(input_text.encode("utf-8")) if input_text is not None else memoryview(b"")
        try:
            while view and process.poll() is None:
                written = os.write(stdin_fd, view[:65_536])
                if written <= 0:
                    raise OSError("short subprocess stdin write")
                view = view[written:]
        except BrokenPipeError:
            pass
        except OSError as exc:
            stdin_errors.append(exc)
            terminate_tree()
        finally:
            try:
                os.close(stdin_fd)
            except OSError:
                pass

    stdin_thread: threading.Thread | None = None
    if input_text is not None and process.stdin is not None:
        stdin_thread = threading.Thread(target=write_stdin, daemon=True, name="bounded-stdin")
        stdin_thread.start()
    timeout_error: subprocess.TimeoutExpired | None = None
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_tree()
        returncode = process.wait()
        timeout_error = exc
    finally:
        if stdin_fd is not None:
            try:
                os.close(stdin_fd)
            except OSError:
                pass
        if stdin_thread is not None:
            stdin_thread.join(timeout=1)
        if timeout_error is not None or drain_errors or stdin_errors:
            terminate_tree()
        drain_deadline = time.monotonic() + 1
        for thread in threads:
            thread.join(timeout=max(drain_deadline - time.monotonic(), 0))
        if any(thread.is_alive() for thread in threads):
            # Parent мог завершиться, пока grandchild всё ещё держит pipe.
            terminate_tree()
            process.stdout.close()
            process.stderr.close()
            for thread in threads:
                thread.join(timeout=1)
        if windows_job is not None:
            windows_job.close()
    stdout, _stdout_total, stdout_redacted_total = captures.get("stdout", (bytearray(), 0, 0))
    stderr, _stderr_total, stderr_redacted_total = captures.get("stderr", (bytearray(), 0, 0))
    artifact: dict[str, Any] | None = None
    if artifact_id is not None:
        close_errors: list[OSError] = []
        for output_artifact in artifacts.values():
            try:
                output_artifact.close()
            except OSError as exc:
                close_errors.append(exc)
        if drain_errors or close_errors:
            abort_capture()
            error = drain_errors[0] if drain_errors else close_errors[0]
            if isinstance(error, (ArtifactPersistenceError, ArtifactQuotaError)):
                raise error
            raise ArtifactPersistenceError(f"artifact capture failed: {error}") from error
        stdout_artifact, stderr_artifact = artifacts["stdout"], artifacts["stderr"]
        stdout_limit = max_stdout_bytes
        stderr_preview_limit = stderr_limit
        if max_combined_bytes is not None:
            combined_limit = max(max_combined_bytes, 0)
            if stdout_artifact.bytes_written and stderr_artifact.bytes_written:
                separator_bytes = 1 if combined_limit > 0 else 0
                payload_limit = max(combined_limit - separator_bytes, 0)
                stdout_limit = (payload_limit + 1) // 2
                stderr_preview_limit = payload_limit - stdout_limit
            elif stdout_artifact.bytes_written:
                stdout_limit = combined_limit
            else:
                stderr_preview_limit = combined_limit
        stdout = bytearray(inline_head_tail(
            stdout_artifact.head, stdout_artifact.preview,
            total_bytes=stdout_artifact.bytes_written, maximum=stdout_limit,
        ).encode("utf-8"))
        stderr = bytearray(inline_head_tail(
            stderr_artifact.head, stderr_artifact.preview,
            total_bytes=stderr_artifact.bytes_written, maximum=stderr_preview_limit,
        ).encode("utf-8"))
        stdout_redacted_total = stdout_artifact.bytes_written
        stderr_redacted_total = stderr_artifact.bytes_written
        manifest = store_for(artifact_settings).root / f"{artifact_id}.json"
        try:
            manifest_data = json.dumps({
                "log_id": artifact_id, "kind": "command_output", "created_at": time.time(),
                "complete": True, "status": "completed",
                "stdout_bytes": stdout_artifact.bytes_written, "stderr_bytes": stderr_artifact.bytes_written,
                "stdout_sha256": stdout_artifact.sha256, "stderr_sha256": stderr_artifact.sha256,
            }, sort_keys=True).encode()
            assert store is not None
            store.write_aux(artifact_id, manifest, manifest_data)
        except OSError as exc:
            abort_capture()
            if isinstance(exc, (ArtifactPersistenceError, ArtifactQuotaError)):
                raise
            raise ArtifactPersistenceError(f"artifact manifest write failed: {exc}") from exc
        artifact = artifact_reference(
            artifact_id, complete=True,
            reason="inline_limit" if stdout_redacted_total > len(stdout) or stderr_redacted_total > len(stderr) else "none",
        )
    if drain_errors:
        error = drain_errors[0]
        if isinstance(error, (ArtifactPersistenceError, ArtifactQuotaError)):
            raise error
        raise ArtifactPersistenceError(f"artifact capture failed: {error}") from error
    if stdin_errors:
        raise OSError(f"subprocess stdin write failed: {stdin_errors[0]}") from stdin_errors[0]
    result = BoundedProcessResult(
        args=args, returncode=returncode,
        stdout=_utf8_prefix(bytes(stdout)),
        stderr=_utf8_prefix(bytes(stderr)),
        stdout_bytes=stdout_redacted_total, stderr_bytes=stderr_redacted_total,
        stdout_truncated=stdout_redacted_total > len(stdout),
        stderr_truncated=stderr_redacted_total > len(stderr),
        artifact=artifact,
    )
    if timeout_error is not None:
        cast(Any, timeout_error).result = result
        raise timeout_error
    return result


def _utf8_prefix(data: bytes) -> str:
    while data:
        try:
            return data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            if exc.start < len(data) - 4:
                raise ValueError("subprocess output contains invalid UTF-8") from exc
            data = data[:exc.start]
    return ""


def _terminate_windows_tree(pid: int) -> None:
    """Use the built-in Windows tree terminator; no shell or captured output."""
    try:
        subprocess.run(
            [str(_windows_taskkill_path()), "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Последний fallback закрывает хотя бы direct child, если taskkill недоступен.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _windows_taskkill_path() -> PureWindowsPath:
    windows_root = PureWindowsPath(os.environ.get("SystemRoot", r"C:\Windows"))
    return windows_root / "System32" / "taskkill.exe"


def _create_windows_job(process: subprocess.Popen[bytes]) -> _WindowsJob:
    """Assign the child to a kill-on-close Job Object without external dependencies."""
    if os.name != "nt":
        raise OSError("Windows Job Objects are unavailable on this platform")
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")  # type: ignore[attr-defined]
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info),
    )
    process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
    assigned = configured and kernel32.AssignProcessToJobObject(handle, process_handle)
    if not assigned:
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        kernel32.CloseHandle(handle)
        raise OSError(error, "Windows Job Object setup failed")
    return _WindowsJob(kernel32, handle)


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Resume a process only after its Job Object owns all future descendants."""
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)  # type: ignore[attr-defined]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = wintypes.LONG
    status = ntdll.NtResumeProcess(wintypes.HANDLE(int(process._handle)))  # type: ignore[attr-defined]
    if status != 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")


def _setup_windows_process(process: subprocess.Popen[bytes]) -> _WindowsJob:
    """Establish tree ownership before suspended user code is allowed to run."""
    job: _WindowsJob | None = None
    try:
        job = _create_windows_job(process)
        _resume_windows_process(process)
        return job
    except BaseException:
        if job is not None:
            job.terminate()
            job.close()
        try:
            process.kill()
        finally:
            process.wait()
        raise
