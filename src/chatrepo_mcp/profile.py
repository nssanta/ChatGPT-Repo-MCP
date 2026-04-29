from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings


DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "git_diff_check": {"command": "git diff --check", "parser": "git_diff_check", "timeout_ms": 120_000},
    "node_version": {"command": "node --version", "parser": "none", "timeout_ms": 30_000},
    "npm_version": {"command": "npm --version", "parser": "none", "timeout_ms": 30_000},
}

DEFAULT_QUALITY_RULES = [
    "no_new_as_any",
    "no_new_colon_any",
    "no_new_ts_ignore",
    "no_new_eslint_disable",
    "no_new_console_log",
    "no_secret_like_literals",
]

DEFAULT_MISSION = {
    "current": "missions/CURRENT.md",
    "backlog": "missions/BACKLOG.md",
    "memory": ".claude/MEMORY.md",
    "packets": "missions/packets",
}


@dataclass(frozen=True)
class RepoProfile:
    path: str | None
    presets: dict[str, dict[str, Any]]
    quality_rules: list[str]
    mission: dict[str, str]
    errors: list[str]


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    section: str | None = None
    subsection: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            if not stripped.endswith(":"):
                raise ValueError(f"top-level YAML keys must end with ':': {stripped}")
            section = stripped[:-1]
            subsection = None
            result.setdefault(section, {})
            continue
        if section is None:
            raise ValueError(f"nested YAML entry without section: {stripped}")
        if stripped.startswith("- "):
            if not isinstance(result.get(section), list):
                result[section] = []
            result[section].append(_parse_scalar(stripped[2:]))
            continue
        if ":" not in stripped:
            raise ValueError(f"invalid YAML entry: {stripped}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if indent == 2 and not value:
            subsection = key
            container = result.setdefault(section, {})
            if not isinstance(container, dict):
                raise ValueError(f"section cannot contain both list and mapping: {section}")
            container.setdefault(subsection, {})
            continue
        target: dict[str, Any]
        if indent >= 4 and subsection:
            container = result.setdefault(section, {})
            if not isinstance(container, dict):
                raise ValueError(f"section is not a mapping: {section}")
            nested = container.setdefault(subsection, {})
            if not isinstance(nested, dict):
                raise ValueError(f"subsection is not a mapping: {subsection}")
            target = nested
        else:
            container = result.setdefault(section, {})
            if not isinstance(container, dict):
                raise ValueError(f"section is not a mapping: {section}")
            target = container
        target[key] = _parse_scalar(value)
    return result


def _normalize_presets(raw: Any) -> dict[str, dict[str, Any]]:
    presets = dict(DEFAULT_PRESETS)
    if not isinstance(raw, dict):
        return presets
    for name, value in raw.items():
        if isinstance(value, str):
            presets[str(name)] = {"command": value, "parser": "auto"}
        elif isinstance(value, dict) and value.get("command"):
            presets[str(name)] = {
                "command": str(value["command"]),
                "parser": str(value.get("parser", "auto")),
                "timeout_ms": value.get("timeout_ms"),
            }
    return presets


def load_repo_profile(settings: Settings) -> RepoProfile:
    path = settings.project_root / ".chatrepo" / "mcp.yml"
    errors: list[str] = []
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = _parse_simple_yaml(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            data = {}

    presets = _normalize_presets(data.get("presets"))
    raw_rules = data.get("quality_rules")
    rules = [str(item) for item in raw_rules] if isinstance(raw_rules, list) else list(DEFAULT_QUALITY_RULES)
    mission = dict(DEFAULT_MISSION)
    raw_mission = data.get("mission")
    if isinstance(raw_mission, dict):
        mission.update({str(key): str(value) for key, value in raw_mission.items()})

    return RepoProfile(
        path=str(path) if path.exists() else None,
        presets=presets,
        quality_rules=rules,
        mission=mission,
        errors=errors,
    )


def list_test_presets(settings: Settings) -> dict[str, Any]:
    profile = load_repo_profile(settings)
    return {
        "ok": not profile.errors,
        "profile_path": profile.path,
        "presets": profile.presets,
        "count": len(profile.presets),
        "errors": profile.errors,
    }
