from __future__ import annotations

from pathlib import Path

import pytest

from chatrepo_mcp import config


_CONFIG_ENV_NAMES = (
    "PROJECT_ROOT", "ACCESS_MODE", "MCP_AUTH_MODE", "MCP_BEARER_TOKEN",
    "COMMAND_POLICY_MODE", "SECRET_GLOBS", "ALLOW_SECRET_ACCESS", "ENABLE_PTY",
    "PERSIST_FULL_OUTPUT", "RESOURCE_PROFILE", "RESOURCE_BUFFER_BYTES", "MAX_HEAVY_OPERATIONS",
    "DEFAULT_INLINE_OUTPUT_BYTES", "MAX_RESPONSE_CHARS", "MAX_DIFF_BYTES",
    "MAX_COMMAND_OUTPUT_CHARS", "COMMAND_TIMEOUT_MS", "COMMAND_JOB_TIMEOUT_MS",
    "FILE_TRANSFER_IMPORT_MAX_BYTES", "FILE_TRANSFER_EXPORT_MAX_BYTES",
    "COMPUTER_USE_ENABLED", "COMPUTER_CONTROL_ENABLED", "COMPUTER_SNAPSHOT_TTL_SECONDS",
    "COMPUTER_ACTION_TIMEOUT_MS", "COMPUTER_IDLE_TIMEOUT_SECONDS",
    "COMPUTER_MAX_SEQUENCE_STEPS", "COMPUTER_CAPTURE_MAX_EDGE",
    "MAINTENANCE_ENABLED", "MAINTENANCE_INTERVAL_SECONDS", "AUDIT_LOG_TTL_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_config_environment(monkeypatch) -> None:
    for name in _CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_from_env_requires_project_root(monkeypatch) -> None:
    monkeypatch.delenv("PROJECT_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="PROJECT_ROOT is required"):
        config.Settings.from_env()


def test_from_env_rejects_invalid_access_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ACCESS_MODE", "unsafe")

    with pytest.raises(RuntimeError, match="ACCESS_MODE must be one of: safe, full"):
        config.Settings.from_env()


def test_from_env_rejects_invalid_auth_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MCP_AUTH_MODE", "token")

    with pytest.raises(RuntimeError, match="MCP_AUTH_MODE must be one of: none, bearer"):
        config.Settings.from_env()


def test_from_env_bearer_mode_requires_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MCP_AUTH_MODE", "bearer")

    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN is required when MCP_AUTH_MODE=bearer"):
        config.Settings.from_env()


def test_from_env_rejects_invalid_command_policy_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COMMAND_POLICY_MODE", "chaos")

    with pytest.raises(RuntimeError, match="COMMAND_POLICY_MODE must be one of"):
        config.Settings.from_env()


def test_from_env_full_access_enables_full_mode_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ACCESS_MODE", "full")
    monkeypatch.delenv("ENABLE_PTY", raising=False)
    settings = config.Settings.from_env()

    assert settings.full_access is True
    assert settings.command_policy_mode == "unrestricted"
    assert settings.require_expected_hash_for_writes is False
    assert settings.allow_move_delete_operations is True
    assert settings.filesystem_unrestricted is True
    assert settings.confirmation_granted(None) is True
    assert settings.enable_pty is True
    assert settings.command_timeout_ms == 300_000
    assert settings.command_job_timeout_ms == 14_400_000
    assert settings.file_transfer_import_max_bytes == 512 * 1024**2
    assert settings.file_transfer_export_max_bytes == 100 * 1024**2


def test_from_env_rejects_empty_secret_globs_in_safe_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_GLOBS", "")

    with pytest.raises(RuntimeError, match="SECRET_GLOBS must not be empty unless ACCESS_MODE=full"):
        config.Settings.from_env()


def test_from_env_allows_empty_secret_globs_with_full_secret_access(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ACCESS_MODE", "full")
    monkeypatch.setenv("ALLOW_SECRET_ACCESS", "true")
    monkeypatch.setenv("SECRET_GLOBS", "")

    settings = config.Settings.from_env()
    assert settings.allow_secret_access is True
    assert settings.secret_globs == ()


def test_env_bool_parsing_and_csv_helpers(monkeypatch) -> None:
    assert config._env_bool("MISSING_BOOL", True) is True
    monkeypatch.setenv("MISSING_BOOL", "yes")
    assert config._env_bool("MISSING_BOOL", False) is True
    monkeypatch.setenv("MISSING_BOOL", "nope")
    assert config._env_bool("MISSING_BOOL", True) is False
    assert config._env_csv("MISSING_CSV", "a,b,") == ("a", "b")


def test_env_int_parsing_respects_blank_as_default(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_INT", "")
    assert config._env_int("BLANK_INT", 12) == 12


@pytest.mark.parametrize("value", ["0", "-1", "200001"])
def test_from_env_rejects_unsafe_inline_output_limit(
    tmp_path: Path, monkeypatch, value: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("DEFAULT_INLINE_OUTPUT_BYTES", value)

    with pytest.raises(RuntimeError, match="DEFAULT_INLINE_OUTPUT_BYTES must be positive"):
        config.Settings.from_env()


def test_from_env_parses_independent_command_timeouts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COMMAND_TIMEOUT_MS", "600000")
    monkeypatch.setenv("COMMAND_JOB_TIMEOUT_MS", "43200000")

    settings = config.Settings.from_env()

    assert settings.command_timeout_ms == 600_000
    assert settings.command_job_timeout_ms == 43_200_000


@pytest.mark.parametrize("name", ["COMMAND_TIMEOUT_MS", "COMMAND_JOB_TIMEOUT_MS"])
def test_from_env_rejects_non_positive_command_timeouts(
    tmp_path: Path, monkeypatch, name: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(name, "0")

    with pytest.raises(RuntimeError, match=name):
        config.Settings.from_env()


def test_settings_dry_run_default_and_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    settings = config.Settings.from_env()

    assert settings.default_dry_run is True
    assert settings.effective_dry_run(None) is True
    assert settings.effective_dry_run(False) is False
    assert settings.confirmation_granted(None) is False
    assert settings.confirmation_granted(True) is True


@pytest.mark.parametrize("name", ["FILE_TRANSFER_IMPORT_MAX_BYTES", "FILE_TRANSFER_EXPORT_MAX_BYTES"])
def test_from_env_rejects_non_positive_file_transfer_limits(
    tmp_path: Path, monkeypatch, name: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(name, "0")

    with pytest.raises(RuntimeError, match=name):
        config.Settings.from_env()


def test_from_env_computer_use_defaults_and_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    for name in (
        "COMPUTER_USE_ENABLED",
        "COMPUTER_CONTROL_ENABLED",
        "COMPUTER_SNAPSHOT_TTL_SECONDS",
        "COMPUTER_ACTION_TIMEOUT_MS",
        "COMPUTER_IDLE_TIMEOUT_SECONDS",
        "COMPUTER_MAX_SEQUENCE_STEPS",
        "COMPUTER_CAPTURE_MAX_EDGE",
        "MAINTENANCE_ENABLED",
        "MAINTENANCE_INTERVAL_SECONDS",
        "AUDIT_LOG_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = config.Settings.from_env()
    assert settings.computer_use_enabled is False
    assert settings.computer_control_enabled is False
    assert settings.computer_snapshot_ttl_seconds == 30
    assert settings.computer_action_timeout_ms == 30_000
    assert settings.computer_idle_timeout_seconds == 300
    assert settings.computer_max_sequence_steps == 20
    assert settings.computer_capture_max_edge == 1568
    assert settings.maintenance_enabled is True
    assert settings.maintenance_interval_seconds == 21_600
    assert settings.audit_log_ttl_seconds == 604_800

    monkeypatch.setenv("COMPUTER_CONTROL_ENABLED", "true")
    with pytest.raises(RuntimeError, match="COMPUTER_CONTROL_ENABLED=true requires"):
        config.Settings.from_env()


@pytest.mark.parametrize(
    "name",
    ["COMPUTER_SNAPSHOT_TTL_SECONDS", "COMPUTER_ACTION_TIMEOUT_MS"],
)
def test_from_env_rejects_non_positive_computer_limits(
    tmp_path: Path, monkeypatch, name: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(name, "0")

    with pytest.raises(RuntimeError, match=name):
        config.Settings.from_env()



@pytest.mark.parametrize("value", ["0", "128", "8193"])
def test_from_env_rejects_unsafe_computer_capture_edge(
    tmp_path: Path, monkeypatch, value: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COMPUTER_CAPTURE_MAX_EDGE", value)

    with pytest.raises(RuntimeError, match="COMPUTER_CAPTURE_MAX_EDGE"):
        config.Settings.from_env()


@pytest.mark.parametrize("value", ["0", "29", "86401"])
def test_from_env_rejects_unsafe_computer_idle_timeout(
    tmp_path: Path, monkeypatch, value: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COMPUTER_IDLE_TIMEOUT_SECONDS", value)

    with pytest.raises(RuntimeError, match="COMPUTER_IDLE_TIMEOUT_SECONDS"):
        config.Settings.from_env()


@pytest.mark.parametrize("value", ["0", "21"])
def test_from_env_rejects_unsafe_computer_sequence_limit(
    tmp_path: Path, monkeypatch, value: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COMPUTER_MAX_SEQUENCE_STEPS", value)

    with pytest.raises(RuntimeError, match="COMPUTER_MAX_SEQUENCE_STEPS"):
        config.Settings.from_env()


@pytest.mark.parametrize("value", ["0", "59", "604801"])
def test_from_env_rejects_unsafe_maintenance_interval(
    tmp_path: Path, monkeypatch, value: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MAINTENANCE_INTERVAL_SECONDS", value)
    with pytest.raises(RuntimeError, match="MAINTENANCE_INTERVAL_SECONDS"):
        config.Settings.from_env()


@pytest.mark.parametrize("value", ["0", "3599"])
def test_from_env_rejects_unsafe_audit_ttl(
    tmp_path: Path, monkeypatch, value: str,
) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AUDIT_LOG_TTL_SECONDS", value)
    with pytest.raises(RuntimeError, match="AUDIT_LOG_TTL_SECONDS"):
        config.Settings.from_env()
