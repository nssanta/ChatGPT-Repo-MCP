from pathlib import Path

from chatrepo_mcp.config import Settings
from chatrepo_mcp.profile import load_repo_profile


def make_settings(tmp_path: Path) -> Settings:
    from test_command_tools import make_settings as base

    return base(tmp_path)


def test_missing_profile_uses_defaults(tmp_path: Path) -> None:
    profile = load_repo_profile(make_settings(tmp_path))

    assert profile.path is None
    assert "git_diff_check" in profile.presets
    assert "no_new_as_any" in profile.quality_rules
    assert profile.mission["current"] == "missions/CURRENT.md"


def test_repo_profile_loads_presets_rules_and_mission(tmp_path: Path) -> None:
    config = tmp_path / ".chatrepo" / "mcp.yml"
    config.parent.mkdir()
    config.write_text(
        """
mission:
  current: work/CURRENT.md
quality_rules:
  - no_new_console_log
presets:
  custom_test:
    command: npm run test -w packages/example
    parser: vitest
    timeout_ms: 123000
""".strip(),
        encoding="utf-8",
    )

    profile = load_repo_profile(make_settings(tmp_path))

    assert profile.path == str(config)
    assert profile.mission["current"] == "work/CURRENT.md"
    assert profile.quality_rules == ["no_new_console_log"]
    assert profile.presets["custom_test"]["command"] == "npm run test -w packages/example"
    assert profile.presets["custom_test"]["timeout_ms"] == 123000
    assert "git_diff_check" in profile.presets


def test_invalid_profile_returns_safe_defaults_and_error(tmp_path: Path) -> None:
    config = tmp_path / ".chatrepo" / "mcp.yml"
    config.parent.mkdir()
    config.write_text("bad: inline", encoding="utf-8")

    profile = load_repo_profile(make_settings(tmp_path))

    assert profile.errors
    assert "git_diff_check" in profile.presets
