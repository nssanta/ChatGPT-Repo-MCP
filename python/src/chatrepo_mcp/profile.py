from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings


DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "git_diff_check": {"command": "git diff --check", "parser": "git_diff_check", "timeout_ms": 120_000},
    "node_version": {"command": "node --version", "parser": "none", "timeout_ms": 30_000},
    "npm_version": {"command": "npm --version", "parser": "none", "timeout_ms": 30_000},
}

# Neutral, stack-agnostic default: no language-specific style rules are
# imposed out of the box. Stack-specific rules (TS/Python/Go/...) are opt-in
# via `quality_rules` in `.chatrepo/mcp.yml`; see workflows.RULE_PATTERNS for
# the available rule ids and the file extensions each one applies to.
DEFAULT_QUALITY_RULES = [
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


def _load_yaml_text(text: str) -> dict[str, Any]:
    """Parse ``.chatrepo/mcp.yml`` text into a dict.

    Prefers PyYAML (``import yaml``) when it is installed, since it supports
    full YAML nesting -- useful for per-service preset sections. PyYAML is an
    optional dependency, never a hard requirement: when it is not installed,
    this falls back to the minimal built-in parser (:func:`_parse_simple_yaml`),
    which only supports a constrained two-level nesting.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return _parse_simple_yaml(text)
    loaded = yaml.safe_load(text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("mcp.yml root must be a mapping")
    return loaded


def _normalize_presets(raw: Any) -> dict[str, dict[str, Any]]:
    presets = dict(DEFAULT_PRESETS)
    if not isinstance(raw, dict):
        return presets
    for name, value in raw.items():
        if isinstance(value, str):
            presets[str(name)] = {"command": value, "parser": "auto", "cwd": None}
        elif isinstance(value, dict) and value.get("command"):
            cwd = value.get("cwd")
            presets[str(name)] = {
                "command": str(value["command"]),
                "parser": str(value.get("parser", "auto")),
                "timeout_ms": value.get("timeout_ms"),
                # Repository-relative sub-directory this preset targets (for
                # polyrepo workspaces); ``None``/absent means the workspace root.
                "cwd": str(cwd) if cwd is not None else None,
            }
    return presets


def load_repo_profile(settings: Settings) -> RepoProfile:
    path = settings.project_root / ".chatrepo" / "mcp.yml"
    errors: list[str] = []
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = _load_yaml_text(path.read_text(encoding="utf-8"))
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


def resolve_presets_for_dir(dir_rel: str, settings: Settings) -> dict[str, dict[str, Any]]:
    """Merge workspace-autodetected presets for ``dir_rel`` with profile overrides.

    ``workspace.resolve_presets_for`` supplies the stack-autodetected /
    Makefile-derived defaults for the directory; any named preset in the repo
    profile (``.chatrepo/mcp.yml`` ``presets`` section, or the generic
    built-in defaults) whose own ``cwd`` matches ``dir_rel`` is layered on top
    as an explicit, trusted override (profile wins on a name collision).
    """
    # Local import: workspace.py has no dependency on profile.py, so this
    # does not introduce an import cycle; kept local purely to mirror the
    # existing lazy-import style already used for optional PyYAML above.
    from .workspace import resolve_presets_for

    normalized_dir = dir_rel.strip("/")
    merged = dict(resolve_presets_for(normalized_dir, settings))
    profile = load_repo_profile(settings)
    for name, cfg in profile.presets.items():
        preset_dir = str(cfg.get("cwd") or "").strip("/")
        if preset_dir == normalized_dir:
            merged[name] = cfg
    return merged


def list_test_presets(settings: Settings, path: str | None = None) -> dict[str, Any]:
    """List available presets.

    Without ``path``: the repo-profile presets (built-ins + `.chatrepo/mcp.yml`
    overrides) plus a per-repo summary of autodetected actions across the
    whole workspace. With ``path``: the fully resolved action -> preset map
    for that specific workspace sub-directory (see `resolve_presets_for_dir`).
    """
    profile = load_repo_profile(settings)
    if path is not None:
        resolved = resolve_presets_for_dir(path, settings)
        return {
            "ok": not profile.errors,
            "profile_path": profile.path,
            "path": path.strip("/"),
            "presets": resolved,
            "count": len(resolved),
            "errors": profile.errors,
        }

    from .workspace import list_workspace_repos, resolve_presets_for

    repos = []
    for repo in list_workspace_repos(settings):
        dir_rel = str(repo.get("path", ""))
        repos.append({**repo, "actions": sorted(resolve_presets_for(dir_rel, settings))})

    return {
        "ok": not profile.errors,
        "profile_path": profile.path,
        "presets": profile.presets,
        "count": len(profile.presets),
        "repos": repos,
        "errors": profile.errors,
    }
