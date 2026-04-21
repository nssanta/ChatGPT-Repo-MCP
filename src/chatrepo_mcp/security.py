from __future__ import annotations

import fnmatch
from pathlib import Path

from .config import Settings


class SecurityError(ValueError):
    """Raised when a path is not allowed."""


def rel_posix(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    return rel.as_posix()


def is_hidden_relative(rel_path: str) -> bool:
    return any(part.startswith(".") for part in Path(rel_path).parts)


def is_blocked_relative(rel_path: str, settings: Settings) -> bool:
    if rel_path.startswith("./"):
        rel_path = rel_path[2:]
    for pattern in settings.blocked_globs:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(rel_path, pattern[3:]):
            return True
    return False


def resolve_repo_path(candidate: str, settings: Settings, *, allow_hidden: bool = False) -> Path:
    if not candidate:
        candidate = "."
    root = settings.project_root.resolve()
    target = (root / candidate).resolve()

    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SecurityError(f"path escapes repository root: {candidate}") from exc

    rel = rel_posix(root, target)
    if rel == ".":
        return target

    if not allow_hidden and is_hidden_relative(rel):
        raise SecurityError(f"hidden paths are not allowed: {rel}")

    if is_blocked_relative(rel, settings):
        raise SecurityError(f"path is blocked by security policy: {rel}")

    return target
