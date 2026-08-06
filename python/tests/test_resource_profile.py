from __future__ import annotations

import pytest

from chatrepo_mcp.config import Settings
from chatrepo_mcp.resource_profile import (
    ResourceBusyError,
    _effective_memory_bytes,
    acquire_heavy_operation,
    resolve_resource_limits,
)


def test_linux_effective_memory_resolves_systemd_v2_process_path(monkeypatch) -> None:
    monkeypatch.setattr("chatrepo_mcp.resource_profile.sys.platform", "linux")
    values = {
        "/proc/self/cgroup": "0::/system.slice/chatrepo.service\n",
        "/proc/self/mountinfo": "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "/sys/fs/cgroup/system.slice/chatrepo.service/memory.max": "8\n",
        "/sys/fs/cgroup/system.slice/chatrepo.service/memory.high": "4\n",
    }

    assert _effective_memory_bytes(16, values.__getitem__) == 4


def test_linux_effective_memory_ignores_missing_invalid_and_unlimited_limits(monkeypatch) -> None:
    monkeypatch.setattr("chatrepo_mcp.resource_profile.sys.platform", "linux")

    def read(path: str) -> str:
        if path == "/proc/self/cgroup":
            return "0::/service\n"
        if path == "/proc/self/mountinfo":
            return "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        if path.endswith("memory.max"):
            return "max"
        if path.endswith("memory.high"):
            return "invalid"
        raise OSError("not mounted")

    assert _effective_memory_bytes(16, read) == 16


def test_linux_effective_memory_resolves_v1_memory_controller(monkeypatch) -> None:
    monkeypatch.setattr("chatrepo_mcp.resource_profile.sys.platform", "linux")
    values = {
        "/proc/self/cgroup": "5:cpu,memory:/docker/abc\n",
        "/proc/self/mountinfo": "40 25 0:35 /docker /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n",
        "/sys/fs/cgroup/memory/abc/memory.limit_in_bytes": "3\n",
    }
    assert _effective_memory_bytes(16, values.__getitem__) == 3


def test_linux_cgroup_traversal_fails_safe_to_host(monkeypatch) -> None:
    monkeypatch.setattr("chatrepo_mcp.resource_profile.sys.platform", "linux")
    values = {
        "/proc/self/cgroup": "0::/../../escape\n",
        "/proc/self/mountinfo": "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
    }
    assert _effective_memory_bytes(16, values.__getitem__) == 16


def test_linux_cgroup_namespace_root_maps_to_mount_point(monkeypatch) -> None:
    monkeypatch.setattr("chatrepo_mcp.resource_profile.sys.platform", "linux")
    values = {
        "/proc/self/cgroup": "0::/\n",
        "/proc/self/mountinfo": "36 25 0:32 /system.slice/chatrepo.service /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "/sys/fs/cgroup/memory.max": "2\n",
        "/sys/fs/cgroup/memory.high": "max\n",
    }
    assert _effective_memory_bytes(16, values.__getitem__) == 2


def test_non_linux_effective_memory_preserves_host_fallback(monkeypatch) -> None:
    monkeypatch.setattr("chatrepo_mcp.resource_profile.sys.platform", "darwin")
    assert _effective_memory_bytes(16, lambda _path: "1") == 16


@pytest.mark.parametrize(
    ("memory_gib", "profile", "buffer_mib", "heavy"),
    [(4, "small", 16, 2), (8, "medium", 32, 4), (32, "large", 64, 8)],
)
def test_auto_resource_profile_is_memory_aware(
    memory_gib: int, profile: str, buffer_mib: int, heavy: int,
) -> None:
    limits = resolve_resource_limits("auto", detected_memory_bytes=memory_gib * 1024**3)

    assert limits.profile == profile
    assert limits.buffer_bytes == buffer_mib * 1024**2
    assert limits.heavy_operations == heavy


def test_custom_resource_profile_requires_only_the_enforced_heavy_limit() -> None:
    with pytest.raises(ValueError, match="MAX_HEAVY_OPERATIONS"):
        resolve_resource_limits("custom")

    diagnostic_default = resolve_resource_limits("custom", custom_heavy_operations=3)
    assert diagnostic_default.buffer_bytes == 16 * 1024**2
    assert diagnostic_default.heavy_operations == 3

    limits = resolve_resource_limits(
        "custom", custom_buffer_bytes=123_456, custom_heavy_operations=3,
    )
    assert limits.buffer_bytes == 123_456
    assert limits.heavy_operations == 3

    with pytest.raises(ValueError, match="RESOURCE_BUFFER_BYTES"):
        resolve_resource_limits("small", custom_buffer_bytes=0)
    with pytest.raises(ValueError, match="MAX_HEAVY_OPERATIONS"):
        resolve_resource_limits("small", custom_heavy_operations=-1)


def test_settings_resolves_resource_profile_and_rejects_disabled_persistence(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("RESOURCE_PROFILE", "custom")
    monkeypatch.setenv("RESOURCE_BUFFER_BYTES", "123456")
    monkeypatch.setenv("MAX_HEAVY_OPERATIONS", "3")
    settings = Settings.from_env()
    assert settings.resource_profile == "custom"
    assert settings.resource_profile_applied == "custom"
    assert settings.resource_buffer_bytes == 123_456
    assert settings.max_heavy_operations == 3
    assert settings.persist_full_output is True

    monkeypatch.setenv("PERSIST_FULL_OUTPUT", "false")
    with pytest.raises(RuntimeError, match="bounded durable output is mandatory"):
        Settings.from_env()


def test_heavy_operation_limiter_is_fail_fast_and_releases(tmp_path) -> None:
    class _Settings:
        project_root = tmp_path
        max_heavy_operations = 2

    first = acquire_heavy_operation(_Settings())
    second = acquire_heavy_operation(_Settings())
    with pytest.raises(ResourceBusyError, match="heavy operation limit reached: 2"):
        acquire_heavy_operation(_Settings())
    first.release()
    replacement = acquire_heavy_operation(_Settings())
    replacement.release()
    second.release()
