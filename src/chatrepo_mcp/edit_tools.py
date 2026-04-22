from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Any

from .config import Settings
from .security import SecurityError, matches_any_glob, normalize_rel_path, rel_posix, resolve_repo_path


class WritePolicyError(ValueError):
    """Raised when a write request violates the repository write policy."""


class StaleWriteError(ValueError):
    """Raised when expected_sha256 does not match the current file contents."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_delta(old_text: str, new_text: str) -> tuple[int, int]:
    added = 0
    removed = 0
    for line in difflib.ndiff(old_text.splitlines(), new_text.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


def _unified_diff(path: str, old_text: str, new_text: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )


def _read_existing_text(path: Path, settings: Settings) -> str:
    data = path.read_bytes()
    if b"\x00" in data:
        raise WritePolicyError(f"binary files are not writable: {path.name}")
    if len(data) > settings.max_write_file_bytes:
        raise WritePolicyError(
            f"file exceeds MAX_WRITE_FILE_BYTES ({len(data)} > {settings.max_write_file_bytes}): {path.name}"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WritePolicyError(f"file is not valid UTF-8: {path.name}") from exc


def _validate_new_text(path: str, text: str, settings: Settings) -> None:
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WritePolicyError(f"content is not UTF-8 encodable: {path}") from exc
    if b"\x00" in encoded:
        raise WritePolicyError(f"binary content is not writable: {path}")
    if len(encoded) > settings.max_write_file_bytes:
        raise WritePolicyError(
            f"content exceeds MAX_WRITE_FILE_BYTES ({len(encoded)} > {settings.max_write_file_bytes}): {path}"
        )


def _is_writable_relative(rel_path: str, settings: Settings) -> bool:
    rel_path = normalize_rel_path(rel_path)
    if matches_any_glob(rel_path, settings.blocked_globs):
        return False
    if "*" in settings.writable_globs and not settings.dangerously_allow_all_writes:
        return False
    return settings.dangerously_allow_all_writes and "*" in settings.writable_globs or matches_any_glob(
        rel_path, settings.writable_globs
    )


def resolve_write_path(
    path: str,
    settings: Settings,
    *,
    create_if_missing: bool = False,
) -> tuple[Path, str]:
    root = settings.project_root.resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SecurityError(f"path escapes repository root: {path}") from exc

    rel = rel_posix(root, target) if target.exists() else normalize_rel_path(path)
    if not rel or rel == ".":
        raise WritePolicyError("path must point to a repo file")
    if target.exists() and not target.is_file():
        raise WritePolicyError(f"not a file: {rel}")
    if target.is_symlink():
        raise WritePolicyError(f"symlink writes are not allowed: {rel}")
    if not target.exists() and not create_if_missing:
        raise FileNotFoundError(f"file does not exist: {rel}")
    if not _is_writable_relative(rel, settings):
        raise WritePolicyError(f"path is not writable by policy: {rel}")
    if target.parent.exists() and not target.parent.is_dir():
        raise WritePolicyError(f"parent path is not a directory: {rel}")
    if not target.parent.exists():
        parent_rel = rel_posix(root, target.parent) if target.parent.exists() else normalize_rel_path(str(Path(rel).parent))
        if not _is_writable_relative(f"{parent_rel}/placeholder", settings):
            raise WritePolicyError(f"parent path is not writable by policy: {parent_rel}")
    return target, rel


def _check_expected_hash(old_sha: str | None, expected_sha256: str | None, settings: Settings, path: str) -> None:
    if old_sha is None and expected_sha256 is None:
        return
    if settings.require_expected_hash_for_writes and not expected_sha256:
        raise StaleWriteError(f"expected_sha256 is required for writes: {path}")
    if expected_sha256 and old_sha != expected_sha256:
        raise StaleWriteError(f"stale write rejected for {path}: expected {expected_sha256}, current {old_sha}")


def _build_result(path: str, old_text: str, new_text: str, *, dry_run: bool) -> dict[str, Any]:
    old_sha = sha256_text(old_text)
    new_sha = sha256_text(new_text)
    added, removed = _line_delta(old_text, new_text)
    return {
        "path": path,
        "changed": old_text != new_text,
        "dry_run": dry_run,
        "old_sha256": old_sha,
        "new_sha256": new_sha,
        "diff_unified": _unified_diff(path, old_text, new_text),
        "lines_added": added,
        "lines_removed": removed,
    }


def _apply_text_change(
    path: str,
    settings: Settings,
    new_text: str,
    *,
    create_if_missing: bool,
    expected_sha256: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings, create_if_missing=create_if_missing)
    old_text = _read_existing_text(target, settings) if target.exists() else ""
    old_sha = sha256_text(old_text) if target.exists() else None
    _check_expected_hash(old_sha, expected_sha256, settings, rel)
    _validate_new_text(rel, new_text, settings)
    result = _build_result(rel, old_text, new_text, dry_run=dry_run)
    if result["changed"] and not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_text, encoding="utf-8")
    return result


def write_text_file(
    path: str,
    content: str,
    settings: Settings,
    *,
    create_if_missing: bool = False,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    return _apply_text_change(
        path,
        settings,
        content,
        create_if_missing=create_if_missing,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


def replace_text_in_file(
    path: str,
    find: str,
    replace: str,
    settings: Settings,
    *,
    replace_all: bool = False,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    if not find:
        raise ValueError("find must not be empty")
    if find not in old_text:
        raise ValueError(f"text fragment not found in {rel}")
    new_text = old_text.replace(find, replace) if replace_all else old_text.replace(find, replace, 1)
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)


def insert_text_in_file(
    path: str,
    anchor: str,
    position: str,
    content: str,
    settings: Settings,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    if position not in {"before", "after"}:
        raise ValueError("position must be 'before' or 'after'")
    idx = old_text.find(anchor)
    if idx < 0:
        raise ValueError(f"anchor not found in {rel}")
    insert_at = idx if position == "before" else idx + len(anchor)
    new_text = old_text[:insert_at] + content + old_text[insert_at:]
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)


def delete_text_in_file(
    path: str,
    settings: Settings,
    *,
    find: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    if find is not None:
        if not find:
            raise ValueError("find must not be empty")
        if find not in old_text:
            raise ValueError(f"text fragment not found in {rel}")
        new_text = old_text.replace(find, "", 1)
    else:
        if start_line is None or end_line is None:
            raise ValueError("provide either find or start_line/end_line")
        lines = old_text.splitlines(keepends=True)
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            raise ValueError("invalid line range")
        del lines[start_line - 1 : end_line]
        new_text = "".join(lines)
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)
