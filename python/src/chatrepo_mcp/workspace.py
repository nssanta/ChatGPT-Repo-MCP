"""Workspace / polyrepo scope helpers.

Pure filesystem logic: resolving allowed roots, locating the nearest git
toplevel, detecting a directory's tech stack, and resolving test/lint/build
presets for that stack (or a Makefile target when available). Intentionally
free of any dependency on ``server``/``command_tools`` to avoid import
cycles; only ``.config`` is used.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Settings

#: Directory names skipped while scanning for nested repos/stacks.
_IGNORED_DIR_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

#: Stack -> action -> shell command, used when no matching Makefile target exists.
STACK_PRESETS: dict[str, dict[str, str]] = {
    "go": {
        "test": "go test ./...",
        "lint": "go vet ./...",
        "build": "go build ./...",
        "format": "gofmt -l .",
    },
    "python": {
        "test": "pytest -x -q",
        "lint": "ruff check .",
        "typecheck": "mypy .",
        "format": "ruff format --check .",
    },
    "node": {
        "test": "npm test",
        "lint": "npm run lint --if-present",
        "typecheck": "npx tsc --noEmit",
        "build": "npm run build --if-present",
    },
    "rust": {
        "test": "cargo test",
        "lint": "cargo clippy",
        "build": "cargo build",
        "format": "cargo fmt --check",
    },
}

_MAKEFILE_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?!=)")


def resolve_roots(settings: Settings) -> list[Path]:
    """Return the list of filesystem roots the agent is allowed to touch."""
    if settings.filesystem_unrestricted:
        return [Path("/")]

    roots: list[Path] = [settings.project_root.resolve()]
    for raw in settings.workspace_roots:
        if not raw:
            continue
        candidate = Path(raw).expanduser().resolve()
        if candidate not in roots:
            roots.append(candidate)
    return roots


def is_within_roots(target: Path, roots: list[Path]) -> bool:
    """True if ``target`` lies inside at least one of ``roots``."""
    resolved = target.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _has_git_marker(directory: Path) -> bool:
    """True if ``directory`` contains a ``.git`` dir (repo) or file (worktree/submodule)."""
    git_path = directory / ".git"
    return git_path.is_dir() or git_path.is_file()


def find_git_toplevel(start: Path, roots: list[Path]) -> Path | None:
    """Walk upward from ``start`` looking for a directory containing ``.git``.

    Stops (returns ``None``) once the current directory is no longer within
    any of ``roots``. Pure filesystem walk, no subprocess/git invocation.
    """
    current = start.resolve()
    if not is_within_roots(current, roots):
        return None

    while True:
        if _has_git_marker(current):
            return current
        if not is_within_roots(current, roots):
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def detect_stack(directory: Path) -> list[str]:
    """Detect the tech stack(s) present in ``directory`` based on marker files."""
    stacks: list[str] = []

    if (directory / "go.mod").exists():
        stacks.append("go")

    if (
        (directory / "pyproject.toml").exists()
        or (directory / "setup.py").exists()
        or (directory / "requirements.txt").exists()
        or (directory / "Pipfile").exists()
    ):
        stacks.append("python")

    if (directory / "package.json").exists():
        stacks.append("node")
        if (directory / "tsconfig.json").exists():
            stacks.append("ts")

    if (directory / "Cargo.toml").exists():
        stacks.append("rust")

    if (directory / "Makefile").exists() or (directory / "makefile").exists():
        stacks.append("make")

    if (directory / "Dockerfile").exists() or any(
        directory.glob("docker-compose*.yml")
    ) or any(directory.glob("docker-compose*.yaml")):
        stacks.append("docker")

    return stacks


def makefile_targets(directory: Path) -> list[str]:
    """Parse target names out of a Makefile/makefile; best-effort, never raises."""
    makefile_path = directory / "Makefile"
    if not makefile_path.exists():
        makefile_path = directory / "makefile"
    if not makefile_path.exists():
        return []

    try:
        with makefile_path.open("rb") as handle:
            raw = handle.read(1_000_001)
        if len(raw) > 1_000_000:
            return []
        text = raw.decode("utf-8", errors="ignore")
    except OSError:
        return []

    targets: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not line or line[0].isspace():
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("."):
            # .PHONY, .DEFAULT, etc. are directives, not real targets.
            continue
        match = _MAKEFILE_TARGET_RE.match(stripped)
        if not match:
            continue
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        targets.append(name)
    return targets


def resolve_presets_for(dir_rel: str, settings: Settings) -> dict[str, dict]:
    """Resolve test/lint/typecheck/format/build presets for a workspace sub-directory.

    ``dir_rel`` is relative to ``settings.project_root`` ("" for the root
    itself). Makefile targets take priority over stack-derived defaults for
    a given action; `.chatrepo/mcp.yml` overrides are applied elsewhere.
    """
    candidate = Path(dir_rel).expanduser()
    directory = (
        candidate.resolve()
        if candidate.is_absolute()
        else (settings.project_root / candidate).resolve()
    )
    if not is_within_roots(directory, resolve_roots(settings)):
        return {}
    if not directory.is_dir():
        return {}
    try:
        preset_cwd = directory.relative_to(settings.project_root.resolve()).as_posix()
    except ValueError:
        preset_cwd = str(directory)
    if preset_cwd == ".":
        preset_cwd = ""

    targets = set(makefile_targets(directory))
    stacks = detect_stack(directory)

    presets: dict[str, dict] = {}

    # Stack-derived defaults first (merged across all detected stacks).
    for stack in stacks:
        for action, command in STACK_PRESETS.get(stack, {}).items():
            if action in presets:
                continue
            presets[action] = {"command": command, "cwd": preset_cwd, "parser": "auto"}

    # Makefile targets take priority when they match a known action name.
    known_actions = ("test", "lint", "typecheck", "format", "build")
    for action in known_actions:
        if action in targets:
            presets[action] = {
                "command": f"make {action}",
                "cwd": preset_cwd,
                "parser": "auto",
            }

    return presets


def list_workspace_repos(settings: Settings) -> list[dict]:
    """Scan the workspace (up to ``workspace_scan_depth`` levels) for git repos/stacks."""
    root = settings.project_root.resolve()

    if _has_git_marker(root):
        return [
            {
                "path": "",
                "stack": detect_stack(root),
                "is_git": True,
                "makefile_targets": makefile_targets(root),
            }
        ]

    found: list[dict] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > settings.workspace_scan_depth:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name in _IGNORED_DIR_NAMES:
                continue
            if _has_git_marker(entry):
                rel = entry.relative_to(root).as_posix()
                found.append(
                    {
                        "path": rel,
                        "stack": detect_stack(entry),
                        "is_git": True,
                        "makefile_targets": makefile_targets(entry),
                    }
                )
                continue
            _walk(entry, depth + 1)

    _walk(root, 1)

    if found:
        return found

    # No nested git repos found anywhere: surface the top-level structure
    # so the agent can still see what's there.
    top_level: list[dict] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name in _IGNORED_DIR_NAMES:
            continue
        rel = entry.relative_to(root).as_posix()
        top_level.append(
            {
                "path": rel,
                "stack": detect_stack(entry),
                "is_git": False,
                "makefile_targets": makefile_targets(entry),
            }
        )
    return top_level
