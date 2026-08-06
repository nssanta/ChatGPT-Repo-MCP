from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

if os.name == "posix":
    import fcntl
    import pty
    import termios
else:  # pragma: no cover - exercised by Windows CI import smoke
    fcntl = None  # type: ignore[assignment]
    pty = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

from .command_tools import _audit, _resolve_cwd, _terminate_process_group, _utc_now
from .config import Settings
from .output_store import artifact_reference, store_for
from .resource_profile import HeavyOperationLease, ResourceBusyError, acquire_heavy_operation
from .runtime_env import command_environment, resolve_binary


@dataclass
class TerminalSession:
    session_id: str
    log_id: str
    process: subprocess.Popen[bytes]
    master_fd: int
    cwd: str
    shell: str
    cols: int
    rows: int
    created_at: str
    idle_timeout_ms: int
    args_fingerprint: str
    heavy_lease: HeavyOperationLease
    status: str = "running"
    last_activity_at: str = field(default_factory=_utc_now)
    exit_code: int | None = None
    term_signal: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


SESSIONS: dict[str, TerminalSession] = {}
SESSIONS_LOCK = threading.RLock()


def _log_paths(settings: Settings, log_id: str) -> tuple[Path, Path]:
    root = settings.command_jobs_dir / "artifacts"
    return root / f"{log_id}.json", root / f"{log_id}.combined"


def _metadata(session: TerminalSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "status": session.status,
        "pid": session.process.pid,
        "pgid": session.process.pid,
        "cwd": session.cwd,
        "shell": session.shell,
        "cols": session.cols,
        "rows": session.rows,
        "created_at": session.created_at,
        "last_activity_at": session.last_activity_at,
        "exit_code": session.exit_code,
        "term_signal": session.term_signal,
        "log_id": session.log_id,
    }


def _persist(settings: Settings, session: TerminalSession) -> None:
    meta, _ = _log_paths(settings, session.log_id)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"log_id": session.log_id, "stream": "combined", **_metadata(session)}, sort_keys=True), encoding="utf-8")


def _set_size(fd: int, cols: int, rows: int) -> None:
    if fcntl is None or termios is None:
        raise RuntimeError("PTY is unavailable on this platform")
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _reader(settings: Settings, session: TerminalSession) -> None:
    _, log_path = _log_paths(settings, session.log_id)
    artifact = store_for(settings).open(
        session.log_id, "combined", log_path, settings.max_command_output_chars,
    )
    capture_error: str | None = None
    try:
        while True:
            try:
                chunk = os.read(session.master_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            artifact.write(chunk)
            with session.lock:
                session.last_activity_at = _utc_now()
                _persist(settings, session)
    except (OSError, ValueError) as exc:
        capture_error = str(exc)
    finally:
        try:
            artifact.close()
        except OSError as exc:
            capture_error = capture_error or str(exc)
    return_code = session.process.wait()
    with session.lock:
        session.exit_code = return_code
        if capture_error:
            session.status = "failed"
        elif session.status not in {"closed", "failed"}:
            session.status = "exited"
        session.last_activity_at = _utc_now()
        _persist(settings, session)
    _, log_path = _log_paths(settings, session.log_id)
    _audit(settings, {
        "timestamp": int(time.time()), "event": "terminal_finished",
        "request_id": session.session_id, "tool": "start_terminal_session",
        "args_fingerprint": session.args_fingerprint,
        "duration_ms": max(0, int((time.time() - datetime.fromisoformat(session.created_at).timestamp()) * 1000)),
        "bytes": log_path.stat().st_size if log_path.exists() else 0,
        "status": "failed" if capture_error else "completed",
        "error": capture_error,
    })
    session.heavy_lease.release()
    try:
        os.close(session.master_fd)
    except OSError:
        pass


def _idle_watch(settings: Settings, session: TerminalSession) -> None:
    while session.status == "running":
        time.sleep(min(max(session.idle_timeout_ms / 4000, 0.1), 5))
        with session.lock:
            if session.status != "running":
                return
            last = datetime.fromisoformat(session.last_activity_at).timestamp()
            if (time.time() - last) * 1000 < session.idle_timeout_ms:
                continue
        close_terminal_session(session.session_id, settings, signal_name="SIGTERM", grace_ms=settings.kill_grace_ms)
        return


def start_terminal_session(
    settings: Settings,
    *,
    cwd: str | None = None,
    shell: str | None = None,
    command: str | None = None,
    cols: int = 120,
    rows: int = 40,
    env: dict[str, str] | None = None,
    idle_timeout_ms: int = 1_800_000,
) -> dict[str, Any]:
    if pty is None:
        raise RuntimeError("PTY is unavailable on this platform")
    with SESSIONS_LOCK:
        active = sum(item.status in {"starting", "running", "closing"} for item in SESSIONS.values())
        if active >= settings.max_terminal_sessions:
            raise ValueError("maximum terminal sessions reached")
    run_cwd = _resolve_cwd(cwd, settings)
    shell_path = shell
    if shell_path:
        candidate = Path(shell_path).expanduser().resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError(f"shell is not executable: {shell}")
        shell_path = str(candidate)
    else:
        shell_path, _ = resolve_binary("bash", settings)
        shell_path = shell_path or "/bin/sh"
    session_id = str(uuid.uuid4())
    log_id = str(uuid.uuid4())
    fingerprint = hashlib.sha256(f"{shell_path}\0{run_cwd}\0{command or ''}".encode()).hexdigest()
    _audit(settings, {
        "timestamp": int(time.time()), "event": "terminal_started", "request_id": session_id,
        "tool": "start_terminal_session", "args_fingerprint": fingerprint,
    })
    try:
        heavy_lease = acquire_heavy_operation(settings)
    except ResourceBusyError:
        _audit(settings, {
            "timestamp": int(time.time()), "event": "terminal_finished", "request_id": session_id,
            "tool": "start_terminal_session", "args_fingerprint": fingerprint,
            "duration_ms": 0, "bytes": 0, "status": "failed", "error_kind": "resource_busy",
        })
        raise
    master_fd: int | None = None
    slave_fd: int | None = None
    argv = [shell_path, "-lc", command] if command else [shell_path, "-i"]
    try:
        master_fd, slave_fd = pty.openpty()
        _set_size(slave_fd, cols, rows)
        process = subprocess.Popen(
            argv,
            cwd=str(run_cwd),
            env=command_environment(settings, env),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
    except OSError as exc:
        heavy_lease.release()
        if master_fd is not None:
            os.close(master_fd)
        _audit(settings, {
            "timestamp": int(time.time()), "event": "terminal_finished", "request_id": session_id,
            "tool": "start_terminal_session", "args_fingerprint": fingerprint,
            "duration_ms": 0, "bytes": 0, "status": "failed", "error": str(exc),
        })
        raise
    finally:
        if slave_fd is not None:
            os.close(slave_fd)
    assert master_fd is not None
    session = TerminalSession(
        session_id=session_id, log_id=log_id, process=process,
        master_fd=master_fd, cwd=str(run_cwd), shell=shell_path, cols=cols, rows=rows,
        created_at=_utc_now(), idle_timeout_ms=idle_timeout_ms, args_fingerprint=fingerprint,
        heavy_lease=heavy_lease,
    )
    with SESSIONS_LOCK:
        SESSIONS[session.session_id] = session
    _persist(settings, session)
    threading.Thread(target=_reader, args=(settings, session), daemon=True, name=f"pty-read-{session.session_id[:8]}").start()
    threading.Thread(target=_idle_watch, args=(settings, session), daemon=True, name=f"pty-idle-{session.session_id[:8]}").start()
    return {
        "ok": True, **_metadata(session), "next_cursor": 0,
        "artifact": artifact_reference(session.log_id, complete=False, reason="source_active"),
    }


def _get(session_id: str) -> TerminalSession:
    with SESSIONS_LOCK:
        session = SESSIONS.get(session_id)
    if session is None:
        raise FileNotFoundError(f"terminal session not found: {session_id}")
    return session


def read_terminal_session(session_id: str, settings: Settings, *, cursor: int = 0, max_bytes: int = 65536, wait_ms: int = 1000) -> dict[str, Any]:
    session = _get(session_id)
    _, path = _log_paths(settings, session.log_id)
    deadline = time.monotonic() + min(max(wait_ms, 0), 30_000) / 1000
    while True:
        size = path.stat().st_size if path.exists() else 0
        if size > cursor or session.status not in {"starting", "running", "closing"} or time.monotonic() >= deadline:
            break
        time.sleep(0.025)
    limit = min(max(max_bytes, 1), 65536)
    data = b""
    if path.exists():
        with path.open("rb") as handle:
            handle.seek(max(cursor, 0))
            data = handle.read(limit)
        size = path.stat().st_size
    next_cursor = max(cursor, 0) + len(data)
    return {
        "ok": True, "session_id": session_id, "data": data.decode("utf-8", errors="replace"),
        "next_cursor": next_cursor, "eof": session.status in {"exited", "failed", "closed"} and next_cursor >= size,
        "truncated": size > next_cursor,
    }


def write_terminal_session(session_id: str, *, data: str, encoding: str = "utf8") -> dict[str, Any]:
    session = _get(session_id)
    if session.status != "running":
        raise ValueError(f"terminal session is not running: {session.status}")
    payload = data.encode("utf-8") if encoding == "utf8" else base64.b64decode(data, validate=True)
    if len(payload) > 65536:
        raise ValueError("terminal write exceeds 65536 bytes")
    written = os.write(session.master_fd, payload)
    with session.lock:
        session.last_activity_at = _utc_now()
    return {"ok": True, "session_id": session_id, "bytes_written": written}


def resize_terminal_session(session_id: str, *, cols: int, rows: int) -> dict[str, Any]:
    session = _get(session_id)
    if not (1 <= cols <= 1000 and 1 <= rows <= 1000):
        raise ValueError("cols and rows must be between 1 and 1000")
    _set_size(session.master_fd, cols, rows)
    with session.lock:
        session.cols, session.rows, session.last_activity_at = cols, rows, _utc_now()
    return {"ok": True, **_metadata(session)}


def close_terminal_session(session_id: str, settings: Settings, *, signal_name: str = "SIGTERM", grace_ms: int = 5000, force: bool = False) -> dict[str, Any]:
    session = _get(session_id)
    with session.lock:
        if session.status in {"exited", "failed", "closed"}:
            return {"ok": True, **_metadata(session), "closed": False}
        session.status = "closing"
        session.term_signal = "SIGKILL" if force else signal_name
    if force:
        try:
            os.killpg(session.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif signal_name == "SIGTERM":
        _terminate_process_group(session.process.pid, grace_seconds=min(max(grace_ms, 0), 30_000) / 1000)
    else:
        allowed = {"SIGINT": signal.SIGINT, "SIGHUP": signal.SIGHUP, "SIGKILL": signal.SIGKILL}
        if signal_name not in allowed:
            raise ValueError("signal must be SIGTERM, SIGINT, SIGHUP, or SIGKILL")
        try:
            os.killpg(session.process.pid, allowed[signal_name])
        except ProcessLookupError:
            pass
    try:
        session.process.wait(timeout=min(max(grace_ms, 1), 30_000) / 1000)
    except subprocess.TimeoutExpired:
        _terminate_process_group(session.process.pid, grace_seconds=settings.kill_grace_ms / 1000)
    with session.lock:
        session.status = "closed"
        session.exit_code = session.process.poll()
        session.last_activity_at = _utc_now()
        _persist(settings, session)
    return {"ok": True, **_metadata(session), "closed": True}


def list_terminal_sessions(*, include_finished: bool = True) -> dict[str, Any]:
    with SESSIONS_LOCK:
        items = list(SESSIONS.values())
    sessions = [_metadata(item) for item in items if include_finished or item.status in {"starting", "running", "closing"}]
    sessions.sort(key=lambda item: item["created_at"], reverse=True)
    return {"ok": True, "sessions": sessions, "count": len(sessions)}


def shutdown_terminal_sessions(settings: Settings) -> None:
    with SESSIONS_LOCK:
        ids = [item.session_id for item in SESSIONS.values() if item.status in {"starting", "running", "closing"}]
    for session_id in ids:
        try:
            close_terminal_session(session_id, settings, grace_ms=settings.kill_grace_ms)
        except Exception:  # noqa: BLE001, S112 - shutdown remains best-effort per session
            continue
