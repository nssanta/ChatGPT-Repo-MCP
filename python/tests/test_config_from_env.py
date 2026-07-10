from __future__ import annotations

from pathlib import Path

import pytest

from chatrepo_mcp import config


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


def test_settings_dry_run_default_and_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    settings = config.Settings.from_env()

    assert settings.default_dry_run is True
    assert settings.effective_dry_run(None) is True
    assert settings.effective_dry_run(False) is False
    assert settings.confirmation_granted(None) is False
    assert settings.confirmation_granted(True) is True
