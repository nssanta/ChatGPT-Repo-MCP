from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .workspace import is_within_roots, resolve_roots


class SecurityError(ValueError):
    """Raised when a path is not allowed."""


@dataclass(frozen=True)
class ResolvedPath:
    """A validated path together with the allowed root that contains it."""

    target: Path
    root: Path
    relative: str
    display: str


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
    patterns = settings.blocked_globs
    if settings.allow_secret_access:
        secret_patterns = {normalize_rel_path(pattern) for pattern in settings.secret_globs}
        patterns = tuple(
            pattern for pattern in patterns if normalize_rel_path(pattern) not in secret_patterns
        )
    return matches_any_glob(rel_path, patterns)


def matches_any_glob(rel_path: str, patterns: tuple[str, ...]) -> bool:
    rel_path = normalize_rel_path(rel_path)
    parts = Path(rel_path).parts
    name = parts[-1] if parts else rel_path
    for pattern in patterns:
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


def is_secret_relative(rel_path: str, settings: Settings) -> bool:
    """True if ``rel_path`` (or its bare filename) matches ``settings.secret_globs``.

    It remains active under ``filesystem_unrestricted`` and is removed only
    by full mode with ``ALLOW_SECRET_ACCESS=true``.
    """
    if settings.allow_secret_access:
        return False
    rel_path = normalize_rel_path(rel_path)
    if matches_any_glob(rel_path, settings.secret_globs):
        return True
    parts = Path(rel_path).parts
    name = parts[-1] if parts else rel_path
    return matches_any_glob(name, settings.secret_globs)


def find_containing_root(target: Path, roots: list[Path]) -> Path:
    """Return the (resolved) root in ``roots`` that contains ``target``.

    Callers must have already established ``is_within_roots(target, roots)`` is
    true; if none of the roots contain ``target`` this raises ``SecurityError``.
    """
    resolved = target.resolve()
    for root in roots:
        root_resolved = root.resolve()
        try:
            resolved.relative_to(root_resolved)
            return root_resolved
        except ValueError:
            continue
    raise SecurityError(f"path escapes allowed roots: {target}")


def _resolve_against_project_root(candidate: str, settings: Settings) -> Path:
    """Resolve ``candidate`` to an absolute path.

    Absolute candidates are used as-is; relative candidates are resolved against
    ``settings.project_root`` (unchanged legacy behavior).
    """
    candidate_path = Path(candidate)
    if candidate_path.is_absolute():
        return candidate_path.resolve()
    return (settings.project_root.resolve() / candidate).resolve()


def display_path(target: Path, settings: Settings) -> str:
    """Use project-relative paths inside PROJECT_ROOT and absolute paths elsewhere."""
    resolved = target.resolve()
    try:
        return resolved.relative_to(settings.project_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def resolve_path_context(
    candidate: str,
    settings: Settings,
    *,
    allow_hidden: bool = False,
) -> ResolvedPath:
    """Resolve and validate a path without losing which allowed root contains it."""
    if not candidate:
        candidate = "."
    roots = resolve_roots(settings)
    target = _resolve_against_project_root(candidate, settings)
    if not is_within_roots(target, roots):
        raise SecurityError(f"path escapes allowed roots: {candidate}")

    root = find_containing_root(target, roots)
    rel = rel_posix(root, target)
    if is_secret_relative(rel, settings):
        raise SecurityError(f"path is blocked by secret policy: {display_path(target, settings)}")
    if rel != "." and not is_allowed_relative(rel, settings, allow_hidden=allow_hidden):
        if not allow_hidden and is_hidden_relative(rel):
            raise SecurityError(f"hidden paths are not allowed: {display_path(target, settings)}")
        raise SecurityError(f"path is blocked by security policy: {display_path(target, settings)}")
    return ResolvedPath(
        target=target,
        root=root,
        relative=rel,
        display=display_path(target, settings),
    )


def resolve_repo_path(candidate: str, settings: Settings, *, allow_hidden: bool = False) -> Path:
    return resolve_path_context(candidate, settings, allow_hidden=allow_hidden).target
