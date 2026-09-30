from __future__ import annotations

import atexit
from dataclasses import dataclass
from pathlib import Path
import threading
import time

from .config import Settings
from .file_transfer import cleanup_computer_shares
from .output_store import store_for


@dataclass(frozen=True)
class MaintenanceResult:
    artifacts_checked: bool = False
    audit_files_removed: int = 0
    stale_runtime_files_removed: int = 0


def run_maintenance_once(settings: Settings, *, now: float | None = None) -> MaintenanceResult:
    """Prune expired durable runtime data without touching active work.

    Computer observation frames are RAM-only. Explicitly shared screenshots are
    short-lived private cache files and are also pruned here after crashes/restarts.
    """
    current = time.time() if now is None else now
    artifacts_checked = False
    if settings.command_jobs_dir.exists():
        try:
            store_for(settings).cleanup(current)
            artifacts_checked = True
        except OSError:
            # Maintenance is best-effort; a transient filesystem failure must
            # never stop the MCP server.
            pass

    audit_removed = _cleanup_rotated_audit_logs(
        settings.command_audit_log_path,
        cutoff=current - settings.audit_log_ttl_seconds,
    )
    runtime_removed = _cleanup_stale_runtime_files(
        settings.command_jobs_dir,
        cutoff=current - settings.artifact_ttl_seconds,
    )
    runtime_removed += cleanup_computer_shares(now=current)
    return MaintenanceResult(
        artifacts_checked=artifacts_checked,
        audit_files_removed=audit_removed,
        stale_runtime_files_removed=runtime_removed,
    )


def _cleanup_rotated_audit_logs(path: Path, *, cutoff: float) -> int:
    """Delete only rotated audit generations; never the active audit file."""
    parent = path.parent
    if not parent.exists():
        return 0
    removed = 0
    prefix = f"{path.name}."
    for candidate in parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(prefix)
        if not suffix.isdigit():
            continue
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _cleanup_stale_runtime_files(root: Path, *, cutoff: float) -> int:
    """Remove abandoned temporary files while leaving jobs/locks to their owners."""
    if not root.exists():
        return 0
    removed = 0
    candidates: list[Path] = []
    # Concurrency lock files have their own PID-aware reconciliation in the job
    # lifecycle. Maintenance deliberately does not age-delete them because an
    # operator may configure artifact retention shorter than a running job.
    # Atomic-write leftovers can exist at the jobs root or artifact root after
    # a crash. Active temporary files are recent and therefore protected.
    for directory in (root, root / "artifacts", root / "logs"):
        if directory.exists():
            candidates.extend(path for path in directory.glob("*.tmp") if path.is_file())

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class RuntimeMaintenance:
    """One sleeping daemon thread; no polling while the interval has not elapsed."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._settings.maintenance_enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="chatrepo-maintenance",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _run(self) -> None:
        interval = float(self._settings.maintenance_interval_seconds)
        # Do not add I/O to the critical startup path. The first lightweight
        # pass happens after at most one minute; later passes use the configured
        # interval (six hours by default).
        initial = min(interval, 60.0)
        if self._stop.wait(initial):
            return
        while not self._stop.is_set():
            run_maintenance_once(self._settings)
            if self._stop.wait(interval):
                return


_runtime_maintenance: RuntimeMaintenance | None = None
_runtime_lock = threading.Lock()


def start_runtime_maintenance(settings: Settings) -> RuntimeMaintenance | None:
    global _runtime_maintenance
    if not settings.maintenance_enabled:
        return None
    with _runtime_lock:
        if _runtime_maintenance is None:
            _runtime_maintenance = RuntimeMaintenance(settings)
            _runtime_maintenance.start()
            atexit.register(stop_runtime_maintenance)
        return _runtime_maintenance


def stop_runtime_maintenance() -> None:
    global _runtime_maintenance
    with _runtime_lock:
        maintenance = _runtime_maintenance
        _runtime_maintenance = None
    if maintenance is not None:
        maintenance.close()
