from __future__ import annotations

import fnmatch
from pathlib import Path

from .config import Settings


class SecurityError(ValueError):
    """Raised when a path is not allowed."""


def rel_posix(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    return rel.as_posix()


def rel_posix_lexical(root: Path, path: Path) -> str:
    rel = path.absolute().relative_to(root.resolve())
    return rel.as_posix()


def is_hidden_relative(rel_path: str) -> bool:
    return any(part.startswith(".") for part in Path(rel_path).parts)


def normalize_rel_path(rel_path: str) -> str:
    if rel_path.startswith("./"):
        rel_path = rel_path[2:]
    return rel_path.strip("/")


def is_blocked_relative(rel_path: str, settings: Settings) -> bool:
    rel_path = normalize_rel_path(rel_path)
    parts = Path(rel_path).parts
    name = parts[-1] if parts else rel_path
    for pattern in settings.blocked_globs:
        pattern = normalize_rel_path(pattern)
        if pattern.startswith("**/") and pattern.endswith("/**"):
            blocked_part = pattern[3:-3]
            if blocked_part in parts:
                return True
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatch(name, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(rel_path, pattern[3:]):
            return True
    return False


def is_allowed_relative(rel_path: str, settings: Settings, *, allow_hidden: bool = False) -> bool:
    rel_path = normalize_rel_path(rel_path)
    if rel_path == ".":
        return True
    if not allow_hidden and is_hidden_relative(rel_path):
        return False
    return not is_blocked_relative(rel_path, settings)


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

    if not is_allowed_relative(rel, settings, allow_hidden=allow_hidden):
        if not allow_hidden and is_hidden_relative(rel):
            raise SecurityError(f"hidden paths are not allowed: {rel}")
        raise SecurityError(f"path is blocked by security policy: {rel}")

    return target
