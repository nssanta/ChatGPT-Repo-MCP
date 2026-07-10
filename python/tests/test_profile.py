from pathlib import Path

from chatrepo_mcp.config import Settings
from chatrepo_mcp.profile import _parse_scalar, _parse_simple_yaml, load_repo_profile


def make_settings(tmp_path: Path) -> Settings:
    from test_command_tools import make_settings as base

    return base(tmp_path)


def test_missing_profile_uses_defaults(tmp_path: Path) -> None:
    profile = load_repo_profile(make_settings(tmp_path))

    assert profile.path is None
    assert "git_diff_check" in profile.presets
    # Default quality rules are neutral/stack-agnostic; stack-specific rules
    # (TS/Python/Go/...) are opt-in via .chatrepo/mcp.yml, not imposed by default.
    assert profile.quality_rules == ["no_secret_like_literals"]
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
    config.write_text("bad inline", encoding="utf-8")

    profile = load_repo_profile(make_settings(tmp_path))

    assert profile.errors
    assert "git_diff_check" in profile.presets


def test_parse_scalar_normalizes_booleans_numbers_and_quotes() -> None:
    assert _parse_scalar("  True ") is True
    assert _parse_scalar("false") is False
    assert _parse_scalar("12") == 12
    assert _parse_scalar("'abc'") == "abc"
    assert _parse_scalar('"xyz"') == "xyz"


def test_parse_simple_yaml_rejects_invalid_entries_and_mixed_sections() -> None:
    valid = _parse_simple_yaml(
        """
presets:
  test:
    command: pytest -x -q
""".strip()
    )
    assert valid["presets"]["test"]["command"] == "pytest -x -q"

    try:
        _parse_simple_yaml("bad inline")
        assert False, "expected ValueError"
    except ValueError:
        assert True

    try:
        _parse_simple_yaml("list:\n- a\nsection:\n  b: 1")
        assert False, "expected section type conflict ValueError"
    except ValueError:
        assert True
