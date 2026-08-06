from __future__ import annotations

import os
import sys
import threading
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
    def __init__(self, semaphore: threading.BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if not self._released:
                self._released = True
                self._semaphore.release()


class ResourceBusyError(RuntimeError):
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        super().__init__(f"heavy operation limit reached: {capacity}")


_HEAVY_LIMITERS: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_HEAVY_LIMITERS_LOCK = threading.Lock()


def acquire_heavy_operation(settings: object) -> HeavyOperationLease:
    capacity = max(int(getattr(settings, "max_heavy_operations", 2)), 1)
    root = str(getattr(settings, "project_root", ""))
    key = (root, capacity)
    with _HEAVY_LIMITERS_LOCK:
        semaphore = _HEAVY_LIMITERS.setdefault(key, threading.BoundedSemaphore(capacity))
    if not semaphore.acquire(blocking=False):
        raise ResourceBusyError(capacity)
    return HeavyOperationLease(semaphore)


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
