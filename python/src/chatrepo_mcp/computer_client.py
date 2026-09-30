from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from .config import Settings


class ComputerHostError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ComputerHostClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._sequence = 0

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is None:
                return
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                except OSError:
                    pass

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            process = self._ensure_process()
            self._sequence += 1
            request_id = self._sequence
            payload = {
                "id": request_id,
                "method": method,
                "params": params or {},
            }
            assert process.stdin is not None
            assert process.stdout is not None
            stdout = process.stdout
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._drop_process()
                raise ComputerHostError("host_unavailable", f"computer host transport failed: {exc}") from exc

            response: list[str] = []
            read_error: list[BaseException] = []

            def read_reply() -> None:
                try:
                    response.append(stdout.readline())
                except BaseException as exc:  # noqa: BLE001 - thread boundary
                    read_error.append(exc)

            thread = threading.Thread(target=read_reply, name="chatrepo-computer-host-read", daemon=True)
            thread.start()
            timeout_seconds = max(
                185.0,
                self._settings.computer_action_timeout_ms / 1000.0 + 5.0,
            )
            deadline = time.monotonic() + timeout_seconds
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
            if thread.is_alive():
                self._drop_process()
                thread.join(timeout=1)
                raise ComputerHostError(
                    "timeout",
                    f"computer host request exceeded {timeout_seconds:.1f}s",
                )
            if read_error:
                self._drop_process()
                raise ComputerHostError(
                    "host_unavailable",
                    f"computer host read failed: {read_error[0]}",
                ) from read_error[0]
            line = response[0] if response else ""
            if not line:
                error = "computer host exited without a reply"
                if process.poll() is not None:
                    error += f" (exit {process.returncode})"
                self._drop_process()
                raise ComputerHostError("host_unavailable", error)
            try:
                reply = json.loads(line)
            except json.JSONDecodeError as exc:
                self._drop_process()
                raise ComputerHostError("host_protocol_error", "computer host returned invalid JSON") from exc
            if reply.get("id") != request_id:
                self._drop_process()
                raise ComputerHostError(
                    "host_protocol_error",
                    f"computer host reply id {reply.get('id')!r} does not match request {request_id}",
                )
            if error := reply.get("error"):
                code = str(error.get("code") or "failed") if isinstance(error, dict) else "failed"
                message = str(error.get("message") or error) if isinstance(error, dict) else str(error)
                raise ComputerHostError(code, message)
            result = reply.get("result")
            if not isinstance(result, dict):
                raise ComputerHostError("host_protocol_error", "computer host returned no result object")
            return result

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        path = locate_computer_host(self._settings)
        env = os.environ.copy()
        env.update(
            {
                "COMPUTER_SNAPSHOT_TTL_SECONDS": str(self._settings.computer_snapshot_ttl_seconds),
                "COMPUTER_ACTION_TIMEOUT_MS": str(self._settings.computer_action_timeout_ms),
                "COMPUTER_IDLE_TIMEOUT_SECONDS": str(self._settings.computer_idle_timeout_seconds),
                "COMPUTER_MAX_SEQUENCE_STEPS": str(self._settings.computer_max_sequence_steps),
                "COMPUTER_CAPTURE_MAX_EDGE": str(self._settings.computer_capture_max_edge),
                "COMPUTER_CONTROL_ENABLED": (
                    "true" if self._settings.computer_control_enabled else "false"
                ),
            }
        )
        try:
            process = subprocess.Popen(
                [str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise ComputerHostError("host_unavailable", f"cannot start computer host {path}: {exc}") from exc
        self._process = process
        return process

    def _drop_process(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass


def locate_computer_host(settings: Settings) -> Path:
    explicit = os.getenv("COMPUTER_HOST_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        raise ComputerHostError(
            "host_unavailable",
            f"COMPUTER_HOST_PATH does not point to a regular file: {path}",
        )

    name = "chatrepo-computer-host.exe" if os.name == "nt" else "chatrepo-computer-host"
    package_dir = Path(__file__).resolve().parent
    project = settings.project_root
    candidates = [
        project / "bin" / name,
        project / "go" / name,
        package_dir / "bin" / name,
        Path(sys.executable).resolve().parent / name,
    ]
    # When PROJECT_ROOT points at the user's workspace rather than ChatRepo itself,
    # resolve the source checkout from this package location.
    for parent in package_dir.parents:
        candidates.append(parent / "bin" / name)
        if (parent / "go" / "go.mod").exists():
            candidates.append(parent / "go" / name)

    path_env = os.getenv("PATH", "")
    for directory in path_env.split(os.pathsep):
        if directory:
            candidates.append(Path(directory) / name)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            candidate = candidate.expanduser().resolve()
        except OSError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate

    raise ComputerHostError(
        "host_unavailable",
        (
            f"{name} was not found. Source checkouts must run make computer-host; "
            "packaged installs place the matching companion beside chatrepo-mcp. "
            "COMPUTER_HOST_PATH is available for custom layouts."
        ),
    )


_client: ComputerHostClient | None = None
_client_lock = threading.Lock()


def computer_call(settings: Settings, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    global _client
    with _client_lock:
        if _client is None:
            _client = ComputerHostClient(settings)
    return _client.call(method, params)


def shutdown_computer_client() -> None:
    global _client
    with _client_lock:
        client = _client
        _client = None
    if client is not None:
        client.close()


atexit.register(shutdown_computer_client)
