from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_GIB = 1024**3


@dataclass(frozen=True)
class ResourceLimits:
    profile: str
    detected_memory_bytes: int | None
    buffer_bytes: int
    heavy_operations: int


class HeavyOperationLease:
    def __init__(
        self,
        limiter: _HeavyLimiter,
        operation_id: str,
    ) -> None:
        self._limiter = limiter
        self.operation_id = operation_id
        self._released = False
        self._lock = threading.Lock()

    def set_cancel(self, callback: Callable[[], None]) -> None:
        self._limiter.set_cancel(self.operation_id, callback)

    def release(self) -> None:
        with self._lock:
            if not self._released:
                self._released = True
                self._limiter.release(self.operation_id)


class ResourceBusyError(RuntimeError):
    def __init__(self, capacity: int, operations: list[dict[str, object]] | None = None) -> None:
        self.capacity = capacity
        self.operations = operations or []
        super().__init__(f"heavy operation limit reached: {capacity}")


class _HeavyLimiter:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.semaphore = threading.BoundedSemaphore(capacity)
        self.lock = threading.RLock()
        self.operations: dict[str, dict[str, object]] = {}

    def snapshot(self) -> list[dict[str, object]]:
        now = time.monotonic()
        with self.lock:
            return [
                {
                    "operation_id": operation_id,
                    "tool": item["tool"],
                    "repo": item["repo"],
                    "cwd": item["cwd"],
                    "request_id": item["request_id"],
                    "started_at": item["started_at"],
                    "age_ms": max(0, int((now - float(str(item["started_monotonic"]))) * 1000)),
                    "cancellable": item["cancel"] is not None,
                    **({"cancel_tool": item["cancel_tool"]} if item["cancel_tool"] else {}),
                    **({"cancel_id": item["cancel_id"]} if item["cancel_id"] else {}),
                }
                for operation_id, item in self.operations.items()
            ]

    def set_cancel(self, operation_id: str, callback: Callable[[], None]) -> None:
        invoke = False
        with self.lock:
            item = self.operations.get(operation_id)
            if item is None:
                return
            item["cancel"] = callback
            invoke = bool(item["cancel_requested"])
        if invoke:
            callback()

    def cancel(self, operation_id: str) -> bool | None:
        callback: Callable[[], None] | None
        with self.lock:
            item = self.operations.get(operation_id)
            if item is None:
                return None
            callback = item["cancel"]  # type: ignore[assignment]
            if callback is None:
                return False
            item["cancel_requested"] = True
        callback()
        return True

    def release(self, operation_id: str) -> None:
        with self.lock:
            removed = self.operations.pop(operation_id, None)
        if removed is not None:
            self.semaphore.release()


_HEAVY_LIMITERS: dict[tuple[str, int], _HeavyLimiter] = {}
_HEAVY_LIMITERS_LOCK = threading.Lock()


def _heavy_limiter(settings: object) -> _HeavyLimiter:
    capacity = max(int(getattr(settings, "max_heavy_operations", 2)), 1)
    root = str(getattr(settings, "project_root", ""))
    key = (root, capacity)
    with _HEAVY_LIMITERS_LOCK:
        return _HEAVY_LIMITERS.setdefault(key, _HeavyLimiter(capacity))


def acquire_heavy_operation(
    settings: object,
    *,
    tool: str = "unknown",
    cwd: str | None = None,
    request_id: str | None = None,
    cancel_tool: str | None = None,
    cancel_id: str | None = None,
) -> HeavyOperationLease:
    limiter = _heavy_limiter(settings)
    if not limiter.semaphore.acquire(blocking=False):
        raise ResourceBusyError(limiter.capacity, limiter.snapshot())
    operation_id = str(uuid.uuid4())
    root = str(getattr(settings, "project_root", ""))
    with limiter.lock:
        limiter.operations[operation_id] = {
            "tool": tool,
            "repo": root,
            "cwd": cwd or root,
            "request_id": request_id or operation_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "started_monotonic": time.monotonic(),
            "cancel": None,
            "cancel_requested": False,
            "cancel_tool": cancel_tool,
            "cancel_id": cancel_id,
        }
    return HeavyOperationLease(limiter, operation_id)


def list_heavy_operations(settings: object) -> dict[str, object]:
    limiter = _heavy_limiter(settings)
    operations = limiter.snapshot()
    return {"ok": True, "capacity": limiter.capacity, "used": len(operations), "operations": operations}


def cancel_heavy_operation(settings: object, operation_id: str) -> dict[str, object]:
    outcome = _heavy_limiter(settings).cancel(operation_id)
    if outcome is None:
        return {"ok": False, "error_kind": "heavy_operation_not_found", "error": "heavy operation was not found", "operation_id": operation_id}
    if outcome is False:
        return {"ok": False, "error_kind": "specialized_cancel_required", "error": "use the operation's cancel_tool and cancel_id", "operation_id": operation_id}
    return {"ok": True, "operation_id": operation_id, "cancel_requested": True}


def _safe_cgroup_dir(mount_root: str, mount_point: str, cgroup_path: str) -> str | None:
    for value in (mount_root, mount_point, cgroup_path):
        parsed = PurePosixPath(value)
        if not parsed.is_absolute() or ".." in parsed.parts:
            return None
    root, point, group = map(PurePosixPath, (mount_root, mount_point, cgroup_path))
    if group == PurePosixPath("/"):
        relative = PurePosixPath(".")
    else:
        try:
            relative = group.relative_to(root)
        except ValueError:
            return None
    candidate = point.joinpath(relative)
    try:
        candidate.relative_to(point)
    except ValueError:
        return None
    return str(candidate)


def _cgroup_memory_limit_paths(read_text: Callable[[str], str]) -> list[str]:
    try:
        memberships = read_text("/proc/self/cgroup").splitlines()
        mounts = read_text("/proc/self/mountinfo").splitlines()
    except OSError:
        return []
    v2_path: str | None = None
    v1_path: str | None = None
    for line in memberships:
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        controllers, membership_group = parts[1], parts[2]
        if controllers == "":
            v2_path = membership_group
        elif "memory" in controllers.split(","):
            v1_path = membership_group
    result: list[str] = []
    for line in mounts:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator < 6 or len(fields) <= separator + 3:
            continue
        mount_root, mount_point = fields[3], fields[4]
        filesystem, super_options = fields[separator + 1], fields[separator + 3]
        if filesystem not in {"cgroup", "cgroup2"}:
            continue
        mounted_group = v2_path if filesystem == "cgroup2" else v1_path
        if mounted_group is None or (filesystem == "cgroup" and "memory" not in super_options.split(",")):
            continue
        directory = _safe_cgroup_dir(mount_root, mount_point, mounted_group)
        if directory is None:
            continue
        names = ("memory.max", "memory.high") if filesystem == "cgroup2" else ("memory.limit_in_bytes",)
        result.extend(str(PurePosixPath(directory, name)) for name in names)
    return result


def _effective_memory_bytes(
    host_memory_bytes: int | None,
    read_text: Callable[[str], str] | None = None,
) -> int | None:
    if host_memory_bytes is None or host_memory_bytes <= 0 or not sys.platform.startswith("linux"):
        return host_memory_bytes
    reader = read_text or (lambda path: Path(path).read_text(encoding="utf-8"))
    limits = [host_memory_bytes]
    for path in _cgroup_memory_limit_paths(reader):
        try:
            value = reader(path).strip()
            if value == "max":
                continue
            parsed = int(value)
            if parsed > 0:
                limits.append(parsed)
        except (OSError, ValueError):
            continue
    return min(limits)


def detect_physical_memory_bytes() -> int | None:
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            windll = getattr(ctypes, "windll", None)
            if windll is not None and windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total_physical)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if isinstance(pages, int) and isinstance(page_size, int) and pages > 0 and page_size > 0:
            return _effective_memory_bytes(pages * page_size)
    except (AttributeError, OSError, ValueError):
        return None
    return None


def resolve_resource_limits(
    profile: str,
    *,
    detected_memory_bytes: int | None = None,
    custom_buffer_bytes: int | None = None,
    custom_heavy_operations: int | None = None,
) -> ResourceLimits:
    normalized = profile.strip().lower()
    if normalized not in {"auto", "small", "medium", "large", "custom"}:
        raise ValueError("RESOURCE_PROFILE must be auto, small, medium, large, or custom")
    memory = detected_memory_bytes
    if normalized == "auto":
        memory = memory if memory is not None else detect_physical_memory_bytes()
        if memory is None or memory <= 4 * _GIB:
            normalized = "small"
        elif memory <= 16 * _GIB:
            normalized = "medium"
        else:
            normalized = "large"
    presets = {
        "small": (16 * 1024**2, 2),
        "medium": (32 * 1024**2, 4),
        "large": (64 * 1024**2, 8),
    }
    if normalized == "custom":
        if custom_heavy_operations is None or custom_heavy_operations <= 0:
            raise ValueError("MAX_HEAVY_OPERATIONS must be positive for RESOURCE_PROFILE=custom")
        if custom_buffer_bytes is not None and custom_buffer_bytes <= 0:
            raise ValueError("RESOURCE_BUFFER_BYTES must be positive when set")
        # Сохраняем поле как диагностическую оценку для обратной совместимости.
        buffer_bytes = custom_buffer_bytes or 16 * 1024**2
        heavy_operations = custom_heavy_operations
    else:
        buffer_bytes, heavy_operations = presets[normalized]
        if custom_buffer_bytes is not None:
            if custom_buffer_bytes <= 0:
                raise ValueError("RESOURCE_BUFFER_BYTES must be positive when set")
            buffer_bytes = custom_buffer_bytes
        if custom_heavy_operations is not None:
            if custom_heavy_operations <= 0:
                raise ValueError("MAX_HEAVY_OPERATIONS must be positive when set")
            heavy_operations = custom_heavy_operations
    return ResourceLimits(
        profile=normalized,
        detected_memory_bytes=memory,
        buffer_bytes=buffer_bytes,
        heavy_operations=heavy_operations,
    )
